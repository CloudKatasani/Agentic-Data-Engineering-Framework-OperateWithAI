"""O6 ``adef-ops-governance-compliance``: detect control drift; assemble audit evidence.

Recommend-only: no grant, revoke, mask, tag or policy change. A suspected security
event or data exposure is escalated to Security and Compliance at once, then the agent
stops collecting. Unclassified is treated as Restricted. "Not verifiable" is a finding.
It reports against the listed controls only; legal compliance is not its call.
"""

from __future__ import annotations

import csv
import fnmatch
import io
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.chainlog import ChainLog
from ..core.hashing import file_hash, text_hash
from ..engine.approvals import Approvals
from ..engine.case_store import CaseStore
from ..engine.escalation import escalate, escalations
from ..engine.workspace import Workspace
from .commander import CommanderAgent

DORMANT_DAYS = 90
INDEX = "governance/findings-index.yaml"
CONTROLS = {
    "grants": "Grants and role membership match the approved access model",
    "dormant": "Privileged roles are in use (no dormant privileged access)",
    "masking": "Restricted columns carry the approved masking policy",
    "classification": "Every object is classified",
    "retention": "Retention settings match requirement",
    "logging": "Audit and access logging is continuous",
}
ENFORCEMENT = [
    ("Monitors are evaluated by code", "monitor engine runs only the OG1-approved spec hash", "control/approvals.jsonl"),
    ("Only catalogue actions touch production", "executor checks catalogue, adapter and level", "control/action-log.jsonl"),
    ("Approvals by named humans, hash-tied", "approvals module", "control/approvals.jsonl"),
    ("Retry, loop and budget caps", "executor counters and registry budget ledger", "control/budget-ledger.jsonl"),
    ("Escalations recorded", "escalation log", "control/escalations.jsonl"),
]
LOGS = ["control/approvals.jsonl", "control/action-log.jsonl", "control/escalations.jsonl", "control/budget-ledger.jsonl"]


def parse_grant(g: str) -> dict:
    role, priv, obj = (g.split() + ["", "", ""])[:3]
    return {"role": role, "privilege": priv, "object": obj, "raw": g}


