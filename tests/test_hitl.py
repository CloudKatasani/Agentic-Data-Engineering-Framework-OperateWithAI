"""Human-in-the-loop operating model: agent team, notifications, and the operating loop."""

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml
from fastapi.testclient import TestClient

from adef_operate.agents.team import AGENTS, FLOW, HUMAN_ROLES
from adef_operate.api.app import create_app, seed_demo
from adef_operate.api.fleet import Fleet
from adef_operate.core.errors import ApprovalError, GuardViolation
from adef_operate.core.hashing import canonical_hash
from adef_operate.engine.approvals import Approvals
from adef_operate.engine.notifications import Notifier
from adef_operate.engine.registry import Registry

U, T = "util_meter_daily", "telco_usage_daily"


@pytest.fixture
def f(tmp_path, monkeypatch):
    monkeypatch.delenv("ADEF_NOTIFY_WEBHOOK", raising=False)
    seed_demo(tmp_path)
    return Fleet(tmp_path)


def ask(f, dp, kind=None, person=None, action=None):
    for n in f.notifications(dp=dp):
        if (kind is None or n["kind"] == kind) and (person is None or n["to_person"] == person) \
                and (action is None or action in n["actions"]):
            return n
    raise AssertionError(f"no open {kind} for {person}: {[(n['kind'], n['to_person'], n['title']) for n in f.notifications(dp=dp)]}")


def executed(f, dp):
    ws = f.ws(dp)
    if not ws.has("control/action-log.jsonl"):
        return []
    return [e["record"] for e in ws.chainlog("executor", "control/action-log.jsonl").entries()
            if e["record"].get("outcome") == "executed"]


# -- the team -------------------------------------------------------------------------------------
def test_every_agent_has_a_role_responsibilities_limits_and_human_checkpoints():
    assert [a["stage"] for a in AGENTS] == [f"O{i}" for i in range(8)]
    roles = {r["role"] for r in HUMAN_ROLES} | {"council", "approver"}
    for a in AGENTS:
        assert a["mission"] and len(a["responsibilities"]) >= 3 and a["never"] and a["triggers"] and a["produces"]
        assert a["humans"] and all(h["role"] in roles and h["asks"] and h["when"] for h in a["humans"])
    assert {s["who"] for s in FLOW} == {"ai", "human", "both"}


def test_team_view_reports_live_work(f):
    f.simulate_fault(U, "F-01")
    t = f.team()
    stats = {a["id"]: a["stats"] for a in t["agents"]}
    assert stats["commander"]["notifications"] >= 1 and stats["remediation"]["open_items"] == 1
    assert stats["diagnosis"]["events"] >= 1
    on_call = next(r for r in t["roles"] if r["role"] == "on_call")
    assert on_call["open"] >= 1 and "Oncall Utilities" in on_call["holders"]


# -- S2: the whole loop through notifications ---------------------------------------------------
def test_s2_incident_end_to_end_with_people_deciding(f):
    out = f.simulate_fault(U, "F-01")
    assert out["tick"]["loop"]["opened"] == ["INC-001"]
    assert f.case("INC-001", U)["diagnosis"]["cause_class"] == "upstream outage"  # diagnosed on its own
    ack = ask(f, U, "acknowledge", "Oncall Utilities")
    assert ack["priority"] == "P2" and ack["due_at"]
    choose = ask(f, U, "decide", "Sam Service", "choose-option")
    assert not executed(f, U)[1:]  # only the rule-based trust status so far; no fix without a person
    f.notification_action(U, ack["id"], "acknowledge", "Oncall Utilities")
    f.resolve_source(U, "F-01")
    rerun = next(o for o in f.case("INC-001", U)["options"] if o["action_type"] == "rerun_latest_window")
    r = f.notification_action(U, choose["id"], "choose-option", "Sam Service", option=rerun["no"])
    assert r["status"] == "approved" and r["follow_up"]["status"] == "approved-awaiting-people"
    run = ask(f, U, "run", "Oncall Utilities", "run-done")
    f.sim(U).run_action("rerun_latest_window", U, {"pipeline": "meter_ingest"})
    assert f.notification_action(U, run["id"], "run-done", "Oncall Utilities")["state"] == "recovered"
    confirm = ask(f, U, "confirm", "Sam Service", "confirm-cause")
    assert confirm["link"]["suggested_cause"] == "upstream outage"
    f.notification_action(U, confirm["id"], "confirm-cause", "Sam Service", cause_class="upstream outage")
    close = ask(f, U, "confirm", "Sam Service", "close")
    assert f.notification_action(U, close["id"], "close", "Sam Service")["state"] == "closed"
    assert f.notifications(dp=U) == []
    acts = f.activity(U)
    assert {"ai", "human"} <= {a["kind"] for a in acts}


