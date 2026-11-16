"""Synthetic incident history for the last 30 days of the demo client, so recurrence
detection and the service review have something to work on. Replace with an export from
the client's ITSM for a real engagement."""

from __future__ import annotations

# (days ago, known error, priority, minutes to acknowledge, minutes to resolve)
ROWS = [
    (29, "KE0002", "P2", 9, 64), (28, "KE0009", "P3", 22, 190), (27, "KE0101", "P3", 31, 140), (26, "KE0007", "P2", 12, 88),
    (25, "KE0002", "P2", 14, 71), (24, "KE0105", "P2", 18, 205), (23, "KE0009", "P3", 35, 240), (21, "KE0004", "P2", 11, 96),
    (20, "KE0107", "P3", 44, 310), (19, "KE0002", "P2", 8, 58), (18, "KE0104", "P2", 16, 122), (17, "KE0006", "P1", 6, 74),
    (16, "KE0009", "P3", 28, 175), (14, "KE0108", "P3", 52, 420), (13, "KE0007", "P2", 19, 101), (12, "KE0102", "P1", 7, 236),
    (11, "KE0005", "P3", 40, 260), (10, "KE0002", "P2", 13, 66), (8, "KE0106", "P3", 37, 180), (7, "KE0009", "P3", 26, 150),
    (6, "KE0001", "P2", 15, 133), (5, "KE0007", "P2", 21, 93), (4, "KE0103", "P1", 9, 281), (2, "KE0004", "P2", 10, 84),
]


def history(ops) -> list[dict]:
    out = []
    if ops.env.client.get("id") != "demo":      # the sample history belongs to the first demo client
        return out
    for days, ke_id, prio, mtta, mttr in ROWS:
        ke = ops.kb.get(ke_id)
        sla = ops.env.sla(prio)
        out.append({"days_ago": days, "ke": ke_id, "title": ke["title"], "tower": ke["tower"], "priority": prio, "mtta": mtta, "mttr": mttr,
                    "respond_met": mtta <= sla["respond"], "resolve_met": mttr <= sla["resolve"]})
    return out


def occurrences(ops, ke_id: str) -> int:
    return sum(1 for r in ROWS if r[1] == ke_id) if ops.env.client.get("id") == "demo" else 0
