"""Service Reporting Agent. Writes the documents a managed service produces every day:
the morning health check, the shift handover and the service review. All of them are built
from tickets, the audit log and the batch projection, so they cannot drift from the facts."""

from __future__ import annotations

from datetime import datetime

from ..engine.history import history

NAME = "Service Reporting Agent"


def _mins(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60


def health(ops) -> dict:
    """One line per system: green, amber or red, with the reason."""
    env, proj = ops.env, ops.projection
    incs = ops.tickets("incident", open_only=True)
    events = [e for e in ops.store.all("event") if e["status"] == "open"]
    rows = []
    for ci in env.cis.values():
        mine = [i for i in incs if ci["id"] in i["cis"]]
        warn = [e for e in events if e["ci"] == ci["id"] and not e["incident"]]
        jobs = [j for j in proj["jobs"].values() if j["ci"] == ci["id"]]
        if mine:
            status, detail = "red", "; ".join(f"{i['id']} ({i['priority']}): {i['title']}" for i in mine)
        elif warn:
            status, detail = "amber", "; ".join(e["message"] for e in warn)
        elif any(j["late_minutes"] > 15 and j["state"] != "succeeded" for j in jobs):
            late = max(jobs, key=lambda j: j["late_minutes"])
            status, detail = "amber", f"{late['name']} is running {late['late_minutes']} min behind plan"
        else:
            done = sum(1 for j in jobs if j["state"] == "succeeded")
            status = "green"
            detail = (f"{done} of {len(jobs)} scheduled runs complete" if jobs else
                      f"Replication lag {proj['lag_minutes']:.0f} min" if ci["tool"] == "goldengate" else "No open alerts")
        rows.append({"ci": ci["id"], "name": ci["name"], "tool": ci["tool"], "tower": env.tower_name(ci["tower"]), "cloud": ci.get("cloud", ""),
                     "status": status, "detail": detail})
    return {"at": ops.now.isoformat(), "systems": rows, "services": list(proj["services"].values()),
            "summary": {s: sum(1 for r in rows if r["status"] == s) for s in ("green", "amber", "red")}}


def _next_step(inc: dict) -> str:
    plan = inc.get("plan")
    if not plan:
        return "Being diagnosed"
    if plan["status"] == "proposed":
        return f"Plan waiting for {inc['assignee']} to accept"
    step = next((s for s in plan["steps"] if s["status"] not in ("done", "skipped")), None)
    if not step:
        return f"Waiting for {inc['assignee']} to confirm resolution"
    who = step.get("approver") if step["status"] == "waiting approval" else inc["assignee"] if step["kind"] == "manual" else "Resolution Agent"
    return f"Step {step['n']} ({step['status']}): {step['title']} - with {who}"


def handover(ops) -> str:
    env, proj = ops.env, ops.projection
    incs = ops.tickets("incident")
    open_incs = [i for i in incs if i["state"] != "Resolved"]
    done = [i for i in incs if i["state"] == "Resolved"]
    asks = [n for n in ops.notes() if n["kind"] != "fyi"]
    reqs = ops.tickets("request", open_only=True)
    adv = [e for e in ops.store.all("event") if e["status"] == "open" and not e["incident"]]
    L = [f"# Shift handover - {env.client['name']}", f"Prepared by the {NAME} at {ops.now.strftime('%H:%M on %d %b %Y')}.", "",
         "## Business services", "| Service | Deadline | Estimate | Status |", "|---|---|---|---|"]
    L += [f"| {s['name']} | {s['deadline'][11:16]} | {s['eta'][11:16]} | {s['status']} |" for s in proj["services"].values()]
    L += ["", f"## Open incidents ({len(open_incs)})"]
    L += [f"- **{i['id']} {i['priority']}** {i['title']} - owner {i['assignee']}. {_next_step(i)}." for i in open_incs] or ["- None."]
    L += ["", f"## Waiting on people ({len(asks)})"]
    L += [f"- {n['escalated_to'] or n['to']}: {n['title']} (due {n['due'][11:16]}{', escalated' if n['escalated_to'] else ''})" for n in asks] or ["- Nothing."]
    L += ["", f"## Resolved this shift ({len(done)})"]
    L += [f"- {i['id']} {i['priority']} {i['title']} - {round(_mins(i['opened_at'], i['resolved_at']))} min, by {i['resolution']['by']}" for i in done] or ["- None."]
    L += ["", f"## Open requests ({len(reqs)})"]
    L += [f"- {r['id']} {r['title']} - {r['stage']}" for r in reqs] or ["- None."]
    L += ["", "## Watch items for the next shift"]
    L += [f"- {e['message']} ({e['handling'] or 'advisory'})" for e in adv] or ["- None."]
    late = [j for j in proj["jobs"].values() if j["state"] not in ("succeeded",) and j["late_minutes"] > 15]
    L += [f"- {j['name']} expected to finish {j['eta'][11:16]} ({j['late_minutes']} min behind plan)" for j in late]
    return "\n".join(L)


def review(ops) -> dict:
    """Service review: the last 30 days (history) plus tonight."""
    hist = history(ops)
    tonight = []
    for i in ops.tickets("incident"):
        if i["state"] == "Resolved":
            tonight.append({"days_ago": 0, "ke": i["ke"] or "new", "title": i["title"], "tower": i["tower"], "priority": i["priority"],
                            "mtta": round(_mins(i["opened_at"], i["ack_at"])) if i["ack_at"] else None, "mttr": round(_mins(i["opened_at"], i["resolved_at"])),
                            "respond_met": i["sla"]["respond_met"], "resolve_met": i["sla"]["resolve_met"]})
    rows = hist + tonight

    def stats(rs: list[dict]) -> dict:
        if not rs:
            return {"count": 0, "respond_pct": None, "resolve_pct": None, "mtta": None, "mttr": None}
        acked = [r for r in rs if r["mtta"] is not None]
        return {"count": len(rs), "respond_pct": round(100 * sum(bool(r["respond_met"]) for r in rs) / len(rs)),
                "resolve_pct": round(100 * sum(bool(r["resolve_met"]) for r in rs) / len(rs)),
                "mtta": round(sum(r["mtta"] for r in acked) / len(acked)) if acked else None, "mttr": round(sum(r["mttr"] for r in rs) / len(rs))}
    by_tower: dict[str, int] = {}
    by_ke: dict[str, dict] = {}
    for r in rows:
        by_tower[ops.env.tower_name(r["tower"])] = by_tower.get(ops.env.tower_name(r["tower"]), 0) + 1
        k = by_ke.setdefault(r["ke"], {"ke": r["ke"], "title": r["title"], "count": 0, "minutes": 0})
        k["count"] += 1
        k["minutes"] += r["mttr"]
    steps = [s for i in ops.tickets("incident") for s in (i.get("plan") or {}).get("steps", []) if s["status"] == "done"]
    log = ops.store.audit_log(limit=5000)
    watch = ops.store.kv("watch_stats") or {"raw": 0, "alerts": 0, "events": 0, "suppressed": 0}
    n_inc = len(ops.tickets("incident"))
    return {
        "period": "Last 30 days (sample history) plus tonight",
        "overall": stats(rows), "history": stats(hist), "tonight": stats(tonight),
        "by_priority": {p: stats([r for r in rows if r["priority"] == p]) for p in ("P1", "P2", "P3")},
        "by_tower": sorted(by_tower.items(), key=lambda x: -x[1]),
        "top_known_errors": sorted(by_ke.values(), key=lambda k: -k["count"])[:6],
        "problems": [{"id": p["id"], "title": p["title"], "stage": p["stage"], "occurrences": p["occurrences"]} for p in ops.tickets("problem")],
        "tonight_work": {
            "tool_reports_read": watch["raw"], "alerts": watch["alerts"], "distinct_alerts": watch["events"], "incidents": n_inc,
            "agent_actions": sum(1 for a in log if a["actor_type"] == "agent"), "human_decisions": sum(1 for a in log if a["actor_type"] == "person"),
            "steps_by_agents": sum(1 for s in steps if s["kind"] != "manual"), "steps_by_people": sum(1 for s in steps if s["kind"] == "manual"),
            "approvals": sum(1 for s in steps if s.get("approved_by")),
        },
    }
