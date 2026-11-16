"""Connectors: turn what each tool reports, in its own format, into one normalised alert.

A normalised alert has: source tool, CI, lineage node (when the tool names one), an entity
and check (together the fingerprint used for de-duplication and clearing), severity, the
error codes found, a short message and a few facts. Message text comes from client systems
and is treated as untrusted: it is stored and shown, never interpreted as instructions.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..env import Environment

CODE_RE = re.compile(r"\b(ORA-\d{5}|OGG-\d{5}|SQL\d{4}N|[A-Z]{2,4}_\d{4,5}|[A-Z]{2,3}-[A-Z]{3}-\d{4}|AADSTS\d{5,7}|000\d{3})\b")
ORDER = ["servicenow_change", "oracle", "oracle_alert", "db2", "dynatrace", "aws", "azure", "snowflake", "mft", "goldengate_log", "goldengate", "informatica", "dbt", "denodo", "powerbi", "cognos"]


def codes_in(text: str) -> list[str]:
    seen: list[str] = []
    for c in CODE_RE.findall(text or ""):
        if c not in seen:
            seen.append(c)
    return seen


def _alert(source, ci, entity, check, severity, message="", codes=(), node=None, facts=None, clear=False) -> dict:
    return {"source": source, "ci": ci, "node": node, "entity": entity, "check": check, "severity": "clear" if clear else severity,
            "codes": list(codes), "message": str(message)[:600], "facts": facts or {}, "fingerprint": f"{ci}:{entity}:{check}"}


def _node_by_name(env: Environment, fragment: str) -> str | None:
    return next((n["id"] for n in env.nodes.values() if fragment and fragment in n["name"]), None)


def _secs(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


# ---------------------------------------------------------------------------- per tool
def goldengate_log(env, line: str) -> list[dict]:
    m = re.match(r"\S+\s+(ERROR|WARNING)\s+(OGG-\d{5})\s+.*?,\s*(\w+)\.prm:\s+(.*)$", line)
    if not m or m.group(2) == "OGG-01668":     # OGG-01668 only says "process abending"
        return []
    proc = m.group(3).upper()
    return [_alert("goldengate", "OGG_ERP", proc, "abend", "critical", f"{proc}: {m.group(2)} {m.group(4)}",
                   codes_in(line), node="ogg.rep_snf", facts={"process": proc})]


def goldengate(env, text: str) -> list[dict]:
    out, worst = [], 0
    limit = env.nodes["ogg.rep_snf"]["max_lag_minutes"] * 60
    for line in text.splitlines()[2:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        _, status, group, lag, since = parts[:5]
        if status != "RUNNING":
            out.append(_alert("goldengate", "OGG_ERP", group, "abend", "critical", f"{group} is {status}", node="ogg.rep_snf",
                              facts={"process": group, "status": status}))
            worst = max(worst, _secs(since))
        else:
            out.append(_alert("goldengate", "OGG_ERP", group, "abend", "critical", node="ogg.rep_snf", clear=True))
            worst = max(worst, _secs(lag))
    if worst > limit:
        out.append(_alert("goldengate", "OGG_ERP", "replication", "lag", "major", f"Replication lag {worst // 60} min (limit {limit // 60})",
                          ["OGG_LAG"], node="ogg.rep_snf", facts={"lag_minutes": worst // 60}))
    else:
        out.append(_alert("goldengate", "OGG_ERP", "replication", "lag", "major", node="ogg.rep_snf", clear=True))
    return out


def informatica(env, row: dict) -> list[dict]:
    node = _node_by_name(env, row["WORKFLOW_NAME"])
    if row["RUN_STATUS_CODE"] == 3:
        msg = row.get("RUN_ERR_MSG", "")
        return [_alert("informatica", "INFA_PRD", row["WORKFLOW_NAME"], "run", "critical", f"{row['WORKFLOW_NAME']} failed: {' '.join(msg.split())}",
                       codes_in(msg), node=node)]
    return [_alert("informatica", "INFA_PRD", row["WORKFLOW_NAME"], "run", "critical", node=node, clear=True)]


def dbt(env, run: dict) -> list[dict]:
    out = []
    marts = {n["model"]: n["id"] for n in env.nodes.values() if n.get("model")}
    bad: dict[str, dict] = {}
    for r in run.get("results", []):
        name = r["unique_id"].split(".")[-1]
        if r["status"] in ("success", "pass"):
            continue
        model = next((m for m in marts if m in name), None)
        if not model:
            continue
        b = bad.setdefault(model, {"codes": [], "msgs": []})
        if r["status"] == "fail":
            b["codes"] += ["DBT_TEST_FAIL", f"DBT_TEST_FAIL:{name}"]
            b["msgs"].append(f"test {name} failed ({r.get('failures')} rows)")
        elif "resource monitor" in r.get("message", ""):
            b["codes"].append("SNOWFLAKE_RM_QUOTA")
            b["msgs"].append(" ".join(r["message"].split()))
        elif "stale" in r.get("message", "").lower():
            b["codes"].append("DBT_STALE_SOURCE")
            stale = [f["unique_id"].split("analytics.")[1] for f in run.get("freshness", [])]
            b["msgs"].append(f"skipped: source not fresh ({', '.join(stale)})")
        else:
            b["codes"] += codes_in(r.get("message", "")) or ["DBT_MODEL_ERROR"]
            b["msgs"].append(" ".join(r.get("message", "").split()))
    for model, node in marts.items():
        if model in bad:
            out.append(_alert("dbt", "DBT_CLOUD", model, "build", "critical", f"dbt nightly_build: {model} not built - {'; '.join(bad[model]['msgs'])}",
                              bad[model]["codes"], node=node, facts={"run_id": run.get("run_id")}))
        elif any(model in r["unique_id"] for r in run.get("results", [])):
            out.append(_alert("dbt", "DBT_CLOUD", model, "build", "critical", node=node, clear=True))
    return out


def powerbi(env, row: dict) -> list[dict]:
    node = _node_by_name(env, row["datasetName"])
    if row["status"] == "Failed":
        err = json.loads(row.get("serviceExceptionJson") or "{}")
        return [_alert("powerbi", "PBI_SERVICE", row["datasetName"], "refresh", "critical",
                       f"Refresh of {row['datasetName']} failed: {err.get('errorDescription', '')}",
                       [err["errorCode"]] if err.get("errorCode") else [], node=node)]
    return [_alert("powerbi", "PBI_SERVICE", row["datasetName"], "refresh", "critical", node=node, clear=True)]


def cognos(env, row: dict) -> list[dict]:
    node = _node_by_name(env, row["COGIPF_REPORTNAME"])
    if row["COGIPF_STATUS"] == "Failure":
        d = row.get("COGIPF_ERRORDETAILS") or ""
        return [_alert("cognos", "COGNOS_PRD", row["COGIPF_REPORTNAME"], "run", "critical", f"{row['COGIPF_REPORTNAME']} failed: {d}", codes_in(d), node=node)]
    return [_alert("cognos", "COGNOS_PRD", row["COGIPF_REPORTNAME"], "run", "critical", node=node, clear=True)]


def denodo(env, row: dict) -> list[dict]:
    node = _node_by_name(env, row["jobName"])
    if row["result"] == "ERROR":
        return [_alert("denodo", "DENODO_PRD", row["jobName"], "job", "critical", f"Scheduler job {row['jobName']} failed: {row.get('message') or ''}",
                       codes_in(row.get("message") or ""), node=node)]
    return [_alert("denodo", "DENODO_PRD", row["jobName"], "job", "critical", node=node, clear=True)]


def dynatrace(env, p: dict) -> list[dict]:
    out = []
    key = {"Host or monitoring unavailable": "DT_HOST_UNAVAILABLE", "CPU saturation": "DT_CPU_SATURATION"}.get(p["ProblemTitle"], "DT_PROBLEM")
    for ent in p.get("ImpactedEntities", []):
        host = ent["name"]
        ci = next((c["id"] for c in env.cis.values() if c.get("host") == host), "DYNATRACE")
        out.append(_alert("dynatrace", ci, host, key, "critical" if key == "DT_HOST_UNAVAILABLE" else "major",
                          f"Dynatrace problem {p['ProblemID']}: {p['ProblemTitle']} on {host}", [key, f"{key}:{host}"],
                          facts={"host": host, "problem_id": p["ProblemID"]}, clear=p["State"] == "RESOLVED"))
    return out


def mft(env, f: dict) -> list[dict]:
    node = "mft.claims_file"
    if f["status"] == "MISSING":
        return [_alert("mft", "MFT_GATEWAY", f["feed"], "arrival", "major", f"{f['expected_file']} not received by {f['expected_by']} from {f['partner']}",
                       ["MFT_FILE_MISSING"], node=node)]
    out = [_alert("mft", "MFT_GATEWAY", f["feed"], "arrival", "major", node=node, clear=True)]
    ratio = f["size_bytes"] / f["avg_size_bytes_7d"]
    if ratio >= 1.8 or ratio <= 0.4:
        out.append(_alert("mft", "MFT_GATEWAY", f["feed"], "size", "warning", f"{f['expected_file']} is {ratio:.1f}x its 7-day average size",
                          ["MFT_SIZE_ANOMALY"], node=node, facts={"size_ratio": round(ratio, 2)}))
    return out


def snowflake(env, snap: dict) -> list[dict]:
    out = []
    for m in snap.get("resource_monitors", []):
        node = next((n["id"] for n in env.nodes.values() if n.get("warehouse") == m["warehouse"]), None)
        pct = m["used_credits"] / m["credit_quota"]
        if pct >= 1 or m["warehouse_state"] == "SUSPENDED":
            out.append(_alert("snowflake", "SNOWFLAKE", m["warehouse"], "quota", "critical",
                              f"Resource monitor {m['name']} is at {pct:.0%} of quota; warehouse {m['warehouse']} is {m['warehouse_state']}",
                              ["SNOWFLAKE_RM_QUOTA"], node=node, facts={"warehouse": m["warehouse"]}))
        else:
            out.append(_alert("snowflake", "SNOWFLAKE", m["warehouse"], "quota", "critical", node=node, clear=True))
    return out


def oracle(env, snap: dict) -> list[dict]:
    out = []
    for u in snap.get("dba_users", []):
        if u["ACCOUNT_STATUS"] != "OPEN":
            out.append(_alert("oracle", "ORAPRD_ERP", u["USERNAME"], "account", "critical", f"Account {u['USERNAME']} is {u['ACCOUNT_STATUS']}",
                              ["ORA-28001"], node="ora.erp", facts={"account": u["USERNAME"]}))
        else:
            out.append(_alert("oracle", "ORAPRD_ERP", u["USERNAME"], "account", "critical", node="ora.erp", clear=True))
            if u["EXPIRES_IN_DAYS"] <= 14:
                out.append(_alert("oracle", "ORAPRD_ERP", u["USERNAME"], "expiry", "warning",
                                  f"Password for {u['USERNAME']} expires in {u['EXPIRES_IN_DAYS']} days", ["ORA_PWD_EXPIRING"],
                                  facts={"account": u["USERNAME"], "days": u["EXPIRES_IN_DAYS"]}))
            else:
                out.append(_alert("oracle", "ORAPRD_ERP", u["USERNAME"], "expiry", "warning", clear=True))
    return out


def oracle_alert(env, line: str) -> list[dict]:
    codes = codes_in(line)
    if not codes:
        return []
    return [_alert("oracle", "ORAPRD_ERP", codes[0], "alert_log", "critical", " ".join(line.split()[1:]) or line, codes, node="ora.erp")]


def db2(env, rec: dict) -> list[dict]:
    msg = str(rec.get("message", ""))
    codes = codes_in(msg)
    if str(rec.get("level", "")).lower() not in ("error", "severe", "critical") or not codes:
        return []
    return [_alert("db2", "DB2PRD_POL", codes[0], "db2diag", "critical", " ".join(msg.split()), codes, node="db2.policy")]


def _host_alert(env, source: str, host: str, title: str, firing: bool, detail: str) -> list[dict]:
    ci = next((c["id"] for c in env.cis.values() if c.get("host") == host), None)
    if not ci:
        return []
    key = "HOST_ALARM"
    return [_alert(source, ci, host, key, "critical", f"{title} on {host}: {detail}", [key, f"{key}:{host}"], facts={"host": host}, clear=not firing)]


def aws(env, alarm: dict) -> list[dict]:
    dims = (alarm.get("Trigger") or {}).get("Dimensions") or []
    host = next((d.get("value") for d in dims if d.get("value")), "")
    return _host_alert(env, "aws", host, f"CloudWatch alarm {alarm.get('AlarmName', '')}", alarm.get("NewStateValue") == "ALARM", alarm.get("NewStateReason", ""))


def azure(env, alert: dict) -> list[dict]:
    e = (alert.get("data") or {}).get("essentials") or {}
    host = (e.get("configurationItems") or [""])[0]
    return _host_alert(env, "azure", host, f"Azure Monitor alert {e.get('alertRule', '')}", e.get("monitorCondition") == "Fired", e.get("description", ""))


def servicenow_change(env, chg: dict) -> list[dict]:
    """An approved change joins the change calendar; alerts on that system inside the window are then suppressed."""
    if chg.get("cmdb_ci") in env.cis and not any(c["id"] == chg.get("number") for c in env.data["changes"]):
        env.data["changes"].append({"id": chg["number"], "ci": chg["cmdb_ci"], "summary": chg.get("short_description", ""),
                                    "window_start": chg["start_date"], "window_end": chg["end_date"], "requested_by": chg.get("requested_by", "")})
    return []


PARSERS = {"oracle_alert": oracle_alert, "db2": db2, "aws": aws, "azure": azure, "servicenow_change": servicenow_change,
           "goldengate_log": goldengate_log, "goldengate": goldengate, "informatica": informatica, "dbt": dbt, "powerbi": powerbi,
           "cognos": cognos, "denodo": denodo, "dynatrace": dynatrace, "mft": mft, "snowflake": snowflake, "oracle": oracle}


def parse(env: Environment, telemetry: list[tuple[str, Any]]) -> list[dict]:
    """Parse a batch of raw tool output. Causes (databases, hosts, feeds) sort before the
    tools that only show the symptom, so correlation sees the cause first."""
    alerts: list[dict] = []
    for source, payload in sorted(telemetry, key=lambda t: ORDER.index(t[0]) if t[0] in ORDER else 99):
        fn = PARSERS.get(source)
        if fn:
            alerts.extend(fn(env, payload))
    return alerts
