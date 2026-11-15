"""Batch and SLA Agent. Re-plans the night every cycle: from the schedulers' current state
and the open incidents it projects when each business service will be delivered, warns the
service delivery manager and incident manager before a deadline is missed, and raises the
urgency of the incident that is in the way."""

from __future__ import annotations

from ..engine.batch import project

NAME = "Batch and SLA Agent"
SEVERITY = ["on track", "at risk", "breach predicted", "breached"]
NOTED = [{"id": "dismiss", "label": "Noted"}]


def run(ops) -> None:
    env = ops.env
    proj = project(ops)
    prev = ops.store.kv("service_status") or {}
    for sid, s in proj["services"].items():
        old, new = prev.get(sid, "on track"), s["status"]
        if new == old:
            continue
        eta, due = s["eta"][11:16], s["deadline"][11:16]
        why = f" Blocked by {', '.join(s['blocking'])}." if s["blocking"] else ""
        ops.audit(NAME, f"{sid}: {old} -> {new}", sid, f"ETA {eta}, deadline {due}.{why}")
        if new in SEVERITY and SEVERITY.index(new) > max(SEVERITY.index(old) if old in SEVERITY else 0, 0):
            for role in ("incident_manager", "sdm") if new != "at risk" else ("incident_manager",):
                ops.notify(env.holder(role), "fyi", None, f"{s['name']}: {new}",
                           f"Deadline {due}. Current estimate {eta} ({-s['slack_minutes']} min late)." if s["slack_minutes"] < 0 else
                           f"Deadline {due}. Current estimate {eta} ({s['slack_minutes']} min of slack).{why}",
                           NOTED, NAME, due_minutes=60, role=role.replace("_", " "), data={"service": sid})
    ops.store.kv("service_status", {sid: s["status"] for sid, s in proj["services"].items()})
    for inc in ops.tickets("incident", open_only=True):
        live = [proj["services"][s] for s in inc["services"] if "delivered" not in proj["services"][s]["status"]]
        if not live:
            continue
        slack = min(s["slack_minutes"] for s in live)
        urgency = "high" if slack < 60 else "medium"
        if urgency == "high" and inc["urgency"] != "high":
            inc["urgency"] = "high"
            worst = min(live, key=lambda s: s["slack_minutes"])
            ops.set_priority(inc, env.priority(inc["impact"], "high"), NAME,
                             f"{worst['name']} now estimated {worst['eta'][11:16]} against a {worst['deadline'][11:16]} deadline")
            if inc["priority"] == "P1" and not inc["major"]:
                inc["major"] = True
                ops.save(inc)
                ops.notify(env.holder("incident_manager"), "major", inc, f"Major incident: {inc['id']} is now P1", 
                           f"{inc['title']}\nAssigned to {inc['assignee']}. {worst['name']} is estimated {worst['eta'][11:16]} against {worst['deadline'][11:16]}.",
                           [{"id": "lead", "label": "I am leading this", "style": "primary"}], NAME, due_minutes=15, role="incident manager")
    ops.projection = proj
