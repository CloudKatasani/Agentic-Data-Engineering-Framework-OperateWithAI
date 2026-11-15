"""Gate requests and decisions (spec section 6; backlog O2-3).

* One request per decision, tied to an artifact hash and an expiry.
* Decisions come from the named human holding a required role in ``service.yaml``;
  agent identities can never approve.
* Standing approvals (OG1) cover a manifest of per-file hashes. Editing any covered
  file voids the approval (OT-11).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..core.errors import ApprovalError
from ..core.hashing import canonical_hash, file_hash
from ..schemas.records import ApprovalDecision, ApprovalRequest
from .workspace import AGENTS, CODE, Workspace

# all_of: every listed role must approve. any_of: one of them is enough.
GATE_ROLES: dict[str, tuple[str, list[str]]] = {
    "OG0": ("all_of", ["product_owner", "platform_owner"]),
    "OG1": ("all_of", ["platform_owner"]),
    "OG2": ("any_of", ["service_owner", "platform_owner"]),
    "OG2-rollback": ("all_of", ["release_manager"]),
    "OG3": ("all_of", ["data_steward", "data_owner"]),
    "OG4": ("all_of", ["platform_owner"]),
    "OG5": ("all_of", ["security_compliance"]),
    "OG6": ("all_of", ["service_owner"]),
    "OG7": ("all_of", ["platform_owner", "tech_lead"]),
}

OG1_FILES = ("service/monitoring-spec.yaml", "service/runbook.md", "service/action-bindings.yaml")
OG1_VALIDITY = timedelta(days=90)
DEFAULT_EXPIRY = timedelta(hours=24)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Approvals:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.log = ws.chainlog("approvals", "control/approvals.jsonl")

    # -- helpers --------------------------------------------------------------------
    def _approvers(self) -> dict[str, str]:
        svc = self.ws.read_yaml("control/service.yaml") or {}
        return svc.get("approvers", {})

    def _records(self, kind: str) -> Iterable[dict[str, Any]]:
        for entry in self.log.entries():
            if entry["record"].get("kind") == kind:
                yield entry["record"]

    def _next_id(self) -> str:
        n = sum(1 for _ in self._records("request")) + 1
        return f"APR-{n:03d}"

    def get_request(self, request_id: str) -> ApprovalRequest:
        for rec in self._records("request"):
            if rec["id"] == request_id:
                return ApprovalRequest.model_validate({k: v for k, v in rec.items() if k != "kind"})
        raise ApprovalError(f"no approval request {request_id}")

    def decisions(self, request_id: str) -> list[ApprovalDecision]:
        return [ApprovalDecision.model_validate({k: v for k, v in r.items() if k != "kind"})
                for r in self._records("decision") if r["request_id"] == request_id]

    # -- request --------------------------------------------------------------------
    def request(self, *, gate: str, decision_asked: str, artifact_hash: str, requested_by: str,
                case_id: Optional[str] = None, evidence: Iterable[str] = (), risk: str = "",
                rollback: str = "", recommendation: str = "", action_type: Optional[str] = None,
                audience: Iterable[str] = (), expires: Optional[datetime] = None,
                role_key: Optional[str] = None, at: Optional[datetime] = None) -> ApprovalRequest:
        at = at or now_utc()
        roles = GATE_ROLES[role_key or gate][1]
        req = ApprovalRequest(
            id=self._next_id(), gate=gate, product=self.ws.dp_id, case_id=case_id,
            decision_asked=decision_asked, artifact_hash=artifact_hash, evidence=list(evidence),
            risk=risk, rollback=rollback, recommendation=recommendation, approver_roles=roles,
            action_type=action_type, audience=list(audience),
            expires=expires or at + DEFAULT_EXPIRY, requested_by=requested_by)
        self.log.append({"kind": "request", **req.model_dump(mode="json")}, at=at)
        self.ws.write("approvals", f"control/approval-requests/{req.id}.md", self._render(req))
        return req

    @staticmethod
    def _render(req: ApprovalRequest) -> str:
        lines = [f"# {req.id} - {req.gate}", "", f"**Decision asked:** {req.decision_asked}", "",
                 f"- Product: {req.product}", f"- Case: {req.case_id or '-'}",
                 f"- Artifact hash: `{req.artifact_hash}`", f"- Approver roles: {', '.join(req.approver_roles)}",
                 f"- Expires: {req.expires.isoformat()}", f"- Risk: {req.risk or '-'}",
                 f"- Rollback: {req.rollback or '-'}", f"- Recommendation: {req.recommendation or '-'}",
                 "", "## Evidence", *[f"- {e}" for e in req.evidence or ["(none)"]]]
        return "\n".join(lines) + "\n"

    # -- decide ---------------------------------------------------------------------
    def decide(self, request_id: str, *, approver: str, role: str, decision: str,
               artifact_hash: str, reason: str = "", at: Optional[datetime] = None) -> ApprovalDecision:
        at = at or now_utc()
        req = self.get_request(request_id)
        if approver in AGENTS or approver in CODE:
            raise ApprovalError(f"{approver!r} is an agent or code identity; only a named human may decide")
        if role not in req.approver_roles:
            raise ApprovalError(f"role {role!r} cannot decide {req.gate}; needs {req.approver_roles}")
        named = self._approvers().get(role)
        if not named:
            raise ApprovalError(f"no person is named for role {role!r} in control/service.yaml")
        if named != approver:
            raise ApprovalError(f"{approver!r} does not hold role {role!r} (named holder: {named!r})")
        if at > req.expires:
            raise ApprovalError(f"{request_id} expired at {req.expires.isoformat()}")
        if artifact_hash != req.artifact_hash:
            raise ApprovalError(f"{request_id}: decision hash does not match the requested artifact hash")
        dec = ApprovalDecision(request_id=request_id, decision=decision, approver=approver, role=role,
                               reason=reason, artifact_hash=artifact_hash, at=at)
        self.log.append({"kind": "decision", **dec.model_dump(mode="json")}, at=at)
        self.ws.append("approvals", "control/decision-log.md",
                       f"- {at.isoformat()} {request_id} {req.gate} {decision} by {approver} ({role})"
                       + (f": {reason}" if reason else ""))
        return dec

    def status(self, request_id: str) -> str:
        """``approved``, ``rejected`` or ``pending``."""
        req = self.get_request(request_id)
        decs = self.decisions(request_id)
        if any(d.decision == "rejected" for d in decs):
            return "rejected"
        approved_roles = {d.role for d in decs if d.decision == "approved"}
        mode = "any_of" if req.gate == "OG2" and "release_manager" not in req.approver_roles else "all_of"
        needed = set(req.approver_roles)
        ok = bool(approved_roles & needed) if mode == "any_of" else needed <= approved_roles
        return "approved" if ok else "pending"

    def require(self, request_id: str, *, gate: str, artifact_hash: str,
                at: Optional[datetime] = None) -> ApprovalRequest:
        """Raise unless ``request_id`` is an approved, unexpired ``gate`` for this exact hash."""
        at = at or now_utc()
        req = self.get_request(request_id)
        if req.gate != gate:
            raise ApprovalError(f"{request_id} is a {req.gate} approval; {gate} is required")
        if req.artifact_hash != artifact_hash:
            raise ApprovalError(f"{request_id} was given for a different artifact (hash mismatch)")
        if at > req.expires:
            raise ApprovalError(f"{request_id} expired at {req.expires.isoformat()}")
        state = self.status(request_id)
        if state != "approved":
            raise ApprovalError(f"{request_id} is {state}, not approved")
        return req

    # -- OG1 standing approval ------------------------------------------------------
    def og1_manifest(self) -> dict[str, str]:
        return {f: file_hash(self.ws.path(f)) for f in OG1_FILES if self.ws.has(f)}

    def request_og1(self, *, requested_by: str = "onboarding", at: Optional[datetime] = None,
                    evidence: Iterable[str] = ("service/readiness-report.md",)) -> ApprovalRequest:
        at = at or now_utc()
        manifest = self.og1_manifest()
        missing = [f for f in OG1_FILES if f not in manifest]
        if missing:
            raise ApprovalError(f"OG1 cannot be requested; missing {missing}")
        req = self.request(gate="OG1", decision_asked="Operational readiness: monitoring spec, runbook, "
                           "action bindings and initial autonomy levels",
                           artifact_hash=canonical_hash(manifest), requested_by=requested_by,
                           evidence=evidence, expires=at + OG1_VALIDITY, at=at)
        self.log.append({"kind": "og1_manifest", "request_id": req.id, "manifest": manifest}, at=at)
        return req

    def _latest_og1(self) -> tuple[Optional[ApprovalRequest], dict[str, str]]:
        latest, manifest = None, {}
        for rec in self._records("og1_manifest"):
            if self.status(rec["request_id"]) == "approved":
                latest, manifest = self.get_request(rec["request_id"]), rec["manifest"]
        return latest, manifest

    def og1_status(self, at: Optional[datetime] = None) -> tuple[bool, str]:
        """(valid, reason). Valid only if approved, unexpired and every covered file unchanged."""
        at = at or now_utc()
        req, manifest = self._latest_og1()
        if req is None:
            return False, "no approved OG1"
        if at > req.expires:
            return False, f"OG1 {req.id} expired at {req.expires.isoformat()}"
        current = self.og1_manifest()
        changed = [f for f, h in manifest.items() if current.get(f) != h]
        if changed:
            return False, f"OG1 {req.id} voided: {', '.join(changed)} changed since approval"
        return True, f"OG1 {req.id} valid"

    def approved_spec_hash(self) -> Optional[str]:
        """Hash of ``monitoring-spec.yaml`` in the latest approved OG1 manifest."""
        _, manifest = self._latest_og1()
        return manifest.get("service/monitoring-spec.yaml")
