"""Watch Desk Agent (L1 monitoring). Reads every tool, removes noise, and passes on only
what needs triage. It never changes anything in the client estate."""

from __future__ import annotations

from ..connectors.parsers import parse

NAME = "Watch Desk Agent"


def run(ops, pushed: list | None = None) -> tuple[list[dict], list[dict]]:
    """Read the tools. ``pushed`` is data that arrived on the integration endpoints; without it
    the agent collects from the estate itself."""
    raw = pushed if pushed is not None else ops.estate.telemetry()
    alerts = parse(ops.env, raw)
    new, cleared = ops.ingest(alerts)
    stats = ops.store.kv("watch_stats") or {"raw": 0, "alerts": 0, "events": 0, "suppressed": 0}
    stats["raw"] += len(raw)
    by = stats.setdefault("by_tool", {})
    for source, _ in raw:
        tool = {"goldengate_log": "goldengate", "oracle_alert": "oracle", "servicenow_change": "servicenow"}.get(source, source)
        by[tool] = by.get(tool, 0) + 1
    stats["alerts"] += sum(1 for a in alerts if a["severity"] != "clear")
    stats["events"] += len(new)
    for ev in new:
        change = ops.env.change_for(ev["ci"], ops.now)
        if change and change["active"]:
            ev.update(status="suppressed", handling=f"Expected: inside the window of approved change {change['id']}")
            ops.store.put("event", ev)
            stats["suppressed"] += 1
            ops.audit(NAME, f"Suppressed alert during {change['id']}", ev["id"], ev["message"])
    ops.store.kv("watch_stats", stats)
    for ev in cleared:
        if ev.get("incident"):
            t = ops.store.get("ticket", ev["incident"])
            if t and t["state"] != "Resolved":
                ops.note(t, NAME, f"Monitoring shows recovery: {ev['source']} {ev['entity']} ({ev['check']}) is healthy again")
    return [e for e in new if e["status"] == "open"], cleared
