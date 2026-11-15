"""Command line: ``python -m adef_operate demo [--home DIR]`` and ``python -m adef_operate faults``.

``demo`` releases and onboards both golden products, records OG0 and OG1 as the synthetic
approvers named in the packs (simulation only), then injects each library fault and runs
one control-plane tick: monitor engine -> Sentinel grouping -> Commander case opening.
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
from .runtime import OpsRuntime


def _approve_as_pack_owners(approvals: Approvals, request_id: str, owners: dict) -> None:
    req = approvals.get_request(request_id)
    for role in req.approver_roles:
        approvals.decide(request_id, approver=owners[role], role=role, decision="approved",
                         artifact_hash=req.artifact_hash, reason="Demo Simulator: synthetic approver")


def demo(home: Path) -> int:
    print(f"ADEF_HOME = {home}\n(Demo Simulator: approvals are recorded as the synthetic approvers in the packs)\n")
    for pack_name in ("utilities", "telco"):
        pack = handover_pack(pack_name)
        for fid, fault in FAULTS.items():
            if pack_name not in fault.packs:
                continue
            sim = SimulatorAdapter()
            rt = OpsRuntime(home / f"{pack_name}-{fid}", sim)
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
            for o in out["outcomes"]:
                if o["outcome"] == "opened":
                    print(f"     {o['case']} {o['rule']} | trust: {o.get('trust_status') or 'unchanged'}")
                else:
                    print(f"     {o['outcome']} -> {o.get('routed_to', '')}"
                          + (f" (escalated to {o['escalated_to']})" if o.get("escalated_to") else ""))
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="adef_operate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="onboard golden products and run the fault library")
    d.add_argument("--home", type=Path, default=None)
    sub.add_parser("faults", help="list the Demo Simulator fault library")
    args = ap.parse_args(argv)
    if args.cmd == "faults":
        for f in FAULTS.values():
            print(f"{f.id}  {'/'.join(f.packs):18} {f.description}  [cause: {f.cause_class}]")
        return 0
    home = args.home or Path(tempfile.mkdtemp(prefix="adef-demo-"))
    return demo(home)


if __name__ == "__main__":
    sys.exit(main())
