"""End-to-end tests: every scenario night, the human checkpoints, and the API."""

from datetime import datetime, timedelta

import pytest

from operatewithai.agents.team import operating_model
from operatewithai.connectors.parsers import parse
from operatewithai.engine.ops import Refused
from operatewithai.env import Environment
from operatewithai.service import Service
from operatewithai.sim.scenarios import SCENARIOS

NOTES = "Killed leaked idle sessions on ds_snowflake_fin and raised maxActive to 30"


def night(ids, until="01:00"):
    svc = Service()
    svc.restart(ids)
    svc.run_until(until)
    return svc


def answer(svc, kinds=None, skip=("approve_request",), delay=0):
    """Every person answers their open asks with the first choice. Returns how many."""
    n_done = 0
    for n in svc.ops.notes():
        if n["status"] != "open" or (kinds and n["kind"] not in kinds) or n["kind"] in skip:
            continue
        if svc.now < datetime.fromisoformat(n["created"]) + timedelta(minutes=delay):
            continue
        a = n["actions"][0]
        svc.act(n["id"], n["escalated_to"] or n["to"], a["id"], NOTES if a.get("input") and not a.get("prefill") else "")
        n_done += 1
    return n_done


def run_with_people(ids, until="08:00", delay=10):
    svc = Service()
    svc.restart(ids)
    while svc.now.strftime("%H:%M") < until:
        svc.cycle()
        while answer(svc, delay=delay):
            pass
    return svc


def incidents(svc):
    return svc.ops.tickets("incident")


def ask(svc, kind, ticket=None):
    return next(n for n in svc.ops.notes() if n["kind"] == kind and (ticket is None or n["ticket"] == ticket))


# ------------------------------------------------------------------ the estate and connectors
def test_quiet_night_delivers_everything_and_opens_no_incident():
    svc = night([], "08:00")
    assert incidents(svc) == []
    assert all(s["status"] == "delivered" for s in svc.ops.projection["services"].values())


def test_lineage_impact_is_specific():
    env = Environment.load()
    assert env.services_hit_by(["mft.claims_file"]) == ["BS-CLAIMS"]
    assert env.services_hit_by(["ogg.rep_snf"]) == ["BS-FIN-CLOSE", "BS-REG-LIQ"]
    assert len(env.services_hit_by(["dbt.nightly_build"])) == 4


def test_connectors_parse_native_formats():
    env = Environment.load()
    alerts = parse(env, [
        ("goldengate_log", "2026-03-03T00:50:00.000-0500  ERROR   OGG-01296  Oracle GoldenGate Delivery for Oracle, rep_snf.prm:  Error mapping from A to B."),
        ("informatica", {"WORKFLOW_NAME": "wf_POLICY_DAILY_LOAD", "RUN_STATUS_CODE": 3, "RUN_ERR_MSG": "[RR_4035] SQL Error [ SQL0911N  deadlock ]"}),
        ("powerbi", {"datasetName": "Finance Daily Close", "status": "Failed",
                     "serviceExceptionJson": '{"errorCode":"DM_GWPipeline_Client_GatewayUnreachable","errorDescription":"x"}'}),
        ("cognos", {"COGIPF_REPORTNAME": "Claims Aging", "COGIPF_STATUS": "Failure", "COGIPF_ERRORDETAILS": "DPR-ERR-2109 The dispatcher cannot service the request"}),
    ])
    by = {a["source"]: a for a in alerts}
    assert by["goldengate"]["codes"] == ["OGG-01296"] and by["goldengate"]["facts"]["process"] == "REP_SNF"
    assert by["informatica"]["node"] == "infa.wf_policy" and "SQL0911N" in by["informatica"]["codes"]
    assert by["powerbi"]["node"] == "pbi.fin_close" and by["cognos"]["codes"] == ["DPR-ERR-2109"]


# ------------------------------------------------------------------ triage and diagnosis
def test_one_cause_seen_by_two_tools_is_one_incident():
    svc = night(["S03"], "01:35")
    (inc,) = incidents(svc)
    sources = {svc.store.need("event", e)["source"] for e in inc["events"]}
    assert {"oracle", "goldengate", "informatica"} <= sources
    assert inc["ke"] == "KE0003" and inc["diagnosis"]["status"] == "confirmed"
    assert inc["assignee"] == "Oracle DBA" and inc["priority"] == "P2"
    assert set(inc["services"]) == {"BS-FIN-CLOSE", "BS-REG-LIQ"}


def test_downstream_failures_join_the_causing_incident():
    svc = night(["S01"], "03:40")       # nobody responds: dbt skips the finance mart on a stale source
    (inc,) = incidents(svc)
    assert any("DBT_STALE_SOURCE" in svc.store.need("event", e)["codes"] for e in inc["events"])


def test_independent_causes_get_separate_incidents():
    svc = night(["S02", "S05"], "01:30")
    assert sorted(i["ke"] for i in incidents(svc)) == ["KE0002", "KE0005"]


