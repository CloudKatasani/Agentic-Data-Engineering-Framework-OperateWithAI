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
            "pir": knowledge.decide, "problem": knowledge.decide, "kedb": knowledge.decide}


class Service:
    def __init__(self, client: str = "demo", data: Optional[str] = None):
        self.client, self.data = client, data
        self.lock = threading.RLock()
        self.runs = 0
        self.reset([])

    def reset(self, scenarios: list[str], learned: Optional[list[dict]] = None) -> None:
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
            batch_sla.run(self.ops)
            self.ops.audit("Service", "Night started", "", f"Scenarios: {', '.join(self.scenarios) or 'none'}")

    def restart(self, scenarios: list[str]) -> None:
        """Start a new night. Known errors the team has learned are kept."""
        self.reset(scenarios, learned=self.store.all("ke"))

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
            self._settle()
            return n

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
