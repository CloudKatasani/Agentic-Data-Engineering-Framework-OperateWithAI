"""Resolution Agent. Turns the runbook of the confirmed known error into a step-by-step
plan, puts it in front of the assigned engineer, and after they accept it carries it out:
standard steps itself, approval steps only after the named approver says yes, manual steps
by handing them to a person. Then it restarts what failed downstream and checks recovery.
Nothing changes in the client estate before a named person has accepted the plan."""

from __future__ import annotations

from datetime import datetime, timedelta

from .diagnosis import context, fmt

NAME = "Resolution Agent"
RERUN = {"infa": "informatica.restart_workflow", "dbt": "dbt.retry_job", "denodo": "denodo.reload_cache",
         "pbi": "powerbi.refresh_dataset", "cognos": "cognos.rerun_schedule"}
RECOVERY = {"kind": "recovery", "title": "Restart failed downstream jobs in dependency order and confirm they complete", "class": "standard"}
BTN_OK = {"style": "primary"}


def _owner_role(ops, inc: dict, role: str) -> str:
    if role != "data_owner":
        return role
    return ops.env.services[inc["services"][0]]["owner_role"] if inc["services"] else "data_owner_finance"


def _steps(ops, inc: dict, raw: list[dict]) -> list[dict]:
    ctx, out = context(ops, inc), []
    for i, s in enumerate(raw + [RECOVERY]):
        step = {"n": i + 1, "kind": s["kind"], "title": s["title"], "status": "pending", "result": "", "class": s.get("class", "")}
        if s["kind"] == "action":
            act = ops.kb.actions[s["action"]]
            step.update(action=s["action"], args=fmt(s.get("args"), ctx), how=act["how"])
            step["class"] = act["class"]
            if act["class"] == "approval":
                role = _owner_role(ops, inc, act["approver_role"])
                step.update(approver_role=role, approver=ops.env.holder(role), why=act["why"])
        elif s["kind"] == "manual":
            step.update(guidance=s.get("guidance", ""), fixes=s.get("fixes", False))
        elif s["kind"] == "verify":
            step.update(probe=s["probe"], args=fmt(s.get("args"), ctx), expect=s["expect"], timeout=s.get("timeout", 30))
        out.append(step)
    return out


def _manual_plan(ops, inc: dict, why: str) -> None:
    raw = [{"kind": "manual", "fixes": True, "title": "Investigate and fix by hand, then record what you did",
            "guidance": why + " Your notes become the resolution record and, if this error is new, a draft known error."}]
    inc["plan"] = {"status": "manual", "steps": _steps(ops, inc, raw), "source": "engineer", "accepted_by": inc["assignee"]}


def propose(ops) -> None:
    for inc in ops.tickets("incident", open_only=True):
        if inc["plan"] or not inc["diagnosis"]:
            continue
        ke, d = ops.kb.get(inc["ke"]), inc["diagnosis"]
        usable = ke and d["status"] in ("confirmed", "likely", "recognised")
        if usable:
            inc["plan"] = {"status": "proposed", "steps": _steps(ops, inc, ke["plan"]), "source": ke["id"], "accepted_by": None}
            approvals = [f"{s['approver']} ({s['approver_role'].replace('_', ' ')}) for step {s['n']}" for s in inc["plan"]["steps"] if s.get("approver")]
            body = (f"Diagnosis ({d['status']}): {d['cause']}\n\nPlan from runbook {ke['id']}:\n"
                    + "\n".join(f"{s['n']}. {s['title']}" + (f"  [needs {s['approver']}]" if s.get("approver") else "") for s in inc["plan"]["steps"])
                    + ("\n\nI will ask " + "; ".join(approvals) + " after you accept." if approvals else "")
                    + "\n\nNothing is changed until you accept.")
            ops.note(inc, NAME, f"Proposed a {len(inc['plan']['steps'])}-step plan from runbook {ke['id']}; waiting for {inc['assignee']} to review")
            ops.close_notes(inc["id"], kinds=("ack",), reason="replaced by plan review")
            ops.notify(inc["assignee"], "plan", inc, f"Review the fix plan for {inc['id']} ({inc['priority']}): {inc['title']}", body,
                       [{"id": "accept", "label": "Accept plan", **BTN_OK}, {"id": "manual", "label": "I will fix it myself", "input": "Why (optional)"}],
                       NAME, due_minutes=ops.env.sla(inc["priority"])["respond"], role="Assigned engineer")
        else:
            why = ("No runbook matches this error." if not ke else "The known error's checks did not confirm its usual cause.")
            _manual_plan(ops, inc, why)
            ops.note(inc, NAME, f"{why} Handed to {inc['assignee']} to investigate; I will restart downstream jobs once it is fixed.")
            ops.save(inc)


