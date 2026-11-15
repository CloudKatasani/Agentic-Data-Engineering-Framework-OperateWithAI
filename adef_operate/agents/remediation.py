"""O4 ``adef-ops-remediation``: safest path to recovery; execute only what is granted; prove it.

Decision rules enforced here (on top of the executor's own checks):
* contain first when a containment rule applies;
* never re-run a non-idempotent pipeline without a human decision (people execute it);
* never scale up to make a failure go away (resize is a trade-off at OG4, never a fix);
* data correction needs both Data Steward and Data Owner (OG3);
* two failed attempts means stop (the case store enforces it);
* recovered only with verification by the monitors that detected the problem.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import yaml

from ..core.autonomy import ActionClass, Level
from ..core.errors import ExecutorBlocked, GuardViolation
from ..engine.approvals import Approvals
from ..engine.case_store import CaseStore
from ..engine.catalogue import lookup
from ..engine.council import Council
from ..engine.executor import Executor, act_hash
from ..engine.monitor_engine import MonitorEngine
from ..engine.registry import Registry
from ..engine.workspace import Workspace
from ..schemas.records import ActionRecord

SAFETY_ORDER = ["contain", "wait", "catalogue", "data-correction", "configuration", "code-fix", "rollback",
                "trade-off"]
SCALE_UP = {"resize"}


@dataclass
class Option:
    no: int
    kind: str
    title: str
    effect: str
    risk: str
    cost: str
    time: str
    reversibility: str
    action_type: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)
    pipeline: Optional[str] = None
    target: Optional[str] = None
    approval: str = "none"
    executable_now: bool = False
    executed_by_people: bool = False
    recommended: bool = False
    note: str = ""


class RemediationAgent:
    identity = "remediation"

    def __init__(self, ws: Workspace, adapter):
        self.ws = ws
        self.adapter = adapter
        self.cases = CaseStore(ws)
        self.registry = Registry(ws)
        self.approvals = Approvals(ws)
        self.executor = Executor(ws, adapter)
        self.sd = ws.read_yaml("service/service-definition.yaml") or {}

    # -- context ----------------------------------------------------------------------------
    def _diag(self, case_id: str) -> dict:
        d = self.ws.read_yaml(f"cases/{case_id}/diagnosis.yaml")
        if d is None:
            raise GuardViolation(f"{case_id}: no diagnosis yet; options need a diagnosis")
        return d

    def _pipeline(self, obj: str) -> Optional[dict]:
        return next((p for p in self.sd.get("pipelines", []) if obj in p["objects"]), None)

    def _group(self, case) -> dict:
        day = case.signal_group.split("-")[1]
        day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        return next(g for g in (json.loads(l) for l in self.ws.read(f"signals/groups/{day}.jsonl").splitlines())
                    if g["id"] == case.signal_group)

    def _approval_text(self, action_type: str) -> tuple[str, bool, bool]:
        """(approval needed, executable by the executor now, executed by people)."""
        e = lookup(action_type)
        level, why = self.registry.effective_level(action_type)
        if e.executed_by_people:
            return f"{e.gate} ({'/'.join(self._roles(e.gate))}); executed by people", False, True
        if e.action_class == ActionClass.A2 and level == Level.L3:
            return "none (L3 within OG1 standing approval)", True, False
        gate = e.gate or "OG2"
        people = level <= Level.L1
        roles = self._roles("OG2-rollback" if action_type == "rollback_request" else gate)
        return (f"{gate} ({'/'.join(roles)})" + ("; executed by people at L1" if people else ""),
                False, people)

    @staticmethod
    def _roles(gate: str) -> list[str]:
        from ..engine.approvals import GATE_ROLES
        return GATE_ROLES[gate][1]

    def _catalogue(self, kind, title, action_type, effect, risk, cost, time, rev, *, pipeline=None,
                   target=None, params=None, note="") -> Option:
        approval, now, people = self._approval_text(action_type)
        return Option(0, kind, title, effect, risk, cost, time, rev, action_type, params or {}, pipeline,
                      target, approval, now, people, False, note)

    # -- T1 / T2: options ---------------------------------------------------------------------
    def build_options(self, case_id: str) -> list[Option]:
        case = self.cases.get(case_id)
        d = self._diag(case_id)
        if case.severity == "S1" and Council(self.ws).status(case_id) != "agreed":
            raise GuardViolation(f"{case_id}: S1 options wait for a council-of-three majority "
                                 f"(status {Council(self.ws).status(case_id)})")
        g = self._group(case)
        head, cause, classes = g["first_failing_object"], d["cause_class"], set(g["classes"])
        pipe = self._pipeline(head) or {}
        pid, idem = pipe.get("id"), pipe.get("idempotent") is True
        downstream = sorted(set(d["impact"]["affected_objects"]) - {head})
        window = self.adapter.latest_window(self.ws.dp_id, pid) if pid else None
        opts: list[Option] = []

        # Containment first: stop bad data spreading before restoring freshness.
        if classes & {"quality", "schema"}:
            opts.append(self._catalogue("contain", f"Pause {pid}", "pause_pipeline",
                        f"Stops {head} feeding {', '.join(downstream) or 'downstream'}", "Freshness worsens while paused",
                        "None", "Minutes", "Reversible (resume)", pipeline=pid, target=head))
        dup = next((e for e in self._evidence(case_id, "batches") if e["data"]["duplicates"]), None)
        if dup:
            b = dup["data"]["duplicates"][0]
            opts.append(self._catalogue("contain", f"Quarantine batch {b['batch_id']}", "quarantine_batch",
                        f"Removes {b['rows']} duplicated rows from {head} and downstream", "Low; batch is kept",
                        "None", "Minutes", "Reversible (release batch)", pipeline=pid, target=head,
                        params={"batch_id": b["batch_id"]}, note=f"evidence {dup['id']}"))

        if cause in ("upstream outage", "unknown"):
            opts.append(Option(0, "wait", "Wait for the source and watch the detecting monitors",
                               "Recovers if the source delivers in time", "Objective may be missed", "None",
                               "Depends on the source", "n/a", approval="none"))
        if cause in ("upstream outage", "platform") and pid:
            if idem:
                at = "rerun_latest_window" if cause == "upstream outage" else "retry_failed_task"
                opts.append(self._catalogue("catalogue", f"{at} for {pid}", at,
                            f"Reloads the latest window of {pid}", "Low; pipeline is idempotent", "One run",
                            "One run", "Idempotent", pipeline=pid, target=head,
                            params={"window": window} if at == "rerun_latest_window" else {},
                            note="only after the source has delivered" if cause == "upstream outage" else ""))
            else:
                opts.append(Option(0, "catalogue", f"Re-run {pid} (not idempotent)",
                                   f"Reloads {pid}", "Duplicates or gaps: pipeline is not declared idempotent",
                                   "One run", "One run", "Not guaranteed", "rerun_past_window", {"window": window},
                                   pid, head, "OG2 (service_owner/platform_owner); human decision, executed by people",
                                   False, True, False, "never automatic on a non-idempotent pipeline"))
        if cause == "source data" and "schema" in classes:
            opts.append(Option(0, "code-fix", "Change request to Build: align contract or mapping",
                               "Load resumes once the mapping matches the source", "Low", "Build effort",
                               "Days", "Via release", approval="Build intake (change.request)"))
        if cause == "source data" and dup:
            opts.append(self._catalogue("data-correction", f"Correct {head} in place", "correct_data",
                        "Deletes duplicated rows in place", "Irreversible without the rollback plan",
                        "One job", "Hours", "Needs rollback plan", pipeline=pid, target=head))
        if cause == "code defect" and classes & {"quality"}:
            opts.append(self._catalogue("rollback", "Roll back the recent release", "rollback_request",
                        "Restores the previous code", "Loses the release's other changes", "One deployment",
                        "Hours", "Reversible (redeploy)", pipeline=pid, target=head))
            opts.append(Option(0, "code-fix", "Change request to Build: fix the defect",
                               "Removes the cause", "Low", "Build effort", "Days", "Via release",
                               approval="Build intake (change.request)"))
            opts.append(self._catalogue("data-correction", f"Backfill {head} after the fix", "backfill",
                        "Repairs affected windows", "Medium", "Backfill compute", "Hours",
                        "Needs rollback plan", pipeline=pid, target=head))
        if cause in ("code defect", "capacity") and classes & {"cost", "run"}:
            opts.append(Option(0, "code-fix", "Change request to Build: fix the query or job",
                               "Removes the waste or slowdown", "Low", "Build effort", "Days", "Via release",
                               approval="Build intake (change.request)"))
            opts.append(self._catalogue("trade-off", "Larger compute (trade-off, not a fix)", "resize",
                        "Hides the problem; cost rises", "Masks the cause", "Higher spend", "Minutes",
                        "Reversible", target=head, note="never scale up to make a failure go away; FinOps decides at OG4"))
        if cause == "access":
            opts.append(Option(0, "configuration", "Governance correction (revoke or re-mask)",
                               "Restores the approved access model", "Low", "None", "Hours",
                               "Reversible", "revoke_access", approval="OG5 (security_compliance); executed by people",
                               executed_by_people=True))

        opts.sort(key=lambda o: SAFETY_ORDER.index(o.kind))
        for i, o in enumerate(opts, start=1):
            o.no = i
        rec = next((o for o in opts if o.kind == "contain"), None) or next(
            (o for o in opts if o.kind not in ("trade-off",)), None)
        if rec:
            rec.recommended = True
        self._write_options(case_id, d, opts)
        if case.state == "diagnosing":
            self.cases.transition(case_id, "options-ready", by=self.identity,
                                  note=f"{len(opts)} options; recommended #{rec.no if rec else '-'}")
        return opts

    def _evidence(self, case_id: str, kind: str) -> list[dict]:
        root = self.ws.path(f"cases/{case_id}/evidence")
        return [json.loads(p.read_text()) for p in sorted(root.glob(f"E-*-{kind}.json"))] if root.exists() else []

    def _write_options(self, case_id: str, d: dict, opts: list[Option]) -> None:
        lines = [f"# Options - {case_id}", "", f"Diagnosis: {d['cause_class']} ({d['confidence']}) - {d['statement']}",
                 "", "Ranked by safety. Containment comes first when a rule applies.", "",
                 "| # | Kind | Option | Effect | Risk | Cost | Time | Reversibility | Approval | Executable now |",
                 "|---|---|---|---|---|---|---|---|---|---|"]
        for o in opts:
            lines.append(f"| {o.no}{' (recommended)' if o.recommended else ''} | {o.kind} | {o.title} | {o.effect} | "
                         f"{o.risk} | {o.cost} | {o.time} | {o.reversibility} | {o.approval} | "
                         f"{'yes' if o.executable_now else 'no'} |")
        notes = [f"- #{o.no}: {o.note}" for o in opts if o.note]
        if notes:
            lines += ["", "Notes:", *notes]
        self.ws.write(self.identity, f"cases/{case_id}/options.md", "\n".join(lines) + "\n")
        self.ws.write_yaml(self.identity, f"cases/{case_id}/actions/options.yaml", [asdict(o) for o in opts])

    def options(self, case_id: str) -> list[Option]:
        return [Option(**o) for o in self.ws.read_yaml(f"cases/{case_id}/actions/options.yaml") or []]

    # -- T3 / T4: act -----------------------------------------------------------------------
    def _next_act_id(self) -> str:
        n = int(self.executor.next_act_id().split("-")[1])
        root = self.ws.path("cases")
        files = [p.stem for p in root.glob("*/actions/ACT-*.yaml")] if root.exists() else []
        n = max([n] + [int(f.split("-")[1]) + 1 for f in files])
        return f"ACT-{n:03d}"

    def _save_act(self, act: ActionRecord, status: str, **extra) -> None:
        self.ws.write_yaml(self.identity, f"cases/{act.case_id}/actions/{act.id}.yaml",
                           {"status": status, **extra, "record": act.model_dump(mode="json")})

    def load_act(self, case_id: str, act_id: str) -> tuple[ActionRecord, dict]:
        doc = self.ws.read_yaml(f"cases/{case_id}/actions/{act_id}.yaml")
        return ActionRecord.model_validate(doc["record"]), doc

    def request_fix(self, case_id: str, action_type: str) -> None:
        """Ad-hoc request (e.g. from a person under pressure). Scale-up is never a fix (OT-07)."""
        if action_type in SCALE_UP:
            raise GuardViolation("refused: scaling up to make a failure go away hides the cause; it is offered "
                                 "only as a trade-off for FinOps at OG4")
        raise GuardViolation(f"choose a ranked option from options.md; {action_type} is not one")

    def act(self, case_id: str, option_no: int) -> dict:
        if self.registry.mode() == "shadow":
            raise GuardViolation("shadow mode: options are recorded for comparison with the team's own response; "
                                 "nothing is executed or requested")
        case = self.cases.get(case_id)
        opt = next((o for o in self.options(case_id) if o.no == option_no), None)
        if opt is None:
            raise GuardViolation(f"{case_id}: no option #{option_no}")
        if opt.kind == "trade-off":
            raise GuardViolation(f"option #{option_no} is a trade-off, not a fix; route to FinOps for OG4")
        if opt.kind == "wait":
            self.cases.update(case_id, by=self.identity, note="option: wait and watch detecting monitors",
                              next_action="adef-ops-remediation: verify")
            return {"option": option_no, "status": "waiting"}
        if not opt.action_type or opt.kind == "code-fix":
            chg = self.raise_change(case_id, title=opt.title)
            return {"option": option_no, "status": "change-requested", "change": chg}
        evidence = [s for s in case.signals]
        if opt.params.get("batch_id"):
            evidence.append(f"batch:{opt.params['batch_id']}")
        act = ActionRecord(id=self._next_act_id(), case_id=case_id, product=self.ws.dp_id,
                           action_type=opt.action_type, pipeline=opt.pipeline, target=opt.target or self.ws.dp_id,
                           params=opt.params, requested_by=self.identity, evidence=evidence,
                           downstream_impact=self._diag(case_id)["impact"]["affected_objects"],
                           rollback_plan=f"runbook {opt.reversibility}" if lookup(opt.action_type).action_class == "A3" else None)
        if opt.executable_now:
            if case.state == "options-ready":
                self.cases.transition(case_id, "executing", by=self.identity, note=f"{act.id} {act.action_type} at L3")
            try:
                out = self.executor.execute(act)
            except ExecutorBlocked as exc:
                self._save_act(act, "blocked", reason=str(exc))
                raise
            self._save_act(act, "executed", by="executor", result=out["result"])
            return {"option": option_no, "status": "executed", "act": act.id}
        gate = lookup(opt.action_type).gate or "OG2"
        role_key = "OG2-rollback" if opt.action_type == "rollback_request" else None
        req = self.approvals.request(gate=gate, case_id=case_id, requested_by="commander", role_key=role_key,
                                     decision_asked=f"{case_id}: {opt.title} ({opt.action_type})",
                                     artifact_hash=act_hash(act), evidence=[f"cases/{case_id}/options.md",
                                                                             f"cases/{case_id}/diagnosis.md"],
                                     risk=opt.risk, rollback=opt.reversibility,
                                     recommendation="recommended" if opt.recommended else "")
        self._save_act(act, "awaiting-approval", approval_id=req.id)
        if case.state == "options-ready":
            self.cases.transition(case_id, "awaiting-approval", by=self.identity, note=f"{req.id} {gate} for {act.id}")
        return {"option": option_no, "status": "awaiting-approval", "act": act.id, "approval": req.id}

    def proceed(self, case_id: str, act_id: str) -> dict:
        act, doc = self.load_act(case_id, act_id)
        state = self.approvals.status(doc["approval_id"])
        if state == "rejected":
            reason = next(d.reason for d in self.approvals.decisions(doc["approval_id"]) if d.decision == "rejected")
            self._save_act(act, "rejected", approval_id=doc["approval_id"], reason=reason)
            root = self.ws.path(f"cases/{case_id}/actions")
            rejected = sum(1 for p in root.glob("ACT-*.yaml") if (self.ws.read_yaml(
                f"cases/{case_id}/actions/{p.name}") or {}).get("status") == "rejected")
            if rejected >= 2:
                from ..engine.escalation import escalate
                escalate(self.ws, by=self.identity, to="platform_owner", case_id=case_id,
                         reason=f"{case_id}: {rejected} approval requests rejected; escalate instead of re-asking")
            if self.cases.get(case_id).state == "awaiting-approval":
                self.cases.transition(case_id, "options-ready", by=self.identity, note=f"rejected: {reason}")
            return {"act": act_id, "status": "rejected", "reason": reason}
        if state != "approved":
            return {"act": act_id, "status": "pending"}
        act = act.model_copy(update={"approval_id": doc["approval_id"]})
        level, _ = self.registry.effective_level(act.action_type)
        if self.cases.get(case_id).state == "awaiting-approval":
            self.cases.transition(case_id, "executing", by=self.identity, note=f"{doc['approval_id']} approved")
        if level >= Level.L2 and not lookup(act.action_type).executed_by_people:
            out = self.executor.execute(act)
            self._save_act(act, "executed", by="executor", approval_id=doc["approval_id"], result=out["result"])
            return {"act": act_id, "status": "executed"}
        self._save_act(act, "approved-awaiting-people", approval_id=doc["approval_id"])
        return {"act": act_id, "status": "approved-awaiting-people"}

    def confirm_executed_by_people(self, case_id: str, act_id: str, *, by: str) -> None:
        svc = self.ws.read_yaml("control/service.yaml") or {}
        named = set((svc.get("approvers") or {}).values()) | {svc.get("on_call")}
        if by not in named:
            raise GuardViolation(f"{by!r} is not a named person for {self.ws.dp_id}")
        act, doc = self.load_act(case_id, act_id)
        if doc["status"] != "approved-awaiting-people":
            raise GuardViolation(f"{act_id} is {doc['status']}, not approved and awaiting people")
        self._save_act(act, "executed-by-people", by=by, approval_id=doc.get("approval_id"))
        self.cases.update(case_id, by=self.identity, note=f"{act_id} executed by {by}")

    # -- T5: change requests ------------------------------------------------------------------
    def raise_change(self, case_id: str, *, title: str) -> str:
        d = self._diag(case_id)
        chg = self.cases.open("CHG", by=self.identity, note=f"from {case_id}: {title}")
        self.cases.link(chg.id, case_id, reason="change needed to remove the incident cause", by=self.identity)
        self.cases.link(case_id, chg.id, reason="change request raised", by=self.identity)
        self.ws.write(self.identity, f"change-requests/{chg.id}.md", "\n".join([
            f"# {chg.id} - {title}", "", f"- Incident: {case_id}", f"- Cause: {d['cause_class']} ({d['confidence']})",
            f"- Statement: {d['statement']}", f"- Evidence: cases/{case_id}/diagnosis.md, cases/{case_id}/evidence/",
            f"- Impact: {d['impact']['summary']}", ""]))
        self.ws.append(self.identity, "change-requests/events.jsonl", json.dumps(
            {"type": "change.request", "change": chg.id, "product": self.ws.dp_id, "incident": case_id,
             "at": datetime.now(timezone.utc).isoformat()}))
        return chg.id

    # -- T6: verify -------------------------------------------------------------------------
    def verify(self, case_id: str, *, at: Optional[datetime] = None) -> dict:
        case = self.cases.get(case_id)
        if case.state in ("executing", "options-ready"):
            case = self.cases.transition(case_id, "verifying", by=self.identity)
        if case.state != "verifying":
            raise GuardViolation(f"{case_id} is {case.state}; nothing to verify")
        run = MonitorEngine(self.ws, self.adapter, simulated=True).run(at)
        detecting = {m.monitor_id for m in case.detecting_monitors}
        results = [r for r in run["results"] if r["monitor_id"] in detecting]
        passed = all(r["status"] == "pass" for r in results) and {r["monitor_id"] for r in results} == detecting
        acts = [self.load_act(case_id, p.stem) for p in sorted(self.ws.path(f"cases/{case_id}/actions").glob("ACT-*.yaml"))] \
            if self.ws.path(f"cases/{case_id}/actions").exists() else []
        lines = [f"# Verification - {case_id}", "", f"Result: {'recovered' if passed else 'NOT recovered'}", "",
                 "Checked with the monitors that detected the problem:", "",
                 "| Monitor result | Monitor | Status | Observed | Expected |", "|---|---|---|---|---|",
                 *[f"| {r['id']} | {r['monitor_id']} | {r['status']} | {r['observed']} | {r['expected']} |" for r in results],
                 "", "Actions:", *([f"- {a.id} {a.action_type}: {doc['status']}" for a, doc in acts] or ["- none"]), ""]
        self.ws.write(self.identity, f"cases/{case_id}/verification.md", "\n".join(lines))
        for a, doc in acts:
            if doc["status"] == "executed":  # executor-run actions feed earned autonomy
                self.executor.record_verification(a, verified=passed)
                self._save_act(a, "verified" if passed else "failed-verification",
                               **{k: v for k, v in doc.items() if k not in ("status", "record")})
        if passed:
            self.cases.transition(case_id, "recovered", by=self.identity, verification_results=[r["id"] for r in results])
        else:
            self.cases.transition(case_id, "failed", by=self.identity, note="detecting monitors still failing")
        return {"case": case_id, "recovered": passed, "results": [r["id"] for r in results]}
