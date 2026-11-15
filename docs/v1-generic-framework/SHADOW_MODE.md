# Shadow mode: running Operate read-only on a real product

This is the Day 10 step of the plan (O5-3) and the path to go-live (O5-4). In shadow mode
Operate watches a real, non-production data product and works out what it would do. Your
team keeps running its normal process. After two weeks, the shadow report compares the
agents with your team's own conclusions.

## What shadow mode guarantees

| Guarantee | How it is enforced |
|---|---|
| Nothing is written to your platform | Operate reads exported metadata files. The export adapter has no actions, so the executor finds nothing it may run. |
| No action at all, even rule-based ones | Every action type is capped at L1 while `mode: shadow` (`Registry.effective_level`). |
| Nobody is paged for routine incidents | The Commander records "would page on-call" and "would set trust status". Exception: a suspected security event is always escalated to Security and Compliance. |
| No approvals are sent to people | Gate requests are recorded with a `[SHADOW ...]` label for comparison only. Remediation refuses to act or request. |
| No credentials held by Operate | Your platform team runs the exports with read-only identities named in START HERE. |
| Nothing goes above L1 without the go-live rule | Two weeks of shadow, diagnosis accuracy at least 0.80 on at least 5 confirmed incidents, alert precision at least 0.85, zero unsupported causes, zero executions, then an approved OG7 go-live decision from the Platform Owner and Tech Lead. A synthetic rehearsal can never support go-live. |

## Steps

1. **Answer START HERE.** Write the template:

   ```bash
   python -m adef_operate start-here --dp <dp_id> --profile <profile id> --out start-here.yaml
   ```

   Fill in the first service, the Stack Profile, one read-only identity per Operate slot
   (each verified by a named person), on-call and approvers, documented idempotent
   pipelines, and baseline figures. Pipelines are treated as **not** idempotent unless
   they are listed with a documentation link.

2. **Export metadata.** Run one export per poll (daily is enough to start) into
   `<exports>/<dp_id>/<snapshot id>/`. Include a week of history before shadow starts;
   it is used for the onboarding dry run. The file layout and sources are below.

3. **Release** with the Operations Handover Pack from Build stage 7:

   ```bash
   python -m adef_operate shadow release --home <ops home> --profile <id> --exports <exports> --pack handover-pack.yaml --answers start-here.yaml
   ```

   The START HERE answers and the Stack Profile are validated, and every gap is listed
   at once. The product enters shadow mode and OG0 is requested.

4. **Decide OG0**, by the Product Owner and the Platform Owner:

   ```bash
   python -m adef_operate decide --home <ops home> --dp <dp_id> --request APR-001 --as "<name>" --role product_owner --decision approved
   ```

5. **Onboard** to readiness:

   ```bash
   python -m adef_operate shadow onboard --home <ops home> --profile <id> --exports <exports> --dp <dp_id>
   ```

   This drafts the monitoring spec, runbook and bindings. It dry-runs them over the
   history snapshots and lists any alerts on your real latest snapshot. It then proves
   every monitor class fires on a synthetic overlay of a copy of that snapshot, and
   requests OG1. The Platform Owner reviews `service/readiness-report.md` and decides
   OG1.

6. **Tick on each new snapshot:**

   ```bash
   python -m adef_operate shadow tick --home <ops home> --profile <id> --exports <exports> --dp <dp_id>
   ```

   Agents detect, group, open cases, diagnose and draft options. They also watch for the
   recovery your team achieves on later snapshots.

7. **Record your team's conclusions.** Record each ticket and its cause, and judge each
   agent case as real or not:

   ```bash
   python -m adef_operate conclude --home <ops home> --dp <dp_id> --by "<name>" --ticket TCK-123 --case INC-004 --opened-at 2026-01-12T07:25:00+00:00 --cause platform --real yes --minutes 35
   ```

   Only IDs are linked; no ticket text is copied. A recovered case with a confirmed cause
   gets its post-incident note and evaluation replay, and closes on the next tick.

8. **Read the report**, which you can do at any time:

   ```bash
   python -m adef_operate shadow report --home <ops home> --dp <dp_id>
   ```

