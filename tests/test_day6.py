"""Day 6: O4-1 (Commander and Onboarding to OG1) and O4-2 (Sentinel).

Exit check: OG1 reached; one group per fault.
"""

import copy

import pytest

from adef_operate.agents import CommanderAgent, OnboardingAgent
from adef_operate.connectors.simulator import FAULTS, SIM_NOW, SimulatorAdapter
from adef_operate.core.errors import GuardViolation
from adef_operate.engine.approvals import Approvals
from adef_operate.engine.escalation import escalations
from adef_operate.engine.onboarding_tools import check_draft, draft_service
from adef_operate.engine.registry import Registry
from adef_operate.engine.severity import assess
from adef_operate.packs import handover_pack
from adef_operate.runtime import OpsRuntime

DAY = SIM_NOW.date().isoformat()


def approve(ws, request_id, owners):
    a = Approvals(ws)
    r = a.get_request(request_id)
    for role in r.approver_roles:
        a.decide(request_id, approver=owners[role], role=role, decision="approved",
                 artifact_hash=r.artifact_hash)


class Env:
    def __init__(self, home, pack_name):
        self.sim = SimulatorAdapter()
        self.rt = OpsRuntime(home, self.sim)
        self.pack = handover_pack(pack_name)
        self.pack_name = pack_name
        self.dp = self.pack["dp_id"]
        res = self.rt.release({"type": "product.released", "handover_pack": self.pack})
        assert res.accepted
        self.ws = res.workspace
        approve(self.ws, res.og0_request, self.pack["owners"])
        self.onboarding = self.rt.onboard(self.dp)
        approve(self.ws, self.onboarding.og1_request, self.pack["owners"])

    def tick(self, at=SIM_NOW):
        return self.rt.tick(self.dp, at)

    @property
    def commander(self):
        return CommanderAgent(self.ws, self.sim, self.rt.pager)


@pytest.fixture
def u(home):
    return Env(home, "utilities")


@pytest.fixture
def t(home):
    return Env(home, "telco")


# -- O4-1 --------------------------------------------------------------------------------
def test_released_golden_product_becomes_operable(u):
    ob = u.onboarding
    assert ob.ready and ob.og1_request
    assert Approvals(u.ws).og1_status()[0]
    assert ob.report["history_days"] == 28
    assert "expected_alerts_per_week" in ob.report
    text = u.ws.read("service/readiness-report.md")
    assert "Dry run over 28 days" in text and "Ready for OG1: yes" in text
    # A healthy tick produces nothing.
    out = u.tick()
    assert out["signals"] == [] and out["outcomes"] == []


def test_onboarding_refuses_before_og0(home):
    rt = OpsRuntime(home, SimulatorAdapter())
    res = rt.release({"type": "product.released", "handover_pack": handover_pack("telco")})
    with pytest.raises(GuardViolation, match="OG0"):
        OnboardingAgent(res.workspace).onboard()


def test_dry_run_alert_volume_is_deterministic(home):
    a = Env(home / "a", "telco").onboarding.report["history_alerts"]
    b = Env(home / "b", "telco").onboarding.report["history_alerts"]
    assert a == b and sum(a.values()) > 0


def test_incomplete_pack_rejected_once_with_all_gaps(home):
    pack = copy.deepcopy(handover_pack("utilities"))
    for p in pack["pipelines"]:
        p.pop("idempotent")
    res = OpsRuntime(home, SimulatorAdapter()).release({"type": "product.released", "handover_pack": pack})
    assert not res.accepted and len([g for g in res.gaps if "idempotency" in g]) == 2


def test_onboarding_rules_objective_without_monitor():
    draft = draft_service(handover_pack("utilities"))
    draft["monitoring_spec"]["monitors"] = [m for m in draft["monitoring_spec"]["monitors"]
                                            if m.get("objective") != "OBJ-3"]
    assert "objective OBJ-3 has no monitor" in check_draft(draft)


def test_non_idempotent_pipeline_gets_no_executable_rerun():
    draft = draft_service(handover_pack("telco"))
    reruns = [b for b in draft["action_bindings"]["bindings"]
              if b["pipeline"] == "usage_ingest" and b["action_type"] in ("retry_failed_task", "rerun_latest_window")]
    assert reruns and not any(b["executable"] for b in reruns)
    assert check_draft(draft) == []


# -- O4-2 --------------------------------------------------------------------------------
@pytest.mark.parametrize("fault_id,pack", [(f, p) for f, fault in sorted(FAULTS.items()) for p in fault.packs])
def test_one_group_per_fault(fault_id, pack, home):
    env = Env(home, pack)
    env.sim.inject(fault_id, pack)
    out = env.tick()
    assert len(out["groups"]) == 1, [g["objects"] for g in out["groups"]]
    g = out["groups"][0]
    assert sorted(g["signals"]) == sorted(s["id"] for s in out["signals"])  # nothing dropped
    assert g["first_failing_object"] == FAULTS[fault_id].first_failing[pack]


def test_f03_one_group_not_one_per_downstream_table(t):
    t.sim.inject("F-03", "telco")
    out = t.tick()
    assert len(out["signals"]) == 3 and len(out["groups"]) == 1
    g = out["groups"][0]
    labels = {s["object"]: g["labels"][s["id"]]["label"] for s in out["signals"]}
    assert labels == {"bronze.usage_records": "primary", "silver.usage_by_region": "downstream-echo",
                      "gold.usage_daily": "downstream-echo"}
    assert any(e["kind"] == "upstream_notice" for e in g["known_events"])
    assert len(out["outcomes"]) == 1 and out["outcomes"][0]["outcome"] == "opened"


