"""Default action catalogue for version one (spec section 6)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.autonomy import ActionClass, Level


@dataclass(frozen=True)
class CatalogueEntry:
    action_type: str
    action_class: ActionClass
    start: Level
    highest: Level
    gate: str | None = None
    preconditions: tuple[str, ...] = field(default_factory=tuple)
    executed_by_people: bool = False


_E = CatalogueEntry
A2, A3 = ActionClass.A2, ActionClass.A3
L1, L2, L3 = Level.L1, Level.L2, Level.L3

DEFAULT_CATALOGUE: dict[str, CatalogueEntry] = {e.action_type: e for e in [
    _E("retry_failed_task", A2, L1, L3, None, ("idempotent", "retry_cap", "no_open_s1")),
    _E("rerun_latest_window", A2, L1, L3, None, ("idempotent", "latest_window")),
    _E("pause_pipeline", A2, L1, L3, None, ("blocking_evidence", "downstream_listed")),
    _E("quarantine_batch", A2, L1, L3, None, ("quarantine_path", "batch_evidenced")),
    _E("set_trust_status", A2, L3, L3, None, ("rule_based",)),
    _E("rerun_past_window", A3, L1, L2, "OG2", ("idempotent",)),
    _E("rollback_request", A3, L1, L2, "OG2", ()),
    _E("backfill", A3, L1, L2, "OG3", ("idempotent", "rollback_plan")),
    _E("correct_data", A3, L1, L2, "OG3", ("rollback_plan",)),
    _E("resize", A3, L1, L2, "OG4", ()),
    _E("reschedule", A3, L1, L2, "OG4", ()),
    _E("drop_object", A3, L1, L2, "OG4", ("lineage_and_usage_evidence",)),
    _E("revoke_access", A3, L1, L1, "OG5", (), True),
    _E("change_policy", A3, L1, L1, "OG5", (), True),
    _E("send_notice", A3, L1, L2, "OG6", ("text_hash",)),
]}

RERUN_TYPES = {"retry_failed_task", "rerun_latest_window", "rerun_past_window", "backfill"}


def lookup(action_type: str) -> CatalogueEntry:
    """Unknown action types are treated as A3 at L1 (backlog O2-1)."""
    return DEFAULT_CATALOGUE.get(action_type) or CatalogueEntry(action_type, A3, L1, L1, "OG2")
