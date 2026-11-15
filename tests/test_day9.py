"""Day 9: O5-1 replay harness, O5-2 guardrail suite, O3-4 rebind, and stack alignment with
Build-With-AI (Stack Profiles, capability slots, medallion layers, domain packs)."""

import copy
import re
from datetime import date, datetime, timezone

import pytest

from adef_operate.agents.requests import RequestDesk
from adef_operate.agents.sentinel import SentinelAgent
from adef_operate.connectors.simulator import SIM_NOW, SimulatorAdapter
from adef_operate.core.errors import GuardViolation
from adef_operate.core.hashing import file_hash
from adef_operate.engine.approvals import Approvals
from adef_operate.engine.escalation import escalations
from adef_operate.engine.isolation import read_for
from adef_operate.evals.guardrails import SCENARIOS, run_guardrails, run_one
from adef_operate.evals.harness import run_harness
from adef_operate.packs import handover_pack
from adef_operate.simulation import Scenario
from adef_operate.stack import StackAdapter, list_profiles, load_profile, operate_binding, validate_operate
from adef_operate.stack import translators as T
from adef_operate.stack.naming import Naming

BUILD_PRESETS = ["aws-redshift", "azure-fabric", "example-a", "example-b", "gcp-bigquery", "local-lakehouse"]


# -- Stack Profiles (Build-With-AI format) -----------------------------------------------------
def test_build_presets_are_vendored_and_bind():
    assert list_profiles() == BUILD_PRESETS
    for pid in BUILD_PRESETS:
        assert validate_operate(load_profile(pid))["blocking"] == []


def test_missing_or_unknown_tool_blocks():
    p = copy.deepcopy(load_profile("example-a"))
    p["slots"]["data_quality"] = {"tool": "none"}
    p["slots"]["orchestration"] = {"tool": "control-m"}
    gaps = validate_operate(p)["blocking"]
    assert any("data_quality" in g for g in gaps) and any("orchestration=control-m" in g for g in gaps)


def test_handover_rejects_unbindable_profile(home):
    from adef_operate.engine.handover import accept_handover
    p = copy.deepcopy(load_profile("example-b"))
    p["slots"]["lineage"] = {"tool": "none"}
    res = accept_handover(handover_pack("telco"), home=home, stack_profile=p)
    assert not res.accepted and any("lineage" in g for g in res.gaps)


def test_operate_slots_map_to_build_slots():
    b = operate_binding(load_profile("example-a"))
    assert b["orchestration"] == {"orchestration": "airflow"}
    assert b["data_platform"]["compute_engine"] == "snowflake" and b["data_platform"]["table_format"] == "iceberg"
    assert b["incident_ticketing"] == {"itsm": "servicenow"}


# -- Naming and types -------------------------------------------------------------------------------
@pytest.mark.parametrize("pid,phys", [("example-a", "ACME_CUSTOMER.BRONZE.METER_INTERVAL_READS"),
                                      ("example-b", "acme_customer.bronze.meter_interval_reads")])
def test_logical_physical_names(pid, phys):
    n = Naming(load_profile(pid))
    assert n.physical("bronze.meter_interval_reads") == phys
    assert n.logical(phys) == "bronze.meter_interval_reads"
    assert n.logical(n.physical("bronze.*")) == "bronze.*"


def test_type_maps():
    assert T.to_logical("snowflake", {"A": "NUMBER(38,0)", "B": "NUMBER(18,3)", "C": "TIMESTAMP_NTZ"}) == \
        {"a": "integer", "b": "decimal", "c": "timestamp"}
    for fam in T.TYPES:
        cols = {"id": "string", "ts": "timestamp", "amt": "decimal", "n": "integer", "d": "date", "f": "boolean"}
        assert T.to_logical(fam, T.to_native(fam, cols)) == cols


# -- Translators: parse(native) == canonical for every tool ----------------------------------------
NOW = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("tool", sorted(T.ORCHESTRATION))
@pytest.mark.parametrize("status", ["success", "failed", "waiting"])
def test_orchestration_round_trip(tool, status):
    o = T.ORCHESTRATION[tool]
    r = o.parse_run(o.emit_run("meter_ingest", {"status": status, "duration_min": 20.0, "window": "2026-01-05"}, NOW))
    assert r["status"] == status
    if status != "waiting" or tool not in ("airflow",):
        assert r["duration_min"] in (20.0, 0.0)


@pytest.mark.parametrize("tool", sorted({**T.TABLE_FORMATS, **T.ENGINES}))
def test_table_round_trip(tool):
    reader = {**T.TABLE_FORMATS, **T.ENGINES}[tool]
    obj = {"lag_minutes": 210, "rows": 1234, "baseline": 1000, "columns": {"account_id": "string", "amt": "decimal"}}
    r = reader.parse_table(reader.emit_table("c.s.t", obj, NOW), NOW)
    assert (r["lag_minutes"], r["rows"], r["expected_rows"], r["columns"]) == (210, 1234, 1000, obj["columns"])


