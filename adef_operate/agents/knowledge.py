"""O7 ``adef-ops-problem-knowledge``: make each incident less likely and cheaper next time.

Writes post-incident notes, problem records, evaluation replays, and OG7 proposals for
runbook and autonomy changes. It changes no live runbook, monitor or autonomy level.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..core.errors import GuardViolation
from ..core.hashing import canonical_hash, file_hash
from ..engine.approvals import Approvals
from ..engine.case_store import CaseStore
from ..engine.catalogue import lookup
from ..engine.escalation import escalations
from ..engine.workspace import Workspace
from .commander import CommanderAgent

PROMOTION_MIN = 5
RECUR_WINDOW = timedelta(days=30)

PREVENTION = {
    "upstream outage": [("platform_owner", "Agree a delivery-time objective and late-file notice with the source owner",
                         "source notices arrive before the freshness monitor fires on the next late delivery")],
    "source data": [("data_steward", "Ask the source owner for contract-change notice and duplicate-free delivery",
                     "source change notices logged; no duplicate-content batch in 30 days")],
    "code defect": [("tech_lead", "Add a pre-release test for the failing quality rule or query pattern",
                     "the test fails on the defective version and passes on the fix")],
    "platform": [("platform_owner", "Review platform reliability for transient run failures",
                  "no transient failure in the next 30 days, or a retry policy approved at OG7")],
    "configuration": [("platform_owner", "Put the configuration under change control",
                       "configuration diff is reviewed before each change")],
    "capacity": [("platform_owner", "Review capacity headroom against volume growth",
                  "duration stays under 2x median for 30 days")],
    "access": [("security_compliance", "Review the grant process that allowed the drift",
                "access review shows no grant outside the model")],
    "unknown": [("tech_lead", "Add the missing evidence source that would have identified the cause",
                 "a replay with the new source yields a supported cause")],
}


class KnowledgeAgent:
    identity = "knowledge"

    def __init__(self, ws: Workspace, adapter=None, commander: Optional[CommanderAgent] = None):
        self.ws = ws
        self.adapter = adapter
        self.cases = CaseStore(ws)
        self.commander = commander or CommanderAgent(ws, adapter)
        self.svc = ws.read_yaml("control/service.yaml") or {}

    def _named(self) -> set[str]:
        return set((self.svc.get("approvers") or {}).values()) | {self.svc.get("on_call")}

    # -- human-confirmed cause --------------------------------------------------------------
    def confirm_cause(self, case_id: str, *, cause_class: str, by: str, note: str = "") -> None:
        if by not in self._named():
            raise GuardViolation(f"{by!r} is not a named person for {self.ws.dp_id}; only people confirm causes")
        self.cases.update(case_id, by=by, note=f"cause confirmed by {by}: {cause_class}" + (f" ({note})" if note else ""),
                          confirmed_cause=cause_class, confirmed_by=by)

    # -- T1 ---------------------------------------------------------------------------------
    def _diag(self, case_id: str) -> dict:
        return self.ws.read_yaml(f"cases/{case_id}/diagnosis.yaml") or {}

    def _signals(self, case) -> list[dict]:
        out = []
        root = self.ws.path("signals")
        for f in sorted(root.glob("*.jsonl")):
            for l in f.read_text().splitlines():
                if l.strip():
                    s = json.loads(l)
                    if s["id"] in case.signals:
                        out.append(s)
        return out

    def _timeline(self, case) -> list[tuple[str, str]]:
        ev = []
        sigs = self._signals(case)
        if sigs:
            first = min(sigs, key=lambda s: s["time"])
            ev.append((first["time"], f"first failing signal {first['id']} ({first['signal_class']} on {first['object']})"))
        for t in case.timeline:
            ev.append((t.at.isoformat(), f"{t.by}: {t.event}"))
        if self.ws.has("control/action-log.jsonl"):
            for e in self.ws.chainlog("executor", "control/action-log.jsonl").entries():
                r = e["record"]
                if r.get("case") == case.id:
                    ev.append((e["at"], f"executor: {r.get('act')} {r.get('action_type')} {r.get('outcome')}"))
        appr = Approvals(self.ws)
        for e in appr.log.entries():
            r = e["record"]
            if r.get("kind") == "request" and r.get("case_id") == case.id:
                ev.append((e["at"], f"approval requested {r['id']} {r['gate']}"))
            if r.get("kind") == "decision":
                req = appr.get_request(r["request_id"])
                if req.case_id == case.id:
                    ev.append((e["at"], f"{r['request_id']} {r['decision']} by {r['approver']} ({r['role']})"))
        for e in escalations(self.ws):
            if e.get("case_id") == case.id:
                ev.append(("", f"escalated to {e['to']}: {e['reason']}"))
        return sorted(ev, key=lambda x: x[0] or "9999")

    def post_incident(self, case_id: str, *, fault_started_at: Optional[datetime] = None,
                      due_in_days: int = 14) -> str:
        case = self.cases.get(case_id)
        if case.state != "recovered":
            raise GuardViolation(f"{case_id} is {case.state}; a post-incident note follows verified recovery")
        d = self._diag(case_id)
        agent_cause = d.get("cause_class", "unknown")
        confirmed = case.confirmed_cause
        match = ("unconfirmed" if not confirmed else "match" if confirmed == agent_cause else
                 "partial" if agent_cause == "unknown" else "miss")
        tl = self._timeline(case)
        sigs = self._signals(case)
        first_sig = min((s["time"] for s in sigs), default=None)
        recovered_at = next((t.at for t in reversed(case.timeline) if t.event.startswith("-> recovered")), None)
        detection = (f"{(datetime.fromisoformat(first_sig) - fault_started_at).total_seconds() / 60:.0f} minutes "
                     f"from fault start to first signal" if fault_started_at and first_sig
                     else "fault start not known from telemetry; detection gap not measurable")
        duration = (f"{(recovered_at - datetime.fromisoformat(first_sig)).total_seconds() / 60:.0f} minutes"
                    if recovered_at and first_sig else "not measurable")
        due = (datetime.now(timezone.utc) + timedelta(days=due_in_days)).date().isoformat()
        cause_for_prevention = confirmed or agent_cause
        actions = []
        for role, what, verify in PREVENTION.get(cause_for_prevention, PREVENTION["unknown"]):
            owner = (self.svc.get("approvers") or {}).get(role)
            actions.append({"action": what, "owner": owner, "owner_role": role, "due": due, "verification": verify})
        if case.failed_attempts:
            actions.append({"action": "Update the runbook step that failed verification", "owner":
                            (self.svc.get("approvers") or {}).get("platform_owner"), "owner_role": "platform_owner",
                            "due": due, "verification": "replay of this incident recovers on the first attempt"})
        bad = [a for a in actions if not (a["owner"] and a["due"] and a["verification"])]
        if bad:
            raise GuardViolation(f"prevention action without owner, date or verification: {bad}")
        consumed = "possibly: trust status was do-not-use" if case.trust_status == "do-not-use" else \
            "no evidence that wrong data was consumed"
        lines = [f"# Post-incident note - {case_id} ({case.severity})", "",
                 "Blameless: conditions, decisions and information available at the time.", "",
                 "## Timeline (UTC)", *[f"- {t or '(time in escalation log)'} - {e}" for t, e in tl], "",
                 "## Impact", f"- {d.get('impact', {}).get('summary', 'see impact.md')}",
                 f"- Duration from first signal to verified recovery: {duration}",
                 f"- Wrong data consumed: {consumed}", "",
                 "## Cause",
                 (f"- Confirmed by {case.confirmed_by}: {confirmed}" if confirmed else
                  f"- Agent diagnosis, UNCONFIRMED: {agent_cause} ({d.get('confidence', '-')})"),
                 f"- Agent diagnosis compared with confirmed cause: {match}", "",
                 "## What worked, what slowed us, luck",
                 f"- Remediation attempts that failed verification: {case.failed_attempts}",
                 "- What worked, what slowed the team and any luck: to be completed by the people involved "
                 "at the review (not inferred by the agent)", "",
                 "## Detection gap", f"- {detection}", "",
                 "## Prevention actions", "| Action | Owner | Due | Verification |", "|---|---|---|---|",
                 *[f"| {a['action']} | {a['owner']} ({a['owner_role']}) | {a['due']} | {a['verification']} |"
                   for a in actions], ""]
        self.ws.write(self.identity, f"cases/{case_id}/post-incident.md", "\n".join(lines))
        self.cases.transition(case_id, "reviewed", by=self.identity, note="post-incident note written")
        self.add_replay(case_id)
        self.check_problems(case_id)
        return "\n".join(lines)

    # -- T4 ---------------------------------------------------------------------------------
    def add_replay(self, case_id: str, *, fault_id: Optional[str] = None) -> dict:
        case = self.cases.get(case_id)
        d = self._diag(case_id)
        sigs = [{k: s[k] for k in ("signal_class", "object", "observed", "expected", "monitor_id", "time")}
                for s in sorted(self._signals(case), key=lambda s: s["time"])]
        label = case.confirmed_cause or d.get("cause_class")
        scenario = {"id": f"RPL-{case_id}", "product": self.ws.dp_id, "case": case_id,
                    "simulated": bool(self.svc.get("simulated")), "fault": fault_id,
                    "signals": sigs, "evidence": [{k: e[k] for k in ("kind", "source", "summary")}
                                                   for e in d.get("evidence", [])],
                    "label": label, "confirmed": bool(case.confirmed_cause),
                    "used_for_scoring": bool(case.confirmed_cause),
                    "agent_cause": d.get("cause_class"), "severity": case.severity}
        self.ws.write(self.identity, f"evals/replays/{case_id}/scenario.json", json.dumps(scenario, indent=1))
        return scenario

    def replays(self) -> list[dict]:
        root = self.ws.path("evals/replays")
        return [json.loads(p.read_text()) for p in sorted(root.glob("*/scenario.json"))] if root.exists() else []

    def accuracy(self) -> dict:
        scored = [r for r in self.replays() if r["used_for_scoring"]]
        correct = sum(1 for r in scored if r["agent_cause"] == r["label"])
        return {"scored": len(scored), "correct": correct,
                "accuracy": round(correct / len(scored), 3) if scored else None,
                "unconfirmed": sum(1 for r in self.replays() if not r["used_for_scoring"])}

    # -- T2 ---------------------------------------------------------------------------------
    def check_problems(self, case_id: str) -> Optional[str]:
        case = self.cases.get(case_id)
        now = case.timeline[-1].at
        recent = [c for c in self.cases.all() if c.type == "INC" and now - c.timeline[0].at <= RECUR_WINDOW]
        cause = lambda c: c.confirmed_cause or (self._diag(c.id) or {}).get("cause_class")
        same_object = [c for c in recent if c.first_failing_object and c.first_failing_object == case.first_failing_object]
        same_cause = [c for c in recent if cause(c) and cause(c) == cause(case)]
        trigger, linked = None, []
        if len(same_object) >= 3:
            trigger, linked = f"{case.first_failing_object} failed {len(same_object)} times in 30 days", same_object
        elif len(same_cause) >= 2:
            trigger, linked = f"cause class '{cause(case)}' recurred {len(same_cause)} times on {self.ws.dp_id}", same_cause
        if not trigger:
            return None
        for p in self.cases.open_cases("PRB"):
            if {l.id for l in p.linked_cases} & {c.id for c in linked}:
                for c in linked:
                    if c.id not in {l.id for l in p.linked_cases}:
                        self.cases.link(p.id, c.id, reason=trigger, by=self.identity)
                return p.id
        prb = self.cases.open("PRB", by=self.identity, note=trigger)
        for c in linked:
            self.cases.link(prb.id, c.id, reason=trigger, by=self.identity)
        owner = (self.svc.get("approvers") or {}).get("tech_lead") or (self.svc.get("approvers") or {}).get("platform_owner")
        due = (now + timedelta(days=30)).date().isoformat()
        retries = 0
        if self.ws.has("control/action-log.jsonl"):
            retries = sum(1 for e in self.ws.chainlog("executor", "control/action-log.jsonl").entries()
                          if e["record"].get("case") in {c.id for c in linked} and e["record"].get("outcome") == "executed")
        self.ws.write(self.identity, f"problems/{prb.id}.md", "\n".join([
            f"# {prb.id} - {trigger}", "", "## Linked incidents", *[f"- {c.id} ({c.severity}, {cause(c)})" for c in linked],
            "", "## Common factor", f"- {trigger} (evidence: case records and diagnoses)", "",
            "## Cost so far", f"- Incidents: {len(linked)}", f"- Executed remediation actions: {retries}", "",
            "## Prevention", f"- Owner: {owner}", f"- Date: {due}",
            "- Proposal: remove the cause (CHG to Build or request to the source owner) rather than add an alert",
            "", "## Status", "- open", ""]))
        self.cases.transition(prb.id, "investigating", by=self.identity)
        return prb.id

    # -- T3 / T5: OG7 proposals --------------------------------------------------------------
    def propose_runbook_change(self, *, step: str, reason_case: str) -> dict:
        n = len(list(self.ws.path("knowledge/runbook-changes").glob("RBC-*.md"))) + 1 \
            if self.ws.path("knowledge/runbook-changes").exists() else 1
        rid = f"RBC-{n:03d}"
        current = file_hash(self.ws.path("service/runbook.md"))
        path = f"knowledge/runbook-changes/{rid}.md"
        self.ws.write(self.identity, path, f"# {rid}\n\n- Proposed step: {step}\n- Justifying incident: {reason_case}\n"
                                           f"- Replaces runbook with hash: `{current}`\n- Status: proposal awaiting OG7\n")
        req = self.commander.request_approval(gate="OG7", decision_asked=f"{rid}: runbook change", owner_agent=
                                              "adef-ops-problem-knowledge", artifact_hash=file_hash(self.ws.path(path)),
                                              evidence=[path], case_id=reason_case)
        return {"id": rid, "approval": req.id}

    def track_record(self) -> dict[str, dict]:
        """From the platform's action log (never from the agent's own counts)."""
        out: dict[str, dict] = {}
        if not self.ws.has("control/action-log.jsonl"):
            return out
        for e in self.ws.chainlog("executor", "control/action-log.jsonl").entries():
            r = e["record"]
            t = r.get("action_type")
            s = out.setdefault(t, {"l2_approved_executions": 0, "executions": 0, "verified": 0,
                                   "failed_verification": 0, "blocked": 0, "approvals": []})
            if r.get("outcome") == "executed":
                s["executions"] += 1
                if r.get("level") == "L2" and r.get("approval"):
                    s["l2_approved_executions"] += 1
                    s["approvals"].append(r["approval"])
            elif r.get("outcome") == "verified":
                s["verified"] += 1
            elif r.get("outcome") == "verification-failed":
                s["failed_verification"] += 1
            elif r.get("outcome") == "blocked":
                s["blocked"] += 1
        return out

    def propose_autonomy(self) -> list[dict]:
        from ..engine.registry import Registry
        reg = Registry(self.ws)
        proposals = []
        for t, s in self.track_record().items():
            entry = lookup(t)
            current = reg.stored_level(t)
            target, why = None, ""
            if s["failed_verification"] or s["blocked"]:
                if current.value > 1:
                    target, why = "L1", f"{s['failed_verification']} failed verification(s), {s['blocked']} block(s)"
            elif s["l2_approved_executions"] >= PROMOTION_MIN and s["verified"] >= s["executions"] \
                    and current.value == 2 and entry.highest.value >= 3:
                target, why = "L3", f"{s['l2_approved_executions']} approved L2 executions, all verified, no block"
            if not target:
                continue
            n = len(list(self.ws.path("control/autonomy-proposals").glob("AUT-*.md"))) + 1
            aid = f"AUT-{n:03d}"
            path = f"control/autonomy-proposals/{aid}.md"
            self.ws.write(self.identity, path, "\n".join([
                f"# {aid} - {t}: {current.name} -> {target}", "", f"- Product: {self.ws.dp_id}",
                f"- Track record (platform action log): {json.dumps({k: v for k, v in s.items() if k != 'approvals'})}",
                f"- Approval IDs: {', '.join(s['approvals']) or '-'}", f"- Reason: {why}",
                f"- Risk: {'more autonomy for a reversible action' if target == 'L3' else 'less autonomy; slower recovery'}",
                "- The Platform Owner and Tech Lead decide (OG7).", ""]))
            req = self.commander.request_approval(
                gate="OG7", decision_asked=f"{aid}: set {t} to {target}", owner_agent="adef-ops-problem-knowledge",
                artifact_hash=canonical_hash({"action_type": t, "level": target}), evidence=[path])
            proposals.append({"id": aid, "action_type": t, "from": current.name, "to": target, "approval": req.id})
        return proposals

    # -- T6 ---------------------------------------------------------------------------------
    def monthly_review(self, month: str) -> str:
        incs = [c for c in self.cases.all() if c.type == "INC"]
        sev = Counter(c.severity for c in incs)
        causes = Counter(c.confirmed_cause or (self._diag(c.id) or {}).get("cause_class", "undiagnosed") for c in incs)
        prbs = self.cases.open_cases("PRB")
        acc = self.accuracy()
        from ..engine.registry import Registry
        levels = Counter(Registry(self.ws).load()["levels"].values())
        text = "\n".join([f"# Operations review - {self.ws.dp_id} - {month}", "",
                          "## Incidents by severity", *([f"- {k}: {v}" for k, v in sorted(sev.items())] or ["- none"]), "",
                          "## Incidents by cause class", *([f"- {k}: {v}" for k, v in sorted(causes.items())] or ["- none"]), "",
                          "## Open problems", *([f"- {p.id}: {p.timeline[0].event}" for p in prbs] or ["- none"]), "",
                          "## Autonomy mix", *[f"- {k}: {v} action type(s)" for k, v in sorted(levels.items())], "",
                          "## Diagnosis accuracy (confirmed replays only)",
                          f"- {acc['correct']} of {acc['scored']} correct; {acc['unconfirmed']} unconfirmed not scored", ""])
        self.ws.write(self.identity, f"knowledge/review-{month}.md", text)
        return text
