"""The live run view: every issue of the night as a pipeline of agent stages, each with the
agent's output and the person it is waiting for. Built for demonstrating how work moves from
one agent to the next through human checkpoints."""

from __future__ import annotations

STAGE_OF = {"detect": "detect", "ack": "triage", "diagnosis": "diagnose", "plan": "plan", "approve": "fix", "manual": "fix", "blocked": "fix",
            "resolve": "resolve", "comms": "inform", "pir": "learn", "problem": "learn", "kedb": "learn", "major": "triage"}


def _stage(key, title, agent, does, output, people, state):
    return {"key": key, "title": title, "agent": agent, "does": does, "output": [o for o in output if o], "people": people, "state": state, "asks": []}


def _event_lines(events):
    return [f"{e['first_seen'][11:16]}  {e['source']}: {e['message'][:160]}" + (f"  (seen {e['count']} times)" if e["count"] > 1 else "") for e in events]


def build(ops) -> dict:
    env = ops.env
    notes = [n for n in ops.store.all("note") if n["status"] == "open" and n["kind"] != "fyi"]
    events = ops.store.all("event")
    issues = []
    for ev in events:                       # detected, not yet a ticket
        if ev.get("pending") and ev["status"] == "open" and not ev.get("pending_with"):
            group = [ev] + [e for e in events if e.get("pending_with") == ev["id"]]
            st = _stage("detect", "Detect", "Watch Desk Agent", "Reads every tool, merges repeats and shows what is new",
                        _event_lines(group), "L1 Operator confirms it is a real issue", "waiting")
            st["asks"] = [n for n in notes if n["kind"] == "detect" and n["data"].get("event") == ev["id"]]
            issues.append({"id": ev["id"], "title": ev["message"][:120], "priority": None, "state": "Detected", "ticket": None, "stages": [st] + [
                _stage(k, t, a, d, [], "", "pending") for k, t, a, d in REST]})
    for inc in ops.tickets("incident"):
        evs = [ops.store.need("event", e) for e in inc["events"]]
        d, plan = inc["diagnosis"], inc["plan"]
        first = evs[0]
        svc = [f"{env.services[s]['name']} ({env.service_due(s)})" for s in inc["services"]]
        stages = [_stage("detect", "Detect", "Watch Desk Agent", "Reads every tool, merges repeats and shows what is new", _event_lines(evs),
                         f"Confirmed by the {first['confirmed_by']}" if first.get("confirmed_by") else "No confirmation step in fast mode", "done")]
        stages.append(_stage("triage", "Triage", "Triage Agent", "One ticket per cause, business impact, priority, routing", [
            f"Opened {inc['id']} as {inc['priority']} (impact {inc['impact']}, urgency {inc['urgency']})",
            f"{len(evs)} alert(s) from {len({e['source'] for e in evs})} tool(s) grouped into this one ticket" if len(evs) > 1 else "",
            "Business services exposed: " + ("; ".join(svc) or "none tonight"),
            f"Routed to {env.tower_name(inc['tower'])}: {inc['assignee']}"],
            f"Acknowledged by the {inc['assignee']} at {inc['ack_at'][11:16]}" if inc["ack_at"] else f"{inc['assignee']} acknowledges", "done" if inc["ack_at"] else "waiting"))
        stages.append(_stage("diagnose", "Diagnose", "Diagnosis Agent", "Matches a known error and runs read-only checks", ([
            f"Cause ({d['status']}{', ' + d['ke'] if d.get('ke') else ''}): {d['cause']}"] + [
            f"{'✓' if e['confirms'] else '•'} {e['check']}: {e['result']}" for e in d["evidence"]]) if d else [],
            (f"Agreed by the {d['agreed_by']}" if d and d.get("agreed_by") not in (None, "not needed") else "Engineer agrees with the diagnosis" if d and not d.get("agreed_by") else
             "No agreement step: handed to the engineer" if d else ""), "done" if d and d.get("agreed_by") else "waiting" if d else "pending"))
        steps = plan["steps"] if plan else []
        stages.append(_stage("plan", "Plan", "Resolution Agent", "Turns the runbook into steps and marks who must approve each",
                             [f"{s['n']}. {s['title']}" + (f"  [needs the {s['approver']}]" if s.get("approver") else "  [by the engineer]" if s["kind"] == "manual" else "") for s in steps],
                             (f"Accepted by the {plan['accepted_by']}" if plan and plan.get("accepted_by") else "Engineer accepts the plan; nothing has changed yet" if plan else ""),
                             "done" if plan and plan["status"] != "proposed" else "waiting" if plan else "pending"))
        started = plan and plan["status"] != "proposed"
        ran = [s for s in steps if s["status"] != "pending"]
        stages.append(_stage("fix", "Fix and recover", "Resolution Agent", "Runs approved steps, restarts downstream jobs, verifies recovery",
                             [f"{s['n']}. {s['title']}: {s['status']}" + (f" - {s['result']}" if s["result"] else "") + (f" (approved by the {s['approved_by']})" if s.get("approved_by") else "")
                              for s in ran] if started else [],
                             "Approvals by the named role for change, security, cost, data and BI steps",
                             "done" if plan and plan["status"] == "done" else "blocked" if plan and plan["status"] == "blocked" else "working" if started else "pending"))
        stages.append(_stage("resolve", "Resolve", "Resolution Agent", "Asks the engineer to confirm; the engineer closes",
                             [f"Resolved at {inc['resolved_at'][11:16]}: {inc['resolution']['code']}",
                              f"Response SLA {'met' if inc['sla']['respond_met'] else 'missed'}, resolution SLA {'met' if inc['sla']['resolve_met'] else 'missed'}"] if inc["state"] == "Resolved" else [],
                             f"Confirmed by the {inc['resolution']['by']}" if inc["state"] == "Resolved" else "Engineer confirms the incident is resolved",
                             "done" if inc["state"] == "Resolved" else "waiting" if plan and plan["status"] == "done" else "pending"))
        comms = inc.get("comms", [])
        stages.append(_stage("inform", "Inform the client", "Comms Agent", "Drafts stakeholder messages from the ticket",
                             [f"{c['stage'].capitalize()} message: {c['status']}" + (f" (approved by the {c['sent_by']}, sent to {', '.join(c['to'])})" if c["sent_by"] else "") for c in comms],
                             "Incident Manager approves every message" if inc["priority"] in ("P1", "P2") else "Only for P1 and P2 incidents",
                             "skipped" if not comms and inc["priority"] not in ("P1", "P2") else "pending" if not comms else
                             "waiting" if any(c["status"] == "draft" for c in comms) else "done"))
        learn = ([f"Post-incident review: {inc['pir']['status']}"] if inc.get("pir") else []) + \
                [f"Problem record {p['id']}: {p['stage']} ({p['occurrences']} occurrences in 30 days)" for p in ops.tickets("problem") if inc["id"] in p.get("incidents", [])] + \
                [f"New known error {k['id']} drafted from the engineer's notes" for k in ops.store.all("ke_draft") if k.get("learned_from") == inc["id"]]
        stages.append(_stage("learn", "Learn", "Problem and Knowledge Agent", "Review, problem record for recurring causes, new known errors", learn,
                             "Service Delivery Manager and the engineer approve", "pending" if inc["state"] != "Resolved" else "done"))
        related = {inc["id"]} | {p["id"] for p in ops.tickets("problem") if inc["id"] in p.get("incidents", [])}
        for n in notes:
            if n["ticket"] in related and n["kind"] in STAGE_OF:
                st = next(s for s in stages if s["key"] == STAGE_OF[n["kind"]])
                st["asks"].append(n)
                st["state"] = "waiting"
        issues.append({"id": inc["id"], "title": inc["title"], "priority": inc["priority"], "state": inc["state"], "ticket": inc["id"], "stages": stages})
    others = [n for n in notes if not any(n in s["asks"] for i in issues for s in i["stages"])]
    waiting = sum(len(s["asks"]) for i in issues for s in i["stages"]) + len(others)
    proj = ops.projection.get("services", {})
    return {"now": ops.now.isoformat(), "guided": ops.guided, "issues": issues, "other_asks": others, "waiting": waiting,
            "services": list(proj.values()), "over": ops.now.hour >= 8,
            "watch": ops.store.kv("watch_stats") or {}}


REST = [("triage", "Triage", "Triage Agent", "One ticket per cause, business impact, priority, routing"),
        ("diagnose", "Diagnose", "Diagnosis Agent", "Matches a known error and runs read-only checks"),
        ("plan", "Plan", "Resolution Agent", "Turns the runbook into steps and marks who must approve each"),
        ("fix", "Fix and recover", "Resolution Agent", "Runs approved steps, restarts downstream jobs, verifies recovery"),
        ("resolve", "Resolve", "Resolution Agent", "Asks the engineer to confirm; the engineer closes"),
        ("inform", "Inform the client", "Comms Agent", "Drafts stakeholder messages from the ticket"),
        ("learn", "Learn", "Problem and Knowledge Agent", "Review, problem record for recurring causes, new known errors")]
