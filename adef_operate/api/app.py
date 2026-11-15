"""Operate-With-AI REST API (OPERATE_SPEC section 11) and the web console.

Section 11 endpoints, exactly as specified:

| Method and path                         | Purpose                                         |
|-----------------------------------------|-------------------------------------------------|
| POST /ops/services                      | Onboard from a handover pack (demo or shadow)   |
| GET  /ops/services/{dp}                 | Service definition, health, autonomy levels     |
| POST /ops/signals                       | Ingest (collector and Consume events)           |
| GET  /ops/cases, GET /ops/cases/{id}    | Queue and case detail with evidence             |
| POST /ops/cases/{id}/approvals          | Decide a gate request (named human only)        |
| POST /ops/simulate/faults               | Inject a fault in Demo Simulator mode           |
| GET  /ops/finops/{dp}, /ops/governance/{dp} | Reviews and findings                        |
| POST /ops/stop                          | Emergency stop                                  |

Supporting endpoints for the console (case-room steps, onboarding, ticks, approvals inbox,
learning, request desk) sit under the same ``/ops`` prefix.

Security, mirroring Build-With-AI's console:
* ``ADEF_API_TOKEN``: when set, every ``/ops`` call except ``/ops/health`` needs it as a
  bearer token.
* ``ADEF_IDENTITY_FILE``: optional YAML mapping ``sha256(token)`` to a person's name. When
  set, each caller uses their own token and may decide or act only as themselves; the
  engine still checks that the person holds the role.
"""

from __future__ import annotations

import hashlib
import os
import secrets as pysecrets
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core.errors import AdefError, ApprovalError, ValidationFailed
from ..engine.workspace import adef_home
from .fleet import Fleet, NotDemo

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _identities() -> dict[str, str]:
    path = os.environ.get("ADEF_IDENTITY_FILE")
    if not path:
        return {}
    return {str(k): str(v) for k, v in (yaml.safe_load(Path(path).read_text()) or {}).items()}


