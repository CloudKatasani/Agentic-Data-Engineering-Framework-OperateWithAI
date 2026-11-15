"""Fleet service layer behind the section 11 API and console.

Everything the HTTP layer does goes through here, so the same enforcement applies whether
a request comes from the console, a script, or a test: the engine's guards, allowlists,
approvals and executor are used unchanged. The HTTP layer adds transport, authentication
and identity binding only.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..agents import (CommanderAgent, DiagnosisAgent, FinOpsAgent, GovernanceAgent, KnowledgeAgent,
                      RemediationAgent, SentinelAgent)
from ..agents.requests import RequestDesk
from ..connectors.paging import OutboxPager
from ..connectors.simulator import FAULTS, SIM_NOW, SimulatorAdapter
from ..core.errors import AdefError, GuardViolation
from ..engine import shadow as shadow_engine
from ..engine.approvals import GATE_ROLES, Approvals
from ..engine.case_store import CaseStore
from ..engine.catalogue import DEFAULT_CATALOGUE, lookup
from ..engine.council import Council
from ..engine.escalation import escalations
from ..engine.monitor_engine import ingest_signal
from ..engine.registry import Registry, global_stop_path
from ..engine.workspace import Workspace
from ..packs import PACKS, handover_pack
from ..runtime import OpsRuntime
from ..shadow_mode import ShadowRuntime
from ..stack import StackAdapter, list_profiles, load_profile, operate_binding
from ..stack.export import ExportAdapter

CASE_STEPS = ("diagnose", "options", "act", "verify", "council", "confirm-cause", "post-incident", "close")


class NotDemo(GuardViolation):
    pass


class Fleet:
    def __init__(self, home: str | Path):
        self.home = Path(home)
        (self.home / "ops").mkdir(parents=True, exist_ok=True)
        self.sims: dict[str, SimulatorAdapter] = {}
        self.pager = OutboxPager()
        self.lock = threading.RLock()

    # -- products --------------------------------------------------------------------------
    def products(self) -> list[str]:
        root = self.home / "ops"
        return sorted(p.name for p in root.iterdir() if (p / "control" / "service.yaml").exists())

    def ws(self, dp: str) -> Workspace:
        ws = Workspace(dp, self.home)
        if not ws.has("control/service.yaml"):
            raise KeyError(f"product {dp}")
        return ws

    def svc(self, dp: str) -> dict:
        return self.ws(dp).read_yaml("control/service.yaml")

    # -- Demo Simulator state (outside the ops workspace; restored on restart) ----------------
    def _sim_path(self, dp: str) -> Path:
        return self.home / "demo-sim" / f"{dp}.json"

    def sim(self, dp: str) -> SimulatorAdapter:
        if dp not in self.sims:
            s = SimulatorAdapter()
            p = self._sim_path(dp)
            if p.exists():
                s.state = json.loads(p.read_text())["state"]
            self.sims[dp] = s
        return self.sims[dp]

    def _save_sim(self, dp: str) -> None:
        if dp in self.sims:
            p = self._sim_path(dp)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"state": self.sims[dp].state}, default=str))

    def _ticks(self, dp: str) -> int:
        p = self.home / "demo-sim" / f"{dp}.ticks"
        return int(p.read_text()) if p.exists() else 0

    def _bump_ticks(self, dp: str) -> int:
        n = self._ticks(dp) + 1
        p = self.home / "demo-sim" / f"{dp}.ticks"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(n))
        return n

    def adapter(self, dp: str):
        svc = self.svc(dp)
        prof_id = (svc.get("stack_profile") or {}).get("profile_id")
        if svc.get("mode", "demo") == "demo":
            sim = self.sim(dp)
            return StackAdapter.over(load_profile(prof_id), sim) if prof_id else sim
        start = self.ws(dp).read_yaml("shadow/start.yaml") or {}
        return ExportAdapter(load_profile(start.get("profile_id") or prof_id), Path(start["exports"]) / dp)

    def _demo_only(self, dp: str) -> None:
        if self.svc(dp).get("mode", "demo") != "demo":
            raise NotDemo(f"{dp} is a {self.svc(dp).get('mode')} product; simulation is for Demo Simulator products only")

    # -- onboarding --------------------------------------------------------------------------
    def create_service(self, body: dict) -> dict:
        with self.lock:
            mode = body.get("mode", "demo")
            profile_id = body.get("profile")
            if mode == "demo":
                pack = handover_pack(body["pack"]) if isinstance(body.get("pack"), str) else body["pack"]
                dp = pack["dp_id"]
                if dp in self.products():
                    raise GuardViolation(f"{dp} is already in operations")
                self.sims.pop(dp, None)
                for f in (self._sim_path(dp), self.home / "demo-sim" / f"{dp}.ticks"):
                    f.unlink(missing_ok=True)
                sim = SimulatorAdapter()
                self.sims[dp] = sim
                prof = load_profile(profile_id) if profile_id else None
                adapter = StackAdapter.over(prof, sim) if prof else sim
                rt = OpsRuntime(self.home, adapter, stack_profile=prof, pager=self.pager)
                res = rt.release({"type": "product.released", "handover_pack": pack})
                self._save_sim(dp)
                return {"accepted": res.accepted, "gaps": res.gaps, "dp_id": dp, "og0_request": res.og0_request}
            if mode == "shadow":
                rt = ShadowRuntime(self.home, profile_id, body["exports"])
                res = rt.release(body["pack"], body["answers"])
                if res.get("accepted"):
                    ws = Workspace(body["pack"]["dp_id"], self.home)
                    start = ws.read_yaml("shadow/start.yaml") or {}
                    ws.write_yaml("shadow", "shadow/start.yaml", {**start, "exports": str(Path(body["exports"]))})
                return {**res, "dp_id": body["pack"]["dp_id"]}
            raise ValueError("mode must be demo or shadow")

    def onboard(self, dp: str) -> dict:
        with self.lock:
            svc = self.svc(dp)
            prof_id = (svc.get("stack_profile") or {}).get("profile_id")
            if svc.get("mode", "demo") == "demo":
                prof = load_profile(prof_id) if prof_id else None
                factory = (lambda: StackAdapter.over(prof, SimulatorAdapter())) if prof else None
                ob = OpsRuntime(self.home, self.adapter(dp), stack_profile=prof, sandbox_factory=factory).onboard(dp)
            else:
                start = self.ws(dp).read_yaml("shadow/start.yaml")
                ob = ShadowRuntime(self.home, start["profile_id"], start["exports"]).onboard(dp)
            return {"ready": ob.ready, "og1_request": ob.og1_request, "problems": ob.problems, "report": ob.report}

    # -- signals and ticks ---------------------------------------------------------------------
    def ingest(self, dp: str, body: dict, *, process: bool = False) -> dict:
        with self.lock:
            ws = self.ws(dp)
            rec = dict(body)
            rec.setdefault("product", dp)
            if rec["product"] != dp:
                raise GuardViolation(f"signal is for {rec['product']}, not {dp}")
            rec.setdefault("time", datetime.now(timezone.utc).isoformat())
            if not rec.get("id"):
                n = sum(len(f.read_text().splitlines()) for f in ws.path("signals").glob("*.jsonl"))
                rec["id"] = f"SIG-{n + 1:06d}"
            sig = ingest_signal(ws, rec)
            out: dict[str, Any] = {"signal": sig}
            if process:
                day = sig["time"][:10]
                adapter = self.adapter(dp)
                groups = SentinelAgent(ws, adapter).run(day)
                out["groups"] = groups
                out["outcomes"] = CommanderAgent(ws, adapter, self.pager).process_groups(groups, day=day)
            return out

    def tick(self, dp: str) -> dict:
        with self.lock:
            svc = self.svc(dp)
            if svc.get("mode", "demo") == "demo":
                n = self._bump_ticks(dp)
                at = SIM_NOW + timedelta(minutes=5 * (n - 1))
                out = OpsRuntime(self.home, self.adapter(dp), pager=self.pager).tick(dp, at)
                self._save_sim(dp)
                return {"at": at.isoformat(), "signals": len(out["signals"]), "groups": out["groups"],
                        "outcomes": out["outcomes"]}
            start = self.ws(dp).read_yaml("shadow/start.yaml")
            runs = ShadowRuntime(self.home, start["profile_id"], start["exports"]).run_new_snapshots(dp)
            return {"snapshots": len(runs), "opened": [c for r in runs for c in r["cases_opened"]],
                    "recovered": [c for r in runs for c in r["recovered"]]}

    def simulate_fault(self, dp: str, fault_id: str, *, tick: bool = True) -> dict:
        with self.lock:
            self._demo_only(dp)
            if fault_id not in FAULTS:
                raise KeyError(f"fault {fault_id}")
            pack = self.svc(dp)["domain_pack"]
            injected = self.sim(dp).inject(fault_id, pack)
            self._save_sim(dp)
            return {"injected": injected, "tick": self.tick(dp) if tick else None}

    def simulate_reset(self, dp: str) -> dict:
        with self.lock:
            self._demo_only(dp)
            self.sim(dp).reset()
            self._save_sim(dp)
            return {"reset": dp}

    def resolve_source(self, dp: str, fault_id: str) -> dict:
        with self.lock:
            self._demo_only(dp)
            self.sim(dp).resolve_source(fault_id, self.svc(dp)["domain_pack"])
            self._save_sim(dp)
            return {"resolved": fault_id}

    # -- read models ----------------------------------------------------------------------------
    def _pending(self, ws: Workspace) -> list[dict]:
        a = Approvals(ws)
        out = []
        for r in a._records("request"):
            st = a.status(r["id"])
            if st == "pending":
                decided = {d.role for d in a.decisions(r["id"])}
                approvers = (ws.read_yaml("control/service.yaml") or {}).get("approvers", {})
                out.append({"dp_id": ws.dp_id, "id": r["id"], "gate": r["gate"], "case_id": r.get("case_id"),
                            "decision_asked": r["decision_asked"], "expires": r["expires"],
                            "roles": [{"role": x, "named": approvers.get(x), "decided": x in decided}
                                      for x in r["approver_roles"]],
                            "any_of": r["gate"] == "OG2" and "release_manager" not in r["approver_roles"],
                            "evidence": r.get("evidence", []), "risk": r.get("risk"),
                            "recommendation": r.get("recommendation")})
        return out

    def approvals(self, dp: Optional[str] = None) -> list[dict]:
        return [p for d in ([dp] if dp else self.products()) for p in self._pending(self.ws(d))]

    def _budget_today(self, ws: Workspace) -> dict:
        reg = Registry(ws)
        caps = (ws.read_yaml("control/service.yaml") or {}).get("budgets", {})
        today = datetime.now(timezone.utc).date()
        return {k: {"spent": round(reg._spent(k, today), 2), "cap": caps.get(k)} for k in caps}

    def product_summary(self, dp: str) -> dict:
        ws = self.ws(dp)
        svc = ws.read_yaml("control/service.yaml")
        cases = CaseStore(ws).all()
        open_inc = [c for c in cases if c.type == "INC" and c.state != "closed"]
        trust = next((c.trust_status for c in sorted(open_inc, key=lambda c: c.severity or "S9") if c.trust_status),
                     "trusted")
        valid, why = Approvals(ws).og1_status()
        return {"dp_id": dp, "name": svc["name"], "client_id": svc.get("client_id"), "mode": svc.get("mode", "demo"),
                "data_origin": svc.get("data_origin"), "domain_pack": svc["domain_pack"],
                "stack_profile": (svc.get("stack_profile") or {}).get("profile_id"), "trust_status": trust,
                "og1": {"valid": valid, "detail": why}, "stop": Registry(ws).stop_state(),
                "open_cases": {s: sum(1 for c in open_inc if c.severity == s) for s in ("S1", "S2", "S3", "S4")},
                "open_findings": {t: sum(1 for c in cases if c.type == t and c.state != "closed")
                                  for t in ("FIN", "GOV", "PRB", "CHG")},
                "pending_approvals": len(self._pending(ws)), "escalations": len(escalations(ws)),
                "budgets": self._budget_today(ws), "onboarded": ws.has("service/monitoring-spec.yaml")}

    def fleet(self) -> dict:
        prods = [self.product_summary(d) for d in self.products()]
        g = global_stop_path(self.home)
        return {"products": prods, "global_stop": g.read_text().strip() if g.exists() else None,
                "totals": {"products": len(prods),
                           "open_s1_s2": sum(p["open_cases"]["S1"] + p["open_cases"]["S2"] for p in prods),
                           "open_cases": sum(sum(p["open_cases"].values()) for p in prods),
                           "pending_approvals": sum(p["pending_approvals"] for p in prods),
                           "escalations": sum(p["escalations"] for p in prods)}}

    def service(self, dp: str) -> dict:
        ws = self.ws(dp)
        reg = Registry(ws)
        tr = reg.load().get("track_record", {})
        ladder = []
        for t, e in DEFAULT_CATALOGUE.items():
            lvl, why = reg.effective_level(t)
            ladder.append({"action_type": t, "class": e.action_class.value, "gate": e.gate,
                           "start": e.start.name, "highest": e.highest.name, "stored": reg.stored_level(t).name,
                           "effective": lvl.name, "reason": why, "executed_by_people": e.executed_by_people,
                           "track_record": tr.get(t, {"verified": 0, "failed": 0})})
        spec = ws.read_yaml("service/monitoring-spec.yaml") or {}
        svc = ws.read_yaml("control/service.yaml")
        prof_id = (svc.get("stack_profile") or {}).get("profile_id")
        latest = sorted(ws.path("health").glob("20*.md"))
        return {"summary": self.product_summary(dp), "service": svc,
                "definition": ws.read_yaml("service/service-definition.yaml"),
                "monitors": spec.get("monitors", []), "runbook": ws.read("service/runbook.md") if ws.has("service/runbook.md") else "",
                "readiness": ws.read("service/readiness-report.md") if ws.has("service/readiness-report.md") else "",
                "autonomy": ladder, "binding": operate_binding(load_profile(prof_id)) if prof_id else None,
                "health": latest[-1].read_text() if latest else "", "escalations": escalations(ws)[-20:],
                "faults": [{"id": f.id, "description": f.description} for f in FAULTS.values()
                           if svc["domain_pack"] in f.packs] if svc.get("mode", "demo") == "demo" else []}

    # -- cases -----------------------------------------------------------------------------------
    def cases(self, dp: Optional[str] = None, state: Optional[str] = None, severity: Optional[str] = None,
              case_type: Optional[str] = None) -> list[dict]:
        out = []
        for d in ([dp] if dp else self.products()):
            for c in CaseStore(self.ws(d)).all():
                if (state == "open" and c.state == "closed") or (state and state != "open" and c.state != state):
                    continue
                if severity and c.severity != severity:
                    continue
                if case_type and c.type != case_type:
                    continue
                out.append({"dp_id": d, "id": c.id, "type": c.type, "state": c.state, "severity": c.severity,
                            "severity_rule": c.severity_rule, "owner": c.owner, "trust_status": c.trust_status,
                            "first_failing_object": c.first_failing_object, "next_action": c.next_action,
                            "opened": c.timeline[0].at.isoformat() if c.timeline else None,
                            "updated": c.timeline[-1].at.isoformat() if c.timeline else None,
                            "escalated": c.escalated, "title": c.timeline[0].event if c.timeline else ""})
        return sorted(out, key=lambda c: (c["state"] == "closed", c["severity"] or "S9", c["updated"] or ""), reverse=False)

    def resolve_case(self, case_id: str, dp: Optional[str]) -> str:
        if dp:
            CaseStore(self.ws(dp)).get(case_id)
            return dp
        hits = [d for d in self.products() if self.ws(d).has(f"cases/{case_id}/case.yaml")]
        if not hits:
            raise KeyError(f"case {case_id}")
        if len(hits) > 1:
            raise LookupError(f"case id {case_id} exists in {hits}; pass dp")
        return hits[0]

    def case(self, case_id: str, dp: Optional[str] = None) -> dict:
        dp = self.resolve_case(case_id, dp)
        ws = self.ws(dp)
        c = CaseStore(ws).get(case_id)
        rd = lambda rel: ws.read(f"cases/{case_id}/{rel}") if ws.has(f"cases/{case_id}/{rel}") else None
        sigs, group = [], None
        if c.signal_group:
            day = c.signal_group.split("-")[1]
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            if ws.has(f"signals/groups/{day}.jsonl"):
                group = next((g for g in (json.loads(l) for l in ws.read(f"signals/groups/{day}.jsonl").splitlines())
                              if g["id"] == c.signal_group), None)
            for f in sorted(ws.path("signals").glob("*.jsonl")):
                for l in f.read_text().splitlines():
                    if l.strip():
                        s = json.loads(l)
                        if s["id"] in c.signals:
                            s["label"] = ((group or {}).get("labels", {}).get(s["id"]) or {}).get("label")
                            sigs.append(s)
        ev_dir = ws.path(f"cases/{case_id}/evidence")
        evidence = [json.loads(p.read_text()) for p in sorted(ev_dir.glob("E-*.json"))] if ev_dir.exists() else []
        for e in evidence:
            e.pop("data", None)  # summaries only in the case room; raw metadata stays in the files
        acts = []
        act_dir = ws.path(f"cases/{case_id}/actions")
        if act_dir.exists():
            for p in sorted(act_dir.glob("ACT-*.yaml")):
                d = ws.read_yaml(f"cases/{case_id}/actions/{p.name}")
                acts.append({"id": p.stem, "status": d.get("status"), "approval_id": d.get("approval_id"),
                             "action_type": d["record"]["action_type"], "reason": d.get("reason")})
        appr = Approvals(ws)
        reqs = [{"id": r["id"], "gate": r["gate"], "decision_asked": r["decision_asked"], "status": appr.status(r["id"]),
                 "roles": r["approver_roles"], "decisions": [d.model_dump(mode="json") for d in appr.decisions(r["id"])]}
                for r in appr._records("request") if r.get("case_id") == case_id]
        return {"dp_id": dp, "case": c.model_dump(mode="json"), "signals": sigs, "group": group,
                "impact": rd("impact.md"), "diagnosis": ws.read_yaml(f"cases/{case_id}/diagnosis.yaml"),
                "diagnosis_md": rd("diagnosis.md"), "evidence": evidence,
                "options": ws.read_yaml(f"cases/{case_id}/actions/options.yaml") or [], "options_md": rd("options.md"),
                "actions": acts, "approvals": reqs, "council": Council(ws).verdicts(case_id),
                "council_status": Council(ws).status(case_id), "verification": rd("verification.md"),
                "post_incident": rd("post-incident.md"),
                "approvers": (ws.read_yaml("control/service.yaml") or {}).get("approvers", {}),
                "mode": (ws.read_yaml("control/service.yaml") or {}).get("mode", "demo")}

    def case_step(self, case_id: str, step: str, body: dict, dp: Optional[str] = None) -> dict:
        if step not in CASE_STEPS:
            raise KeyError(f"step {step}")
        with self.lock:
            dp = self.resolve_case(case_id, dp)
            ws, adapter = self.ws(dp), self.adapter(dp)
            try:
                if step == "diagnose":
                    r = DiagnosisAgent(ws, adapter).diagnose(case_id)
                    return {"cause_class": r.cause_class, "confidence": r.confidence}
                if step == "options":
                    return {"options": [o.__dict__ for o in RemediationAgent(ws, adapter).build_options(case_id)]}
                if step == "act":
                    return RemediationAgent(ws, adapter).act(case_id, int(body["option"]))
                if step == "verify":
                    return RemediationAgent(ws, adapter).verify(case_id)
                if step == "council":
                    return {"verdicts": Council(ws).record(case_id, reviewer=body["reviewer"],
                                                           agrees=bool(body["agrees"]), note=body.get("note", ""))}
                if step == "confirm-cause":
                    KnowledgeAgent(ws, adapter).confirm_cause(case_id, cause_class=body["cause_class"], by=body["by"],
                                                              note=body.get("note", ""))
                    return {"confirmed": body["cause_class"]}
                if step == "post-incident":
                    return {"note": KnowledgeAgent(ws, adapter).post_incident(case_id)}
                CommanderAgent(ws, adapter).close(case_id)
                return {"closed": case_id}
            finally:
                self._save_sim(dp)

    # -- approvals ------------------------------------------------------------------------------
    def decide(self, dp: str, request_id: str, *, approver: str, role: str, decision: str,
               reason: str = "") -> dict:
        with self.lock:
            ws = self.ws(dp)
            a = Approvals(ws)
            req = a.get_request(request_id)
            a.decide(request_id, approver=approver, role=role, decision=decision, artifact_hash=req.artifact_hash,
                     reason=reason)
            status = a.status(request_id)
            follow: dict[str, Any] = {}
            if status in ("approved", "rejected") and req.case_id:
                adapter = self.adapter(dp)
                act_dir = ws.path(f"cases/{req.case_id}/actions")
                for p in sorted(act_dir.glob("ACT-*.yaml")) if act_dir.exists() else []:
                    d = ws.read_yaml(f"cases/{req.case_id}/actions/{p.name}")
                    if d.get("approval_id") == request_id and d.get("status") == "awaiting-approval":
                        follow = RemediationAgent(ws, adapter).proceed(req.case_id, p.stem)
                if req.case_id.startswith("FIN-"):
                    follow = {"decision": FinOpsAgent(ws, adapter).record_decision(req.case_id, request_id)}
            self._save_sim(dp)
            return {"request": request_id, "gate": req.gate, "status": status, "follow_up": follow}

    def confirm_people_executed(self, dp: str, case_id: str, act_id: str, by: str) -> dict:
        with self.lock:
            RemediationAgent(self.ws(dp), self.adapter(dp)).confirm_executed_by_people(case_id, act_id, by=by)
            return {"act": act_id, "status": "executed-by-people"}

    # -- reviews --------------------------------------------------------------------------------
    def finops(self, dp: str) -> dict:
        ws = self.ws(dp)
        f = FinOpsAgent(ws, self.adapter(dp))
        reviews = sorted(ws.path("finops").glob("20*.md"))
        return {"latest_review": reviews[-1].read_text() if reviews else None,
                "findings": [{**c, "body": ws.read(f"finops/findings/{c['id']}.md")
                              if ws.has(f"finops/findings/{c['id']}.md") else ""}
                             for c in self.cases(dp, case_type="FIN")],
                "ledger": f.ledger()}

    def finops_review(self, dp: str) -> dict:
        with self.lock:
            ws = self.ws(dp)
            return FinOpsAgent(ws, self.adapter(dp), CommanderAgent(ws, self.adapter(dp), self.pager)).review()

    def governance(self, dp: str) -> dict:
        ws = self.ws(dp)
        reviews = sorted(ws.path("governance").glob("review-*.md"))
        return {"controls": GovernanceAgent(ws, self.adapter(dp)).compare()["controls"],
                "latest_review": reviews[-1].read_text() if reviews else None,
                "findings": [{**c, "body": ws.read(f"governance/drift/{c['id']}.md")
                              if ws.has(f"governance/drift/{c['id']}.md") else ""}
                             for c in self.cases(dp, case_type="GOV")]}

    def governance_review(self, dp: str) -> dict:
        with self.lock:
            ws = self.ws(dp)
            return GovernanceAgent(ws, self.adapter(dp), CommanderAgent(ws, self.adapter(dp), self.pager)).review()

    def evidence_pack(self, dp: str, period: str) -> str:
        with self.lock:
            return GovernanceAgent(self.ws(dp), self.adapter(dp)).evidence_pack(period)

    def learning(self, dp: str) -> dict:
        ws = self.ws(dp)
        k = KnowledgeAgent(ws, self.adapter(dp))
        noise = sorted(ws.path("signals").glob("noise-report-*.md"))
        props = sorted(ws.path("control/autonomy-proposals").glob("AUT-*.md"))
        tun = sorted(ws.path("service/tuning-proposals").glob("TUN-*.md"))
        return {"problems": [{**c, "body": ws.read(f"problems/{c['id']}.md") if ws.has(f"problems/{c['id']}.md") else ""}
                             for c in self.cases(dp, case_type="PRB")],
                "replays": [{k2: r[k2] for k2 in ("id", "case", "label", "agent_cause", "confirmed", "severity")}
                            for r in k.replays()],
                "accuracy": k.accuracy(), "track_record": k.track_record(),
                "noise_report": noise[-1].read_text() if noise else None,
                "autonomy_proposals": [p.read_text() for p in props],
                "tuning_proposals": [p.read_text() for p in tun]}

    def learning_action(self, dp: str, action: str, body: dict) -> dict:
        with self.lock:
            ws, adapter = self.ws(dp), self.adapter(dp)
            cmd = CommanderAgent(ws, adapter, self.pager)
            if action == "noise-report":
                start = date.fromisoformat(body.get("week_start") or SIM_NOW.date().isoformat())
                return SentinelAgent(ws, adapter).noise_report(start)
            if action == "propose-autonomy":
                return {"proposals": KnowledgeAgent(ws, adapter, cmd).propose_autonomy()}
            if action == "propose-tuning":
                return SentinelAgent(ws, adapter).propose_tuning(body["monitor_id"], body["changes"],
                                                                 reason=body.get("reason", ""), commander=cmd)
            raise KeyError(f"learning action {action}")

    def shadow_report(self, dp: str) -> dict:
        ws = self.ws(dp)
        rep = shadow_engine.shadow_report(ws)
        return {"report": rep, "markdown": ws.read(shadow_engine.write_report(ws, rep))}

    # -- control ---------------------------------------------------------------------------------
    def stop(self, *, by: str, reason: str, dp: Optional[str] = None) -> dict:
        with self.lock:
            if dp:
                Registry(self.ws(dp)).stop(by=by, reason=reason)
                return {"stopped": dp}
            Registry.emergency_stop(by=by, reason=reason, home=self.home)
            for d in self.products():
                from ..engine.escalation import escalate
                escalate(self.ws(d), by="console", reason=f"global emergency stop by {by}: {reason}")
            return {"stopped": "all"}

    def clear_stop(self, *, by: str, dp: Optional[str] = None) -> dict:
        """Lifting a stop is a person's decision, recorded in each product's escalation log."""
        from ..engine.escalation import escalate
        with self.lock:
            targets = [dp] if dp else self.products()
            if dp:
                reg = Registry(self.ws(dp))
                data = reg.load()
                data["stopped"] = None
                reg.save(data)
            else:
                global_stop_path(self.home).unlink(missing_ok=True)
            for d in targets:
                escalate(self.ws(d), by="console", reason=f"stop lifted by {by}" + ("" if dp else " (global)"))
            return {"cleared": dp or "all"}

    def request(self, dp: str, text: str, by: str) -> dict:
        with self.lock:
            return RequestDesk(self.ws(dp)).handle(text, by=by)

    # -- catalogue for the console ---------------------------------------------------------------
    @staticmethod
    def catalog() -> dict:
        return {"profiles": [{"id": p, "label": load_profile(p).get("label", p)} for p in list_profiles()],
                "packs": list(PACKS), "faults": [{"id": f.id, "packs": f.packs, "description": f.description}
                                                 for f in FAULTS.values()],
                "gates": {g: {"mode": m, "roles": r} for g, (m, r) in GATE_ROLES.items()},
                "actions": [{"action_type": t, "class": e.action_class.value, "gate": e.gate, "start": e.start.name,
                             "highest": e.highest.name} for t, e in DEFAULT_CATALOGUE.items()],
                "case_steps": list(CASE_STEPS)}
