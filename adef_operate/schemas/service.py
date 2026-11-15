"""Handover pack, service configuration and monitoring specification."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .records import SignalClass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class Objective(_Model):
    id: str
    kind: Literal["freshness", "completeness", "availability", "quality", "runtime", "cost"]
    object: str
    target: float
    unit: str
    description: str = ""


class Pipeline(_Model):
    id: str
    owner: str
    # None means "not declared"; the receiver check rejects that (assumption OA4).
    idempotent: Optional[bool] = None
    rollback_ref: str
    objects: list[str] = Field(default_factory=list)
    schedule: str = ""
    quarantine_path: Optional[str] = None


class QualityRule(_Model):
    id: str
    object: str
    blocking: bool
    description: str = ""


class HandoverPack(_Model):
    """Operations Handover Pack produced by Build (stage 7)."""

    dp_id: str
    name: str
    domain_pack: str
    regulated: bool = False
    owners: dict[str, str]
    on_call: str
    objectives: list[Objective]
    pipelines: list[Pipeline]
    quality_rules: list[QualityRule] = Field(default_factory=list)
    consumers: list[str] = Field(default_factory=list)
    lineage: list[tuple[str, str]] = Field(default_factory=list)
    budgets: dict[str, float] = Field(default_factory=dict)
    cost_estimate_daily: Optional[float] = None
    access_model: list[str] = Field(default_factory=list)
    schema_contract: dict[str, dict[str, str]] = Field(default_factory=dict)
    object_tags: dict[str, list[str]] = Field(default_factory=dict)
    build_g7_autonomy: dict[str, str] = Field(default_factory=dict)


class ServiceConfig(_Model):
    """``control/service.yaml``: scope, approvers, on-call and daily budgets."""

    dp_id: str
    name: str
    domain_pack: str
    approvers: dict[str, str]
    on_call: str
    budgets: dict[str, float]
    simulated: bool = False


class Monitor(_Model):
    id: str
    signal_class: SignalClass
    object: str
    pipeline: Optional[str] = None
    rule: dict[str, Any]
    owner: str = Field(min_length=1)
    severity_hint: Literal["S1", "S2", "S3", "S4"]
    runbook_entry: str = Field(min_length=1)
    objective: Optional[str] = None
    quality_rule: Optional[str] = None
    enabled: bool = True


class MonitoringSpec(_Model):
    dp_id: str
    version: int = 1
    monitors: list[Monitor]