def create_app(home: Optional[str | Path] = None, autotick: Optional[float] = None) -> FastAPI:
    import threading
    from contextlib import asynccontextmanager
    fleet = Fleet(home or adef_home())
    stop_event = threading.Event()

    def scheduler():
        while not stop_event.wait(autotick):
            try:
                fleet.run_all()
            except Exception:  # the scheduler never dies on one bad cycle; the next cycle retries
                pass

    @asynccontextmanager
    async def lifespan(_app):
        t = None
        if autotick:
            t = threading.Thread(target=scheduler, name="operate-scheduler", daemon=True)
            t.start()
        yield
        stop_event.set()

    app = FastAPI(title="DataCrew- OperateWithAI - Operate-With-AI API", version=__version__, lifespan=lifespan)
    app.state.fleet = fleet
    token = os.environ.get("ADEF_API_TOKEN", "")

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.person = None
        path = request.url.path
        if path.startswith("/ops/") and path != "/ops/health":
            tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if token and not pysecrets.compare_digest(tok, token) and not _identities():
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            ids = _identities()
            if ids:
                person = ids.get(hashlib.sha256(tok.encode()).hexdigest())
                if person is None:
                    return JSONResponse({"detail": "unauthorized: token not mapped to a person"}, status_code=401)
                request.state.person = person
        resp = await call_next(request)
        if path == "/" or path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def as_person(request: Request, claimed: Optional[str]) -> str:
        """With an identity file, the caller acts only as themselves."""
        person = request.state.person
        if person is not None:
            if claimed and claimed != person:
                raise HTTPException(403, f"this token belongs to {person!r}; it cannot act as {claimed!r}")
            return person
        if not claimed:
            raise HTTPException(422, "name the person acting (by / approver)")
        return claimed

    # -- error mapping: guards refuse with a reason, never a stack trace -------------------------
    @app.exception_handler(ApprovalError)
    async def _approval(_, e: ApprovalError):
        return JSONResponse({"detail": str(e), "guardrail": True, "type": "approval"}, status_code=403)

    @app.exception_handler(NotDemo)
    async def _notdemo(_, e: NotDemo):
        return JSONResponse({"detail": str(e), "guardrail": True, "type": "not-demo"}, status_code=409)

    @app.exception_handler(ValidationFailed)
    async def _invalid(_, e: ValidationFailed):
        return JSONResponse({"detail": str(e), "type": "validation"}, status_code=422)

    @app.exception_handler(AdefError)
    async def _guard(_, e: AdefError):
        return JSONResponse({"detail": str(e), "guardrail": True, "type": type(e).__name__,
                             "escalated": e.escalate}, status_code=409)

    @app.exception_handler(LookupError)
    async def _lookup(_, e: LookupError):
        if isinstance(e, KeyError):
            return JSONResponse({"detail": f"not found: {e.args[0] if e.args else e}"}, status_code=404)
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(FileNotFoundError)
    async def _nofile(_, e: FileNotFoundError):
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(ValueError)
    async def _value(_, e: ValueError):
        return JSONResponse({"detail": str(e)}, status_code=422)

    # -- health and catalogue ----------------------------------------------------------------------
    @app.get("/ops/health")
    def health():
        return {"ok": True, "version": __version__, "home": str(fleet.home),
                "brand": {"name": os.environ.get("ADEF_BRAND_NAME", "DataCrew- OperateWithAI"),
                          "tagline": os.environ.get("ADEF_BRAND_TAGLINE", "Agentic Data Operations")},
                "identity_binding": bool(_identities())}

    @app.get("/ops/whoami")
    def whoami(request: Request):
        return {"person": request.state.person}

    @app.get("/ops/catalog")
    def catalog():
        return fleet.catalog()

    @app.get("/ops/fleet")
    def fleet_health():
        return fleet.fleet()

    # -- services --------------------------------------------------------------------------------
    @app.post("/ops/services", status_code=201)
    def create_service(body: dict = Body(...)):
        res = fleet.create_service(body)
        if not res.get("accepted"):
            return JSONResponse({"detail": "handover rejected", **res}, status_code=422)
        return res

    @app.get("/ops/services/{dp}")
    def get_service(dp: str):
        return fleet.service(dp)

    @app.post("/ops/services/{dp}/onboard")
    def onboard(dp: str):
        return fleet.onboard(dp)

    @app.post("/ops/services/{dp}/tick")
    def tick(dp: str):
        return fleet.tick(dp)

    @app.get("/ops/services/{dp}/shadow")
    def shadow_report(dp: str):
        return fleet.shadow_report(dp)

    # -- signals ---------------------------------------------------------------------------------
    @app.post("/ops/signals", status_code=201)
    def signals(body: dict = Body(...), process: bool = False):
        dp = body.get("product")
        if not dp:
            raise HTTPException(422, "signal needs a product")
        return fleet.ingest(dp, body, process=process)

    # -- cases -----------------------------------------------------------------------------------
    @app.get("/ops/cases")
    def cases(dp: Optional[str] = None, state: Optional[str] = None, severity: Optional[str] = None,
              type: Optional[str] = None):
        return fleet.cases(dp, state, severity, type)

    @app.get("/ops/cases/{case_id}")
    def case(case_id: str, dp: Optional[str] = None):
        return fleet.case(case_id, dp)

    @app.post("/ops/cases/{case_id}/approvals")
    def case_approval(case_id: str, request: Request, body: dict = Body(...), dp: Optional[str] = None):
        dp = fleet.resolve_case(case_id, dp or body.get("dp"))
        person = as_person(request, body.get("approver"))
        req = body["request_id"]
        from ..engine.approvals import Approvals
        if Approvals(fleet.ws(dp)).get_request(req).case_id not in (case_id, None):
            raise HTTPException(409, f"{req} does not belong to {case_id}")
        return fleet.decide(dp, req, approver=person, role=body["role"], decision=body["decision"],
                            reason=body.get("reason", ""))

    @app.post("/ops/cases/{case_id}/steps/{step}")
    def case_step(case_id: str, step: str, request: Request, body: dict = Body(default={}), dp: Optional[str] = None):
        if step in ("council",):
            body = {**body, "reviewer": as_person(request, body.get("reviewer"))}
        if step in ("confirm-cause",):
            body = {**body, "by": as_person(request, body.get("by"))}
        return fleet.case_step(case_id, step, body, dp)

    @app.post("/ops/cases/{case_id}/actions/{act_id}/executed")
    def people_executed(case_id: str, act_id: str, request: Request, body: dict = Body(default={}),
                        dp: Optional[str] = None):
        dp = fleet.resolve_case(case_id, dp)
        return fleet.confirm_people_executed(dp, case_id, act_id, as_person(request, body.get("by")))

    # -- approvals inbox (gates without a case: OG0, OG1, OG4-7) -----------------------------------
    @app.get("/ops/approvals")
    def approvals(dp: Optional[str] = None):
        return fleet.approvals(dp)

    @app.post("/ops/approvals/{dp}/{request_id}")
    def decide(dp: str, request_id: str, request: Request, body: dict = Body(...)):
        return fleet.decide(dp, request_id, approver=as_person(request, body.get("approver")), role=body["role"],
                            decision=body["decision"], reason=body.get("reason", ""))

    # -- human-in-the-loop: notifications, team, activity -------------------------------------------
    @app.get("/ops/notifications")
    def notifications(person: Optional[str] = None, status: str = "open", dp: Optional[str] = None):
        return fleet.notifications(person=person, status=status, dp=dp)

    def _params(body: dict) -> dict:
        out = {k: body[k] for k in ("option", "cause_class", "act_id", "role", "note", "request_id") if body.get(k) is not None}
        if "option" in out:
            out["option"] = int(out["option"])
        return out

    @app.post("/ops/notifications/{dp}/{nid}/actions")
    def notification_action(dp: str, nid: str, request: Request, body: dict = Body(...)):
        return fleet.notification_action(dp, nid, body["action"], as_person(request, body.get("by")), **_params(body))

    @app.post("/ops/cases/{case_id}/actions")
    def case_action(case_id: str, request: Request, body: dict = Body(...), dp: Optional[str] = None):
        dp = fleet.resolve_case(case_id, dp or body.get("dp"))
        return fleet.case_action(dp, case_id, body["action"], as_person(request, body.get("by")), **_params(body))

    @app.get("/ops/team")
    def team():
        return fleet.team()

    @app.get("/ops/people")
    def people():
        return fleet.people()

    @app.get("/ops/activity")
    def activity(dp: Optional[str] = None, limit: int = 150):
        return fleet.activity(dp, limit)

    @app.post("/ops/run")
    def run_cycle():
        """One scheduled cycle across the fleet (what the background scheduler does)."""
        return fleet.run_all()

    # -- simulator -------------------------------------------------------------------------------
    @app.post("/ops/simulate/faults")
    def simulate(body: dict = Body(...)):
        return fleet.simulate_fault(body["dp"], body["fault_id"], tick=body.get("tick", True))

    @app.post("/ops/simulate/reset")
    def simulate_reset(body: dict = Body(...)):
        return fleet.simulate_reset(body["dp"])

    @app.post("/ops/simulate/resolve-source")
    def simulate_resolve(body: dict = Body(...)):
        return fleet.resolve_source(body["dp"], body["fault_id"])

    # -- reviews ---------------------------------------------------------------------------------
    @app.get("/ops/finops/{dp}")
    def finops(dp: str):
        return fleet.finops(dp)

    @app.post("/ops/finops/{dp}/review")
    def finops_review(dp: str):
        return fleet.finops_review(dp)

    @app.get("/ops/governance/{dp}")
    def governance(dp: str):
        return fleet.governance(dp)

    @app.post("/ops/governance/{dp}/review")
    def governance_review(dp: str):
        return fleet.governance_review(dp)

    @app.get("/ops/governance/{dp}/evidence-pack", response_class=PlainTextResponse)
    def evidence_pack(dp: str, period: str):
        return fleet.evidence_pack(dp, period)

    @app.get("/ops/learning/{dp}")
    def learning(dp: str):
        return fleet.learning(dp)

    @app.post("/ops/learning/{dp}/{action}")
    def learning_action(dp: str, action: str, body: dict = Body(default={})):
        return fleet.learning_action(dp, action, body)

    # -- control ---------------------------------------------------------------------------------
    @app.post("/ops/stop")
    def stop(request: Request, body: dict = Body(...)):
        return fleet.stop(by=as_person(request, body.get("by")), reason=body.get("reason", "emergency stop"),
                          dp=body.get("dp"))

    @app.delete("/ops/stop")
    def clear_stop(request: Request, by: Optional[str] = None, dp: Optional[str] = None):
        return fleet.clear_stop(by=as_person(request, by), dp=dp)

    @app.post("/ops/requests")
    def request_desk(request: Request, body: dict = Body(...)):
        return fleet.request(body["dp"], body["text"], as_person(request, body.get("by")))

    # -- console ---------------------------------------------------------------------------------
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB_DIR / "index.html")

    return app


def seed_demo(home: str | Path, profile: Optional[str] = None) -> list[str]:
    """Onboard both golden products to OG1 with the packs' synthetic approvers (Demo Simulator only)."""
    from ..packs import handover_pack
    fleet = Fleet(home)
    done = []
    for name in ("utilities", "telco"):
        pack = handover_pack(name)
        if pack["dp_id"] in fleet.products():
            continue
        res = fleet.create_service({"mode": "demo", "pack": name, "profile": profile})
        for rid in (res["og0_request"],):
            for role in fleet.approvals(pack["dp_id"])[0]["roles"]:
                fleet.decide(pack["dp_id"], rid, approver=pack["owners"][role["role"]], role=role["role"],
                             decision="approved", reason="demo seed (synthetic approver)")
        ob = fleet.onboard(pack["dp_id"])
        fleet.decide(pack["dp_id"], ob["og1_request"], approver=pack["owners"]["platform_owner"],
                     role="platform_owner", decision="approved", reason="demo seed (synthetic approver)")
        done.append(pack["dp_id"])
    return done
