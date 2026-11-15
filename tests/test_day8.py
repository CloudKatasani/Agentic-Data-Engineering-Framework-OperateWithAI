"""Day 8: O4-5 (FinOps), O4-6 (Governance), O4-7 (Problem and Knowledge).

Exit check: F-06 and F-07 found; replay added.
"""

import csv
import io
import json
from datetime import timedelta

import pytest

from adef_operate.connectors.simulator import SIM_NOW
from adef_operate.core.errors import GuardViolation
from adef_operate.core.hashing import text_hash
from adef_operate.engine.approvals import Approvals
from adef_operate.engine.council import Council
from adef_operate.engine.escalation import escalations
from adef_operate.engine.registry import Registry
from adef_operate.simulation import Scenario


@pytest.fixture
def u(home):
    return Scenario(home, "utilities")


@pytest.fixture
def t(home):
    return Scenario(home, "telco")


# -- O4-5 FinOps -----------------------------------------------------------------------------
@pytest.mark.parametrize("pack", ["utilities", "telco"])
def test_f06_found_with_saving_arithmetic(pack, home):
    sc = Scenario(home, pack)
    sc.sim.inject("F-06", pack)
    out = sc.tick()
    fin = out["reviews"]["finops"]
    assert fin["material"] and "full scan" in fin["driver"]
    f = fin["findings"][0]
    est = sc.pack["cost_estimate_daily"]
    excess = est * 2.5 - est
    assert f["monthly_high"] == pytest.approx(excess * 30)
    assert f["monthly_low"] == pytest.approx(excess * 30 * 0.8)
    md = sc.ws.read(f"finops/findings/{f['id']}.md")
    assert "## Saving arithmetic" in md and f"{excess:,.2f} x 30 days" in md
    assert "never by this agent" in md
    req = Approvals(sc.ws).get_request(f["approval"])
    assert req.gate == "OG4" and req.requested_by == "commander"
    assert sc.commander.cases.get(f["id"]).state == "awaiting-decision"
    assert sc.sim.state[sc.dp]["action_calls"] == []  # nothing resized or changed
    assert sc.ws.has(f"finops/{fin['period']}.md")


def test_finops_decision_and_realised_saving_in_ledger(u):
    u.sim.inject("F-06", "utilities")
    f = u.tick()["reviews"]["finops"]["findings"][0]
    baseline = u.sim.state[u.dp]["cost"]["daily"]
    u.approve(f["approval"])
    assert u.finops.record_decision(f["id"], f["approval"]) == "accepted"
    # After one cycle, the fix shipped through Build: cost falls back to normal.
    u.sim.state[u.dp]["cost"].update(daily=36.0, full_scans=0)
    realised = u.finops.measure_realised(f["id"], baseline_daily=baseline, period="2026-01-05/2026-02-04")
    assert realised == pytest.approx((100.0 - 36.0) * 30)
    row = next(r for r in u.finops.ledger() if r["finding"] == f["id"])
    assert row["decision"] == "accepted" and float(row["realised_monthly"]) == realised


def test_rejected_with_defer_reason_is_deferred(u):
    u.sim.inject("F-06", "utilities")
    f = u.tick()["reviews"]["finops"]["findings"][0]
    u.approve(f["approval"], decision="rejected", reason="Defer to next quarter")
    assert u.finops.record_decision(f["id"], f["approval"]) == "deferred"


def test_unused_needs_telemetry_usage_and_lineage(t):
    # Telemetry alone: no finding (OT-08 at the agent level).
    assert t.finops.unused_objects(None) == []
    # raw.* have no reads but have downstream lineage; leaves have reads: nothing is unused.
    assert t.finops.unused_objects({o: 0 for o in t.sim.state[t.dp]["object_reads"]}) == []
    t.sim.state[t.dp]["object_reads"]["mart.billing_summary"] = 0
    assert t.finops.unused_objects({"mart.billing_summary": 0}) == ["mart.billing_summary"]
    res = t.finops.review(usage_signals={"mart.billing_summary": 0})
    f = res["findings"][0]
    assert "A3 drop_object at OG4" in t.ws.read(f"finops/findings/{f['id']}.md")


