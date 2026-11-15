"""A simulated client estate for demos, training and tests.

It runs the nightly batch from the environment model on a simulated clock, keeps the state
of GoldenGate, hosts, warehouses, the file feed and service accounts, and reports status the
way each real tool does (see ``telemetry``): Informatica workflow runs, GoldenGate ``info
all`` and ``ggserr.log``, dbt run results, Power BI refresh history, Cognos audit rows,
Denodo Scheduler jobs, Dynatrace problems, MFT feed status and Snowflake monitors.

Faults are named conditions (for example ``ogg.ddl_mismatch``). Diagnostic probes read the
state; catalogue actions change it. Nothing here is real client data.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from ..env import Environment, hhmm

NIGHT = datetime(2026, 3, 3, 0, 30)
TICK = 5  # minutes
MODEL_OF = {"snow.mart.fin": "fct_gl_balances", "snow.mart.policy": "fct_policy_premium", "snow.mart.claims": "fct_claims"}
SOURCE_OF = {"snow.raw.erp": "raw_erp.gl_je_lines", "snow.raw.gl_controls": "raw_gl.control_totals",
             "snow.raw.policy": "raw_policy.policy_txn", "snow.raw.claims": "raw_claims.claims"}
MANUAL_CLEARS = {"denodo.cache_fin": ["denodo.pool_exhausted"]}
TYPICAL_ROWS = {"infa.wf_policy": 412_300, "infa.wf_gl_controls": 1_840, "infa.wf_claims": 18_412}


def _fmt(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y %H:%M:%S")


def _hms(seconds: float) -> str:
    s = int(max(seconds, 0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class World:
    def __init__(self, env: Environment, start: datetime = NIGHT):
        self.env = env
        self.now = start
        self.day = start
        self.conditions: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        for n in env.jobs():
            self.jobs[n["id"]] = {"state": "scheduled", "planned": hhmm(start, n["planned"]), "duration": n["duration"],
                                  "started": None, "ended": None, "elapsed": 0.0, "attempt": 0, "error": None,
                                  "rows": None, "run_minutes": n["duration"]}
        self.ready: dict[str, bool] = {n["id"]: n["kind"] == "source" for n in env.nodes.values()}
        self.built: set[str] = set()
        self.ogg = {p: {"status": "RUNNING", "since": None, "backlog_s": 0.0, "error": None}
                    for p in ("EXT_ERP", "PMP_ERP", "REP_SNF")}
        self.hosts = {c["host"]: {"available": True, "cpu": 28} for c in env.cis.values() if c.get("host")}
        self.warehouses = {w["name"]: {"state": "STARTED", "monitor": w["resource_monitor"],
                                       "quota": w["monthly_quota_credits"], "used": round(w["monthly_quota_credits"] * 0.71, 1)}
                           for w in env.data["snowflake"]["warehouses"]}
        self.file = {"arrives_at": hhmm(start, "01:05"), "received_at": None, "duplicate": False, "cutoff_reported": False}
        self.accounts = {f"{a['ci']}/{a['account']}": {"status": "OPEN", "expires_in_days": a["password_expires_in_days"]}
                         for a in env.data["service_accounts"]}
        self.claims_duplicated = False
        self.target_columns_missing: list[str] = []
        self.pending: list[tuple[datetime, str, dict]] = []   # scheduled fault injections
        self.outbox: list[tuple[str, Any]] = []                # telemetry produced since last read
        self.problems: dict[str, dict] = {}                    # open Dynatrace problems
        self.actions: list[dict] = []
        self.delivered: dict[str, datetime] = {}
        self.grants: list[dict] = []
        self.prevented: list[str] = []

    # ------------------------------------------------------------------ faults
    def schedule(self, at: str, condition: str, **detail) -> None:
        self.pending.append((hhmm(self.day, at), condition, detail))

    def set_condition(self, name: str, **detail) -> None:
        self.conditions[name] = detail
        if name == "ogg.ddl_mismatch":
            self.target_columns_missing = ["COST_CENTER_SEGMENT"]
            self._abend("REP_SNF", "OGG-01296", "Error mapping from ERP.GL_JE_LINES to RAW_ERP.GL_JE_LINES.",
                        "OGG-01668", "PROCESS ABENDING.")
        elif name == "oracle.account_expired":
            if self.accounts["ORAPRD_ERP/SVC_DATA"]["expires_in_days"] > 30:   # already rotated preventively: nothing happens
                del self.conditions[name]
                self.prevented.append(name)
                return
            self.accounts["ORAPRD_ERP/SVC_DATA"] = {"status": "EXPIRED", "expires_in_days": 0}
            self._abend("EXT_ERP", "OGG-00664", "OCI Error beginning session (status = 28001-ORA-28001: the password has expired).",
                        "OGG-01668", "PROCESS ABENDING.")
        elif name == "snowflake.rm_quota":
            w = self.warehouses["WH_TRANSFORM"]
            w.update(used=w["quota"] + 0.4, state="SUSPENDED")
        elif name == "azure.vm_down":
            self.hosts["gw-az-01"]["available"] = False
            self._problem("gw-az-01", "Host or monitoring unavailable", "AVAILABILITY")
        elif name == "host.cpu_saturation":
            self.hosts["infa-prd-01"]["cpu"] = 97
            self._problem("infa-prd-01", "CPU saturation", "RESOURCE_CONTENTION")
        elif name == "mft.file_late":
            self.file["arrives_at"] = hhmm(self.day, detail.get("arrives", "03:40"))
        elif name == "mft.duplicate_file":
            self.file["duplicate"] = True

    def _abend(self, proc: str, code: str, msg: str, code2: str, msg2: str) -> None:
        self.ogg[proc].update(status="ABENDED", since=self.now, error={"code": code, "message": msg})
        ts = self.now.strftime("%Y-%m-%dT%H:%M:%S.000-0500")
        prm = proc.lower() + ".prm"
        self.outbox.append(("goldengate_log", f"{ts}  ERROR   {code}  Oracle GoldenGate {'Capture' if proc.startswith('EXT') else 'Delivery'} for Oracle, {prm}:  {msg}"))
        self.outbox.append(("goldengate_log", f"{ts}  ERROR   {code2}  Oracle GoldenGate {'Capture' if proc.startswith('EXT') else 'Delivery'} for Oracle, {prm}:  {msg2}"))

    def _problem(self, host: str, title: str, severity: str) -> None:
        pid = f"P-{self.now.strftime('%y%m%d%H%M')}{len(self.problems) + 1}"
        self.problems[f"{host}:{title}"] = {"ProblemID": pid, "host": host, "title": title, "severity": severity}
        self.outbox.append(("dynatrace", {"ProblemID": pid, "State": "OPEN", "ProblemTitle": title, "ProblemSeverity": severity,
                                          "ProblemImpact": "INFRASTRUCTURE",
                                          "ImpactedEntities": [{"type": "HOST", "name": host, "entity": f"HOST-{abs(hash(host)) % 10**8:08d}"}],
                                          "ProblemURL": f"https://dynatrace.example/#problems/problemdetails;pid={pid}"}))

    def _resolve_problem(self, host: str, title: str) -> None:
        p = self.problems.pop(f"{host}:{title}", None)
        if p:
            self.outbox.append(("dynatrace", {"ProblemID": p["ProblemID"], "State": "RESOLVED", "ProblemTitle": title,
                                              "ProblemSeverity": p["severity"], "ProblemImpact": "INFRASTRUCTURE",
                                              "ImpactedEntities": [{"type": "HOST", "name": host}]}))

    # ------------------------------------------------------------------ clock
    def tick(self, minutes: int = TICK) -> None:
        self.now += timedelta(minutes=minutes)
        for item in [p for p in self.pending if p[0] <= self.now]:
            self.pending.remove(item)
            self.set_condition(item[1], **item[2])
        self._file()
        self._replication(minutes)
        for node in sorted(self.jobs, key=lambda n: self.env.depth[n]):
            self._job(node, minutes)

    def _file(self) -> None:
        f = self.file
        cutoff = hhmm(self.day, self.env.nodes["mft.claims_file"]["expected_by"])
        if f["received_at"] is None and self.now >= f["arrives_at"]:
            f["received_at"] = f["arrives_at"]
            self.ready["mft.claims_file"] = True
            self.outbox.append(("mft", self._mft_payload()))
        elif f["received_at"] is None and self.now >= cutoff and not f["cutoff_reported"]:
            f["cutoff_reported"] = True
            self.outbox.append(("mft", self._mft_payload()))

    def _mft_payload(self) -> dict:
        f = self.file
        day = self.day.strftime("%Y%m%d")
        size = 7_364_800 * (2 if f["duplicate"] else 1)
        return {"feed": "claims_vendor", "expected_file": f"claims_{day}.csv", "expected_by": self.env.nodes["mft.claims_file"]["expected_by"],
                "status": "RECEIVED" if f["received_at"] else "MISSING",
                "received_at": f["received_at"].isoformat() if f["received_at"] else None,
                "size_bytes": size if f["received_at"] else None, "avg_size_bytes_7d": 7_310_000,
                "partner": "Claims administration vendor"}

    def _replication(self, minutes: int) -> None:
        for name, p in self.ogg.items():
            if p["status"] == "RUNNING" and p["backlog_s"] > 0:
                p["backlog_s"] = max(0.0, p["backlog_s"] - minutes * 60 * 4)   # catches up 4x real time
        self.ready["ogg.rep_snf"] = all(p["status"] == "RUNNING" for p in self.ogg.values())
        self.ready["snow.raw.erp"] = self.replication_lag_s() <= self.env.nodes["ogg.rep_snf"]["max_lag_minutes"] * 60

    def replication_lag_s(self) -> float:
        lag = 5.0
        for p in self.ogg.values():
            if p["status"] != "RUNNING":
                lag = max(lag, (self.now - p["since"]).total_seconds())
            lag = max(lag, p["backlog_s"])
        return lag

    # ------------------------------------------------------------------ batch
    def _inputs_ready(self, node: str) -> list[str]:
        return [u for u in self.env.up.get(node, []) if not self.ready.get(u)]

    def _slow(self, node: str) -> float:
        return 2.5 if "host.cpu_saturation" in self.conditions and self.env.nodes[node]["ci"] == "INFA_PRD" else 1.0

    def _job(self, node: str, minutes: int) -> None:
        j = self.jobs[node]
        if j["state"] in ("scheduled", "waiting"):
            if self.now < j["planned"]:
                return
            blocked = [] if node == "dbt.nightly_build" else self._inputs_ready(node)
            if blocked:
                j["state"], j["blocked_by"] = "waiting", blocked
                return
            self._start(node)
            return
        if j["state"] != "running":
            return
        j["elapsed"] += minutes / self._slow(node)
        fail = self._failure(node, j)
        if fail:
            self._finish(node, "failed", fail)
        elif j["elapsed"] >= j["run_minutes"]:
            self._finish(node, "succeeded", None)

    def _start(self, node: str, run_minutes: Optional[int] = None) -> None:
        j = self.jobs[node]
        j.update(state="running", started=self.now, ended=None, elapsed=0.0, error=None, attempt=j["attempt"] + 1,
                 run_minutes=run_minutes or j["duration"], blocked_by=[])
        if node == "dbt.nightly_build":
            j["stale_at_start"] = {m: self._inputs_ready(m) for m in self.env.nodes[node]["builds"]
                                   if m not in self.built and self._inputs_ready(m)}
        fail = self._failure(node, j)
        if fail and fail.get("at_start"):
            self._finish(node, "failed", fail)

    def _failure(self, node: str, j: dict) -> Optional[dict]:
        c = self.conditions
        if node == "infa.wf_policy" and "db2.lock_contention" in c and j["elapsed"] >= 20:
            del c["db2.lock_contention"]   # transient: the housekeeping job has finished by the retry
            return {"code": "RR_4035", "message": "Session task instance [s_m_POLICY_TXN_STG] : [RR_4035] SQL Error [\n[IBM][CLI Driver][DB2/LINUXX8664] "
                    "SQL0911N  The current transaction has been rolled back because of a deadlock or timeout.  Reason code \"68\".  SQLSTATE=40001\n]."}
        if node == "infa.wf_gl_controls" and "oracle.account_expired" in c:
            return {"at_start": True, "code": "CMN_1022", "message": "Session task instance [s_m_GL_CONTROL_TOTALS] : [CMN_1022] Database driver error...\n"
                    "CMN_1022 [\nORA-28001: the password has expired\n\nDatabase driver error...\nFunction Name : Logon\n"
                    "Database Error: Failed to connect to database using user [SVC_DATA] and connection string [ORAPRD_ERP].]"}
        if node == "dbt.nightly_build" and self.warehouses["WH_TRANSFORM"]["state"] == "SUSPENDED":
            return {"at_start": True, "code": "DBT_DATABASE_ERROR", "message": "Database Error\n  Warehouse 'WH_TRANSFORM' cannot be resumed because "
                    "resource monitor 'RM_TRANSFORM' has exceeded its quota."}
        if node == "pbi.fin_close" and "azure.vm_down" in c and j["elapsed"] >= 2:
            return {"code": "DM_GWPipeline_Client_GatewayUnreachable", "message": "The gateway is either offline or could not be reached."}
        if node == "denodo.cache_fin" and "denodo.pool_exhausted" in c and j["elapsed"] >= 5:
            return {"code": "", "message": "Finished with error: the cache load process of view 'bv_fin_close' failed. "
                    "com.denodo.vdb.cache.CacheException: Unable to obtain connection from pool ds_snowflake_fin: "
                    "pool exhausted (maxActive=20, active=20, idle=0) after waiting 30000 ms"}
        if node.startswith("cognos.") and "cognos.dispatcher_overload" in c and j["elapsed"] >= 3:
            return {"code": "DPR-ERR-2109", "message": "DPR-ERR-2109 The dispatcher cannot service the request at this time. "
                    "The dispatcher is still initializing. Contact your administrator if this problem persists."}
        return None

    def _finish(self, node: str, state: str, error: Optional[dict]) -> None:
        j = self.jobs[node]
        j.update(state=state, ended=self.now, error=error)
        tool = node.split(".")[0]
        if tool == "infa":
            rows = TYPICAL_ROWS[node] * (2 if node == "infa.wf_claims" and self.file["duplicate"] and state == "succeeded" else 1)
            j["rows"] = rows if state == "succeeded" else 0
            if state == "succeeded":
                for out in self.env.down[node]:
                    self.ready[out] = True
                if node == "infa.wf_claims":
                    self.claims_duplicated = self.file["duplicate"]
            self.outbox.append(("informatica", self._infa_payload(node)))
        elif tool == "dbt":
            self._dbt_finish(node, j)
        else:
            if state == "succeeded":
                for out in self.env.down[node]:
                    self.ready[out] = True
                self.delivered[node] = self.now
            self.outbox.append((tool if tool != "pbi" else "powerbi", self._bi_payload(node)))

    def _dbt_finish(self, node: str, j: dict) -> None:
        results, freshness = [], []
        if j["error"]:   # job-level failure (warehouse): nothing built
            for m in self.env.nodes[node]["builds"]:
                if m not in self.built:
                    results.append({"unique_id": f"model.analytics.{MODEL_OF[m]}", "status": "error", "message": j["error"]["message"]})
        else:
            for m in self.env.nodes[node]["builds"]:
                if m in self.built:
                    continue
                stale = j.get("stale_at_start", {}).get(m, [])
                if stale:
                    for u in stale:
                        ago = int(self.replication_lag_s()) if u == "snow.raw.erp" else 26 * 3600
                        freshness.append({"unique_id": f"source.analytics.{SOURCE_OF[u]}", "status": "error",
                                          "max_loaded_at_time_ago_in_s": ago,
                                          "criteria": {"error_after": {"count": 30 if u == "snow.raw.erp" else 24,
                                                                       "period": "minute" if u == "snow.raw.erp" else "hour"}}})
                    results.append({"unique_id": f"model.analytics.{MODEL_OF[m]}", "status": "skipped",
                                    "message": "Skipping due to stale source"})
                elif m == "snow.mart.claims" and self.claims_duplicated:
                    results.append({"unique_id": "model.analytics.fct_claims", "status": "success", "message": "SUCCESS 1"})
                    results.append({"unique_id": "test.analytics.unique_fct_claims_claim_id", "status": "fail", "failures": 18_412,
                                    "message": "Got 18412 results, configured to fail if != 0"})
                else:
                    results.append({"unique_id": f"model.analytics.{MODEL_OF[m]}", "status": "success", "message": "SUCCESS 1"})
                    self.built.add(m)
                    self.ready[m] = True
        ok = all(m in self.built for m in self.env.nodes[node]["builds"])
        j["state"] = "succeeded" if ok else "failed"
        if not ok and not j["error"]:
            j["error"] = {"code": "DBT_RUN_ERROR", "message": "; ".join(r["message"] for r in results if r["status"] != "success")}
        j["results"], j["freshness"] = results, freshness
        self.outbox.append(("dbt", {"job": "nightly_build", "run_id": 70_403_000 + j["attempt"], "status": "success" if ok else "error",
                                    "started_at": j["started"].isoformat(), "finished_at": self.now.isoformat(),
                                    "results": results, "freshness": freshness}))

    # ------------------------------------------------------------------ native payloads
    def _infa_payload(self, node: str) -> dict:
        j = self.jobs[node]
        code = {"succeeded": 1, "failed": 3, "running": 6}[j["state"]]
        return {"SUBJECT_AREA": "INS_DW", "WORKFLOW_NAME": self.env.nodes[node]["name"], "SERVER_NAME": "IS_PRD",
                "RUN_STATUS_CODE": code, "RUN_STATUS": {1: "Succeeded", 3: "Failed", 6: "Running"}[code],
                "START_TIME": _fmt(j["started"]), "END_TIME": _fmt(j["ended"]) if j["ended"] else None,
                "RUN_ERR_CODE": 0 if code == 1 else 36331, "RUN_ERR_MSG": (j["error"] or {}).get("message", ""),
                "SRC_SUCCESS_ROWS": j["rows"] or 0, "TARG_SUCCESS_ROWS": j["rows"] or 0, "TARG_FAILED_ROWS": 0}

    def _bi_payload(self, node: str) -> dict:
        j, name = self.jobs[node], self.env.nodes[node]["name"]
        ok = j["state"] == "succeeded"
        if node.startswith("pbi."):
            return {"datasetName": name.split(": ", 1)[1], "requestId": f"rq-{node}-{j['attempt']}", "refreshType": "Scheduled",
                    "startTime": j["started"].isoformat() + "Z", "endTime": j["ended"].isoformat() + "Z",
                    "status": "Completed" if ok else "Failed",
                    "serviceExceptionJson": None if ok else json.dumps({"errorCode": j["error"]["code"], "errorDescription": j["error"]["message"]})}
        if node.startswith("cognos."):
            return {"COGIPF_REPORTNAME": name.split(": ", 1)[1], "COGIPF_STATUS": "Success" if ok else "Failure",
                    "COGIPF_ERRORDETAILS": None if ok else j["error"]["message"], "COGIPF_LOCALTIMESTAMP": j["ended"].isoformat(),
                    "COGIPF_RUNTIME": int(j["elapsed"] * 60_000), "COGIPF_TARGET_TYPE": "ReportService"}
        return {"jobName": name.split("job ", 1)[1], "type": "VDPCache", "result": "OK" if ok else "ERROR",
                "lastExecution": j["ended"].isoformat(), "message": None if ok else j["error"]["message"]}

    def telemetry(self) -> list[tuple[str, Any]]:
        """Everything the monitoring tools reported since the last call."""
        out, self.outbox = self.outbox, []
        lines = ["Program     Status      Group       Lag at Chkpt  Time Since Chkpt", "MANAGER     RUNNING"]
        for name, p in self.ogg.items():
            since = (self.now - p["since"]).total_seconds() if p["status"] != "RUNNING" else 6
            lines.append(f"{'EXTRACT' if name != 'REP_SNF' else 'REPLICAT':<11} {p['status']:<11} {name:<11} {_hms(p['backlog_s'] or 4):<13} {_hms(since)}")
        out.append(("goldengate", "\n".join(lines)))
        out.append(("snowflake", {"resource_monitors": [{"name": w["monitor"], "credit_quota": w["quota"], "used_credits": w["used"],
                                                         "warehouse": n, "warehouse_state": w["state"]} for n, w in self.warehouses.items()]}))
        out.append(("oracle", {"dba_users": [{"USERNAME": k.split("/")[1], "ACCOUNT_STATUS": a["status"], "EXPIRES_IN_DAYS": a["expires_in_days"],
                                              "PROFILE": "DEFAULT"} for k, a in self.accounts.items() if k.startswith("ORAPRD_ERP")]}))
        return out

    # ------------------------------------------------------------------ probes (read-only)
    def probe(self, name: str, **a) -> dict:
        c = self.conditions
        if name == "goldengate.process_report":
            p = self.ogg[a["process"]]
            return {"summary": f"{a['process']} is {p['status']}" + (f": {p['error']['code']} {p['error']['message']}" if p["error"] else ""),
                    "facts": {"status": p["status"], "error_code": (p["error"] or {}).get("code"),
                              "minutes_since_checkpoint": round((self.now - p["since"]).total_seconds() / 60) if p["since"] else 0}}
        if name == "goldengate.lag":
            lag = round(self.replication_lag_s() / 60)
            return {"summary": f"End-to-end replication lag is {lag} min (limit {self.env.nodes['ogg.rep_snf']['max_lag_minutes']})",
                    "facts": {"lag_minutes": lag, "within_limit": self.ready["snow.raw.erp"]}}
        if name == "change.recent":
            ch = self.env.change_for(a["ci"], self.now)
            return {"summary": f"{ch['id']}: {ch['summary']} (window closed {ch['window_end']})" if ch else f"No recent approved change on {a['ci']}",
                    "facts": {"change_found": bool(ch), "change_id": (ch or {}).get("id")}}
        if name == "snowflake.column_diff":
            miss = self.target_columns_missing
            return {"summary": f"Source ERP.GL_JE_LINES has column(s) missing on RAW_ERP.GL_JE_LINES: {miss}" if miss else "Source and target columns match",
                    "facts": {"columns_missing_on_target": bool(miss), "columns": miss}}
        if name == "oracle.account_status":
            acc = self.accounts[f"ORAPRD_ERP/{a['account']}"]
            return {"summary": f"{a['account']} on ORAPRD_ERP is {acc['status']} (profile DEFAULT, password life 180 days)",
                    "facts": {"account_status": acc["status"], "expired": acc["status"] == "EXPIRED"}}
        if name == "informatica.session_log":
            j = self.jobs[a["node"]]
            msg = (j["error"] or {}).get("message", "no error")
            return {"summary": f"{self.env.nodes[a['node']]['name']} attempt {j['attempt']}: {j['state']}; rows {j['rows']}; {msg.splitlines()[0][:140]}",
                    "facts": {"state": j["state"], "rows": j["rows"], "typical_rows": TYPICAL_ROWS.get(a["node"]),
                              "rows_double_typical": bool(j["rows"]) and j["rows"] >= 1.9 * TYPICAL_ROWS.get(a["node"], 10**12),
                              "has_sql0911": "SQL0911N" in msg, "has_ora_28001": "ORA-28001" in msg}}
        if name == "db2.lock_events":
            return {"summary": "MON_GET_LOCKS history: lock timeouts 01:18-01:21 on POLICY.POLICY_TXN held by application db2reorg (online REORG, housekeeping job HK_REORG_POLICY started 01:10)",
                    "facts": {"blocker": "HK_REORG_POLICY", "blocker_is_housekeeping": True}}
        if name == "snowflake.resource_monitor":
            w = self.warehouses[a["warehouse"]]
            return {"summary": f"{w['monitor']}: {w['used']} of {w['quota']} credits used; {a['warehouse']} is {w['state']}. "
                               "Top consumer this month: ad-hoc user ANALYST_SANDBOX, 212 credits on 28 Feb (unfiltered join on fct_gl_balances)",
                    "facts": {"quota_exceeded": w["used"] >= w["quota"], "warehouse_state": w["state"], "top_consumer": "ANALYST_SANDBOX"}}
        if name == "dbt.failed_results":
            j = self.jobs["dbt.nightly_build"]
            bad = [r for r in j.get("results", []) if r["status"] not in ("success",)]
            return {"summary": "; ".join(f"{r['unique_id'].split('.')[-1]}: {r['status']} - {r['message'][:80]}" for r in bad) or "no failures",
                    "facts": {"unique_test_failed": any("unique_fct_claims" in r["unique_id"] for r in bad),
                              "stale_sources": [f["unique_id"].split("analytics.")[1] for f in j.get("freshness", [])],
                              "has_stale_source": bool(j.get("freshness"))}}
        if name == "mft.file_status":
            p = self._mft_payload()
            ratio = round(p["size_bytes"] / p["avg_size_bytes_7d"], 2) if p["size_bytes"] else None
            return {"summary": f"{p['expected_file']}: {p['status']}" + (f", {ratio}x the 7-day average size" if ratio else f", expected by {p['expected_by']}"),
                    "facts": {"status": p["status"], "missing": p["status"] == "MISSING", "size_ratio": ratio, "size_double": bool(ratio and ratio >= 1.8)}}
        if name == "dynatrace.host":
            h = self.hosts[a["host"]]
            top = "bkagent (backup agent, started 00:50, outside its 22:00-23:30 window)" if "host.cpu_saturation" in c and a["host"] == "infa-prd-01" else "pmdtm (Informatica session processes)"
            return {"summary": f"{a['host']}: {'available' if h['available'] else 'UNAVAILABLE'}, CPU {h['cpu']}%, top process {top}",
                    "facts": {"available": h["available"], "cpu": h["cpu"], "cpu_saturated": h["cpu"] >= 90,
                              "backup_agent_running": "host.cpu_saturation" in c and a["host"] == "infa-prd-01"}}
        if name == "azure.vm_status":
            up = self.hosts[a["host"]]["available"]
            return {"summary": f"Azure VM {a['host']}: {'running' if up else 'stopped (deallocated after a failed guest OS update at 04:38)'}",
                    "facts": {"vm_running": up, "vm_stopped": not up}}
        if name == "cognos.dispatcher_status":
            over = "cognos.dispatcher_overload" in c
            return {"summary": f"Report service: {'8 of 8 batch report processes busy, 41 requests queued, 3 processes hung since 05:20' if over else '2 of 8 processes busy, queue empty'}",
                    "facts": {"report_service_saturated": over}}
        if name == "denodo.datasource_pool":
            full = "denodo.pool_exhausted" in c
            return {"summary": f"ds_snowflake_fin pool: {'20 of 20 active, 14 idle-in-transaction older than 2 h (leaked by an ad-hoc export)' if full else '3 of 20 active'}",
                    "facts": {"pool_exhausted": full}}
        if name == "batch.job_state":
            j = self.jobs[a["node"]]
            return {"summary": f"{self.env.nodes[a['node']]['name']} is {j['state']} (attempt {j['attempt']})",
                    "facts": {"state": j["state"], "succeeded": j["state"] == "succeeded", "not_failed": j["state"] != "failed"}}
        if name == "snowflake.warehouse_state":
            w = self.warehouses[a["warehouse"]]
            return {"summary": f"{a['warehouse']} is {w['state']}", "facts": {"started": w["state"] == "STARTED"}}
        raise KeyError(f"unknown probe {name}")

    # ------------------------------------------------------------------ actions
    def rerun(self, node: str, minutes: Optional[int] = None) -> None:
        self._start(node, minutes)

    def apply(self, action: str, **a) -> dict:
        self.actions.append({"at": self.now.isoformat(), "action": action, **a})
        c = self.conditions
        if action == "goldengate.start_process":
            p = self.ogg[a["process"]]
            blocked = (a["process"] == "REP_SNF" and "ogg.ddl_mismatch" in c) or (a["process"] == "EXT_ERP" and "oracle.account_expired" in c)
            if blocked:
                p["since"] = p["since"] or self.now
                return {"ok": False, "message": f"{a['process']} abended again: {p['error']['code']} {p['error']['message']}"}
            if p["status"] != "RUNNING":
                p.update(status="RUNNING", backlog_s=(self.now - p["since"]).total_seconds(), since=None, error=None)
            return {"ok": True, "message": f"{a['process']} started; applying backlog"}
        if action == "snowflake.add_column_from_source":
            c.pop("ogg.ddl_mismatch", None)
            self.target_columns_missing = []
            return {"ok": True, "message": "ALTER TABLE RAW_ERP.GL_JE_LINES ADD COLUMN COST_CENTER_SEGMENT VARCHAR(30) applied"}
        if action == "oracle.unexpire_account":
            c.pop("oracle.account_expired", None)
            self.accounts[f"ORAPRD_ERP/{a['account']}"] = {"status": "OPEN", "expires_in_days": 180}
            return {"ok": True, "message": f"{a['account']} password rotated in the vault and account set to OPEN; credential aliases refreshed"}
        if action in ("informatica.restart_workflow", "informatica.reload_file_batch"):
            if action == "informatica.reload_file_batch":
                self.file["duplicate"] = False
                c.pop("mft.duplicate_file", None)
                if self.jobs[a["node"]]["state"] in ("scheduled", "waiting"):
                    return {"ok": True, "message": "Duplicate rows removed from the landed file before the load; the workflow will run as planned"}
                self.ready["snow.raw.claims"] = False
            self.rerun(a["node"])
            return {"ok": True, "message": f"{self.env.nodes[a['node']]['name']} restarted" + (" in recovery mode" if action.endswith("workflow") else " as truncate-and-reload for the business date")}
        if action == "dbt.retry_job":
            minutes = 20 if self.built else None
            if self.warehouses["WH_TRANSFORM"]["state"] == "SUSPENDED":
                self.rerun("dbt.nightly_build", minutes)
                return {"ok": False, "message": "dbt retry failed again: warehouse WH_TRANSFORM is suspended"}
            self.rerun("dbt.nightly_build", minutes)
            return {"ok": True, "message": "dbt retry started for the models not yet built"}
        if action == "snowflake.raise_quota":
            w = self.warehouses[a["warehouse"]]
            w.update(quota=w["quota"] + int(a.get("add_credits", 100)), state="STARTED")
            c.pop("snowflake.rm_quota", None)
            return {"ok": True, "message": f"{w['monitor']} quota raised to {w['quota']} credits; {a['warehouse']} resumed"}
        if action == "azure.restart_vm":
            self.hosts[a["host"]]["available"] = True
            c.pop("azure.vm_down", None)
            self._resolve_problem(a["host"], "Host or monitoring unavailable")
            return {"ok": True, "message": f"VM {a['host']} started; gateway service online"}
        if action == "infra.stop_process":
            self.hosts[a["host"]]["cpu"] = 34
            c.pop("host.cpu_saturation", None)
            self._resolve_problem(a["host"], "CPU saturation")
            return {"ok": True, "message": f"{a['process']} stopped on {a['host']}; CPU back to 34%"}
        if action == "cognos.restart_report_service":
            c.pop("cognos.dispatcher_overload", None)
            return {"ok": True, "message": "Report service restarted; 8 of 8 processes available"}
        if action in ("powerbi.refresh_dataset", "cognos.rerun_schedule", "denodo.reload_cache"):
            self.rerun(a["node"])
            return {"ok": True, "message": f"{self.env.nodes[a['node']]['name']} started"}
        if action == "snowflake.grant_role":
            self.grants.append({"user": a["user"], "role": a["role"], "at": self.now.isoformat()})
            return {"ok": True, "message": f"GRANT ROLE {a['role']} TO USER {a['user']} applied"}
        if action == "manual.fix":      # a person fixed something by hand; the simulator clears that node's fault
            for cond in MANUAL_CLEARS.get(a.get("node", ""), []):
                c.pop(cond, None)
            return {"ok": True, "message": "Recorded"}
        raise KeyError(f"unknown action {action}")

    # ------------------------------------------------------------------ scheduler view
    def batch_status(self) -> dict:
        """What the schedulers show now: job states and progress, dataset readiness."""
        jobs = {}
        for node, j in self.jobs.items():
            jobs[node] = {"state": j["state"], "planned": j["planned"], "started": j["started"], "ended": j["ended"],
                          "attempt": j["attempt"], "duration": j["duration"], "run_minutes": j["run_minutes"],
                          "pct": round(min(j["elapsed"] / j["run_minutes"], 1.0), 3) if j["state"] == "running" else None,
                          "error": (j["error"] or {}).get("message", "")[:200] if j["state"] == "failed" else ""}
        return {"now": self.now, "jobs": jobs, "ready": dict(self.ready), "built": sorted(self.built),
                "lag_minutes": round(self.replication_lag_s() / 60, 1),
                "file_received": self.file["received_at"]}
