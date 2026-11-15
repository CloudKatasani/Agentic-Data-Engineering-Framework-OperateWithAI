"""Service Request Agent. Works the request queue: checks each request against the
catalogue rules, collects the approvals the rules call for, fulfils the request with a
catalogue action and closes it. It also raises preventive requests from advisories, such as
a service account that is about to expire."""

from __future__ import annotations

from .resolution import RERUN

NAME = "Service Request Agent"


def raise_request(ops, cat_id: str, fields: dict, requester: str, by: str | None = None) -> dict:
    cat = ops.kb.requests[cat_id]
    subject = fields.get("role") or fields.get("account") or (ops.env.nodes.get(fields.get("node", ""), {}).get("name")) or ""
    who = f" for {fields['user']}" if fields.get("user") else ""
    t = ops.open_ticket("request", f"{cat['title']}: {subject}{who}", "P4", cat["tower"], by or requester, catalogue=cat_id,
                        fields=fields, requester=requester, approvals=[], stage="new", event=fields.pop("_event", None))
    return t


def _close(ops, t: dict, code: str, text: str) -> None:
    t["stage"] = code.lower()
    ops.resolve(t, NAME, code, text, human=False)
    for person in {t["requester"], ops.env.on_call(t["tower"])}:
        if ops.env.is_person(person):
            ops.notify(person, "fyi", t, f"{t['id']} {code.lower()}: {t['title']}", text, [{"id": "dismiss", "label": "Noted"}], NAME, due_minutes=240)


def _fulfil(ops, t: dict) -> None:
    cat, f = ops.kb.requests[t["catalogue"]], t["fields"]
    if t["catalogue"] == "SR-REPORT-RERUN":
        bs, node = ops.estate.batch_status(), f["node"]
        st = bs["jobs"][node]
        waiting = [ops.env.nodes[u]["name"] for u in ops.env.up[node] if not bs["ready"].get(u)]
        if st["state"] == "running":
            return _close(ops, t, "Fulfilled", f"{ops.env.nodes[node]['name']} is already running; no second run started.")
        if waiting:
            if t["stage"] != "waiting for data":
                t["stage"], t["state"] = "waiting for data", "On Hold"
                blockers = [i["id"] for i in ops.tickets("incident", open_only=True) if node in i["impact_nodes"]]
                ops.note(t, NAME, f"On hold: the data it reads is not ready ({', '.join(waiting)})."
                         + (f" Linked to {', '.join(blockers)}; it will run when that is resolved." if blockers else ""))
            return
        res = ops.estate.apply(RERUN[node.split(".")[0]], node=node)
        return _close(ops, t, "Fulfilled", res["message"])
    args = {"user": f.get("user"), "role": f.get("role")} if cat["action"] == "snowflake.grant_role" else {"account": f.get("account")}
    res = ops.estate.apply(cat["action"], **args)
    _close(ops, t, "Fulfilled" if res["ok"] else "Failed", res["message"])


def run(ops) -> None:
    open_reqs = ops.tickets("request", open_only=True)
    for ev in ops.store.all("event"):          # preventive: an account close to expiry
        if ev["status"] == "open" and "ORA_PWD_EXPIRING" in ev["codes"] and not any(r.get("event") == ev["id"] for r in ops.tickets("request")):
            used = next((a["used_by"] for a in ops.env.data["service_accounts"] if a["account"] == ev["facts"]["account"]), [])
            t = raise_request(ops, "SR-SVC-PWD", {"account": ev["facts"]["account"], "_event": ev["id"],
                                                   "reason": f"Expires in {ev['facts']['days']} days. Used by {', '.join(used)}: when it expires, replication and loads stop."},
                              "Watch Desk Agent", by=NAME)
            ev["handling"] = f"Preventive request {t['id']} raised"
            ops.store.put("event", ev)
            open_reqs.append(t)
    for t in open_reqs:
        cat, f = ops.kb.requests[t["catalogue"]], t["fields"]
        if t.get("event") and ops.store.need("event", t["event"])["status"] == "cleared":
            _close(ops, t, "Cancelled", "No longer needed: the account has already been rotated.")
            ops.close_notes(t["id"], kinds=("approve_request",), reason="request cancelled")
            continue
        if t["stage"] == "new":
            rule = cat.get("roles", {}).get(f.get("role"), {}) if "roles" in cat else cat
            if "roles" in cat and f.get("role") not in cat["roles"]:
                _close(ops, t, "Rejected", f"Role {f.get('role')} is not in the catalogue. Ask the data owner to add it first.")
                continue
            approvers = [] if cat.get("standard") else rule.get("approvers", [])
            t["state"] = "Assigned"
            if not approvers:
                t["stage"] = "ready"
                ops.note(t, NAME, "Standard request: no approval needed")
            else:
                t["stage"] = "awaiting approval"
                t["approvals"] = [{"role": r, "person": ops.env.holder(r), "status": "pending"} for r in approvers]
                ops.note(t, NAME, "Validated. Needs approval from " + " and ".join(f"the {a['person']}" for a in t["approvals"])
                         + (f". {rule['note']}" if rule.get("note") else ""))
                for i, a in enumerate(t["approvals"]):
                    ops.notify(a["person"], "approve_request", t, f"Approve request {t['id']}: {t['title']}",
                               f"Requested by {t['requester']}.\nReason: {f.get('reason', '')}\n{rule.get('note', '')}",
                               [{"id": "approve", "label": "Approve", "style": "primary"}, {"id": "reject", "label": "Decline", "input": "Reason", "style": "bad"}],
                               NAME, due_minutes=240, step=i, role=a["role"].replace("_", " "))
        if t["stage"] in ("ready", "waiting for data"):
            _fulfil(ops, t)


def decide(ops, n: dict, t: dict, actor: str, action: str, text: str) -> None:
    a = t["approvals"][n["step"]]
    a.update(status="approved" if action == "approve" else "declined", by=actor, at=ops.now.isoformat())
    ops.note(t, actor, f"{'Approved' if action == 'approve' else 'Declined'} the request" + (f": {text}" if text else ""), human=True)
    if action != "approve":
        ops.close_notes(t["id"], kinds=("approve_request",), reason="declined by another approver")
        _close(ops, t, "Rejected", f"Declined by {actor}: {text or 'no reason given'}")
    elif all(x["status"] == "approved" for x in t["approvals"]):
        t["stage"] = "ready"
        _fulfil(ops, t)