def test_schema_drift_heads_its_own_group_and_absorbs_echoes(t):
    t.sim.inject("F-04", "telco")
    g = t.tick()["groups"][0]
    assert g["standalone"] and set(g["classes"]) == {"schema", "run"}


def test_repeat_tick_labels_duplicates_and_attaches(u):
    u.sim.inject("F-01", "utilities")
    first = u.tick()
    case_id = first["outcomes"][0]["case"]
    second = u.tick(SIM_NOW.replace(minute=15))
    assert [g["id"] for g in second["groups"]] == [first["groups"][0]["id"]]
    g = second["groups"][0]
    assert sum(1 for l in g["labels"].values() if l["label"] == "duplicate") == len(second["signals"])
    assert second["outcomes"] == [{"group": g["id"], "outcome": "attached", "case": case_id}]
    case = u.commander.cases.get(case_id)
    assert len(case.signals) == 8 and len(u.commander.cases.all()) == 1


def test_unrelated_faults_make_separate_groups_and_cases(u):
    u.sim.inject("F-01", "utilities")
    u.sim.inject("F-02", "utilities")
    out = u.tick()
    assert len(out["groups"]) == 2
    assert {o["outcome"] for o in out["outcomes"]} == {"opened"}


def test_quality_failure_stays_separate_and_cases_are_linked(u):
    u.sim.inject("F-01", "utilities")
    u.sim.inject("F-05", "utilities")
    out = u.tick()
    assert len(out["groups"]) == 2
    q = next(g for g in out["groups"] if g["standalone"])
    assert q["classes"] == ["quality"]
    assert any(e["kind"] == "release" for e in q["known_events"])
    cases = u.commander.cases.all()
    assert len(cases) == 2
    assert any(l.reason.startswith("shared lineage neighbourhood") for c in cases for l in c.linked_cases)


# -- Commander ---------------------------------------------------------------------------
def test_case_opening_cites_rule_sets_trust_and_pages(u):
    u.sim.inject("F-01", "utilities")
    o = u.tick()["outcomes"][0]
    assert o["severity"] == "S2" and o["rule"].startswith("S2:")
    assert o["trust_status"] == "degraded"
    case = u.commander.cases.get(o["case"])
    assert case.state == "triaged" and case.owner == "adef-ops-incident-diagnosis"
    assert case.first_failing_object == "bronze.meter_interval_reads"
    assert u.rt.pager.sent[-1]["to"] == "Oncall Utilities"
    assert u.sim.state[u.dp]["trust_status"] == "degraded"


def test_s1_requests_council_and_containment(t):
    t.sim.inject("F-05", "telco")
    o = t.tick()["outcomes"][0]
    case = t.commander.cases.get(o["case"])
    assert case.severity == "S1" and case.trust_status == "do-not-use"
    assert any("council" in q for q in case.open_questions)
    assert "contain" in case.next_action


def test_domain_pack_raises_but_never_lowers(t):
    t.sim.inject("F-08", "telco")
    o = t.tick()["outcomes"][0]
    assert o["severity"] == "S2" and "raised by Domain Pack" in o["rule"]
    sd = t.ws.read_yaml("service/service-definition.yaml")
    group = {"objects": ["gold.billing_summary"]}
    sig = [{"signal_class": "quality", "object": "gold.billing_summary", "monitor_id": "x"}]
    lowering = {"severity_overrides": [{"match": {"object_tag": "billing"}, "min_severity": "S4"}]}
    a = assess(group, sig, sd, lowering, {}, {"gold.billing_summary": ["billing"]})
    assert a.severity == "S1" and a.override is None


def test_access_drift_escalated_not_actioned(u):
    u.sim.inject("F-07", "utilities")
    o = u.tick()["outcomes"][0]
    assert o["outcome"] == "routed" and o["routed_to"] == "adef-ops-governance-compliance"
    assert any(e["to"] == "security_compliance" for e in escalations(u.ws))
    assert [c.type for c in u.commander.cases.all()] == ["GOV"]  # Governance's finding; no INC
    assert u.sim.state[u.dp]["action_calls"] == []


def test_cost_routed_to_finops(t):
    t.sim.inject("F-06", "telco")
    o = t.tick()["outcomes"][0]
    assert o["routed_to"] == "adef-ops-finops-performance"


def test_s4_noise_is_batched_not_opened(u):
    u.sim.state[u.dp]["runs"]["meter_ingest"]["duration_min"] = 45.0
    o = u.tick()["outcomes"][0]
    assert o["outcome"] == "s4-batched"
    assert u.ws.has(f"health/s4-batch-{DAY}.jsonl")


def test_commander_routes_nothing_when_stopped(u):
    Registry(u.ws).stop(by="Pat Platform", reason="drill")
    u.sim.inject("F-01", "utilities")
    out = u.tick()
    assert out["outcomes"][0]["outcome"] == "stopped" and u.commander.cases.all() == []


def test_health_summary(u):
    u.sim.inject("F-01", "utilities")
    u.tick()
    text = u.commander.publish_health(DAY, now=SIM_NOW)
    assert "INC-001 INC S2 [triaged]" in text and "Signals simulated: yes" in text
    assert u.ws.has(f"health/{DAY}.md")
