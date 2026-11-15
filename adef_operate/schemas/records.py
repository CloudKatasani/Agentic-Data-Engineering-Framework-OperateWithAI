"""SIG, case and ACT records (OPERATE_SPEC.md sections 5 and 8, backlog O1-4)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..core.errors import ValidationFailed

Scalar = Union[int, float, bool, str, None]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SignalClass(str, Enum):
    run = "run"
    freshness = "freshness"
    volume = "volume"
    schema = "schema"
    quality = "quality"
    slo = "slo"
    cost = "cost"
    access = "access"
    consumer = "consumer"
    upstream = "upstream"


class Signal(_Strict):
    """``SIG-######``. Statistics and metadata only, never row values."""

    id: str = Field(pattern=r"^SIG-\d{6}$")
    signal_class: SignalClass
    product: str = Field(min_length=1)
    object: str = Field(min_length=1)
    observed: Scalar
    expected: Scalar
    evidence_path: str = Field(min_length=1)
    time: datetime
    monitor_id: Optional[str] = None
    detail: str = Field(default="", max_length=500)
    simulated: bool = False

    @field_validator("detail")
    @classmethod
    def _no_row_dump(cls, v: str) -> str:
        # A crude guard against copying result sets into a signal: multi-line tabular text.
        if v.count("\n") > 3 or v.count("|") > 6:
            raise ValueError("detail looks like row values; signals hold statistics and metadata only")
        return v


class CaseType(str, Enum):
    INC = "INC"
    PRB = "PRB"
    FIN = "FIN"
    GOV = "GOV"
    CHG = "CHG"


class Severity(str, Enum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"


class TrustStatus(str, Enum):
    trusted = "trusted"
    degraded = "degraded"
    do_not_use = "do-not-use"


class TimelineEntry(_Strict):
    at: datetime = Field(default_factory=utcnow)
    by: str
    event: str


class LinkedCase(_Strict):
    id: str
    reason: str = Field(min_length=1)


class MonitorRef(_Strict):
    """The monitor that detected a case and the spec hash it had at detection time."""

    monitor_id: str
    spec_hash: str


class Case(_Strict):
    id: str
    type: CaseType
    product: str
    state: str
    severity: Optional[Severity] = None
    severity_rule: Optional[str] = None
    owner: Optional[str] = None
    signals: list[str] = Field(default_factory=list)
    signal_group: Optional[str] = None
    first_failing_object: Optional[str] = None
    severity_would_lower: Optional[str] = None
    confirmed_cause: Optional[str] = None
    confirmed_by: Optional[str] = None
    linked_cases: list[LinkedCase] = Field(default_factory=list)
    detecting_monitors: list[MonitorRef] = Field(default_factory=list)
    trust_status: Optional[TrustStatus] = None
    failed_attempts: int = 0
    escalated: bool = False
    escalation_reason: Optional[str] = None
    decision: Optional[Literal["accepted", "rejected", "deferred"]] = None
    next_action: Optional[str] = None
    open_questions: list[str] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _id_matches_type(self) -> "Case":
        if not re.fullmatch(r"(INC|PRB|FIN|GOV|CHG)-\d{3,}", self.id):
            raise ValueError(f"case id {self.id!r} must look like INC-001")
        if not self.id.startswith(f"{self.type}-"):
            raise ValueError(f"case id {self.id!r} does not match case type {self.type}")
        return self


class ActionRecord(_Strict):
    """``ACT-###``: a request to the executor to run one catalogue action."""

    id: str = Field(pattern=r"^ACT-\d{3,}$")
    case_id: str
    product: str
    action_type: str = Field(min_length=1)
    pipeline: Optional[str] = None
    target: str = Field(min_length=1)
    params: dict[str, Scalar] = Field(default_factory=dict)
    requested_by: str
    approval_id: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)
    downstream_impact: list[str] = Field(default_factory=list)
    rollback_plan: Optional[str] = None
    created: datetime = Field(default_factory=utcnow)


class ApprovalRequest(_Strict):
    id: str = Field(pattern=r"^APR-\d{3,}$")
    gate: Literal["OG0", "OG1", "OG2", "OG3", "OG4", "OG5", "OG6", "OG7"]
    product: str
    case_id: Optional[str] = None
    decision_asked: str = Field(min_length=1)
    artifact_hash: str
    evidence: list[str] = Field(default_factory=list)
    risk: str = ""
    rollback: str = ""
    recommendation: str = ""
    approver_roles: list[str]
    action_type: Optional[str] = None
    audience: list[str] = Field(default_factory=list)
    expires: datetime
    requested_by: str


class ApprovalDecision(_Strict):
    request_id: str
    decision: Literal["approved", "rejected"]
    approver: str = Field(min_length=1)
    role: str
    reason: str = ""
    artifact_hash: str
    at: datetime = Field(default_factory=utcnow)


_MODELS: dict[str, type[BaseModel]] = {
    "signal": Signal,
    "case": Case,
    "action": ActionRecord,
    "approval_request": ApprovalRequest,
    "approval_decision": ApprovalDecision,
}


def validate_record(kind: str, data: dict[str, Any]) -> BaseModel:
    """Validate ``data`` as a record of ``kind``; raise :class:`ValidationFailed` on error."""
    model = _MODELS.get(kind)
    if model is None:
        raise ValidationFailed(f"unknown record kind {kind!r}")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        )
        raise ValidationFailed(f"invalid {kind} record: {problems}") from exc
