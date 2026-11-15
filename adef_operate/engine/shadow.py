"""Shadow mode (O5-3) and the go or no-go decision (O5-4).

* ``START HERE`` answers are validated before a real product is onboarded.
* In shadow mode every action type is capped at L1, nobody is paged except Security and
  Compliance for a suspected security event, and nothing is written to the platform.
* People record their own conclusions per incident. The shadow report compares the
  agents with them against OPERATE_SPEC 13.3.
* Go-live rule: two weeks of shadow mode with diagnosis accuracy and alert precision on
  target and zero guardrail violations, then an approved OG7 go-live decision, before any
  action type is raised above L1.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from ..core.errors import ApprovalError, GuardViolation
from ..core.hashing import file_hash
from .approvals import Approvals
from .workspace import Workspace

MIN_DAYS = 14
MIN_CONFIRMED = 5
TARGETS = {"diagnosis_accuracy": 0.80, "alert_precision": 0.85, "judged_share": 0.90}
REQUIRED_SLOT_IDENTITIES = ("orchestration", "data_platform", "data_quality", "cost_billing", "access_audit",
                            "catalog", "lineage")
APPROVER_ROLES = ("product_owner", "service_owner", "platform_owner", "data_steward", "data_owner",
                  "security_compliance", "release_manager", "tech_lead")
CAUSES = ("source data", "upstream outage", "code defect", "configuration", "capacity", "platform", "access",
          "unknown")


# -- START HERE -------------------------------------------------------------------------------------
def start_here_template(dp_id: str = "<dp_id>", profile_id: str = "<stack profile id>") -> str:
    ids = "\n".join(f"    {s}: {{identity: \"<read-only identity>\", read_only: true, verified_by: \"<name>\", "
                    f"verified_on: \"<YYYY-MM-DD>\"}}" for s in REQUIRED_SLOT_IDENTITIES)
    roles = "\n".join(f"    {r}: \"<name>\"" for r in APPROVER_ROLES)
    return f"""# START HERE FIRST (OPERATE_SPEC) - answers for the first service. No secrets in this file.
first_service:
  dp_id: {dp_id}                 # must match the Operations Handover Pack from Build stage 7
  non_production: true           # shadow mode starts on a non-production product
telemetry:
  stack_profile: {profile_id}    # a Build-With-AI Stack Profile id
  access_method: export-folder   # export-folder (no credentials held by Operate) or api
  identities:                    # one read-only identity per required Operate slot
{ids}
people:
  on_call: "<name>"
  approvers:
{roles}
idempotency:
  declared_idempotent: []        # pipelines DOCUMENTED idempotent; assume none until shown
  evidence: {{}}                   # pipeline -> link to the idempotency documentation
baseline:                        # today's figures for a comparable product
  time_to_detect_min: null
  time_to_recover_min: null
  monthly_incidents: null