def test_cost_spike_linked_to_open_incident(u):
    u.sim.inject("F-01", "utilities")
    u.tick()
    u.sim.inject("F-06", "utilities")
    f = u.tick(SIM_NOW.replace(minute=20))["reviews"]["finops"]["findings"][0]
    assert any(l.id.startswith("INC-") for l in u.commander.cases.get(f["id"]).linked_cases)


# -- O4-6 Governance -------------------------------------------------------------------------
@pytest.mark.parametrize("pack", ["utilities", "telco"])
def test_f07_found_and_no_action_taken(pack, home):
    sc = Scenario(home, pack)
    sc.sim.inject("F-07", pack)
    grants_before = list(sc.sim.state[sc.dp]["grants"])
    out = sc.tick()
    gov = out["reviews"]["governance"]
    assert gov["controls"]["grants"]["status"] == "drifted"
    g = gov["raised"][0]
    assert g["type"] == "Grant outside the model" and g["exposure"]
    md = sc.ws.read(f"governance/drift/{g['id']}.md")
    assert "role:temp_contractor SELECT raw.*" in md and "Consumer impact of the correction" in md
    assert Approvals(sc.ws).get_request(g["approval"]).gate == "OG5"
    assert any(e["to"] == "security_compliance" and g["id"] in e["reason"] for e in escalations(sc.ws))
    # No agent action: grants unchanged, nothing executed.
    assert sc.sim.state[sc.dp]["grants"] == grants_before
    assert sc.sim.state[sc.dp]["action_calls"] == []
    assert not sc.ws.has("control/action-log.jsonl") or all(
        e["record"].get("action_type") != "revoke_access"
        for e in sc.ws.chainlog("executor", "control/action-log.jsonl").entries())


def test_repeat_review_does_not_duplicate_findings(u):
    u.sim.inject("F-07", "utilities")
    u.tick()
    again = u.governance.review(at=SIM_NOW)
    assert again["raised"] == []


def test_masking_retention_classification_and_logging_drift(u):
    st = u.sim.state[u.dp]
    st["masking"].pop("raw.meter_interval_reads.meter_id")
    st["retention"]["raw.outage_events"] = 30
    st["catalog_objects"].append("scratch.tmp_extract")
    st["audit_logging"]["gaps"] = ["2026-01-04T02:00/03:00"]
    res = u.governance.review(at=SIM_NOW)
    types = {r["type"] for r in res["raised"]}
    assert types == {"Unmasked Restricted column", "Retention drift", "Unclassified object", "Logging gap"}
    assert next(r for r in res["raised"] if r["type"] == "Unmasked Restricted column")["exposure"]


def test_not_verifiable_is_a_finding(u):
    real = u.sim.governance_state
    u.sim.governance_state = lambda p: {k: v for k, v in real(p).items() if k != "masking"}
    res = u.governance.review(at=SIM_NOW)
    assert res["controls"]["masking"]["status"] == "not verifiable"
    assert any(r["type"] == "Not verifiable" for r in res["raised"])


def test_waiver_and_expired_waiver(u):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    u.sim.inject("F-07", "utilities")
    key = "grant:role:temp_contractor SELECT raw.*"
    appr = Approvals(u.ws)
    req = appr.request(gate="OG5", decision_asked="waiver: contractor read access for migration",
                       artifact_hash=text_hash(key), requested_by="commander", expires=now + timedelta(days=7))
    u.approve(req.id)
    assert u.governance.compare(at=now)["controls"]["grants"]["status"] == "waived"
    later = u.governance.compare(at=now + timedelta(days=8))
    assert any("waiver expired" in d["type"] for d in later["drifts"])


