"""Product registry record: autonomy levels, earned autonomy, budgets, emergency stop.

Backlog O2-1 (levels and hook check), O2-4 (earned autonomy and demotion), O2-6
(per-product daily budgets and emergency stop).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..core.autonomy import Level
from ..core.errors import ApprovalError, BudgetExceeded
from .approvals import Approvals
from .catalogue import DEFAULT_CATALOGUE, lookup
from .escalation import escalate
from .workspace import Workspace, adef_home

BUDGET_KINDS = ("model_spend", "diagnostic_query_spend", "actions")
WARN_AT = 0.8


def global_stop_path(home: str | Path | None = None) -> Path:
    return Path(home or adef_home()) / "ops" / "EMERGENCY_STOP"


class Registry:
    PATH = "control/registry.yaml"

    def __init__(self, ws: Workspace, home: str | Path | None = None):
        self.ws = ws
        self.home = home if home is not None else ws.root.parent.parent

    # -- persistence ----------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        data = self.ws.read_yaml(self.PATH)
        if data is None:
            data = {"dp_id": self.ws.dp_id, "stopped": None,
                    "levels": {t: e.start.name for t, e in DEFAULT_CATALOGUE.items()},
                    "track_record": {}, "trust_status": "trusted"}
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.ws.write_yaml("registry", self.PATH, data)

    def init(self, proposed: Optional[dict[str, str]] = None) -> dict[str, Any]:
        data = self.load()
        for t, lvl in (proposed or {}).items():
            entry = lookup(t)
            data["levels"][t] = min(Level.parse(lvl), entry.highest).name
        self.save(data)
        return data

    # -- levels ---------------------------------------------------------------------
    def stored_level(self, action_type: str) -> Level:
        entry = lookup(action_type)
        if action_type not in DEFAULT_CATALOGUE:
            return Level.L1
        lvl = Level.parse(self.load()["levels"].get(action_type, entry.start.name))
        return min(lvl, entry.highest)

    def effective_level(self, action_type: str, at: Optional[datetime] = None) -> tuple[Level, str]:
        """The level the hook enforces now, with the reason for any cap."""
        stop = self.stop_state()
        if stop:
            return Level.L0, f"stopped: {stop}"
        lvl = self.stored_level(action_type)
        valid, why = Approvals(self.ws).og1_status(at)
        if not valid and lvl > Level.L1:
            return Level.L1, why
        return lvl, "registry level"

    def set_level(self, action_type: str, level: str | Level, *, approval_id: str,
                  approvals: Approvals, artifact_hash: str) -> None:
        """Promotion or demotion by people: needs an approved OG7 for this exact proposal."""
        approvals.require(approval_id, gate="OG7", artifact_hash=artifact_hash)
        entry = lookup(action_type)
        new = Level.parse(level)
        if new > entry.highest:
            raise ApprovalError(f"{action_type} cannot exceed {entry.highest.name}")
        data = self.load()
        data["levels"][action_type] = new.name
        self.save(data)

    # -- earned autonomy (O2-4) -----------------------------------------------------
    def record_outcome(self, action_type: str, *, verified: bool, case_id: str | None = None) -> None:
        data = self.load()
        tr = data["track_record"].setdefault(action_type, {"verified": 0, "failed": 0})
        if verified:
            tr["verified"] += 1
            self.save(data)
            return
        tr["failed"] += 1
        before = Level.parse(data["levels"].get(action_type, "L1"))
        if before > Level.L1:
            data["levels"][action_type] = Level.L1.name
        self.save(data)
        escalate(self.ws, by="registry", case_id=case_id,
                 reason=f"{action_type} failed verification; demoted {before.name} -> L1")

    # -- stop -----------------------------------------------------------------------
    def stop_state(self) -> Optional[str]:
        g = global_stop_path(self.home)
        if g.exists():
            return "global emergency stop: " + g.read_text(encoding="utf-8").strip()
        return self.load().get("stopped")

    def stop(self, *, by: str, reason: str) -> None:
        data = self.load()
        data["stopped"] = f"{reason} (by {by})"
        self.save(data)
        escalate(self.ws, by="registry", reason=f"product set to L0: {reason} (by {by})")

    @staticmethod
    def emergency_stop(*, by: str, reason: str, home: str | Path | None = None) -> Path:
        p = global_stop_path(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{reason} (by {by}, {datetime.now(timezone.utc).isoformat()})\n", encoding="utf-8")
        return p

    # -- budgets (O2-6) -------------------------------------------------------------
    def _spent(self, kind: str, day: date) -> float:
        total = 0.0
        if self.ws.has("control/budget-ledger.jsonl"):
            for e in self.ws.chainlog("registry", "control/budget-ledger.jsonl").entries():
                r = e["record"]
                if r["kind"] == kind and r["day"] == day.isoformat():
                    total += float(r["amount"])
        return total

    def charge(self, kind: str, amount: float, *, by: str, day: Optional[date] = None) -> str:
        """Record spend. Returns ``ok`` or ``warn``; raises on breach after setting L0."""
        if kind not in BUDGET_KINDS:
            raise ValueError(f"unknown budget kind {kind!r}")
        day = day or datetime.now(timezone.utc).date()
        svc = self.ws.read_yaml("control/service.yaml") or {}
        cap = (svc.get("budgets") or {}).get(kind)
        spent = self._spent(kind, day) + amount
        if cap is not None and spent > cap:
            self.stop(by="registry", reason=f"daily {kind} budget exceeded ({spent:g} > {cap:g})")
            raise BudgetExceeded(f"daily {kind} budget exceeded ({spent:g} > {cap:g}); "
                                 f"product agents set to L0", escalate=True)
        self.ws.chainlog("registry", "control/budget-ledger.jsonl").append(
            {"kind": kind, "amount": amount, "day": day.isoformat(), "by": by})
        if cap is not None and spent >= WARN_AT * cap:
            return "warn"
        return "ok"