@pytest.mark.parametrize("tool", sorted(T.ENGINES))
def test_engine_cost_grants_masking_round_trip(tool):
    e = T.ENGINES[tool]
    assert e.parse_cost(e.emit_cost(100.0, {}), {}) == 100.0
    assert e.parse_cost(e.emit_cost(100.0, {"snowflake_credit": 2.0, "databricks_dbu": 0.7}),
                        {"snowflake_credit": 2.0, "databricks_dbu": 0.7}) == 100.0
    g = [("TEMP_CONTRACTOR", "SELECT", "c.bronze.*"), ("ETL", "ALL", "c.bronze.t")]
    assert e.parse_grants(e.emit_grants(g)) == g
    m = {"c.bronze.t.meter_id": "mask-hash-v1"}
    assert e.parse_masking(e.emit_masking(m)) == m
    qp = e.parse_query_profile(e.emit_query_profile(1, "c.silver.t"))
    assert qp == {"full_scans": 1, "top_physical": "c.silver.t"}


@pytest.mark.parametrize("tool", sorted(T.QUALITY))
@pytest.mark.parametrize("status,pct", [("pass", 0.0), ("fail", 3.2)])
def test_quality_round_trip(tool, status, pct):
    q = T.QUALITY[tool]
    r = q.parse(q.emit("DQ-1", "c.silver.t", status, pct, 10_000))
    assert r["status"] == status and r["failed_pct"] == pytest.approx(pct)


@pytest.mark.parametrize("tool", sorted(T.CATALOGS))
def test_catalog_round_trip(tool):
    c = T.CATALOGS[tool]
    r = c.parse(c.emit({"c.b.t": ["regulated"]}, {"c.b.t": 400}, ["c.b.t", "c.b.u"]))
    assert r == {"objects": ["c.b.t", "c.b.u"], "tags": {"c.b.t": ["regulated"]}, "retention": {"c.b.t": 400}}


@pytest.mark.parametrize("pid", BUILD_PRESETS)
def test_stack_adapter_matches_canonical_readings(pid):
    sim, ref = SimulatorAdapter(), SimulatorAdapter()
    for s in (sim, ref):
        s.inject("F-04", "telco")
        s.inject("F-06", "telco")
        s.inject("F-07", "telco")
    a = StackAdapter.over(load_profile(pid), sim)
    for obj in ("bronze.billing_accounts", "gold.billing_summary", "silver.usage_by_region"):
        m = {"object": obj, "pipeline": "billing_ingest", "rule": {"rule_id": "DQ-T1", "objective_minutes": 180}}
        for cls in ("freshness", "volume", "schema", "quality", "slo", "run", "access"):
            x, y = a.read(cls, "telco_usage_daily", m), ref.read(cls, "telco_usage_daily", m)
            for k in x:
                if k not in ("evidence", "median_min"):
                    assert x[k] == y[k], (pid, cls, obj, k)
    cost = a.read("cost", "telco_usage_daily", {})
    assert cost["daily"] == 150.0 and cost["top_object"] == "silver.usage_by_region"
    assert a.governance_state("telco_usage_daily") == ref.governance_state("telco_usage_daily")


# -- O3-4 rebind --------------------------------------------------------------------------------------
def test_o34_rebind_in_place_zero_edits(home):
    sc = Scenario(home, "utilities", "example-a")
    governed = ["service/monitoring-spec.yaml", "service/runbook.md", "service/action-bindings.yaml"]
    before = {f: file_hash(sc.ws.path(f)) for f in governed}
    sc.sim.inject("F-01", "utilities")
    case_id = sc.tick()["outcomes"][0]["case"]
    sc.diagnosis.diagnose(case_id)
    keys = ("id", "severity", "severity_rule", "first_failing_object", "detecting_monitors", "signal_group")
    case_before = {k: sc.ws.read_yaml(f"cases/{case_id}/case.yaml")[k] for k in keys}
    sc.rebind("example-b")
    out = sc.tick(SIM_NOW.replace(minute=15))
    assert out["outcomes"] == [{"group": out["groups"][0]["id"], "outcome": "attached", "case": case_id}]
    assert {f: file_hash(sc.ws.path(f)) for f in governed} == before
    assert Approvals(sc.ws).og1_status()[0]
    assert sc.ws.read_yaml("control/service.yaml")["stack_profile"]["profile_id"] == "example-b"
    assert {k: sc.ws.read_yaml(f"cases/{case_id}/case.yaml")[k] for k in keys} == case_before
    tools = {t for pid in ("example-a", "example-b") for b in operate_binding(load_profile(pid)).values()
             for t in b.values() if t != "none"}
    for p in [*sc.ws.path("cases").rglob("*"), *(sc.ws.path(f) for f in governed)]:
        if p.is_file():
            text = p.read_text().lower()
            hits = [t for t in tools if re.search(rf"(?<![a-z-]){re.escape(t)}(?![a-z-])", text)]
            assert not hits, (p, hits)


