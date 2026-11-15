"""O1 ``adef-ops-service-onboarding``: make a released product operable (ends at OG1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..connectors.simulator import SimulatorAdapter
from ..core.errors import GuardViolation
from ..engine.approvals import Approvals
from ..engine.onboarding_tools import check_draft, draft_service, readiness, readiness_on_exports, write_service
from ..engine.workspace import Workspace


@dataclass
class OnboardingResult:
    ready: bool
    report: dict[str, Any]
    problems: list[str] = field(default_factory=list)
    og1_request: Optional[str] = None


class OnboardingAgent:
    identity = "onboarding"

    def __init__(self, ws: Workspace, sandbox_factory: Callable[[], Any] = SimulatorAdapter):
        self.ws = ws
        self.sandbox_factory = sandbox_factory
        self.approvals = Approvals(ws)

    def _og0_approved(self) -> bool:
        reqs = [r for r in self.approvals._records("request") if r["gate"] == "OG0"]
        return bool(reqs) and self.approvals.status(reqs[-1]["id"]) == "approved"

    def onboard(self) -> OnboardingResult:
        if not self._og0_approved():
            raise GuardViolation(f"{self.ws.dp_id}: OG0 is not approved; onboarding does not start")
        pack = self.ws.read_yaml("control/handover-pack.yaml")
        draft = draft_service(pack)
        problems = check_draft(draft)
        if problems:
            return OnboardingResult(ready=False, report={}, problems=problems)
        write_service(self.ws, draft)
        if hasattr(self.sandbox_factory(), "inject_class"):
            report = readiness_on_exports(self.ws, self.sandbox_factory)
        else:
            report = readiness(self.ws, pack["domain_pack"], self.sandbox_factory)
        if not report["ready"]:
            return OnboardingResult(ready=False, report=report,
                                    problems=["sandbox proofs failed; see service/readiness-report.md"])
        req = self.approvals.request_og1(requested_by=self.identity)
        return OnboardingResult(ready=True, report=report, og1_request=req.id)
