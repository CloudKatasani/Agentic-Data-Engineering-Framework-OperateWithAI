"""Problem and Knowledge Agent. After an incident is resolved it writes the post-incident
review for P1 and P2, opens a problem record when the same known error keeps coming back,
and drafts a new known error when an engineer fixed something that was not in the database.
People approve each of these before it counts."""

from __future__ import annotations

import re
from datetime import datetime

from ..engine.history import occurrences

NAME = "Problem and Knowledge Agent"


def _mins(a: str, b: str) -> int:
    return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60)


def _pir(ops, inc: dict) -> str:
    ke, plan = ops.kb.get(inc["ke"]), inc["plan"] or {"steps": []}
    first = ops.store.need("event", inc["root_event"])
    steps = plan["steps"]
    by_agent = sum(1 for s in steps if s["kind"] in ("action", "verify", "recovery") and s["status"] == "done")
    by_people = sum(1 for s in steps if s["kind"] == "manual" and s["status"] == "done")
    approvals = [f"- Step {s['n']} approved by the {s['approved_by']}" for s in steps if s.get("approved_by")]
    svc = [f"- {s['name']}: {s['status']} (due {s['deadline'][11:16]}, estimate or actual {s['eta'][11:16]})"
           for sid, s in (ops.projection.get("services") or {}).items() if sid in inc["services"]]
    late = [t for t in inc["timeline"] if "escalated" in t["text"]]
    improve = ([f"- {len(late)} request(s) to people passed their response time and were escalated."] if late else []) + \
              ([] if inc["sla"]["respond_met"] else ["- The response SLA was missed."]) + ([] if inc["sla"]["resolve_met"] else ["- The resolution SLA was missed."])
    return "\n".join([
        f"# Post-incident review: {inc['id']}", f"**{inc['title']}** ({inc['priority']}, {ops.env.tower_name(inc['tower'])})", "",
        "## Timeline", f"- {first['first_seen'][11:16]} first alert from {first['source']}: {first['message'][:140]}",
        f"- {inc['opened_at'][11:16]} ticket opened and routed to {inc['assignee']}",
        f"- {inc['ack_at'][11:16] if inc['ack_at'] else 'not'} acknowledged ({_mins(inc['opened_at'], inc['ack_at']) if inc['ack_at'] else '-'} min; SLA {'met' if inc['sla']['respond_met'] else 'missed'})",
        f"- {inc['resolved_at'][11:16]} resolved ({_mins(inc['opened_at'], inc['resolved_at'])} min; SLA {'met' if inc['sla']['resolve_met'] else 'missed'})", "",
        "## Business impact", *(svc or ["- No business service was affected."]), "",
        "## Cause", (inc["diagnosis"] or {}).get("cause", ""), "",
        "## What was done", *[f"{s['n']}. {s['title']}: {s['result']}" for s in steps], *approvals,
        f"- {by_agent} step(s) carried out by agents, {by_people} by people; {len(inc['events'])} alert(s) handled in one ticket.", "",
        "## What to improve", *(improve or ["- Response and resolution were inside SLA; no process finding."]), "",
        "## Permanent fix", (ke or {}).get("permanent_fix") or "To be decided by the tower lead.",
    ])


def _fragment(message: str) -> str:
    parts = [p.strip() for p in re.split(r":\s+", message) if p.strip()]
    keyed = [p for p in parts if re.search(r"exhaust|unable|denied|timeout|timed out|expired|refused|not found|invalid|failed", p, re.I)]
    frag = re.sub(r"\(.*$", "", (keyed[-1] if keyed else parts[-1])).strip()
    return frag[:60].lower()


