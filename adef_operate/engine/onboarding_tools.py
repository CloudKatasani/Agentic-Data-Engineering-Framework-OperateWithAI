"""Deterministic drafting tools for Service Onboarding (O1).

The onboarding agent owns ``service/``; these helpers produce a first draft from the
handover pack that the agent reviews, and the sandbox proofs it reports at OG1.
"""

from __future__ import annotations

from typing import Any, Callable

import yaml

from ..connectors.simulator import FAULTS, SimulatorAdapter
from .catalogue import lookup
from .monitor_engine import evaluate_spec
from .workspace import Workspace

RUNBOOK_ACTIONS = {
    "run": ["retry_failed_task"],
    "freshness": ["rerun_latest_window", "set_trust_status"],
    "slo": ["set_trust_status"],
    "volume": ["quarantine_batch", "pause_pipeline"],
    "schema": ["pause_pipeline"],
    "quality": ["pause_pipeline", "quarantine_batch"],
    "cost": [],
    "access": [],
}


def _objectives_by_object(pack: dict) -> dict[tuple[str, str], dict]:
    return {(o["object"], o["kind"]): o for o in pack["objectives"]}


def draft_service(pack: dict[str, Any]) -> dict[str, Any]:
    """Return {service_definition, monitoring_spec, runbook, action_bindings}."""
    dp, owner = pack["dp_id"], pack["on_call"]
    objs = _objectives_by_object(pack)
    downstream: dict[str, list[str]] = {}
    for a, b in pack.get("lineage", []):
        downstream.setdefault(a, []).append(b)

    monitors: list[dict[str, Any]] = []

    def add(mid, cls, obj, rule, sev, pipeline=None, objective=None, quality_rule=None):
        monitors.append({"id": mid, "signal_class": cls, "object": obj, "pipeline": pipeline,
                         "rule": rule, "owner": owner, "severity_hint": sev,
                         "runbook_entry": f"RB-{cls}", "objective": objective,
                         "quality_rule": quality_rule, "enabled": True})

    for p in pack["pipelines"]:
        add(f"MON-RUN-{p['id']}", "run", p["objects"][0], {"kind": "run_state"}, "S3", p["id"])
        add(f"MON-DUR-{p['id']}", "run", p["objects"][0], {"kind": "duration_ratio", "max_ratio": 2.0},
            "S4", p["id"])
        for o in p["objects"]:
            fo = objs.get((o, "freshness"))
            add(f"MON-FRESH-{o}", "freshness", o,
                {"kind": "freshness", "max_lag_minutes": fo["target"] if fo else 120},
                "S2" if fo else "S3", p["id"], fo["id"] if fo else None)
            if fo:
                add(f"MON-SLO-{o}", "slo", o, {"kind": "burn_rate", "max_burn": 1.0,
                                               "objective_minutes": fo["target"]}, "S2", p["id"], fo["id"])
            vo = objs.get((o, "completeness"))
            add(f"MON-VOL-{o}", "volume", o,
                {"kind": "volume_band", "tolerance_pct": vo["target"] if vo else 25},
                "S3", p["id"], vo["id"] if vo else None)
    for obj, cols in (pack.get("schema_contract") or {}).items():
        pipe = next((p["id"] for p in pack["pipelines"] if obj in p["objects"]), None)
        add(f"MON-SCHEMA-{obj}", "schema", obj, {"kind": "schema_contract", "columns": cols}, "S2", pipe)
    for r in pack.get("quality_rules", []):
        if r["blocking"]:
            pipe = next((p["id"] for p in pack["pipelines"] if r["object"] in p["objects"]), None)
            add(f"MON-DQ-{r['id']}", "quality", r["object"], {"kind": "quality_rule", "rule_id": r["id"]},
                "S2", pipe, quality_rule=r["id"])
    first_obj = pack["pipelines"][0]["objects"][0]
    add("MON-COST", "cost", first_obj, {"kind": "cost_ratio", "max_ratio": 1.5}, "S4")
    add("MON-ACCESS", "access", first_obj, {"kind": "access_model", "allowed": pack.get("access_model", [])}, "S2")

    bindings = []
    for cls, actions in RUNBOOK_ACTIONS.items():
        for a in actions:
            entry = lookup(a)
            for p in pack["pipelines"]:
                if a == "quarantine_batch" and not p.get("quarantine_path"):
                    continue
                executable = bool(p.get("idempotent")) or a in ("set_trust_status", "pause_pipeline")
                bindings.append({"runbook_entry": f"RB-{cls}", "action_type": a,
                                 "action_class": entry.action_class.value, "pipeline": p["id"],
                                 "executable": executable,
                                 "note": "" if executable else "pipeline not declared idempotent: people decide"})

    runbook = [f"# Runbook - {pack['name']} ({dp})", "",
               "Every step names an adapter action type and its action class. Steps on a pipeline "
               "without an idempotency declaration are not executable by the executor.", ""]
    for cls, actions in RUNBOOK_ACTIONS.items():
        runbook.append(f"## RB-{cls}")
        runbook.append("")
        runbook.append("1. Commander opens or attaches the case; severity by rule.")
        for i, a in enumerate(actions, start=2):
            runbook.append(f"{i}. `{a}` ({lookup(a).action_class.value}) - only through the executor.")
        if not actions:
            runbook.append("2. No catalogue action. Route the finding to its owner "
                           f"({'FinOps, OG4' if cls == 'cost' else 'Governance, OG5'}).")
        runbook.append("")

    service_def = {
        "dp_id": dp, "name": pack["name"], "domain_pack": pack["domain_pack"],
        "criticality": "regulated" if pack.get("regulated") else "standard",
        "objects": sorted({o for p in pack["pipelines"] for o in p["objects"]}),
        "objectives": pack["objectives"], "owners": pack["owners"], "on_call": owner,
        "consumers": pack.get("consumers", []), "dependencies": downstream,
        "pipelines": pack["pipelines"],
    }
    return {"service_definition": service_def,
            "monitoring_spec": {"dp_id": dp, "version": 1, "monitors": monitors},
            "runbook": "\n".join(runbook),
            "action_bindings": {"dp_id": dp, "bindings": bindings}}


