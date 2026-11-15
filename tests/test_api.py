"""Section 11: REST API and console."""

import hashlib

import pytest
import yaml
from fastapi.testclient import TestClient

from adef_operate.api.app import create_app, seed_demo
from adef_operate.packs import handover_pack


@pytest.fixture
def home(tmp_path):
    seed_demo(tmp_path, None)
    return tmp_path


@pytest.fixture
def c(home, monkeypatch):
    monkeypatch.delenv("ADEF_API_TOKEN", raising=False)
    monkeypatch.delenv("ADEF_IDENTITY_FILE", raising=False)
    return TestClient(create_app(home))


def test_health_catalog_and_console(c):
    assert c.get("/ops/health").json()["ok"]
    cat = c.get("/ops/catalog").json()
    assert {p["id"] for p in cat["profiles"]} >= {"example-a", "example-b"} and len(cat["faults"]) == 8
    page = c.get("/")
    assert page.status_code == 200 and "DataCrew- OperateWithAI" in page.text
    assert c.get("/static/app.js").status_code == 200 and c.get("/static/styles.css").status_code == 200


def test_post_services_onboards_from_a_handover_pack(tmp_path, monkeypatch):
    monkeypatch.delenv("ADEF_API_TOKEN", raising=False)
    c = TestClient(create_app(tmp_path))
    r = c.post("/ops/services", json={"mode": "demo", "pack": "utilities", "profile": "example-a"})
    assert r.status_code == 201 and r.json()["og0_request"] == "APR-001"
    assert c.post("/ops/services/util_meter_daily/onboard").status_code == 409  # OG0 not decided yet
    owners = handover_pack("utilities")["owners"]
    for role in ("product_owner", "platform_owner"):
        c.post("/ops/approvals/util_meter_daily/APR-001", json={"approver": owners[role], "role": role, "decision": "approved"})
    ob = c.post("/ops/services/util_meter_daily/onboard").json()
    assert ob["ready"] and ob["og1_request"] == "APR-002"
    bad = handover_pack("telco")
    del bad["pipelines"][0]["idempotent"]
    r = c.post("/ops/services", json={"mode": "demo", "pack": bad})
    assert r.status_code == 422 and any("idempotency" in g for g in r.json()["gaps"])
    assert c.post("/ops/services", json={"mode": "demo", "pack": "utilities"}).status_code == 409


def test_get_service_shows_definition_health_and_autonomy(c):
    s = c.get("/ops/services/util_meter_daily").json()
    assert s["summary"]["og1"]["valid"] and len(s["monitors"]) == 19
    ladder = {a["action_type"]: a for a in s["autonomy"]}
    assert ladder["retry_failed_task"]["effective"] == "L1" and ladder["revoke_access"]["executed_by_people"]
    assert c.get("/ops/services/nope").status_code == 404


def test_signals_ingest_and_process(c):
    r = c.post("/ops/signals?process=true", json={"product": "util_meter_daily", "signal_class": "consumer",
                                                  "object": "gold.daily_consumption", "observed": "reported wrong total",
                                                  "expected": "matches source", "evidence_path": "consume://issue/77",
                                                  "time": "2026-01-05T08:00:00+00:00"})
    assert r.status_code == 201
    out = r.json()["outcomes"][0]
    assert out["outcome"] == "opened" and out["severity"] == "S2" and "consumer" in out["rule"]
    bad = c.post("/ops/signals", json={"product": "util_meter_daily", "signal_class": "network", "object": "x",
                                       "observed": 1, "expected": 0, "evidence_path": "p"})
    assert bad.status_code == 422


def test_full_case_flow_and_named_human_approvals(c):
    r = c.post("/ops/simulate/faults", json={"dp": "util_meter_daily", "fault_id": "F-02"}).json()
    case = r["tick"]["outcomes"][0]["case"]
    q = c.get("/ops/cases?state=open&severity=S1").json()
    assert [x["id"] for x in q] == [case]
    assert c.post(f"/ops/cases/{case}/steps/diagnose").json()["cause_class"] == "source data"
    blocked = c.post(f"/ops/cases/{case}/steps/options")
    assert blocked.status_code == 409 and blocked.json()["guardrail"]
    for who in ("Reviewer A", "Reviewer B"):
        c.post(f"/ops/cases/{case}/steps/council", json={"reviewer": who, "agrees": True})
    opts = c.post(f"/ops/cases/{case}/steps/options").json()["options"]
    act = c.post(f"/ops/cases/{case}/steps/act", json={"option": opts[0]["no"]}).json()
    assert act["status"] == "awaiting-approval"
    body = {"request_id": act["approval"], "role": "service_owner", "decision": "approved"}
    assert c.post(f"/ops/cases/{case}/approvals", json={**body, "approver": "remediation"}).status_code == 403
    assert c.post(f"/ops/cases/{case}/approvals", json={**body, "approver": "Somebody Else"}).status_code == 403
    ok = c.post(f"/ops/cases/{case}/approvals", json={**body, "approver": "Sam Service"}).json()
    assert ok["status"] == "approved" and ok["follow_up"]["status"] == "approved-awaiting-people"
    detail = c.get(f"/ops/cases/{case}").json()
    assert detail["diagnosis"]["cause_class"] == "source data" and detail["evidence"] and detail["signals"]
    assert all("data" not in e for e in detail["evidence"])
    assert c.post(f"/ops/cases/{case}/steps/close").status_code == 409  # guards still apply


def test_ambiguous_case_id_needs_dp(c):
    c.post("/ops/simulate/faults", json={"dp": "util_meter_daily", "fault_id": "F-01"})
    c.post("/ops/simulate/faults", json={"dp": "telco_usage_daily", "fault_id": "F-03"})
    assert c.get("/ops/cases/INC-001").status_code == 409
    assert c.get("/ops/cases/INC-001?dp=telco_usage_daily").json()["dp_id"] == "telco_usage_daily"


