"""Day 7: O4-3 (Diagnosis) and O4-4 (Remediation at L1).

Exit check: 7 of 8 causes correct with evidence; ranked options with approval needs;
verification record on recovery.
"""

import json

import pytest

from adef_operate.agents.diagnosis import DiagnosisAgent
from adef_operate.agents.reasoning import Hypothesis
from adef_operate.core.autonomy import Level
from adef_operate.core.errors import GuardViolation
from adef_operate.engine.council import Council
from adef_operate.engine.escalation import escalations
from adef_operate.engine.evidence import EvidenceReader
from adef_operate.engine.registry import Registry
from adef_operate.simulation import Scenario, diagnosis_accuracy


@pytest.fixture
def u(home):
    return Scenario(home, "utilities")


@pytest.fixture
def t(home):
    return Scenario(home, "telco")


def incident(sc, fault):
    sc.sim.inject(fault, sc.pack_name)
    out = sc.tick()
    return out["outcomes"][0]["case"]


def council_agrees(sc, case_id):
    c = Council(sc.ws)
    for r in ("Reviewer A", "Reviewer B"):
        c.record(case_id, reviewer=r, agrees=True)


# -- O4-3 Diagnosis --------------------------------------------------------------------------
def test_exit_check_cause_class_on_library_faults(tmp_path):
    r = diagnosis_accuracy(tmp_path)
    assert r["faults_correct"] >= 7, r["rows"]
    assert r["unsupported_causes"] == 0
    for row in r["rows"]:
        assert row["hypotheses"] >= 2 and row["evidence"] >= 1 and row["ruled_out"] >= 1


def test_diagnosis_writes_impact_evidence_and_moves_case(u):
    case_id = incident(u, "F-01")
    r = u.diagnosis.diagnose(case_id)
    assert r.cause_class == "upstream outage" and r.confidence == "high"
    case = u.commander.cases.get(case_id)
    assert case.state == "diagnosing"
    assert "settlement-reporting" in u.ws.read(f"cases/{case_id}/impact.md")
    md = u.ws.read(f"cases/{case_id}/diagnosis.md")
    assert "## Ruled out" in md and "What would confirm it" in md
    assert list(u.ws.path(f"cases/{case_id}/evidence").glob("E-*.json"))


def test_recent_change_is_a_lead_not_a_conclusion(u):
    """A release on an object with no failing signal must not become 'code defect'."""
    case_id = incident(u, "F-01")
    u.sim.state[u.dp]["releases"].append({"at": "2026-01-05T05:00:00+00:00", "object": "mart.daily_consumption",
                                          "release": "R-x"})
    r = u.diagnosis.diagnose(case_id)
    assert r.cause_class == "upstream outage"
    code = next(h for h in r.hypotheses if h["cause_class"] == "code defect")
    assert code["status"] != "supported"


class EmptyReasoner:
    name = "empty"

    def hypothesize(self, context, evidence):
        return [Hypothesis("code defect", "guess", 5, ["E-99: invented"], [], "supported"),
                Hypothesis("platform", "guess", 0, [], [], "unsupported")]


def test_no_cause_without_evidence_whatever_the_reasoner(u):
    case_id = incident(u, "F-01")
    r = DiagnosisAgent(u.ws, u.sim, reasoner=EmptyReasoner()).diagnose(case_id)
    assert r.cause_class == "unknown" and r.confidence == "low"


def test_ot05_restricted_raw_values_refused_and_escalated(u):
    case_id = incident(u, "F-05")
    reader = EvidenceReader(u.ws, u.sim, case_id)
    with pytest.raises(GuardViolation, match="statistics"):
        reader.raw_values("raw.meter_interval_reads", "meter_id", reason="check negative reads")
    assert any(e["to"] == "data_steward" for e in escalations(u.ws))
    stats = reader.profile("raw.meter_interval_reads", "meter_id")
    assert set(stats.data) == {"object", "column", "rows", "null_pct", "distinct_estimate"}


