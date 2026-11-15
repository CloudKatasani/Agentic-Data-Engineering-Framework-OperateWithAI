"""Demo Simulator scenarios: a golden product released, onboarded and operable, plus a
fault-library evaluation of Diagnosis. Approvals here are recorded as the synthetic
approvers named in the packs; this module never runs against a real product.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .agents import CommanderAgent, DiagnosisAgent, RemediationAgent
from .connectors.simulator import FAULTS, SIM_NOW, SimulatorAdapter
from .engine.approvals import Approvals
from .engine.workspace import Workspace
from .packs import handover_pack
from .runtime import OpsRuntime


class Scenario:
    def __init__(self, home: str | Path, pack_name: str):
        self.sim = SimulatorAdapter()
        self.rt = OpsRuntime(home, self.sim)
        self.pack_name = pack_name
        self.pack = handover_pack(pack_name)
        self.dp = self.pack["dp_id"]
        res = self.rt.release({"type": "product.released", "handover_pack": self.pack})
        if not res.accepted:
            raise ValueError(f"handover rejected: {res.gaps}")
        self.ws: Workspace = res.workspace
        self.approve(res.og0_request)
        self.onboarding = self.rt.onboard(self.dp)
        if not self.onboarding.ready:
            raise ValueError(f"not ready: {self.onboarding.problems}")
        self.approve(self.onboarding.og1_request)

    def approve(self, request_id: str, decision: str = "approved", reason: str = "") -> None:
        """Decide as the synthetic approvers named in the pack (simulation only)."""
        a = Approvals(self.ws)
        r = a.get_request(request_id)
        roles = r.approver_roles[:1] if r.gate == "OG2" and "release_manager" not in r.approver_roles \
            else r.approver_roles
        for role in roles:
            a.decide(request_id, approver=self.pack["owners"][role], role=role, decision=decision,
                     artifact_hash=r.artifact_hash, reason=reason or "Demo Simulator approver")

    def tick(self, at: Optional[datetime] = None) -> dict:
        return self.rt.tick(self.dp, at or SIM_NOW)

    @property
    def commander(self) -> CommanderAgent:
        return CommanderAgent(self.ws, self.sim, self.rt.pager)

    @property
    def diagnosis(self) -> DiagnosisAgent:
        return DiagnosisAgent(self.ws, self.sim)

    @property
    def remediation(self) -> RemediationAgent:
        return RemediationAgent(self.ws, self.sim)


def diagnosis_accuracy(home: str | Path) -> dict:
    """Run every library fault on every pack it applies to; compare cause class with the label."""
    rows = []
    for fid, fault in FAULTS.items():
        for pack in fault.packs:
            sc = Scenario(Path(home) / f"{fid}-{pack}", pack)
            sc.sim.inject(fid, pack)
            out = sc.tick()
            o = out["outcomes"][0]
            d = sc.diagnosis
            r = d.diagnose(o["case"]) if o.get("case") else d.diagnose_group(out["groups"][0])
            rows.append({"fault": fid, "pack": pack, "expected": fault.cause_class, "got": r.cause_class,
                         "confidence": r.confidence, "correct": r.cause_class == fault.cause_class,
                         "evidence": len(r.supporting_evidence), "hypotheses": len(r.hypotheses),
                         "ruled_out": len(r.ruled_out)})
    per_fault = {f: all(r["correct"] for r in rows if r["fault"] == f) for f in FAULTS}
    return {"rows": rows, "faults_correct": sum(per_fault.values()), "faults": len(per_fault),
            "unsupported_causes": sum(1 for r in rows if r["got"] != "unknown" and r["evidence"] == 0)}
