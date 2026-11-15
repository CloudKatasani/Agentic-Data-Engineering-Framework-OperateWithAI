"""Council-of-three review for S1 diagnoses (spec section 5.2; Diagnosis decision rules).

Three named, distinct reviewers (people or independent reviewer agents) each record whether
they agree with the diagnosis. Remediation presents options for an S1 only once a majority
of three agrees.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..core.errors import GuardViolation
from .workspace import AGENTS, Workspace

SIZE = 3


class Council:
    identity = "council"

    def __init__(self, ws: Workspace):
        self.ws = ws

    def _path(self, case_id: str) -> str:
        return f"cases/{case_id}/council.yaml"

    def verdicts(self, case_id: str) -> list[dict]:
        return (self.ws.read_yaml(self._path(case_id)) or {}).get("verdicts", [])

    def record(self, case_id: str, *, reviewer: str, agrees: bool, note: str = "") -> list[dict]:
        if reviewer == "diagnosis" or reviewer == "adef-ops-incident-diagnosis":
            raise GuardViolation("the diagnosing agent cannot review its own diagnosis")
        if reviewer in AGENTS:
            raise GuardViolation(f"{reviewer!r} is a team agent; council reviewers are independent")
        v = self.verdicts(case_id)
        if any(x["reviewer"] == reviewer for x in v):
            raise GuardViolation(f"{reviewer} has already reviewed {case_id}")
        if len(v) >= SIZE:
            raise GuardViolation(f"{case_id} already has {SIZE} council verdicts")
        v.append({"reviewer": reviewer, "agrees": agrees, "note": note,
                  "at": datetime.now(timezone.utc).isoformat()})
        self.ws.write_yaml(self.identity, self._path(case_id), {"case": case_id, "verdicts": v})
        return v

    def status(self, case_id: str) -> str:
        v = self.verdicts(case_id)
        agree = sum(1 for x in v if x["agrees"])
        if agree >= 2:
            return "agreed"
        if len(v) - agree >= 2:
            return "disagreed"
        return "pending"