def test_correction_by_people_is_verified(u):
    u.sim.inject("F-07", "utilities")
    g = u.tick()["reviews"]["governance"]["raised"][0]
    u.approve(g["approval"])
    assert not u.governance.verify_correction(g["id"])  # not yet corrected
    u.sim.state[u.dp]["grants"].remove("role:temp_contractor SELECT raw.*")  # people revoke
    assert u.governance.verify_correction(g["id"])
    assert u.commander.cases.get(g["id"]).state == "resolved"


def test_evidence_pack_and_entitlements(u):
    u.sim.inject("F-07", "utilities")
    u.tick()
    pack = u.governance.evidence_pack("2026-01")
    assert "`control/approvals.jsonl`: verified" in pack and "not judged by this agent" in pack
    rows = list(csv.DictReader(io.StringIO(u.governance.entitlements("2026-01"))))
    assert next(r for r in rows if r["role"] == "role:temp_contractor")["recommendation"].startswith("candidate")


def test_governance_cannot_execute_corrections(u):
    from adef_operate.core.errors import WriteNotAllowed
    with pytest.raises(WriteNotAllowed):
        u.ws.write("governance", "control/registry.yaml", "x")


# -- O4-7 Problem and Knowledge --------------------------------------------------------------
def recovered_f02(sc):
    sc.sim.inject("F-02", "utilities")
    case_id = sc.tick()["outcomes"][0]["case"]
    sc.diagnosis.diagnose(case_id)
    for r in ("Reviewer A", "Reviewer B"):
        Council(sc.ws).record(case_id, reviewer=r, agrees=True)
    opts = sc.remediation.build_options(case_id)
    q = next(o for o in opts if o.action_type == "quarantine_batch")
    res = sc.remediation.act(case_id, q.no)
    sc.approve(res["approval"])
    sc.remediation.proceed(case_id, res["act"])
    sc.sim.run_action("quarantine_batch", sc.dp, {"batch_id": "B-20260105-06"})
    sc.remediation.confirm_executed_by_people(case_id, res["act"], by="Oncall Utilities")
    assert sc.remediation.verify(case_id)["recovered"]
    return case_id


def test_post_incident_note_replay_and_close(u):
    case_id = recovered_f02(u)
    u.knowledge.confirm_cause(case_id, cause_class="source data", by="Sam Service", note="source confirmed resend")
    note = u.knowledge.post_incident(case_id, fault_started_at=SIM_NOW - timedelta(minutes=5))
    assert "## Timeline (UTC)" in note and "first failing signal" in note
    assert "Confirmed by Sam Service: source data" in note and "compared with confirmed cause: match" in note
    assert "5 minutes from fault start to first signal" in note
    assert "| Dana Steward (data_steward) |" in note
    replay = json.loads(u.ws.read(f"evals/replays/{case_id}/scenario.json"))
    assert replay["label"] == "source data" and replay["confirmed"] and replay["used_for_scoring"]
    assert all(set(s) == {"signal_class", "object", "observed", "expected", "monitor_id", "time"} for s in replay["signals"])
    assert u.knowledge.accuracy() == {"scored": 1, "correct": 1, "accuracy": 1.0, "unconfirmed": 0}
    u.commander.close(case_id)
    assert u.commander.cases.get(case_id).state == "closed"


def test_unconfirmed_cause_is_not_scored(u):
    case_id = recovered_f02(u)
    note = u.knowledge.post_incident(case_id)
    assert "UNCONFIRMED" in note
    assert u.knowledge.accuracy()["unconfirmed"] == 1 and u.knowledge.accuracy()["scored"] == 0


def test_post_incident_needs_recovery_and_named_confirmer(u):
    u.sim.inject("F-01", "utilities")
    case_id = u.tick()["outcomes"][0]["case"]
    with pytest.raises(GuardViolation, match="verified recovery"):
        u.knowledge.post_incident(case_id)
    with pytest.raises(GuardViolation, match="named person"):
        u.knowledge.confirm_cause(case_id, cause_class="upstream outage", by="knowledge")


