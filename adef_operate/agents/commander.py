"""O0 ``adef-ops-commander``: run the queue. Adds no technical content.

Implements T1 (accept a service), T2 (open and maintain cases, severity by rule, trust
status, paging), T3 (route), T5 (daily health summary) and the stop rule of T6.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..connectors.paging import OutboxPager
from ..core.errors import ExecutorBlocked
from ..engine.approvals import Approvals
from ..engine.case_store import CaseStore
from ..engine.escalation import escalate, escalations
from ..engine.executor import Executor
from ..engine.handover import ReceiverResult, on_product_released
from ..engine.lineage import Lineage
from ..engine.registry import Registry
from ..engine.severity import assess
from ..engine.workspace import Workspace
from ..packs import domain_pack
from ..schemas.records import ActionRecord

DIAGNOSIS = "adef-ops-incident-diagnosis"
ROUTES = {"cost": "adef-ops-finops-performance", "access": "adef-ops-governance-compliance"}
CONTAINMENT_CLASSES = {"quality", "schema"}


class CommanderAgent:
    identity = "commander"

    def __init__(self, ws: Workspace, adapter, pager: Optional[OutboxPager] = None):
        self.ws = ws
        self.adapter = adapter
        self.pager = pager or OutboxPager()
        self.cases = CaseStore(ws)
        self.registry = Registry(ws)
        self.executor = Executor(ws, adapter)

    # -- T1 ---------------------------------------------------------------------------
    @staticmethod
    def accept(event: dict[str, Any], *, home: str | Path | None = None,
               simulated: bool = False, stack_profile: Optional[dict] = None) -> ReceiverResult:
        """Receiver check (pack and Stack Profile); reject once with all gaps; on success request OG0."""
        return on_product_released(event, home=home, simulated=simulated, stack_profile=stack_profile)

    def record_rebind(self, stack_profile: dict, *, by: str) -> None:
        """Record a rebind in service.yaml. Case records, monitoring spec and runbook are untouched."""
        svc = self.svc_config
        old = (svc.get("stack_profile") or {}).get("profile_id")
        svc["stack_profile"] = {"profile_id": stack_profile["profile_id"],
                                "profile_version": stack_profile.get("profile_version", "")}
        self.ws.write_yaml(self.identity, "control/service.yaml", svc)
        self.ws.append(self.identity, "health/rebinds.jsonl",
                       json.dumps({"from": old, "to": stack_profile["profile_id"], "by": by}))

    # -- context ----------------------------------------------------------------------
    @property
    def service(self) -> dict:
        return self.ws.read_yaml("service/service-definition.yaml") or {}

    @property
    def svc_config(self) -> dict:
        return self.ws.read_yaml("control/service.yaml") or {}

    def _monitors(self) -> dict[str, dict]:
        spec = self.ws.read_yaml("service/monitoring-spec.yaml") or {}
        return {m["id"]: m for m in spec.get("monitors", [])}

    def _signals(self, day: str) -> dict[str, dict]:
        path = f"signals/{day}.jsonl"
        if not self.ws.has(path):
            return {}
        return {s["id"]: s for s in (json.loads(l) for l in self.ws.read(path).splitlines() if l.strip())}

    def _log(self, kind: str, day: str, record: dict) -> None:
        self.ws.append(self.identity, f"health/{kind}-{day}.jsonl", json.dumps(record, default=str))

    # -- T2 / T3 ----------------------------------------------------------------------
    def process_groups(self, groups: list[dict], *, day: str) -> list[dict]:
        stop = self.registry.stop_state()
        if stop:
            self._log("routes", day, {"stopped": stop, "groups": [g["id"] for g in groups],
                                      "action": "routed nothing, requested nothing"})
            return [{"group": g["id"], "outcome": "stopped"} for g in groups]
        sigs = self._signals(day)
        return [self._handle(g, sigs, day) for g in groups]

    def _handle(self, g: dict, sigs: dict[str, dict], day: str) -> dict:
        members = [sigs[i] for i in g["signals"] if i in sigs]

        # Group already owned by a case: attach the new signals to it.
        for c in self.cases.open_cases():
            if c.signal_group == g["id"]:
                new = [i for i in g["signals"] if i not in c.signals]
                if new:
                    self.cases.update(c.id, by=self.identity, signals=c.signals + new,
                                      note=f"attached {len(new)} signal(s) from {g['id']} (labels in group)")
                return {"group": g["id"], "outcome": "attached", "case": c.id}

        if g["family"] in ROUTES:
            return self._route(g, day)

        a = assess(g, members, self.service, domain_pack(self.svc_config.get("domain_pack", "")),
                   self._monitors(), (self.ws.read_yaml("control/handover-pack.yaml") or {}).get("object_tags", {}))
        if a.severity == "S4":
            self._log("s4-batch", day, {"group": g["id"], "rule": a.rule_text,
                                        "reason": "batched into the weekly review"})
            return {"group": g["id"], "outcome": "s4-batched", "severity": "S4", "rule": a.rule_text}

        lineage = Lineage.from_service(self.service)
        hood = lineage.neighbourhood(g["objects"])
        same_cause, related = None, []
        for c in self.cases.open_cases("INC"):
            if c.first_failing_object == g["first_failing_object"] or \
                    set(g["monitors"]) & {m.monitor_id for m in c.detecting_monitors}:
                same_cause = c
                break
            if c.first_failing_object and c.first_failing_object in hood:
                related.append(c)
        if same_cause:
            self.cases.update(same_cause.id, by=self.identity,
                              signals=same_cause.signals + [i for i in g["signals"] if i not in same_cause.signals],
                              note=f"attached {g['id']}: same first-failing object "
                                   f"{g['first_failing_object']} (one case per cause)")
            return {"group": g["id"], "outcome": "attached", "case": same_cause.id}

        case = self.cases.open(
            "INC", by=self.identity, severity=a.severity, severity_rule=a.rule_text, owner=DIAGNOSIS,
            signals=g["signals"], signal_group=g["id"], detecting_monitors=g["monitors"],
            note=f"opened from {g['id']}; {a.rule_text}")
        case = self.cases.update(case.id, by=self.identity, note="first-failing object recorded",
                                 first_failing_object=g["first_failing_object"],
                                 severity_would_lower=a.would_lower or None)
        for r in related:
            reason = (f"shared lineage neighbourhood: {r.first_failing_object} and "
                      f"{g['first_failing_object']}; merge only if Diagnosis confirms one cause")
            self.cases.link(case.id, r.id, reason=reason, by=self.identity)
            self.cases.link(r.id, case.id, reason=reason, by=self.identity)
        self.cases.transition(case.id, "triaged", by=self.identity, note=a.rule_text)

        outcome = {"group": g["id"], "outcome": "opened", "case": case.id, "severity": a.severity,
                   "rule": a.rule_text, "linked": [r.id for r in related]}
        shadow = self.registry.mode() == "shadow"
        if shadow:
            would = {"S1": "do-not-use", "S2": "degraded"}.get(a.severity)
            outcome["trust_status"] = None
            outcome["shadow_would"] = [x for x in (f"set trust status {would}" if would else None,
                                                    "page on-call" if a.severity in ("S1", "S2") else None) if x]
            self.cases.update(case.id, by=self.identity, note="shadow mode: would " +
                              (", ".join(outcome["shadow_would"]) or "take no outward action") + "; nothing sent")
        else:
            outcome["trust_status"] = self._set_trust(case.id, a.severity)
        if a.severity in ("S1", "S2") and not shadow:
            on_call = self.svc_config.get("on_call", "UNNAMED")
            self.pager.page(to=on_call, role="on_call", case_id=case.id,
                            subject=f"{case.id} {a.severity} {self.ws.dp_id}: {a.rule_text}")
            self.cases.update(case.id, by=self.identity, note=f"paged on-call {on_call}")
        questions = []
        if a.severity == "S1":
            questions.append("S1: council-of-three review of the diagnosis before options are presented")
        contain = bool(set(g["classes"]) & CONTAINMENT_CLASSES)
        next_action = (f"{DIAGNOSIS}: diagnose; in parallel adef-ops-remediation: contain (runbook "
                       f"containment rule for {', '.join(sorted(set(g['classes']) & CONTAINMENT_CLASSES))})"
                       if contain else f"{DIAGNOSIS}: diagnose")
        self.cases.update(case.id, by=self.identity, note=f"routed: {next_action}",
                          next_action=next_action, open_questions=questions)
        outcome["next_action"] = next_action
        return outcome

    def _already_routed(self, group_id: str, day: str) -> bool:
        path = f"health/routes-{day}.jsonl"
        return self.ws.has(path) and any(json.loads(l).get("group") == group_id
                                         for l in self.ws.read(path).splitlines() if l.strip())

    def _route(self, g: dict, day: str) -> dict:
        owner = ROUTES[g["family"]]
        if self._already_routed(g["id"], day):
            # Repeats of a routed group are duplicates (labelled by the Sentinel); route once.
            return {"group": g["id"], "outcome": "already-routed", "routed_to": owner}
        record = {"group": g["id"], "routed_to": owner, "objects": g["objects"]}
        if g["family"] == "access":
            # Deliberate exception to shadow mode: a suspected security event always reaches
            # Security and Compliance, because the team's own process may not have seen it.
            escalate(self.ws, by=self.identity, to="security_compliance",
                     reason=f"{g['id']}: access outside the approved model on {', '.join(g['objects'])}; "
                            "suspected security event, no agent action")
            sec = self.svc_config.get("approvers", {}).get("security_compliance", "UNNAMED")
            self.pager.page(to=sec, role="security_compliance", subject=f"{g['id']} access drift {self.ws.dp_id}")
            record["escalated_to"] = "security_compliance"
        self._log("routes", day, record)
        return {"group": g["id"], "outcome": "routed", **record}

    def _set_trust(self, case_id: str, severity: str) -> Optional[str]:
        status = {"S1": "do-not-use", "S2": "degraded"}.get(severity)
        if status is None:
            return None
        act = ActionRecord(id=self.executor.next_act_id(), case_id=case_id, product=self.ws.dp_id,
                           action_type="set_trust_status", target=self.ws.dp_id,
                           params={"status": status}, requested_by=self.identity)
        try:
            self.executor.execute(act)
        except ExecutorBlocked as exc:
            case = self.cases.get(case_id)
            self.cases.update(case_id, by=self.identity, note=f"trust status not set: {exc}",
                              open_questions=case.open_questions + [f"set trust status {status} by hand: {exc}"])
            return None
        self.cases.update(case_id, by=self.identity, note=f"trust status set to {status} by rule ({severity})",
                          trust_status=status)
        return status

    # -- T4: approvals on behalf of the owning agent ---------------------------------------
    def request_approval(self, *, gate: str, decision_asked: str, artifact_hash: str, owner_agent: str,
                         case_id: Optional[str] = None, evidence=(), risk: str = "", rollback: str = "",
                         recommendation: str = "", role_key: Optional[str] = None):
        """One request per decision. Refuses while stopped and refuses a duplicate open request."""
        stop = self.registry.stop_state()
        if stop:
            raise ExecutorBlocked(f"stopped ({stop}); the Commander requests nothing")
        appr = Approvals(self.ws)
        if self.registry.mode() == "shadow":
            decision_asked = "[SHADOW - recorded for comparison; not sent to approvers] " + decision_asked
        for r in appr._records("request"):
            if r["artifact_hash"] == artifact_hash and r["gate"] == gate and appr.status(r["id"]) == "pending":
                return appr.get_request(r["id"])
        return appr.request(gate=gate, decision_asked=decision_asked, artifact_hash=artifact_hash,
                            requested_by=self.identity, case_id=case_id, evidence=evidence, risk=risk,
                            rollback=rollback, recommendation=f"{owner_agent}: {recommendation}".strip(": "),
                            role_key=role_key)

    # -- T6: close ------------------------------------------------------------------------
    def close(self, case_id: str) -> None:
        """Close only when verification and the post-incident note exist (case-store guards)."""
        self.cases.transition(case_id, "closed", by=self.identity, note="verification and post-incident note present")

    # -- T5 ---------------------------------------------------------------------------
    def publish_health(self, day: Optional[str] = None, *, now: Optional[datetime] = None) -> str:
        now = now or datetime.now(timezone.utc)
        day = day or now.date().isoformat()
        svc, cases = self.svc_config, self.cases.all()
        open_cases = [c for c in cases if c.state != "closed"]
        appr = Approvals(self.ws)
        pending = [r for r in appr._records("request") if appr.status(r["id"]) == "pending"]
        l3 = [e["record"] for e in self.executor.log.entries()
              if e["record"].get("outcome") == "executed" and e["record"].get("level") == "L3"]
        stale = [c.id for c in open_cases if c.timeline and now - c.timeline[-1].at > timedelta(hours=24)]
        trust = next((c.trust_status for c in sorted(open_cases, key=lambda c: c.severity or "S9")
                      if c.trust_status), "trusted")
        lines = [f"# Health summary - {self.ws.dp_id} - {day}", "",
                 f"- Service: {svc.get('name')} | Trust status: {trust}",
                 f"- Stop state: {self.registry.stop_state() or 'running'}",
                 f"- Signals simulated: {'yes' if svc.get('simulated') else 'no'}",
                 "- Agents: Commander, Onboarding and Sentinel are rule-based (no model served)", "",
                 "## Objectives", *[f"- {o['id']} {o['kind']} on {o['object']}: target {o['target']} {o['unit']}"
                                    for o in self.service.get("objectives", [])], "",
                 "## Open cases by severity"]
        for sev in ("S1", "S2", "S3", "S4", None):
            for c in (c for c in open_cases if c.severity == sev):
                lines.append(f"- {c.id} {c.type} {sev or '-'} [{c.state}] owner {c.owner or 'UNASSIGNED'} - "
                             f"{c.severity_rule or ''} - next: {c.next_action or '-'}")
        if not open_cases:
            lines.append("- none")
        lines += ["", "## Actions taken at L3", *([f"- {r['act']} {r['action_type']} ({r['case']})" for r in l3]
                                                 or ["- none"]),
                  "", "## Waiting on a human", *([f"- {r['id']} {r['gate']}: {r['decision_asked']}" for r in pending]
                                                 or ["- none"]),
                  "", "## Escalations", *([f"- to {e['to']}: {e['reason']}" for e in escalations(self.ws)]
                                          or ["- none"]),
                  "", "## No state change in 24 hours", *([f"- {i}" for i in stale] or ["- none"]), ""]
        text = "\n".join(lines)
        self.ws.write(self.identity, f"health/{day}.md", text)
        return text
