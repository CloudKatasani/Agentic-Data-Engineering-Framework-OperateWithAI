"""Per-tool telemetry translators for the tools in Build-With-AI's preset Stack Profiles.

Each translator has two halves:

* ``parse_*`` turns the tool's native payload (the shape its API, system table or result
  file returns) into Operate's canonical, metadata-only reading. This is adapter code.
* ``emit_*`` produces that native payload from canonical state. It is used only by
  :class:`~adef_operate.stack.native.NativePlatform` to emulate the platform offline.

Payload shapes follow each tool's public interface (job/run APIs, information-schema or
system views, result files). Field names are the documented ones; values are synthetic.
Prices are illustrative defaults and must be set per contract in the profile
(``variables.prices``).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

ISO = lambda d: d.isoformat().replace("+00:00", "Z")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _minutes(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 60, 1)


# ---------------------------------------------------------------------------------------------
# Logical type maps (adapter contract section 2). Canonical: string, timestamp, decimal,
# integer, date, boolean.
TYPES: dict[str, dict[str, str]] = {
    "snowflake": {"string": "VARCHAR", "timestamp": "TIMESTAMP_NTZ", "decimal": "NUMBER(18,3)",
                  "integer": "NUMBER(38,0)", "date": "DATE", "boolean": "BOOLEAN"},
    "spark": {"string": "string", "timestamp": "timestamp", "decimal": "decimal(18,3)", "integer": "bigint",
              "date": "date", "boolean": "boolean"},
    "iceberg": {"string": "string", "timestamp": "timestamptz", "decimal": "decimal(18,3)", "integer": "long",
                "date": "date", "boolean": "boolean"},
    "bigquery": {"string": "STRING", "timestamp": "TIMESTAMP", "decimal": "NUMERIC", "integer": "INT64",
                 "date": "DATE", "boolean": "BOOL"},
    "redshift": {"string": "character varying", "timestamp": "timestamp without time zone",
                 "decimal": "numeric(18,3)", "integer": "bigint", "date": "date", "boolean": "boolean"},
    "tsql": {"string": "varchar", "timestamp": "datetime2", "decimal": "decimal(18,3)", "integer": "bigint",
             "date": "date", "boolean": "bit"},
    "duckdb": {"string": "VARCHAR", "timestamp": "TIMESTAMP", "decimal": "DECIMAL(18,3)", "integer": "BIGINT",
               "date": "DATE", "boolean": "BOOLEAN"},
}


def to_native(family: str, cols: dict[str, str]) -> dict[str, str]:
    return {c: TYPES[family].get(t, t) for c, t in cols.items()}


def to_logical(family: str, cols: dict[str, str]) -> dict[str, str]:
    inv = {v.lower(): k for k, v in TYPES[family].items()}
    out = {}
    for c, t in cols.items():
        tl = t.lower()
        if tl in inv:
            out[c.lower()] = inv[tl]
        elif tl.startswith(("number", "numeric", "decimal")):
            out[c.lower()] = "integer" if re.search(r",\s*0\)", tl) else "decimal"
        else:
            out[c.lower()] = tl
    return out


# ---------------------------------------------------------------------------------------------
# Orchestration: run state and duration. Canonical status: success | failed | waiting.
class Orchestrator:
    name = "base"
    COMMANDS: dict[str, str] = {}

    def emit_run(self, pipeline: str, run: dict, now: datetime) -> dict: ...
    def parse_run(self, p: dict) -> dict: ...

    def command(self, action_type: str, pipeline: str) -> str:
        return self.COMMANDS.get(action_type, f"{self.name}: {action_type} via the platform UI (no API mapped)") \
            .format(p=pipeline)


class Airflow(Orchestrator):
    name = "airflow"
    STATE = {"success": "success", "failed": "failed", "waiting": "queued"}
    COMMANDS = {"retry_failed_task": "POST /api/v1/dags/{p}/clearTaskInstances only_failed=true",
                "rerun_latest_window": "POST /api/v1/dags/{p}/clearTaskInstances for the latest logical_date",
                "rerun_past_window": "POST /api/v1/dags/{p}/clearTaskInstances for a past logical_date",
                "pause_pipeline": "PATCH /api/v1/dags/{p} is_paused=true"}

    def emit_run(self, pipeline, run, now):
        start = now - timedelta(minutes=run["duration_min"])
        return {"dag_id": pipeline, "dag_run_id": f"scheduled__{run['window']}", "state": self.STATE[run["status"]],
                "start_date": ISO(start), "end_date": None if run["status"] == "waiting" else ISO(now)}

    def parse_run(self, p):
        state = {"success": "success", "failed": "failed", "upstream_failed": "failed"}.get(p["state"], "waiting")
        dur = _minutes(_dt(p["start_date"]), _dt(p["end_date"])) if p.get("end_date") else 0.0
        return {"status": state, "duration_min": dur}


class DatabricksWorkflows(Orchestrator):
    name = "platform-workflows"
    COMMANDS = {"retry_failed_task": "POST /api/2.1/jobs/runs/repair rerun_all_failed_tasks=true",
                "rerun_latest_window": "POST /api/2.1/jobs/run-now job_parameters.window=<latest>",
                "rerun_past_window": "POST /api/2.1/jobs/run-now job_parameters.window=<past>",
                "pause_pipeline": "POST /api/2.1/jobs/update schedule.pause_status=PAUSED"}

    def emit_run(self, pipeline, run, now):
        life, result = {"success": ("TERMINATED", "SUCCESS"), "failed": ("TERMINATED", "FAILED"),
                        "waiting": ("PENDING", None)}[run["status"]]
        st = {"life_cycle_state": life}
        if result:
            st["result_state"] = result
        return {"run_name": pipeline, "state": st, "execution_duration": int(run["duration_min"] * 60_000)}

    def parse_run(self, p):
        st = p["state"]
        if st.get("life_cycle_state") in ("PENDING", "QUEUED", "RUNNING", "BLOCKED"):
            status = "waiting"
        else:
            status = "success" if st.get("result_state") == "SUCCESS" else "failed"
        return {"status": status, "duration_min": round(p.get("execution_duration", 0) / 60_000, 1)}


class DataFactory(Orchestrator):
    name = "azure-data-factory"
    STATE = {"success": "Succeeded", "failed": "Failed", "waiting": "Queued"}
    COMMANDS = {"retry_failed_task": "pipelines/createRun isRecovery=true referencePipelineRunId=<failed run>",
                "rerun_latest_window": "pipelines/createRun parameters.window=<latest>",
                "rerun_past_window": "pipelines/createRun parameters.window=<past>",
                "pause_pipeline": "triggers/stop for the pipeline's schedule trigger"}

    def emit_run(self, pipeline, run, now):
        return {"pipelineName": pipeline, "status": self.STATE[run["status"]],
                "durationInMs": int(run["duration_min"] * 60_000), "runEnd": ISO(now)}

    def parse_run(self, p):
        status = {"Succeeded": "success", "Failed": "failed", "Cancelled": "failed"}.get(p["status"], "waiting")
        return {"status": status, "duration_min": round(p.get("durationInMs", 0) / 60_000, 1)}


class SnowflakeTasks(Orchestrator):
    name = "snowflake-tasks"
    STATE = {"success": "SUCCEEDED", "failed": "FAILED", "waiting": "SCHEDULED"}
    COMMANDS = {"retry_failed_task": "EXECUTE TASK {p} RETRY LAST", "rerun_latest_window": "EXECUTE TASK {p}",
                "pause_pipeline": "ALTER TASK {p} SUSPEND"}

    def emit_run(self, pipeline, run, now):
        return {"NAME": pipeline, "STATE": self.STATE[run["status"]],
                "QUERY_START_TIME": ISO(now - timedelta(minutes=run["duration_min"])), "COMPLETED_TIME": ISO(now)}

    def parse_run(self, p):
        status = {"SUCCEEDED": "success", "FAILED": "failed", "FAILED_AND_AUTO_SUSPENDED": "failed"}.get(p["STATE"], "waiting")
        return {"status": status, "duration_min": _minutes(_dt(p["QUERY_START_TIME"]), _dt(p["COMPLETED_TIME"]))}


class Dagster(Orchestrator):
    name = "dagster"
    STATE = {"success": "SUCCESS", "failed": "FAILURE", "waiting": "QUEUED"}
    COMMANDS = {"retry_failed_task": "launchPipelineReexecution strategy=FROM_FAILURE",
                "rerun_latest_window": "launchPartitionBackfill partition=<latest>",
                "pause_pipeline": "stopRunningSchedule"}

    def emit_run(self, pipeline, run, now):
        return {"jobName": pipeline, "status": self.STATE[run["status"]],
                "startTime": (now - timedelta(minutes=run["duration_min"])).timestamp(), "endTime": now.timestamp()}

    def parse_run(self, p):
        status = {"SUCCESS": "success", "FAILURE": "failed", "CANCELED": "failed"}.get(p["status"], "waiting")
        return {"status": status, "duration_min": round((p["endTime"] - p["startTime"]) / 60, 1)}


class AdefRunner(Orchestrator):
    name = "adef-runner"
    COMMANDS = {"retry_failed_task": "adef runner retry {p}", "rerun_latest_window": "adef runner run {p} --latest",
                "pause_pipeline": "adef runner pause {p}"}

    def emit_run(self, pipeline, run, now):
        return {"pipeline": pipeline, "status": run["status"], "duration_min": run["duration_min"]}

    def parse_run(self, p):
        return {"status": p["status"], "duration_min": p["duration_min"]}


ORCHESTRATION: dict[str, Orchestrator] = {o.name: o for o in
                                          (Airflow(), DatabricksWorkflows(), DataFactory(), SnowflakeTasks(), Dagster(),
                                           AdefRunner())}


# ---------------------------------------------------------------------------------------------
# Table telemetry. Canonical: lag_minutes, rows (latest window), columns (logical types),
# and trailing same-weekday row counts for the volume band.
class TableReader:
    name = "base"
    family = "spark"

    def emit_table(self, physical: str, obj: dict, now: datetime) -> dict: ...
    def parse_table(self, p: dict, now: datetime) -> dict: ...

    @staticmethod
    def expected_rows(trailing: list[int]) -> int:
        return int(sum(trailing) / len(trailing)) if trailing else 0


class Delta(TableReader):
    """DESCRIBE HISTORY (latest commit, operationMetrics are strings) and the table schema."""
    name, family = "delta", "spark"

    def emit_table(self, physical, obj, now):
        ts = now - timedelta(minutes=obj["lag_minutes"])
        return {"table": physical,
                "history": [{"version": 42, "timestamp": ISO(ts), "operation": "MERGE",
                             "operationMetrics": {"numOutputRows": str(obj["rows"])}}],
                "trailing_weekday_rows": [str(obj["baseline"])] * 4,
                "schema": {"type": "struct", "fields": [{"name": c, "type": t, "nullable": True}
                                                        for c, t in to_native(self.family, obj["columns"]).items()]}}

    def parse_table(self, p, now):
        last = max(p["history"], key=lambda h: h["version"])
        return {"lag_minutes": round(_minutes(_dt(last["timestamp"]), now)),
                "rows": int(last["operationMetrics"]["numOutputRows"]),
                "expected_rows": self.expected_rows([int(x) for x in p["trailing_weekday_rows"]]),
                "columns": to_logical(self.family, {f["name"]: f["type"] for f in p["schema"]["fields"]})}


class Iceberg(TableReader):
    """``<table>.snapshots`` metadata table (summary map) and the current schema."""
    name, family = "iceberg", "iceberg"

    def emit_table(self, physical, obj, now):
        ts = now - timedelta(minutes=obj["lag_minutes"])
        return {"table": physical,
                "snapshots": [{"snapshot_id": 8812, "committed_at": int(ts.timestamp() * 1000), "operation": "overwrite",
                               "summary": {"added-records": str(obj["rows"]), "total-records": str(obj["rows"] * 30)}}],
                "trailing_weekday_rows": [obj["baseline"]] * 4,
                "schema": {"fields": [{"id": i, "name": c, "type": t, "required": False}
                                      for i, (c, t) in enumerate(to_native(self.family, obj["columns"]).items(), 1)]}}

    def parse_table(self, p, now):
        last = max(p["snapshots"], key=lambda s: s["committed_at"])
        ts = datetime.fromtimestamp(last["committed_at"] / 1000, tz=timezone.utc)
        return {"lag_minutes": round(_minutes(ts, now)), "rows": int(last["summary"]["added-records"]),
                "expected_rows": self.expected_rows([int(x) for x in p["trailing_weekday_rows"]]),
                "columns": to_logical(self.family, {f["name"]: f["type"] for f in p["schema"]["fields"]})}


class Hudi(Delta):
    name = "hudi"


TABLE_FORMATS: dict[str, TableReader] = {t.name: t for t in (Delta(), Iceberg(), Hudi())}


# ---------------------------------------------------------------------------------------------
# Serving engines (gold layer): tables, cost, query profile, grants, masking, access history.
class Engine(TableReader):
    unit = "currency"
    price_key = ""
    default_price = 1.0
    PRIV = {"SELECT": "SELECT", "ALL": "ALL"}

    def price(self, prices: dict) -> float:
        return float(prices.get(self.price_key, self.default_price)) if self.price_key else 1.0

    # cost
    def emit_cost(self, daily: float, prices: dict) -> dict:
        return {"usage": round(daily / self.price(prices), 6), "unit": self.unit}

    def parse_cost(self, p: dict, prices: dict) -> float:
        return round(p["usage"] * self.price(prices), 2)

    # query profile: canonical full_scans and top_object (logical)
    def emit_query_profile(self, full_scans: int, top_physical: str | None) -> dict:
        return {"queries": [{"table": top_physical, "scanned": 100, "total": 100}] if full_scans else []}

    def parse_query_profile(self, p: dict) -> dict:
        full = [q for q in p["queries"] if q["scanned"] >= q["total"]]
        return {"full_scans": len(full), "top_physical": full[0]["table"] if full else None}

    # grants: canonical tuples (logical role, privilege, physical object)
    def emit_grants(self, grants: list[tuple[str, str, str]]) -> list[dict]:
        return [{"grantee": r, "privilege": self.PRIV.get(pv, pv), "object": o} for r, pv, o in grants]

    def parse_grants(self, rows: list[dict]) -> list[tuple[str, str, str]]:
        inv = {v: k for k, v in self.PRIV.items()}
        return [(r["grantee"], inv.get(r["privilege"], r["privilege"]), r["object"]) for r in rows]

    # masking: canonical {physical_table.column: policy}
    def emit_masking(self, masks: dict[str, str]) -> list[dict]:
        return [{"column": k, "policy": v} for k, v in masks.items()]

    def parse_masking(self, rows: list[dict]) -> dict[str, str]:
        return {r["column"]: r["policy"] for r in rows}


class Snowflake(Engine):
    """INFORMATION_SCHEMA.TABLES/COLUMNS, WAREHOUSE_METERING_HISTORY (credits), QUERY_HISTORY
    (PARTITIONS_SCANNED/PARTITIONS_TOTAL), GRANTS_TO_ROLES, POLICY_REFERENCES."""
    name, family, unit, price_key, default_price = "snowflake", "snowflake", "credits", "snowflake_credit", 3.0

    def emit_table(self, physical, obj, now):
        db, schema, table = physical.split(".")
        return {"TABLE_CATALOG": db, "TABLE_SCHEMA": schema, "TABLE_NAME": table,
                "LAST_ALTERED": ISO(now - timedelta(minutes=obj["lag_minutes"])), "WINDOW_ROW_COUNT": obj["rows"],
                "TRAILING_WEEKDAY_ROW_COUNTS": [obj["baseline"]] * 4,
                "COLUMNS": [{"COLUMN_NAME": c.upper(), "DATA_TYPE": t} for c, t in
                            to_native(self.family, obj["columns"]).items()]}

    def parse_table(self, p, now):
        return {"lag_minutes": round(_minutes(_dt(p["LAST_ALTERED"]), now)), "rows": int(p["WINDOW_ROW_COUNT"]),
                "expected_rows": self.expected_rows(p["TRAILING_WEEKDAY_ROW_COUNTS"]),
                "columns": to_logical(self.family, {c["COLUMN_NAME"]: c["DATA_TYPE"] for c in p["COLUMNS"]})}

    def emit_cost(self, daily, prices):
        return {"WAREHOUSE_METERING_HISTORY": [{"WAREHOUSE_NAME": "OPS_WH",
                                                "CREDITS_USED": round(daily / self.price(prices), 6)}]}

    def parse_cost(self, p, prices):
        return round(sum(r["CREDITS_USED"] for r in p["WAREHOUSE_METERING_HISTORY"]) * self.price(prices), 2)

    def emit_query_profile(self, full_scans, top_physical):
        return {"QUERY_HISTORY": [{"QUERY_ID": "01b2", "PARTITIONS_SCANNED": 1200, "PARTITIONS_TOTAL": 1200,
                                   "OBJECT": top_physical}] if full_scans else []}

    def parse_query_profile(self, p):
        full = [q for q in p["QUERY_HISTORY"] if q["PARTITIONS_SCANNED"] >= q["PARTITIONS_TOTAL"]]
        return {"full_scans": len(full), "top_physical": full[0]["OBJECT"] if full else None}

    def emit_grants(self, grants):
        return [{"PRIVILEGE": pv if pv != "ALL" else "OWNERSHIP", "GRANTED_ON": "TABLE", "NAME": o,
                 "GRANTEE_NAME": r} for r, pv, o in grants]

    def parse_grants(self, rows):
        return [(r["GRANTEE_NAME"], "ALL" if r["PRIVILEGE"] == "OWNERSHIP" else r["PRIVILEGE"], r["NAME"]) for r in rows]

    def emit_masking(self, masks):
        return [{"POLICY_NAME": v, "REF_ENTITY_NAME": k.rsplit(".", 1)[0], "REF_COLUMN_NAME": k.rsplit(".", 1)[1].upper()}
                for k, v in masks.items()]

    def parse_masking(self, rows):
        return {f"{r['REF_ENTITY_NAME']}.{r['REF_COLUMN_NAME'].lower()}": r["POLICY_NAME"] for r in rows}


class Databricks(Engine):
    """Gold tables are Delta (DESCRIBE HISTORY); system.billing.usage (DBUs); Unity Catalog
    information_schema.table_privileges and column masks."""
    name, family, unit, price_key, default_price = "databricks", "spark", "DBU", "databricks_dbu", 0.55
    PRIV = {"SELECT": "SELECT", "ALL": "ALL PRIVILEGES"}

    def emit_table(self, physical, obj, now):
        return Delta().emit_table(physical, obj, now)

    def parse_table(self, p, now):
        return Delta().parse_table(p, now)

    def emit_cost(self, daily, prices):
        return {"system.billing.usage": [{"sku_name": "PREMIUM_JOBS_COMPUTE", "usage_unit": "DBU",
                                          "usage_quantity": round(daily / self.price(prices), 6)}]}

    def parse_cost(self, p, prices):
        return round(sum(r["usage_quantity"] for r in p["system.billing.usage"]) * self.price(prices), 2)

    def emit_query_profile(self, full_scans, top_physical):
        return {"query_history": [{"statement_id": "q1", "read_files": 900, "pruned_files": 0, "table": top_physical}]
                if full_scans else []}

    def parse_query_profile(self, p):
        full = [q for q in p["query_history"] if q["pruned_files"] == 0 and q["read_files"] > 0]
        return {"full_scans": len(full), "top_physical": full[0]["table"] if full else None}

    def emit_grants(self, grants):
        out = []
        for r, pv, o in grants:
            cat, sch, tbl = o.split(".")
            out.append({"grantee": r, "privilege_type": self.PRIV.get(pv, pv), "table_catalog": cat,
                        "table_schema": sch, "table_name": tbl})
        return out

    def parse_grants(self, rows):
        inv = {v: k for k, v in self.PRIV.items()}
        return [(r["grantee"], inv.get(r["privilege_type"], r["privilege_type"]),
                 f"{r['table_catalog']}.{r['table_schema']}.{r['table_name']}") for r in rows]

    def emit_masking(self, masks):
        return [{"table_full_name": k.rsplit(".", 1)[0], "column_name": k.rsplit(".", 1)[1], "mask_function": v}
                for k, v in masks.items()]

    def parse_masking(self, rows):
        return {f"{r['table_full_name']}.{r['column_name']}": r["mask_function"] for r in rows}


class BigQuery(Engine):
    """INFORMATION_SCHEMA (TABLE_STORAGE, COLUMNS), JOBS (total_bytes_billed), OBJECT_PRIVILEGES."""
    name, family, unit, price_key, default_price = "bigquery", "bigquery", "TiB billed", "bigquery_tib", 6.25
    PRIV = {"SELECT": "roles/bigquery.dataViewer", "ALL": "roles/bigquery.dataOwner"}

    def emit_table(self, physical, obj, now):
        return {"table_id": physical, "last_modified_time": int((now - timedelta(minutes=obj["lag_minutes"])).timestamp() * 1000),
                "window_row_count": obj["rows"], "trailing_weekday_row_counts": [obj["baseline"]] * 4,
                "columns": [{"column_name": c, "data_type": t} for c, t in to_native(self.family, obj["columns"]).items()]}

    def parse_table(self, p, now):
        ts = datetime.fromtimestamp(p["last_modified_time"] / 1000, tz=timezone.utc)
        return {"lag_minutes": round(_minutes(ts, now)), "rows": int(p["window_row_count"]),
                "expected_rows": self.expected_rows(p["trailing_weekday_row_counts"]),
                "columns": to_logical(self.family, {c["column_name"]: c["data_type"] for c in p["columns"]})}

    def emit_cost(self, daily, prices):
        return {"jobs": [{"job_id": "j1", "total_bytes_billed": int(round(daily / self.price(prices) * 2**40))}]}

    def parse_cost(self, p, prices):
        return round(sum(j["total_bytes_billed"] for j in p["jobs"]) / 2**40 * self.price(prices), 2)

    def emit_query_profile(self, full_scans, top_physical):
        return {"jobs": [{"job_id": "j1", "total_bytes_processed": 10**12, "table_size_bytes": 10**12,
                          "referenced_table": top_physical}] if full_scans else []}

    def parse_query_profile(self, p):
        full = [j for j in p["jobs"] if j["total_bytes_processed"] >= j["table_size_bytes"]]
        return {"full_scans": len(full), "top_physical": full[0]["referenced_table"] if full else None}


class Redshift(Engine):
    """SVV_TABLE_INFO / SVV_COLUMNS, SYS_SERVERLESS_USAGE (RPU-seconds), SVV_RELATION_PRIVILEGES."""
    name, family, unit, price_key, default_price = "redshift", "redshift", "RPU-hours", "redshift_rpu_hour", 0.375

    def emit_table(self, physical, obj, now):
        return {"table": physical, "last_load_end": ISO(now - timedelta(minutes=obj["lag_minutes"])),
                "window_rows": obj["rows"], "trailing_weekday_rows": [obj["baseline"]] * 4,
                "columns": [{"column_name": c, "data_type": t} for c, t in to_native(self.family, obj["columns"]).items()]}

    def parse_table(self, p, now):
        return {"lag_minutes": round(_minutes(_dt(p["last_load_end"]), now)), "rows": int(p["window_rows"]),
                "expected_rows": self.expected_rows(p["trailing_weekday_rows"]),
                "columns": to_logical(self.family, {c["column_name"]: c["data_type"] for c in p["columns"]})}

    def emit_cost(self, daily, prices):
        return {"sys_serverless_usage": [{"charged_seconds": round(daily / self.price(prices) * 3600, 3)}]}

    def parse_cost(self, p, prices):
        return round(sum(r["charged_seconds"] for r in p["sys_serverless_usage"]) / 3600 * self.price(prices), 2)


class Fabric(Engine):
    """Warehouse tables over OneLake Delta; capacity metrics (CU-seconds); sys.database_permissions."""
    name, family, unit, price_key, default_price = "fabric", "tsql", "CU-hours", "fabric_cu_hour", 0.18

    def emit_table(self, physical, obj, now):
        d = Delta().emit_table(physical, obj, now)
        d["schema"]["fields"] = [{"name": c, "type": t, "nullable": True}
                                 for c, t in to_native("tsql", obj["columns"]).items()]
        return d

    def parse_table(self, p, now):
        r = Delta().parse_table({**p, "schema": {"fields": []}}, now)
        r["columns"] = to_logical("tsql", {f["name"]: f["type"] for f in p["schema"]["fields"]})
        return r

    def emit_cost(self, daily, prices):
        return {"capacity_metrics": [{"operation": "Warehouse Query", "cu_seconds": round(daily / self.price(prices) * 3600, 3)}]}

    def parse_cost(self, p, prices):
        return round(sum(r["cu_seconds"] for r in p["capacity_metrics"]) / 3600 * self.price(prices), 2)


class DuckDBLocal(Engine):
    """Local lakehouse: DuckDB catalog views; cost is the local cost model in currency units."""
    name, family = "duckdb-local", "duckdb"

    def emit_table(self, physical, obj, now):
        return {"table": physical, "last_commit": ISO(now - timedelta(minutes=obj["lag_minutes"])),
                "window_rows": obj["rows"], "trailing_weekday_rows": [obj["baseline"]] * 4,
                "columns": to_native(self.family, obj["columns"])}

    def parse_table(self, p, now):
        return {"lag_minutes": round(_minutes(_dt(p["last_commit"]), now)), "rows": p["window_rows"],
                "expected_rows": self.expected_rows(p["trailing_weekday_rows"]),
                "columns": to_logical(self.family, p["columns"])}


ENGINES: dict[str, Engine] = {e.name: e for e in (Snowflake(), Databricks(), BigQuery(), Redshift(), Fabric(), DuckDBLocal())}


# ---------------------------------------------------------------------------------------------
# Data quality results. Canonical: status pass|fail and failed_pct.
class QualityTool:
    name = "base"

    def emit(self, rule_id: str, dataset: str, status: str, failed_pct: float, rows: int) -> dict: ...
    def parse(self, p: dict) -> dict: ...


class GreatExpectations(QualityTool):
    name = "great-expectations"

    def emit(self, rule_id, dataset, status, failed_pct, rows):
        return {"success": status == "pass", "meta": {"expectation_suite_name": dataset},
                "results": [{"success": status == "pass",
                             "expectation_config": {"type": "expect_column_values_to_be_between",
                                                    "meta": {"rule_id": rule_id}},
                             "result": {"unexpected_percent": failed_pct, "element_count": rows}}]}

    def parse(self, p):
        r = p["results"][0]
        return {"status": "pass" if r["success"] else "fail", "failed_pct": float(r["result"]["unexpected_percent"])}


class DbtTests(QualityTool):
    name = "dbt-tests"

    def emit(self, rule_id, dataset, status, failed_pct, rows):
        return {"results": [{"unique_id": f"test.ops.{rule_id.lower().replace('-', '_')}", "status": status,
                             "failures": int(rows * failed_pct / 100), "adapter_response": {"rows_affected": rows}}]}

    def parse(self, p):
        r = p["results"][0]
        rows = r["adapter_response"]["rows_affected"] or 1
        return {"status": "pass" if r["status"] == "pass" else "fail", "failed_pct": round(r["failures"] / rows * 100, 2)}


class PipelineExpectations(QualityTool):
    """Lakeflow declarative pipelines event log: flow_progress.data_quality.expectations."""
    name = "pipeline-expectations"

    def emit(self, rule_id, dataset, status, failed_pct, rows):
        failed = int(rows * failed_pct / 100)
        return {"event_type": "flow_progress", "details": {"flow_progress": {"data_quality": {"expectations": [
            {"name": rule_id, "dataset": dataset, "passed_records": rows - failed, "failed_records": failed}]}}}}

    def parse(self, p):
        e = p["details"]["flow_progress"]["data_quality"]["expectations"][0]
        total = (e["passed_records"] + e["failed_records"]) or 1
        return {"status": "fail" if e["failed_records"] else "pass",
                "failed_pct": round(e["failed_records"] / total * 100, 2)}


class Soda(QualityTool):
    name = "soda"

    def emit(self, rule_id, dataset, status, failed_pct, rows):
        return {"checks": [{"name": rule_id, "table": dataset, "outcome": status,
                            "diagnostics": {"value": failed_pct}}]}

    def parse(self, p):
        c = p["checks"][0]
        return {"status": "pass" if c["outcome"] == "pass" else "fail", "failed_pct": float(c["diagnostics"]["value"])}


class NativeMetrics(QualityTool):
    name = "native-metrics"

    def emit(self, rule_id, dataset, status, failed_pct, rows):
        return {"rule": rule_id, "dataset": dataset, "status": status, "failed_pct": failed_pct}

    def parse(self, p):
        return {"status": p["status"], "failed_pct": p["failed_pct"]}


class DataformAssertions(DbtTests):
    name = "dataform-assertions"


QUALITY: dict[str, QualityTool] = {q.name: q for q in
                                   (GreatExpectations(), DbtTests(), PipelineExpectations(), Soda(), NativeMetrics(),
                                    DataformAssertions())}


# ---------------------------------------------------------------------------------------------
# Catalog and governance: classification tags, retention, trust-status writes.
class Catalog:
    name = "base"
    trust_command = "set trust status attribute"

    def emit(self, tags: dict[str, list[str]], retention: dict[str, int], objects: list[str]) -> dict:
        return {"assets": [{"name": o, "tags": tags.get(o, []), "retention_days": retention.get(o)} for o in objects]}

    def parse(self, p: dict) -> dict:
        assets = p["assets"]
        return {"objects": [a["name"] for a in assets],
                "tags": {a["name"]: a["tags"] for a in assets if a["tags"]},
                "retention": {a["name"]: a["retention_days"] for a in assets if a.get("retention_days") is not None}}


class Collibra(Catalog):
    name = "collibra"
    trust_command = "PATCH /rest/2.0/attributes (Trust Status) on the data set asset"

    def emit(self, tags, retention, objects):
        return {"results": [{"name": o, "type": {"name": "Table"},
                             "tags": [{"name": t} for t in tags.get(o, [])],
                             "attributes": ([{"type": {"name": "Retention Period (days)"}, "value": retention[o]}]
                                            if o in retention else [])} for o in objects]}

    def parse(self, p):
        out = {"objects": [], "tags": {}, "retention": {}}
        for a in p["results"]:
            out["objects"].append(a["name"])
            if a["tags"]:
                out["tags"][a["name"]] = [t["name"] for t in a["tags"]]
            for at in a["attributes"]:
                if at["type"]["name"] == "Retention Period (days)":
                    out["retention"][a["name"]] = int(at["value"])
        return out


class UnityCatalog(Catalog):
    name = "unity-catalog"
    trust_command = "ALTER TABLE <t> SET TAGS ('trust_status' = <status>)"

    def emit(self, tags, retention, objects):
        rows = []
        for o in objects:
            for t in tags.get(o, []):
                rows.append({"table_name": o, "tag_name": t, "tag_value": ""})
        return {"table_tags": rows, "tables": [{"table_name": o, "properties": (
            {"delta.deletedFileRetentionDuration": f"interval {retention[o]} days"} if o in retention else {})}
            for o in objects]}

    def parse(self, p):
        tags: dict[str, list[str]] = {}
        for r in p["table_tags"]:
            tags.setdefault(r["table_name"], []).append(r["tag_name"])
        ret = {}
        for t in p["tables"]:
            v = t["properties"].get("delta.deletedFileRetentionDuration")
            if v:
                ret[t["table_name"]] = int(re.search(r"(\d+)", v).group(1))
        return {"objects": [t["table_name"] for t in p["tables"]], "tags": tags, "retention": ret}


class Dataplex(Catalog):
    name = "dataplex"
    trust_command = "entries.patch aspect trust-status"


class Purview(Catalog):
    name = "purview"
    trust_command = "PUT entity classification TrustStatus"


class DataHub(Catalog):
    name = "datahub"
    trust_command = "upsert structured property trust_status"


class SnowflakeHorizon(Catalog):
    name = "snowflake-horizon"
    trust_command = "ALTER TABLE <t> SET TAG trust_status = <status>"


class LocalCatalog(Catalog):
    name = "adef-local-catalog"
    trust_command = "adef catalog set-trust"


class Alation(Catalog):
    name = "alation"
    trust_command = "PUT trust flag on the table"


class Atlan(Catalog):
    name = "atlan"
    trust_command = "update certificateStatus on the asset"


CATALOGS: dict[str, Catalog] = {c.name: c for c in (Collibra(), UnityCatalog(), Dataplex(), Purview(), DataHub(),
                                                    SnowflakeHorizon(), LocalCatalog(), Alation(), Atlan())}

QUARANTINE_COMMAND = {"delta": "INSERT INTO <quarantine> SELECT ... WHERE _batch_id = <id>; DELETE FROM <t> WHERE _batch_id = <id>",
                      "iceberg": "write batch to <quarantine>; CALL system.rollback_to_snapshot or DELETE WHERE _batch_id = <id>",
                      "hudi": "move batch to <quarantine>; DELETE WHERE _batch_id = <id>"}
