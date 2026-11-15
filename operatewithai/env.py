"""The client environment model: systems, the data chain, business services, the batch
plan, towers, the roster, priorities and SLAs. Agents read the client only through this."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any, Optional

import yaml

CLIENTS_DIR = Path(__file__).parent / "clients"


def hhmm(day: datetime, text: str) -> datetime:
    h, m = (int(x) for x in text.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


class Environment:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.client = data["client"]
        self.cis = {c["id"]: c for c in data["cis"]}
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.services = {s["id"]: s for s in data["services"]}
        self.towers = {t["id"]: t for t in data["towers"]}
        self.people = data["people"]
        self.down: dict[str, list[str]] = defaultdict(list)
        self.up: dict[str, list[str]] = defaultdict(list)
        for a, b in data["edges"]:
            self.down[a].append(b)
            self.up[b].append(a)

    @classmethod
    def load(cls, client: str = "demo") -> "Environment":
        return cls(yaml.safe_load((CLIENTS_DIR / client / "environment.yaml").read_text("utf-8")))

    # -- graph ------------------------------------------------------------------------------
    def _walk(self, start: str, graph: dict[str, list[str]]) -> list[str]:
        seen, stack, order = set(), [start], []
        while stack:
            for n in graph.get(stack.pop(), []):
                if n not in seen:
                    seen.add(n)
                    order.append(n)
                    stack.append(n)
        return order

    def descendants(self, node: str) -> list[str]:
        return self._walk(node, self.down)

    def ancestors(self, node: str) -> list[str]:
        return self._walk(node, self.up)

    def related(self, a: str, b: str) -> bool:
        return any(x == y or y in self.descendants(x) or x in self.descendants(y)
                   for x in self.expand(a) for y in self.expand(b))

    @cached_property
    def depth(self) -> dict[str, int]:
        out: dict[str, int] = {}

        def d(n: str) -> int:
            if n not in out:
                out[n] = 0 if not self.up.get(n) else 1 + max(d(u) for u in self.up[n])
            return out[n]
        for n in self.nodes:
            d(n)
        for n in self.nodes.values():  # a job that builds datasets sits just above the first of them
            if n.get("builds"):
                out[n["id"]] = min(out[b] for b in n["builds"]) - 0.5
        return out

    def expand(self, node: str) -> list[str]:
        """The lineage nodes a node stands for: a builder job stands for what it builds."""
        return list(self.nodes.get(node, {}).get("builds") or [node])

    def builder_of(self, dataset: str) -> Optional[str]:
        return next((n["id"] for n in self.nodes.values() if dataset in (n.get("builds") or [])), None)

    def nodes_of_ci(self, ci: str) -> list[str]:
        """Nodes that live on a CI or need it to run."""
        return [n["id"] for n in self.nodes.values() if n["ci"] == ci or ci in n.get("requires", [])]

    def jobs(self) -> list[dict]:
        return [n for n in self.nodes.values() if n["kind"] == "job"]

    def chain(self, service_id: str) -> list[str]:
        """Every node the service depends on, upstream first."""
        end = self.services[service_id]["delivered_by"]
        nodes = self.ancestors(end) + [end]
        return sorted(nodes, key=lambda n: (self.depth[n], n))

    def services_hit_by(self, node_ids: list[str]) -> list[str]:
        hit = set()
        expanded = {x for n in node_ids for x in self.expand(n)}
        for sid in self.services:
            if set(self.chain(sid)) & expanded:
                hit.add(sid)
        return sorted(hit)

    # -- people -----------------------------------------------------------------------------
    def on_call(self, tower: str) -> Optional[str]:
        return next((p["name"] for p in self.people if p.get("tower") == tower and p.get("on_call")), None)

    def holder(self, role: str) -> Optional[str]:
        return next((p["name"] for p in self.people if p.get("role") == role), None)

    def person(self, name: str) -> Optional[dict]:
        return next((p for p in self.people if p["name"] == name), None)

    def is_person(self, name: str) -> bool:
        return self.person(name) is not None

    def tower_of_ci(self, ci: str) -> str:
        return self.cis[ci]["tower"]

    def tower_name(self, tower: str) -> str:
        return self.towers.get(tower, {}).get("name", tower)

    # -- policy -----------------------------------------------------------------------------
    def priority(self, impact: str, urgency: str) -> str:
        return self.data["priority_matrix"][impact][urgency]

    def sla(self, priority: str) -> dict[str, int]:
        return self.data["sla_policy"][priority]

    def change_for(self, ci: str, at: datetime, lookback_hours: int = 12) -> Optional[dict]:
        """An approved change on this CI whose window is open or closed within the lookback."""
        for c in self.data.get("changes", []):
            if c["ci"] != ci:
                continue
            start = datetime.strptime(c["window_start"], "%Y-%m-%d %H:%M")
            end = datetime.strptime(c["window_end"], "%Y-%m-%d %H:%M")
            if start <= at <= end + timedelta(hours=lookback_hours):
                return {**c, "active": start <= at <= end}
        return None
