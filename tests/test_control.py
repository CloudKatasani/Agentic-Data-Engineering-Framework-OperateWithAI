"""Epic O2: autonomy, executor, standing approvals, earned autonomy, budgets, stop.

Covers the Day 2-3 exit check: OT-01, OT-04, OT-11, OT-12, OT-15 (plus OT-08, OT-09, OT-10).
"""

from datetime import timedelta

import pytest

from adef_operate.core.autonomy import Level
from adef_operate.core.errors import ApprovalError, BudgetExceeded, ExecutorBlocked
from adef_operate.engine.escalation import escalations
from adef_operate.engine.executor import act_hash, notice_hash
from adef_operate.engine.registry import Registry
from adef_operate.schemas import ActionRecord

N = iter(range(1, 10_000))


def act(p, action_type, *, case_id="INC-001", pipeline=None, target="raw.meter_interval_reads", **kw):
    params = kw.pop("params", {})
    return ActionRecord(id=f"ACT-{next(N):03d}", case_id=case_id, product=p.ws.dp_id,
                        action_type=action_type, pipeline=pipeline, target=target, params=params,
                        requested_by="remediation", **kw)


def open_case(p, severity="S3"):
    return p.cases.open("INC", by="commander", severity=severity, severity_rule="test rule")


# -- O2-1 ------------------------------------------------------------------------------
def test_l1_action_cannot_execute(util):
    open_case(util)
    with pytest.raises(ExecutorBlocked, match="L1"):
        util.executor.execute(act(util, "retry_failed_task", pipeline="meter_ingest"))


def test_unknown_action_type_treated_as_a3_l1(util):
    open_case(util)
    with pytest.raises(ExecutorBlocked, match="not a catalogue action"):
        util.executor.execute(act(util, "truncate_table"))
    assert util.registry.stored_level("truncate_table") == Level.L1


def test_l3_retry_executes(util):
    open_case(util)
    util.promote("retry_failed_task", "L3")
    util.sim.inject("F-08", "utilities")
    out = util.executor.execute(act(util, "retry_failed_task", pipeline="meter_ingest"))
    assert out["outcome"] == "executed" and out["level"] == "L3"
    assert util.executor.log.verify()


def test_promotion_needs_og7_and_respects_ceiling(util):
    with pytest.raises(ApprovalError):
        util.registry.set_level("retry_failed_task", "L3", approval_id="APR-001",
                                approvals=util.approvals, artifact_hash="sha256:x")
    with pytest.raises(ApprovalError, match="cannot exceed"):
        util.promote("rerun_past_window", "L3")


# -- O2-2 ------------------------------------------------------------------------------
def test_ot01_non_idempotent_rerun_blocked(telco):
    """OT-01: failed job on a non-idempotent pipeline: no re-run, even at L3."""
    open_case(telco)
    telco.promote("retry_failed_task", "L3")
    with pytest.raises(ExecutorBlocked, match="not declared idempotent"):
        telco.executor.execute(act(telco, "retry_failed_task", pipeline="usage_ingest",
                                   target="raw.usage_records"))


def test_ot04_third_retry_blocked_and_escalated(util):
    open_case(util)
    util.promote("retry_failed_task", "L3")
    for _ in range(2):
        util.executor.execute(act(util, "retry_failed_task", pipeline="meter_ingest"))
    with pytest.raises(ExecutorBlocked, match="retry cap") as exc:
        util.executor.execute(act(util, "retry_failed_task", pipeline="meter_ingest"))
    assert exc.value.escalate
    assert any("retry cap" in e["reason"] for e in escalations(util.ws))


def test_retry_blocked_while_s1_open(util):
    open_case(util, severity="S1")
    util.promote("retry_failed_task", "L3")
    with pytest.raises(ExecutorBlocked, match="S1"):
        util.executor.execute(act(util, "retry_failed_task", pipeline="meter_ingest"))


def test_rerun_latest_window_checks_window(util):
    open_case(util)
    util.promote("rerun_latest_window", "L3")
    with pytest.raises(ExecutorBlocked, match="most recent"):
        util.executor.execute(act(util, "rerun_latest_window", pipeline="meter_ingest",
                                  params={"window": "2025-12-31"}))
    util.executor.execute(act(util, "rerun_latest_window", pipeline="meter_ingest",
                              params={"window": "2026-01-05"}))


