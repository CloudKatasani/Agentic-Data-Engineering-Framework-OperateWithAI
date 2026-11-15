"""Case store with lifecycle state machines and guards (backlog O1-3, O2-5).

Illegal transitions are rejected with a specific message. Guards enforced here, not in
prompts:

* ``executing`` is refused after 2 failed attempts (escalate instead; OT-13).
* ``recovered`` needs ``verification.md`` and passing monitor results from the
  detecting monitors (recovery needs proof).
* ``reviewed`` needs ``post-incident.md``.
* ``closed`` needs verification, the post-incident note, and every detecting monitor
  still enabled, unchanged since detection, and passing (no muting to close; OT-14).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Optional

import yaml

from ..core.errors import GuardViolation, IllegalTransition
from ..core.hashing import canonical_hash
from ..schemas.records import Case, CaseType, LinkedCase, MonitorRef, TimelineEntry, validate_record
from .escalation import escalate
from .workspace import Workspace

MAX_FAILED_ATTEMPTS = 2

TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "INC": {
        "detected": {"triaged"},
        "triaged": {"contained", "diagnosing"},
        "contained": {"diagnosing"},
        "diagnosing": {"options-ready"},
        "options-ready": {"awaiting-approval", "executing", "verifying"},
        "awaiting-approval": {"executing", "options-ready"},
        "executing": {"verifying", "options-ready"},  # options-ready: containment done, choose the fix
        "verifying": {"recovered", "failed"},
        "failed": {"options-ready", "diagnosing"},
        "recovered": {"reviewed"},
        "reviewed": {"closed"},
        "closed": set(),
    },
    "PRB": {"open": {"investigating"}, "investigating": {"prevention-live"},
            "prevention-live": {"verified", "investigating"}, "verified": {"closed"}, "closed": set()},
    "FIN": {"open": {"awaiting-decision"}, "awaiting-decision": {"decided"},
            "decided": {"closed"}, "closed": set()},
    "GOV": {"open": {"awaiting-decision"}, "awaiting-decision": {"resolved", "accepted"},
            "resolved": {"closed"}, "accepted": {"closed"}, "closed": set()},
    "CHG": {"open": {"submitted"}, "submitted": {"intake-confirmed", "open"},
            "intake-confirmed": {"release-live"}, "release-live": {"closed"}, "closed": set()},
}
INITIAL = {"INC": "detected", "PRB": "open", "FIN": "open", "GOV": "open", "CHG": "open"}


def monitor_hash(monitor: dict) -> str:
    return canonical_hash(monitor)


class CaseStore:
    def __init__(self, ws: Workspace):
        self.ws = ws

    # -- io -------------------------------------------------------------------------
    def _path(self, case_id: str) -> str:
        return f"cases/{case_id}/case.yaml"

    def get(self, case_id: str) -> Case:
        data = self.ws.read_yaml(self._path(case_id))
        if data is None:
            raise KeyError(f"no case {case_id}")
        return Case.model_validate(data)

    def _save(self, case: Case) -> None:
        validate_record("case", case.model_dump(mode="json"))
        self.ws.write("case_store", self._path(case.id),
                      yaml.safe_dump(case.model_dump(mode="json"), sort_keys=False))

    def all(self) -> list[Case]:
        root = self.ws.path("cases")
        if not root.exists():
            return []
        return [self.get(p.name) for p in sorted(root.iterdir()) if (p / "case.yaml").exists()]

    def open_cases(self, case_type: str | None = None) -> list[Case]:
        return [c for c in self.all() if c.state != "closed" and (case_type is None or c.type == case_type)]

    def _next_id(self, case_type: str) -> str:
        n = sum(1 for c in self.all() if c.type == case_type) + 1
        return f"{case_type}-{n:03d}"

    # -- open -----------------------------------------------------------------------
    def open(self, case_type: str, *, by: str, severity: Optional[str] = None,
             severity_rule: Optional[str] = None, owner: Optional[str] = None,
             signals: Iterable[str] = (), signal_group: Optional[str] = None,
             detecting_monitors: Iterable[str] = (), note: str = "opened") -> Case:
        ct = CaseType(case_type).value
        if ct == "INC" and (severity is None or not severity_rule):
            raise GuardViolation("an INC needs a severity and the rule that set it")
        spec = self._monitors()
        refs = []
        for mid in detecting_monitors:
            if mid not in spec:
                raise GuardViolation(f"detecting monitor {mid} is not in the monitoring spec")
            refs.append(MonitorRef(monitor_id=mid, spec_hash=monitor_hash(spec[mid])))
        case = Case(id=self._next_id(ct), type=ct, product=self.ws.dp_id, state=INITIAL[ct],
                    severity=severity, severity_rule=severity_rule, owner=owner,
                    signals=list(signals), signal_group=signal_group, detecting_monitors=refs,
                    timeline=[TimelineEntry(by=by, event=note)])
        self._save(case)
        return case

    def link(self, case_id: str, other_id: str, *, reason: str, by: str) -> Case:
        case = self.get(case_id)
        case.linked_cases.append(LinkedCase(id=other_id, reason=reason))
        case.timeline.append(TimelineEntry(by=by, event=f"linked {other_id}: {reason}"))
        self._save(case)
        return case

    def update(self, case_id: str, *, by: str, note: str, **fields) -> Case:
        """Change non-lifecycle fields (owner, next action, trust status, signals...)."""
        if "state" in fields or "failed_attempts" in fields:
            raise GuardViolation("state and attempt count change only through transition()")
        case = self.get(case_id)
        data = case.model_dump()
        data.update(fields)
        data["timeline"] = [*case.timeline, TimelineEntry(by=by, event=note)]
        case = Case.model_validate(data)
        self._save(case)
        return case

    # -- monitors and results (read-only for guards) --------------------------------
    def _monitors(self) -> dict[str, dict]:
        spec = self.ws.read_yaml("service/monitoring-spec.yaml") or {}
        return {m["id"]: m for m in spec.get("monitors", [])}

    def _results(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        root = self.ws.path("signals/monitor-results")
        if root.exists():
            for f in sorted(root.glob("*.jsonl")):
                for line in f.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        r = json.loads(line)
                        out[r["id"]] = r
        return out

    def _latest_result(self, monitor_id: str) -> Optional[dict]:
        latest = None
        for r in self._results().values():
            if r["monitor_id"] == monitor_id and (latest is None or r["time"] >= latest["time"]):
                latest = r
        return latest

    # -- transition -----------------------------------------------------------------
    def transition(self, case_id: str, to_state: str, *, by: str, note: str = "",
                   verification_results: Iterable[str] = (), decision: Optional[str] = None) -> Case:
        case = self.get(case_id)
        machine = TRANSITIONS[case.type]
        if to_state not in machine:
            raise IllegalTransition(f"{case_id}: {to_state!r} is not a {case.type} state; "
                                    f"states are {sorted(machine)}")
        allowed = machine[case.state]
        if to_state not in allowed:
            raise IllegalTransition(
                f"{case_id}: illegal transition {case.state} -> {to_state}; "
                f"allowed from {case.state}: {sorted(allowed) or 'none (terminal)'}")

        guard = getattr(self, "_guard_" + to_state.replace("-", "_"), None)
        if guard:
            guard(case, verification_results=list(verification_results), decision=decision)

        case.state = to_state
        if to_state == "failed":
            case.failed_attempts += 1
            if case.failed_attempts >= MAX_FAILED_ATTEMPTS and not case.escalated:
                case.escalated = True
                case.escalation_reason = f"{case.failed_attempts} failed remediation attempts"
                escalate(self.ws, by="case_store", case_id=case.id, severity=case.severity,
                         reason=f"{case.id}: {case.escalation_reason}; stop and escalate")
        if decision:
            case.decision = decision
        case.timeline.append(TimelineEntry(by=by, event=f"-> {to_state}" + (f": {note}" if note else "")))
        self._save(case)
        return case

    # -- guards ---------------------------------------------------------------------
    def _guard_executing(self, case: Case, **_) -> None:
        if case.type == "INC" and case.failed_attempts >= MAX_FAILED_ATTEMPTS:
            raise GuardViolation(f"{case.id}: {case.failed_attempts} failed attempts; no further "
                                 "execution. Stop and escalate", escalate=True)

    def _check_verification(self, case: Case, result_ids: list[str]) -> None:
        if not self.ws.has(f"cases/{case.id}/verification.md"):
            raise GuardViolation(f"{case.id}: no verification.md; recovery needs proof")
        if not case.detecting_monitors:
            raise GuardViolation(f"{case.id}: no detecting monitor recorded; cannot verify recovery")
        results = self._results()
        covered = set()
        for rid in result_ids:
            r = results.get(rid)
            if r is None:
                raise GuardViolation(f"{case.id}: verification cites unknown monitor result {rid}")
            if r["status"] != "pass":
                raise GuardViolation(f"{case.id}: monitor result {rid} is {r['status']}, not pass")
            covered.add(r["monitor_id"])
        missing = [m.monitor_id for m in case.detecting_monitors if m.monitor_id not in covered]
        if missing:
            raise GuardViolation(f"{case.id}: verification lacks passing results for detecting "
                                 f"monitor(s) {missing}")

    def _guard_recovered(self, case: Case, verification_results: list[str], **_) -> None:
        self._check_verification(case, verification_results)

    def _guard_reviewed(self, case: Case, **_) -> None:
        if not self.ws.has(f"cases/{case.id}/post-incident.md"):
            raise GuardViolation(f"{case.id}: no post-incident note; cannot mark reviewed")

    def _guard_decided(self, case: Case, decision: Optional[str], **_) -> None:
        if decision not in ("accepted", "rejected", "deferred"):
            raise GuardViolation(f"{case.id}: a FIN is decided only as accepted, rejected or deferred")

    def _guard_closed(self, case: Case, **_) -> None:
        if case.type != "INC":
            return
        if not self.ws.has(f"cases/{case.id}/verification.md"):
            raise GuardViolation(f"{case.id}: cannot close without verification")
        if not self.ws.has(f"cases/{case.id}/post-incident.md"):
            raise GuardViolation(f"{case.id}: cannot close without a post-incident note")
        spec = self._monitors()
        for ref in case.detecting_monitors:
            m = spec.get(ref.monitor_id)
            if m is None:
                raise GuardViolation(f"{case.id}: detecting monitor {ref.monitor_id} was removed")
            if not m.get("enabled", True):
                raise GuardViolation(f"{case.id}: detecting monitor {ref.monitor_id} is disabled")
            if monitor_hash(m) != ref.spec_hash:
                raise GuardViolation(f"{case.id}: detecting monitor {ref.monitor_id} changed since detection")
            latest = self._latest_result(ref.monitor_id)
            if latest is None or latest["status"] != "pass":
                raise GuardViolation(f"{case.id}: detecting monitor {ref.monitor_id} is not passing")

    def has_open_s1(self) -> bool:
        return any(c.type == "INC" and c.severity == "S1" and c.state != "closed" for c in self.all())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
