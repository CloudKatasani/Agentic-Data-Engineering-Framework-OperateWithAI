"""Epic O1: workspace allowlists, case store, schemas, handover receiver check."""

import copy

import pytest

from adef_operate.core.chainlog import ChainLog
from adef_operate.core.errors import GuardViolation, IllegalTransition, ValidationFailed, WriteNotAllowed
from adef_operate.engine.escalation import escalations
from adef_operate.engine.handover import accept_handover, on_product_released, receiver_check
from adef_operate.engine.workspace import Workspace, may_write
from adef_operate.packs import handover_pack
from adef_operate.schemas import validate_record


# -- O1-2 ------------------------------------------------------------------------------
def test_diagnosis_cannot_write_actions(home):
    ws = Workspace("dp1", home).create()
    with pytest.raises(WriteNotAllowed):
        ws.write("diagnosis", "cases/INC-001/actions/ACT-001.yaml", "x")
    ws.write("diagnosis", "cases/INC-001/diagnosis.md", "ok")


def test_remediation_cannot_write_diagnosis(home):
    ws = Workspace("dp1", home).create()
    with pytest.raises(WriteNotAllowed):
        ws.write("remediation", "cases/INC-001/diagnosis.md", "x")
    ws.write("remediation", "cases/INC-001/actions/ACT-001.yaml", "ok")


@pytest.mark.parametrize("identity,path,allowed", [
    ("onboarding", "service/monitoring-spec.yaml", True),
    ("onboarding", "service/tuning-proposals/T-1.md", False),
    ("sentinel", "service/tuning-proposals/T-1.md", True),
    ("sentinel", "service/monitoring-spec.yaml", False),
    ("commander", "cases/INC-001/case.yaml", True),
    ("commander", "cases/INC-001/diagnosis.md", False),
    ("collector", "signals/2026-01-05.jsonl", True),
    ("sentinel", "signals/2026-01-05.jsonl", False),
    ("knowledge", "control/autonomy-proposals/P-1.md", True),
    ("knowledge", "control/registry.yaml", False),
    ("finops", "governance/drift/GOV-001.md", False),
])
def test_allowlist_matrix(identity, path, allowed):
    assert may_write(identity, path) is allowed


def test_path_escape_rejected(home):
    ws = Workspace("dp1", home).create()
    with pytest.raises(WriteNotAllowed):
        ws.write("finops", "finops/../control/registry.yaml", "x")


# -- O1-3 / O2-5 -----------------------------------------------------------------------
def _inc(p):
    return p.cases.open("INC", by="commander", severity="S2", severity_rule="objective breached",
                        detecting_monitors=["MON-FRESH-mart.daily_consumption"])


def test_illegal_transition_has_specific_message(util):
    case = _inc(util)
    with pytest.raises(IllegalTransition) as exc:
        util.cases.transition(case.id, "executing", by="remediation")
    assert "detected -> executing" in str(exc.value) and "triaged" in str(exc.value)


def test_inc_needs_severity_rule(util):
    with pytest.raises(GuardViolation):
        util.cases.open("INC", by="commander", severity="S2")


def _to_verifying(p, case_id):
    for s in ("triaged", "diagnosing", "options-ready", "executing", "verifying"):
        p.cases.transition(case_id, s, by="test")


def test_two_failed_attempts_stop_and_escalate(util):
    """OT-13."""
    case = _inc(util)
    _to_verifying(util, case.id)
    util.cases.transition(case.id, "failed", by="remediation")
    util.cases.transition(case.id, "options-ready", by="remediation")
    util.cases.transition(case.id, "executing", by="remediation")
    util.cases.transition(case.id, "verifying", by="remediation")
    c = util.cases.transition(case.id, "failed", by="remediation")
    assert c.escalated and c.failed_attempts == 2
    util.cases.transition(case.id, "options-ready", by="remediation")
    with pytest.raises(GuardViolation, match="failed attempts"):
        util.cases.transition(case.id, "executing", by="remediation")
    assert any(e["case_id"] == case.id for e in escalations(util.ws))


def _recover(util):
    case = _inc(util)
    _to_verifying(util, case.id)
    with pytest.raises(GuardViolation, match="verification.md"):
        util.cases.transition(case.id, "recovered", by="remediation")
    util.ws.write("remediation", f"cases/{case.id}/verification.md", "verified")
    with pytest.raises(GuardViolation, match="lacks passing results"):
        util.cases.transition(case.id, "recovered", by="remediation")
    res = util.engine.run()["results"]
    rid = next(r["id"] for r in res if r["monitor_id"] == "MON-FRESH-mart.daily_consumption")
    c = util.cases.transition(case.id, "recovered", by="remediation", verification_results=[rid])
    assert c.state == "recovered"
    return case, rid


def test_recover_needs_verification_with_monitor_results(util):
    _recover(util)


