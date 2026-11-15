"""Triage Agent. Decides whether an alert is a new incident or part of one that is already
open, works out which business services are exposed, sets the priority from the client's
matrix and routes the ticket to the on-call engineer of the right tower."""

from __future__ import annotations

NAME = "Triage Agent"


def _impact_nodes(env, roots: list[str]) -> list[str]:
    out = set()
    for r in roots:
        for x in env.expand(r):
            out.add(x)
            out.update(env.descendants(x))
        out.add(r)
    for n in env.nodes.values():            # a job that builds an affected dataset is affected
        if n.get("builds") and out & set(n["builds"]):
            out.add(n["id"])
    return sorted(out)


def _services(ops, roots: list[str]) -> list[str]:
    delivered = {sid for sid, s in (ops.projection.get("services") or {}).items() if "delivered" in s["status"]}
    return [s for s in ops.env.services_hit_by(roots) if s not in delivered]


def _impact(env, services: list[str]) -> str:
    if any(env.services[s].get("tier") == 1 for s in services):
        return "high"
    return "medium" if services else "low"


def _correlate(ops, ev: dict, ke) -> tuple[dict | None, str, bool]:
    incs = ops.tickets("incident", open_only=True)
    for inc in incs:
        if ev["fingerprint"] in inc["fingerprints"]:
            return inc, "same alert as before", False
    if ke and not ke.get("symptom"):
        for inc in incs:
            if inc.get("ke") == ke["id"]:
                return inc, f"same cause ({ke['id']}) seen from {ev['source']}", True
        return None, "", False
    if ev["node"]:
        scope = set(ops.env.expand(ev["node"])) | {ev["node"]}
        for inc in incs:
            if ke and scope & set(inc["impact_nodes"]):
                return inc, f"downstream effect of {inc['id']}", False
            if not ke and ev["node"] in inc["root_nodes"]:
                return inc, "another failure on the same job", False
    return None, "", False


def run(ops, new_events: list[dict]) -> None:
    env = ops.env
    for ev in new_events:
        ke = ops.kb.match(ev["codes"], ev["message"])
        if ev["severity"] == "warning" and not ke:
            ev["handling"] = "Advisory: no action needed yet"
            ops.store.put("event", ev)
            continue
        inc, why, cause_level = _correlate(ops, ev, ke)
        if inc:
            ev.update(incident=inc["id"], handling=f"Correlated: {why}")
            ops.store.put("event", ev)
            inc["events"].append(ev["id"])
            inc["fingerprints"].append(ev["fingerprint"])
            if ev["ci"] not in inc["cis"]:
                inc["cis"].append(ev["ci"])
            if cause_level and ev["node"] and ev["node"] not in inc["root_nodes"]:
                inc["root_nodes"].append(ev["node"])
                inc["impact_nodes"] = _impact_nodes(env, inc["root_nodes"])
                inc["services"] = sorted(set(inc["services"]) | set(_services(ops, inc["root_nodes"])))
            ops.note(inc, NAME, f"Added {ev['source']} alert on {ev['entity']} to this incident: {why}. No new ticket.")
            continue
        roots = [ev["node"]] if ev["node"] else env.nodes_of_ci(ev["ci"])
        services = _services(ops, roots)
        impact = _impact(env, services)
        urgency = "medium" if services else "low"
        tower = ke["tower"] if ke else env.tower_of_ci(ev["ci"])
        title = ke["title"] if ke else f"{ev['entity']} failed on {env.cis[ev['ci']]['name']}: error not in the known error database"
        inc = ops.open_ticket("incident", title, env.priority(impact, urgency), tower, NAME, impact=impact, urgency=urgency,
                              ke=ke["id"] if ke else None, events=[ev["id"]], fingerprints=[ev["fingerprint"]], cis=[ev["ci"]],
                              root_nodes=roots, impact_nodes=_impact_nodes(env, roots), services=services, root_event=ev["id"],
                              diagnosis=None, plan=None, comms=[], major=False)
        ev.update(incident=inc["id"], handling="Opened incident")
        ops.store.put("event", ev)
        names = ", ".join(env.services[s]["name"] for s in services) or "no business service tonight"
        ops.note(inc, NAME, f"Exposed: {names}. Impact {impact}, urgency {urgency}. Routed to {env.tower_name(tower)} ({inc['assignee']}).")
        ops.notify(inc["assignee"], "ack", inc, f"Acknowledge {inc['id']} ({inc['priority']}): {title}",
                   f"{ev['message']}\n\nExposed: {names}.", [{"id": "ack", "label": "Acknowledge", "style": "primary"}],
                   NAME, due_minutes=env.sla(inc["priority"])["respond"], role=f"{env.tower_name(tower)} on call")
