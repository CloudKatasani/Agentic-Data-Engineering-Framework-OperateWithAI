"""Shadow-mode runtime: onboard a real (non-production) product read-only from exports and
run the control plane on each new snapshot. Agents detect, group, open cases, diagnose and
draft options; people keep running their own process and record their conclusions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from .agents import DiagnosisAgent, RemediationAgent
from .core.errors import AdefError
from .engine import shadow
from .engine.workspace import Workspace
from .runtime import OpsRuntime
from .stack.export import ExportAdapter
from .stack.profiles import load_profile


class ShadowRuntime:
    def __init__(self, home: str | Path, profile_id: str, export_root: str | Path):
        self.home = Path(home)
        self.profile = load_profile(profile_id)
        self.export_root = Path(export_root)

    def adapter(self, dp_id: str, snapshot: Optional[str] = None) -> ExportAdapter:
        return ExportAdapter(self.profile, self.export_root / dp_id, snapshot)

    # -- onboarding (ends at OG0, then OG1) -----------------------------------------------------
    def release(self, pack: dict, answers: dict) -> dict:
        gaps = shadow.validate_start_here(answers, pack, self.profile)
        if gaps["blocking"]:
            return {"accepted": False, "gaps": gaps["blocking"], "warnings": gaps["warnings"]}
        adapter = self.adapter(pack["dp_id"])
        origin = "synthetic-export" if adapter.synthetic else "real-export"
        effective = shadow.apply_start_here(pack, answers)
        rt = OpsRuntime(self.home, adapter, simulated=adapter.synthetic, stack_profile=self.profile)
        res = rt.release({"type": "product.released", "handover_pack": effective})
        if not res.accepted:
            return {"accepted": False, "gaps": res.gaps, "warnings": gaps["warnings"]}
        shadow.enter_shadow(res.workspace, answers, data_origin=origin)
        res.workspace.write_yaml("shadow", "shadow/start.yaml", {"after_snapshot": adapter.snapshot,
                                                                  "profile_id": self.profile["profile_id"]})
        return {"accepted": True, "og0_request": res.og0_request, "warnings": gaps["warnings"],
                "data_origin": origin}

    def onboard(self, dp_id: str):
        rt = OpsRuntime(self.home, self.adapter(dp_id), stack_profile=self.profile,
                        sandbox_factory=lambda: self.adapter(dp_id))
        return rt.onboard(dp_id)

    # -- one snapshot -------------------------------------------------------------------------------
    def tick(self, dp_id: str, snapshot: str) -> dict[str, Any]:
        ws = Workspace(dp_id, self.home)
        if (ws.read_yaml("control/service.yaml") or {}).get("mode") != "shadow":
            raise AdefError(f"{dp_id} is not in shadow mode")
        if snapshot <= (ws.read_yaml("shadow/start.yaml") or {}).get("after_snapshot", ""):
            raise AdefError(f"{snapshot} predates the shadow period; it was used for the onboarding dry run")
        adapter = self.adapter(dp_id, snapshot)
        out = OpsRuntime(self.home, adapter, simulated=adapter.synthetic).tick(dp_id, adapter.now)
        opened = [o["case"] for o in out["outcomes"] if o["outcome"] == "opened"]
        diag, rem = DiagnosisAgent(ws, adapter), RemediationAgent(ws, adapter)
        for cid in opened:
            diag.diagnose(cid)
            try:
                rem.build_options(cid)  # recorded for comparison; S1 waits for the council
            except AdefError:
                pass
        recovered = self._observe(ws, adapter, out, exclude=set(opened))
        shadow.record_tick(ws, snapshot=snapshot, at=adapter.now.isoformat(), signals=len(out["signals"]),
                           cases_opened=opened)
        return {**out, "cases_opened": opened, "recovered": recovered}

    def _observe(self, ws: Workspace, adapter: ExportAdapter, out: dict, exclude: set[str]) -> list[str]:
        """Observe the team's own recovery: when every detecting monitor passes in this tick, the
        case is marked recovered by observation (no attempt is counted, nothing is executed).
        A recovered case with a person's confirmed cause gets its post-incident note and closes."""
        from .agents import CommanderAgent, KnowledgeAgent
        from .engine.case_store import CaseStore
        cs = CaseStore(ws)
        rem = RemediationAgent(ws, adapter)
        passing = {r["monitor_id"] for r in out.get("signals_results", [])}
        recovered = []
        for c in cs.open_cases("INC"):
            if c.id in exclude:
                continue
            if c.state == "diagnosing":
                try:
                    rem.build_options(c.id)
                    c = cs.get(c.id)
                except AdefError:
                    continue
            if c.state != "options-ready":
                continue
            latest = {r["monitor_id"]: r["status"] for r in out["results"]}
            if all(latest.get(m.monitor_id) == "pass" for m in c.detecting_monitors):
                cs.transition(c.id, "verifying", by="remediation", note="shadow: observing the team's recovery")
                ids = [r["id"] for r in out["results"] if r["monitor_id"] in {m.monitor_id for m in c.detecting_monitors}]
                ws.write("remediation", f"cases/{c.id}/verification.md", "\n".join([
                    f"# Verification - {c.id} (shadow mode)", "",
                    "Recovered by the team's own actions; observed, not executed, by Operate.", "",
                    *[f"- {r['id']} {r['monitor_id']}: {r['status']}" for r in out["results"] if r["id"] in ids], ""]))
                cs.transition(c.id, "recovered", by="remediation", verification_results=ids)
                recovered.append(c.id)
        know = KnowledgeAgent(ws, adapter)
        concl = {x["case_id"]: x for x in shadow.conclusions(ws) if x.get("case_id")}
        for c in cs.open_cases("INC"):
            h = concl.get(c.id)
            if c.state == "recovered" and h and h.get("cause_class"):
                know.confirm_cause(c.id, cause_class=h["cause_class"], by=h["by"])
                know.post_incident(c.id)
                CommanderAgent(ws, adapter).close(c.id)
        return recovered

    def run_new_snapshots(self, dp_id: str) -> list[dict]:
        ws = Workspace(dp_id, self.home)
        seen = {t["snapshot"] for t in shadow.ticks(ws)}
        start = (ws.read_yaml("shadow/start.yaml") or {}).get("after_snapshot", "")
        return [self.tick(dp_id, s) for s in self.adapter(dp_id).snapshots() if s > start and s not in seen]

    def report(self, dp_id: str) -> tuple[dict, str]:
        ws = Workspace(dp_id, self.home)
        rep = shadow.shadow_report(ws)
        return rep, shadow.write_report(ws, rep)
