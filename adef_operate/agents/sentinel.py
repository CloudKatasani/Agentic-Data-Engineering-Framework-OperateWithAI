"""O2 ``adef-ops-signal-sentinel``: tell people and agents about real problems once."""

from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from ..core.errors import GuardViolation
from ..core.hashing import file_hash
from ..engine.correlator import Correlator
from ..engine.monitor_engine import evaluate_spec
from ..engine.lineage import Lineage
from ..engine.workspace import Workspace


class SentinelAgent:
    identity = "sentinel"

    REPEAT_WITHOUT_ACTION = 3

    def __init__(self, ws: Workspace, adapter, history_source: Optional[Callable[[], Any]] = None):
        self.ws = ws
        self.adapter = adapter
        self.history_source = history_source or (lambda: adapter)
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

    # -- T5: weekly noise review -------------------------------------------------------------
    def _days(self, week_start: date, days: int = 7) -> list[str]:
        return [(week_start + timedelta(days=i)).isoformat() for i in range(days)]

    def _acted_cases(self) -> set[str]:
        root = self.ws.path("cases")
        if not root.exists():
            return set()
        return {p.parent.parent.name for p in root.glob("*/actions/ACT-*.yaml")}

    def noise_report(self, week_start: date) -> dict:
        days = self._days(week_start)
        sigs, groups = [], []
        for d in days:
            if self.ws.has(f"signals/{d}.jsonl"):
                sigs += [json.loads(l) for l in self.ws.read(f"signals/{d}.jsonl").splitlines() if l.strip()]
            groups += self.correlator.load_groups(d)
        cases = {}
        root = self.ws.path("cases")
        if root.exists():
            for p in root.glob("*/case.yaml"):
                import yaml
                c = yaml.safe_load(p.read_text())
                if c.get("signal_group"):
                    cases[c["signal_group"]] = c["id"]
        acted = self._acted_cases()
        fires = Counter(s.get("monitor_id") for s in sigs)
        labels = Counter(l["label"] for g in groups for l in g["labels"].values())
        no_action = Counter()
        for g in groups:
            if cases.get(g["id"]) not in acted:
                for m in g["monitors"]:
                    no_action[m] += sum(1 for sid in g["signals"]
                                        if next((x for x in sigs if x["id"] == sid), {}).get("monitor_id") == m)
        candidates = sorted(m for m, n in no_action.items() if m and n >= self.REPEAT_WITHOUT_ACTION)
        opened = sum(1 for g in groups if g["id"] in cases)
        week = week_start.isoformat()
        lines = [f"# Noise report - {self.ws.dp_id} - week of {week}", "",
                 f"- Raw signals: {len(sigs)}", f"- Signal groups: {len(groups)}", f"- Cases opened: {opened}",
                 f"- Signals per case: {round(len(sigs) / opened, 1) if opened else '-'}",
                 f"- Labels: {dict(labels)} (suppression is a label; nothing was dropped)", "",
                 "## Fires per monitor", *[f"- {m}: {n}" for m, n in fires.most_common()], "",
                 f"## Fired {self.REPEAT_WITHOUT_ACTION}+ times with no action (tuning candidates for OG7, never muted)",
                 *([f"- {m}: {no_action[m]}" for m in candidates] or ["- none"]), ""]
        self.ws.write(self.identity, f"signals/noise-report-{week}.md", "\n".join(lines))
        return {"signals": len(sigs), "groups": len(groups), "cases": opened, "candidates": candidates,
                "labels": dict(labels)}

    def propose_tuning(self, monitor_id: str, rule_changes: dict, *, reason: str, commander=None,
                       history_days: int = 28) -> dict:
        """Replay the change on history and propose it at OG7. Never edits the live spec."""
        if rule_changes.get("enabled") is False or rule_changes.get("kind") == "disabled":
            raise GuardViolation("refused: disabling a monitor is muting; propose a tuned threshold instead")
        spec = self.ws.read_yaml("service/monitoring-spec.yaml")
        mon = next((m for m in spec["monitors"] if m["id"] == monitor_id), None)
        if mon is None:
            raise GuardViolation(f"no monitor {monitor_id}")
        if mon["signal_class"] == "quality":
            raise GuardViolation("refused: a quality rule threshold is not tuned to stop alerts; "
                                 "ask the Data Owner for a time-limited waiver at OG7")
        after = copy.deepcopy(spec)
        for m in after["monitors"]:
            if m["id"] == monitor_id:
                m["rule"].update(rule_changes)
        only = lambda sp: {**sp, "monitors": [m for m in sp["monitors"] if m["id"] == monitor_id]}
        before_n = after_n = 0
        for _, past in self.history_source().history(history_days):
            before_n += sum(1 for r in evaluate_spec(only(spec), past, self.ws.dp_id) if not r["passed"])
            after_n += sum(1 for r in evaluate_spec(only(after), past, self.ws.dp_id) if not r["passed"])
        root = self.ws.path("service/tuning-proposals")
        n = len(list(root.glob("TUN-*.md"))) + 1 if root.exists() else 1
        tid = f"TUN-{n:03d}"
        path = f"service/tuning-proposals/{tid}.md"
        self.ws.write(self.identity, path, "\n".join([
            f"# {tid} - tune {monitor_id}", "", f"- Reason: {reason}", f"- Current rule: {mon['rule']}",
            f"- Proposed change: {rule_changes}",
            f"- Replay over {history_days} days of history: {before_n} alert(s) before, {after_n} after",
            f"- Replaces monitoring spec with hash: `{file_hash(self.ws.path('service/monitoring-spec.yaml'))}`",
            "- Status: proposal awaiting OG7 (Platform Owner and Tech Lead); the live threshold is unchanged", ""]))
        out = {"id": tid, "before": before_n, "after": after_n}
        if commander is not None:
            req = commander.request_approval(gate="OG7", decision_asked=f"{tid}: tune {monitor_id}",
                                             artifact_hash=file_hash(self.ws.path(path)),
                                             owner_agent="adef-ops-signal-sentinel", evidence=[path])
            out["approval"] = req.id
        return out