suite_assumptions_confirmed: false   # SUITE_SPEC.md section 10, confirmed by the Platform Owner
"""


def validate_start_here(answers: dict, pack: dict, profile: Optional[dict] = None) -> dict[str, list[str]]:
    blocking, warnings = [], []
    fs = answers.get("first_service") or {}
    if fs.get("dp_id") != pack.get("dp_id"):
        blocking.append(f"first_service.dp_id {fs.get('dp_id')!r} does not match the handover pack {pack.get('dp_id')!r}")
    if fs.get("non_production") is not True:
        blocking.append("first_service.non_production must be true for shadow mode")
    tel = answers.get("telemetry") or {}
    if profile is not None and tel.get("stack_profile") != profile.get("profile_id"):
        blocking.append(f"telemetry.stack_profile {tel.get('stack_profile')!r} is not the bound profile")
    if tel.get("access_method") not in ("export-folder", "api"):
        blocking.append("telemetry.access_method must be export-folder or api")
    ids = tel.get("identities") or {}
    for slot in REQUIRED_SLOT_IDENTITIES:
        i = ids.get(slot) or {}
        if not i.get("identity") or str(i.get("identity")).startswith("<"):
            blocking.append(f"telemetry.identities.{slot}: no identity named")
        elif i.get("read_only") is not True:
            blocking.append(f"telemetry.identities.{slot}: identity is not declared read-only")
        elif not i.get("verified_by") or str(i.get("verified_by")).startswith("<"):
            blocking.append(f"telemetry.identities.{slot}: read-only access not verified by a named person")
    people = answers.get("people") or {}
    if not people.get("on_call") or str(people.get("on_call")).startswith("<"):
        blocking.append("people.on_call is not named")
    appr = people.get("approvers") or {}
    for role in APPROVER_ROLES[:6]:
        if not appr.get(role) or str(appr.get(role)).startswith("<"):
            blocking.append(f"people.approvers.{role} is not named")
    if appr.get("service_owner") and appr.get("service_owner") == appr.get("platform_owner"):
        blocking.append("service_owner and platform_owner must be different people")
    pipes = {p["id"] for p in pack.get("pipelines", [])}
    idem = (answers.get("idempotency") or {}).get("declared_idempotent") or []
    for p in idem:
        if p not in pipes:
            blocking.append(f"idempotency: {p} is not a pipeline of {pack.get('dp_id')}")
        elif not ((answers.get("idempotency") or {}).get("evidence") or {}).get(p):
            blocking.append(f"idempotency: {p} declared idempotent without a documentation link")
    for p in pack.get("pipelines", []):
        if p.get("idempotent") is True and p["id"] not in idem:
            warnings.append(f"{p['id']} is idempotent in the handover pack but not documented; treated as NOT "
                            "idempotent until shown")
    base = answers.get("baseline") or {}
    for k in ("time_to_detect_min", "time_to_recover_min", "monthly_incidents"):
        if not isinstance(base.get(k), (int, float)) or isinstance(base.get(k), bool):
            blocking.append(f"baseline.{k} needs a number")
    if answers.get("suite_assumptions_confirmed") is not True:
        blocking.append("suite_assumptions_confirmed must be true (SUITE_SPEC section 10)")
    return {"blocking": blocking, "warnings": warnings}


def apply_start_here(pack: dict, answers: dict) -> dict:
    """Idempotency: assume none until shown; approvers and on-call come from the answers."""
    out = json.loads(json.dumps(pack))
    idem = set((answers.get("idempotency") or {}).get("declared_idempotent") or [])
    for p in out["pipelines"]:
        p["idempotent"] = p["id"] in idem
    out["owners"] = {**out.get("owners", {}), **(answers["people"]["approvers"])}
    out["on_call"] = answers["people"]["on_call"]
    return out


def enter_shadow(ws: Workspace, answers: dict, *, data_origin: str) -> None:
    svc = ws.read_yaml("control/service.yaml")
    svc["mode"] = "shadow"
    svc["data_origin"] = data_origin
    svc["simulated"] = data_origin != "real-export"
    ws.write_yaml("commander", "control/service.yaml", svc)
    ws.write_yaml("commander", "control/start-here.yaml", answers)


# -- people's conclusions -----------------------------------------------------------------------------
def _named(ws: Workspace) -> set[str]:
    svc = ws.read_yaml("control/service.yaml") or {}
    return set((svc.get("approvers") or {}).values()) | {svc.get("on_call")}


def record_conclusion(ws: Workspace, *, by: str, ticket: Optional[str] = None, case_id: Optional[str] = None,
                      opened_at: Optional[str] = None, cause_class: Optional[str] = None,
                      real_problem: Optional[bool] = None, minutes_spent: Optional[float] = None,
                      note: str = "") -> dict:
    """A person's own conclusion: a ticket from the team's normal process and/or a judgement
    of an agent case. Linked by ID only (OA2); no ticket text is copied."""
    if by not in _named(ws):
        raise GuardViolation(f"{by!r} is not a named person for {ws.dp_id}; conclusions come from people")
    if cause_class is not None and cause_class not in CAUSES:
        raise GuardViolation(f"cause_class must be one of {CAUSES}")
    if not ticket and not case_id:
        raise GuardViolation("a conclusion names a ticket, an agent case, or both")
    rec = {"by": by, "ticket": ticket, "case_id": case_id, "opened_at": opened_at, "cause_class": cause_class,
           "real_problem": real_problem, "minutes_spent": minutes_spent, "note": note[:200]}
    ws.chainlog("shadow", "shadow/conclusions.jsonl").append(rec)
    return rec


def conclusions(ws: Workspace) -> list[dict]:
    if not ws.has("shadow/conclusions.jsonl"):
        return []
    out: dict[tuple, dict] = {}
    for e in ws.chainlog("shadow", "shadow/conclusions.jsonl").entries():
        r = e["record"]
        out[(r.get("ticket"), r.get("case_id"))] = r  # the latest statement wins
    return list(out.values())


def record_tick(ws: Workspace, *, snapshot: str, at: str, signals: int, cases_opened: list[str]) -> None:
    ws.append("shadow", "shadow/ticks.jsonl", json.dumps({"snapshot": snapshot, "at": at, "signals": signals,
                                                          "cases_opened": cases_opened}))


def ticks(ws: Workspace) -> list[dict]:
    if not ws.has("shadow/ticks.jsonl"):
        return []
    return [json.loads(l) for l in ws.read("shadow/ticks.jsonl").splitlines() if l.strip()]


# -- report ---------------------------------------------------------------------------------------------
def _executed(ws: Workspace) -> int:
    if not ws.has("control/action-log.jsonl"):
        return 0
    return sum(1 for e in ws.chainlog("executor", "control/action-log.jsonl").entries()
               if e["record"].get("outcome") == "executed")


def shadow_report(ws: Workspace) -> dict:
    from .case_store import CaseStore
    cs = CaseStore(ws)
    svc = ws.read_yaml("control/service.yaml") or {}
    answers = ws.read_yaml("control/start-here.yaml") or {}
    tk = ticks(ws)
    days = sorted({t["at"][:10] for t in tk})
    ats = sorted(datetime.fromisoformat(t["at"]) for t in tk)
    span = (ats[-1] - ats[0]).days + 1 if ats else 0
    incs = [c for c in cs.all() if c.type == "INC"]
    first_sig = {}
    for t in tk:
        for cid in t["cases_opened"]:
            first_sig.setdefault(cid, t["at"])
    concl = conclusions(ws)
    by_case = {c["case_id"]: c for c in concl if c.get("case_id")}
    tickets = [c for c in concl if c.get("ticket")]

    judged = [c for c in incs if c.id in by_case and by_case[c.id].get("real_problem") is not None]
    real = [c for c in judged if by_case[c.id]["real_problem"]]
    precision = round(len(real) / len(judged), 3) if judged else None
    pairs = []
    for c in incs:
        h = by_case.get(c.id)
        d = ws.read_yaml(f"cases/{c.id}/diagnosis.yaml") or {}
        if h and h.get("cause_class") and h["cause_class"] != "unknown" and d:
            pairs.append({"case": c.id, "ticket": h.get("ticket"), "agent": d.get("cause_class"),
                          "human": h["cause_class"], "match": d.get("cause_class") == h["cause_class"]})
    accuracy = round(sum(p["match"] for p in pairs) / len(pairs), 3) if pairs else None
    unsupported = 0
    for c in incs:
        d = ws.read_yaml(f"cases/{c.id}/diagnosis.yaml") or {}
        if d and d.get("cause_class") != "unknown":
            h = next((h for h in d.get("hypotheses", []) if h["cause_class"] == d["cause_class"]), {})
            unsupported += 0 if h.get("supporting") else 1
    missed = [t["ticket"] for t in tickets if not t.get("case_id")]
    lead = []
    for t in tickets:
        if t.get("case_id") in first_sig and t.get("opened_at"):
            lead.append(round((datetime.fromisoformat(t["opened_at"]) -
                               datetime.fromisoformat(first_sig[t["case_id"]])).total_seconds() / 60, 1))
    minutes = [c["minutes_spent"] for c in concl if isinstance(c.get("minutes_spent"), (int, float))]
    violations = _executed(ws)
    judged_share = round(len(judged) / len(incs), 3) if incs else None

    reasons = []
    if span < MIN_DAYS:
        reasons.append(f"shadow period is {span} day(s); at least {MIN_DAYS} needed")
    if accuracy is None or len(pairs) < MIN_CONFIRMED:
        reasons.append(f"{len(pairs)} incident(s) with a human-confirmed cause; at least {MIN_CONFIRMED} needed")
    elif accuracy < TARGETS["diagnosis_accuracy"]:
        reasons.append(f"diagnosis accuracy {accuracy} below {TARGETS['diagnosis_accuracy']}")
    if precision is None:
        reasons.append("no agent case has been judged real or not by a person")
    elif precision < TARGETS["alert_precision"]:
        reasons.append(f"alert precision {precision} below {TARGETS['alert_precision']}")
    if judged_share is not None and judged_share < TARGETS["judged_share"]:
        reasons.append(f"only {judged_share:.0%} of agent cases judged by a person")
    if unsupported:
        reasons.append(f"{unsupported} unsupported cause(s)")
    if violations:
        reasons.append(f"{violations} action(s) executed during shadow mode")
    base = answers.get("baseline") or {}
    result = {
        "product": ws.dp_id, "mode": svc.get("mode"), "data_origin": svc.get("data_origin"),
        "synthetic": svc.get("data_origin") != "real-export", "period": [days[0], days[-1]] if days else [],
        "days": span, "snapshots": len(tk), "agent_cases": len(incs), "judged": len(judged),
        "judged_share": judged_share, "alert_precision": precision, "diagnosis_pairs": pairs,
        "diagnosis_accuracy": accuracy, "unsupported_causes": unsupported, "missed_tickets": missed,
        "agent_lead_minutes": lead, "human_minutes_per_case": round(sum(minutes) / len(minutes), 1) if minutes else None,
        "guardrail_violations": violations, "baseline": base,
        "decision": "go" if not reasons else "no-go", "reasons": reasons,
    }
    return result


def write_report(ws: Workspace, rep: dict) -> str:
    end = rep["period"][-1] if rep["period"] else datetime.now(timezone.utc).date().isoformat()
    lead = rep["agent_lead_minutes"]
    lines = [f"# Shadow-mode report - {rep['product']} - {' to '.join(rep['period']) or 'no ticks'}", "",
             f"**Data origin:** {rep['data_origin']}" + (" (SYNTHETIC: rehearsal, not evidence for go-live on a real "
                                                         "product)" if rep["synthetic"] else ""),
             f"**Decision by the go-live rule:** {rep['decision'].upper()}", "",
             "| Measure | Value | Target |", "|---|---|---|",
             f"| Shadow period | {rep['days']} day(s), {rep['snapshots']} snapshot(s) | at least {MIN_DAYS} days |",
             f"| Diagnosis accuracy (human-confirmed) | {rep['diagnosis_accuracy']} over {len(rep['diagnosis_pairs'])} "
             f"incident(s) | {TARGETS['diagnosis_accuracy']} or higher, at least {MIN_CONFIRMED} incidents |",
             f"| Alert precision (judged by people) | {rep['alert_precision']} ({rep['judged']} of {rep['agent_cases']} "
             f"judged) | {TARGETS['alert_precision']} or higher |",
             f"| Unsupported causes | {rep['unsupported_causes']} | 0 |",
             f"| Guardrail violations (anything executed) | {rep['guardrail_violations']} | 0 |",
             f"| Tickets the agents missed | {len(rep['missed_tickets'])} | reported |",
             f"| Agent detection ahead of the team's ticket (min) | "
             f"{(sum(lead) / len(lead)) if lead else '-'} average | reported |",
             f"| Human minutes per case | {rep['human_minutes_per_case']} | tracked |",
             f"| Baseline (START HERE) | {rep['baseline']} | comparison |", "",
             "## Agent diagnosis against the team's conclusion", "| Case | Ticket | Agent | People | Match |",
             "|---|---|---|---|---|",
             *[f"| {p['case']} | {p['ticket'] or '-'} | {p['agent']} | {p['human']} | {'yes' if p['match'] else 'NO'} |"
               for p in rep["diagnosis_pairs"]], "",
             "## Why not go" if rep["reasons"] else "## All go-live conditions met",
             *[f"- {r}" for r in rep["reasons"]], "",
             "A GO here allows a go-live request at OG7. It raises nothing by itself: each action type is "
             "still promoted one at a time, at OG7, from the platform's own track record.", ""]
    path = f"shadow/report-{end}.md"
    ws.write("shadow", path, "\n".join(lines))
    ws.write("shadow", f"shadow/report-{end}.json", json.dumps(rep, indent=1, default=str))
    return path


# -- go or no-go (O5-4) -------------------------------------------------------------------------------
def request_go_live(ws: Workspace, report_path: str) -> str:
    rep = json.loads(ws.read(report_path.replace(".md", ".json")))
    if rep["decision"] != "go":
        raise GuardViolation("the go-live rule is not met: " + "; ".join(rep["reasons"]))
    if rep["synthetic"]:
        raise GuardViolation("a synthetic (rehearsal) shadow period cannot support go-live for a real product")
    from ..agents.commander import CommanderAgent
    req = CommanderAgent(ws, None).request_approval(
        gate="OG7", decision_asked=f"Go-live for {ws.dp_id}: allow action types to be proposed above L1",
        artifact_hash=file_hash(ws.path(report_path)), owner_agent="adef-ops-problem-knowledge",
        evidence=[report_path])
    ws.write_yaml("shadow", "shadow/go-live-request.yaml", {"request": req.id, "report": report_path})
    return req.id


def go_live_status(ws: Workspace) -> tuple[bool, str]:
    doc = ws.read_yaml("shadow/go-live-request.yaml")
    if not doc:
        return False, "no go-live decision requested"
    appr = Approvals(ws)
    try:
        appr.require(doc["request"], gate="OG7", artifact_hash=file_hash(ws.path(doc["report"])))
    except ApprovalError as exc:
        return False, str(exc)
    return True, f"go-live {doc['request']} approved"


def apply_go_live(ws: Workspace) -> None:
    ok, why = go_live_status(ws)
    if not ok:
        raise GuardViolation(f"go-live not approved: {why}")
    svc = ws.read_yaml("control/service.yaml")
    svc["mode"] = "live"
    ws.write_yaml("commander", "control/service.yaml", svc)