def test_noise_is_reduced():
    svc = night(["S01"], "03:00")
    w = svc.store.kv("watch_stats")
    assert w["raw"] > 50 and w["events"] <= 4 and len(incidents(svc)) == 1


# ------------------------------------------------------------------ human in the loop
def test_nothing_changes_until_a_person_accepts_the_plan():
    svc = night(["S01"], "02:00")
    assert svc.world.actions == [] and svc.world.ogg["REP_SNF"]["status"] == "ABENDED"
    (inc,) = incidents(svc)
    assert inc["plan"]["status"] == "proposed"


def test_only_the_addressed_person_can_decide():
    svc = night(["S01"], "01:00")
    plan = ask(svc, "plan")
    with pytest.raises(Refused):
        svc.act(plan["id"], "Informatica Engineer", "accept")
    with pytest.raises(Refused):
        svc.act(plan["id"], "Mallory Nobody", "accept")
    svc.act(plan["id"], "GoldenGate Engineer", "accept")
    with pytest.raises(Refused):                      # already handled
        svc.act(plan["id"], "GoldenGate Engineer", "accept")


def test_approval_class_step_waits_for_the_named_approver():
    svc = night(["S01"], "01:00")
    svc.act(ask(svc, "plan")["id"], "GoldenGate Engineer", "accept")
    (inc,) = incidents(svc)
    assert inc["ack_at"] and inc["sla"]["respond_met"]
    assert svc.world.actions == []                    # accepted, but the DDL still needs the change manager
    appr = ask(svc, "approve")
    assert appr["to"] == "Change Manager"
    with pytest.raises(Refused):
        svc.act(appr["id"], "GoldenGate Engineer", "approve")
    svc.act(appr["id"], "Change Manager", "approve")
    assert [a["action"] for a in svc.world.actions] == ["snowflake.add_column_from_source", "goldengate.start_process"]
    assert svc.world.ogg["REP_SNF"]["status"] == "RUNNING"


def test_declined_approval_stops_the_plan_and_hands_back():
    svc = night(["S04"], "02:15")
    svc.act(ask(svc, "plan")["id"], "Snowflake and dbt Engineer", "accept")
    svc.act(ask(svc, "approve")["id"], "Service Delivery Manager", "reject", "Not this month")
    (inc,) = incidents(svc)
    assert inc["plan"]["status"] == "blocked" and svc.world.actions == []
    assert ask(svc, "blocked")["to"] == "Snowflake and dbt Engineer"


def test_unanswered_ask_escalates_to_incident_manager_then_sdm():
    svc = night(["S01"], "01:45")
    plan = ask(svc, "plan")
    assert plan["escalated_to"] in ("Incident Manager", "Service Delivery Manager")
    svc.run_until("02:30")
    assert ask(svc, "plan")["escalated_to"] == "Service Delivery Manager"
    svc.act(plan["id"], "Service Delivery Manager", "accept")    # the escalation recipient may act


def test_stakeholder_message_needs_the_incident_manager():
    svc = night(["S03"], "01:15")
    c = ask(svc, "comms")
    assert c["to"] == "Incident Manager"
    (inc,) = incidents(svc)
    assert inc["comms"][0]["status"] == "draft"
    svc.act(c["id"], "Incident Manager", "send", "Edited by the incident manager.")
    (inc,) = incidents(svc)
    assert inc["comms"][0]["status"] == "sent" and inc["comms"][0]["text"] == "Edited by the incident manager."
    assert any(n["to"] == "Client Service Owner" and n["kind"] == "fyi" for n in svc.ops.notes())


# ------------------------------------------------------------------ whole nights
@pytest.mark.parametrize("sid", [s["id"] for s in SCENARIOS])
def test_every_scenario_recovers_with_people_answering(sid):
    svc = run_with_people([sid])
    incs = incidents(svc)
    assert len(incs) == 1 and incs[0]["state"] == "Resolved", [(i["title"], i["state"]) for i in incs]
    assert all(s["status"] == "delivered" for s in svc.ops.projection["services"].values())
    assert svc.store.audit_verify()


def test_everything_at_once():
    svc = run_with_people(["S01", "S02", "S04", "S05", "S06", "S07", "S08"], delay=5)
    assert all(i["state"] == "Resolved" for i in incidents(svc)) and len(incidents(svc)) == 7
    assert all("delivered" in s["status"] for s in svc.ops.projection["services"].values())


def test_breach_is_predicted_and_priority_raised_when_nobody_acts():
    svc = night(["S01"], "06:00")
    (inc,) = incidents(svc)
    assert inc["priority"] == "P1" and inc["major"]
    assert svc.ops.projection["services"]["BS-FIN-CLOSE"]["status"] == "breach predicted"
    assert any(n["kind"] == "major" and n["to"] == "Incident Manager" for n in svc.ops.notes())


def test_slow_host_is_caught_before_anything_fails():
    svc = night(["S09"], "01:00")
    (inc,) = incidents(svc)
    assert inc["ke"] == "KE0008" and all(j["state"] != "failed" for j in svc.world.jobs.values())


