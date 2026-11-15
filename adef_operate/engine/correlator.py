"""Correlator support for the Signal Sentinel (spec section 7, O2; backlog O4-2).

Turns raw signals into candidate-incident groups. Rules from the skill contract:

* Group by product, lineage neighbourhood and time; attach the likely first-failing
  object (the most upstream object in the group, earliest signal on ties).
* Never drop a signal. Suppression is a label: ``primary``, ``downstream-echo`` or
  ``duplicate``, each with a reason.
* A blocking quality failure or schema drift always heads a group of its own; it is
  never absorbed into another group as an echo (it may absorb its own echoes).
* Cost and access signals form their own groups (they route to FinOps and Governance).
* Known events (upstream notices, recent releases) are attached as leads, not causes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from .lineage import Lineage
from .workspace import Workspace

STANDALONE = {"quality", "schema"}
OWN_FAMILY = {"cost", "access"}
REUSE_WINDOW = timedelta(hours=6)
GROUP_WINDOW = timedelta(minutes=60)


def _t(s: dict) -> datetime:
    return datetime.fromisoformat(s["time"].replace("Z", "+00:00"))


class Correlator:
    def __init__(self, ws: Workspace, lineage: Lineage):
        self.ws = ws
        self.lineage = lineage

    # -- storage --------------------------------------------------------------------
    def _file(self, day: str) -> str:
        return f"signals/groups/{day}.jsonl"

    def load_groups(self, day: str) -> list[dict[str, Any]]:
        if not self.ws.has(self._file(day)):
            return []
        return [json.loads(l) for l in self.ws.read(self._file(day)).splitlines() if l.strip()]

    def _save(self, day: str, groups: list[dict[str, Any]]) -> None:
        self.ws.write("sentinel", self._file(day), "".join(json.dumps(g) + "\n" for g in groups))

    def grouped_signal_ids(self, day: str) -> set[str]:
        return {sid for g in self.load_groups(day) for sid in g["signals"]}

    # -- grouping -------------------------------------------------------------------
    def _family(self, s: dict) -> str:
        return s["signal_class"] if s["signal_class"] in OWN_FAMILY else "service"

    def _fits(self, g: dict, s: dict) -> bool:
        if g["family"] != self._family(s):
            return False
        if g["family"] in OWN_FAMILY:
            return True
        if abs(_t(s) - datetime.fromisoformat(g["last_seen"])) > GROUP_WINDOW:
            return False
        if s["signal_class"] in STANDALONE:
            return False  # a standalone signal never joins another group
        head = g["first_failing_object"]
        if g["standalone"]:
            # Absorb only echoes of the standalone failure: same object or downstream of it.
            return s["object"] == head or s["object"] in self.lineage.descendants(head)
        return any(self.lineage.related(s["object"], o) for o in g["objects"])

    def _new_group(self, day: str, n: int, s: dict) -> dict[str, Any]:
        return {"id": f"GRP-{day.replace('-', '')}-{n:03d}", "product": self.ws.dp_id, "day": day,
                "family": self._family(s), "standalone": s["signal_class"] in STANDALONE,
                "signals": [], "monitors": [], "objects": [], "classes": [], "labels": {},
                "first_failing_object": s["object"], "first_seen": s["time"], "last_seen": s["time"],
                "known_events": [], "simulated": bool(s.get("simulated"))}

    def _add(self, g: dict, s: dict) -> None:
        g["signals"].append(s["id"])
        for key, val in (("monitors", s.get("monitor_id")), ("objects", s["object"]),
                         ("classes", s["signal_class"])):
            if val and val not in g[key]:
                g[key].append(val)
        g["last_seen"] = max(g["last_seen"], s["time"])
        g["simulated"] = g["simulated"] or bool(s.get("simulated"))

    def _relabel(self, g: dict, sigs: dict[str, dict]) -> None:
        members = [sigs[i] for i in g["signals"] if i in sigs]
        if g["standalone"]:
            head = next(s for s in members if s["signal_class"] in STANDALONE)["object"]
        else:
            head = min(members, key=lambda s: (self.lineage.depth(s["object"]), s["time"]))["object"]
        g["first_failing_object"] = head
        seen_monitor_time: dict[str, str] = {}
        for s in sorted(members, key=lambda s: s["time"]):
            mid = s.get("monitor_id") or s["id"]
            if mid in seen_monitor_time:
                g["labels"][s["id"]] = {"label": "duplicate",
                                        "reason": f"monitor {mid} already fired in this group"}
            elif s["object"] == head:
                g["labels"][s["id"]] = {"label": "primary", "reason": "on the first-failing object"}
            else:
                g["labels"][s["id"]] = {"label": "downstream-echo",
                                        "reason": f"{s['object']} is downstream of {head}"}
            seen_monitor_time.setdefault(mid, s["time"])

    def group(self, signals: Iterable[dict], *, day: str,
              known_events: Optional[list[dict]] = None) -> list[dict[str, Any]]:
        """Group ``signals`` (not yet grouped) into the day's groups. Returns touched groups."""
        groups = self.load_groups(day)
        already = {sid for g in groups for sid in g["signals"]}
        new = sorted((s for s in signals if s["id"] not in already),
                     key=lambda s: (s["signal_class"] not in STANDALONE, self.lineage.depth(s["object"]), s["time"]))
        sigs = {s["id"]: s for s in new}
        # Re-read earlier signals of the day for labelling.
        for s in self._day_signals(day):
            sigs.setdefault(s["id"], s)
        touched: dict[str, dict] = {}
        for s in new:
            target = None
            # A repeat of a monitor already in a recent group is a duplicate of that group.
            for g in groups:
                if s.get("monitor_id") in g["monitors"] and \
                        _t(s) - datetime.fromisoformat(g["last_seen"]) <= REUSE_WINDOW:
                    target = g
                    break
            if target is None:
                # Prefer an ordinary lineage group; a standalone group absorbs only what nothing else explains.
                fits = sorted((g for g in groups if self._fits(g, s)), key=lambda g: g["standalone"])
                target = fits[0] if fits else None
            if target is None:
                target = self._new_group(day, len(groups) + 1, s)
                groups.append(target)
            self._add(target, s)
            touched[target["id"]] = target
        for g in touched.values():
            self._relabel(g, sigs)
            g["known_events"] = self._known(g, known_events or [])
        self._save(day, groups)
        return list(touched.values())

    def _known(self, g: dict, events: list[dict]) -> list[dict]:
        hood = self.lineage.neighbourhood(g["objects"])
        out = []
        for e in events:
            if e.get("object") and e["object"] not in hood:
                continue
            out.append({**e, "note": "lead, not a conclusion"})
        return out

    def _day_signals(self, day: str) -> list[dict]:
        path = f"signals/{day}.jsonl"
        if not self.ws.has(path):
            return []
        return [json.loads(l) for l in self.ws.read(path).splitlines() if l.strip()]
