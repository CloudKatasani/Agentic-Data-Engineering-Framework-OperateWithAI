"""Diagnosis Agent. Matches the alert to a known error and runs that entry's read-only
checks to confirm the cause, recording what each check showed. When nothing matches it
gathers the standard facts for the tool and says plainly that the cause is not known."""

from __future__ import annotations

from . import llm

NAME = "Diagnosis Agent"
GENERIC = {
    "denodo": [("denodo.datasource_pool", {}, "State of the data source connection pool")],
    "informatica": [("informatica.session_log", {"node": "{node}"}, "Session log summary")],
    "goldengate": [("goldengate.lag", {}, "Replication lag")],
    "dbt": [("dbt.failed_results", {}, "Failed and skipped results of the run")],
    "cognos": [("cognos.dispatcher_status", {}, "Report service state")],
    "mft": [("mft.file_status", {}, "File status on the gateway")],
}


def fmt(args: dict, ctx: dict) -> dict:
    return {k: (v.format_map(ctx) if isinstance(v, str) else v) for k, v in (args or {}).items()}


def _probe(ops, name: str, args: dict) -> dict:
    try:
        return ops.estate.probe(name, **args)
    except Exception as exc:   # a check that cannot run is evidence too
        return {"summary": f"Check could not run: {exc}", "facts": {}}


def context(ops, inc: dict) -> dict:
    ev = ops.store.need("event", inc["root_event"])
    return {"node": ev["node"] or (inc["root_nodes"][0] if inc["root_nodes"] else ""), "ci": ev["ci"],
            "host": ev["facts"].get("host") or ops.env.cis[ev["ci"]].get("host", "")}


def run(ops) -> None:
    for inc in ops.tickets("incident", open_only=True):
        if inc["diagnosis"]:
            continue
        ev = ops.store.need("event", inc["root_event"])
        ke = ops.kb.get(inc["ke"])
        ctx = context(ops, inc)
        evidence = []
        if ke and ke.get("checks"):
            for c in ke["checks"]:
                res = _probe(ops, c["probe"], fmt(c.get("args"), ctx))
                ok = all(res["facts"].get(k) == v for k, v in c["expect"].items())
                evidence.append({"check": c["says"], "probe": c["probe"], "result": res["summary"], "confirms": ok})
            hit = sum(e["confirms"] for e in evidence)
            status = "confirmed" if hit == len(evidence) else "likely" if hit * 2 >= len(evidence) else "unconfirmed"
        elif ke:
            status = "recognised"
        else:
            for probe, args, says in GENERIC.get(ev["source"], []):
                res = _probe(ops, probe, fmt(args, ctx))
                evidence.append({"check": says, "probe": probe, "result": res["summary"], "confirms": None})
            status = "unknown"
        res = _probe(ops, "change.recent", {"ci": ev["ci"]})
        evidence.append({"check": "Recent approved changes on this system", "probe": "change.recent", "result": res["summary"], "confirms": None})
        d = {"status": status, "ke": inc["ke"], "evidence": evidence, "by": NAME, "at": ops.now.isoformat(), "suggestion": None,
             "cause": ke["cause"] if ke and status != "unconfirmed" else
             ("The alert matches a known error, but the checks do not confirm its usual cause." if ke else
              "This error is not in the known error database. The facts below are what the standard checks show.")}
        if status in ("unknown", "unconfirmed"):
            d["suggestion"] = llm.suggest(ops, inc, ev, evidence)
        inc["diagnosis"] = d
        label = {"confirmed": f"Cause confirmed by {len(evidence) - 1} check(s): {ke['id'] if ke else ''}", "likely": "Cause likely; not every check agrees",
                 "recognised": f"Recognised as {inc['ke']}; no automated checks for it", "unconfirmed": "Known error matched but checks do not confirm it",
                 "unknown": "No known error matches; facts gathered for the engineer"}[status]
        ops.note(inc, NAME, f"{label}. {d['cause']}")