def take_over(ops, inc: dict, person: str, why: str) -> None:
    _manual_plan(ops, inc, f"{person} chose to fix this by hand" + (f": {why}." if why else "."))
    inc["plan"]["accepted_by"] = person
    ops.note(inc, person, "Took over the fix by hand" + (f": {why}" if why else ""), human=True)


# ------------------------------------------------------------------------------ carry out
def _recover(ops, inc: dict, step: dict) -> bool:
    """Restart failed jobs in this incident's part of the chain. True when nothing is left."""
    env, bs = ops.env, ops.estate.batch_status()
    scope = set(inc["impact_nodes"])
    reruns = step.setdefault("reruns", {})
    jobs = sorted((j for j in bs["jobs"] if j in scope), key=lambda j: env.depth[j])
    pending = False
    for job in jobs:
        st, n = bs["jobs"][job], env.nodes[job]
        builds = [m for m in n.get("builds", []) if m in scope and m not in bs["built"]]
        failed = st["state"] == "failed" and (not n.get("builds") or builds)
        if failed:
            ready = any(all(bs["ready"].get(u) for u in env.up[m]) for m in builds) if n.get("builds") else all(bs["ready"].get(u) for u in env.up[job])
            if ready and reruns.get(job, 0) >= 2:
                step.update(status="failed", result=f"{n['name']} failed again after two restarts: {st['error'][:120]}")
                return False
            if ready:
                res = ops.estate.apply(RERUN[job.split(".")[0]], node=job)
                reruns[job] = reruns.get(job, 0) + 1
                ops.note(inc, NAME, f"Recovery: {res['message']}")
            pending = True
        elif st["state"] == "waiting" or (st["state"] == "running" and st["planned"] <= bs["now"]):
            pending = True
    if not pending:
        step["result"] = ("Restarted: " + ", ".join(env.nodes[j]["name"] for j in reruns)) if reruns else "Nothing downstream needed a restart"
    return not pending


def advance(ops, inc: dict) -> None:
    plan = inc["plan"]
    if not plan or plan["status"] not in ("accepted", "manual"):
        return
    for step in plan["steps"]:
        if step["status"] in ("done", "skipped"):
            continue
        if step["status"] == "failed":
            break
        if step["kind"] == "action":
            if step["class"] == "approval" and not step.get("approved_by"):
                if step["status"] != "waiting approval":
                    step["status"] = "waiting approval"
                    ops.note(inc, NAME, f"Step {step['n']} needs approval from {step['approver']} ({step['approver_role'].replace('_', ' ')})")
                    ops.notify(step["approver"], "approve", inc, f"Approve: {step['title']} ({inc['id']}, {inc['priority']})",
                               f"Why this needs you: {step['why']}.\nHow it is done: {step['how']}.\n\nIncident: {inc['title']}\n"
                               f"Cause: {inc['diagnosis']['cause']}\nPlan accepted by {plan['accepted_by']}.",
                               [{"id": "approve", "label": "Approve", **BTN_OK}, {"id": "reject", "label": "Decline", "input": "Reason", "style": "bad"}],
                               NAME, due_minutes=15 if inc["priority"] in ("P1", "P2") else 30, step=step["n"], role=step["approver_role"].replace("_", " "))
                break
            res = ops.estate.apply(step["action"], **step["args"])
            step.update(status="done" if res["ok"] else "failed", result=res["message"], at=ops.now.isoformat())
            ops.note(inc, NAME, f"Step {step['n']} {'done' if res['ok'] else 'FAILED'}: {res['message']}")
            if not res["ok"]:
                break
        elif step["kind"] == "manual":
            if step["status"] != "waiting for person":
                step["status"] = "waiting for person"
                ops.notify(inc["assignee"], "manual", inc, f"Your step on {inc['id']}: {step['title']}", step.get("guidance", ""),
                           [{"id": "done", "label": "Done", "input": "What you did / what you found", "required": True, **BTN_OK}],
                           NAME, due_minutes=60, step=step["n"], role="Assigned engineer")
            break
        elif step["kind"] == "verify":
            step.setdefault("started", ops.now.isoformat())
            res = ops.estate.probe(step["probe"], **step["args"])
            if all(res["facts"].get(k) == v for k, v in step["expect"].items()):
                step.update(status="done", result=res["summary"], at=ops.now.isoformat())
                ops.note(inc, NAME, f"Step {step['n']} verified: {res['summary']}")
            elif ops.now > datetime.fromisoformat(step["started"]) + timedelta(minutes=step["timeout"]):
                step.update(status="failed", result=f"Not true after {step['timeout']} min: {res['summary']}")
            else:
                step.update(status="checking", result=res["summary"])
                break
        elif step["kind"] == "recovery":
            step["status"] = "running"
            if _recover(ops, inc, step):
                step.update(status="done", at=ops.now.isoformat())
                ops.note(inc, NAME, f"Recovery complete. {step['result']}")
            else:
                break
        if step["status"] == "failed":
            break
    failed = next((s for s in plan["steps"] if s["status"] == "failed"), None)
    if failed and plan["status"] != "blocked":
        plan["status"] = "blocked"
        ops.note(inc, NAME, f"Plan stopped at step {failed['n']}: {failed['result']}")
        ops.notify(inc["assignee"], "blocked", inc, f"Plan stopped on {inc['id']}: step {failed['n']} did not work", failed["result"],
                   [{"id": "retry", "label": "Try the step again"}, {"id": "manual", "label": "I will fix it myself", "input": "Why (optional)"}],
                   NAME, due_minutes=15, role="Assigned engineer")
    elif all(s["status"] in ("done", "skipped") for s in plan["steps"]):
        plan["status"] = "done"
        summary = "\n".join(f"{s['n']}. {s['title']}: {s['result']}" for s in plan["steps"])
        ops.note(inc, NAME, f"All steps complete; asked {inc['assignee']} to confirm the incident is resolved")
        ops.notify(inc["assignee"], "resolve", inc, f"Confirm {inc['id']} is resolved", summary,
                   [{"id": "confirm", "label": "Confirm resolved", **BTN_OK}, {"id": "reopen", "label": "Not fixed", "input": "What is still wrong", "required": True}],
                   NAME, due_minutes=30, role="Assigned engineer")
    ops.save(inc)


