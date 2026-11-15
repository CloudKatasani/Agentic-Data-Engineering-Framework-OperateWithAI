"""Day 10: O5-3 shadow mode (read-only, from exports) and the O5-4 go or no-go rule."""

import copy
import json
import shutil

import pytest
import yaml

from adef_operate.connectors.simulator import SIM_NOW, SimulatorAdapter
from adef_operate.core.autonomy import Level
from adef_operate.core.errors import AdefError, ApprovalError, ExecutorBlocked, GuardViolation
from adef_operate.core.hashing import canonical_hash
from adef_operate.engine import shadow
from adef_operate.engine.approvals import Approvals
from adef_operate.engine.executor import Executor
from adef_operate.engine.registry import Registry
from adef_operate.engine.workspace import Workspace
from adef_operate.packs import handover_pack
from adef_operate.schemas import ActionRecord
from adef_operate.shadow_mode import ShadowRuntime
from adef_operate.shadow_rehearsal import rehearsal_answers, run_rehearsal
from adef_operate.stack import NativePlatform, load_profile
from adef_operate.stack.export import ExportAdapter, export_snapshot


@pytest.fixture(scope="module")
def rehearsal(tmp_path_factory):
    return run_rehearsal(tmp_path_factory.mktemp("rehearsal"), "example-a")


# -- START HERE -------------------------------------------------------------------------------------
def test_template_is_blocked_until_answered():
    pack = handover_pack("utilities")
    gaps = shadow.validate_start_here(yaml.safe_load(shadow.start_here_template(pack["dp_id"], "example-a")), pack,
                                      load_profile("example-a"))["blocking"]
    assert any("no identity named" in g for g in gaps) and any("baseline" in g for g in gaps)
    assert any("suite_assumptions_confirmed" in g for g in gaps)


def test_answers_pass_and_idempotency_is_assumed_none_until_shown():
    pack = handover_pack("utilities")
    a = rehearsal_answers(pack, "example-a")
    assert shadow.validate_start_here(a, pack, load_profile("example-a"))["blocking"] == []
    a["idempotency"] = {"declared_idempotent": ["meter_ingest"], "evidence": {"meter_ingest": "doc"}}
    v = shadow.validate_start_here(a, pack, load_profile("example-a"))
    assert any("outage_ingest" in w and "NOT idempotent" in w for w in v["warnings"])
    eff = shadow.apply_start_here(pack, a)
    assert {p["id"]: p["idempotent"] for p in eff["pipelines"]} == {"meter_ingest": True, "outage_ingest": False}


@pytest.mark.parametrize("patch,expect", [
    (lambda a: a["first_service"].update(non_production=False), "non_production"),
    (lambda a: a["telemetry"]["identities"]["cost_billing"].update(read_only=False), "not declared read-only"),
    (lambda a: a["idempotency"].update(declared_idempotent=["nope"]), "not a pipeline"),
    (lambda a: a["people"]["approvers"].update(platform_owner=a["people"]["approvers"]["service_owner"]),
     "different people"),
])
def test_start_here_blocking_rules(patch, expect):
    pack = handover_pack("utilities")
    a = copy.deepcopy(rehearsal_answers(pack, "example-a"))
    patch(a)
    assert any(expect in g for g in shadow.validate_start_here(a, pack, load_profile("example-a"))["blocking"])


# -- Export adapter ------------------------------------------------------------------------------------
def _exports(root, profile="example-a", faults=()):
    p = load_profile(profile)
    for d in range(-3, 1):
        sim = SimulatorAdapter(SIM_NOW.replace(day=5 + d))
        for f in faults if d == 0 else ():
            sim.inject(f, "utilities")
        export_snapshot(NativePlatform(p, sim), "util_meter_daily", root, sim.now.strftime("%Y-%m-%dT%H%M"))
    return root / "util_meter_daily"


def test_export_adapter_is_read_only(tmp_path):
    a = ExportAdapter(load_profile("example-a"), _exports(tmp_path))
    assert a.actions == {}
    with pytest.raises(AttributeError):
        a.inject("F-01")
    assert len(a.history()) == 3