# ------------------------------------------------------------------ learning, prevention, requests
def test_unknown_error_becomes_a_known_error_after_a_person_approves():
    svc = run_with_people(["S08"])
    assert any(k.get("learned_from") for k in svc.ops.kb.known_errors())
    svc.restart(["S08"])                              # the next night recognises it
    svc.run_until("04:10")
    (inc,) = incidents(svc)
    assert inc["ke"] and inc["ke"].startswith("KE02") and inc["plan"]["status"] == "proposed"


def test_recurring_known_error_opens_a_problem_for_the_sdm():
    svc = run_with_people(["S02"])
    (prb,) = svc.ops.tickets("problem")
    assert prb["ke"] == "KE0002" and prb["stage"] == "in backlog"
    assert incidents(svc)[0]["pir"]["status"] == "approved"


def test_preventive_rotation_stops_the_incident_from_happening():
    svc = night(["S03"], "00:40")
    req = ask(svc, "approve_request")
    assert req["to"] == "Security Lead"
    svc.act(req["id"], "Security Lead", "approve")
    svc.run_until("02:00")
    assert incidents(svc) == [] and svc.world.prevented == ["oracle.account_expired"]


def test_restricted_access_request_needs_owner_and_security():
    svc = night([], "01:00")
    t = svc.raise_request("SR-SNOW-ROLE", {"user": "jdoe", "role": "CLAIMS_PII_RO", "reason": "Fraud review"}, "Business user")
    asks = [n for n in svc.ops.notes() if n["ticket"] == t["id"]]
    assert {n["to"] for n in asks} == {"Claims Data Owner", "Security Lead"}
    svc.act(asks[0]["id"], asks[0]["to"], "approve")
    assert svc.world.grants == []
    svc.act(asks[1]["id"], asks[1]["to"], "approve")
    assert svc.world.grants[0]["role"] == "CLAIMS_PII_RO" and svc.store.need("ticket", t["id"])["state"] == "Resolved"


def test_report_rerun_waits_for_data():
    svc = night([], "01:00")
    t = svc.raise_request("SR-REPORT-RERUN", {"node": "pbi.policy_ops", "reason": "Missed it"}, "Business user")
    assert svc.store.need("ticket", t["id"])["stage"] == "waiting for data"
    svc.run_until("06:00")
    assert svc.store.need("ticket", t["id"])["state"] == "Resolved"


def test_llm_suggestion_is_optional_and_never_the_diagnosis():
    svc = night(["S08"], "04:10")
    (inc,) = incidents(svc)
    assert inc["diagnosis"]["status"] == "unknown" and inc["diagnosis"]["suggestion"] is None


# ------------------------------------------------------------------ documents and API
def test_operating_model_document_matches_the_code():
    from pathlib import Path
    doc = Path(__file__).parent.parent / "OPERATING_MODEL.md"
    assert doc.read_text() == operating_model(Environment.load()), "regenerate: python -m operatewithai operating-model > OPERATING_MODEL.md"


def test_api_and_console(monkeypatch):
    from fastapi.testclient import TestClient

    from operatewithai.api.app import create_app
    svc = night(["S03", "S05"], "01:20")
    c = TestClient(create_app(svc))
    assert c.get("/").status_code == 200 and c.get("/static/app.js").status_code == 200
    for path in ("/api/meta", "/api/overview", "/api/tickets", "/api/batch", "/api/signals", "/api/knowledge", "/api/team", "/api/audit",
                 "/api/reports/health", "/api/reports/handover", "/api/reports/review", "/api/inbox?person=Oracle%20DBA"):
        assert c.get(path).status_code == 200, path
    inc = next(t for t in c.get("/api/tickets?type=incident").json() if t["ke"] == "KE0003")
    detail = c.get(f"/api/tickets/{inc['id']}").json()
    assert detail["chains"] and detail["ticket"]["servicenow"]["table"] == "incident"
    plan = next(n for n in c.get("/api/inbox?person=Oracle%20DBA").json()["items"] if n["kind"] == "plan")
    assert c.post(f"/api/inbox/{plan['id']}", json={"action": "accept"}).status_code == 403                      # no identity
    assert c.post(f"/api/inbox/{plan['id']}", json={"action": "accept"}, headers={"X-Actor": "BI Engineer"}).status_code == 403
    assert c.post(f"/api/inbox/{plan['id']}", json={"action": "accept"}, headers={"X-Actor": "Oracle DBA"}).status_code == 200
    assert c.get("/api/tickets/NOPE").status_code == 404
    assert c.post("/api/sim/advance", json={"minutes": 10}).json()["now"].endswith("01:30:00")

    monkeypatch.setenv("ADEF_API_TOKEN", "s3cret")
    c2 = TestClient(create_app(svc))
    assert c2.get("/api/overview").status_code == 401 and c2.get("/api/health").status_code == 200
    assert c2.get("/api/overview", headers={"Authorization": "Bearer s3cret"}).status_code == 200
