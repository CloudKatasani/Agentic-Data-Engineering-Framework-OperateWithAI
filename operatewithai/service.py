"""The running managed service: one client estate, the agent team and the operating loop.

Each cycle the agents run in a fixed order: watch -> triage -> diagnose -> re-plan the
batch -> carry out accepted plans -> requests -> comms -> reviews -> escalate overdue asks.
People act through ``act`` on a notification addressed to them.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from .agents import batch_sla, comms, diagnosis, knowledge, requests, resolution, triage, watch
from .engine.ops import Ops, Refused
from .env import Environment, hhmm
from .sim.scenarios import BY_ID, PRESETS, inject
from .sim.world import TICK, World
from .store import Store

HANDLERS = {"plan": resolution.decide, "approve": resolution.decide, "manual": resolution.decide, "blocked": resolution.decide,
            "resolve": resolution.decide, "comms": comms.decide, "approve_request": requests.decide,
            "pir": knowledge.decide, "problem": knowledge.decide, "kedb": knowledge.decide,
            "detect": triage.confirm, "diagnosis": diagnosis.decide}


class Service:
    def __init__(self, client: str = "demo", data: Optional[str] = None):
        self.client, self.data = client, data
        self.lock = threading.RLock()
        self.runs = 0
        self.received: dict[str, dict] = {}     # per integration endpoint: how much arrived and when
        self.reset([])

    def reset(self, scenarios: list[str], learned: Optional[list[dict]] = None, guided: bool = False) -> None:
        with self.lock:
            self.runs += 1
            self.env = Environment.load(self.client)
            self.world = World(self.env)
            self.scenarios = [s for s in scenarios if s in BY_ID]
            inject(self.world, self.scenarios)
            self.store = Store(f"{self.data}/night-{self.runs}.db" if self.data else ":memory:")
            for ke in learned or []:
                self.store.put("ke", ke)
            self.ops = Ops(self.env, self.store, self.world)
            self.ops.guided = self.guided = guided
            batch_sla.run(self.ops)
            self.ops.audit("Service", "Night started", "", f"Scenarios: {', '.join(self.scenarios) or 'none'}")

    def restart(self, scenarios: list[str], guided: bool = False) -> None:
        """Start a new night. Known errors the team has learned are kept. ``guided`` is the
        step-by-step mode: a person confirms every hand-off from one agent to the next."""
        self.reset(scenarios, learned=self.store.all("ke"), guided=guided)

    def _signature(self) -> tuple:
        return (len(self.store.all("note")), len(self.store.all("event")),
                tuple((t["id"], t["state"], (t.get("plan") or {}).get("status")) for t in self.ops.tickets()))

    def run_to_next(self, limit: str = "08:30") -> bool:
        """Move the clock until something new happens (an alert, an ask, a ticket change).
        Returns False when the night ended without anything new."""
        end, before = hhmm(self.world.day, limit), self._signature()
        while self.world.now < end:
            self.cycle()
            if self._signature() != before:
                return True
        return False

    # ------------------------------------------------------------------ the loop
    def _settle(self) -> None:
        ops = self.ops
        batch_sla.run(ops)
        resolution.run(ops)
        requests.run(ops)
        comms.run(ops)
        knowledge.run(ops)

    def cycle(self) -> None:
        with self.lock:
            ops = self.ops
            self.world.tick()
            new, _ = watch.run(ops)
            triage.run(ops, new)
            diagnosis.run(ops)
            self._settle()
            ops.escalate_overdue()

    def advance(self, minutes: int) -> None:
        for _ in range(max(1, minutes // TICK)):
            self.cycle()

    def run_until(self, hhmm_text: str) -> None:
        end = hhmm(self.world.day, hhmm_text)
        while self.world.now < end:
            self.cycle()

    def run_to_first_issue(self) -> None:
        """Start of a demo: move the clock until the Watch Desk Agent has something to show."""
        while self.world.now.hour < 8 and not any(n["kind"] in ("detect", "ack", "plan") for n in self.ops.notes()):
            if not self.run_to_next():
                break

    # ------------------------------------------------------------------ people
    def act(self, note_id: str, actor: str, action: str, text: str = "") -> dict:
        with self.lock:
            ops = self.ops
            if not self.env.is_person(actor):
                raise Refused(f"{actor} is not one of the seats in the client model.")
            n = self.store.need("note", note_id)
            ops.may_act(n, actor)
            spec = next((a for a in n["actions"] if a["id"] == action), None)
            if not spec:
                raise Refused(f"'{action}' is not one of the choices for this request.")
            if spec.get("required") and not text.strip():
                raise Refused(f"Please fill in: {spec['input']}.")
            ops.done(n, actor, action)
            t = self.store.get("ticket", n["ticket"]) if n["ticket"] else None
            if n["kind"] == "ack":
                ops.acknowledge(t, actor)
            elif n["kind"] == "major":
                t["incident_manager"] = actor
                ops.note(t, actor, "Leading this major incident", human=True)
            elif n["kind"] in HANDLERS:
                HANDLERS[n["kind"]](ops, n, t, actor, action, text.strip())
            else:
                ops.audit(actor, f"Read: {n['title']}"[:100], n["ticket"], human=True)
            diagnosis.run(ops)
            self._settle()
            return n

    def ingest(self, source: str, payload) -> dict:
        """Data pushed to an integration endpoint, in the tool's own format. It goes through
        the same path as collected data: Watch Desk, then triage and the rest."""
        from .connectors.parsers import PARSERS
        if source not in PARSERS:
            raise KeyError(source)
        with self.lock:
            ops = self.ops
            try:
                new, cleared = watch.run(ops, pushed=[(source, payload)])
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise Refused(f"The payload is not in the format this endpoint expects ({type(e).__name__}: {e}).")
            st = self.received.setdefault(source, {"count": 0, "last": None, "alerts": 0})
            st.update(count=st["count"] + 1, last=datetime.now().isoformat(timespec="seconds"), alerts=st["alerts"] + len(new))
            triage.run(ops, new)
            diagnosis.run(ops)
            self._settle()
            fresh = [ops.store.need("event", e["id"]) for e in new]
            return {"accepted": True, "new_alerts": [{"id": e["id"], "message": e["message"], "codes": e["codes"], "handling": e["handling"],
                                                      "incident": e["incident"]} for e in fresh], "cleared": len(cleared)}

    def raise_request(self, cat_id: str, fields: dict, requester: str) -> dict:
        with self.lock:
            if cat_id not in self.ops.kb.requests:
                raise Refused(f"{cat_id} is not in the request catalogue.")
            t = requests.raise_request(self.ops, cat_id, dict(fields), requester)
            self._settle()
            return t

    def comment(self, ticket_id: str, actor: str, text: str) -> dict:
        with self.lock:
            if not self.env.is_person(actor):
                raise Refused(f"{actor} is not one of the seats in the client model.")
            t = self.store.need("ticket", ticket_id)
            self.ops.note(t, actor, text, human=True)
            return t

    @property
    def now(self) -> datetime:
        return self.world.now
