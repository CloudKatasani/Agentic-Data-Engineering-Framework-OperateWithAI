"""Command line: ``python -m adef_operate demo [--home DIR]`` and ``python -m adef_operate faults``.

``demo`` releases and onboards both golden products, records OG0 and OG1 as the synthetic
approvers named in the packs (simulation only), then injects each library fault and runs
one control-plane tick: monitor engine -> Sentinel grouping -> Commander case opening,
then diagnoses each group.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from .connectors.simulator import FAULTS, SimulatorAdapter
from .engine.approvals import Approvals
from .connectors.simulator import SIM_NOW
from .packs import handover_pack
from .agents import DiagnosisAgent
from .runtime import OpsRuntime
from .stack import StackAdapter, list_profiles, load_profile, operate_binding, validate_operate


def _approve_as_pack_owners(approvals: Approvals, request_id: str, owners: dict) -> None:
    req = approvals.get_request(request_id)
    for role in req.approver_roles:
        approvals.decide(request_id, approver=owners[role], role=role, decision="approved",
                         artifact_hash=req.artifact_hash, reason="Demo Simulator: synthetic approver")


def demo(home: Path, profile: str | None = None) -> int:
    print(f"ADEF_HOME = {home}\n(Demo Simulator: approvals are recorded as the synthetic approvers in the packs)")
    print(f"Stack Profile: {profile or 'canonical simulator'}\n")
    for pack_name in ("utilities", "telco"):
        pack = handover_pack(pack_name)
        for fid, fault in FAULTS.items():
            if pack_name not in fault.packs:
                continue
            sim = SimulatorAdapter()
            prof = load_profile(profile) if profile else None
            adapter = StackAdapter.over(prof, sim) if prof else sim
            rt = OpsRuntime(home / f"{pack_name}-{fid}", adapter, stack_profile=prof,
                            sandbox_factory=(lambda: StackAdapter.over(prof, SimulatorAdapter())) if prof else None)
            res = rt.release({"type": "product.released", "handover_pack": pack})
            if not res.accepted:
                print(f"{pack_name}: handover rejected: {res.gaps}")
                return 1
            appr = Approvals(res.workspace)
            _approve_as_pack_owners(appr, res.og0_request, pack["owners"])
            ob = rt.onboard(pack["dp_id"])
            if not ob.ready:
                print(f"{pack_name}: not ready for OG1: {ob.problems}")
                return 1
            _approve_as_pack_owners(appr, ob.og1_request, pack["owners"])
            if fid == next(f for f, x in FAULTS.items() if pack_name in x.packs):
                print(f"== {pack['dp_id']} ({pack_name}): {ob.report['monitors']} monitors, "
                      f"expected {ob.report['expected_alerts_per_week']} alerts/week from a 28-day dry run, "
                      f"{appr.og1_status()[1]}")
            sim.inject(fid, pack_name)
            out = rt.tick(pack["dp_id"], SIM_NOW)
            print(f"  {fid} {fault.description}")
            print(f"     {len(out['signals'])} signals -> {len(out['groups'])} group(s); first failing: "
                  f"{', '.join(g['first_failing_object'] for g in out['groups'])}")
            diag = DiagnosisAgent(res.workspace, adapter)
            for o in out["outcomes"]:
                r = diag.diagnose(o["case"]) if o.get("case") else diag.diagnose_group(
                    next(g for g in out["groups"] if g["id"] == o["group"]))
                cause = (f"     diagnosis: {r.cause_class} ({r.confidence}), {len(r.supporting_evidence)} supporting "
                         f"evidence, {len(r.ruled_out)} ruled out [label: {fault.cause_class}]")
                if o["outcome"] == "opened":
                    print(f"     {o['case']} {o['rule']} | trust: {o.get('trust_status') or 'unchanged'}")
                    print(cause)
                    continue
                print(f"     {o['outcome']} -> {o.get('routed_to', '')}"
                      + (f" (escalated to {o['escalated_to']})" if o.get("escalated_to") else ""))
                print(cause)
            fin = out.get("reviews", {}).get("finops")
            for f in (fin or {}).get("findings", []):
                print(f"     FinOps {f['id']}: {f['title']}; saving {f['monthly_low']:,.2f} to "
                      f"{f['monthly_high']:,.2f}/month; {f['approval']} OG4 pending")
            gov = out.get("reviews", {}).get("governance")
            for g in (gov or {}).get("raised", []):
                print(f"     Governance {g['id']}: {g['type']}"
                      f"{' (suspected exposure, escalated)' if g['exposure'] else ''}; {g['approval']} OG5 pending; "
                      "no agent action")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="adef_operate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="onboard golden products and run the fault library")
    d.add_argument("--home", type=Path, default=None)
    d.add_argument("--profile", default=None, help="Build-With-AI Stack Profile id (see `profiles`)")
    sub.add_parser("faults", help="list the Demo Simulator fault library")
    sub.add_parser("profiles", help="list Stack Profiles and validate Operate's slot binding")
    g = sub.add_parser("guardrails", help="run guardrail scenarios OT-01 to OT-16")
    g.add_argument("--profile", default=None)
    g.add_argument("--home", type=Path, default=None)
    e = sub.add_parser("evals", help="run the replay harness over the fault library")
    e.add_argument("--profiles", default="canonical", help="comma list, or 'all'")
    e.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.cmd == "profiles":
        for pid in list_profiles():
            p = load_profile(pid)
            v = validate_operate(p)
            b = operate_binding(p)
            print(f"{pid:16} {p.get('label', '')}")
            print(f"{'':16} orchestration={b['orchestration']['orchestration']} "
                  f"lake={b['data_platform']['table_format']} serving={b['data_platform']['compute_engine']} "
                  f"dq={b['data_quality']['data_quality']} catalog={b['catalog']['catalog_governance']}")
            print(f"{'':16} {'OK' if not v['blocking'] else 'BLOCKED: ' + '; '.join(v['blocking'])}")
        return 0
    if args.cmd == "guardrails":
        from .evals.guardrails import report, run_guardrails
        res = run_guardrails(args.home, args.profile)
        print(report(res, args.profile))
        return 0 if all(r.passed for r in res) else 1
    if args.cmd == "evals":
        from .evals.harness import render, run_harness
        profs = list_profiles() if args.profiles == "all" else args.profiles.split(",")
        profs = [None if p == "canonical" else p for p in profs]
        res = run_harness(None, profs, args.out)
        print(render(res))
        m = res["measures"]
        ok = (all(v >= 7 for v in m["diagnosis_faults_correct"].values()) and m["unsupported_causes"] == 0
              and m["guardrail_violations_reaching_execution"] == 0 and not m["rebind_mismatches"])
        return 0 if ok else 1
    if args.cmd == "faults":
        for f in FAULTS.values():
            print(f"{f.id}  {'/'.join(f.packs):18} {f.description}  [cause: {f.cause_class}]")
        return 0
    home = args.home or Path(tempfile.mkdtemp(prefix="adef-demo-"))
    return demo(home, getattr(args, "profile", None))


if __name__ == "__main__":
    sys.exit(main())
