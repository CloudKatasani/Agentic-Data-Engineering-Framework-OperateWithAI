"""The operations core shared by every agent: alerts, tickets with SLA clocks, and
notifications to named people. Agents call these; people act through the API."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from ..connectors import servicenow
from ..env import Environment
from ..knowledge import KB
from ..store import Store

OPEN_STATES = ("New", "Assigned", "In Progress", "On Hold")
PREFIX = {"incident": ("INC", 41000), "request": ("RITM", 52000), "problem": ("PRB", 3000)}
ESCALATE_TO = ["incident_manager", "sdm"]
ESCALATING = ("ack", "plan", "approve", "manual", "blocked", "resolve", "comms", "major")   # incident work; requests and reviews wait


class Refused(Exception):
    """A person tried something they are not allowed to do, or that no longer applies."""


class Ops:
    def __init__(self, env: Environment, store: Store, estate: Any):
        self.env, self.store, self.estate = env, store, estate
        self.kb = KB(store)
        self.projection: dict = {}

    @property
    def now(self) -> datetime:
        return self.estate.now

    def audit(self, actor: str, action: str, ref: str = "", detail: str = "", human: bool = False) -> None:
        self.store.audit(at=self.now, actor=actor, actor_type="person" if human else "agent", action=action, ref=ref, detail=detail)

    # ------------------------------------------------------------------ alerts
    def ingest(self, alerts: list[dict]) -> tuple[list[dict], list[dict]]:
        """De-duplicate alerts by fingerprint. Returns (new events, events that cleared)."""
        open_by_fp = {e["fingerprint"]: e for e in self.store.all("event") if e["status"] == "open"}
        new, cleared = [], []
        for a in alerts:
            ev = open_by_fp.get(a["fingerprint"])
            if a["severity"] == "clear":
                if ev:
                    ev.update(status="cleared", cleared_at=self.now.isoformat())
                    self.store.put("event", ev)
                    del open_by_fp[a["fingerprint"]]
                    cleared.append(ev)
                continue
            if ev:
                ev["count"] += 1
                ev["last_seen"] = self.now.isoformat()
                ev["codes"] += [c for c in a["codes"] if c not in ev["codes"]]
                if len(a["message"]) > len(ev["message"]):
                    ev["message"] = a["message"]
                self.store.put("event", ev)
                continue
            ev = {**a, "id": f"EV{self.store.next_number('EV', 0):05d}", "status": "open", "count": 1, "incident": None,
                  "first_seen": self.now.isoformat(), "last_seen": self.now.isoformat(), "handling": ""}
            self.store.put("event", ev)
            open_by_fp[a["fingerprint"]] = ev
            new.append(ev)
        return new, cleared

    # ------------------------------------------------------------------ tickets
    def tickets(self, type_: Optional[str] = None, open_only: bool = False) -> list[dict]:
        out = [t for t in self.store.all("ticket") if (not type_ or t["type"] == type_)]
        return [t for t in out if t["state"] in OPEN_STATES] if open_only else out

    def open_ticket(self, type_: str, title: str, priority: str, tower: str, by: str, **fields) -> dict:
        prefix, start = PREFIX[type_]
        sla = self.env.sla(priority)
        t = {"id": f"{prefix}{self.store.next_number(prefix, start):07d}", "type": type_, "title": title, "priority": priority,
             "state": "New", "tower": tower, "assignee": self.env.on_call(tower), "opened_at": self.now.isoformat(), "opened_by": by,
             "ack_at": None, "resolved_at": None, "timeline": [], "cis": [],
             "sla": {"respond_due": (self.now + timedelta(minutes=sla["respond"])).isoformat(),
                     "resolve_due": (self.now + timedelta(minutes=sla["resolve"])).isoformat(), "respond_met": None, "resolve_met": None},
             **fields}
        self.note(t, by, f"Opened as {priority}: {title}")
        return t

    def note(self, t: dict, actor: str, text: str, human: bool = False) -> None:
        t["timeline"].append({"at": self.now.isoformat(), "actor": actor, "human": human, "text": text})
        self.save(t)
        self.audit(actor, text[:80], t["id"], text, human)

    def save(self, t: dict) -> dict:
        t["servicenow"] = servicenow.payload(t, self.env.tower_name(t["tower"]))
        return self.store.put("ticket", t)

    def acknowledge(self, t: dict, person: str) -> None:
        if t["ack_at"]:
            return
        t["ack_at"] = self.now.isoformat()
        t["sla"]["respond_met"] = self.now <= datetime.fromisoformat(t["sla"]["respond_due"])
        t["assignee"] = person
        if t["state"] in ("New", "Assigned"):
            t["state"] = "In Progress"
        self.note(t, person, "Acknowledged; working on it", human=True)
        self.close_notes(t["id"], kinds=("ack",), reason="acknowledged")

    def set_priority(self, t: dict, priority: str, by: str, why: str) -> None:
        if priority == t["priority"]:
            return
        old, t["priority"] = t["priority"], priority
        sla = self.env.sla(priority)
        opened = datetime.fromisoformat(t["opened_at"])
        t["sla"]["resolve_due"] = (opened + timedelta(minutes=sla["resolve"])).isoformat()
        if not t["ack_at"]:
            t["sla"]["respond_due"] = (opened + timedelta(minutes=sla["respond"])).isoformat()
        self.note(t, by, f"Priority {old} -> {priority}: {why}")

    def resolve(self, t: dict, by: str, code: str, notes: str, human: bool = True) -> None:
        t["state"] = "Resolved"
        t["resolved_at"] = self.now.isoformat()
        t["sla"]["resolve_met"] = self.now <= datetime.fromisoformat(t["sla"]["resolve_due"])
        t["resolution"] = {"code": code, "notes": notes, "by": by}
        self.note(t, by, f"Resolved ({code}). {notes}".strip(), human=human)
        self.close_notes(t["id"], reason="ticket resolved", keep=("pir", "kedb", "problem", "comms", "fyi"))

    # ------------------------------------------------------------------ notifications
    def notify(self, to: Optional[str], kind: str, ticket: Optional[dict], title: str, body: str, actions: list[dict],
               agent: str, due_minutes: int = 30, step: Optional[int] = None, role: str = "", data: Optional[dict] = None) -> Optional[dict]:
        """Ask a named person for something. One open ask per person, kind, ticket and step."""
        if not to:
            return None
        tid = ticket["id"] if ticket else ""
        for n in self.store.all("note"):
            if n["status"] == "open" and (n["to"], n["kind"], n["ticket"], n["step"]) == (to, kind, tid, step):
                return n
        n = {"id": f"N{self.store.next_number('N', 0):05d}", "to": to, "role": role, "kind": kind, "ticket": tid, "step": step,
             "title": title, "body": body, "actions": actions, "agent": agent, "status": "open", "created": self.now.isoformat(),
             "due": (self.now + timedelta(minutes=due_minutes)).isoformat(), "escalated_to": None, "done_by": None, "done_action": None,
             "priority": ticket["priority"] if ticket else "P4", "data": data or {}}
        self.store.put("note", n)
        self.audit(agent, f"Notified {to}: {title}"[:120], tid, f"{kind} -> {to} ({role or 'on call'}), respond by {n['due'][11:16]}")
        return n

    def notes(self, person: Optional[str] = None, open_only: bool = True) -> list[dict]:
        out = [n for n in self.store.all("note") if not open_only or n["status"] == "open"]
        if person:
            out = [n for n in out if person in (n["to"], n["escalated_to"])]
        return sorted(out, key=lambda n: (n["status"] != "open", n["kind"] == "fyi", n["priority"], n["due"]))

    def done(self, n: dict, actor: str, action: str) -> None:
        n.update(status="done", done_by=actor, done_action=action, done_at=self.now.isoformat())
        self.store.put("note", n)

    def close_notes(self, ticket_id: str, kinds: Optional[tuple] = None, reason: str = "", keep: tuple = ()) -> None:
        for n in self.store.all("note"):
            if n["ticket"] == ticket_id and n["status"] == "open" and (not kinds or n["kind"] in kinds) and n["kind"] not in keep:
                n.update(status="closed", done_action=reason)
                self.store.put("note", n)

    def may_act(self, n: dict, actor: str) -> None:
        if n["status"] != "open":
            raise Refused(f"This was already handled ({n['done_action'] or n['status']}).")
        if actor not in (n["to"], n["escalated_to"]):
            raise Refused(f"Only {n['to']}" + (f" or {n['escalated_to']}" if n["escalated_to"] else "") + " can act on this.")

    def escalate_overdue(self) -> None:
        """An ask that passes its response time goes up: on-call -> incident manager -> SDM."""
        for n in self.store.all("note"):
            if n["status"] != "open" or n["kind"] not in ESCALATING or datetime.fromisoformat(n["due"]) > self.now:
                continue
            chain = [self.env.holder(r) for r in ESCALATE_TO]
            current = n["escalated_to"] or n["to"]
            nxt = next((p for p in chain if p and p != n["to"] and (current not in chain or chain.index(p) > chain.index(current))), None)
            if not nxt or nxt == current:
                continue
            n["escalated_to"] = nxt
            n["due"] = (self.now + timedelta(minutes=15)).isoformat()
            n["escalations"] = n.get("escalations", 0) + 1
            self.store.put("note", n)
            self.audit("Watch Desk Agent", f"Escalated to {nxt}: no response from {current}", n["ticket"], n["title"])
            t = self.store.get("ticket", n["ticket"]) if n["ticket"] else None
            if t:
                self.note(t, "Watch Desk Agent", f"No response from {current} to \"{n['title']}\" in time; escalated to {nxt}")