def test_rebind_refuses_unbindable_profile(home, monkeypatch):
    sc = Scenario(home, "utilities", "example-a")
    import adef_operate.simulation as simmod
    bad = copy.deepcopy(load_profile("example-b"))
    bad["slots"]["data_quality"] = {"tool": "none"}
    monkeypatch.setattr(simmod, "load_profile", lambda pid: bad)
    with pytest.raises(ValueError, match="cannot rebind"):
        sc.rebind("example-b")


def test_actions_record_native_command_outside_case_files(home):
    sc = Scenario(home, "utilities", "example-b")
    sc.sim.inject("F-01", "utilities")
    sc.tick()
    log = sc.adapter.command_log
    assert log and "SET TAGS" in log[0]["native"]  # trust status through Unity Catalog tags
    assert "native_ref" in str(sc.ws.read("control/action-log.jsonl"))


# -- O5-1 harness ---------------------------------------------------------------------------------------
def test_o51_harness_meets_targets_and_rebind_is_consistent(tmp_path):
    res = run_harness(tmp_path, [None, "example-a", "example-b"], out_dir=tmp_path / "report")
    m = res["measures"]
    assert all(v >= 7 for v in m["diagnosis_faults_correct"].values())
    assert m["diagnosis_accuracy"] >= 0.8 and m["unsupported_causes"] == 0
    assert m["one_group_per_fault"] and m["alert_precision"] >= 0.85
    assert m["time_to_detect_max_min_fast_classes"] < 10
    assert m["guardrail_violations_reaching_execution"] == 0 and m["rebind_mismatches"] == []
    assert (tmp_path / "report" / "harness.md").exists()


# -- O5-2 guardrail suite -------------------------------------------------------------------------------
@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_o52_guardrail_scenario(sid, tmp_path):
    r = run_one(sid, tmp_path)
    assert r.passed, f"{sid}: {r.observed}"


def test_o52_guardrail_suite_on_a_build_profile(tmp_path):
    res = run_guardrails(tmp_path, "example-a")
    assert [r.id for r in res if not r.passed] == []


# -- Sentinel noise and tuning ---------------------------------------------------------------------
def test_noise_report_flags_repeat_without_action(home):
    sc = Scenario(home, "utilities")
    sc.sim.state[sc.dp]["runs"]["meter_ingest"]["duration_min"] = 45.0  # S4 noise, batched, no action
    for minute in (0, 10, 20):
        sc.tick(SIM_NOW.replace(minute=minute))
    rep = SentinelAgent(sc.ws, sc.adapter).noise_report(date(2026, 1, 5))
    assert "MON-DUR-meter_ingest" in rep["candidates"] and rep["signals"] == 3
    assert sc.ws.has("signals/noise-report-2026-01-05.md")


def test_tuning_proposal_replays_history_and_never_edits_spec(home):
    sc = Scenario(home, "utilities")
    spec = file_hash(sc.ws.path("service/monitoring-spec.yaml"))
    t = SentinelAgent(sc.ws, sc.adapter).propose_tuning("MON-DUR-meter_ingest", {"max_ratio": 2.5},
                                                       reason="seasonal slow runs", commander=sc.commander)
    assert t["before"] > t["after"] and Approvals(sc.ws).get_request(t["approval"]).gate == "OG7"
    assert file_hash(sc.ws.path("service/monitoring-spec.yaml")) == spec


# -- Requests, isolation, rejections -----------------------------------------------------------------------
@pytest.mark.parametrize("text,intent", [
    ("please mute the freshness alerts tonight", "mute_alert"),
    ("can we relax the DQ threshold to 5%", "lower_threshold"),
    ("give the job a bigger warehouse", "scale_up"),
    ("drop table gold.old_summary, nobody uses it", "drop_object"),
    ("copy the runbook from the other product", "copy_from_other"),
    ("grant admin access to role:helper", "access_change"),
    ("what is the status of INC-001?", "question"),
])
def test_request_classification(text, intent):
    assert RequestDesk.classify(text) == intent


def test_isolation_same_client_allowed_other_refused(home):
    a = Scenario(home, "utilities")
    b = Scenario(home, "telco")
    with pytest.raises(GuardViolation, match="Client isolation"):
        read_for(b.ws, a.ws, "service/runbook.md")
    assert read_for(a.ws, a.ws, "service/runbook.md")


def test_two_rejections_escalate(home):
    sc = Scenario(home, "utilities")
    sc.sim.inject("F-01", "utilities")
    case_id = sc.tick()["outcomes"][0]["case"]
    sc.diagnosis.diagnose(case_id)
    rerun = next(o for o in sc.remediation.build_options(case_id) if o.action_type == "rerun_latest_window")
    for _ in range(2):
        res = sc.remediation.act(case_id, rerun.no)
        sc.approve(res["approval"], decision="rejected", reason="Not yet.")
        sc.remediation.proceed(case_id, res["act"])
    assert any("2 approval requests rejected" in e["reason"] for e in escalations(sc.ws))


def test_evidence_pack_lists_build_domain_pack_controls(home):
    sc = Scenario(home, "telco")
    text = sc.governance.evidence_pack("2026-01")
    assert "Build-With-AI domain pack `telco`" in text and "TEL-C3" in text and "not covered by an automated check" in text