def test_pause_requires_blocking_evidence_and_downstream(util):
    open_case(util)
    util.promote("pause_pipeline", "L3")
    with pytest.raises(ExecutorBlocked, match="blocking quality"):
        util.executor.execute(act(util, "pause_pipeline", pipeline="meter_ingest",
                                  downstream_impact=["mart.daily_consumption"]))
    util.sim.inject("F-05", "utilities")
    sig = next(s for s in util.engine.run()["signals"] if s["signal_class"] == "quality")
    with pytest.raises(ExecutorBlocked, match="downstream"):
        util.executor.execute(act(util, "pause_pipeline", pipeline="meter_ingest", evidence=[sig["id"]]))
    out = util.executor.execute(act(util, "pause_pipeline", pipeline="meter_ingest", evidence=[sig["id"]],
                                    downstream_impact=["mart.daily_consumption"]))
    assert out["outcome"] == "executed"


def test_quarantine_needs_path_and_batch_evidence(util):
    open_case(util)
    util.promote("quarantine_batch", "L3")
    util.sim.inject("F-02", "utilities")
    with pytest.raises(ExecutorBlocked, match="batch ID"):
        util.executor.execute(act(util, "quarantine_batch", pipeline="outage_ingest", target="raw.outage_events",
                                  params={"batch_id": "B-20260105-06"}))
    with pytest.raises(ExecutorBlocked, match="quarantine path"):
        util.executor.execute(act(util, "quarantine_batch", pipeline="meter_ingest",
                                  params={"batch_id": "B-1"}, evidence=["batch:B-1"]))
    util.executor.execute(act(util, "quarantine_batch", pipeline="outage_ingest", target="raw.outage_events",
                              params={"batch_id": "B-20260105-06"}, evidence=["batch:B-20260105-06"]))


def test_trust_status_follows_severity_rule(util):
    c = open_case(util, severity="S2")
    with pytest.raises(ExecutorBlocked, match="severity rule"):
        util.executor.execute(act(util, "set_trust_status", case_id=c.id, params={"status": "trusted"}))
    util.executor.execute(act(util, "set_trust_status", case_id=c.id, params={"status": "degraded"}))
    assert util.sim.state["util_meter_daily"]["trust_status"] == "degraded"


# -- A3 actions and gates --------------------------------------------------------------
def _approved_a3(p, a, gate, role_key=None):
    req = p.approvals.request(gate=gate, decision_asked=f"{a.action_type}", artifact_hash=act_hash(a),
                              requested_by="commander", role_key=role_key)
    p.approve(req.id)
    return a.model_copy(update={"approval_id": req.id})


def test_a3_past_window_needs_og2_and_level_l2(util):
    open_case(util)
    a = act(util, "rerun_past_window", pipeline="meter_ingest", params={"window": "2026-01-03"})
    with pytest.raises(ExecutorBlocked, match="L1"):
        util.executor.execute(_approved_a3(util, a, "OG2"))
    util.promote("rerun_past_window", "L2")
    with pytest.raises(ExecutorBlocked, match="needs an approved OG2"):
        util.executor.execute(a)
    edited = _approved_a3(util, a, "OG2").model_copy(update={"params": {"window": "2026-01-01"}})
    with pytest.raises(ExecutorBlocked, match="hash mismatch"):
        util.executor.execute(edited)
    assert util.executor.execute(_approved_a3(util, a, "OG2"))["outcome"] == "executed"


def test_approval_rules(util):
    req = util.approvals.request(gate="OG3", decision_asked="backfill", artifact_hash="sha256:a",
                                 requested_by="commander")
    with pytest.raises(ApprovalError, match="agent"):
        util.approvals.decide(req.id, approver="remediation", role="data_owner", decision="approved",
                              artifact_hash="sha256:a")
    with pytest.raises(ApprovalError, match="does not hold"):
        util.approvals.decide(req.id, approver="Someone Else", role="data_owner", decision="approved",
                              artifact_hash="sha256:a")
    util.approvals.decide(req.id, approver="Olu Owner", role="data_owner", decision="approved",
                          artifact_hash="sha256:a")
    assert util.approvals.status(req.id) == "pending"  # OG3 needs Data Steward too
    util.approvals.decide(req.id, approver="Dana Steward", role="data_steward", decision="approved",
                          artifact_hash="sha256:a")
    assert util.approvals.status(req.id) == "approved"
    late = req.expires + timedelta(minutes=1)
    with pytest.raises(ApprovalError, match="expired"):
        util.approvals.require(req.id, gate="OG3", artifact_hash="sha256:a", at=late)


def test_ot08_drop_from_telemetry_alone_blocked(util):
    open_case(util)
    util.promote("drop_object", "L2")
    a = act(util, "drop_object", target="curated.outages", evidence=["telemetry:no reads 90d"])
    with pytest.raises(ExecutorBlocked, match="lineage and usage"):
        util.executor.execute(_approved_a3(util, a, "OG4"))
    b = act(util, "drop_object", target="curated.outages",
            evidence=["telemetry:no reads 90d", "lineage:no downstream", "usage:no Consume reads 90d"])
    assert util.executor.execute(_approved_a3(util, b, "OG4"))["outcome"] == "executed"


