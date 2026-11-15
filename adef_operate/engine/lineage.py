"""Lineage helpers over the product's edges (upstream -> downstream)."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable


class Lineage:
    def __init__(self, edges: Iterable[Iterable[str]]):
        self.down: dict[str, set[str]] = defaultdict(set)
        self.up: dict[str, set[str]] = defaultdict(set)
        for a, b in edges:
            self.down[a].add(b)
            self.up[b].add(a)

    @classmethod
    def from_service(cls, service_def: dict) -> "Lineage":
        deps = service_def.get("dependencies", {}) or {}
        return cls((a, b) for a, bs in deps.items() for b in bs)

    def _walk(self, start: str, graph: dict[str, set[str]]) -> set[str]:
        seen, stack = set(), [start]
        while stack:
            for n in graph.get(stack.pop(), ()):
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        return seen

    def descendants(self, obj: str) -> set[str]:
        return self._walk(obj, self.down)

    def ancestors(self, obj: str) -> set[str]:
        return self._walk(obj, self.up)

    def neighbourhood(self, objs: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for o in objs:
            out |= {o} | self.ancestors(o) | self.descendants(o)
        return out

    def related(self, a: str, b: str) -> bool:
        return a == b or b in self.descendants(a) or a in self.descendants(b)

    def depth(self, obj: str) -> int:
        """Longest distance from a root; the smallest depth is the most upstream object."""
        ups = self.up.get(obj, set())
        return 0 if not ups else 1 + max(self.depth(u) for u in ups)

    def is_leaf(self, obj: str) -> bool:
        return not self.down.get(obj)
