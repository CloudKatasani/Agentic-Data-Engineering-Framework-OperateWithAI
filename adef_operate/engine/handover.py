"""Handover receiver check and ``product.released`` consumer (backlog O1-5; T1 of the Commander).

The receiver check lists *every* gap at once, so an incomplete pack is rejected once with
the complete list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core.hashing import canonical_hash
from ..schemas.service import HandoverPack, ServiceConfig
from .approvals import Approvals
from .registry import Registry
from .workspace import Workspace

REQUIRED_ROLES = ("product_owner", "service_owner", "platform_owner", "data_steward",
                  "data_owner", "security_compliance")
OPTIONAL_ROLES = ("release_manager", "tech_lead")
DEFAULT_BUDGETS = {"model_spend": 50.0, "diagnostic_query_spend": 20.0, "actions": 20}


@dataclass
class ReceiverResult:
    accepted: bool
    gaps: list[str] = field(default_factory=list)
    pack: Optional[HandoverPack] = None
    workspace: Optional[Workspace] = None
    og0_request: Optional[str] = None


def receiver_check(raw: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    for key in ("dp_id", "name", "domain_pack", "owners", "on_call", "objectives", "pipelines"):
        if not raw.get(key):
            gaps.append(f"missing {key}")
    owners = raw.get("owners") or {}
    for role in REQUIRED_ROLES:
        if not owners.get(role):
            gaps.append(f"owners.{role} is not named")
    if owners.get("service_owner") and owners.get("service_owner") == owners.get("platform_owner"):
        gaps.append("service_owner and platform_owner are the same person; OG2 needs a second named person")
    for i, obj in enumerate(raw.get("objectives") or []):
        oid = obj.get("id", f"#{i}")
        if not isinstance(obj.get("target"), (int, float)) or isinstance(obj.get("target"), bool):
            gaps.append(f"objective {oid} has no numeric target")
        if not obj.get("object"):
            gaps.append(f"objective {oid} names no object")
    for i, p in enumerate(raw.get("pipelines") or []):
        pid = p.get("id", f"#{i}")
        if not p.get("owner"):
            gaps.append(f"pipeline {pid} has no owner")
        if not isinstance(p.get("idempotent"), bool):
            gaps.append(f"pipeline {pid} has no idempotency declaration")
        if not p.get("rollback_ref"):
            gaps.append(f"pipeline {pid} has no rollback reference")
    if not gaps:
        try:
            HandoverPack.model_validate(raw)
        except Exception as exc:  # pydantic detail as a single gap
            gaps.append(f"pack does not match the handover schema: {exc}")
    return gaps


def accept_handover(raw: dict[str, Any], *, home: str | Path | None = None,
                    simulated: bool = False) -> ReceiverResult:
    """Run the receiver check; on success create the workspace and request OG0."""
    gaps = receiver_check(raw)
    if gaps:
        return ReceiverResult(accepted=False, gaps=gaps)
    pack = HandoverPack.model_validate(raw)
    ws = Workspace(pack.dp_id, home).create()
    approvers = {r: pack.owners[r] for r in (*REQUIRED_ROLES, *OPTIONAL_ROLES) if pack.owners.get(r)}
    svc = ServiceConfig(dp_id=pack.dp_id, name=pack.name, domain_pack=pack.domain_pack,
                        approvers=approvers, on_call=pack.on_call,
                        budgets={**DEFAULT_BUDGETS, **pack.budgets}, simulated=simulated)
    ws.write_yaml("commander", "control/service.yaml", svc.model_dump(mode="json"))
    ws.write_yaml("commander", "control/handover-pack.yaml", pack.model_dump(mode="json"))
    Registry(ws).init(pack.build_g7_autonomy)
    req = Approvals(ws).request(
        gate="OG0", requested_by="commander",
        decision_asked=f"Accept {pack.dp_id} into operations: scope, objectives, owner, on-call, budgets",
        artifact_hash=canonical_hash(svc.model_dump(mode="json")),
        evidence=["control/service.yaml", "control/handover-pack.yaml"])
    return ReceiverResult(accepted=True, pack=pack, workspace=ws, og0_request=req.id)


def on_product_released(event: dict[str, Any], *, home: str | Path | None = None,
                        simulated: bool = False) -> ReceiverResult:
    """Consumer for the suite event ``product.released``."""
    if event.get("type") != "product.released":
        return ReceiverResult(accepted=False, gaps=[f"unexpected event type {event.get('type')!r}"])
    pack = event.get("handover_pack")
    if not isinstance(pack, dict):
        return ReceiverResult(accepted=False, gaps=["event carries no handover_pack"])
    return accept_handover(pack, home=home, simulated=simulated)
