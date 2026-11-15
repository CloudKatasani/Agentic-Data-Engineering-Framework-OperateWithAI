"""Control-plane loop: collector and monitor engine -> Sentinel -> Commander."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .agents import CommanderAgent, FinOpsAgent, GovernanceAgent, OnboardingAgent, SentinelAgent
from .connectors.paging import OutboxPager
from .engine.monitor_engine import MonitorEngine
from .engine.workspace import Workspace


class OpsRuntime:
    def __init__(self, home: str | Path, adapter, *, simulated: bool = True,
                 pager: Optional[OutboxPager] = None, stack_profile: Optional[dict] = None,
                 sandbox_factory=None):
        self.home = Path(home)
        self.adapter = adapter
        self.stack_profile = stack_profile
        self.sandbox_factory = sandbox_factory
        self.simulated = simulated
        self.pager = pager or OutboxPager()

    def workspace(self, dp_id: str) -> Workspace:
        return Workspace(dp_id, self.home)

    def release(self, event: dict[str, Any]):
        return CommanderAgent.accept(event, home=self.home, simulated=self.simulated,
                                     stack_profile=self.stack_profile)

    def onboard(self, dp_id: str):
        if self.sandbox_factory:
            return OnboardingAgent(self.workspace(dp_id), self.sandbox_factory).onboard()
        return OnboardingAgent(self.workspace(dp_id)).onboard()

    def tick(self, dp_id: str, at: Optional[datetime] = None) -> dict[str, Any]:
        at = at or datetime.now(timezone.utc)
        ws = self.workspace(dp_id)
        detected = MonitorEngine(ws, self.adapter, simulated=self.simulated).run(at)
        day = at.date().isoformat()
        groups = SentinelAgent(ws, self.adapter).run(day)
        commander = CommanderAgent(ws, self.adapter, self.pager)
        outcomes = commander.process_groups(groups, day=day)
        reviews = {}
        # Routed groups wake their owning (recommend-only) agent.
        routed = {o.get("routed_to") for o in outcomes if o["outcome"] == "routed"}
        if "adef-ops-finops-performance" in routed:
            reviews["finops"] = FinOpsAgent(ws, self.adapter, commander).review(day)
        if "adef-ops-governance-compliance" in routed:
            reviews["governance"] = GovernanceAgent(ws, self.adapter, commander).review(day, at=at)
        return {"signals": detected["signals"], "results": detected["results"], "groups": groups,
                "outcomes": outcomes, "reviews": reviews}
