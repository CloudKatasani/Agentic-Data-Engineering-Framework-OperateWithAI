"""REST API and web console for OperateWithAI.

Every page of the console reads from here and every human decision is a POST to
``/api/inbox/{id}``; the engine checks that the caller is the person the request was
addressed to (or the person it was escalated to).

Security, same convention as Build-With-AI:
* ``ADEF_API_TOKEN``: when set, every ``/api`` call except ``/api/health`` needs it as a bearer token.
* ``ADEF_IDENTITY_FILE``: optional YAML mapping ``sha256(token)`` to a roster name. When set,
  each person uses their own token and can only act as themselves. Without it the caller
  names themselves in the ``X-Actor`` header (demo and single-team use).
"""

from __future__ import annotations

import hashlib
import os
import secrets as pysecrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..agents import llm, reporting, team
from ..engine.ops import Refused
from ..service import Service
from ..sim.scenarios import PRESETS, SCENARIOS

WEB = Path(__file__).resolve().parent.parent / "web"


def _identities() -> dict[str, str]:
    path = os.environ.get("ADEF_IDENTITY_FILE")
    return {str(k): str(v) for k, v in (yaml.safe_load(Path(path).read_text()) or {}).items()} if path else {}


def create_app(service: Optional[Service] = None, autotick: Optional[float] = None) -> FastAPI:
    svc = service or Service()
    stop = threading.Event()

    def clock():
        while not stop.wait(autotick):
            try:
                if svc.now.hour < 9:
                    svc.cycle()
            except Exception:   # one bad cycle must not stop the clock
                pass

    @asynccontextmanager
    async def lifespan(_app):
        if autotick:
            threading.Thread(target=clock, name="owai-clock", daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="OperateWithAI", version=__version__, lifespan=lifespan)
    app.state.service = svc
    token, ids = os.environ.get("ADEF_API_TOKEN", ""), _identities()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        request.state.actor = None
        if path.startswith("/api") and path != "/api/health":
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            who = ids.get(hashlib.sha256(given.encode()).hexdigest()) if given else None
            if ids and not who and not (token and pysecrets.compare_digest(given, token)):
                return JSONResponse({"detail": "A personal API token is required."}, status_code=401)
            if not ids and token and not pysecrets.compare_digest(given, token):
                return JSONResponse({"detail": "The API token is missing or wrong."}, status_code=401)
            request.state.actor = who
        return await call_next(request)

    @app.exception_handler(Refused)
    async def refused(_r, exc: Refused):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(KeyError)
    async def missing(_r, exc: KeyError):
        return JSONResponse({"detail": f"Not found: {exc.args[0]}"}, status_code=404)

    def actor(request: Request) -> str:
        name = request.state.actor or request.headers.get("x-actor", "")
        if request.state.actor and request.headers.get("x-actor") not in (None, "", name):
            raise Refused(f"Your token identifies you as {name}; you cannot act as someone else.")
        if not name:
            raise Refused("Say who you are (X-Actor header).")
        return name

    def ops():
        return svc.ops

    # ------------------------------------------------------------------ read
    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/pulse")
    def pulse():
        return {"now": svc.now.isoformat(), "notes": len(ops().store.all("note")), "run": svc.runs}

    @app.get("/api/meta")
    def meta(request: Request):
        env = svc.env
        return {"version": __version__, "client": env.client, "me": request.state.actor, "llm_assist": llm.enabled(),
                "people": [{**p, "tower_name": env.tower_name(p["tower"]) if p.get("tower") else "", "role_text": env.data["roles"].get(p.get("role", ""), "")}
                           for p in env.people],
                "towers": env.data["towers"], "scenarios": SCENARIOS, "presets": PRESETS, "running": svc.scenarios,
                "requests": list(ops().kb.requests.values()),
                "nodes": {n["id"]: n["name"] for n in env.nodes.values()}}

    def _inc_row(t: dict) -> dict:
        return {k: t.get(k) for k in ("id", "type", "title", "priority", "state", "tower", "assignee", "opened_at", "ack_at", "resolved_at",
                                      "sla", "services", "ke", "major", "stage", "requester", "catalogue")} | {
            "tower_name": svc.env.tower_name(t["tower"]), "events": len(t.get("events", [])), "next": reporting._next_step(t) if t["type"] == "incident" and t["state"] != "Resolved" else ""}

    @app.get("/api/overview")
    def overview():
        o, env = ops(), svc.env
        h = reporting.health(o)
        asks = [n for n in o.notes() if n["kind"] != "fyi"]
        waiting: dict[str, int] = {}
        for n in asks:
            waiting[n["escalated_to"] or n["to"]] = waiting.get(n["escalated_to"] or n["to"], 0) + 1
        incs = o.tickets("incident")
        towers = []
        for t in env.data["towers"]:
            systems = [s for s in h["systems"] if s["tower"] == t["name"]]
            mine = [i for i in incs if i["tower"] == t["id"] and i["state"] != "Resolved"]
            towers.append({"id": t["id"], "name": t["name"], "on_call": env.on_call(t["id"]), "systems": systems, "incidents": [i["id"] for i in mine],
                           "status": "red" if mine or any(s["status"] == "red" for s in systems) else "amber" if any(s["status"] == "amber" for s in systems) else "green"})
        return {"now": svc.now.isoformat(), "client": env.client["name"], "services": h["services"], "towers": towers,
                "incidents": [_inc_row(i) for i in sorted(incs, key=lambda i: (i["state"] == "Resolved", i["priority"], i["opened_at"]))],
                "waiting": sorted(waiting.items(), key=lambda x: -x[1]), "asks": len(asks),
                "advisories": [e for e in o.store.all("event") if e["status"] == "open" and not e["incident"]],
                "activity": o.store.audit_log(limit=14), "watch": o.store.kv("watch_stats") or {}, "running": svc.scenarios,
                "prevented": svc.world.prevented}

    @app.get("/api/inbox")
    def inbox(person: str = "", all: bool = False):
        return {"person": person, "items": ops().notes(person or None, open_only=not all)[:200]}

    @app.get("/api/tickets")
    def tickets(type: str = ""):
        return [_inc_row(t) for t in sorted(ops().tickets(type or None), key=lambda t: (t["state"] in ("Resolved", "Closed"), t["priority"], t["id"]))]

    @app.get("/api/tickets/{tid}")
    def ticket(tid: str):
        o, env = ops(), svc.env
        t = o.store.need("ticket", tid)
        out: dict[str, Any] = {"ticket": t, "tower_name": env.tower_name(t["tower"]),
                               "asks": [n for n in o.store.all("note") if n["ticket"] == tid and n["status"] == "open" and n["kind"] != "fyi"],
                               "audit": o.store.audit_log(ref=tid, limit=200)}
        if t["type"] == "incident":
            proj = o.projection
            out["events"] = [o.store.need("event", e) for e in t["events"]]
            out["ke"] = o.kb.get(t["ke"])
            chains = []
            for sid in t["services"]:
                nodes = []
                for nid in env.chain(sid):
                    n = env.nodes[nid]
                    job = proj["jobs"].get(nid)
                    state = job["state"] if job else ("ready" if proj["ready"].get(nid) else "not ready")
                    nodes.append({"id": nid, "name": n["name"], "kind": n["kind"], "ci": n["ci"], "state": state,
                                  "root": nid in t["root_nodes"], "affected": nid in t["impact_nodes"], "eta": job["eta"] if job else None})
                chains.append({"service": proj["services"][sid], "nodes": nodes})
            out["chains"] = chains
        return out

    @app.get("/api/batch")
    def batch():
        p = ops().projection
        return {"now": p["now"], "jobs": sorted(p["jobs"].values(), key=lambda j: j["planned"]), "services": list(p["services"].values()),
                "lag_minutes": p["lag_minutes"]}

    @app.get("/api/events")
    def events():
        return {"events": sorted(ops().store.all("event"), key=lambda e: e["first_seen"], reverse=True)[:300], "watch": ops().store.kv("watch_stats") or {}}

    @app.get("/api/knowledge")
    def knowledge():
        kb = ops().kb
        return {"known_errors": kb.known_errors(), "actions": list(kb.actions.values()), "never": kb.never, "connectors": kb.connectors,
                "requests": list(kb.requests.values())}

    @app.get("/api/reports/{name}")
    def report(name: str):
        o = ops()
        if name == "health":
            return reporting.health(o)
        if name == "handover":
            return {"markdown": reporting.handover(o)}
        if name == "review":
            return reporting.review(o)
        raise KeyError(name)

    @app.get("/api/team")
    def team_view():
        env = svc.env
        return {"agents": team.AGENTS, "stats": team.stats(ops()), "decisions": team.HUMAN_DECISIONS, "flow": team.FLOW,
                "roster": [{**p, "tower_name": env.tower_name(p["tower"]) if p.get("tower") else "", "role_text": env.data["roles"].get(p.get("role", ""), ""),
                            "open": sum(1 for n in ops().notes(p["name"]) if n["kind"] != "fyi")} for p in env.people]}

    @app.get("/api/audit")
    def audit(ref: str = "", limit: int = 300):
        return {"entries": ops().store.audit_log(ref=ref or None, limit=min(limit, 2000)), "chain_intact": ops().store.audit_verify()}

    # ------------------------------------------------------------------ people act
    @app.post("/api/inbox/{note_id}")
    def act(note_id: str, request: Request, body: dict = Body(...)):
        n = svc.act(note_id, actor(request), str(body.get("action", "")), str(body.get("text", "")))
        return {"ok": True, "ticket": n["ticket"]}

    @app.post("/api/requests")
    def new_request(request: Request, body: dict = Body(...)):
        t = svc.raise_request(str(body.get("catalogue", "")), dict(body.get("fields") or {}), str(body.get("requester") or actor(request)))
        return {"id": t["id"]}

    @app.post("/api/tickets/{tid}/comment")
    def comment(tid: str, request: Request, body: dict = Body(...)):
        if not str(body.get("text", "")).strip():
            raise HTTPException(422, "Write a comment first.")
        svc.comment(tid, actor(request), str(body["text"]).strip()[:2000])
        return {"ok": True}

    # ------------------------------------------------------------------ simulator
    @app.post("/api/sim/start")
    def sim_start(body: dict = Body(...)):
        svc.restart([str(s) for s in body.get("scenarios", [])])
        if body.get("until"):
            svc.run_until(str(body["until"]))
        return {"now": svc.now.isoformat(), "scenarios": svc.scenarios}

    @app.post("/api/sim/advance")
    def sim_advance(body: dict = Body(...)):
        if svc.now.hour >= 9:
            raise HTTPException(409, "The night is over (09:00). Start a new night in the simulator.")
        if body.get("until"):
            svc.run_until(str(body["until"]))
        else:
            svc.advance(max(5, min(int(body.get("minutes", 5)), 240)))
        return {"now": svc.now.isoformat()}

    # ------------------------------------------------------------------ console
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    return app
