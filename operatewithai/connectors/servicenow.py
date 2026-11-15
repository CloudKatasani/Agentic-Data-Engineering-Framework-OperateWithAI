"""ServiceNow mapping. The client's ITSM stays the system of record: every ticket here is
mirrored as a ServiceNow record. This module builds the Table API payload; sending it needs
the client's instance and credentials, so in the demo the payload is kept and shown."""

from __future__ import annotations

STATE = {"New": 1, "Assigned": 2, "In Progress": 2, "On Hold": 3, "Resolved": 6, "Closed": 7}
TABLE = {"incident": "incident", "request": "sc_req_item", "problem": "problem"}
LEVEL = {"high": 1, "medium": 2, "low": 3}


def payload(ticket: dict, tower_name: str) -> dict:
    body = {
        "table": TABLE[ticket["type"]], "number": ticket["id"], "short_description": ticket["title"],
        "priority": int(ticket["priority"][1]), "state": STATE.get(ticket["state"], 2),
        "assignment_group": tower_name, "assigned_to": ticket.get("assignee") or "",
        "cmdb_ci": (ticket.get("cis") or [""])[0], "opened_at": ticket["opened_at"],
        "work_notes": "\n".join(f"{t['at'][11:16]} {t['actor']}: {t['text']}" for t in ticket.get("timeline", [])[-8:]),
    }
    if ticket["type"] == "incident":
        body.update(impact=LEVEL[ticket["impact"]], urgency=LEVEL[ticket["urgency"]], category="Data Platform",
                    u_known_error=ticket.get("ke") or "", close_notes=(ticket.get("resolution") or {}).get("notes", ""))
    return body