def run(ops) -> None:
    propose(ops)
    for inc in ops.tickets("incident", open_only=True):
        advance(ops, inc)


# ------------------------------------------------------------------------------ people
def decide(ops, n: dict, inc: dict, actor: str, action: str, text: str) -> None:
    plan = inc["plan"]
    if n["kind"] == "plan":
        ops.acknowledge(inc, actor)
        if action == "accept":
            plan.update(status="accepted", accepted_by=actor)
            ops.note(inc, actor, f"Accepted the plan from runbook {plan['source']}", human=True)
        else:
            take_over(ops, inc, actor, text)
    elif n["kind"] == "approve":
        step = next(s for s in plan["steps"] if s["n"] == n["step"])
        if action == "approve":
            step.update(approved_by=actor, approved_at=ops.now.isoformat(), status="approved")
            ops.note(inc, actor, f"Approved step {step['n']}: {step['title']}", human=True)
        else:
            step.update(status="failed", result=f"Declined by {actor}: {text or 'no reason given'}")
            ops.note(inc, actor, f"Declined step {step['n']}: {text or 'no reason given'}", human=True)
    elif n["kind"] == "manual":
        step = next(s for s in plan["steps"] if s["n"] == n["step"])
        step.update(status="done", result=f"{actor}: {text}", at=ops.now.isoformat())
        if step.get("fixes"):
            ops.estate.apply("manual.fix", node=context(ops, inc)["node"])
            inc["manual_notes"] = text
        ops.note(inc, actor, f"Completed step {step['n']} by hand: {text}", human=True)
    elif n["kind"] == "blocked":
        if action == "retry":
            for s in plan["steps"]:
                if s["status"] == "failed":
                    s.update(status="pending", result="")
                    s.pop("started", None)
                    s.pop("reruns", None)
            plan["status"] = "accepted" if plan["source"] != "engineer" else "manual"
            ops.note(inc, actor, "Asked for the failed step to be tried again", human=True)
        else:
            take_over(ops, inc, actor, text)
    elif n["kind"] == "resolve":
        if action == "confirm":
            code = "Solved by runbook" if plan["source"] != "engineer" else "Solved by engineer"
            ops.resolve(inc, actor, code, (inc.get("manual_notes") or (ops.kb.get(inc["ke"]) or {}).get("cause", "")))
            return
        take_over(ops, inc, actor, text)
    advance(ops, inc)
