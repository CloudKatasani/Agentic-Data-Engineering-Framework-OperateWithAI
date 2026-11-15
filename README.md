# Operate-With-AI (ADEF suite)

An approval-gated team of AI agents that keeps live data products inside their service,
quality, cost and compliance targets. The specification is [`OPERATE_SPEC.md`](OPERATE_SPEC.md);
the eight agent contracts are in [`skills/`](skills/) (unpacked from
`operate-with-ai-skills.zip`).

**Detection is code and judgement is agents.** The deterministic runtime enforces the
guardrails in spec section 8 outside the prompts. All eight agents are built: Commander, Service Onboarding, Signal Sentinel, Incident
Diagnosis, Remediation, FinOps and Performance, Governance and Compliance, and Problem
and Knowledge.

## Status

All ten days of the plan are built and tested: Epics O1, O2 and O4, O3-1, O3-3 and O3-4,
O5-1 and O5-2, and the shadow-mode machinery and go-live rule for O5-3 and O5-4. Telemetry
translators exist for every tool in Build-With-AI's preset Stack Profiles (O3-2 is partly
done; live API clients are pending). The real two-week shadow period starts once a real
product's exports and START HERE answers are available: see [`SHADOW_MODE.md`](SHADOW_MODE.md).

## Built for the data-engineering stack Build-With-AI produces

Operate takes over a data product after Build releases it. It reads the same **Stack
Profile** and **Domain Pack** formats, and the same medallion layers (bronze, silver, gold).
All six Build presets bind out of the box:

| Profile | Orchestration | Lake (bronze, silver) | Serving (gold) | Quality | Catalog |
|---|---|---|---|---|---|
| `example-a` | Airflow | Iceberg on S3 (Glue) | Snowflake | Great Expectations | Collibra |
| `example-b` | Databricks Workflows | Delta on ADLS | Databricks SQL | Lakeflow expectations | Unity Catalog |
| `gcp-bigquery` | Airflow | Iceberg on GCS | BigQuery | Great Expectations | Dataplex |
| `azure-fabric` | Data Factory | Delta on OneLake | Fabric | Great Expectations | Purview |
| `aws-redshift` | Airflow | Iceberg on S3 (EMR) | Redshift | Great Expectations | DataHub |
| `local-lakehouse` | ADEF runner | Delta on local disk | DuckDB | native metrics | local catalog |

Signals, cases, monitoring specs and runbooks speak logical names and slots only. Moving
a live product to another profile changes none of them (O3-4). See [`BUILD_STATUS.md`](BUILD_STATUS.md) for the backlog item by item, the guardrail
scenarios already covered, the assumptions made, and what comes next.

## Quick start

```bash
pip install -e ".[dev]"
```

```bash
pytest -q
```

```bash
python -m adef_operate demo
```

```bash
python -m adef_operate faults
```

```bash
python -m adef_operate profiles
```

```bash
python -m adef_operate demo --profile example-b
```

```bash
python -m adef_operate guardrails --profile example-a
```

```bash
python -m adef_operate evals --profiles all --out evals-report
```

```bash
python -m adef_operate shadow-rehearsal --profile example-a
```

The `demo` command releases and onboards both golden products (Utilities and Telco), records
OG0 and OG1 as the synthetic approvers named in the packs, then injects each library fault
and runs one tick. A tick runs the monitor engine, then Sentinel grouping, then the
Commander opening, routing or batching the case.

## Layout

```
adef_operate/
  agents/      commander.py, onboarding.py, sentinel.py, diagnosis.py, remediation.py,
               finops.py, governance.py, knowledge.py, requests.py (operator request desk),
               reasoning.py (Reasoner seam and deterministic RuleReasoner)
  simulation.py  golden-product scenarios and the diagnosis accuracy evaluation
  runtime.py   one control-plane tick: monitor engine -> Sentinel -> Commander
  shadow_mode.py      shadow runtime: read-only onboarding from exports, ticks, observed recovery
  shadow_rehearsal.py 14-day rehearsal on synthetic exports
  stack/       Build-With-AI Stack Profiles, Operate slot binding, naming, per-tool translators,
               native-platform emulator, StackAdapter, read-only export adapter
  evals/       replay harness (O5-1) and guardrail suite OT-01 to OT-16 (O5-2)
  core/        stand-in for adef-core: hashing, hash-chained log, autonomy levels, errors
  schemas/     SIG, case, ACT, approval, handover pack, service and monitoring-spec models
  engine/
    workspace.py         ops workspace and per-identity write allowlists
    case_store.py        lifecycle state machines plus recover and close guards
    handover.py          receiver check and product.released consumer
    approvals.py         gate requests and decisions, OG1 standing approval (hash manifest)
    registry.py          autonomy levels, earned autonomy, budgets, emergency stop
    catalogue.py         default action catalogue (spec section 6)
    executor.py          the hook: level, gate, precondition re-check and caps
    monitor_engine.py    evaluates only the OG1-approved monitoring-spec.yaml
    lineage.py           ancestors, descendants and neighbourhoods
    correlator.py        Sentinel grouping, labels and known events
    severity.py          severity by rule; Domain Pack overrides raise only
    evidence.py          read-only, metadata-only evidence reader (budgeted; raw values refused)
    council.py           council-of-three review for S1 diagnoses
    isolation.py         client isolation rule
    shadow.py            START HERE intake, people's conclusions, shadow report, go-live rule
    injection.py         treats log and ticket text as untrusted data
    onboarding_tools.py  draft service files, dry run and fault proofs for OG1
  connectors/  adapter contract (operate section) and the Demo Simulator with faults F-01 to F-08
  packs/       synthetic Utilities and Telco Domain Packs and golden handover packs
  api/         (next: section 11 endpoints)
skills/        eight SKILL.md contracts (validated by adef_operate.skills_check)
tests/         foundation, control, signals, day 6 to day 10, skills
```