def write_service(ws: Workspace, draft: dict[str, Any]) -> None:
    ws.write_yaml("onboarding", "service/service-definition.yaml", draft["service_definition"])
    ws.write_yaml("onboarding", "service/monitoring-spec.yaml", draft["monitoring_spec"])
    ws.write("onboarding", "service/runbook.md", draft["runbook"])
    ws.write_yaml("onboarding", "service/action-bindings.yaml", draft["action_bindings"])


def check_draft(draft: dict[str, Any]) -> list[str]:
    """Onboarding decision rules: no monitor without an owner, no objective without a
    monitor, no executable re-run on a pipeline without an idempotency declaration."""
    problems = []
    monitors = draft["monitoring_spec"]["monitors"]
    sd = draft["service_definition"]
    for m in monitors:
        if not m.get("owner"):
            problems.append(f"monitor {m['id']} has no owner")
    covered = {m.get("objective") for m in monitors}
    for o in sd["objectives"]:
        if o["id"] not in covered:
            problems.append(f"objective {o['id']} has no monitor")
    qcovered = {m.get("quality_rule") for m in monitors}
    idem = {p["id"]: p.get("idempotent") is True for p in sd["pipelines"]}
    for b in draft["action_bindings"]["bindings"]:
        if b["executable"] and b["action_type"] in ("retry_failed_task", "rerun_latest_window") \
                and not idem.get(b["pipeline"]):
            problems.append(f"binding {b['action_type']} on {b['pipeline']} is executable but the "
                            "pipeline is not declared idempotent")
    return problems


def readiness(ws: Workspace, pack_name: str,
              adapter_factory: Callable[[], SimulatorAdapter] = SimulatorAdapter) -> dict[str, Any]:
    """Dry-run the spec on a healthy sandbox and prove each fault fires the right class."""
    spec = yaml.safe_load(ws.read("service/monitoring-spec.yaml"))
    healthy = evaluate_spec(spec, adapter_factory(), ws.dp_id)
    baseline_alerts = [r["monitor_id"] for r in healthy if not r["passed"]]
    proofs = {}
    for fid, fault in FAULTS.items():
        if pack_name not in fault.packs:
            continue
        sim = adapter_factory()
        sim.inject(fid, pack_name)
        fired = {r["signal_class"] for r in evaluate_spec(spec, sim, ws.dp_id) if not r["passed"]}
        proofs[fid] = {"expected": list(fault.signal_classes), "fired": sorted(fired),
                       "ok": set(fault.signal_classes) <= fired}
    history_alerts: dict[str, int] = {}
    hist = adapter_factory().history(28)
    for _, sim in hist:
        for r in evaluate_spec(spec, sim, ws.dp_id):
            if not r["passed"]:
                history_alerts[r["monitor_id"]] = history_alerts.get(r["monitor_id"], 0) + 1
    total = sum(history_alerts.values())
    report = {"healthy_alerts": baseline_alerts, "history_days": len(hist),
              "history_alerts": history_alerts, "expected_alerts_per_week": round(total * 7 / len(hist), 1), "monitors": len(spec["monitors"]), "fault_proofs": proofs,
              "ready": not baseline_alerts and all(p["ok"] for p in proofs.values())}
    lines = [f"# Readiness report - {ws.dp_id}", "",
             f"- Monitors: {report['monitors']}",
             f"- Alerts on a healthy sandbox: {len(baseline_alerts)}",
             f"- Dry run over {len(hist)} days of history: {total} alerts, "
             f"expected {report['expected_alerts_per_week']} per week", "",
             "| Monitor | Alerts in history |", "|---|---|",
             *[f"| {m} | {n} |" for m, n in sorted(history_alerts.items())], "",
             "| Fault | Expected classes | Fired classes | OK |", "|---|---|---|---|"]
    for fid, p in proofs.items():
        lines.append(f"| {fid} | {', '.join(p['expected'])} | {', '.join(p['fired'])} | {'yes' if p['ok'] else 'NO'} |")
    lines += ["", f"Ready for OG1: {'yes' if report['ready'] else 'no'}"]
    ws.write("onboarding", "service/readiness-report.md", "\n".join(lines) + "\n")
    return report
