"""Command line: ``python -m adef_operate demo [--home DIR]`` and ``python -m adef_operate faults``.

``demo`` onboards both golden products into a fresh workspace, records the OG0 and OG1
approvals as the named approvers in the synthetic packs (simulation only), runs every
library fault through the monitor engine, and prints what fired.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from .connectors.simulator import FAULTS, SimulatorAdapter
from .engine.approvals import Approvals
from .engine.handover import accept_handover
from .engine.monitor_engine import MonitorEngine
from .engine.onboarding_tools import draft_service, readiness, write_service
from .packs import handover_pack


def _approve_as_pack_owners(approvals: Approvals, request_id: str, owners: dict) -> None:
    req = approvals.get_request(request_id)
    for role in req.approver_roles:
        approvals.decide(request_id, approver=owners[role], role=role, decision="approved",
                         artifact_hash=req.artifact_hash, reason="Demo Simulator: synthetic approver")


def demo(home: Path) -> int:
    print(f"ADEF_HOME = {home}\n")
    for pack_name in ("utilities", "telco"):
        pack = handover_pack(pack_name)
        res = accept_handover(pack, home=home, simulated=True)
        if not res.accepted:
            print(f"{pack_name}: handover rejected: {res.gaps}")
            return 1
        ws, appr = res.workspace, Approvals(res.workspace)
        _approve_as_pack_owners(appr, res.og0_request, pack["owners"])
        write_service(ws, draft_service(pack))
        rep = readiness(ws, pack_name)
        _approve_as_pack_owners(appr, appr.request_og1().id, pack["owners"])
        print(f"== {pack['dp_id']} ({pack_name}): {rep['monitors']} monitors, "
              f"{len(rep['healthy_alerts'])} alerts at rest, {appr.og1_status()[1]}")
        for fid, fault in FAULTS.items():
            if pack_name not in fault.packs:
                continue
            sim = SimulatorAdapter()
            sim.inject(fid, pack_name)
            sigs = MonitorEngine(ws, sim, simulated=True).run()["signals"]
            fired = sorted({f"{s['signal_class']}:{s['object']}" for s in sigs})
            print(f"  {fid} {fault.description}\n     -> {len(sigs)} signals: {', '.join(fired)}")
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