def test_problem_opened_when_object_fails_three_times(u):
    cs = u.commander.cases
    ids = []
    for _ in range(3):
        c = cs.open("INC", by="commander", severity="S3", severity_rule="test")
        cs.update(c.id, by="commander", note="x", first_failing_object="raw.meter_interval_reads")
        ids.append(c.id)
    prb = u.knowledge.check_problems(ids[-1])
    assert prb and u.ws.has(f"problems/{prb}.md")
    assert {l.id for l in cs.get(prb).linked_cases} == set(ids)
    assert u.knowledge.check_problems(ids[-1]) == prb  # no duplicate problem


def test_autonomy_proposal_from_platform_track_record(u):
    reg, appr = Registry(u.ws), Approvals(u.ws)
    from adef_operate.core.hashing import canonical_hash
    prop = canonical_hash({"action_type": "rerun_latest_window", "level": "L2"})
    req = appr.request(gate="OG7", decision_asked="L2", artifact_hash=prop, requested_by="knowledge")
    u.approve(req.id)
    reg.set_level("rerun_latest_window", "L2", approval_id=req.id, approvals=appr, artifact_hash=prop)
    from adef_operate.engine.executor import Executor, act_hash
    from adef_operate.schemas import ActionRecord
    ex = Executor(u.ws, u.sim)
    case = u.commander.cases.open("INC", by="commander", severity="S3", severity_rule="test")
    svc = u.ws.read_yaml("control/service.yaml")
    svc["budgets"]["actions"] = 100
    u.ws.write_yaml("commander", "control/service.yaml", svc)
    for i in range(5):
        a = ActionRecord(id=f"ACT-{100 + i}", case_id=case.id, product=u.dp, action_type="rerun_latest_window",
                         pipeline="meter_ingest" if i % 2 == 0 else "outage_ingest",
                         target=f"obj-{i}", params={"window": "2026-01-05"}, requested_by="remediation")
        r = appr.request(gate="OG2", decision_asked="rerun", artifact_hash=act_hash(a), requested_by="commander")
        u.approve(r.id)
        a = a.model_copy(update={"approval_id": r.id})
        ex.execute(a)
        ex.record_verification(a, verified=True)
    props = u.knowledge.propose_autonomy()
    p = next(x for x in props if x["action_type"] == "rerun_latest_window")
    assert (p["from"], p["to"]) == ("L2", "L3")
    assert appr.get_request(p["approval"]).gate == "OG7"
    # The proposal is only a proposal: the level is unchanged until OG7 is decided.
    assert reg.stored_level("rerun_latest_window").name == "L2"
    u.approve(p["approval"])
    reg.set_level("rerun_latest_window", "L3", approval_id=p["approval"], approvals=appr,
                  artifact_hash=canonical_hash({"action_type": "rerun_latest_window", "level": "L3"}))
    assert reg.stored_level("rerun_latest_window").name == "L3"


def test_runbook_change_is_a_proposal_with_current_hash(u):
    case_id = recovered_f02(u)
    before = u.ws.read("service/runbook.md")
    p = u.knowledge.propose_runbook_change(step="Check batch checksums before quarantine", reason_case=case_id)
    assert "Replaces runbook with hash" in u.ws.read(f"knowledge/runbook-changes/{p['id']}.md")
    assert u.ws.read("service/runbook.md") == before
    assert Approvals(u.ws).get_request(p["approval"]).gate == "OG7"


def test_monthly_review(u):
    case_id = recovered_f02(u)
    u.knowledge.confirm_cause(case_id, cause_class="source data", by="Sam Service")
    u.knowledge.post_incident(case_id)
    text = u.knowledge.monthly_review("2026-01")
    assert "- S1: 1" in text and "- source data: 1" in text and "1 of 1 correct" in text
