"""Notifications: how agents bring people in.

Every notification goes to a role and the person who holds it, asks for one kind of action,
and carries a response deadline. Agents create them; only people resolve them (by taking
the action, or by dismissing with a reason). Missing a deadline escalates to the next role.

Delivery: the console inbox always. Optionally a webhook (``ADEF_NOTIFY_WEBHOOK``, a
Slack-compatible ``{"text": ...}`` POST) for anything that needs action. In shadow mode
nothing leaves the console. The log is hash-chained (``control/notifications.jsonl``).
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..core.errors import GuardViolation
from .workspace import Workspace

PATH = "control/notifications.jsonl"
KINDS = ("acknowledge", "review", "decide", "run", "confirm", "fyi", "escalation")
# Response deadline by priority (P1 = S1, P2 = S2, P3 = S3 and decisions, P4 = informational).
DUE = {"P1": timedelta(minutes=15), "P2": timedelta(minutes=30), "P3": timedelta(hours=4), "P4": None}
ESCALATE_TO = {"on_call": "platform_owner", "service_owner": "platform_owner", "data_steward": "data_owner",
               "council": "platform_owner"}
SEV_PRIORITY = {"S1": "P1", "S2": "P2", "S3": "P3", "S4": "P4"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Notifier:
    identity = "notifier"

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.log = ws.chainlog(self.identity, PATH)

    # -- people ---------------------------------------------------------------------------
    def svc(self) -> dict:
        return self.ws.read_yaml("control/service.yaml") or {}

    def holder(self, role: str) -> Optional[str]:
        svc = self.svc()
        if role == "on_call":
            return svc.get("on_call")
        return (svc.get("approvers") or {}).get(role)

    def council(self) -> list[str]:
        from ..agents.team import COUNCIL_ROLES
        names = []
        for r in COUNCIL_ROLES:
            n = self.holder(r)
            if n and n not in names:
                names.append(n)
        return names

    def roles_of(self, person: str) -> list[str]:
        svc = self.svc()
        roles = [r for r, n in (svc.get("approvers") or {}).items() if n == person]
        if svc.get("on_call") == person:
            roles.append("on_call")
        if person in self.council():
            roles.append("council")
        return roles

    # -- state ----------------------------------------------------------------------------
    def all(self) -> list[dict]:
        items: dict[str, dict] = {}
        for e in self.log.entries():
            r = e["record"]
            if r["event"] == "created":
                items[r["id"]] = {**r["notification"], "status": "open", "history": [], "deliveries": []}
            elif r["id"] in items:
                n = items[r["id"]]
                if r["event"] == "delivered":
                    n["deliveries"].append(r["delivery"])
                else:
                    n["history"].append({"event": r["event"], "by": r.get("by"), "at": e["at"], "note": r.get("note")})
                    if r["event"] in ("acknowledged", "done", "dismissed", "escalated", "superseded"):
                        n["status"] = {"acknowledged": "acknowledged", "done": "done", "dismissed": "dismissed",
                                       "escalated": "escalated", "superseded": "done"}[r["event"]]
        return sorted(items.values(), key=lambda n: n["created_at"], reverse=True)

    def get(self, nid: str) -> dict:
        n = next((x for x in self.all() if x["id"] == nid), None)
        if n is None:
            raise KeyError(f"notification {nid}")
        return n

    def open_items(self) -> list[dict]:
        return [n for n in self.all() if n["status"] in ("open", "acknowledged")]

    # -- create ---------------------------------------------------------------------------
    def notify(self, *, from_agent: str, to_role: str, kind: str, title: str, body: str = "",
               priority: str = "P3", case_id: Optional[str] = None, request_id: Optional[str] = None,
               actions: Iterable[str] = (), link: Optional[dict] = None, to_person: Optional[str] = None,
               dedupe_key: Optional[str] = None) -> Optional[dict]:
        if kind not in KINDS:
            raise ValueError(f"unknown notification kind {kind}")
        key = dedupe_key or f"{kind}:{to_role}:{to_person or ''}:{case_id or ''}:{request_id or ''}"
        if any(n["dedupe_key"] == key for n in self.open_items()):
            return None  # one open ask per decision and person
        person = to_person or self.holder(to_role)
        created = now_utc()
        due = DUE.get(priority)
        shadow = self.svc().get("mode") == "shadow"
        n = {"id": f"NTF-{len(self.all()) + 1:04d}", "product": self.ws.dp_id, "from_agent": from_agent,
             "to_role": to_role, "to_person": person or "UNNAMED", "kind": kind, "priority": priority,
             "title": title, "body": body, "case_id": case_id, "request_id": request_id,
             "actions": list(actions), "link": link or {}, "created_at": created.isoformat(),
             "due_at": (created + due).isoformat() if due and kind != "fyi" else None,
             "dedupe_key": key, "shadow": shadow}
        self.log.append({"event": "created", "id": n["id"], "notification": n})
        if kind != "fyi" and not shadow:
            self._deliver(n)
        return n

    def _deliver(self, n: dict) -> None:
        url = os.environ.get("ADEF_NOTIFY_WEBHOOK")
        if not url:
            return
        text = (f"[{n['priority']}] {n['product']} {n['case_id'] or ''} - {n['title']}\n"
                f"For {n['to_person']} ({n['to_role'].replace('_', ' ')}). Asks: {', '.join(n['actions']) or n['kind']}."
                + (f" Respond by {n['due_at']}." if n["due_at"] else ""))
        req = urllib.request.Request(url, data=json.dumps({"text": text, "notification": {
            k: n[k] for k in ("id", "product", "kind", "priority", "to_role", "to_person", "case_id", "title")}}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = {"channel": "webhook", "ok": 200 <= resp.status < 300, "status": resp.status}
        except Exception as exc:  # delivery never blocks operations; the console inbox always has it
            result = {"channel": "webhook", "ok": False, "error": type(exc).__name__}
        self.log.append({"event": "delivered", "id": n["id"], "delivery": result})

    # -- resolve --------------------------------------------------------------------------
    def _named(self) -> set[str]:
        svc = self.svc()
        return set((svc.get("approvers") or {}).values()) | {svc.get("on_call")}

    def record(self, nid: str, event: str, *, by: str, note: str = "") -> None:
        if by not in self._named():
            from ..core.errors import ApprovalError
            raise ApprovalError(f"{by!r} is not a named person for {self.ws.dp_id}; only people resolve notifications")
        n = self.get(nid)
        if n["status"] in ("done", "dismissed", "escalated"):
            raise GuardViolation(f"{nid} is already {n['status']}")
        if event == "dismissed" and not note.strip():
            raise GuardViolation("dismissing needs a reason")
        self.log.append({"event": event, "id": nid, "by": by, "note": note[:300]})

    def resolve_for(self, *, case_id: Optional[str] = None, request_id: Optional[str] = None,
                    kinds: Iterable[str] = (), by: str = "system", note: str = "") -> None:
        """Close open asks that a later event made moot (e.g. the case recovered)."""
        kinds = set(kinds)
        for n in self.open_items():
            if (case_id and n["case_id"] == case_id or request_id and n["request_id"] == request_id) \
                    and (not kinds or n["kind"] in kinds):
                self.log.append({"event": "superseded", "id": n["id"], "by": by, "note": note})

    # -- deadlines ------------------------------------------------------------------------
    def check_deadlines(self, at: Optional[datetime] = None) -> list[dict]:
        at = at or now_utc()
        raised = []
        for n in self.open_items():
            if n["status"] != "open" or not n["due_at"] or datetime.fromisoformat(n["due_at"]) > at:
                continue
            nxt = ESCALATE_TO.get(n["to_role"], "platform_owner")
            if nxt == n["to_role"]:
                nxt = "tech_lead"
            self.log.append({"event": "escalated", "id": n["id"], "by": "notifier",
                             "note": f"no response by {n['due_at']}; escalated to {nxt}"})
            from .escalation import escalate
            escalate(self.ws, by="notifier", to=nxt, case_id=n["case_id"],
                     reason=f"{n['id']} to {n['to_person']} ({n['to_role']}) not answered by {n['due_at']}: {n['title']}")
            esc = self.notify(from_agent="commander", to_role=nxt, kind="escalation", priority=n["priority"],
                              title=f"Escalated: {n['title']}",
                              body=f"{n['to_person']} ({n['to_role'].replace('_', ' ')}) did not respond in time. {n['body']}",
                              case_id=n["case_id"], request_id=n["request_id"],
                              actions=n["actions"] or ["acknowledge"], link=n["link"],
                              dedupe_key=f"esc:{n['id']}")
            if esc:
                raised.append(esc)
        return raised