def run(ops) -> None:
    for inc in ops.tickets("incident"):
        if inc["state"] != "Resolved" or inc.get("reviewed"):
            continue
        inc["reviewed"] = True
        ke = ops.kb.get(inc["ke"])
        if inc["priority"] in ("P1", "P2") or inc["major"]:
            inc["pir"] = {"text": _pir(ops, inc), "status": "draft", "approved_by": None}
            ops.note(inc, NAME, "Drafted the post-incident review for the service delivery manager")
            ops.notify(ops.env.holder("sdm"), "pir", inc, f"Review the post-incident review for {inc['id']}", inc["pir"]["text"],
                       [{"id": "approve", "label": "Approve review", "style": "primary"}], NAME, due_minutes=480, role="service delivery manager")
        if ke and ke.get("permanent_fix"):
            past = occurrences(ops, ke["id"])
            tonight = sum(1 for t in ops.tickets("incident") if t["ke"] == ke["id"])
            exists = any(p["ke"] == ke["id"] for p in ops.tickets("problem"))
            if past + tonight >= 2 and not exists:
                p = ops.open_ticket("problem", f"Recurring: {ke['title']}", "P3", ke["tower"], NAME, ke=ke["id"], impact="medium", urgency="low",
                                    occurrences=past + tonight, permanent_fix=ke["permanent_fix"], incidents=[inc["id"]], stage="proposed")
                ops.note(p, NAME, f"{past} time(s) in the last 30 days plus {inc['id']} tonight. Proposed permanent fix: {ke['permanent_fix']}")
                ops.notify(ops.env.holder("sdm"), "problem", p, f"Recurring problem: {ke['title']} ({past + tonight} times in 30 days)",
                           f"Each occurrence costs an incident and puts a deadline at risk.\n\nProposed permanent fix: {ke['permanent_fix']}\n\n"
                           "Accept to put it in the problem backlog and raise it with the client.",
                           [{"id": "accept", "label": "Accept into backlog", "style": "primary"}, {"id": "decline", "label": "Decline", "input": "Reason"}],
                           NAME, due_minutes=1440, role="service delivery manager")
        if not ke and inc.get("manual_notes"):
            ev = ops.store.need("event", inc["root_event"])
            tool = ops.env.cis[ev["ci"]]["tool"]
            draft = {"id": f"KE{ops.store.next_number('KE', 200):04d}", "title": f"{ops.env.cis[ev['ci']]['name']}: {_fragment(ev['message'])}",
                     "tool": tool.capitalize(), "tower": inc["tower"], "signatures": [f"text:{_fragment(ev['message'])}"], "typical_fix_minutes": 30,
                     "cause": f"First seen in {inc['id']}. Engineer's finding: {inc['manual_notes']}",
                     "plan": [{"kind": "manual", "fixes": True, "title": "Apply the fix recorded from the first occurrence", "guidance": inc["manual_notes"]}],
                     "permanent_fix": "", "learned_from": inc["id"]}
            ops.store.put("ke_draft", draft)
            ops.note(inc, NAME, f"This error was not in the known error database. Drafted {draft['id']} from the engineer's notes for approval")
            ops.notify(inc["resolution"]["by"], "kedb", inc, f"Add a new known error from {inc['id']}?",
                       f"Title: {draft['title']}\nMatches alerts containing: \"{draft['signatures'][0][5:]}\"\nCause and fix: {draft['cause']}\n\n"
                       "If you add it, the next occurrence is recognised and your notes are offered as the runbook.",
                       [{"id": "add", "label": "Add to known errors", "style": "primary"}, {"id": "discard", "label": "Discard"}],
                       NAME, due_minutes=1440, role="Resolver", data={"draft": draft["id"]})
        ops.save(inc)


def decide(ops, n: dict, t: dict, actor: str, action: str, text: str) -> None:
    if n["kind"] == "pir":
        t["pir"].update(status="approved", approved_by=actor)
        ops.note(t, actor, "Approved the post-incident review", human=True)
    elif n["kind"] == "problem":
        if action == "accept":
            t.update(state="Assigned", stage="in backlog")
            ops.note(t, actor, f"Accepted into the problem backlog; owner {t['assignee']}", human=True)
        else:
            t["stage"] = "declined"
            ops.resolve(t, actor, "Declined", text or "Not taken forward")
    elif n["kind"] == "kedb":
        draft = ops.store.need("ke_draft", n["data"]["draft"])
        if action == "add":
            draft["approved_by"] = actor
            ops.store.put("ke", draft)
            ops.note(t, actor, f"Added {draft['id']} to the known error database", human=True)
        else:
            ops.note(t, actor, f"Discarded the draft known error {draft['id']}", human=True)
    ops.save(t)
