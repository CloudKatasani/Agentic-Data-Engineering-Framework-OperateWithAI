"""Action executor behind the hook (backlog O2-1, O2-2, O2-4, O2-6).

The executor is a separate identity from every agent. It accepts an ``ACT`` record only if:

1. nothing is stopped (global emergency stop, product L0, budget breach);
2. the action type is in the catalogue *and* in the bound adapter's operate section;
3. the effective autonomy level allows execution (L1 never executes; unknown types are
   A3 at L1);
4. a matching, unexpired gate approval exists where the class or level needs one;
5. every precondition holds when re-read from the workspace and the adapter, never from
   what the agent asserted;
6. retry, loop and budget caps are not breached.

Every attempt, blocked or executed, is appended to the hash-chained action log.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..core.autonomy import ActionClass, Level
from ..core.errors import AdefError, ApprovalError, ExecutorBlocked
from ..core.hashing import canonical_hash
from ..schemas.records import ActionRecord
from .approvals import Approvals
from .case_store import CaseStore
from .catalogue import DEFAULT_CATALOGUE, RERUN_TYPES, lookup
from .escalation import escalate
from .registry import Registry
from .workspace import Workspace

TRUST_BY_SEVERITY = {"S1": "do-not-use", "S2": "degraded"}


def act_hash(act: ActionRecord) -> str:
    """Hash an approval is tied to: the ACT record without its approval reference."""
    return canonical_hash(act.model_dump(mode="json", exclude={"approval_id", "created"}))


def notice_hash(text: str, audience: str) -> str:
    return canonical_hash({"text": text, "audience": sorted(a.strip() for a in audience.split(","))})


class Executor:
    COUNTERS = "control/executor-counters.yaml"

    def __init__(self, ws: Workspace, adapter):
        self.ws = ws
        self.adapter = adapter
        self.registry = Registry(ws)
        self.approvals = Approvals(ws)
        self.cases = CaseStore(ws)
        self.log = ws.chainlog("executor", "control/action-log.jsonl")

    # -- helpers ------------------------------------------------------------------------
    def next_act_id(self) -> str:
        ids = {e["record"].get("act") for e in self.log.entries()}
        return f"ACT-{len(ids) + 1:03d}"

    def _pipeline(self, pipeline_id: Optional[str]) -> Optional[dict]:
        if not pipeline_id:
            return None
        for src in ("service/service-definition.yaml", "control/handover-pack.yaml"):
            doc = self.ws.read_yaml(src) or {}
            for p in doc.get("pipelines", []):
                if p.get("id") == pipeline_id:
                    return p
        return None

    def _signals(self) -> dict[str, dict]:
        import json
        out: dict[str, dict] = {}
        root = self.ws.path("signals")
        if root.exists():
            for f in root.glob("*.jsonl"):
                for line in f.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        s = json.loads(line)
                        out[s["id"]] = s
        return out

    def _counter(self, day: str, obj: str) -> int:
        data = self.ws.read_yaml(self.COUNTERS) or {}
        return int(data.get(day, {}).get(obj, 0))

    def _bump(self, day: str, obj: str) -> None:
        data = self.ws.read_yaml(self.COUNTERS) or {}
        data = {day: data.get(day, {})}  # keep today only
        data[day][obj] = int(data[day].get(obj, 0)) + 1
        self.ws.write_yaml("executor", self.COUNTERS, data)

    def _block(self, act: ActionRecord, reason: str, *, escalate_to: Optional[str] = None) -> None:
        self.log.append({"act": act.id, "case": act.case_id, "action_type": act.action_type,
                         "outcome": "blocked", "reason": reason})
        if escalate_to:
            escalate(self.ws, by="executor", to=escalate_to, case_id=act.case_id,
                     reason=f"{act.id} {act.action_type} blocked: {reason}")
        raise ExecutorBlocked(f"{act.id} {act.action_type} blocked: {reason}", escalate=bool(escalate_to))

    # -- preconditions ------------------------------------------------------------------
    def _check_preconditions(self, act: ActionRecord, entry, day: str) -> None:
        pipe = self._pipeline(act.pipeline)
        for pre in entry.preconditions:
            if pre == "idempotent":
                if pipe is None or pipe.get("idempotent") is not True:
                    self._block(act, f"pipeline {act.pipeline!r} is not declared idempotent; a re-run needs "
                                     "a human decision and is executed by people")
            elif pre == "retry_cap":
                pass  # enforced for every re-run type below
            elif pre == "no_open_s1":
                if self.cases.has_open_s1():
                    self._block(act, "an S1 is open on this product")
            elif pre == "latest_window":
                latest = self.adapter.latest_window(self.ws.dp_id, act.pipeline)
                if act.params.get("window") != latest:
                    self._block(act, f"window {act.params.get('window')!r} is not the most recent "
                                     f"scheduled window ({latest}); use rerun_past_window at OG2")
            elif pre == "blocking_evidence":
                sigs = self._signals()
                ok = any(sigs.get(e, {}).get("signal_class") in ("quality", "schema") for e in act.evidence)
                if not ok:
                    self._block(act, "no blocking quality failure or schema drift signal is evidenced")
            elif pre == "downstream_listed":
                if not act.downstream_impact:
                    self._block(act, "downstream impact is not listed")
            elif pre == "quarantine_path":
                if pipe is None or not pipe.get("quarantine_path"):
                    self._block(act, f"no quarantine path is defined for pipeline {act.pipeline!r}")
            elif pre == "batch_evidenced":
                if not act.params.get("batch_id") or not any(str(act.params["batch_id"]) in e for e in act.evidence):
                    self._block(act, "batch ID is not evidenced")
            elif pre == "rollback_plan":
                if not act.rollback_plan:
                    self._block(act, "no rollback plan")
            elif pre == "lineage_and_usage_evidence":
                kinds = {e.split(":", 1)[0] for e in act.evidence}
                if not {"lineage", "usage"} <= kinds:
                    self._block(act, "drop needs lineage and usage (Consume) evidence, not telemetry alone")
            elif pre == "rule_based":
                case = self.cases.get(act.case_id)
                want = TRUST_BY_SEVERITY.get(case.severity or "", "trusted")
                if act.params.get("status") != want:
                    self._block(act, f"trust status must follow the severity rule ({case.severity} -> {want})")
            elif pre == "text_hash":
                pass  # checked with the OG6 approval below
        if act.action_type in RERUN_TYPES:
            cap = int(self.adapter.limits.get("retry_cap_per_object_per_day", 2))
            if self._counter(day, act.target) >= cap:
                self._block(act, f"retry cap reached for {act.target} today ({cap}); stop and escalate",
                            escalate_to="on_call")

    # -- main ---------------------------------------------------------------------------
    def execute(self, act: ActionRecord | dict[str, Any], *, at: Optional[datetime] = None) -> dict[str, Any]:
        at = at or datetime.now(timezone.utc)
        act = act if isinstance(act, ActionRecord) else ActionRecord.model_validate(act)
        if act.product != self.ws.dp_id:
            self._block(act, f"ACT is for product {act.product}, not {self.ws.dp_id}")
        entry = lookup(act.action_type)
        if entry.executed_by_people:
            self._block(act, f"{act.action_type} is executed by people after {entry.gate}; never by an agent")
        if act.action_type not in DEFAULT_CATALOGUE or act.action_type not in self.adapter.actions:
            self._block(act, f"{act.action_type!r} is not a catalogue action of the bound adapter; "
                             "treated as A3 at L1")

        level, why = self.registry.effective_level(act.action_type, at)
        if level == Level.L0:
            self._block(act, f"product is at L0 ({why})")
        if level == Level.L1:
            self._block(act, f"{act.action_type} is at L1 ({why}); the executor refuses. Present it as "
                             f"an option for {entry.gate or 'a human decision'}")

        needs_approval = entry.action_class == ActionClass.A3 or level == Level.L2
        if needs_approval:
            gate = entry.gate or "OG2"
            if not act.approval_id:
                self._block(act, f"{act.action_type} at {level.name} needs an approved {gate}")
            want = (notice_hash(str(act.params.get("text", "")), str(act.params.get("audience", "")))
                    if act.action_type == "send_notice" else act_hash(act))
            try:
                self.approvals.require(act.approval_id, gate=gate, artifact_hash=want, at=at)
            except ApprovalError as exc:
                self._block(act, str(exc))

        day = at.date().isoformat()
        self._check_preconditions(act, entry, day)

        try:
            self.registry.charge("actions", 1, by="executor", day=at.date())
        except AdefError as exc:
            self._block(act, str(exc))

        if act.action_type in RERUN_TYPES:
            self._bump(day, act.target)
        params = {**act.params, "pipeline": act.pipeline, "target": act.target}
        result = self.adapter.run_action(act.action_type, self.ws.dp_id, params)
        self.log.append({"act": act.id, "case": act.case_id, "action_type": act.action_type,
                         "level": level.name, "approval": act.approval_id, "outcome": "executed",
                         "result": result}, at=at)
        return {"act": act.id, "outcome": "executed", "level": level.name, "result": result}

    def record_verification(self, act: ActionRecord, *, verified: bool) -> None:
        """Feed the earned-autonomy counter; a failed verification demotes to L1."""
        self.log.append({"act": act.id, "case": act.case_id, "action_type": act.action_type,
                         "outcome": "verified" if verified else "verification-failed"})
        self.registry.record_outcome(act.action_type, verified=verified, case_id=act.case_id)
