"""The operating loop: agents do the read-only work on their own and bring people in at
every decision. Human-in-the-loop for fixes is the rule.

After each control-plane tick:
  case opened      -> on-call asked to acknowledge; Diagnosis runs at once
  diagnosis ready  -> S1: the council of three is asked to agree; otherwise fix options are built
  options ready    -> the approver is asked to choose a fix (one is recommended)
  fix approved     -> the executor runs it (earned autonomy) or on-call is asked to run it (L1)
  fix executed     -> recovery is verified with the detecting monitors on the next tick
  recovered        -> the service owner is asked to confirm the cause; the post-incident note
                      is drafted; then asked to close
  failed           -> the approver is asked to choose the next fix (two failures: escalation)
  any gate request -> each required role is asked to approve or reject
  any escalation   -> the target role is notified
  no response      -> deadlines escalate to the next role

Nothing is executed without a person choosing it, except a recommended catalogue action that
has earned standing approval (L3); that runs and the owner is told, with how to undo it.
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.errors import AdefError, ApprovalError, GuardViolation
from .approvals import Approvals
from .case_store import CaseStore
from .council import Council
from .escalation import escalations
from .notifications import SEV_PRIORITY, Notifier
from .registry import Registry
from .workspace import Workspace

GATE_ROLE = {"OG2": "service_owner", "OG3": "data_steward", "OG4": "platform_owner", "OG5": "security_compliance",
             "OG6": "service_owner", "OG7": "platform_owner", "OG0": "product_owner", "OG1": "platform_owner"}
CAUSES = ["source data", "upstream outage", "code defect", "configuration", "capacity", "platform", "access", "unknown"]


class OpsLoop:
    def __init__(self, ws: Workspace, adapter):
        self.ws = ws
        self.adapter = adapter
        self.n = Notifier(ws)
        self.cases = CaseStore(ws)
        self.registry = Registry(ws)

    # -- helpers ------------------------------------------------------------------------------
    @property
    def shadow(self) -> bool:
        return self.registry.mode() == "shadow"

    def _agents(self):
        from ..agents import CommanderAgent, DiagnosisAgent, FinOpsAgent, KnowledgeAgent, RemediationAgent
        return CommanderAgent, DiagnosisAgent, FinOpsAgent, KnowledgeAgent, RemediationAgent

    def notify(self, **kw) -> Optional[dict]:
        if self.shadow and kw.get("kind") not in ("escalation",):
            kw = {**kw, "kind": "fyi", "actions": [], "title": "[Shadow] Operate would ask: " + kw["title"]}
        return self.n.notify(**kw)

    def _prio(self, case) -> str:
        return SEV_PRIORITY.get(case.severity or "S3", "P3")

    def _ever(self, request_id: str) -> bool:
        return any(x["request_id"] == request_id and x["kind"] == "decide" for x in self.n.all())

    # -- after a tick -------------------------------------------------------------------------
    def after_tick(self, out: dict[str, Any]) -> dict[str, Any]:
        done: dict[str, list] = {"opened": [], "advanced": [], "notified": []}
        stop = self.registry.stop_state()
        if stop:
            self._sweep_escalations()
            return done
        for o in out.get("outcomes", []):
            if o.get("outcome") == "opened":
                done["opened"].append(o["case"])
                self._on_opened(o["case"])
        results = {r["monitor_id"]: r["status"] for r in out.get("results", [])}
        for c in self.cases.open_cases("INC"):
            if c.id not in done["opened"] and self._advance(c.id, results):
                done["advanced"].append(c.id)
        self._sweep_pending_approvals()
        self._sweep_escalations()
        done["notified"] = [x["id"] for x in self.n.check_deadlines()]
        return done

    def _on_opened(self, case_id: str) -> None:
        c = self.cases.get(case_id)
        self.notify(from_agent="commander", to_role="on_call", kind="acknowledge", priority=self._prio(c),
                    case_id=case_id, actions=["acknowledge"],
                    title=f"{case_id} {c.severity}: {c.first_failing_object or ''} - acknowledge and own it",
                    body=f"{c.severity_rule}. Diagnosis is running now; you will be asked to choose a fix.")
        self._diagnose(case_id)

    def _diagnose(self, case_id: str) -> None:
        _, DiagnosisAgent, *_ = self._agents()
        c = self.cases.get(case_id)
        if not self.ws.has(f"cases/{case_id}/diagnosis.yaml"):
            try:
                DiagnosisAgent(self.ws, self.adapter).diagnose(case_id)
            except AdefError as exc:
                self.notify(from_agent="diagnosis", to_role="on_call", kind="review", priority=self._prio(c),
                            case_id=case_id, actions=["acknowledge"], title=f"{case_id}: diagnosis could not run",
                            body=str(exc))
                return
        d = self.ws.read_yaml(f"cases/{case_id}/diagnosis.yaml") or {}
        if c.severity == "S1" and Council(self.ws).status(case_id) != "agreed":
            for person in self.n.council():
                self.notify(from_agent="diagnosis", to_role="council", to_person=person, kind="review",
                            priority="P1", case_id=case_id, actions=["council-agree", "council-disagree"],
                            title=f"{case_id} S1: review the diagnosis ({d.get('cause_class')}, {d.get('confidence')})",
                            body=f"{d.get('statement', '')}. Fixes are offered once two of three reviewers agree.",
                            dedupe_key=f"council:{case_id}:{person}")
            return
        self._offer_options(case_id)

    def _offer_options(self, case_id: str) -> None:
        *_, RemediationAgent = self._agents()
        rem = RemediationAgent(self.ws, self.adapter)
        c = self.cases.get(case_id)
        try:
            opts = rem.build_options(case_id) if c.state in ("diagnosing", "options-ready") else rem.options(case_id)
        except AdefError as exc:
            self.notify(from_agent="remediation", to_role="on_call", kind="review", priority=self._prio(c),
                        case_id=case_id, actions=["acknowledge"], title=f"{case_id}: fix options not available",
                        body=str(exc))
            return
        rec = next((o for o in opts if o.recommended), opts[0] if opts else None)
        if rec is None:
            return
        if rec.executable_now and rec.action_type and not self.shadow:
            out = rem.act(case_id, rec.no)  # earned standing approval (L3); a person is told at once
            self.notify(from_agent="remediation", to_role="service_owner", kind="fyi", priority=self._prio(c),
                        case_id=case_id, title=f"{case_id}: ran '{rec.title}' under standing approval (L3)",
                        body=f"Reversibility: {rec.reversibility}. Recovery is verified on the next check. Status: {out['status']}.")
            return
        gate = (rec.approval or "").split(" ")[0]
        role = "release_manager" if rec.action_type == "rollback_request" else GATE_ROLE.get(gate, "service_owner")
        summary = "; ".join(f"#{o.no} {o.title} ({o.approval.split(';')[0]})" for o in opts[:4])
        self.notify(from_agent="remediation", to_role=role, kind="decide", priority=self._prio(c), case_id=case_id,
                    actions=["choose-option"], link={"options": [o.no for o in opts], "recommended": rec.no},
                    title=f"{case_id}: choose a fix - recommended #{rec.no} {rec.title}",
                    body=f"Options ranked by safety: {summary}.", dedupe_key=f"choose:{case_id}")

    def _advance(self, case_id: str, results: dict[str, str]) -> bool:
        *_, RemediationAgent = self._agents()
        c = self.cases.get(case_id)
        rem = RemediationAgent(self.ws, self.adapter)
        if c.state == "diagnosing" and c.severity == "S1" and Council(self.ws).status(case_id) == "agreed":
            self._offer_options(case_id)
            return True
        executed = self._executed_acts(case_id)
        detecting = [m.monitor_id for m in c.detecting_monitors]
        if c.state == "executing" and executed:
            return self._verify(case_id, rem)
        if c.state == "options-ready" and detecting and all(results.get(m) == "pass" for m in detecting):
            return self._verify(case_id, rem, note="monitors pass again")
        return False

    def _executed_acts(self, case_id: str) -> list[str]:
        root = self.ws.path(f"cases/{case_id}/actions")
        if not root.exists():
            return []
        return [p.stem for p in root.glob("ACT-*.yaml")
                if (self.ws.read_yaml(f"cases/{case_id}/actions/{p.name}") or {}).get("status") in ("executed", "executed-by-people")]

    def _last_was_containment(self, case_id: str) -> Optional[str]:
        root = self.ws.path(f"cases/{case_id}/actions")
        acts = sorted(root.glob("ACT-*.yaml")) if root.exists() else []
        for p in reversed(acts):
            d = self.ws.read_yaml(f"cases/{case_id}/actions/{p.name}") or {}
            if d.get("status") in ("executed", "executed-by-people"):
                t = d["record"]["action_type"]
                return t if t in ("pause_pipeline", "quarantine_batch") else None
        return None

    def _verify(self, case_id: str, rem, note: str = "") -> bool:
        if self.shadow:
            return False
        contained = self._last_was_containment(case_id)
        if contained:
            # Containment stops bad data spreading; it is not expected to recover the monitors.
            from .monitor_engine import MonitorEngine
            c = self.cases.get(case_id)
            results = MonitorEngine(self.ws, self.adapter).run()["results"]
            status = {r["monitor_id"]: r["status"] for r in results}
            if not all(status.get(m.monitor_id) == "pass" for m in c.detecting_monitors):
                self.cases.transition(case_id, "options-ready", by="remediation",
                                      note=f"contained ({contained}); no attempt counted - choose the fix")
                self.notify(from_agent="remediation", to_role="on_call", kind="fyi", case_id=case_id,
                            title=f"{case_id}: contained with {contained}; bad data stops spreading",
                            body="The cause is not fixed yet. The approver is asked to choose the fix.")
                self.n.resolve_for(case_id=case_id, kinds=["decide"], note="contained")
                self._offer_options(case_id)
                return True
        res = rem.verify(case_id)
        c = self.cases.get(case_id)
        if res["recovered"]:
            self._on_recovered(case_id)
        elif c.escalated:
            self.notify(from_agent="remediation", to_role="platform_owner", kind="escalation", priority=self._prio(c),
                        case_id=case_id, actions=["acknowledge"], title=f"{case_id}: two fixes failed - take over",
                        body="No further automatic attempt is allowed. Decide the next step.")
        else:
            self.cases.transition(case_id, "options-ready", by="remediation", note="fix did not recover the monitors")
            self.n.resolve_for(case_id=case_id, kinds=["decide"], note="new decision needed")
            self._offer_options(case_id)
        return True

    def _on_recovered(self, case_id: str) -> None:
        c = self.cases.get(case_id)
        self.n.resolve_for(case_id=case_id, kinds=["acknowledge", "decide", "run", "review"], note="recovered")
        d = self.ws.read_yaml(f"cases/{case_id}/diagnosis.yaml") or {}
        self.notify(from_agent="knowledge", to_role="service_owner", kind="confirm", priority="P3", case_id=case_id,
                    actions=["confirm-cause"], link={"suggested_cause": d.get("cause_class"), "causes": CAUSES},
                    title=f"{case_id} recovered: confirm the cause (agent says {d.get('cause_class')})",
                    body="Verified by the monitors that detected it. Your confirmed cause labels the evaluation "
                         "replay and the post-incident note.")

    def _sweep_pending_approvals(self) -> None:
        appr = Approvals(self.ws)
        for r in appr._records("request"):
            if appr.status(r["id"]) != "pending" or self._ever(r["id"]):
                continue
            decided = {d.role for d in appr.decisions(r["id"])}
            roles = r["approver_roles"]
            if r["gate"] == "OG2" and "release_manager" not in roles:
                roles = roles[:1]  # any one of the roles may decide; ask the first, escalation reaches the other
            for role in roles:
                if role in decided:
                    continue
                self.notify(from_agent=_owner(r), to_role=role, kind="decide", priority="P3",
                            case_id=r.get("case_id"), request_id=r["id"], actions=["approve", "reject"],
                            title=f"{r['id']} {r['gate']}: {r['decision_asked']}",
                            body=f"Risk: {r.get('risk') or '-'}. Evidence: {', '.join(r.get('evidence') or []) or '-'}.",
                            dedupe_key=f"decide:{r['id']}:{role}")

    def _sweep_escalations(self) -> None:
        seen = {x["dedupe_key"] for x in self.n.all()}
        for i, e in enumerate(escalations(self.ws)):
            key = f"escalog:{i}"
            if key in seen or e.get("by") == "notifier":
                continue
            self.n.notify(from_agent=e.get("by", "commander"), to_role=e.get("to", "platform_owner"), kind="escalation",
                          priority="P1" if e.get("to") == "security_compliance" else "P2", case_id=e.get("case_id"),
                          actions=["acknowledge"], title=f"Escalation: {e['reason'][:140]}", body=e["reason"],
                          dedupe_key=key)

    # -- human actions ------------------------------------------------------------------------
    def act(self, action: str, *, by: str, notification_id: Optional[str] = None, case_id: Optional[str] = None,
            request_id: Optional[str] = None, option: Optional[int] = None, act_id: Optional[str] = None,
            cause_class: Optional[str] = None, role: Optional[str] = None, note: str = "") -> dict:
        if by not in self.n._named():
            raise ApprovalError(f"{by!r} is not a named person for {self.ws.dp_id}; only people take these actions")
        n = self.n.get(notification_id) if notification_id else None
        case_id = case_id or (n or {}).get("case_id")
        request_id = request_id or (n or {}).get("request_id")
        CommanderAgent, _, FinOpsAgent, KnowledgeAgent, RemediationAgent = self._agents()
        result: dict[str, Any] = {"action": action, "by": by}
        gate_only = action in ("approve", "reject") and request_id and \
            not Approvals(self.ws).get_request(request_id).case_id
        if self.shadow and action not in ("acknowledge", "dismiss") and not gate_only:
            raise GuardViolation("shadow mode: record your team's own conclusions instead; Operate takes no action")

        if action == "dismiss":
            if not n:
                raise GuardViolation("dismiss needs a notification")
            self.n.record(n["id"], "dismissed", by=by, note=note)
            return {**result, "status": "dismissed"}
        if action == "acknowledge":
            if case_id:
                c = self.cases.get(case_id)
                self.cases.update(case_id, by=by, note=f"acknowledged by {by}", owner=c.owner)
            result["status"] = "acknowledged"
        elif action in ("council-agree", "council-disagree"):
            Council(self.ws).record(case_id, reviewer=by, agrees=action == "council-agree", note=note)
            status = Council(self.ws).status(case_id)
            result["council"] = status
            if status == "agreed":
                self.n.resolve_for(case_id=case_id, kinds=["review"], note="council agreed")
                self._offer_options(case_id)
            elif status == "disagreed":
                self.n.resolve_for(case_id=case_id, kinds=["review"], note="council disagreed")
                self.notify(from_agent="diagnosis", to_role="on_call", kind="review", priority="P1", case_id=case_id,
                            actions=["acknowledge"], title=f"{case_id}: council disagreed with the diagnosis",
                            body="Take manual control of the diagnosis; no fix is offered automatically.")
        elif action == "choose-option":
            result.update(self._choose(case_id, int(option), by))
        elif action in ("approve", "reject"):
            result.update(self._decide(request_id, by, "approved" if action == "approve" else "rejected", role, note))
        elif action == "run-done":
            rem = RemediationAgent(self.ws, self.adapter)
            rem.confirm_executed_by_people(case_id, act_id or (n or {}).get("link", {}).get("act"), by=by)
            self.n.resolve_for(case_id=case_id, kinds=["run"], note=f"run by {by}")
            self._verify(case_id, rem)
            result["state"] = self.cases.get(case_id).state
        elif action == "confirm-cause":
            k = KnowledgeAgent(self.ws, self.adapter)
            k.confirm_cause(case_id, cause_class=cause_class, by=by, note=note)
            if self.cases.get(case_id).state == "recovered":
                k.post_incident(case_id)
                self.notify(from_agent="knowledge", to_role="service_owner", kind="confirm", priority="P3",
                            case_id=case_id, actions=["close"],
                            title=f"{case_id}: post-incident note drafted - review and close",
                            body="Prevention actions have owners and dates. Closing needs verification and the note.",
                            dedupe_key=f"close:{case_id}")
            result["state"] = self.cases.get(case_id).state
        elif action == "close":
            CommanderAgent(self.ws, self.adapter).close(case_id)
            self.n.resolve_for(case_id=case_id, note=f"closed by {by}")
            result["state"] = "closed"
        else:
            raise GuardViolation(f"unknown action {action}")
        if n and self.n.get(n["id"])["status"] in ("open", "acknowledged"):
            self.n.record(n["id"], "acknowledged" if action == "acknowledge" and n["kind"] != "acknowledge" else "done",
                          by=by, note=note or action)
        return result

    def _choose(self, case_id: str, option: int, by: str) -> dict:
        *_, RemediationAgent = self._agents()
        rem = RemediationAgent(self.ws, self.adapter)
        out = rem.act(case_id, option)
        self.n.resolve_for(case_id=case_id, kinds=["decide"], note=f"option #{option} chosen by {by}")
        if out["status"] == "awaiting-approval":
            req = Approvals(self.ws).get_request(out["approval"])
            mine = [r for r in req.approver_roles if self.n.holder(r) == by]
            if mine:  # the chooser holds the gate: choosing is approving, recorded as a decision
                out.update(self._decide(req.id, by, "approved", mine[0], f"chose option #{option}"))
            else:
                self._sweep_pending_approvals()
        elif out["status"] == "executed":
            self.notify(from_agent="remediation", to_role="on_call", kind="fyi", case_id=case_id,
                        title=f"{case_id}: option #{option} executed; recovery verified on the next check")
        elif out["status"] == "change-requested":
            self.notify(from_agent="remediation", to_role="tech_lead", kind="fyi", case_id=case_id,
                        title=f"{case_id}: change request {out['change']} raised to Build")
        return out

    def _decide(self, request_id: str, by: str, decision: str, role: Optional[str], note: str) -> dict:
        *_, FinOpsAgent, _, RemediationAgent = self._agents()
        appr = Approvals(self.ws)
        req = appr.get_request(request_id)
        role = role or next((r for r in req.approver_roles if self.n.holder(r) == by), None)
        if role is None:
            raise ApprovalError(f"{by} holds none of the roles that decide {request_id}: {req.approver_roles}")
        appr.decide(request_id, approver=by, role=role, decision=decision, artifact_hash=req.artifact_hash, reason=note)
        status = appr.status(request_id)
        out: dict[str, Any] = {"request": request_id, "gate": req.gate, "status": status}
        if status == "pending":
            return out
        self.n.resolve_for(request_id=request_id, kinds=["decide"], note=f"{status} by {by}")
        if req.case_id and req.case_id.startswith("FIN-"):
            out["follow_up"] = {"decision": FinOpsAgent(self.ws, self.adapter).record_decision(req.case_id, request_id)}
            return out
        if not req.case_id:
            return out
        rem = RemediationAgent(self.ws, self.adapter)
        root = self.ws.path(f"cases/{req.case_id}/actions")
        for p in sorted(root.glob("ACT-*.yaml")) if root.exists() else []:
            d = self.ws.read_yaml(f"cases/{req.case_id}/actions/{p.name}")
            if d.get("approval_id") == request_id and d.get("status") == "awaiting-approval":
                f = rem.proceed(req.case_id, p.stem)
                out["follow_up"] = f
                if f["status"] == "approved-awaiting-people":
                    self.notify(from_agent="remediation", to_role="on_call", kind="run", priority="P2",
                                case_id=req.case_id, actions=["run-done"], link={"act": p.stem},
                                title=f"{req.case_id}: run {d['record']['action_type']} ({p.stem}) - approved by {by}",
                                body="This action is executed by people at its current autonomy level. "
                                     "Run it on the platform, then confirm here; recovery is verified at once.")
                elif f["status"] == "rejected":
                    self._offer_options(req.case_id)
        return out


def _owner(r: dict) -> str:
    rec = r.get("recommendation") or ""
    if rec.startswith("adef-ops-"):
        head = rec.split(":")[0].replace("adef-ops-", "").split("-")[0]
        return {"problem": "knowledge", "signal": "sentinel", "incident": "diagnosis",
                "service": "onboarding"}.get(head, head)
    return {"OG0": "commander", "OG1": "onboarding", "OG2": "remediation", "OG3": "remediation"}.get(r["gate"], "commander")
