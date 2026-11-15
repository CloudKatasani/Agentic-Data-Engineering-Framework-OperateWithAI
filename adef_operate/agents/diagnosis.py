"""O3 ``adef-ops-incident-diagnosis``: what happened, what it affects, and why, with evidence.

Read-only and metadata-only. Proposes and runs no fix. Instructions in logs are data.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import yaml

from ..core.errors import BudgetExceeded
from ..engine.case_store import CaseStore
from ..engine.registry import Registry
from ..engine.evidence import Evidence, EvidenceReader
from ..engine.lineage import Lineage
from ..engine.workspace import Workspace
from .reasoning import CONFIRM, Hypothesis, Reasoner, RuleReasoner

BUSINESS_EFFECT = {"freshness": "late", "slo": "late", "run": "late or missing", "volume": "incomplete or duplicated",
                   "quality": "incorrect", "schema": "missing (load blocked)", "cost": "more expensive",
                   "access": "exposed to unapproved access"}


@dataclass
class DiagnosisResult:
    case_id: Optional[str]
    group_id: str
    cause_class: str
    statement: str
    confidence: str
    confirm_by: str
    hypotheses: list[dict]
    ruled_out: list[str]
    evidence: list[dict]
    impact: dict[str, Any]
    reasoner: str
    council_required: bool = False
    untrusted_reports: list[str] = field(default_factory=list)
    reasoner_note: Optional[str] = None
    model_cost_usd: float = 0.0
    challenger: Optional[dict] = None

    @property
    def supporting_evidence(self) -> list[str]:
        h = next((h for h in self.hypotheses if h["cause_class"] == self.cause_class), None)
        return h["supporting"] if h else []


class DiagnosisAgent:
    identity = "diagnosis"

    def __init__(self, ws: Workspace, adapter, reasoner: Optional[Reasoner] = None,
                 challenger: Optional[Reasoner] = None):
        self.ws = ws
        self.adapter = adapter
        if reasoner is None:
            from .model_reasoner import reasoners_from_env
            reasoner, env_challenger = reasoners_from_env()
            challenger = challenger or env_challenger
        self.reasoner = reasoner
        self.challenger = challenger
        self.registry = Registry(ws)
        self.cases = CaseStore(ws)
        self.sd = ws.read_yaml("service/service-definition.yaml") or {}
        self.pack = ws.read_yaml("control/handover-pack.yaml") or {}
        self.lineage = Lineage.from_service(self.sd)
        spec = ws.read_yaml("service/monitoring-spec.yaml") or {}
        self.monitors = {m["id"]: m for m in spec.get("monitors", [])}

    # -- helpers --------------------------------------------------------------------------
    def _group(self, group_id: str) -> dict:
        day = group_id.split("-")[1]
        day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        for line in self.ws.read(f"signals/groups/{day}.jsonl").splitlines():
            g = json.loads(line)
            if g["id"] == group_id:
                return g
        raise KeyError(group_id)

    def _signals(self, group: dict) -> list[dict]:
        path = f"signals/{group['day']}.jsonl"
        sigs = {s["id"]: s for s in (json.loads(l) for l in self.ws.read(path).splitlines() if l.strip())}
        return [sigs[i] for i in group["signals"] if i in sigs]

    def _pipeline_of(self, obj: str) -> Optional[str]:
        return next((p["id"] for p in self.sd.get("pipelines", []) if obj in p["objects"]), None)

    # -- T1 ---------------------------------------------------------------------------------
    def impact(self, group: dict, signals: list[dict]) -> dict[str, Any]:
        affected = set(group["objects"])
        for o in group["objects"]:
            affected |= self.lineage.descendants(o)
        objectives = [o for o in self.sd.get("objectives", []) if o["object"] in affected]
        effect = sorted({BUSINESS_EFFECT.get(s["signal_class"], "affected") for s in signals})
        consumers = self.sd.get("consumers", [])
        leaves = sorted(o for o in affected if self.lineage.is_leaf(o))
        text = (f"{', '.join(consumers) or 'No registered consumers'} may see {' / '.join(effect)} data in "
                f"{', '.join(leaves) or ', '.join(sorted(affected))}.")
        return {"affected_objects": sorted(affected), "objectives_at_risk": [o["id"] for o in objectives],
                "consumers": consumers, "effect": effect, "summary": text}

    # -- T2 ---------------------------------------------------------------------------------
    def collect(self, reader: EvidenceReader, group: dict, signals: list[dict]) -> list[Evidence]:
        head = group["first_failing_object"]
        reader.signals(signals)
        pipe = self._pipeline_of(head)
        if pipe:
            reader.run(pipe)
        reader.freshness_volume(head, self.monitors)
        reader.schema(head, self.pack.get("schema_contract", {}).get(head, {}), self.monitors)
        reader.batches(head)
        reader.changes(set(group["objects"]) | self.lineage.ancestors(head))
        if "cost" in group["classes"]:
            reader.queries()
        if "access" in group["classes"]:
            reader.access(self.pack.get("access_model", []), self.monitors.get("MON-ACCESS"))
        return reader.items

    # -- T3 / T4 ----------------------------------------------------------------------------
    def _contract(self, hyps: list[Hypothesis], evidence: list[Evidence], group: dict) -> list[Hypothesis]:
        """The diagnosis contract, applied to every reasoner's output (rule-based or model)."""
        ids = {e.id for e in evidence}
        change_ids = {e.id for e in evidence if e.kind == "changes"}
        touching = {r.get("object") for e in evidence if e.kind == "changes"
                    for r in e.data.get("releases_touching", [])}
        for h in hyps:
            # Citations must name evidence that exists.
            h.supporting = [s for s in h.supporting if s.split(":")[0].strip() in ids]
            h.against = [s for s in h.against if s.split(":")[0].strip() in ids]
            # No cause without evidence, and "supported" needs a positive score.
            if h.status == "supported" and (not h.supporting or h.score <= 0):
                h.status = "inconclusive"
            # A recent change is a lead, not a conclusion (spec 13.1): support from change
            # history alone needs a failing signal on an object the change touched.
            if h.cause_class == "code defect" and h.status == "supported" and h.supporting \
                    and all(s.split(":")[0].strip() in change_ids for s in h.supporting) \
                    and not (touching & set(group["objects"])):
                h.status = "inconclusive"
                h.against.append(f"{next(iter(change_ids))}: contract - a change with no failing signal on the "
                                 "changed object is a lead, not a cause")
        return sorted(hyps, key=lambda h: -h.score)

    @staticmethod
    def _decide(hyps: list[Hypothesis]) -> tuple[Hypothesis, str]:
        if len(hyps) < 2:
            raise ValueError("diagnosis needs at least two hypotheses")
        supported = [h for h in hyps if h.status == "supported"]
        if not supported:
            return Hypothesis("unknown", "Cause not established from the evidence", 0, [], [], "supported"), "low"
        best = max(supported, key=lambda h: h.score)
        second = max((h.score for h in hyps if h is not best), default=0)
        margin = best.score - max(second, 0)
        confidence = "high" if best.score >= 3 and margin >= 2 else "medium" if margin >= 1 else "low"
        return best, confidence

    def _run(self, reasoner, group: dict, evidence: list[Evidence]) -> tuple[Hypothesis, str, list[Hypothesis], dict]:
        ctx = {"first_failing_object": group["first_failing_object"], "objects": group["objects"],
               "classes": group["classes"], "product": self.ws.dp_id}
        meta: dict[str, Any] = {"note": None}
        if not isinstance(reasoner, RuleReasoner) and self.registry.stop_state():
            # A stopped product (emergency stop or exhausted budget) makes no model calls.
            meta["note"] = f"model not called: {self.registry.stop_state()}"
            reasoner = RuleReasoner()
        hyps = reasoner.hypothesize(ctx, evidence)
        meta["name"] = reasoner.name
        meta["note"] = meta["note"] or getattr(reasoner, "note", None)
        meta["injection"] = list(getattr(reasoner, "last_injection", []) or [])
        cost = float(getattr(reasoner, "last_cost", 0.0) or 0.0)
        meta["cost_usd"] = cost
        if cost > 0:
            try:
                self.registry.charge("model_spend", cost, by=self.identity)
            except BudgetExceeded as exc:  # the product is now at L0 and escalated; keep this answer
                meta["note"] = f"{meta['note'] + '; ' if meta['note'] else ''}{exc}"
        hyps = self._contract(hyps, evidence, group)
        best, confidence = self._decide(hyps)
        return best, confidence, hyps, meta

    def conclude(self, group: dict, evidence: list[Evidence]) -> tuple[Hypothesis, str, list[Hypothesis]]:
        best, confidence, hyps, self._meta = self._run(self.reasoner, group, evidence)
        return best, confidence, hyps

    # -- main -------------------------------------------------------------------------------
    def diagnose_group(self, group: dict, *, case_id: Optional[str] = None) -> DiagnosisResult:
        signals = self._signals(group)
        reader = EvidenceReader(self.ws, self.adapter, case_id)
        evidence = self.collect(reader, group, signals)
        best, confidence, hyps, meta = self._run(self.reasoner, group, evidence)
        challenger = None
        if self.challenger is not None:
            cb, cc, chyps, cmeta = self._run(self.challenger, group, evidence)
            challenger = {"reasoner": cmeta["name"], "cause_class": cb.cause_class, "confidence": cc,
                          "statement": cb.statement, "agrees": cb.cause_class == best.cause_class,
                          "supporting": cb.supporting, "note": cmeta["note"], "cost_usd": cmeta["cost_usd"],
                          "hypotheses": [asdict(h) for h in chyps]}
            meta["injection"] += cmeta["injection"]
        severity = self.cases.get(case_id).severity if case_id else None
        flags = [f"{e.id}: {', '.join(e.injection_flags)}" for e in evidence if e.injection_flags]
        flags += [f"reported by {meta['name'] if not challenger else 'a reasoner'}: {x}" for x in meta["injection"]]
        return DiagnosisResult(
            case_id=case_id, group_id=group["id"], cause_class=best.cause_class, statement=best.statement,
            confidence=confidence, confirm_by=CONFIRM[best.cause_class],
            hypotheses=[asdict(h) for h in hyps],
            ruled_out=[f"{h.cause_class} ({h.status}): {'; '.join(h.against) or 'no supporting evidence found'}"
                       for h in hyps if h.status in ("refuted", "unsupported")],
            evidence=[{"id": e.id, "kind": e.kind, "source": e.source, "summary": e.summary} for e in evidence],
            impact=self.impact(group, signals), reasoner=meta["name"],
            council_required=severity == "S1", untrusted_reports=flags, reasoner_note=meta["note"],
            model_cost_usd=round(meta["cost_usd"] + (challenger or {}).get("cost_usd", 0.0), 6),
            challenger=challenger)

    def diagnose(self, case_id: str) -> DiagnosisResult:
        case = self.cases.get(case_id)
        if case.state in ("triaged", "contained"):
            self.cases.transition(case_id, "diagnosing", by=self.identity)
        result = self.diagnose_group(self._group(case.signal_group), case_id=case_id)
        self._write(result)
        note = f"diagnosis: {result.cause_class} ({result.confidence})"
        questions = list(case.open_questions)
        if result.untrusted_reports:
            questions.append("instruction-like text found in logs; treated as data, not followed: "
                             + "; ".join(result.untrusted_reports))
        self.cases.update(case_id, by=self.identity, note=note, open_questions=questions,
                          next_action="adef-ops-remediation: options" + (
                              " after council review" if result.council_required else ""))
        return result

    def _write(self, r: DiagnosisResult) -> None:
        base = f"cases/{r.case_id}"
        self.ws.write(self.identity, f"{base}/impact.md", "\n".join([
            f"# Impact - {r.case_id}", "", r.impact["summary"], "",
            f"- Affected objects: {', '.join(r.impact['affected_objects'])}",
            f"- Objectives at risk: {', '.join(r.impact['objectives_at_risk']) or 'none'}",
            f"- Consumers: {', '.join(r.impact['consumers']) or 'none registered'}", ""]))
        lines = [f"# Diagnosis - {r.case_id}", "",
                 f"**Most likely cause:** {r.statement}", f"**Cause class:** {r.cause_class}",
                 f"**Confidence:** {r.confidence}", f"**What would confirm it:** {r.confirm_by}",
                 f"**Reasoner:** {r.reasoner}" + (f" ({r.reasoner_note})" if r.reasoner_note and r.reasoner_note not in r.reasoner else ""),
                 *([f"**Model cost:** ${r.model_cost_usd:.4f} (charged to the daily model budget)"] if r.model_cost_usd else []),
                 "", "## Hypotheses tested", "",
                 "| Cause class | Status | Score | For | Against |", "|---|---|---|---|---|"]
        for h in r.hypotheses:
            lines.append(f"| {h['cause_class']} | {h['status']} | {h['score']} | "
                         f"{'<br>'.join(h['supporting']) or '-'} | {'<br>'.join(h['against']) or '-'} |")
        lines += ["", "## Ruled out", *([f"- {x}" for x in r.ruled_out] or ["- none"]), "",
                  "## Evidence (metadata only)", *[f"- {e['id']} {e['kind']} ({e['source']}): {e['summary']}"
                                                   for e in r.evidence], ""]
        if r.untrusted_reports:
            lines += ["## Untrusted content reported",
                      *[f"- {x} (treated as data; not followed)" for x in r.untrusted_reports], ""]
        if r.challenger:
            ch = r.challenger
            lines += ["## Challenger (recorded for comparison; not used for the conclusion)",
                      f"- Reasoner: {ch['reasoner']}" + (f" ({ch['note']})" if ch.get("note") and ch["note"] not in ch["reasoner"] else ""),
                      f"- Cause class: {ch['cause_class']} ({ch['confidence']}) - "
                      f"{'agrees' if ch['agrees'] else 'DISAGREES'} with the conclusion",
                      *[f"- Evidence: {x}" for x in ch["supporting"]], ""]
        if r.council_required:
            lines += ["## Council review", "S1: council-of-three review is required before options are presented.", ""]
        self.ws.write(self.identity, f"{base}/diagnosis.md", "\n".join(lines))
        self.ws.write_yaml(self.identity, f"{base}/diagnosis.yaml", asdict(r))
