"""Paging and chat channel (optional slot ``paging_chat``). The outbox stands in for it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class OutboxPager:
    sent: list[dict] = field(default_factory=list)

    def page(self, *, to: str, role: str, subject: str, case_id: str | None = None) -> dict:
        msg = {"at": datetime.now(timezone.utc).isoformat(), "to": to, "role": role,
               "subject": subject, "case_id": case_id}
        self.sent.append(msg)
        return msg
