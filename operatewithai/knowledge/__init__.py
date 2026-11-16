"""The knowledge the agents work from: known errors with runbooks, the action catalogue,
the request catalogue and the connector list. Known errors learned from resolved incidents
are kept in the store and matched the same way."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

DIR = Path(__file__).parent


def _load(name: str) -> dict:
    return yaml.safe_load((DIR / name).read_text("utf-8"))


class KB:
    def __init__(self, store=None):
        self.store = store
        self.base = _load("kedb.yaml")["known_errors"]
        acts = _load("actions.yaml")
        self.actions = {a["id"]: a for a in acts["actions"]}
        self.never = acts["never"]
        self.requests = {r["id"]: r for r in _load("requests.yaml")["catalogue"]}
        self.signals = _load("signals.yaml")["tools"]
        self.endpoints = _load("endpoints.yaml")["tools"]

    def known_errors(self) -> list[dict]:
        learned = self.store.all("ke") if self.store else []
        return self.base + learned

    def get(self, ke_id: Optional[str]) -> Optional[dict]:
        return next((k for k in self.known_errors() if k["id"] == ke_id), None)

    def match(self, codes: list[str], message: str = "") -> Optional[dict]:
        """The known error for an alert. A specific cause wins over a generic symptom."""
        text = (message or "").lower()
        hits = []
        for ke in self.known_errors():
            for sig in ke["signatures"]:
                if sig in codes or (sig.startswith("text:") and sig[5:].lower() in text):
                    hits.append(ke)
                    break
        hits.sort(key=lambda k: bool(k.get("symptom")))
        return hits[0] if hits else None
