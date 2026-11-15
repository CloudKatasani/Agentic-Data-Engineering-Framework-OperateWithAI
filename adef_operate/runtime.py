"""Control-plane loop: collector and monitor engine -> Sentinel -> Commander."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .agents import CommanderAgent, OnboardingAgent, SentinelAgent
from .connectors.paging import OutboxPager
from .engine.monitor_engine import MonitorEngine
from .engine.workspace import Workspace


class OpsRuntime:
    def __init__(self, home: str | Path, adapter, *, simulated: bool = True,
                 pager: Optional[OutboxPager] = None):
        self.home = Path(home)
        self.adapter = adapter
        self.simulated = simulated
        self.pager = pager or OutboxPager()

    def workspace(self, dp_id: str) -> Workspace:
        return Workspace(dp_id, self.home)

    def release(self, event: dict[str, Any]):
        return CommanderAgent.accept(event, home=self.home, simulated=self.simulated)

    def onboard(self, dp_id: str):
        return OnboardingAgent(self.workspace(dp_id)).onboard()

    def tick(self, dp_id: str, at: Optional[datetime] = None) -> dict[str, Any]:
        at = at or datetime.now(timezone.utc)
        ws = self.workspace(dp_id)
        detected = MonitorEngine(ws, self.adapter, simulated=self.simulated).run(at)
        day = at.date().isoformat()
        groups = SentinelAgent(ws, self.adapter).run(day)
        outcomes = CommanderAgent(ws, self.adapter, self.pager).process_groups(groups, day=day)
        return {"signals": detected["signals"], "groups": groups, "outcomes": outcomes}
