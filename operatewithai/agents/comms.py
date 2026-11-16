"""Comms Agent. Drafts the stakeholder updates for incidents that expose a business
service: first notice, a change of outlook, and the all-clear. It writes in plain language
from the ticket's facts. The incident manager approves or edits every message; nothing
reaches the client without that."""

from __future__ import annotations

from . import llm

NAME = "Comms Agent"


def _recipients(ops, inc: dict) -> list[str]:
    roles = ["client_service_owner"] + [ops.env.services[s]["owner_role"] for s in inc["services"]]
    out = []
    for r in roles:
        p = ops.env.holder(r)
        if p and p not in out:
            out.append(p)
    return out


def _outlook(ops, inc: dict) -> tuple[str, str]:
    lines, worst = [], "on track"
    order = ["delivered", "on track", "at risk", "delivered late", "breach predicted", "breached"]
    for sid in inc["services"]:
        s = (ops.projection.get("services") or {}).get(sid)
        if not s:
            continue
        lines.append(f"- {s['name']}: due {s['deadline'][11:16]}, current estimate {s['eta'][11:16]} ({s['status']})")
        if order.index(s["status"]) > order.index(worst):
            worst = s["status"]
    return "\n".join(lines), worst


def _draft(ops, inc: dict, stage: str) -> str:
    outlook, worst = _outlook(ops, inc)
    d, plan = inc["diagnosis"] or {}, inc["plan"] or {}
    head = {"initial": "We are working on an issue that may delay tonight's data.",
            "update": "Update: the outlook for tonight's data has changed.",
            "resolved": "Resolved: the issue below is fixed."}[stage]
    doing = "; ".join(s["title"] for s in plan.get("steps", []) if s["kind"] in ("action", "manual")) or "The on-call engineer is investigating."
    text = (f"Subject: [{inc['priority']}] {inc['id']} - {inc['title']}\n\n{head}\n\nWhat is affected\n{outlook or '- No business service is affected.'}\n\n"
            f"What happened\n{d.get('cause', 'Under investigation.')}\n\n")
    if stage == "resolved":
        text += f"What we did\n{(inc.get('resolution') or {}).get('notes') or doing}\n\nA review with the permanent fix follows for P1 and P2 incidents."
    else:
        text += f"What we are doing\n{doing}\nOwner: {inc['assignee']} ({ops.env.tower_name(inc['tower'])}).\n\nNext update: when the outlook changes, or within 60 minutes."
    return text


def run(ops) -> None:
    for inc in ops.tickets("incident"):
        if not inc["services"] or inc["priority"] not in ("P1", "P2") or not inc["diagnosis"]:
            continue
        stages = [c["stage"] for c in inc["comms"] if c["status"] in ("draft", "sent")]
        _, worst = _outlook(ops, inc)
        if inc["state"] == "Resolved":
            stage = "resolved" if "resolved" not in stages and "initial" in stages else None
        elif "initial" not in stages:
            stage = "initial"
        elif worst in ("breach predicted", "breached") and inc.get("comms_outlook") not in ("breach predicted", "breached"):
            stage = "update"
        else:
            stage = None
        if not stage:
            continue
        for c in inc["comms"]:
            if c["status"] == "draft":
                c["status"] = "superseded"
        ops.close_notes(inc["id"], kinds=("comms",), reason="superseded by a newer draft")
        inc["comms_outlook"] = worst
        text = _draft(ops, inc, stage)
        better = llm.rewrite_message(ops, inc, text)
        c = {"n": len(inc["comms"]) + 1, "stage": stage, "status": "draft", "text": better["text"] if better else text, "to": _recipients(ops, inc),
             "created": ops.now.isoformat(), "sent_by": None, "written_with": better["model"] if better else None}
        inc["comms"].append(c)
        ops.note(inc, NAME, f"Drafted the {stage} stakeholder message for {ops.env.holder('incident_manager')} to approve")
        ops.notify(ops.env.holder("incident_manager"), "comms", inc, f"Approve the {stage} message for {inc['id']} ({inc['priority']})",
                   f"To: {', '.join(c['to'])}\n\n{c['text']}",
                   [{"id": "send", "label": "Approve and send", "style": "primary", "input": "Message (edit if needed)", "prefill": c["text"]},
                    {"id": "discard", "label": "Do not send"}], NAME, due_minutes=20, step=c["n"], role="incident manager")


def decide(ops, n: dict, inc: dict, actor: str, action: str, text: str) -> None:
    c = next(c for c in inc["comms"] if c["n"] == n["step"])
    if action == "send":
        c.update(status="sent", text=text or c["text"], sent_by=actor, sent_at=ops.now.isoformat())
        ops.note(inc, actor, f"Approved and sent the {c['stage']} message to {', '.join(c['to'])}", human=True)
        for person in c["to"]:
            ops.notify(person, "fyi", inc, f"From the incident manager: {inc['id']} {c['stage']} notice", c["text"],
                       [{"id": "dismiss", "label": "Read"}], NAME, due_minutes=120, step=c["n"], role="client")
    else:
        c["status"] = "discarded"
        ops.note(inc, actor, f"Decided not to send the {c['stage']} message", human=True)
    ops.save(inc)