def test_ot06_injected_log_text_is_reported_not_followed(u):
    case_id = incident(u, "F-08")
    before = u.ws.read("service/monitoring-spec.yaml")
    r = u.diagnosis.diagnose(case_id)
    assert r.cause_class == "platform" and r.untrusted_reports
    assert u.ws.read("service/monitoring-spec.yaml") == before
    assert all(lvl == "L1" for t, lvl in Registry(u.ws).load()["levels"].items() if t != "set_trust_status")
    assert "not followed" in u.ws.read(f"cases/{case_id}/diagnosis.md")
    assert any("treated as data" in q for q in u.commander.cases.get(case_id).open_questions)


def test_diagnosis_is_charged_to_the_diagnostic_budget(u):
    case_id = incident(u, "F-01")
    u.diagnosis.diagnose(case_id)
    ledger = [json.loads(l)["record"] for l in u.ws.read("control/budget-ledger.jsonl").splitlines()]
    assert any(r["kind"] == "diagnostic_query_spend" for r in ledger)


def test_diagnosis_cannot_write_actions(u):
    from adef_operate.core.errors import WriteNotAllowed
    with pytest.raises(WriteNotAllowed):
        u.ws.write("diagnosis", "cases/INC-001/options.md", "x")


# -- Council ---------------------------------------------------------------------------------
def test_s1_options_wait_for_council(u):
    case_id = incident(u, "F-05")
    u.diagnosis.diagnose(case_id)
    with pytest.raises(GuardViolation, match="council"):
        u.remediation.build_options(case_id)
    with pytest.raises(GuardViolation):
        Council(u.ws).record(case_id, reviewer="diagnosis", agrees=True)
    council_agrees(u, case_id)
    assert u.remediation.build_options(case_id)


# -- O4-4 Remediation ------------------------------------------------------------------------
def test_options_ranked_with_approval_needs(u):
    case_id = incident(u, "F-05")
    u.diagnosis.diagnose(case_id)
    council_agrees(u, case_id)
    opts = u.remediation.build_options(case_id)
    kinds = [o.kind for o in opts]
    assert kinds[0] == "contain" and opts[0].action_type == "pause_pipeline" and opts[0].recommended
    assert "rollback" in kinds and "code-fix" in kinds
    rollback = next(o for o in opts if o.kind == "rollback")
    assert "release_manager" in rollback.approval
    backfill = next(o for o in opts if o.action_type == "backfill")
    assert "data_steward" in backfill.approval and "data_owner" in backfill.approval
    assert all(o.approval for o in opts)
    assert u.commander.cases.get(case_id).state == "options-ready"
    assert "| 1 (recommended) | contain |" in u.ws.read(f"cases/{case_id}/options.md")


def test_ot01_non_idempotent_rerun_offered_for_og2_only(t):
    case_id = incident(t, "F-03")
    t.diagnosis.diagnose(case_id)
    council_agrees(t, case_id)
    opts = t.remediation.build_options(case_id)
    rerun = next(o for o in opts if o.pipeline == "usage_ingest" and o.action_type)
    assert not rerun.executable_now and rerun.executed_by_people and "OG2" in rerun.approval
    assert opts[0].kind == "wait"


def test_ot07_scale_up_refused_as_fix(u):
    case_id = None
    with pytest.raises(GuardViolation, match="trade-off"):
        u.remediation.request_fix(case_id, "resize")


def test_l1_recovery_flow_with_verification_record(u):
    """F-02 at L1: quarantine needs OG2, people execute it, monitors prove recovery."""
    case_id = incident(u, "F-02")
    u.diagnosis.diagnose(case_id)
    council_agrees(u, case_id)  # F-02 is S1 (duplicates reached consumers)
    opts = u.remediation.build_options(case_id)
    q = next(o for o in opts if o.action_type == "quarantine_batch")
    assert q.recommended and not q.executable_now
    res = u.remediation.act(case_id, q.no)
    assert res["status"] == "awaiting-approval"
    u.approve(res["approval"])
    assert u.remediation.proceed(case_id, res["act"])["status"] == "approved-awaiting-people"
    # A person runs the action on the platform, then confirms it.
    u.sim.run_action("quarantine_batch", u.dp, {"batch_id": "B-20260105-06"})
    u.remediation.confirm_executed_by_people(case_id, res["act"], by="Oncall Utilities")
    v = u.remediation.verify(case_id)
    assert v["recovered"]
    case = u.commander.cases.get(case_id)
    assert case.state == "recovered"
    text = u.ws.read(f"cases/{case_id}/verification.md")
    assert all(r in text for r in v["results"]) and "executed-by-people" in text


