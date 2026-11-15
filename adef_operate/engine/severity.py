"""Severity by rule, not by feel (spec section 5.2; Commander T2).

Returns the severity, the rule that set it, any Domain Pack override applied, and what
evidence would lower it. A Domain Pack can raise a severity, never lower it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .lineage import Lineage

ORDER = ["S1", "S2", "S3", "S4"]
INTEGRITY = {"quality", "volume", "schema"}
REGULATORY_TAGS = {"regulated", "regulatory"}


def more_severe(a: str, b: str) -> str:
    return a if ORDER.index(a) <= ORDER.index(b) else b


@dataclass
class Assessment:
    severity: str
    rule: str
    base_severity: str
    override: Optional[str] = None
    would_lower: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def rule_text(self) -> str:
        return self.rule + (f"; raised by Domain Pack: {self.override}" if self.override else "")


def assess(group: dict[str, Any], signals: list[dict], service_def: dict, domain_pack: dict,
           monitors: dict[str, dict], object_tags: dict[str, list[str]]) -> Assessment:
    lineage = Lineage.from_service(service_def)
    classes = {s["signal_class"] for s in signals}
    objects = {s["object"] for s in signals}
    consumers = bool(service_def.get("consumers"))
    fresh_obj = {o["object"] for o in service_def.get("objectives", []) if o["kind"] == "freshness"}
    complete_obj = {o["object"] for o in service_def.get("objectives", []) if o["kind"] == "completeness"}

    def consumer_facing(o: str) -> bool:
        # Published objects: leaves of the lineage, or objects carrying a timeliness promise.
        return consumers and (lineage.is_leaf(o) or o in fresh_obj)
    tags = {o: set(object_tags.get(o, [])) for o in objects}

    def integrity_on(pred) -> list[str]:
        return sorted(s["object"] for s in signals if s["signal_class"] in INTEGRITY and pred(s["object"]))

    if "access" in classes:
        a = Assessment("S1", "S1: suspected security event (access outside the approved model)", "S1",
                       would_lower="Governance shows the grant is inside the approved access model")
    elif hit := integrity_on(consumer_facing):
        a = Assessment("S1", f"S1: wrong data reached consumers ({', '.join(hit)} is consumer-facing)", "S1",
                       would_lower="Diagnosis shows the affected data did not reach consumer-facing objects")
    elif hit := integrity_on(lambda o: tags[o] & REGULATORY_TAGS):
        a = Assessment("S1", f"S1: a regulatory dataset is affected ({', '.join(hit)})", "S1",
                       would_lower="Diagnosis shows the regulated values are unaffected")
    elif all(monitors.get(s.get("monitor_id") or "", {}).get("severity_hint") == "S4" for s in signals):
        a = Assessment("S4", "S4: informational (only S4 monitors fired)", "S4")
    elif "consumer" in classes:
        a = Assessment("S2", "S2: consumer-reported issue (at least S2 until Diagnosis shows otherwise)", "S2",
                       would_lower="Diagnosis shows no defect")
    elif "slo" in classes or (objects & fresh_obj and classes & {"freshness", "run"}) or \
            integrity_on(lambda o: o in complete_obj):
        a = Assessment("S2", "S2: a service-level objective is breached", "S2",
                       would_lower="the objective recovers inside the current window")
    elif classes & {"freshness", "run"} and any(lineage.descendants(o) & fresh_obj for o in objects):
        a = Assessment("S2", "S2: a service-level objective will be breached within the current window "
                       "(upstream of a freshness objective)", "S2",
                       would_lower="the load lands with enough time left in the window")
    else:
        a = Assessment("S3", "S3: degradation with no consumer impact yet", "S3",
                       would_lower="the signal is shown to be noise")

    if a.severity == "S4":
        return a  # informational only: no dataset is affected, so no override applies
    for ov in domain_pack.get("severity_overrides", []):
        tag = (ov.get("match") or {}).get("object_tag")
        if tag and any(tag in t for t in tags.values()):
            raised = more_severe(a.severity, ov["min_severity"])
            if raised != a.severity:
                a.override = f"{ov.get('reason', tag)} -> at least {ov['min_severity']}"
                a.severity = raised
    return a