class GovernanceAgent:
    identity = "governance"

    def __init__(self, ws: Workspace, adapter, commander: Optional[CommanderAgent] = None):
        self.ws = ws
        self.adapter = adapter
        self.cases = CaseStore(ws)
        self.commander = commander or CommanderAgent(ws, adapter)
        self.pack = ws.read_yaml("control/handover-pack.yaml") or {}
        self.sd = ws.read_yaml("service/service-definition.yaml") or {}

    # -- baseline ---------------------------------------------------------------------
    def _restricted_objects(self) -> set[str]:
        return set(self.pack.get("restricted_columns", {}))

    def _waived(self, key: str, at: datetime) -> Optional[str]:
        appr = Approvals(self.ws)
        for r in appr._records("request"):
            if r["gate"] == "OG5" and r["decision_asked"].startswith("waiver:") and r["artifact_hash"] == text_hash(key):
                if appr.status(r["id"]) == "approved":
                    exp = datetime.fromisoformat(r["expires"])
                    return f"waived by {r['id']} until {r['expires']}" if at <= exp else f"EXPIRED waiver {r['id']}"
        return None

    # -- T1 / T2 -----------------------------------------------------------------------
    def compare(self, at: Optional[datetime] = None) -> dict[str, Any]:
        at = at or datetime.now(timezone.utc)
        live = self.adapter.governance_state(self.ws.dp_id)
        results: dict[str, dict] = {}
        drifts: list[dict] = []

        def nv(control):
            results[control] = {"status": "not verifiable", "detail": "the adapter exposes no reading"}
            drifts.append({"type": "Not verifiable", "control": control, "key": f"nv:{control}", "object": "-",
                           "live": "unreadable", "approved": CONTROLS[control], "risk": "Control cannot be evidenced"})

        model = set(self.pack.get("access_model", []))
        privileged = set(self.pack.get("privileged_roles", []))
        restricted = self._restricted_objects()
        if "grants" not in live:
            nv("grants")
        else:
            outside = [parse_grant(g) for g in live["grants"] if g not in model]
            for g in outside:
                touches_restricted = any(fnmatch.fnmatch(o, g["object"]) for o in restricted) or \
                    any(fnmatch.fnmatch(o, g["object"]) for o in live.get("objects", [])
                        if o not in self.pack.get("object_tags", {}) and o not in
                        {x for p in self.pack.get("pipelines", []) for x in p["objects"]})
                exposure = touches_restricted or g["role"] in privileged or g["privilege"] in ("ALL", "OWNER", "ADMIN")
                drifts.append({"type": "Grant outside the model", "control": "grants", "key": f"grant:{g['raw']}",
                               "object": g["object"], "live": g["raw"], "approved": "no matching rule in the access model",
                               "risk": ("Restricted data reachable by an unapproved role" if touches_restricted
                                        else "Access beyond the approved model"),
                               "exposure": exposure, "correction": f"revoke {g['privilege']} on {g['object']} from {g['role']}",
                               "role": g["role"]})
            results["grants"] = {"status": "drifted" if outside else "matching",
                                 "detail": f"{len(outside)} grant(s) outside the model"}
        if "role_last_used_days" not in live:
            nv("dormant")
        else:
            dormant = [r for r in privileged if live["role_last_used_days"].get(r, 10**6) > DORMANT_DAYS]
            for r in dormant:
                drifts.append({"type": "Dormant privileged access", "control": "dormant", "key": f"dormant:{r}",
                               "object": "-", "live": f"last used {live['role_last_used_days'].get(r, 'never')} days ago",
                               "approved": f"used within {DORMANT_DAYS} days", "risk": "Unused privilege",
                               "correction": f"review membership of {r}"})
            results["dormant"] = {"status": "drifted" if dormant else "matching", "detail": f"{len(dormant)} dormant"}
        if "masking" not in live:
            nv("masking")
        else:
            bad = 0
            for col, approved in self.pack.get("masking_policies", {}).items():
                got = live["masking"].get(col)
                if got != approved:
                    bad += 1
                    drifts.append({"type": "Unmasked Restricted column" if got is None else "Policy changed or detached",
                                   "control": "masking", "key": f"mask:{col}", "object": col, "live": got or "none",
                                   "approved": approved, "risk": "Restricted values readable unmasked",
                                   "exposure": got is None, "correction": f"re-attach policy {approved} to {col}"})
            results["masking"] = {"status": "drifted" if bad else "matching", "detail": f"{bad} column(s) off policy"}
        if "objects" not in live:
            nv("classification")
        else:
            known = {o for p in self.pack.get("pipelines", []) for o in p["objects"]} | set(self.pack.get("object_tags", {}))
            new = sorted(set(live["objects"]) - known)
            for o in new:
                drifts.append({"type": "Unclassified object", "control": "classification", "key": f"class:{o}",
                               "object": o, "live": "no tag", "approved": "classified in the design",
                               "risk": "Treated as Restricted until a person classifies it",
                               "correction": f"classify {o}"})
            results["classification"] = {"status": "drifted" if new else "matching", "detail": f"{len(new)} unclassified"}
        if "retention" not in live:
            nv("retention")
        else:
            off = {o: (live["retention"].get(o), d) for o, d in self.pack.get("retention_days", {}).items()
                   if live["retention"].get(o) != d}
            for o, (got, want) in off.items():
                drifts.append({"type": "Retention drift", "control": "retention", "key": f"ret:{o}", "object": o,
                               "live": f"{got} days", "approved": f"{want} days", "risk": "Kept too long or too short",
                               "correction": f"restore retention of {o} to {want} days"})
            results["retention"] = {"status": "drifted" if off else "matching", "detail": f"{len(off)} off requirement"}
        if "audit_logging" not in live:
            nv("logging")
        else:
            al = live["audit_logging"]
            gap = (not al.get("enabled")) or bool(al.get("gaps"))
            if gap:
                drifts.append({"type": "Logging gap", "control": "logging", "key": "logging", "object": "-",
                               "live": str(al), "approved": "enabled, no gaps", "risk": "Activity not evidenced",
                               "correction": "restore audit logging"})
            results["logging"] = {"status": "drifted" if gap else "matching", "detail": str(al)}

        for d in drifts:
            w = self._waived(d["key"], at)
            if w and not w.startswith("EXPIRED"):
                d["waived"] = w
                results[d["control"]] = {"status": "waived", "detail": w}
            elif w:
                d["type"] += " (waiver expired)"
        return {"controls": results, "drifts": [d for d in drifts if not d.get("waived")]}

    # -- review -----------------------------------------------------------------------
    def review(self, day: Optional[str] = None, *, at: Optional[datetime] = None) -> dict:
        at = at or datetime.now(timezone.utc)
        day = day or at.date().isoformat()
        cmp = self.compare(at)
        index = self.ws.read_yaml(INDEX) or {}
        raised = []
        for d in cmp["drifts"]:
            if d["key"] in index and self.cases.get(index[d["key"]]).state != "closed":
                continue  # already an open finding
            raised.append(self._raise(d, day))
            index[d["key"]] = raised[-1]["id"]
        self.ws.write_yaml(self.identity, INDEX, index)
        lines = [f"# Governance review - {self.ws.dp_id} - {day}", "",
                 "Reported against the listed controls only; legal compliance is not judged here.", "",
                 "| Control | Status | Detail |", "|---|---|---|",
                 *[f"| {CONTROLS[c]} | {r['status']} | {r['detail']} |" for c, r in cmp["controls"].items()], "",
                 "## Open findings", *([f"- {i}: {k}" for k, i in index.items()] or ["- none"]), ""]
        self.ws.write(self.identity, f"governance/review-{day}.md", "\n".join(lines))
        return {"controls": cmp["controls"], "raised": raised}

    def _consumer_impact(self, d: dict) -> str:
        if d["control"] == "grants":
            return (f"Revoking affects only {d['role']}, which is not in the access model; "
                    f"registered consumers ({', '.join(self.sd.get('consumers', [])) or 'none'}) keep their access")
        return "No registered consumer depends on the drifted setting"

    def _raise(self, d: dict, day: str) -> dict:
        case = self.cases.open("GOV", by=self.identity, note=d["type"])
        exposure = bool(d.get("exposure"))
        if exposure:
            already = any(case.id in (e.get("reason") or "") for e in escalations(self.ws))
            if not already:
                escalate(self.ws, by=self.identity, to="security_compliance", case_id=case.id,
                         reason=f"{case.id}: suspected exposure - {d['type']}: {d['live']}; no agent action, "
                                "no further collection unless asked")
        body = [f"# {case.id} - {d['type']}", "",
                f"- Product: {self.ws.dp_id}", f"- Object: {d['object']}", f"- Live: {d['live']}",
                f"- Approved: {d['approved']}", f"- First observed: {day}",
                f"- Evidence: access_audit / catalog reading of {day}; baseline control/handover-pack.yaml",
                f"- Risk: {d['risk']}",
                f"- Recommended correction: {d.get('correction', '-')} (executed by people or the platform team)",
                f"- Consumer impact of the correction: {self._consumer_impact(d)}",
                "- Decision owner: Security and Compliance (OG5)",
                f"- Status: {'escalated as suspected exposure; collection stopped' if exposure else 'open'}", ""]
        path = f"governance/drift/{case.id}.md"
        self.ws.write(self.identity, path, "\n".join(body))
        self.cases.transition(case.id, "awaiting-decision", by=self.identity)
        req = self.commander.request_approval(
            gate="OG5", decision_asked=f"{case.id}: {d.get('correction', d['type'])}",
            artifact_hash=file_hash(self.ws.path(path)), owner_agent="adef-ops-governance-compliance",
            case_id=case.id, evidence=[path], risk=d["risk"])
        return {"id": case.id, "type": d["type"], "exposure": exposure, "approval": req.id, "key": d["key"]}

    def verify_correction(self, gov_id: str) -> bool:
        """After people correct the drift: re-check and record. Resolved only if it is gone."""
        index = self.ws.read_yaml(INDEX) or {}
        key = next(k for k, v in index.items() if v == gov_id)
        still = any(d["key"] == key for d in self.compare()["drifts"])
        path = f"governance/drift/{gov_id}.md"
        self.ws.write(self.identity, path, self.ws.read(path) +
                      f"\n## Verification\n- {datetime.now(timezone.utc).isoformat()}: "
                      f"{'still drifted' if still else 'correction verified; drift gone'}\n")
        if not still:
            self.cases.transition(gov_id, "resolved", by=self.identity, note="correction verified")
        return not still

    # -- T4 / T5 -----------------------------------------------------------------------
    def evidence_pack(self, period: str) -> str:
        appr = Approvals(self.ws)
        reqs = list(appr._records("request"))
        chain = {}
        for path in LOGS:
            chain[path] = ChainLog(self.ws.path(path)).verify() if self.ws.has(path) else None
        gov = [c for c in self.cases.all() if c.type == "GOV"]
        lines = [f"# Control evidence pack - {self.ws.dp_id} - {period}", "",
                 "## Controls and enforcement", "| Control | Enforced by | Evidence |", "|---|---|---|",
                 *[f"| {c} | {e} | `{p}` |" for c, e, p in ENFORCEMENT], "",
                 "## Log integrity (hash chain)",
                 *[f"- `{p}`: {'verified' if ok else ('NOT VERIFIED' if ok is False else 'absent')}" for p, ok in chain.items()],
                 "", "## Domain Pack controls in scope", *self._pack_controls(),
                 "", "## Approvals in the record", f"- {len(reqs)} request(s); see `control/approvals.jsonl`", "",
                 "## Governance findings", *([f"- {c.id} [{c.state}] {c.timeline[0].event}" for c in gov] or ["- none"]),
                 "", "## Not covered",
                 "- Whether any law or regulation is satisfied (not judged by this agent)",
                 "- Controls outside the listed set and systems without an access_audit adapter", ""]
        text = "\n".join(lines)
        self.ws.write(self.identity, f"governance/evidence-pack-{period}.md", text)
        return text

    def _pack_controls(self) -> list[str]:
        from ..packs import domain_pack
        dp = domain_pack(self.pack.get("domain_pack", ""))
        src = (dp.get("build_pack") or {})
        rows = [f"Source: Build-With-AI domain pack `{src.get('pack_id', dp.get('id'))}` "
                f"v{src.get('pack_version', '?')}", "", "| Control | Checked by | Coverage |", "|---|---|---|"]
        for c in dp.get("controls", []):
            by = c.get("checked_by") or []
            rows.append(f"| {c['id']}: {c['text']} | {', '.join(CONTROLS[b] for b in by) or '-'} | "
                        f"{'partial: automated checks above' if by else 'not covered by an automated check'} |")
        return rows if dp.get("controls") else ["- none listed"]

    def entitlements(self, period: str) -> str:
        live = self.adapter.governance_state(self.ws.dp_id)
        model = set(self.pack.get("access_model", []))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["role", "object", "privilege", "last_used_days", "purpose", "recommendation"])
        for g in map(parse_grant, live.get("grants", [])):
            in_model = g["raw"] in model
            w.writerow([g["role"], g["object"], g["privilege"], live.get("role_last_used_days", {}).get(g["role"], ""),
                        "approved access model" if in_model else "none recorded",
                        "keep" if in_model else "candidate for removal (Data Owner decides)"])
        self.ws.write(self.identity, f"governance/entitlements-{period}.csv", buf.getvalue())
        return buf.getvalue()
