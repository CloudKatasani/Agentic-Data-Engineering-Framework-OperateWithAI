"""A 14-day shadow-mode rehearsal on synthetic exports (Demo Simulator in a Build profile's
native shapes). It exercises the whole Day 10 path: START HERE, read-only onboarding from
exports, daily ticks, the team's conclusions, observed recovery, post-incident notes, the
shadow report and the go-live rule. Its results are SYNTHETIC and are labelled as such; they
can never support go-live for a real product.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Optional

from .connectors.simulator import SIM_NOW, FAULTS, SimulatorAdapter
from .engine import shadow
from .engine.approvals import Approvals
from .engine.council import Council
from .engine.workspace import Workspace
from .packs import handover_pack
from .shadow_mode import ShadowRuntime
from .stack import NativePlatform, load_profile
from .stack.export import export_snapshot

# day -> fault for the Utilities golden product (all faults that apply to the pack)
SCHEDULE = {1: "F-01", 3: "F-02", 5: "F-05", 7: "F-08", 9: "F-01", 11: "F-06", 12: "F-07"}
TEAM_DELAY = timedelta(minutes=25)  # the team's own ticket opens this long after the snapshot


def rehearsal_answers(pack: dict, profile_id: str) -> dict:
    ids = {s: {"identity": f"svc_operate_{s}_ro", "read_only": True, "verified_by": pack["owners"]["platform_owner"],
               "verified_on": "2026-01-02"} for s in shadow.REQUIRED_SLOT_IDENTITIES}
    return {"first_service": {"dp_id": pack["dp_id"], "non_production": True},
            "telemetry": {"stack_profile": profile_id, "access_method": "export-folder", "identities": ids},
            "people": {"on_call": pack["on_call"], "approvers": dict(pack["owners"])},
            "idempotency": {"declared_idempotent": ["meter_ingest", "outage_ingest"],
                            "evidence": {"meter_ingest": "runbook#idempotency", "outage_ingest": "runbook#idempotency"}},
            "baseline": {"time_to_detect_min": 45, "time_to_recover_min": 240, "monthly_incidents": 6},
            "suite_assumptions_confirmed": True}


def snap_id(d) -> str:
    return d.strftime("%Y-%m-%dT%H%M")


def run_rehearsal(home: Optional[str | Path] = None, profile_id: str = "example-a", days: int = 14) -> dict:
    base = Path(home or tempfile.mkdtemp(prefix="adef-shadow-"))
    exports, ops_home = base / "exports", base / "adef"
    profile = load_profile(profile_id)
    pack = handover_pack("utilities")
    dp = pack["dp_id"]

    def export_day(d: int, fault: Optional[str]) -> str:
        now = SIM_NOW + timedelta(days=d)
        sim = SimulatorAdapter(now)
        if fault:
            sim.inject(fault, "utilities")
        export_snapshot(NativePlatform(profile, sim), dp, exports, snap_id(now), synthetic=True)
        return snap_id(now)

    for d in range(-7, 0):  # a week of pre-shadow history for the dry run
        export_day(d, None)
    rt = ShadowRuntime(ops_home, profile_id, exports)
    answers = rehearsal_answers(pack, profile_id)
    rel = rt.release(pack, answers)
    if not rel["accepted"]:
        raise ValueError(f"rejected: {rel['gaps']}")
    ws = Workspace(dp, ops_home)
    approve = lambda rid: [Approvals(ws).decide(rid, approver=pack["owners"][r], role=r, decision="approved",
                                                artifact_hash=Approvals(ws).get_request(rid).artifact_hash,
                                                reason="rehearsal approver")
                           for r in Approvals(ws).get_request(rid).approver_roles]
    approve(rel["og0_request"])
    ob = rt.onboard(dp)
    if not ob.ready:
        raise ValueError(f"not ready: {ob.problems}")
    approve(ob.og1_request)

    log, ticket_no = [], 0
    for d in range(days):
        fault = SCHEDULE.get(d)
        sid = export_day(d, fault)
        out = rt.tick(dp, sid)
        log.append({"day": d, "snapshot": sid, "fault": fault, "signals": len(out["signals"]),
                    "opened": out["cases_opened"], "recovered": out["recovered"],
                    "routed": [o.get("routed_to") for o in out["outcomes"] if o["outcome"] == "routed"]})
        now = SIM_NOW + timedelta(days=d)
        for cid in out["cases_opened"]:
            case = Workspace(dp, ops_home).read_yaml(f"cases/{cid}/case.yaml")
            if case["severity"] == "S1":  # independent council reviewers (people), not team agents
                for r in ("Reviewer A", "Reviewer B"):
                    Council(ws).record(cid, reviewer=r, agrees=True, note="rehearsal")
            ticket_no += 1
            shadow.record_conclusion(ws, by=pack["on_call"], ticket=f"TCK-{ticket_no:04d}", case_id=cid,
                                     opened_at=(now + TEAM_DELAY).isoformat(), cause_class=FAULTS[fault].cause_class,
                                     real_problem=True, minutes_spent=35, note="team conclusion (synthetic)")
    # One problem the team saw that the agents did not (a consumer-reported number with no signal).
    ticket_no += 1
    shadow.record_conclusion(ws, by=pack["owners"]["service_owner"], ticket=f"TCK-{ticket_no:04d}",
                             opened_at=(SIM_NOW + timedelta(days=6, hours=3)).isoformat(), cause_class="source data",
                             real_problem=True, minutes_spent=50, note="consumer-reported; no monitor covers it")
    rt.run_new_snapshots(dp)
    rep, path = rt.report(dp)
    return {"home": str(base), "workspace": str(ws.root), "log": log, "report": rep, "report_path": path,
            "readiness": ob.report}