def test_rejection_returns_reason_word_for_word(u):
    case_id = incident(u, "F-01")
    u.diagnosis.diagnose(case_id)
    opts = u.remediation.build_options(case_id)
    rerun = next(o for o in opts if o.action_type == "rerun_latest_window")
    res = u.remediation.act(case_id, rerun.no)
    u.approve(res["approval"], decision="rejected", reason="Source has not delivered yet.")
    out = u.remediation.proceed(case_id, res["act"])
    assert out == {"act": res["act"], "status": "rejected", "reason": "Source has not delivered yet."}
    assert u.commander.cases.get(case_id).state == "options-ready"


def test_wait_then_people_rerun_after_source_arrives(u):
    case_id = incident(u, "F-01")
    u.diagnosis.diagnose(case_id)
    opts = u.remediation.build_options(case_id)
    assert opts[0].kind == "wait"
    u.remediation.act(case_id, opts[0].no)
    v = u.remediation.verify(case_id)  # still late: one failed attempt
    assert not v["recovered"] and u.commander.cases.get(case_id).failed_attempts == 1
    u.commander.cases.transition(case_id, "options-ready", by="remediation")
    u.sim.resolve_source("F-01")
    rerun = next(o for o in u.remediation.options(case_id) if o.action_type == "rerun_latest_window")
    res = u.remediation.act(case_id, rerun.no)
    u.approve(res["approval"])
    u.remediation.proceed(case_id, res["act"])
    u.sim.run_action("rerun_latest_window", u.dp, {"pipeline": "meter_ingest"})
    u.remediation.confirm_executed_by_people(case_id, res["act"], by="Oncall Utilities")
    assert u.remediation.verify(case_id)["recovered"]


def test_l3_retry_executes_and_earns_track_record(u, home):
    case_id = incident(u, "F-08")
    from adef_operate.core.hashing import canonical_hash
    from adef_operate.engine.approvals import Approvals
    proposal = canonical_hash({"action_type": "retry_failed_task", "level": "L3"})
    req = Approvals(u.ws).request(gate="OG7", decision_asked="promote", artifact_hash=proposal, requested_by="knowledge")
    u.approve(req.id)
    Registry(u.ws).set_level("retry_failed_task", "L3", approval_id=req.id, approvals=Approvals(u.ws),
                             artifact_hash=proposal)
    u.diagnosis.diagnose(case_id)
    opts = u.remediation.build_options(case_id)
    retry = next(o for o in opts if o.action_type == "retry_failed_task")
    assert retry.executable_now and retry.approval.startswith("none")
    assert u.remediation.act(case_id, retry.no)["status"] == "executed"
    assert u.remediation.verify(case_id)["recovered"]
    assert Registry(u.ws).load()["track_record"]["retry_failed_task"] == {"verified": 1, "failed": 0}
    assert Registry(u.ws).stored_level("retry_failed_task") == Level.L3


def test_schema_drift_raises_change_request(t):
    case_id = incident(t, "F-04")
    t.diagnosis.diagnose(case_id)
    opts = t.remediation.build_options(case_id)
    assert opts[0].action_type == "pause_pipeline"
    chg_opt = next(o for o in opts if o.kind == "code-fix")
    out = t.remediation.act(case_id, chg_opt.no)
    assert out["status"] == "change-requested"
    chg = out["change"]
    assert t.ws.has(f"change-requests/{chg}.md")
    ev = json.loads(t.ws.read("change-requests/events.jsonl").splitlines()[-1])
    assert ev["type"] == "change.request" and ev["incident"] == case_id
    assert any(l.id == chg for l in t.commander.cases.get(case_id).linked_cases)


def test_remediation_cannot_mark_recovered_without_monitors(u):
    case_id = incident(u, "F-01")
    u.diagnosis.diagnose(case_id)
    u.remediation.build_options(case_id)
    for s in ("verifying",):
        u.commander.cases.transition(case_id, s, by="remediation")
    with pytest.raises(GuardViolation):
        u.commander.cases.transition(case_id, "recovered", by="remediation")
