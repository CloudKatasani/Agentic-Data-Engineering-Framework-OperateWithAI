"""O2 ``adef-ops-signal-sentinel``: tell people and agents about real problems once."""

from __future__ import annotations

import json
from typing import Any

from ..engine.correlator import Correlator
from ..engine.lineage import Lineage
from ..engine.workspace import Workspace


class SentinelAgent:
    identity = "sentinel"

    def __init__(self, ws: Workspace, adapter):
        self.ws = ws
        self.adapter = adapter
        sd = ws.read_yaml("service/service-definition.yaml") or {}
        self.correlator = Correlator(ws, Lineage.from_service(sd))

    def _known_events(self) -> list[dict]:
        events = []
        for n in getattr(self.adapter, "upstream_notices", lambda _p: [])(self.ws.dp_id):
            events.append({"kind": "upstream_notice", **n})
        for r in getattr(self.adapter, "releases", lambda _p: [])(self.ws.dp_id):
            events.append({"kind": "release", **r})
        return events

    def run(self, day: str) -> list[dict[str, Any]]:
        path = f"signals/{day}.jsonl"
        if not self.ws.has(path):
            return []
        signals = [json.loads(l) for l in self.ws.read(path).splitlines() if l.strip()]
        return self.correlator.group(signals, day=day, known_events=self._known_events())
