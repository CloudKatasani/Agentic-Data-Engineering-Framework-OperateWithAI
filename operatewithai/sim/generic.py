"""A data-driven simulated estate for any client model.

Where ``world.py`` hand-models one estate (Oracle, GoldenGate, Informatica ...), this one runs
whatever the client's ``environment.yaml`` describes: batch jobs, streams, landed files and
the datasets between them. What goes wrong comes from the client's ``scenarios.yaml`` as
faults; each fault says what breaks, which alert the tool raises, what the read-only checks
would show while it is active and which catalogue action fixes it. Tools report through the
event descriptions in the technology catalogue, so the payloads are the ones the real tools send.

Fault types:
  job_error  a job fails with a message (after N minutes of running, or at start)
  stall      a stream stops moving; its backlog grows until fixed
  slow       jobs on a system run slower by a factor
  file_late  a landed file arrives late
  quality    a dataset arrives but fails a data quality check and is held back
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

import yaml

from ..env import CLIENTS_DIR, Environment, hhmm
from .world import NIGHT, TICK


def library(client: str) -> dict:
    return yaml.safe_load((CLIENTS_DIR / client / "scenarios.yaml").read_text("utf-8"))


def _put(obj: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    for i, p in enumerate(parts[:-1]):
        nxt = [] if parts[i + 1].isdigit() else {}
        if isinstance(obj, list):
            while len(obj) <= int(p):
                obj.append(nxt)
            obj = obj[int(p)]
        else:
            obj = obj.setdefault(p, nxt)
    if isinstance(obj, list):
        obj.append(value)
    else:
        obj[parts[-1]] = value


class GenericWorld:
    def __init__(self, env: Environment, kb, start: datetime = NIGHT):
        self.env, self.kb = env, kb
        self.now = self.day = start
        self.lib = library(env.client["id"])
        self.jobs: dict[str, dict] = {}
        for n in env.jobs():
            self.jobs[n["id"]] = {"state": "scheduled", "planned": hhmm(start, n["planned"]), "duration": n["duration"], "started": None,
                                  "ended": None, "elapsed": 0.0, "attempt": 0, "error": None, "run_minutes": n["duration"]}
        self.ready = {n["id"]: n["kind"] == "source" for n in env.nodes.values()}
        self.lags = {n["id"]: 1.0 for n in env.nodes.values() if n["kind"] == "stream"}
        self.files = {n["id"]: {"arrives": hhmm(start, n.get("arrives", "00:50")), "received": None, "reported": False}
                      for n in env.nodes.values() if n["kind"] == "file"}
        self.faults: dict[str, dict] = {}          # active faults by id
        self.pending: list[tuple[datetime, dict]] = []
        self.held: set[str] = set()                # datasets held back by a quality check
        self.outbox: list[tuple[str, Any]] = []
        self.actions: list[dict] = []
        self.prevented: list[str] = []
        self.grants: list[dict] = []

    # ------------------------------------------------------------------ faults
    def schedule_scenario(self, scenario: dict) -> None:
        for i, f in enumerate(scenario["faults"]):
            self.pending.append((hhmm(self.day, f["at"]), {**f, "id": f"{scenario['id']}-{i}"}))

    def _activate(self, f: dict) -> None:
        self.faults[f["id"]] = f
        if f["type"] == "file_late":
            self.files[f["node"]]["arrives"] = hhmm(self.day, f["arrives"])
        if f.get("alert"):
            self.outbox.append((f["alert"]["source"], f["alert"]["payload"]))

    def _clear(self, fid: str) -> None:
        f = self.faults.pop(fid, None)
        if not f:
            return
        if f.get("alert") and f["alert"].get("clear"):
            self.outbox.append((f["alert"]["source"], f["alert"]["clear"]))
        for job in f.get("reprocess", []):       # after the fix these jobs must run again, in order
            j = self.jobs[job]
            j.update(state="scheduled", planned=self.now, started=None, ended=None, elapsed=0.0, error=None)
            for out in self.env.down[job]:
                self.ready[out] = False
        if f["type"] == "quality" and not f.get("reprocess"):
            self.held.discard(f["node"])
            self.ready[f["node"]] = True
            self._quality(f, passed=True)

    def _active(self, type_: str, **match) -> Optional[dict]:
        for f in self.faults.values():
            if f["type"] == type_ and all(f.get(k) == v or v in (f.get(k + "s") or []) for k, v in match.items()):
                return f
        return None

    # ------------------------------------------------------------------ reporting, the way the tools do it
    def _spec(self, node: str, kind: str) -> Optional[dict]:
        tool = self.env.cis[self.env.nodes[node]["ci"]]["tool"]
        return next((s for s in self.kb.event_specs.values() if s["tech"] == tool and s["kind"] == kind), None)

    def _report_run(self, node: str, ok: bool, message: str = "") -> None:
        n = self.env.nodes[node]
        spec = self._spec(node, "run")
        if spec:
            payload: dict = {}
            _put(payload, spec["entity"], n.get("ref", n["name"]))
            _put(payload, spec["status"], spec["ok"][0] if ok else spec["failed"][0])
            if spec.get("message") and message:
                _put(payload, spec["message"], message)
            self.outbox.append((spec["id"], payload))
        elif self.env.cis[n["ci"]]["tool"] == "powerbi":
            self.outbox.append(("powerbi", {"datasetName": n["name"].split(": ", 1)[-1], "status": "Completed" if ok else "Failed",
                                            "serviceExceptionJson": None if ok else json.dumps({"errorCode": "", "errorDescription": message})}))

    def _quality(self, f: dict, passed: bool) -> None:
        self.outbox.append(("data_quality", {"table": self.env.nodes[f["node"]].get("table", f["node"]), "test_type": f["check"],
                                             "status": "pass" if passed else "fail", "description": "" if passed else f["message"]}))

    def telemetry(self) -> list[tuple[str, Any]]:
        out, self.outbox = self.outbox, []
        for node, lag in self.lags.items():
            spec = self._spec(node, "lag")
            if spec:
                payload: dict = {}
                _put(payload, spec["entity"], self.env.nodes[node].get("ref", node))
                _put(payload, spec["value"], round(lag))
                out.append((spec["id"], payload))
        return out

    # ------------------------------------------------------------------ clock
    def tick(self, minutes: int = TICK) -> None:
        self.now += timedelta(minutes=minutes)
        for item in [p for p in self.pending if p[0] <= self.now]:
            self.pending.remove(item)
            self._activate(item[1])
        for node, f in self.files.items():
            n = self.env.nodes[node]
            cutoff = hhmm(self.day, n["expected_by"])
            base = {"feed": n.get("ref", node), "expected_file": n["name"], "expected_by": n["expected_by"], "partner": n.get("partner", "the sender"),
                    "avg_size_bytes_7d": 5_000_000}
            if not f["received"] and self.now >= f["arrives"]:
                f["received"] = f["arrives"]
                self.ready[node] = True
                self._clear_where("file_late", node)
                self.outbox.append(("mft", {**base, "status": "RECEIVED", "received_at": f["received"].isoformat(), "size_bytes": 5_050_000}))
            elif not f["received"] and self.now >= cutoff and not f["reported"]:
                f["reported"] = True
                self.outbox.append(("mft", {**base, "status": "MISSING", "received_at": None, "size_bytes": None}))
        for node in self.lags:
            limit = self.env.nodes[node].get("max_lag_minutes", 15)
            self.lags[node] = self.lags[node] + minutes if self._active("stall", node=node) else max(1.0, self.lags[node] - minutes * 4)
            self.ready[node] = self.lags[node] <= limit
            for d in self.env.down[node]:
                if self.env.nodes[d]["kind"] == "dataset":
                    self.ready[d] = self.ready[node]
        for node in sorted(self.jobs, key=lambda n: self.env.depth[n]):
            self._job(node, minutes)

    def _clear_where(self, type_: str, node: str) -> None:
        for fid in [fid for fid, f in self.faults.items() if f["type"] == type_ and f.get("node") == node]:
            self._clear(fid)

    def _job(self, node: str, minutes: int) -> None:
        j = self.jobs[node]
        if j["state"] in ("scheduled", "waiting"):
            if self.now < j["planned"]:
                return
            if any(not self.ready.get(u) for u in self.env.up.get(node, [])):
                j["state"] = "waiting"
                return
            self._start(node)
            return
        if j["state"] != "running":
            return
        slow = self._active("slow", ci=self.env.nodes[node]["ci"])
        j["elapsed"] += minutes / (slow["factor"] if slow else 1.0)
        f = self._active("job_error", node=node)
        if f and j["elapsed"] >= f.get("after", 0):
            self._finish(node, f)
        elif j["elapsed"] >= j["run_minutes"]:
            self._finish(node, None)

    def _start(self, node: str) -> None:
        j = self.jobs[node]
        j.update(state="running", started=self.now, ended=None, elapsed=0.0, error=None, attempt=j["attempt"] + 1)
        f = self._active("job_error", node=node)
        if f and not f.get("after"):
            self._finish(node, f)

    def _finish(self, node: str, fault: Optional[dict]) -> None:
        j = self.jobs[node]
        j.update(state="failed" if fault else "succeeded", ended=self.now, error={"message": fault["message"]} if fault else None)
        if fault:
            if fault.get("transient"):
                self._clear(fault["id"])
            return self._report_run(node, False, fault["message"])
        for out in self.env.down[node]:
            q = self._active("quality", node=out)
            if q and q.get("after_job") == node:
                self.held.add(out)
                self._quality(q, passed=False)
            else:
                if out in self.held:
                    self.held.discard(out)
                    self.outbox.append(("data_quality", {"table": self.env.nodes[out].get("table", out), "test_type": "volume_anomalies", "status": "pass", "description": ""}))
                self.ready[out] = True
        self._report_run(node, True)

    # ------------------------------------------------------------------ read-only checks
    def probe(self, name: str, **a) -> dict:
        for f in self.faults.values():
            if name in (f.get("evidence") or {}):
                return f["evidence"][name]
        if name == "change.recent":
            ch = self.env.change_for(a["ci"], self.now)
            return {"summary": f"{ch['id']}: {ch['summary']} (window closed {ch['window_end']})" if ch else f"No recent approved change on {a['ci']}",
                    "facts": {"change_found": bool(ch)}}
        if name == "batch.job_state":
            j = self.jobs[a["node"]]
            return {"summary": f"{self.env.nodes[a['node']]['name']} is {j['state']} (attempt {j['attempt']})",
                    "facts": {"state": j["state"], "succeeded": j["state"] == "succeeded", "not_failed": j["state"] != "failed"}}
        if name == "stream.lag":
            lag, limit = self.lags[a["node"]], self.env.nodes[a["node"]].get("max_lag_minutes", 15)
            return {"summary": f"{self.env.nodes[a['node']]['name']} is {lag:.0f} min behind (limit {limit})", "facts": {"within_limit": lag <= limit, "lag_minutes": lag}}
        if name == "mft.file_status":
            node = next(iter(self.files))
            got = bool(self.files[node]["received"])
            return {"summary": f"{self.env.nodes[node]['name']}: {'RECEIVED' if got else 'MISSING, expected by ' + self.env.nodes[node]['expected_by']}",
                    "facts": {"missing": not got, "status": "RECEIVED" if got else "MISSING", "size_double": False}}
        if name in (self.lib.get("probes") or {}):
            return self.lib["probes"][name]
        raise KeyError(f"unknown probe {name}")

    # ------------------------------------------------------------------ catalogue actions
    def apply(self, action: str, **a) -> dict:
        self.actions.append({"at": self.now.isoformat(), "action": action, **a})
        title = self.kb.actions.get(action, {}).get("title", action)
        if action == "manual.fix":
            for fid in [fid for fid, f in self.faults.items() if a.get("node") in ([f.get("node")] + (f.get("nodes") or []))]:
                self._clear(fid)
            return {"ok": True, "message": "Recorded"}
        fixed = [fid for fid, f in self.faults.items() if f.get("fixed_by") == action]
        for fid in fixed:
            self._clear(fid)
        node = a.get("node")
        if node in self.jobs and (action in self.kb.rerun.values() or self.kb.actions.get(action, {}).get("reruns")):
            j = self.jobs[node]
            if j["state"] != "running" and not (fixed and j["state"] == "scheduled"):
                if any(not self.ready.get(u) for u in self.env.up.get(node, [])):
                    j["state"] = "waiting"
                else:
                    self._start(node)
            if j["state"] == "failed":
                return {"ok": False, "message": f"{self.env.nodes[node]['name']} failed again: {j['error']['message'][:160]}"}
            return {"ok": True, "message": f"{title}: {self.env.nodes[node]['name']} started (attempt {j['attempt']})"}
        if action == "snowflake.grant_role":
            self.grants.append({"user": a.get("user"), "role": a.get("role"), "at": self.now.isoformat()})
        return {"ok": True, "message": f"{title}: done"}

    # ------------------------------------------------------------------ scheduler view
    def batch_status(self) -> dict:
        jobs = {}
        for node, j in self.jobs.items():
            jobs[node] = {"state": j["state"], "planned": j["planned"], "started": j["started"], "ended": j["ended"], "attempt": j["attempt"],
                          "duration": j["duration"], "run_minutes": j["run_minutes"],
                          "pct": round(min(j["elapsed"] / j["run_minutes"], 1.0), 3) if j["state"] == "running" else None,
                          "error": (j["error"] or {}).get("message", "")[:200] if j["state"] == "failed" else ""}
        return {"now": self.now, "jobs": jobs, "ready": dict(self.ready), "built": [], "lags": dict(self.lags),
                "lag_minutes": round(max(self.lags.values(), default=0), 1), "files": {n: bool(f["received"]) for n, f in self.files.items()},
                "file_received": all(f["received"] for f in self.files.values())}