# -- S1: council, containment, next fix -----------------------------------------------------------
def test_s1_council_then_choose_containment_then_rollback(f):
    f.simulate_fault(T, "F-05")
    reviewers = {n["to_person"] for n in f.notifications(dp=T) if n["kind"] == "review"}
    assert reviewers == {"Tia Lead", "Pat Platform", "Nia Owner"}
    assert not any("choose-option" in n["actions"] for n in f.notifications(dp=T))  # no fix before the council
    f.notification_action(T, ask(f, T, "review", "Tia Lead")["id"], "council-agree", "Tia Lead")
    assert not any("choose-option" in n["actions"] for n in f.notifications(dp=T))
    r = f.notification_action(T, ask(f, T, "review", "Pat Platform")["id"], "council-agree", "Pat Platform")
    assert r["council"] == "agreed"
    choose = ask(f, T, "decide", "Lee Service", "choose-option")
    assert "Pause" in choose["title"]
    f.notification_action(T, choose["id"], "choose-option", "Lee Service", option=choose["link"]["recommended"])
    run = ask(f, T, "run", "Oncall Telco")
    f.sim(T).run_action("pause_pipeline", T, {"pipeline": "billing_ingest"})
    f.notification_action(T, run["id"], "run-done", "Oncall Telco")
    case = f.case("INC-001", T)["case"]
    assert case["state"] == "options-ready" and case["failed_attempts"] == 0  # containment is not a failed fix
    nxt = ask(f, T, "decide", action="choose-option")
    assert "Roll back" in nxt["title"] and nxt["to_person"] == "Rae Release"
    assert "Pause" not in " ".join(o["title"] for o in f.case("INC-001", T)["options"])


# -- people only, deadlines, dismissals --------------------------------------------------------------
def test_only_named_people_act(f):
    f.simulate_fault(U, "F-01")
    n = ask(f, U, "acknowledge")
    with pytest.raises(ApprovalError):
        f.notification_action(U, n["id"], "acknowledge", "commander")
    with pytest.raises(ApprovalError):
        f.notification_action(U, n["id"], "acknowledge", "Someone Random")
    with pytest.raises(GuardViolation, match="reason"):
        f.notification_action(U, n["id"], "dismiss", "Oncall Utilities")
    f.notification_action(U, n["id"], "dismiss", "Oncall Utilities", note="duplicate of the vendor ticket")
    assert all(x["id"] != n["id"] for x in f.notifications(dp=U))


def test_missed_deadline_escalates_to_the_next_role(f):
    f.simulate_fault(U, "F-01")
    ws = f.ws(U)
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    raised = Notifier(ws).check_deadlines(later)
    assert any(n["to_person"] == "Pat Platform" and n["kind"] == "escalation" for n in raised)
    ack = next(n for n in Notifier(ws).all() if n["kind"] == "acknowledge")
    assert ack["status"] == "escalated"


