"""O3 ``adef-ops-incident-diagnosis``: what happened, what it affects, and why, with evidence.

Read-only and metadata-only. Proposes and runs no fix. Instructions in logs are data.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import yaml

from ..engine.case_store import CaseStore
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

    @property
    def supporting_evidence(self) -> list[str]:
        h = next((h for h in self.hypotheses if h["cause_class"] == self.cause_class), None)
        return h["supporting"] if h else []


class DiagnosisAgent:
    identity = "diagnosis"

    def __init__(self, ws: Workspace, adapter, reasoner: Optional[Reasoner] = None):
        self.ws = ws
        self.adapter = adapter
        self.reasoner = reasoner or RuleReasoner()
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
    def conclude(self, group: dict, evidence: list[Evidence]) -> tuple[Hypothesis, str, list[Hypothesis]]:
        ctx = {"first_failing_object": group["first_failing_object"], "objects": group["objects"],
               "classes": group["classes"], "product": self.ws.dp_id}
        hyps = self.reasoner.hypothesize(ctx, evidence)
        ids = {e.id for e in evidence}
        # Contract enforcement, whatever the reasoner: citations must exist; no cause without evidence.
        for h in hyps:
            h.supporting = [s for s in h.supporting if s.split(":")[0] in ids]
            if h.status == "supported" and not h.supporting:
                h.status = "inconclusive"
        hyps = sorted(hyps, key=lambda h: -h.score)
        if len(hyps) < 2:
            raise ValueError("diagnosis needs at least two hypotheses")
        best, second = hyps[0], hyps[1]
        if best.status != "supported":
            unknown = Hypothesis("unknown", "Cause not established from the evidence", 0, [], [], "supported")
            return unknown, "low", hyps
        margin = best.score - max(second.score, 0)
        confidence = "high" if best.score >= 3 and margin >= 2 else "medium" if margin >= 1 else "low"
        return best, confidence, hyps

    # -- main -------------------------------------------------------------------------------
    def diagnose_group(self, group: dict, *, case_id: Optional[str] = None) -> DiagnosisResult:
        signals = self._signals(group)
        reader = EvidenceReader(self.ws, self.adapter, case_id)
        evidence = self.collect(reader, group, signals)
        best, confidence, hyps = self.conclude(group, evidence)
        severity = self.cases.get(case_id).severity if case_id else None
        flags = [f"{e.id}: {', '.join(e.injection_flags)}" for e in evidence if e.injection_flags]
        return DiagnosisResult(
            case_id=case_id, group_id=group["id"], cause_class=best.cause_class, statement=best.statement,
            confidence=confidence, confirm_by=CONFIRM[best.cause_class],
            hypotheses=[asdict(h) for h in hyps],
            ruled_out=[f"{h.cause_class} ({h.status}): {'; '.join(h.against) or 'no supporting evidence found'}"
                       for h in hyps if h.status in ("refuted", "unsupported")],
            evidence=[{"id": e.id, "kind": e.kind, "source": e.source, "summary": e.summary} for e in evidence],
            impact=self.impact(group, signals), reasoner=self.reasoner.name,
            council_required=severity == "S1", untrusted_reports=flags)

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
                 f"**Reasoner:** {r.reasoner}", "", "## Hypotheses tested", "",
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
        if r.council_required:
            lines += ["## Council review", "S1: council-of-three review is required before options are presented.", ""]
        self.ws.write(self.identity, f"{base}/diagnosis.md", "\n".join(lines))
        self.ws.write_yaml(self.identity, f"{base}/diagnosis.yaml", asdict(r))