def test_simulate_refused_for_non_demo_products(c, home):
    from adef_operate.engine.workspace import Workspace
    ws = Workspace("telco_usage_daily", home)
    svc = ws.read_yaml("control/service.yaml")
    svc["mode"] = "shadow"
    ws.write_yaml("commander", "control/service.yaml", svc)
    r = c.post("/ops/simulate/faults", json={"dp": "telco_usage_daily", "fault_id": "F-03"})
    assert r.status_code == 409 and r.json()["type"] == "not-demo"


def test_finops_and_governance_reviews(c):
    c.post("/ops/simulate/faults", json={"dp": "telco_usage_daily", "fault_id": "F-06"})
    c.post("/ops/simulate/faults", json={"dp": "telco_usage_daily", "fault_id": "F-07"})
    c.post("/ops/services/telco_usage_daily/tick")
    f = c.get("/ops/finops/telco_usage_daily").json()
    assert len(f["findings"]) == 1 and "Saving arithmetic" in f["findings"][0]["body"]  # routed once, one finding
    g = c.get("/ops/governance/telco_usage_daily").json()
    assert g["controls"]["grants"]["status"] == "drifted" and len(g["findings"]) == 1
    pack = c.get("/ops/governance/telco_usage_daily/evidence-pack?period=2026-01").text
    assert "TEL-C1" in pack
    fin_req = next(a for a in c.get("/ops/approvals?dp=telco_usage_daily").json() if a["gate"] == "OG4")
    r = c.post(f"/ops/approvals/telco_usage_daily/{fin_req['id']}",
               json={"approver": "Pat Platform", "role": "platform_owner", "decision": "approved"}).json()
    assert r["follow_up"]["decision"] == "accepted"


def test_emergency_stop_and_lift(c):
    assert c.post("/ops/stop", json={"by": "Pat Platform", "reason": "drill"}).json() == {"stopped": "all"}
    assert c.get("/ops/fleet").json()["global_stop"].startswith("drill")
    out = c.post("/ops/simulate/faults", json={"dp": "util_meter_daily", "fault_id": "F-01"}).json()
    assert {o["outcome"] for o in out["tick"]["outcomes"]} == {"stopped"}
    assert c.delete("/ops/stop?by=Pat%20Platform").json() == {"cleared": "all"}
    assert c.get("/ops/fleet").json()["global_stop"] is None
    c.post("/ops/stop", json={"by": "Pat Platform", "reason": "one product", "dp": "telco_usage_daily"})
    assert c.get("/ops/services/telco_usage_daily").json()["summary"]["stop"].startswith("one product")


def test_request_desk_and_learning(c):
    r = c.post("/ops/requests", json={"dp": "util_meter_daily", "text": "just mute that alert", "by": "Sam Service"}).json()
    assert r["decision"] == "refused" and r["gate"] == "OG7"
    tun = c.post("/ops/learning/util_meter_daily/propose-tuning",
                 json={"monitor_id": "MON-DUR-meter_ingest", "changes": {"max_ratio": 2.5}, "reason": "seasonal"}).json()
    assert tun["before"] >= tun["after"] and tun["approval"]
    assert c.post("/ops/learning/util_meter_daily/propose-tuning",
                  json={"monitor_id": "MON-DUR-meter_ingest", "changes": {"enabled": False}}).status_code == 409
    assert "accuracy" in c.get("/ops/learning/util_meter_daily").json()


def test_token_auth(home, monkeypatch):
    monkeypatch.setenv("ADEF_API_TOKEN", "s3cret")
    monkeypatch.delenv("ADEF_IDENTITY_FILE", raising=False)
    c = TestClient(create_app(home))
    assert c.get("/ops/health").status_code == 200
    assert c.get("/ops/fleet").status_code == 401
    assert c.get("/ops/fleet", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_identity_binding_means_you_decide_only_as_yourself(home, tmp_path, monkeypatch):
    ids = tmp_path / "people.yaml"
    ids.write_text(yaml.safe_dump({hashlib.sha256(b"tok-sam").hexdigest(): "Sam Service",
                                   hashlib.sha256(b"tok-pat").hexdigest(): "Pat Platform"}))
    monkeypatch.setenv("ADEF_IDENTITY_FILE", str(ids))
    monkeypatch.delenv("ADEF_API_TOKEN", raising=False)
    c = TestClient(create_app(home))
    sam = {"Authorization": "Bearer tok-sam"}
    assert c.get("/ops/fleet").status_code == 401
    assert c.get("/ops/whoami", headers=sam).json() == {"person": "Sam Service"}
    case = c.post("/ops/simulate/faults", json={"dp": "util_meter_daily", "fault_id": "F-01"}, headers=sam).json()["tick"]["outcomes"][0]["case"]
    c.post(f"/ops/cases/{case}/steps/diagnose", headers=sam)
    opts = c.post(f"/ops/cases/{case}/steps/options", headers=sam).json()["options"]
    rerun = next(o for o in opts if o["action_type"] == "rerun_latest_window")
    act = c.post(f"/ops/cases/{case}/steps/act", json={"option": rerun["no"]}, headers=sam).json()
    body = {"request_id": act["approval"], "role": "platform_owner", "decision": "approved"}
    r = c.post(f"/ops/cases/{case}/approvals", json={**body, "approver": "Pat Platform"}, headers=sam)
    assert r.status_code == 403 and "cannot act as" in r.json()["detail"]
    r = c.post(f"/ops/cases/{case}/approvals", json=body, headers={"Authorization": "Bearer tok-pat"})
    assert r.json()["status"] == "approved"
