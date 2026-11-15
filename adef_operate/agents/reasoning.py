"""Hypothesis reasoning for Incident Diagnosis.

``Reasoner`` is the model seam. Any implementation (rule-based or model-backed) receives
the evidence and returns scored hypotheses that cite evidence IDs. The Diagnosis agent,
not the reasoner, enforces the contract rules: at least two hypotheses, no cause without
cited evidence, a recent change is a lead only when a failing signal ties it to the
changed object.

``RuleReasoner`` is deterministic, so the replay evaluation is repeatable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from ..engine.evidence import Evidence

CAUSE_CLASSES = ("source data", "upstream outage", "code defect", "configuration", "capacity",
                 "platform", "access", "unknown")

TRANSIENT = re.compile(r"connection reset|timed? ?out|throttl|temporar(il)?y unavailable|node lost", re.I)
CONFIG = re.compile(r"\b(config|parameter|setting|credential expired)\b", re.I)


@dataclass
class Hypothesis:
    cause_class: str
    statement: str
    score: int = 0
    supporting: list[str] = field(default_factory=list)
    against: list[str] = field(default_factory=list)
    status: str = "inconclusive"  # supported | refuted | unsupported | inconclusive


class Reasoner(Protocol):
    name: str

    def hypothesize(self, context: dict, evidence: list[Evidence]) -> list[Hypothesis]: ...


class RuleReasoner:
    name = "rule-reasoner-v1"

    def hypothesize(self, context: dict, evidence: list[Evidence]) -> list[Hypothesis]:
        by = {e.kind: e for e in evidence}
        failing = context["first_failing_object"]
        objects = set(context["objects"])
        classes = set(context["classes"])
        H = {c: Hypothesis(c, "") for c in CAUSE_CLASSES if c != "unknown"}

        def add(c, pts, ev, why):
            h = H[c]
            if ev is None:
                return
            h.score += pts
            (h.supporting if pts > 0 else h.against).append(f"{ev.id}: {why}")

        ch = by.get("changes")
        notices = ch.data["upstream_notices"] if ch else []
        touching = ch.data["releases_touching"] if ch else []
        run = by.get("run")
        last = (run.data["history"][-1] if run and run.data["history"] else {}) if run else {}
        log = run.data["log_wrapped"] if run else ""
        fv, sch, bat, q, acc = (by.get(k) for k in ("freshness_volume", "schema", "batches", "queries", "access"))

        # upstream outage
        if notices:
            add("upstream outage", 2, ch, f"upstream notice(s): {[n.get('source') + ' ' + n.get('status', '') for n in notices]}")
        if last.get("status") == "waiting" and "waiting for source" in log:
            add("upstream outage", 1, run, "run is waiting for the source")
        if fv and fv.data.get("breakdown") and notices:
            add("upstream outage", 1, fv, "shortfall confined to the region with a degraded feed")
        # source data
        if sch and (sch.data["missing"] or sch.data["added"]):
            if touching:
                add("source data", 1, sch, "schema differs from contract, but a release also touched it")
            else:
                add("source data", 2, sch, "schema differs from the contract with no release on the object")
            missing = sch.data["missing"]
            if run and any(c in log for c in missing):
                add("source data", 1, run, "run error names the missing contract column")
        if bat and bat.data["duplicates"]:
            add("source data", 2, bat, f"identical-content batch(es) {[d['batch_id'] for d in bat.data['duplicates']]}")
            vol = (fv.data.get("volume") if fv else None) or {}
            extra = sum(d["rows"] for d in bat.data["duplicates"])
            if vol and vol.get("rows", 0) - vol.get("expected_rows", 0) == extra:
                add("source data", 1, fv, "volume excess equals the duplicate batch rows")
        # code defect (a release is a lead only when a failing signal ties to the changed object)
        tied = [r for r in touching if r.get("object") in objects]
        if tied and classes & {"quality", "volume", "schema"}:
            add("code defect", 2, ch, f"release {tied[0].get('release')} changed {tied[0]['object']}, "
                                      "which has a failing signal")
        elif touching:
            add("code defect", 0, ch, "a release is recent but no failing signal is tied to it")
        if q and q.data.get("full_scans"):
            add("code defect", 2, q, f"full scan on {q.data.get('top_object')}")
            if q.data.get("daily_cost", 0) > q.data.get("estimate", 0) * 1.5:
                add("code defect", 1, q, "cost variance matches the full scan")
            add("capacity", -1, q, "extra cost is explained by a full scan, not by load growth")
        # configuration
        if run and CONFIG.search(log):
            add("configuration", 2, run, "run error references configuration")
        # capacity
        if last and last.get("status") == "success" and run and len(run.data["history"]) > 1:
            med = run.data["history"][0]["duration_min"]
            if last["duration_min"] > 2 * med:
                add("capacity", 2, run, "duration above twice the trailing median with no error")
        # platform
        if run and last.get("status") == "failed" and TRANSIENT.search(log):
            add("platform", 2, run, "transient platform error in the run log")
            if notices:
                add("platform", -1, ch, "an upstream notice is open")
        # access
        if acc and acc.data["outside"]:
            add("access", 3, acc, f"grant(s) outside the approved model: {acc.data['outside']}")

        statements = {
            "upstream outage": f"An upstream source feeding {failing} is late or degraded",
            "source data": f"The source delivered data that breaks expectations for {failing}",
            "code defect": f"A change in our code or query affects {failing}",
            "configuration": f"A configuration problem stopped the pipeline for {failing}",
            "capacity": f"Insufficient capacity slowed the pipeline for {failing}",
            "platform": f"A transient platform fault failed the run for {failing}",
            "access": "Access drifted from the approved access model",
        }
        out = []
        for c, h in H.items():
            h.statement = statements[c]
            if h.score >= 2:
                h.status = "supported"
            elif h.score < 0 or (h.against and not h.supporting):
                h.status = "refuted"
            elif not h.supporting:
                h.status = "unsupported"
            else:
                h.status = "inconclusive"
            out.append(h)
        return sorted(out, key=lambda h: -h.score)


CONFIRM = {
    "upstream outage": "the source owner confirms the delay, and the object recovers once the source delivers",
    "source data": "the source owner confirms the change or duplicate delivery",
    "code defect": "the release diff shows the change, and the previous version passes on the same input",
    "configuration": "the configuration change log shows the change",
    "capacity": "resource metrics show saturation during the run",
    "platform": "the platform status page or a successful retry with no other change",
    "access": "Security and Compliance confirm the grant was not approved",
    "unknown": "more evidence; escalate to the on-call engineer",
}
