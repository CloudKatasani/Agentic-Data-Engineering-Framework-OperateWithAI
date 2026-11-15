from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from adef_operate.connectors.simulator import SimulatorAdapter
from adef_operate.core.hashing import canonical_hash
from adef_operate.engine.approvals import Approvals
from adef_operate.engine.case_store import CaseStore
from adef_operate.engine.executor import Executor
from adef_operate.engine.handover import accept_handover
from adef_operate.engine.monitor_engine import MonitorEngine
from adef_operate.engine.onboarding_tools import draft_service, readiness, write_service
from adef_operate.engine.registry import Registry
from adef_operate.engine.workspace import Workspace
from adef_operate.packs import handover_pack


@dataclass
class Product:
    ws: Workspace
    pack: dict
    pack_name: str
    sim: SimulatorAdapter
    approvals: Approvals
    registry: Registry
    cases: CaseStore
    executor: Executor
    engine: MonitorEngine

    def approve(self, request_id: str, decision: str = "approved"):
        req = self.approvals.get_request(request_id)
        owners = self.pack["owners"]
        roles = req.approver_roles if req.gate != "OG2" else req.approver_roles[:1]
        for role in roles:
            self.approvals.decide(request_id, approver=owners[role], role=role, decision=decision,
                                  artifact_hash=req.artifact_hash)
        return req

    def promote(self, action_type: str, level: str):
        proposal = canonical_hash({"action_type": action_type, "level": level})
        req = self.approvals.request(gate="OG7", decision_asked=f"promote {action_type} to {level}",
                                     artifact_hash=proposal, requested_by="knowledge")
        self.approve(req.id)
        self.registry.set_level(action_type, level, approval_id=req.id, approvals=self.approvals,
                                artifact_hash=proposal)


def _onboard(home, pack_name: str) -> Product:
    pack = handover_pack(pack_name)
    res = accept_handover(pack, home=home, simulated=True)
    assert res.accepted, res.gaps
    ws = res.workspace
    sim = SimulatorAdapter()
    p = Product(ws=ws, pack=pack, pack_name=pack_name, sim=sim, approvals=Approvals(ws),
                registry=Registry(ws), cases=CaseStore(ws), executor=Executor(ws, sim),
                engine=MonitorEngine(ws, sim, simulated=True))
    p.approve(res.og0_request)
    write_service(ws, draft_service(pack))
    assert readiness(ws, pack_name)["ready"]
    p.approve(p.approvals.request_og1().id)
    return p


@pytest.fixture
def home(tmp_path):
    return tmp_path / "adef"


@pytest.fixture
def util(home) -> Product:
    return _onboard(home, "utilities")


@pytest.fixture
def telco(home) -> Product:
    return _onboard(home, "telco")


@pytest.fixture
def now():
    return datetime.now(timezone.utc)