9. **Go or no-go.** If the report says GO:

   ```bash
   python -m adef_operate shadow go-live-request --home <ops home> --dp <dp_id>
   ```

   The Platform Owner and Tech Lead decide the OG7 request. Then:

   ```bash
   python -m adef_operate shadow go-live-apply --home <ops home> --dp <dp_id>
   ```

   Go-live raises nothing by itself. Each action type is still promoted one at a time at
   OG7, using the platform's own track record.

To see the whole path end to end on synthetic exports first:

```bash
python -m adef_operate shadow-rehearsal --profile example-a
```

## Export layout and read-only sources

Each snapshot holds `manifest.json` (`profile_id`, `captured_at`, `windows` per
pipeline, `synthetic: false`) and the files below. Physical names follow your profile's
naming standard. The parse side of each tool's translator is in
`adef_operate/stack/translators.py`. Check the first real export against it: onboarding
fails loudly on a missing or mis-shaped file.

| File | Airflow / Workflows / Data Factory | Snowflake | Databricks | BigQuery | Redshift |
|---|---|---|---|---|---|
| `orchestration/<pipeline>.json` | Airflow `GET /api/v1/dags/{dag}/dagRuns` (latest); Databricks `GET /api/2.1/jobs/runs/get`; Data Factory `queryPipelineRuns` | `TASK_HISTORY` (if Snowflake tasks orchestrate) | - | - | - |
| `orchestration/<pipeline>.log` | Task log of the latest run (treated as untrusted text) | | | | |
| `tables/<physical>.json` (bronze, silver) | Delta `DESCRIBE HISTORY` plus schema; Iceberg `<table>.snapshots` plus schema | | | | |
| `tables/<physical>.json` (gold) | | `INFORMATION_SCHEMA.TABLES` / `COLUMNS` plus a capped latest-window count | Delta `DESCRIBE HISTORY` | `INFORMATION_SCHEMA.TABLE_STORAGE` / `COLUMNS` plus a capped count | `SVV_TABLE_INFO` / `SVV_COLUMNS` plus a capped count |
| `quality/<rule>.json` | Great Expectations validation result; dbt `run_results.json`; Lakeflow event log `flow_progress` expectations; Soda scan result | | | | |
| `cost.json` | | `WAREHOUSE_METERING_HISTORY` | `system.billing.usage` | `INFORMATION_SCHEMA.JOBS` (`total_bytes_billed`) | `SYS_SERVERLESS_USAGE` |
| `query_profile.json` | | `QUERY_HISTORY` (partitions scanned against total) | query history (files read, pruned) | `JOBS` (bytes processed against table size) | query history |
| `grants.json` | | `GRANTS_TO_ROLES` | Unity Catalog `information_schema.table_privileges` | `OBJECT_PRIVILEGES` | `SVV_RELATION_PRIVILEGES` |
| `masking.json` | | `POLICY_REFERENCES` | Unity Catalog column masks | - | - |
| `catalog.json` | Collibra REST assets; Unity Catalog `table_tags`; Dataplex, Purview or DataHub entries | | | | |
| `access_history.json`, `audit.json` | Role last-used days; audit-logging status from the platform's access history | | | | |
| `evidence/*` (optional) | Run history, batch checksums, region breakdowns, releases (CI/CD), upstream notices, cost breakdown, object reads. Without these, Diagnosis has less evidence and says so ("unknown" rather than a guess). | | | | |

Prices for cost conversion (credits, DBUs, bytes billed, RPU-hours, CU-hours) are set per
contract in the profile's `variables.prices`. The defaults are illustrative only.

## What the rehearsal shows, and what it does not

The rehearsal (`adef_operate/shadow_rehearsal.py`) exports 14 daily snapshots of the
Utilities golden product in the `example-a` native shapes, with six library faults. One
fault (F-01) recurs on day 9. The team's conclusions in the rehearsal are synthetic and are
taken from the fault labels. So its 1.0 accuracy shows only that the shadow machinery
works end to end. It is not evidence about the agents' real accuracy, and the report and
the go-live request both refuse to treat it as such.
