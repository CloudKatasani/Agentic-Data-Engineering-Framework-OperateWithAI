"""O5 ``adef-ops-finops-performance``: keep cost and runtime explainable and within estimate.

Recommend-only. Every finding shows its saving arithmetic and goes to the Commander for
OG4; code-level fixes go to Build as a CHG once accepted. Nothing is resized, rescheduled,
suspended or dropped by this agent.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.hashing import file_hash
from ..engine.case_store import CaseStore
from ..engine.lineage import Lineage
from ..engine.workspace import Workspace
from .commander import CommanderAgent

TOLERANCE_PCT = 20.0
DAYS_PER_MONTH = 30
MIN_MONTHLY_SAVING = 10.0  # below this, a summary line rather than a finding
LEDGER = "finops/savings-ledger.csv"
LEDGER_COLS = ["finding", "decision", "estimated_monthly_low", "estimated_monthly_high", "realised_monthly",
               "measurement_period"]


def money(x: float) -> str:
    return f"{x:,.2f}"


class FinOpsAgent:
    identity = "finops"

    def __init__(self, ws: Workspace, adapter, commander: Optional[CommanderAgent] = None):
        self.ws = ws
        self.adapter = adapter
        self.cases = CaseStore(ws)
        self.commander = commander or CommanderAgent(ws, adapter)
        self.sd = ws.read_yaml("service/service-definition.yaml") or {}
        self.lineage = Lineage.from_service(self.sd)

    # -- ledger -------------------------------------------------------------------------
    def ledger(self) -> list[dict]:
        if not self.ws.has(LEDGER):
            return []
        return list(csv.DictReader(io.StringIO(self.ws.read(LEDGER))))

    def _save_ledger(self, rows: list[dict]) -> None:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=LEDGER_COLS)
        w.writeheader()
        w.writerows(rows)
        self.ws.write(self.identity, LEDGER, buf.getvalue())

    # -- T1 to T4 -----------------------------------------------------------------------
    def review(self, period: Optional[str] = None, *, usage_signals: Optional[dict[str, int]] = None) -> dict:
        """Daily or weekly review. ``usage_signals`` are reads per object from Consume (usage.signal)."""
        cb = self.adapter.cost_breakdown(self.ws.dp_id)
        period = period or cb.get("day") or datetime.now(timezone.utc).date().isoformat()
        total, est = cb["total"], cb["estimate"]
        var = round(total - est, 2)
        pct = round(var / est * 100, 1) if est else 0.0
        material = abs(pct) > TOLERANCE_PCT
        qp = self.adapter.query_profile(self.ws.dp_id)

        driver, evidence = None, []
        if material:
            if qp.get("full_scans"):
                driver = (f"runtime per run: full scan of {qp['object']} ({qp['bytes_scanned_ratio']:g}x the bytes "
                          f"of a pruned read; partitioned by {qp['partitioned_by']} but no partition filter)")
                evidence = ["query profile", "object layout", "billing by pipeline"]
            else:
                driver = "unexplained; next check: run count and volume against the same weekday"

        findings, summary_lines = [], []
        if material and var > 0 and qp.get("full_scans"):
            f = self._full_scan_finding(cb, qp, var)
            (findings if f["monthly_high"] >= MIN_MONTHLY_SAVING else summary_lines).append(f)
        for obj in self.unused_objects(usage_signals):
            findings.append(self._unused_finding(obj))

        raised = [self._raise(f) for f in findings]
        opened = [r for r in raised if not r.get("duplicate_of_open_finding")]
        lines = [f"# FinOps review - {self.ws.dp_id} - {period}", "",
                 "| Item | Estimate | Actual | Variance | Variance % |", "|---|---|---|---|---|",
                 f"| Product total (daily) | {money(est)} | {money(total)} | {money(var)} | {pct:+.1f}% |", "",
                 "## Actual by component", *[f"- {k}: {money(v)}" for k, v in cb["components"].items()],
                 f"- unallocated: {money(cb.get('unallocated', 0.0))}", "",
                 "## Actual compute by pipeline", *[f"- {k}: {money(v)}" for k, v in cb["pipelines_compute"].items()], "",
                 "## Variance drivers",
                 f"- {'material' if material else 'within tolerance'} (tolerance {TOLERANCE_PCT:g}%)"
                 + (f": {driver}; evidence: {', '.join(evidence) or '-'}" if driver else ""), "",
                 "## Findings", *([f"- {o['id']}: {o['title']} (saving {money(o['monthly_low'])} - "
                                   f"{money(o['monthly_high'])} per month)" for o in opened] or ["- none"]),
                 *([""] + ["## Below review threshold"] + [f"- {f['title']}" for f in summary_lines] if summary_lines else []),
                 "", f"## Forecast", f"- Month at today's rate: {money(total * DAYS_PER_MONTH)} against "
                 f"{money(est * DAYS_PER_MONTH)} estimated (assumes today's rate holds)", ""]
        self.ws.write(self.identity, f"finops/{period}.md", "\n".join(lines))
        return {"period": period, "variance": var, "variance_pct": pct, "material": material, "driver": driver,
                "findings": opened}

    def _full_scan_finding(self, cb: dict, qp: dict, var: float) -> dict:
        monthly = var * DAYS_PER_MONTH
        low, high = round(monthly * 0.8, 2), round(monthly, 2)
        arithmetic = [
            f"Daily actual {money(cb['total'])} - daily estimate {money(cb['estimate'])} = {money(var)} above estimate",
            f"{money(var)} x {DAYS_PER_MONTH} days = {money(monthly)} per month",
            f"Removable share with a partition filter: 80% to 100% (bytes scanned {qp['bytes_scanned_ratio']:g}x a "
            f"pruned read) = {money(low)} to {money(high)} per month",
        ]
        return {"kind": "full-scan", "title": f"Full scan of {qp['object']} where pruning is possible",
                "object": qp["object"], "monthly_low": low, "monthly_high": high, "confidence": "medium",
                "arithmetic": arithmetic, "evidence": [
                    f"query profile: full scans {qp['full_scans']}, filter on partition {qp['filter_on_partition']}",
                    f"object layout: partitioned by {qp['partitioned_by']}",
                    f"billing by pipeline: {cb['pipelines_compute']}"],
                "risk": "None to objectives; runtime also falls", "effort": "Small: add a partition filter",
                "reversibility": "Via release", "action_class": "code change (CHG to Build after OG4)",
                "executor": "Build team through the release process", "trade_off": False}

    def unused_objects(self, usage_signals: Optional[dict[str, int]]) -> list[str]:
        """Unused only when telemetry, Consume usage and lineage all agree."""
        if usage_signals is None:
            return []  # without usage.signal from Consume there is no unused finding
        reads = self.adapter.object_reads(self.ws.dp_id)
        return sorted(o for o, n in reads.items()
                      if n == 0 and usage_signals.get(o, 0) == 0 and not self.lineage.descendants(o)
                      and o in usage_signals)

    def _unused_finding(self, obj: str) -> dict:
        return {"kind": "unused", "title": f"Unused object {obj}", "object": obj, "monthly_low": 0.0,
                "monthly_high": 0.0, "confidence": "medium",
                "arithmetic": ["Storage saving to be measured from billing per object (not split by the adapter)"],
                "evidence": ["telemetry: 0 reads in 90 days", "usage: 0 reads from Consume", "lineage: no downstream"],
                "risk": "Unknown readers outside the platform", "effort": "Small",
                "reversibility": "Irreversible after the retention of the backup", "action_class": "A3 drop_object at OG4",
                "executor": "Platform team", "trade_off": False}

    def _open_duplicate(self, f: dict) -> Optional[str]:
        for c in self.cases.all():
            if c.type == "FIN" and c.state not in ("closed",) and c.decision is None \
                    and c.timeline and c.timeline[0].event == f["title"]:
                return c.id
        return None

    def _raise(self, f: dict) -> dict:
        dup = self._open_duplicate(f)
        if dup:  # one finding per cause: an open finding is not raised again
            from ..engine.approvals import Approvals
            req = next((r["id"] for r in Approvals(self.ws)._records("request") if r.get("case_id") == dup), None)
            return {"id": dup, "approval": req, "duplicate_of_open_finding": True, **f}
        related = [c for c in self.cases.open_cases("INC")
                   if c.first_failing_object == f["object"] or f["object"] in
                   self.lineage.neighbourhood([c.first_failing_object or ""])]
        case = self.cases.open("FIN", by=self.identity, note=f["title"])
        for c in related:  # a spike caused by an incident belongs to that case: link, do not duplicate
            self.cases.link(case.id, c.id, reason="cost driver overlaps an open incident", by=self.identity)
        body = [f"# {case.id} - {f['title']}", "",
                f"- Product: {self.ws.dp_id}", f"- Object: {f['object']}",
                f"- {'Trade-off' if f['trade_off'] else 'Recommendation'} for the Platform Owner (OG4)", "",
                "## Evidence", *[f"- {e}" for e in f["evidence"]], "",
                "## Saving arithmetic", *[f"{i}. {a}" for i, a in enumerate(f["arithmetic"], 1)],
                f"", f"Estimated saving: {money(f['monthly_low'])} to {money(f['monthly_high'])} per month "
                f"(confidence {f['confidence']})", "",
                f"- Risk to objectives: {f['risk']}", f"- Effort: {f['effort']}",
                f"- Reversibility: {f['reversibility']}", f"- Action class: {f['action_class']}",
                f"- Executed by: {f['executor']} (never by this agent)",
                *([f"- Linked incidents: {', '.join(c.id for c in related)}"] if related else []), ""]
        path = f"finops/findings/{case.id}.md"
        self.ws.write(self.identity, path, "\n".join(body))
        self.cases.transition(case.id, "awaiting-decision", by=self.identity)
        req = self.commander.request_approval(
            gate="OG4", decision_asked=f"{case.id}: {f['title']}", artifact_hash=file_hash(self.ws.path(path)),
            owner_agent="adef-ops-finops-performance", case_id=case.id, evidence=[path],
            risk=f["risk"], rollback=f["reversibility"], recommendation="accept" if not f["trade_off"] else "trade-off")
        rows = self.ledger() + [{"finding": case.id, "decision": "pending", "estimated_monthly_low": f["monthly_low"],
                                 "estimated_monthly_high": f["monthly_high"], "realised_monthly": "",
                                 "measurement_period": ""}]
        self._save_ledger(rows)
        return {"id": case.id, "approval": req.id, **f}

    # -- decisions and T5 ---------------------------------------------------------------
    def record_decision(self, fin_id: str, approval_id: str) -> str:
        from ..engine.approvals import Approvals
        appr = Approvals(self.ws)
        state = appr.status(approval_id)
        if state == "pending":
            return "pending"
        reason = next((d.reason for d in appr.decisions(approval_id) if d.decision == "rejected"), "")
        decision = "accepted" if state == "approved" else ("deferred" if reason.lower().startswith("defer") else "rejected")
        self.cases.transition(fin_id, "decided", by=self.identity, decision=decision, note=f"{approval_id} {state}")
        rows = self.ledger()
        for r in rows:
            if r["finding"] == fin_id:
                r["decision"] = decision
        self._save_ledger(rows)
        return decision

    def measure_realised(self, fin_id: str, *, baseline_daily: float, period: str) -> float:
        """After one full cycle: realised = (baseline daily - current daily) x 30. Misses are reported."""
        now = self.adapter.cost_breakdown(self.ws.dp_id)["total"]
        realised = round((baseline_daily - now) * DAYS_PER_MONTH, 2)
        rows = self.ledger()
        for r in rows:
            if r["finding"] == fin_id:
                if r["decision"] != "accepted":
                    raise ValueError(f"{fin_id} was {r['decision']}; only accepted findings are measured")
                r["realised_monthly"], r["measurement_period"] = realised, period
        self._save_ledger(rows)
        return realised