def test_export_profile_mismatch_refused(tmp_path):
    with pytest.raises(ValueError, match="taken for profile example-a"):
        ExportAdapter(load_profile("example-b"), _exports(tmp_path))


# -- Shadow mode enforcement ---------------------------------------------------------------------------
@pytest.fixture
def shadow_product(tmp_path):
    exports = tmp_path / "exports"
    _exports(exports)
    rt = ShadowRuntime(tmp_path / "adef", "example-a", exports)
    pack = handover_pack("utilities")
    rel = rt.release(pack, rehearsal_answers(pack, "example-a"))
    ws = Workspace(pack["dp_id"], tmp_path / "adef")
    for rid in (rel["og0_request"],):
        r = Approvals(ws).get_request(rid)
        for role in r.approver_roles:
            Approvals(ws).decide(rid, approver=pack["owners"][role], role=role, decision="approved",
                                 artifact_hash=r.artifact_hash)
    ob = rt.onboard(pack["dp_id"])
    r = Approvals(ws).get_request(ob.og1_request)
    Approvals(ws).decide(ob.og1_request, approver=pack["owners"]["platform_owner"], role="platform_owner",
                         decision="approved", artifact_hash=r.artifact_hash)
    return rt, ws, pack, exports, ob


def test_readiness_on_exports_proves_every_monitor_class(shadow_product):
    _, ws, _, _, ob = shadow_product
    assert ob.ready and ob.report["history_days"] == 3
    assert set(ob.report["fault_proofs"]) == {f"synthetic-{c}" for c in
                                              ("access", "cost", "freshness", "quality", "run", "schema", "slo", "volume")}
    assert "read-only exports" in ws.read("service/readiness-report.md")


def test_shadow_caps_everything_at_l1_and_executes_nothing(shadow_product):
    rt, ws, pack, exports, _ = shadow_product
    reg = Registry(ws)
    assert reg.mode() == "shadow"
    assert reg.effective_level("set_trust_status") == (Level.L1, "shadow mode: observe and propose only")
    p = load_profile("example-a")
    sim = SimulatorAdapter(SIM_NOW.replace(day=6))
    sim.inject("F-01", "utilities")
    export_snapshot(NativePlatform(p, sim), "util_meter_daily", exports, "2026-01-06T0700")
    out = rt.tick(pack["dp_id"], "2026-01-06T0700")
    o = out["outcomes"][0]
    assert o["outcome"] == "opened" and o["trust_status"] is None
    assert o["shadow_would"] == ["set trust status degraded", "page on-call"]
    assert not ws.has("control/action-log.jsonl") or not any(
        e["record"].get("outcome") == "executed" for e in ws.chainlog("executor", "control/action-log.jsonl").entries())
    from adef_operate.agents import RemediationAgent
    with pytest.raises(GuardViolation, match="shadow mode"):
        RemediationAgent(ws, rt.adapter(pack["dp_id"])).act(o["case"], 1)
    ex = Executor(ws, rt.adapter(pack["dp_id"]))
    with pytest.raises(ExecutorBlocked):
        ex.execute(ActionRecord(id="ACT-900", case_id=o["case"], product=pack["dp_id"], action_type="set_trust_status",
                                target=pack["dp_id"], params={"status": "degraded"}, requested_by="commander"))
    assert (ws.root / f"cases/{o['case']}/diagnosis.md").exists()


def test_pre_shadow_snapshot_cannot_be_ticked(shadow_product):
    rt, _, pack, _, _ = shadow_product
    with pytest.raises(AdefError, match="predates the shadow period"):
        rt.tick(pack["dp_id"], "2026-01-03T0700")


def test_conclusions_come_from_named_people(shadow_product):
    _, ws, _, _, _ = shadow_product
    with pytest.raises(GuardViolation, match="named person"):
        shadow.record_conclusion(ws, by="diagnosis", ticket="TCK-1", cause_class="platform")
    with pytest.raises(GuardViolation):
        shadow.record_conclusion(ws, by="Oncall Utilities", ticket="TCK-1", cause_class="gremlins")