# -- delivery ----------------------------------------------------------------------------------------
class _Hook(BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        _Hook.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_webhook_delivery_and_no_delivery_for_fyi(f, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ADEF_NOTIFY_WEBHOOK", f"http://127.0.0.1:{srv.server_port}/hook")
    _Hook.got.clear()
    f.simulate_fault(U, "F-01")
    srv.shutdown()
    kinds = {g["notification"]["kind"] for g in _Hook.got}
    assert {"acknowledge", "decide"} <= kinds and "fyi" not in kinds
    assert all("text" in g for g in _Hook.got)
    n = ask(f, U, "acknowledge")
    assert n["deliveries"] and n["deliveries"][0]["ok"]


# -- standing approval, sweeps --------------------------------------------------------------------------
def test_standing_approval_runs_and_tells_a_person(f):
    ws = f.ws(U)
    appr, h = Approvals(ws), canonical_hash({"action_type": "retry_failed_task", "level": "L3"})
    req = appr.request(gate="OG7", decision_asked="L3", artifact_hash=h, requested_by="knowledge")
    for role in ("platform_owner", "tech_lead"):
        appr.decide(req.id, approver=ws.read_yaml("control/service.yaml")["approvers"][role], role=role,
                    decision="approved", artifact_hash=h)
    Registry(ws).set_level("retry_failed_task", "L3", approval_id=req.id, approvals=appr, artifact_hash=h)
    f.simulate_fault(U, "F-08")
    fyi = next(n for n in Notifier(ws).all() if n["kind"] == "fyi" and "standing approval" in n["title"])
    assert fyi["to_person"] == "Sam Service" and "Reversibility" in fyi["body"]
    assert any(e["action_type"] == "retry_failed_task" and e["level"] == "L3" for e in executed(f, U))
    f.tick(U)  # recovery verified on the next check
    assert f.case("INC-001", U)["case"]["state"] == "recovered"
    assert ask(f, U, "confirm", "Sam Service", "confirm-cause")


def test_findings_and_escalations_reach_their_people(f):
    f.simulate_fault(T, "F-06")
    f.simulate_fault(T, "F-07")
    fin = ask(f, T, "decide", "Pat Platform", "approve")
    assert "FIN-001" in fin["title"]
    r = f.notification_action(T, fin["id"], "approve", "Pat Platform")
    assert r["follow_up"]["decision"] == "accepted"
    sec = [n for n in f.notifications(dp=T) if n["to_person"] == "Sec Compliance"]
    assert any(n["kind"] == "escalation" and n["priority"] == "P1" for n in sec)
    assert any(n["kind"] == "decide" and "GOV-001" in n["title"] for n in sec)


# -- API and scheduler ----------------------------------------------------------------------------------
def test_api_inbox_actions_with_personal_tokens(tmp_path, monkeypatch):
    seed_demo(tmp_path)
    ids = tmp_path / "people.yaml"
    ids.write_text(yaml.safe_dump({hashlib.sha256(b"oncall").hexdigest(): "Oncall Utilities",
                                   hashlib.sha256(b"sam").hexdigest(): "Sam Service"}))
    monkeypatch.setenv("ADEF_IDENTITY_FILE", str(ids))
    monkeypatch.delenv("ADEF_API_TOKEN", raising=False)
    c = TestClient(create_app(tmp_path))
    oc, sam = {"Authorization": "Bearer oncall"}, {"Authorization": "Bearer sam"}
    c.post("/ops/simulate/faults", json={"dp": U, "fault_id": "F-01"}, headers=oc)
    mine = c.get("/ops/notifications?person=Oncall%20Utilities", headers=oc).json()
    assert [n["kind"] for n in mine] == ["acknowledge"]
    url = f"/ops/notifications/{U}/{mine[0]['id']}/actions"
    assert c.post(url, json={"action": "acknowledge", "by": "Sam Service"}, headers=oc).status_code == 403
    assert c.post(url, json={"action": "acknowledge"}, headers=oc).json()["status"] == "acknowledged"
    team = c.get("/ops/team", headers=sam).json()
    assert len(team["agents"]) == 8 and team["flow"]
    assert c.get("/ops/activity", headers=sam).json()
    assert any(p["name"] == "Sam Service" for p in c.get("/ops/people", headers=sam).json())


def test_background_scheduler_runs_cycles(tmp_path, monkeypatch):
    monkeypatch.delenv("ADEF_API_TOKEN", raising=False)
    monkeypatch.delenv("ADEF_IDENTITY_FILE", raising=False)
    seed_demo(tmp_path)
    with TestClient(create_app(tmp_path, autotick=0.2)):
        time.sleep(1.0)
    ticks = int((tmp_path / "demo-sim" / f"{U}.ticks").read_text())
    assert ticks >= 2


def test_operating_model_document_matches_the_code():
    from pathlib import Path
    from adef_operate.agents.team import render_operating_model
    doc = Path(__file__).parent.parent / "OPERATING_MODEL.md"
    assert doc.read_text().strip() == render_operating_model().strip(), \
        "regenerate: python -m adef_operate operating-model > OPERATING_MODEL.md"
