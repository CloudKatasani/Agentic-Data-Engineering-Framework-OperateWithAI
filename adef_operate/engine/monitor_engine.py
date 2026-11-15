"""Monitor engine (backlog O3-1). Detection is code: a model is never in the path that
decides whether a threshold was crossed.

The engine evaluates only a ``monitoring-spec.yaml`` whose hash matches the spec hash in
the latest approved OG1 manifest. Every evaluation writes a monitor result; every failing
result also yields a ``SIG-######`` record written by the collector.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.errors import GuardViolation
from ..core.hashing import file_hash
from ..schemas.records import Signal
from ..schemas.service import MonitoringSpec
from .approvals import Approvals
from .injection import scan
from .workspace import Workspace


def _evaluate(m: dict[str, Any], reading: dict[str, Any]) -> tuple[bool, Any, Any, str]:
    """Return (passed, observed, expected, detail). Pure function of rule and reading."""
    rule = m["rule"]
    kind = rule["kind"]
    if kind == "run_state":
        return reading["status"] not in ("failed",), reading["status"], "success", ""
    if kind == "duration_ratio":
        ratio = reading["duration_min"] / max(reading["median_min"], 1e-9)
        return ratio <= rule["max_ratio"], round(ratio, 3), rule["max_ratio"], "duration / trailing median"
    if kind == "freshness":
        return (reading["lag_minutes"] <= rule["max_lag_minutes"], reading["lag_minutes"],
                rule["max_lag_minutes"], "lag minutes")
    if kind == "volume_band":
        exp = reading["expected_rows"]
        dev = abs(reading["rows"] - exp) / max(exp, 1) * 100
        return dev <= rule["tolerance_pct"], reading["rows"], exp, f"deviation {dev:.1f}% of weekday band"
    if kind == "schema_contract":
        want, got = rule["columns"], reading["columns"]
        missing = sorted(set(want) - set(got))
        added = sorted(set(got) - set(want))
        retyped = sorted(c for c in set(want) & set(got) if want[c] != got[c])
        ok = not (missing or added or retyped)
        detail = f"missing={missing} added={added} retyped={retyped}"
        return ok, len(missing) + len(added) + len(retyped), 0, detail
    if kind == "quality_rule":
        return reading["status"] == "pass", reading["failed_pct"], 0.0, f"rule {rule['rule_id']}"
    if kind == "burn_rate":
        return reading["burn_rate"] <= rule["max_burn"], reading["burn_rate"], rule["max_burn"], "error budget burn"
    if kind == "cost_ratio":
        ratio = reading["daily"] / max(reading["estimate"], 1e-9)
        return (ratio <= rule["max_ratio"], round(ratio, 3), rule["max_ratio"],
                f"daily {reading['daily']} vs estimate {reading['estimate']}; full scans {reading.get('full_scans', 0)}"
                + (f" on {reading['top_object']}" if reading.get("top_object") else ""))
    if kind == "access_model":
        outside = [g for g in reading["grants"] if g not in rule["allowed"]]
        return not outside, len(outside), 0, f"grants outside model: {outside}"
    raise ValueError(f"unknown rule kind {kind!r} in monitor {m['id']}")


class MonitorEngine:
    def __init__(self, ws: Workspace, adapter, *, simulated: bool = False):
        self.ws = ws
        self.adapter = adapter
        self.simulated = simulated

    def load_spec(self) -> MonitoringSpec:
        path = "service/monitoring-spec.yaml"
        if not self.ws.has(path):
            raise GuardViolation("no monitoring-spec.yaml")
        approved = Approvals(self.ws).approved_spec_hash()
        actual = file_hash(self.ws.path(path))
        if approved is None:
            raise GuardViolation("monitoring-spec.yaml has no approved OG1; the engine will not run it")
        if actual != approved:
            raise GuardViolation("monitoring-spec.yaml does not match the approved hash; "
                                 "the engine runs only the approved specification")
        return MonitoringSpec.model_validate(self.ws.read_yaml(path))

    # -- id counters ------------------------------------------------------------------
    def _count(self, folder: str) -> int:
        root = self.ws.path(folder)
        if not root.exists():
            return 0
        return sum(len(f.read_text(encoding="utf-8").splitlines()) for f in root.glob("*.jsonl"))

    def run(self, at: Optional[datetime] = None) -> dict[str, list[dict]]:
        at = at or datetime.now(timezone.utc)
        spec = self.load_spec()
        day = at.date().isoformat()
        results, signals = [], []
        n_res = self._count("signals/monitor-results")
        n_sig = self._count_signals()
        for mon in spec.monitors:
            if not mon.enabled:
                continue
            m = mon.model_dump(mode="json")
            reading = self.adapter.read(m["signal_class"], self.ws.dp_id, m)
            passed, observed, expected, detail = _evaluate(m, reading)
            n_res += 1
            res = {"id": f"MR-{n_res:06d}", "monitor_id": m["id"], "time": at.isoformat(),
                   "status": "pass" if passed else "fail", "observed": observed, "expected": expected}
            results.append(res)
            self.ws.append("monitor_engine", f"signals/monitor-results/{day}.jsonl", json.dumps(res))
            if passed:
                continue
            if m["signal_class"] == "run" and m.get("pipeline"):
                logs = " ".join(self.adapter.run_logs(self.ws.dp_id, m["pipeline"]))
                sr = scan(logs, source=f"run-log:{m['pipeline']}")
                if sr.suspicious:
                    detail = ("log text contains instruction-like content; treated as data and "
                              "reported (" + "; ".join(sr.matches)[:200] + ")")
            n_sig += 1
            sig = Signal(id=f"SIG-{n_sig:06d}", signal_class=m["signal_class"], product=self.ws.dp_id,
                         object=m["object"], observed=observed, expected=expected,
                         evidence_path=reading.get("evidence", ""), time=at, monitor_id=m["id"],
                         detail=detail[:500], simulated=self.simulated)
            signals.append(sig.model_dump(mode="json"))
            self.ws.append("collector", f"signals/{day}.jsonl", json.dumps(signals[-1]))
        return {"results": results, "signals": signals}

    def _count_signals(self) -> int:
        root = self.ws.path("signals")
        if not root.exists():
            return 0
        return sum(len(f.read_text(encoding="utf-8").splitlines()) for f in root.glob("*.jsonl"))


def ingest_signal(ws: Workspace, data: dict[str, Any]) -> dict[str, Any]:
    """Collector entry point for externally produced signals (Consume events, upstream feeds)."""
    sig = Signal.model_validate(data)
    ws.append("collector", f"signals/{sig.time.date().isoformat()}.jsonl",
              json.dumps(sig.model_dump(mode="json")))
    return sig.model_dump(mode="json")


def evaluate_spec(spec: dict[str, Any], adapter, product: str) -> list[dict[str, Any]]:
    """Sandbox evaluation (dry runs, readiness proofs). Writes nothing."""
    out = []
    for m in MonitoringSpec.model_validate(spec).monitors:
        md = m.model_dump(mode="json")
        if not md["enabled"]:
            continue
        passed, observed, expected, detail = _evaluate(md, adapter.read(md["signal_class"], product, md))
        out.append({"monitor_id": md["id"], "signal_class": md["signal_class"], "object": md["object"],
                    "passed": passed, "observed": observed, "expected": expected, "detail": detail})
    return out