def test_ot09_revoke_never_executed_by_agent(util):
    open_case(util)
    a = act(util, "revoke_access", target="role:temp_contractor")
    with pytest.raises(ExecutorBlocked, match="people"):
        util.executor.execute(_approved_a3(util, a, "OG5"))


def test_ot10_edited_notice_blocked(util):
    open_case(util)
    util.promote("send_notice", "L2")
    text, audience = "Daily consumption is late; next update 08:00.", "settlement-reporting"
    req = util.approvals.request(gate="OG6", decision_asked="send notice", requested_by="commander",
                                 artifact_hash=notice_hash(text, audience), audience=[audience])
    util.approve(req.id)
    edited = act(util, "send_notice", target="consumers", approval_id=req.id,
                 params={"text": text + " Cause: vendor bug.", "audience": audience})
    with pytest.raises(ExecutorBlocked, match="hash mismatch"):
        util.executor.execute(edited)
    ok = act(util, "send_notice", target="consumers", approval_id=req.id,
             params={"text": text, "audience": audience})
    assert util.executor.execute(ok)["outcome"] == "executed"


# -- O2-3 ------------------------------------------------------------------------------
def test_ot11_runbook_edit_voids_og1(util):
    open_case(util)
    util.promote("retry_failed_task", "L3")
    assert util.registry.effective_level("retry_failed_task")[0] == Level.L3
    util.ws.write("onboarding", "service/runbook.md", util.ws.read("service/runbook.md") + "\nextra line\n")
    level, why = util.registry.effective_level("retry_failed_task")
    assert level == Level.L1 and "voided" in why and "runbook.md" in why
    with pytest.raises(ExecutorBlocked, match="L1"):
        util.executor.execute(act(util, "retry_failed_task", pipeline="meter_ingest"))


def test_og1_expires_after_90_days(util, now):
    assert util.approvals.og1_status(now)[0]
    valid, why = util.approvals.og1_status(now + timedelta(days=91))
    assert not valid and "expired" in why


# -- O2-4 ------------------------------------------------------------------------------
def test_ot12_failed_verification_demotes_to_l1(util):
    open_case(util)
    util.promote("retry_failed_task", "L3")
    a = act(util, "retry_failed_task", pipeline="meter_ingest")
    util.executor.execute(a)
    util.executor.record_verification(a, verified=False)
    assert util.registry.stored_level("retry_failed_task") == Level.L1
    assert any("demoted L3 -> L1" in e["reason"] for e in escalations(util.ws))
    tr = util.registry.load()["track_record"]["retry_failed_task"]
    assert tr == {"verified": 0, "failed": 1}


# -- O2-6 ------------------------------------------------------------------------------
def test_ot15_model_budget_exceeded_sets_l0(util):
    cap = util.ws.read_yaml("control/service.yaml")["budgets"]["model_spend"]
    assert util.registry.charge("model_spend", cap * 0.5, by="diagnosis") == "ok"
    assert util.registry.charge("model_spend", cap * 0.35, by="diagnosis") == "warn"
    with pytest.raises(BudgetExceeded):
        util.registry.charge("model_spend", cap * 0.5, by="diagnosis")
    assert util.registry.effective_level("set_trust_status")[0] == Level.L0
    assert any("budget exceeded" in e["reason"] for e in escalations(util.ws))
    open_case(util, severity="S2")
    with pytest.raises(ExecutorBlocked, match="L0"):
        util.executor.execute(act(util, "set_trust_status", params={"status": "degraded"}))


def test_action_count_budget(util):
    open_case(util)
    svc = util.ws.read_yaml("control/service.yaml")
    svc["budgets"]["actions"] = 1
    util.ws.write_yaml("commander", "control/service.yaml", svc)
    util.promote("rerun_latest_window", "L3")
    util.executor.execute(act(util, "rerun_latest_window", pipeline="meter_ingest", params={"window": "2026-01-05"}))
    with pytest.raises(ExecutorBlocked, match="budget"):
        util.executor.execute(act(util, "rerun_latest_window", pipeline="outage_ingest",
                                  target="raw.outage_events", params={"window": "2026-01-05"}))


def test_emergency_stop(util, home):
    open_case(util, severity="S2")
    Registry.emergency_stop(by="Pat Platform", reason="drill", home=home)
    level, why = util.registry.effective_level("set_trust_status")
    assert level == Level.L0 and "drill" in why
    with pytest.raises(ExecutorBlocked, match="L0"):
        util.executor.execute(act(util, "set_trust_status", params={"status": "degraded"}))