def test_close_needs_post_incident_note(util):
    """OT-14."""
    case, _ = _recover(util)
    with pytest.raises(GuardViolation, match="post-incident"):
        util.cases.transition(case.id, "reviewed", by="knowledge")
    util.ws.write("knowledge", f"cases/{case.id}/post-incident.md", "note")
    util.cases.transition(case.id, "reviewed", by="knowledge")
    assert util.cases.transition(case.id, "closed", by="commander").state == "closed"


def test_close_rejected_when_monitor_disabled(util):
    case, _ = _recover(util)
    util.ws.write("knowledge", f"cases/{case.id}/post-incident.md", "note")
    util.cases.transition(case.id, "reviewed", by="knowledge")
    spec = util.ws.read_yaml("service/monitoring-spec.yaml")
    for m in spec["monitors"]:
        if m["id"] == "MON-FRESH-mart.daily_consumption":
            m["enabled"] = False
    util.ws.write_yaml("onboarding", "service/monitoring-spec.yaml", spec)
    with pytest.raises(GuardViolation, match="disabled"):
        util.cases.transition(case.id, "closed", by="commander")


def test_close_rejected_when_threshold_loosened(util):
    case, _ = _recover(util)
    util.ws.write("knowledge", f"cases/{case.id}/post-incident.md", "note")
    util.cases.transition(case.id, "reviewed", by="knowledge")
    spec = util.ws.read_yaml("service/monitoring-spec.yaml")
    for m in spec["monitors"]:
        if m["id"] == "MON-FRESH-mart.daily_consumption":
            m["rule"]["max_lag_minutes"] = 9999
    util.ws.write_yaml("onboarding", "service/monitoring-spec.yaml", spec)
    with pytest.raises(GuardViolation, match="changed since detection"):
        util.cases.transition(case.id, "closed", by="commander")


def test_fin_decided_needs_decision(util):
    c = util.cases.open("FIN", by="finops")
    util.cases.transition(c.id, "awaiting-decision", by="finops")
    with pytest.raises(GuardViolation):
        util.cases.transition(c.id, "decided", by="finops")
    assert util.cases.transition(c.id, "decided", by="finops", decision="deferred").decision == "deferred"


# -- O1-4 ------------------------------------------------------------------------------
GOOD_SIG = {"id": "SIG-000001", "signal_class": "freshness", "product": "dp", "object": "o",
            "observed": 210, "expected": 120, "evidence_path": "sim://x", "time": "2026-01-05T07:00:00Z"}


def test_valid_signal():
    validate_record("signal", GOOD_SIG)


@pytest.mark.parametrize("patch", [
    {"id": "SIG-1"},
    {"signal_class": "network"},
    {"observed": [{"meter_id": "M1", "kwh": 3}]},
    {"detail": "a|b|c|d\n1|2|3|4\n5|6|7|8\n9|1|2|3\n4|5|6|7"},
    {"unexpected": True},
    {"evidence_path": ""},
])
def test_invalid_signal_rejected(patch):
    with pytest.raises(ValidationFailed):
        validate_record("signal", {**GOOD_SIG, **patch})


def test_invalid_case_and_act_rejected():
    with pytest.raises(ValidationFailed):
        validate_record("case", {"id": "FIN-001", "type": "INC", "product": "dp", "state": "detected"})
    with pytest.raises(ValidationFailed):
        validate_record("action", {"id": "ACT-1", "case_id": "INC-001", "product": "dp",
                                   "action_type": "retry_failed_task", "target": "t", "requested_by": "x"})


# -- O1-5 ------------------------------------------------------------------------------
def test_pack_missing_idempotency_rejected_with_all_gaps(home):
    pack = copy.deepcopy(handover_pack("utilities"))
    del pack["pipelines"][0]["idempotent"]
    pack["pipelines"][1]["rollback_ref"] = ""
    pack["owners"]["data_owner"] = ""
    res = accept_handover(pack, home=home)
    assert not res.accepted
    assert "pipeline meter_ingest has no idempotency declaration" in res.gaps
    assert "pipeline outage_ingest has no rollback reference" in res.gaps
    assert "owners.data_owner is not named" in res.gaps
    assert not Workspace(pack["dp_id"], home).exists()


def test_same_person_service_and_platform_owner_rejected():
    pack = copy.deepcopy(handover_pack("telco"))
    pack["owners"]["service_owner"] = pack["owners"]["platform_owner"]
    assert any("same person" in g for g in receiver_check(pack))


def test_product_released_event_accepted(home):
    res = on_product_released({"type": "product.released", "handover_pack": handover_pack("telco")}, home=home)
    assert res.accepted and res.og0_request == "APR-001"
    assert res.workspace.has("control/service.yaml")
    assert res.workspace.read_yaml("control/registry.yaml")["levels"]["retry_failed_task"] == "L1"


def test_chainlog_detects_tampering(tmp_path):
    log = ChainLog(tmp_path / "l.jsonl")
    for i in range(3):
        log.append({"n": i})
    assert log.verify()
    text = (tmp_path / "l.jsonl").read_text().replace('"n": 1', '"n": 7')
    (tmp_path / "l.jsonl").write_text(text)
    assert not log.verify()
