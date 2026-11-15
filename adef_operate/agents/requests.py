"""Operator requests at the Commander's front door.

People under pressure ask for shortcuts. Each request is classified by rule, never
executed directly, and answered with the governed route. The refusals mirror the
guardrail scenarios in OPERATE_SPEC section 13.2 (OT-02, OT-03, OT-07, OT-08, OT-16).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from ..core.errors import GuardViolation
from ..engine.isolation import RULE as ISOLATION_RULE, check_same_client
from ..engine.workspace import Workspace

INTENTS = [
    ("mute_alert", r"\b(mute|silence|snooze|disable|turn off|switch off)\b.{0,40}\b(alert|alerts|monitor|monitoring|page|pages)\b"),
    ("lower_threshold", r"\b(lower|loosen|relax|raise|bump|widen|drop)\b.{0,40}\b(threshold|tolerance|rule|check|expectation)\b"
                        r"|\bignore\b.{0,30}\b(dq|quality) (rule|check)\b"),
    ("scale_up", r"\b(bigger|larger|scale up|upsize|resize)\b.{0,40}\b(warehouse|cluster|compute|capacity|slots?)\b"),
    ("drop_object", r"\b(drop|delete|truncate|purge)\b.{0,30}\b(table|object|dataset|view)\b"),
    ("copy_from_other", r"\b(copy|reuse|clone|import|take)\b.{0,60}\b(runbook|monitor(ing)?( spec)?|playbook|case|replay)s?\b"
                        r".{0,60}\b(from|of)\b"),
    ("access_change", r"\b(grant|revoke|give|remove)\b.{0,40}\b(access|admin|role|permission|privilege)s?\b"),
]
RX = [(i, re.compile(p, re.I)) for i, p in INTENTS]


class RequestDesk:
    identity = "commander"

    def __init__(self, ws: Workspace):
        self.ws = ws

    @staticmethod
    def classify(text: str) -> str:
        for intent, rx in RX:
            if rx.search(text):
                return intent
        return "question"

    def handle(self, text: str, *, by: str, other: Optional[Workspace] = None) -> dict:
        intent = self.classify(text)
        if intent == "mute_alert":
            out = {"decision": "refused", "reason": "Alerts are never muted to make a problem go away; the close "
                   "guard also rejects a case whose detecting monitor was disabled or changed.",
                   "alternative": "Sentinel prepares a tuning proposal replayed on history, decided at OG7.",
                   "gate": "OG7"}
        elif intent == "lower_threshold":
            out = {"decision": "refused", "reason": "A failing quality rule or threshold is not loosened to pass.",
                   "alternative": "Request a time-limited waiver from the Data Owner, recorded at OG7 with an expiry; "
                                  "the rule stays as approved.", "gate": "OG7"}
        elif intent == "scale_up":
            out = {"decision": "refused", "reason": "Scaling up to make a failure go away hides the cause.",
                   "alternative": "FinOps presents it as a cost trade-off at OG4 alongside the fix.", "gate": "OG4"}
        elif intent == "drop_object":
            out = {"decision": "refused", "reason": "Dropping needs lineage and Consume usage evidence, not telemetry alone.",
                   "alternative": "FinOps raises an unused-object finding when telemetry, usage and lineage agree; "
                                  "drop is A3 at OG4.", "gate": "OG4"}
        elif intent == "copy_from_other":
            if other is None:
                out = {"decision": "refused", "reason": "The source product is not identified.",
                       "alternative": "Name the source product; only same-client products qualify.", "gate": None}
            else:
                try:
                    check_same_client(other, self.ws)
                    out = {"decision": "routed", "reason": "Same client.",
                           "alternative": "Onboarding drafts an adapted copy; it takes effect only through OG1.",
                           "gate": "OG1"}
                except GuardViolation:
                    out = {"decision": "refused", "reason": ISOLATION_RULE,
                           "alternative": "Onboarding writes this product's runbook from its own handover pack.",
                           "gate": None}
        elif intent == "access_change":
            out = {"decision": "refused", "reason": "No agent grants or revokes access.",
                   "alternative": "Governance records the request; Security and Compliance decide at OG5 and "
                                  "people execute it.", "gate": "OG5"}
        else:
            out = {"decision": "routed", "reason": "Not a control change.", "alternative": "Queued for the Commander.",
                   "gate": None}
        out = {"intent": intent, "by": by, **out}
        self.ws.append(self.identity, f"health/requests-{datetime.now(timezone.utc).date().isoformat()}.jsonl",
                       json.dumps({**out, "text_chars": len(text)}))
        return out
