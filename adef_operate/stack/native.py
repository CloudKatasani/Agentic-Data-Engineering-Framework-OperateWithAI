"""Offline emulation of a bound platform: serves the simulator's canonical state as each
tool's native payload, under physical names. Real adapters replace this with API and
system-table calls; the parse side in ``translators`` is unchanged.
"""

from __future__ import annotations

from typing import Any

from . import translators as T
from .naming import Naming
from .profiles import tool

LAKE_LAYERS = ("bronze", "silver")


class NativePlatform:
    def __init__(self, profile: dict, sim):
        self.profile = profile
        self.sim = sim
        self.naming = Naming(profile)
        self.orch = T.ORCHESTRATION[tool(profile, "orchestration")]
        self.table_format = T.TABLE_FORMATS[tool(profile, "table_format")]
        self.engine = T.ENGINES[tool(profile, "compute_engine")]
        self.quality_tool = T.QUALITY[tool(profile, "data_quality")]
        self.catalog = T.CATALOGS[tool(profile, "catalog_governance")]
        self.prices = (profile.get("variables") or {}).get("prices", {})

    @property
    def now(self):
        return self.sim.now

    def _s(self, product: str) -> dict:
        return self.sim.state[product]

    def table_reader(self, logical: str) -> T.TableReader:
        return self.table_format if logical.split(".")[0] in LAKE_LAYERS else self.engine

    # -- reads (native payloads) --------------------------------------------------------
    def run(self, product: str, pipeline: str) -> dict:
        return self.orch.emit_run(self.naming.pipeline(pipeline), self._s(product)["runs"][pipeline], self.now)

    def table(self, product: str, logical: str) -> dict:
        return self.table_reader(logical).emit_table(self.naming.physical(logical), self._s(product)["objects"][logical],
                                                     self.now)

    def quality(self, product: str, rule_id: str) -> dict:
        q = self._s(product)["quality"][rule_id]
        rows = self._s(product)["objects"][q["object"]]["rows"]
        return self.quality_tool.emit(rule_id, self.naming.physical(q["object"]), q["status"], q["failed_pct"], rows)

    def cost(self, product: str) -> dict:
        return self.engine.emit_cost(self._s(product)["cost"]["daily"], self.prices)

    def query_profile(self, product: str) -> dict:
        c = self._s(product)["cost"]
        top = c.get("top_object")
        return self.engine.emit_query_profile(c["full_scans"], self.naming.physical(top) if top else None)

    def grants(self, product: str) -> list[dict]:
        rows = []
        for g in self._s(product)["grants"]:
            role, priv, obj = g.split()
            rows.append((self.naming.role(role), priv, self.naming.physical(obj)))
        return self.engine.emit_grants(rows)

    def masking(self, product: str) -> list[dict]:
        out = {}
        for k, v in self._s(product)["masking"].items():
            table, col = k.rsplit(".", 1)
            out[f"{self.naming.physical(table)}.{col}"] = v
        return self.engine.emit_masking(out)

    def catalog_state(self, product: str) -> dict:
        s = self._s(product)
        phys = self.naming.physical
        return self.catalog.emit({phys(o): t for o, t in s["catalog_tags"].items()},
                                 {phys(o): d for o, d in s["retention"].items()},
                                 [phys(o) for o in s["catalog_objects"]])

    def access_history(self, product: str) -> dict:
        return {self.naming.role(r): d for r, d in self._s(product)["role_usage"].items()}

    def audit_logging(self, product: str) -> dict:
        return dict(self._s(product)["audit_logging"])

    # -- actions --------------------------------------------------------------------------
    def command(self, action_type: str, pipeline: str | None) -> str:
        if action_type == "quarantine_batch":
            return T.QUARANTINE_COMMAND.get(tool(self.profile, "table_format"), "quarantine via the table format")
        if action_type == "set_trust_status":
            return self.catalog.trust_command
        if action_type in ("retry_failed_task", "rerun_latest_window", "rerun_past_window", "pause_pipeline"):
            return self.orch.command(action_type, self.naming.pipeline(pipeline or ""))
        return f"{action_type}: executed through the platform's change process"