# -- Rehearsal (O5-3 path end to end) -------------------------------------------------------------------
def test_rehearsal_report(rehearsal):
    rep = rehearsal["report"]
    assert rep["days"] == 14 and rep["snapshots"] == 14 and rep["synthetic"]
    assert rep["agent_cases"] == 5 and rep["judged"] == 5 and rep["alert_precision"] == 1.0
    assert rep["diagnosis_accuracy"] == 1.0 and len(rep["diagnosis_pairs"]) == 5
    assert rep["missed_tickets"] == ["TCK-0006"] and rep["guardrail_violations"] == 0
    assert rep["decision"] == "go"
    from pathlib import Path
    ws = Path(rehearsal["workspace"])
    states = {p.parent.name: yaml.safe_load(p.read_text())["state"] for p in ws.glob("cases/*/case.yaml")}
    assert all(states[c] == "closed" for c in states if c.startswith("INC"))
    assert states["PRB-001"] == "investigating"  # F-01 recurred on days 1 and 9
    assert len(list(ws.glob("evals/replays/*/scenario.json"))) == 5
    assert "SYNTHETIC" in (ws / rehearsal["report_path"]).read_text()


def test_synthetic_rehearsal_cannot_request_go_live(rehearsal):
    from pathlib import Path
    ws = Workspace("util_meter_daily", Path(rehearsal["home"]) / "adef")
    with pytest.raises(GuardViolation, match="synthetic"):
        shadow.request_go_live(ws, rehearsal["report_path"])


def test_short_shadow_period_is_no_go(tmp_path):
    res = run_rehearsal(tmp_path, "example-b", days=6)
    assert res["report"]["decision"] == "no-go"
    assert any("at least 14 needed" in r for r in res["report"]["reasons"])
    assert any("at least 5 needed" in r for r in res["report"]["reasons"])


# -- Go-live rule (O5-4) --------------------------------------------------------------------------------
def test_go_live_rule_enforced(rehearsal, tmp_path):
    from pathlib import Path
    home = tmp_path / "copy"
    shutil.copytree(Path(rehearsal["home"]) / "adef", home)
    ws = Workspace("util_meter_daily", home)
    pack = handover_pack("utilities")
    svc = ws.read_yaml("control/service.yaml")
    svc["data_origin"] = "real-export"  # treat as a real product's shadow period for this check
    ws.write_yaml("commander", "control/service.yaml", svc)
    approve = lambda rid: [Approvals(ws).decide(rid, approver=pack["owners"][r], role=r, decision="approved",
                                                artifact_hash=Approvals(ws).get_request(rid).artifact_hash)
                           for r in Approvals(ws).get_request(rid).approver_roles]
    # Promotion above L1 is refused before go-live, even with an approved OG7 for that level.
    h = canonical_hash({"action_type": "retry_failed_task", "level": "L3"})
    promo = Approvals(ws).request(gate="OG7", decision_asked="promote", artifact_hash=h, requested_by="knowledge")
    approve(promo.id)
    with pytest.raises(ApprovalError, match="go-live rule"):
        Registry(ws).set_level("retry_failed_task", "L3", approval_id=promo.id, approvals=Approvals(ws), artifact_hash=h)
    rep = shadow.shadow_report(ws)
    path = shadow.write_report(ws, rep)
    rid = shadow.request_go_live(ws, path)
    assert Approvals(ws).get_request(rid).approver_roles == ["platform_owner", "tech_lead"]
    with pytest.raises(GuardViolation):
        shadow.apply_go_live(ws)
    approve(rid)
    shadow.apply_go_live(ws)
    assert Registry(ws).mode() == "live"
    Registry(ws).set_level("retry_failed_task", "L3", approval_id=promo.id, approvals=Approvals(ws), artifact_hash=h)
    assert Registry(ws).stored_level("retry_failed_task") == Level.L3
    # Editing the report after approval voids the go-live decision.
    ws.write("shadow", path, ws.read(path) + "\nedited\n")
    assert not shadow.go_live_status(ws)[0]


def test_cli_start_here_and_rehearsal_help(capsys):
    from adef_operate.__main__ import main
    assert main(["start-here", "--dp", "util_meter_daily", "--profile", "example-a"]) == 0
    assert "suite_assumptions_confirmed" in capsys.readouterr().out
