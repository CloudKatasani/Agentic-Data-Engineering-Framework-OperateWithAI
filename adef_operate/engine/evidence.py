"""Read-only, metadata-only evidence collection for Incident Diagnosis.

* Every read is charged to the product's daily diagnostic-query budget.
* Log and error text is wrapped as untrusted and scanned for instructions.
* There is no way to read row values. A request for them is refused, and a request
  touching a Restricted (or unclassified) column is escalated to the Data Steward (OT-05).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..core.errors import GuardViolation
from .escalation import escalate
from .injection import scan
from .registry import Registry
from .workspace import Workspace

READ_COST = 0.05


@dataclass
class Evidence:
    id: str
    kind: str
    source: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    untrusted: bool = False
    injection_flags: list[str] = field(default_factory=list)


class EvidenceReader:
    identity = "diagnosis"

    def __init__(self, ws: Workspace, adapter, case_id: Optional[str] = None):
        self.ws = ws
        self.adapter = adapter
        self.case_id = case_id
        self.items: list[Evidence] = []
        self.registry = Registry(ws)
        pack = ws.read_yaml("control/handover-pack.yaml") or {}
        self.restricted = pack.get("restricted_columns", {})
        self.classified = set(pack.get("object_tags", {})) | {o for p in pack.get("pipelines", []) for o in p["objects"]}

    def _add(self, kind: str, source: str, summary: str, data: dict, *, untrusted: bool = False,
             flags: Optional[list[str]] = None) -> Evidence:
        self.registry.charge("diagnostic_query_spend", READ_COST, by=self.identity)
        ev = Evidence(id=f"E-{len(self.items) + 1:02d}", kind=kind, source=source, summary=summary,
                      data=data, untrusted=untrusted, injection_flags=flags or [])
        self.items.append(ev)
        if self.case_id:
            self.ws.write(self.identity, f"cases/{self.case_id}/evidence/{ev.id}-{kind}.json",
                          json.dumps(asdict(ev), indent=1, default=str))
        return ev

    # -- reads ------------------------------------------------------------------------
    def signals(self, signals: list[dict]) -> Evidence:
        rows = [{k: s.get(k) for k in ("id", "signal_class", "object", "observed", "expected", "monitor_id",
                                       "detail")} for s in signals]
        return self._add("signals", "signal store", f"{len(rows)} signal(s) in the group", {"signals": rows})

    def run(self, pipeline: str) -> Evidence:
        hist = self.adapter.run_history(self.ws.dp_id, pipeline)
        logs = " ".join(self.adapter.run_logs(self.ws.dp_id, pipeline))
        sr = scan(logs, source=f"run-log:{pipeline}")
        last = hist[-1] if hist else {}
        summary = f"{pipeline}: latest run {last.get('status')} (prior {len(hist) - 1} runs succeeded)" \
            if hist else f"{pipeline}: no run history"
        if sr.suspicious:
            summary += "; log contains instruction-like text, treated as data and reported"
        return self._add("run", f"orchestration:{pipeline}", summary,
                         {"history": hist, "log_wrapped": sr.wrapped}, untrusted=True, flags=list(sr.matches))

    def freshness_volume(self, obj: str, monitors: dict[str, dict]) -> Evidence:
        data: dict[str, Any] = {}
        for cls in ("freshness", "volume"):
            m = monitors.get(f"MON-{'FRESH' if cls == 'freshness' else 'VOL'}-{obj}")
            if m:
                data[cls] = {k: v for k, v in self.adapter.read(cls, self.ws.dp_id, m).items() if k != "evidence"}
        bd = self.adapter.volume_breakdown(self.ws.dp_id, obj)
        if bd:
            data["breakdown"] = bd
        summary = ", ".join(f"{k}: {v}" for k, v in data.items() if k != "breakdown")
        if bd:
            short = [r for r, v in bd.items() if v["observed"] is not None and v["observed"] < 0.5 * v["expected"]]
            summary += f"; regions below half of expected: {short or 'none'}"
        return self._add("freshness_volume", f"data_platform:{obj}", summary, data)

    def schema(self, obj: str, contract: dict[str, str], monitors: dict[str, dict]) -> Optional[Evidence]:
        m = monitors.get(f"MON-SCHEMA-{obj}")
        if not m or not contract:
            return None
        cols = self.adapter.read("schema", self.ws.dp_id, m)["columns"]
        missing, added = sorted(set(contract) - set(cols)), sorted(set(cols) - set(contract))
        return self._add("schema", f"catalog:{obj}", f"against contract: missing {missing}, added {added}",
                         {"missing": missing, "added": added})

    def batches(self, obj: str) -> Optional[Evidence]:
        stats = self.adapter.batch_stats(self.ws.dp_id, obj)
        if not stats:
            return None
        seen: dict[str, str] = {}
        dups = []
        for b in stats:
            if b["checksum"] in seen and not b["quarantined"]:
                dups.append({"batch_id": b["batch_id"], "same_as": seen[b["checksum"]], "rows": b["rows"]})
            seen.setdefault(b["checksum"], b["batch_id"])
        return self._add("batches", f"data_platform:{obj}",
                         f"{len(stats)} batch(es); identical-content batches: {[d['batch_id'] for d in dups] or 'none'}",
                         {"batches": stats, "duplicates": dups})

    def changes(self, objects: set[str]) -> Evidence:
        rel = self.adapter.releases(self.ws.dp_id)
        notes = self.adapter.upstream_notices(self.ws.dp_id)
        touching = [r for r in rel if r.get("object") in objects]
        return self._add("changes", "change history and upstream status",
                         f"releases touching affected objects: {len(touching)}; upstream notices: {len(notes)}",
                         {"releases": rel, "releases_touching": touching, "upstream_notices": notes})

    def queries(self) -> Evidence:
        q = self.adapter.query_history(self.ws.dp_id)
        return self._add("queries", "query history", f"full scans: {q.get('full_scans')} on {q.get('top_object')}", q)

    def access(self, model: list[str], monitor: Optional[dict]) -> Optional[Evidence]:
        if not monitor:
            return None
        grants = self.adapter.read("access", self.ws.dp_id, monitor)["grants"]
        outside = [g for g in grants if g not in model]
        return self._add("access", "access_audit", f"grants outside the approved model: {outside or 'none'}",
                         {"outside": outside, "grant_count": len(grants)})

    def profile(self, obj: str, column: str) -> Evidence:
        stats = self.adapter.profile(self.ws.dp_id, obj, column)
        return self._add("profile", f"profiling:{obj}.{column}", f"statistics only for {obj}.{column}", stats)

    def raw_values(self, obj: str, column: str, *, reason: str = "") -> None:
        """Always refused: Diagnosis reads counts, hashes and metadata only."""
        restricted = column in self.restricted.get(obj, []) or obj not in self.classified
        if restricted:
            escalate(self.ws, by=self.identity, to="data_steward", case_id=self.case_id,
                     reason=f"diagnosis asked for raw values of Restricted column {obj}.{column}"
                            + (f" ({reason})" if reason else "") + "; refused, statistics offered")
        raise GuardViolation(f"raw values of {obj}.{column} are not readable by Diagnosis; use profile() "
                             "for statistics" + ("; escalated to the Data Steward" if restricted else ""),
                             escalate=restricted)
