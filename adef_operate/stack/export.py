"""Read-only metadata exports: the shadow-mode path to a real product.

The client's platform team exports metadata with read-only identities, on a schedule, into
a folder per snapshot. Operate never connects to the platform: it parses the files with the
same per-tool translators a live adapter uses. Layout (``<root>/<dp_id>/<snapshot_id>/``)::

    manifest.json                     profile_id, captured_at, windows per pipeline, synthetic flag
    orchestration/<pipeline>.json     native run record (e.g. Airflow dagRun, Databricks run)
    orchestration/<pipeline>.log      run log text (treated as untrusted)
    tables/<physical>.json            table telemetry in the layer's native shape
    quality/<rule_id>.json            quality tool result
    cost.json, query_profile.json     billing and query profile from the serving engine
    grants.json, masking.json         grants and column masks
    catalog.json                      catalog tags and retention
    access_history.json, audit.json   role last-used days; audit logging status
    evidence/                         optional metadata for Diagnosis and FinOps:
        run_history/<pipeline>.json, batches/<object>.json, breakdown/<object>.json,
        query_history.json, query_profile.json, upstream_notices.json, releases.json,
        cost_breakdown.json, object_reads.json

There are no actions: an export adapter is read-only by construction.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import translators as T
from .adapter import StackAdapter
from .naming import Naming
from .profiles import tool

LAKE_LAYERS = ("bronze", "silver")
EVIDENCE_FILES = ("query_history", "upstream_notices", "releases", "cost_breakdown", "object_reads")


def _j(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


class FolderPlatform:
    """The platform interface of :class:`NativePlatform`, served from one export snapshot."""

    def __init__(self, profile: dict, snapshot_dir: Path):
        self.profile = profile
        self.dir = Path(snapshot_dir)
        self.manifest = _j(self.dir / "manifest.json", {})
        if self.manifest.get("profile_id") != profile["profile_id"]:
            raise ValueError(f"export {self.dir} was taken for profile {self.manifest.get('profile_id')}, "
                             f"not {profile['profile_id']}")
        self.naming = Naming(profile)
        self.orch = T.ORCHESTRATION[tool(profile, "orchestration")]
        self.table_format = T.TABLE_FORMATS[tool(profile, "table_format")]
        self.engine = T.ENGINES[tool(profile, "compute_engine")]
        self.quality_tool = T.QUALITY[tool(profile, "data_quality")]
        self.catalog = T.CATALOGS[tool(profile, "catalog_governance")]
        self.prices = (profile.get("variables") or {}).get("prices", {})
        self.now = datetime.fromisoformat(self.manifest["captured_at"])

    def table_reader(self, logical: str):
        return self.table_format if logical.split(".")[0] in LAKE_LAYERS else self.engine

    def _need(self, rel: str) -> Any:
        p = self.dir / rel
        if not p.exists():
            raise FileNotFoundError(f"export snapshot {self.dir.name} has no {rel}")
        return _j(p)

    def run(self, product, pipeline):
        return self._need(f"orchestration/{self.naming.pipeline(pipeline)}.json")

    def table(self, product, logical):
        return self._need(f"tables/{self.naming.physical(logical)}.json")

    def quality(self, product, rule_id):
        return self._need(f"quality/{rule_id}.json")

    def cost(self, product):
        return self._need("cost.json")

    def query_profile(self, product):
        return self._need("query_profile.json")

    def grants(self, product):
        return self._need("grants.json")

    def masking(self, product):
        return self._need("masking.json")

    def catalog_state(self, product):
        return self._need("catalog.json")

    def access_history(self, product):
        return self._need("access_history.json")

    def audit_logging(self, product):
        return self._need("audit.json")

    def command(self, action_type, pipeline):
        return "read-only export: no command"


class ExportAdapter(StackAdapter):
    """Read-only adapter over exported snapshots for one product."""

    def __init__(self, profile: dict, product_dir: str | Path, snapshot: Optional[str] = None):
        self.product_dir = Path(product_dir)
        snaps = self.snapshots()
        if not snaps:
            raise ValueError(f"no export snapshots under {self.product_dir}")
        self.snapshot = snapshot or snaps[-1]
        super().__init__(profile, FolderPlatform(profile, self.product_dir / self.snapshot))
        self.name = f"export:{profile['profile_id']}"
        self.overrides: set[str] = set()

    def snapshots(self) -> list[str]:
        return sorted(p.name for p in self.product_dir.iterdir() if (p / "manifest.json").exists()) \
            if self.product_dir.exists() else []

    def at(self, snapshot: str) -> "ExportAdapter":
        return ExportAdapter(self.profile, self.product_dir, snapshot)

    @property
    def synthetic(self) -> bool:
        return bool(self.platform.manifest.get("synthetic"))

    @property
    def sim(self):
        raise AttributeError("an export adapter has no simulator; it is read-only real metadata")

    def _ev(self, rel: str, default: Any) -> Any:
        return _j(self.platform.dir / "evidence" / rel, default)

    # -- history and sandbox proofs --------------------------------------------------------
    def history(self, days: int = 28) -> list[tuple[str, "ExportAdapter"]]:
        prior = [s for s in self.snapshots() if s < self.snapshot][-days:]
        return [(s, self.at(s)) for s in prior]

    def inject_class(self, signal_class: str) -> None:
        """Sandbox proof on a copy: overlay a synthetic fault of one class on the parsed readings."""
        self.overrides.add(signal_class)

    def inject(self, fault_id, pack=None):
        raise AttributeError("library faults need the Demo Simulator; use inject_class() on exports")

    def _table(self, product, obj):
        t = super()._table(product, obj)
        if self.overrides & {"freshness", "slo"}:
            t = {**t, "lag_minutes": t["lag_minutes"] + 240}
        if "volume" in self.overrides:
            t = {**t, "rows": int(t["rows"] * 0.3)}
        if "schema" in self.overrides and t["columns"]:
            cols = dict(t["columns"])
            cols.pop(next(iter(cols)))
            t = {**t, "columns": cols}
        return t

    def _read_run(self, product, m):
        r = self.platform.orch.parse_run(self.platform.run(product, m["pipeline"]))
        hist = self._ev(f"run_history/{m['pipeline']}.json", [])
        prior = sorted(h["duration_min"] for h in hist[:-1]) or [r["duration_min"] or 1.0]
        if "run" in self.overrides:
            r = {**r, "status": "failed"}
        return {**r, "median_min": prior[len(prior) // 2], "evidence": f"orchestration://{m['pipeline']}/runs/latest"}

    def _read_quality(self, product, m):
        r = super()._read_quality(product, m)
        return {**r, "status": "fail", "failed_pct": max(r["failed_pct"], 1.0)} if "quality" in self.overrides else r

    def _read_cost(self, product, m):
        r = super()._read_cost(product, m)
        return {**r, "daily": round(r["daily"] * 3, 2)} if "cost" in self.overrides else r

    def _grants(self, product):
        g = super()._grants(product)
        return g + ["role:synthetic_probe SELECT bronze.*"] if "access" in self.overrides else g

    # -- facts and evidence -------------------------------------------------------------------
    def latest_window(self, product, pipeline):
        return self.platform.manifest.get("windows", {}).get(pipeline, "")

    def run_logs(self, product, pipeline):
        p = self.platform.dir / "orchestration" / f"{self.naming.pipeline(pipeline)}.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def run_history(self, product, pipeline, days=7):
        return self._ev(f"run_history/{pipeline}.json", [])

    def batch_stats(self, product, obj):
        return self._ev(f"batches/{obj}.json", [])

    def volume_breakdown(self, product, obj):
        return self._ev(f"breakdown/{obj}.json", {})

    def query_history(self, product):
        return self._ev("query_history.json", {})

    def upstream_notices(self, product):
        return self._ev("upstream_notices.json", [])

    def releases(self, product):
        return self._ev("releases.json", [])

    def profile_column(self, product, obj, column):
        return {}

    def cost_breakdown(self, product):
        cb = self._ev("cost_breakdown.json", None) or {"components": {}, "pipelines_compute": {}, "unallocated": 0.0}
        cb["total"] = self.platform.engine.parse_cost(self.platform.cost(product), self.platform.prices)
        cb.setdefault("day", self.platform.now.date().isoformat())
        return cb

    def query_profile(self, product):
        return self._ev("query_profile.json", {})

    def object_reads(self, product, window_days=90):
        return self._ev("object_reads.json", {})

    @property
    def actions(self):
        return {}  # read-only by construction: the executor finds no catalogue action to run


# -- exporter (reference implementation; used by the rehearsal) ------------------------------------
def export_snapshot(native, product: str, out_root: str | Path, snapshot_id: str, *,
                    synthetic: bool = True) -> Path:
    """Write one snapshot from a :class:`NativePlatform` (the Demo Simulator in native shapes)."""
    sim = native.sim
    st = sim.state[product]
    d = Path(out_root) / product / snapshot_id
    for sub in ("orchestration", "tables", "quality", "evidence/run_history", "evidence/batches",
                "evidence/breakdown"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    w = lambda rel, obj: (d / rel).write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    w("manifest.json", {"profile_id": native.profile["profile_id"], "product": product,
                        "captured_at": sim.now.isoformat(), "synthetic": synthetic,
                        "windows": {p: r["window"] for p, r in st["runs"].items()}})
    for p in st["runs"]:
        name = native.naming.pipeline(p)
        w(f"orchestration/{name}.json", native.run(product, p))
        (d / "orchestration" / f"{name}.log").write_text("\n".join(sim.run_logs(product, p)), encoding="utf-8")
        w(f"evidence/run_history/{p}.json", sim.run_history(product, p))
    for o in st["objects"]:
        w(f"tables/{native.naming.physical(o)}.json", native.table(product, o))
        w(f"evidence/batches/{o}.json", sim.batch_stats(product, o))
        w(f"evidence/breakdown/{o}.json", sim.volume_breakdown(product, o))
    for rule in st["quality"]:
        w(f"quality/{rule}.json", native.quality(product, rule))
    w("cost.json", native.cost(product))
    w("query_profile.json", native.query_profile(product))
    w("grants.json", native.grants(product))
    w("masking.json", native.masking(product))
    w("catalog.json", native.catalog_state(product))
    w("access_history.json", native.access_history(product))
    w("audit.json", native.audit_logging(product))
    w("evidence/query_history.json", sim.query_history(product))
    w("evidence/upstream_notices.json", sim.upstream_notices(product))
    w("evidence/releases.json", sim.releases(product))
    w("evidence/cost_breakdown.json", sim.cost_breakdown(product))
    w("evidence/object_reads.json", sim.object_reads(product))
    w("evidence/query_profile.json", sim.query_profile(product))
    return d
