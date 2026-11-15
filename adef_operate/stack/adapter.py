"""``StackAdapter``: Operate's adapter for any bound Stack Profile.

Signals are read as native payloads and parsed by the slot's translator; bronze and silver
come from the open table format, gold from the serving engine. Everything returned to
agents is logical (names, types, roles) and vendor-free; evidence paths name the slot, not
the tool. Actions run through the platform and are logged in the adapter's own command
log; case files receive only an opaque reference.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..connectors.base import ActionSpec, OperateAdapter, SignalSpec
from .native import NativePlatform


class StackAdapter(OperateAdapter):
    def __init__(self, profile: dict, platform: NativePlatform):
        super().__init__(name=f"stack:{profile['profile_id']}")
        self.profile = profile
        self.platform = platform
        self.naming = platform.naming
        self.command_log: list[dict] = []

    @classmethod
    def over(cls, profile: dict, sim) -> "StackAdapter":
        return cls(profile, NativePlatform(profile, sim))

    @property
    def sim(self):
        return self.platform.sim

    @property
    def now(self):
        return self.platform.now

    # -- scenario passthrough (Demo Simulator only) ----------------------------------------
    def inject(self, fault_id: str, pack: str | None = None) -> dict:
        return self.sim.inject(fault_id, pack)

    def history(self, days: int = 28) -> list[tuple[str, "StackAdapter"]]:
        return [(d, StackAdapter.over(self.profile, s)) for d, s in self.sim.history(days)]

    # -- signal reads ------------------------------------------------------------------------
    def _table(self, product: str, obj: str) -> dict:
        return self.platform.table_reader(obj).parse_table(self.platform.table(product, obj), self.now)

    def _read_run(self, product, m):
        r = self.platform.orch.parse_run(self.platform.run(product, m["pipeline"]))
        return {**r, "median_min": self.sim.state[product]["runs"][m["pipeline"]]["median_min"],
                "evidence": f"orchestration://{m['pipeline']}/runs/latest"}

    def _read_freshness(self, product, m):
        return {"lag_minutes": self._table(product, m["object"])["lag_minutes"],
                "evidence": f"data_platform://{m['object']}/freshness"}

    def _read_volume(self, product, m):
        t = self._table(product, m["object"])
        return {"rows": t["rows"], "expected_rows": t["expected_rows"],
                "evidence": f"data_platform://{m['object']}/volume"}

    def _read_schema(self, product, m):
        return {"columns": self._table(product, m["object"])["columns"],
                "evidence": f"data_platform://{m['object']}/schema"}

    def _read_quality(self, product, m):
        r = self.platform.quality_tool.parse(self.platform.quality(product, m["rule"]["rule_id"]))
        return {**r, "evidence": f"data_quality://{m['rule']['rule_id']}"}

    def _read_slo(self, product, m):
        lag = self._table(product, m["object"])["lag_minutes"]
        return {"burn_rate": round(lag / float(m["rule"]["objective_minutes"]), 3),
                "evidence": f"data_platform://{m['object']}/slo"}

    def _read_cost(self, product, m):
        daily = self.platform.engine.parse_cost(self.platform.cost(product), self.platform.prices)
        qp = self.platform.engine.parse_query_profile(self.platform.query_profile(product))
        top = self.naming.logical(qp["top_physical"]) if qp["top_physical"] else None
        return {"daily": daily, "full_scans": qp["full_scans"], "top_object": top,
                "evidence": "cost_billing://daily"}

    def _grants(self, product) -> list[str]:
        out = []
        for role, priv, obj in self.platform.engine.parse_grants(self.platform.grants(product)):
            out.append(f"{self.naming.logical_role(role)} {priv} {self.naming.logical(obj)}")
        return out

    def _read_access(self, product, m):
        return {"grants": self._grants(product), "evidence": "access_audit://grants"}

    @property
    def signals(self) -> dict[str, SignalSpec]:
        return {c: SignalSpec(c, getattr(self, f"_read_{c}"))
                for c in ("run", "freshness", "volume", "schema", "quality", "slo", "cost", "access")}

    # -- governance ----------------------------------------------------------------------------
    def governance_state(self, product: str) -> dict:
        cat = self.platform.catalog.parse(self.platform.catalog_state(product))
        lg = self.naming.logical
        masks = {}
        for k, v in self.platform.engine.parse_masking(self.platform.masking(product)).items():
            table, col = k.rsplit(".", 1)
            masks[f"{lg(table)}.{col}"] = v
        return {"grants": self._grants(product), "masking": masks,
                "retention": {lg(o): d for o, d in cat["retention"].items()},
                "tags": {lg(o): t for o, t in cat["tags"].items()},
                "objects": sorted(lg(o) for o in cat["objects"]),
                "role_last_used_days": {self.naming.logical_role(r): d
                                        for r, d in self.platform.access_history(product).items()},
                "audit_logging": self.platform.audit_logging(product)}

    # -- facts for the hook and evidence reads -------------------------------------------------
    def latest_window(self, product: str, pipeline: str) -> str:
        return self.sim.latest_window(product, pipeline)

    def run_logs(self, product: str, pipeline: str) -> list[str]:
        return self.sim.run_logs(product, pipeline)

    # Evidence reads below pass through unchanged for now (metadata already logical).
    def run_history(self, product, pipeline, days=7):
        return self.sim.run_history(product, pipeline, days)

    def batch_stats(self, product, obj):
        return self.sim.batch_stats(product, obj)

    def volume_breakdown(self, product, obj):
        return self.sim.volume_breakdown(product, obj)

    def query_history(self, product):
        return self.sim.query_history(product)

    def upstream_notices(self, product):
        return self.sim.upstream_notices(product)

    def releases(self, product):
        return self.sim.releases(product)

    def profile_column(self, product, obj, column):
        return self.sim.profile_column(product, obj, column)

    def cost_breakdown(self, product):
        cb = self.sim.cost_breakdown(product)
        cb["total"] = self.platform.engine.parse_cost(self.platform.cost(product), self.platform.prices)
        return cb

    def query_profile(self, product):
        return self.sim.query_profile(product)

    def object_reads(self, product, window_days=90):
        return self.sim.object_reads(product, window_days)

    # -- actions --------------------------------------------------------------------------------
    @property
    def actions(self) -> dict[str, ActionSpec]:
        out = {}
        for name, spec in self.sim.actions.items():
            out[name] = ActionSpec(name, spec.action_class, self._wrap(name, spec.call), spec.verification,
                                   spec.reverse, spec.preconditions)
        return out

    def _wrap(self, name, call):
        def run(product: str, params: dict) -> dict:
            ref = f"CMD-{len(self.command_log) + 1:04d}"
            self.command_log.append({"ref": ref, "product": product, "action": name,
                                     "native": self.platform.command(name, params.get("pipeline")), "params": params})
            return {**call(product, params), "native_ref": ref}
        return run

    def run_action(self, action_type, product, params):
        return self.actions[action_type].call(product, params)
