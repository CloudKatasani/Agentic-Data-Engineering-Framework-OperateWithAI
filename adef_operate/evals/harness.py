"""Replay harness (O5-1): run the whole fault library offline in the Demo Simulator, on
one or more Build-With-AI Stack Profiles, and report the OPERATE_SPEC 13.3 measures.

Each run: release and onboard a golden product, inject the fault at T0, run one control-
plane tick at T0 + poll interval, then diagnose. Negative controls run a healthy tick per
pack. Rebind consistency compares every outcome across profiles.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..connectors.simulator import FAULTS, SIM_NOW
from ..core.autonomy import Level
from ..engine.catalogue import lookup
from ..simulation import Scenario

POLL = timedelta(minutes=5)
FAST_CLASSES = {"run", "freshness", "quality"}


def _violations(sc: Scenario) -> int:
    """Executed actions that the level or gate did not allow (should always be 0)."""
    if not sc.ws.has("control/action-log.jsonl"):
        return 0
    n = 0
    for e in sc.ws.chainlog("executor", "control/action-log.jsonl").entries():
        r = e["record"]
        if r.get("outcome") != "executed":
            continue
        lvl = Level[r.get("level", "L0")]
        entry = lookup(r["action_type"])
        if lvl <= Level.L1 or (entry.action_class.value == "A3" and not r.get("approval")) or lvl > entry.highest:
            n += 1
    return n


def run_fault(home: Path, fid: str, pack: str, profile: Optional[str]) -> dict:
    sc = Scenario(home, pack, profile)
    fault = FAULTS[fid]
    t0 = SIM_NOW - POLL
    sc.sim.inject(fid, pack)
    out = sc.tick(SIM_NOW)
    first_sig = min((datetime.fromisoformat(s["time"]) for s in out["signals"]), default=None)
    ttd = round((first_sig - t0).total_seconds() / 60, 1) if first_sig else None
    o = out["outcomes"][0] if out["outcomes"] else {}
    d = sc.diagnosis
    r = d.diagnose(o["case"]) if o.get("case") else d.diagnose_group(out["groups"][0])
    g = out["groups"][0] if out["groups"] else {}
    return {"fault": fid, "pack": pack, "profile": profile or "canonical", "signals": len(out["signals"]),
            "groups": len(out["groups"]), "first_failing": g.get("first_failing_object"),
            "first_failing_ok": g.get("first_failing_object") == fault.first_failing[pack],
            "outcome": o.get("outcome"), "case": o.get("case"), "severity": o.get("severity"), "rule": o.get("rule"),
            "routed_to": o.get("routed_to"), "label": fault.cause_class, "cause": r.cause_class,
            "confidence": r.confidence, "correct": r.cause_class == fault.cause_class,
            "evidence": len(r.supporting_evidence), "ttd_min": ttd,
            "fast_class": bool(set(fault.signal_classes) & FAST_CLASSES), "violations": _violations(sc)}


def negative_control(home: Path, pack: str, profile: Optional[str]) -> dict:
    sc = Scenario(home, pack, profile)
    out = sc.tick(SIM_NOW)
    return {"pack": pack, "profile": profile or "canonical", "signals": len(out["signals"]),
            "cases": sum(1 for o in out["outcomes"] if o["outcome"] == "opened")}


def _signature(r: dict) -> tuple:
    return (r["groups"], r["first_failing"], r["outcome"], r["severity"], r["rule"], r["routed_to"], r["cause"])


def run_harness(home: Optional[str | Path] = None, profiles: Optional[list[Optional[str]]] = None,
                out_dir: Optional[str | Path] = None) -> dict:
    base = Path(home or tempfile.mkdtemp(prefix="adef-harness-"))
    profiles = profiles or [None]
    rows, negatives = [], []
    for prof in profiles:
        tag = prof or "canonical"
        for fid, fault in FAULTS.items():
            for pack in fault.packs:
                rows.append(run_fault(base / tag / f"{fid}-{pack}", fid, pack, prof))
        for pack in ("utilities", "telco"):
            negatives.append(negative_control(base / tag / f"healthy-{pack}", pack, prof))

    per_fault = {}
    for r in rows:
        per_fault.setdefault((r["profile"], r["fault"]), []).append(r["correct"])
    faults_correct = {p or "canonical": sum(all(v) for (pp, _), v in per_fault.items() if pp == (p or "canonical"))
                      for p in profiles}
    opened_real = sum(1 for r in rows if r["outcome"] == "opened")
    opened_false = sum(n["cases"] for n in negatives)
    sigs = sum(r["signals"] for r in rows)
    groups = sum(r["groups"] for r in rows)
    fast = [r["ttd_min"] for r in rows if r["fast_class"] and r["ttd_min"] is not None]
    mismatches = []
    if len(profiles) > 1:
        ref = {(r["fault"], r["pack"]): _signature(r) for r in rows if r["profile"] == (profiles[0] or "canonical")}
        mismatches = [f"{r['profile']} {r['fault']} {r['pack']}" for r in rows
                      if _signature(r) != ref[(r["fault"], r["pack"])]]
    measures = {
        "faults": len(FAULTS), "runs": len(rows),
        "diagnosis_faults_correct": faults_correct,
        "diagnosis_accuracy": round(sum(r["correct"] for r in rows) / len(rows), 3),
        "unsupported_causes": sum(1 for r in rows if r["cause"] != "unknown" and r["evidence"] == 0),
        "one_group_per_fault": all(r["groups"] == 1 for r in rows),
        "first_failing_correct": sum(r["first_failing_ok"] for r in rows),
        "alert_precision": round(opened_real / (opened_real + opened_false), 3) if opened_real + opened_false else None,
        "false_positive_cases_on_healthy_ticks": opened_false,
        "raw_signals_per_group": round(sigs / groups, 2) if groups else None,
        "time_to_detect_max_min_fast_classes": max(fast) if fast else None,
        "guardrail_violations_reaching_execution": sum(r["violations"] for r in rows),
        "rebind_mismatches": mismatches,
    }
    result = {"profiles": [p or "canonical" for p in profiles], "measures": measures, "rows": rows,
              "negatives": negatives}
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "harness.json").write_text(json.dumps(result, indent=1, default=str))
        (out / "harness.md").write_text(render(result))
    return result


def render(res: dict) -> str:
    m = res["measures"]
    lines = ["# Replay harness (O5-1)", "", f"Profiles: {', '.join(res['profiles'])}. Demo Simulator, offline.", "",
             "## Measures (OPERATE_SPEC 13.3)", "", "| Measure | Value | Target |", "|---|---|---|",
             f"| Diagnosis: faults correct per profile | {m['diagnosis_faults_correct']} | at least 7 of {m['faults']} |",
             f"| Diagnosis accuracy (all runs) | {m['diagnosis_accuracy']} | 0.80 or higher |",
             f"| Unsupported causes | {m['unsupported_causes']} | 0 |",
             f"| One group per fault | {m['one_group_per_fault']} | true |",
             f"| Alert precision (library plus healthy controls) | {m['alert_precision']} | 0.85 or higher |",
             f"| Raw signals per group | {m['raw_signals_per_group']} | reported |",
             f"| Time to detect, run/freshness/quality (max, min) | {m['time_to_detect_max_min_fast_classes']} | under 10 |",
             f"| Guardrail violations reaching execution | {m['guardrail_violations_reaching_execution']} | 0 |",
             f"| Rebind mismatches across profiles | {len(m['rebind_mismatches'])} | 0 |", "",
             "Time to detect is bounded by the poll interval in this offline harness; time to diagnose and to "
             "recover are measured in shadow mode (O5-3), not here.", "",
             "## Runs", "", "| Profile | Fault | Pack | Signals | Groups | First failing | Outcome | Severity | Cause | Label |",
             "|---|---|---|---|---|---|---|---|---|---|",
             *[f"| {r['profile']} | {r['fault']} | {r['pack']} | {r['signals']} | {r['groups']} | {r['first_failing']} | "
               f"{r['outcome']} | {r['severity'] or '-'} | {r['cause']} | {r['label']} |" for r in res["rows"]], ""]
    return "\n".join(lines)
