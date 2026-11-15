"""Command line: ``python -m adef_operate demo [--home DIR]`` and ``python -m adef_operate faults``.

``demo`` releases and onboards both golden products, records OG0 and OG1 as the synthetic
approvers named in the packs (simulation only), then injects each library fault and runs
one control-plane tick: monitor engine -> Sentinel grouping -> Commander case opening,
then diagnoses each group.
"""

from __future__ import annotations

import argparse
import os
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
    sv = sub.add_parser("serve", help="run the section 11 API and the web console")
    sv.add_argument("--home", type=Path, default=None, help="ADEF_HOME (default: $ADEF_HOME or ~/.adef)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8090)
    sv.add_argument("--demo", action="store_true", help="seed both golden products to OG1 (Demo Simulator)")
    sv.add_argument("--profile", default=None, help="Stack Profile for --demo")
    t = sub.add_parser("start-here", help="write the START HERE answers template")
    t.add_argument("--dp", default="<dp_id>")
    t.add_argument("--profile", default="<stack profile id>")
    t.add_argument("--out", type=Path, default=None)
    r = sub.add_parser("shadow-rehearsal", help="14-day shadow-mode rehearsal on synthetic exports")
    r.add_argument("--profile", default="example-a")
    r.add_argument("--home", type=Path, default=None)
    sh = sub.add_parser("shadow", help="shadow mode on a real product from read-only exports")
    sh.add_argument("action", choices=["release", "onboard", "tick", "report", "go-live-request", "go-live-apply"])
    sh.add_argument("--home", type=Path, required=True, help="ADEF_HOME for the ops workspaces")
    sh.add_argument("--profile", help="Stack Profile id")
    sh.add_argument("--exports", type=Path, help="export root (<root>/<dp_id>/<snapshot>/)")
    sh.add_argument("--pack", type=Path, help="Operations Handover Pack (YAML) from Build stage 7")
    sh.add_argument("--answers", type=Path, help="START HERE answers (YAML)")
    sh.add_argument("--dp")
    dc = sub.add_parser("decide", help="record a named person's decision on a gate request")
    dc.add_argument("--home", type=Path, required=True)
    dc.add_argument("--dp", required=True)
    dc.add_argument("--request", required=True)
    dc.add_argument("--as", dest="person", required=True)
    dc.add_argument("--role", required=True)
    dc.add_argument("--decision", choices=["approved", "rejected"], required=True)
    dc.add_argument("--reason", default="")
    cc = sub.add_parser("conclude", help="record the team's own conclusion (shadow mode)")
    cc.add_argument("--home", type=Path, required=True)
    cc.add_argument("--dp", required=True)
    cc.add_argument("--by", required=True)
    cc.add_argument("--ticket")
    cc.add_argument("--case")
    cc.add_argument("--opened-at")
    cc.add_argument("--cause")
    cc.add_argument("--real", choices=["yes", "no"])
    cc.add_argument("--minutes", type=float)
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        import uvicorn
        from .api.app import create_app, seed_demo
        from .engine.workspace import adef_home
        home = args.home or adef_home()
        if args.demo:
            seeded = seed_demo(home, args.profile)
            print(f"Demo Simulator products ready: {seeded or 'already present'}")
        if args.host not in ("127.0.0.1", "localhost") and not (os.environ.get("ADEF_API_TOKEN")
                                                                 or os.environ.get("ADEF_IDENTITY_FILE")):
            print("Refusing to listen beyond localhost without ADEF_API_TOKEN or ADEF_IDENTITY_FILE.")
            return 2
        print(f"Console: http://{args.host}:{args.port}/   API docs: http://{args.host}:{args.port}/docs   home: {home}")
        uvicorn.run(create_app(home), host=args.host, port=args.port, log_level="warning")
        return 0
    if args.cmd == "start-here":
        from .engine.shadow import start_here_template
        text = start_here_template(args.dp, args.profile)
        if args.out:
            args.out.write_text(text)
            print(f"wrote {args.out}")
        else:
            print(text)
        return 0
    if args.cmd == "shadow-rehearsal":
        from .shadow_rehearsal import run_rehearsal
        res = run_rehearsal(args.home, args.profile)
        for l in res["log"]:
            print(f"day {l['day']:2} {l['snapshot']} fault={l['fault'] or '-':5} signals={l['signals']} "
                  f"opened={l['opened']} recovered={l['recovered']} routed={l['routed']}")
        print()
        print((Path(res["workspace"]) / res["report_path"]).read_text())
        return 0
    if args.cmd == "decide":
        from .engine.approvals import Approvals
        from .engine.workspace import Workspace
        ws = Workspace(args.dp, args.home)
        a = Approvals(ws)
        req = a.get_request(args.request)
        a.decide(args.request, approver=args.person, role=args.role, decision=args.decision,
                 artifact_hash=req.artifact_hash, reason=args.reason)
        print(f"{args.request} {req.gate}: {a.status(args.request)}")
        return 0
    if args.cmd == "conclude":
        from .engine.shadow import record_conclusion
        from .engine.workspace import Workspace
        rec = record_conclusion(Workspace(args.dp, args.home), by=args.by, ticket=args.ticket, case_id=args.case,
                                opened_at=args.opened_at, cause_class=args.cause,
                                real_problem=None if args.real is None else args.real == "yes",
                                minutes_spent=args.minutes)
        print(f"recorded: {rec}")
        return 0
    if args.cmd == "shadow":
        import yaml as _y
        from .engine import shadow as _sh
        from .engine.workspace import Workspace
        from .shadow_mode import ShadowRuntime
        if args.action in ("release", "onboard", "tick"):
            if not (args.profile and args.exports):
                ap.error("--profile and --exports are required")
            rt = ShadowRuntime(args.home, args.profile, args.exports)
        if args.action == "release":
            pack, answers = _y.safe_load(args.pack.read_text()), _y.safe_load(args.answers.read_text())
            res = rt.release(pack, answers)
            print(_y.safe_dump(res, sort_keys=False))
            return 0 if res["accepted"] else 1
        if args.action == "onboard":
            ob = rt.onboard(args.dp)
            print(f"ready={ob.ready} og1_request={ob.og1_request} problems={ob.problems}")
            return 0 if ob.ready else 1
        if args.action == "tick":
            for out in rt.run_new_snapshots(args.dp):
                print(f"opened={out['cases_opened']} recovered={out['recovered']} signals={len(out['signals'])}")
            return 0
        ws = Workspace(args.dp, args.home)
        if args.action == "report":
            rep = _sh.shadow_report(ws)
            path = _sh.write_report(ws, rep)
            print(ws.read(path))
            return 0
        if args.action == "go-live-request":
            rep = _sh.shadow_report(ws)
            print("requested", _sh.request_go_live(ws, _sh.write_report(ws, rep)))
            return 0
        _sh.apply_go_live(ws)
        print(f"{args.dp} is live; action types stay at L1 until each is promoted at OG7")
        return 0
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
