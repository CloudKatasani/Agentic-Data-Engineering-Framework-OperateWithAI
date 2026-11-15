"""Escalation log: every escalation is appended to a hash-chained log for people to act on."""

from __future__ import annotations

from typing import Optional

from .workspace import Workspace


def escalate(ws: Workspace, *, by: str, reason: str, to: str = "platform_owner",
             case_id: Optional[str] = None, severity: Optional[str] = None) -> dict:
    record = {"type": "escalation", "by": by, "to": to, "reason": reason,
              "case_id": case_id, "severity": severity}
    return ws.chainlog("escalations", "control/escalations.jsonl").append(record)


def escalations(ws: Workspace) -> list[dict]:
    if not ws.has("control/escalations.jsonl"):
        return []
    return [e["record"] for e in ws.chainlog("escalations", "control/escalations.jsonl").entries()]
