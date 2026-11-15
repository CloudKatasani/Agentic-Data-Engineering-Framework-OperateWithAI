# Operate-With-AI (ADEF suite)

An approval-gated team of AI agents that keeps live data products inside their service,
quality, cost and compliance targets. The specification is [`OPERATE_SPEC.md`](OPERATE_SPEC.md);
the eight agent contracts are in [`skills/`](skills/) (unpacked from
`operate-with-ai-skills.zip`).

## How it works: agents do the work, people decide

Eight AI agents run operations around the clock and bring people in at every decision. See
[`OPERATING_MODEL.md`](OPERATING_MODEL.md) for each agent's role, responsibilities and limits, the
human roles, and the full "who is notified, for what, by when" matrix.

| Agents do on their own | People decide (notified in their inbox, with one-click actions) |
|---|---|
| Detect (monitors), group signals, open one case per cause, set severity by rule | **On-call:** acknowledge and own the incident (S1 15 min, S2 30 min) |
| Diagnose with evidence and hypotheses | **Council of three** (S1 only): agree with the diagnosis before fixes are offered |
| Propose fixes ranked by safety and recommend one | **Approver:** choose the fix; choosing records the approval at the gate |
| Execute only what a person approved (or a standing L3 approval allows, with a notice) | **On-call:** run fixes that are executed by people, then "I ran it" |
| Verify recovery with the detecting monitors; contain first without counting an attempt | **Service owner:** confirm the cause, review the post-incident note, close |
| Draft the post-incident note, open problems, propose control and autonomy changes | **Platform owner and tech lead:** OG7 proposals; **Security:** OG5 and security escalations |

Unanswered asks escalate to the next role when their response time passes. The console's
home page is each person's inbox; the "Agent team and roles" page shows what every agent owns and
is doing now. With `--autotick`, the loop runs continuously:

```bash
./run.sh --demo --profile example-a --autotick 30
```

Set `ADEF_NOTIFY_WEBHOOK` to a Slack-compatible incoming webhook to also push every ask
that needs action.

**Detection is code and judgement is agents.** The deterministic runtime enforces the
guardrails in spec section 8 outside the prompts. All eight agents are built: Commander, Service Onboarding, Signal Sentinel, Incident
Diagnosis, Remediation, FinOps and Performance, Governance and Compliance, and Problem
and Knowledge.

## Status

All ten days of the plan and the section 11 API and console are built and tested: Epics O1, O2 and O4, O3-1, O3-3 and O3-4,
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

## Console and API (section 11)

```bash
./run.sh --demo --profile example-b
```

Then open http://127.0.0.1:8090/. The console has these views:

| View | What it shows |
|---|---|
| My inbox (home) | Everything the agents need from you: acknowledge, review, choose a fix, approve, run, confirm, close. Each shows the asking agent, priority and response time. |
| Agent team and roles | Each agent's role, responsibilities, limits and when it brings a person in, with live work; the human roles and who holds them; how an incident moves between AI and people. |
| Activity | Every agent action and every human decision across the fleet. |
| Fleet health | Every product: mode, trust status, open cases by severity, findings, OG1, today's budgets, items waiting on a person. Includes the emergency stop. |
| Case room | Lifecycle, timeline, impact, signals with Sentinel labels, metadata-only evidence, hypotheses with for and against, options ranked by safety, approvals, council of three, verification, confirmed cause, post-incident note. |
| Service page | Autonomy ladder (stored against effective level, with the reason and track record), monitors, runbook, readiness, health summary, stack binding, escalations, the Demo Simulator controls, and "Ask the Commander". |
| FinOps | Latest review, findings with the saving arithmetic, savings ledger. |
| Governance | Each control as matching, drifted, waived or not verifiable; drift findings; the evidence pack. |
| Learning | Problems, evaluation replays and accuracy, platform track record, noise report, autonomy and tuning proposals. |
| Approvals | Every open gate request across the fleet. Only the named holder of a role can decide; agents never can. |
| Onboard | Release a golden product (Demo Simulator), or a real product in shadow mode from exports. |

The API follows OPERATE_SPEC section 11 exactly. Interactive docs are at `/docs`.

| Method and path | Purpose |
|---|---|
| `POST /ops/services` | Onboard from a handover pack (`mode: demo` or `shadow`) |
| `GET /ops/services/{dp}` | Service definition, health, autonomy levels |
| `POST /ops/signals` | Ingest (collector and Consume events); `?process=true` groups and routes |
| `GET /ops/cases`, `GET /ops/cases/{id}` | Queue (filters `dp`, `state`, `severity`, `type`) and case detail with evidence |
| `POST /ops/cases/{id}/approvals` | Decide a gate request (named human only) |
| `POST /ops/simulate/faults` | Inject a fault (Demo Simulator products only) |
| `GET /ops/finops/{dp}`, `GET /ops/governance/{dp}` | Reviews and findings |
| `POST /ops/stop` (and `DELETE /ops/stop` to lift it) | Emergency stop, global or per product |

Supporting endpoints sit under the same prefix: case steps, onboarding, ticks, the approvals
inbox, learning actions, the request desk and the shadow report.

**Security** follows Build-With-AI's console. The server refuses to listen beyond
localhost without one of these:

- `ADEF_API_TOKEN`: one shared bearer token.
- `ADEF_IDENTITY_FILE`: a YAML map of `sha256(token)` to a person's name. Each person
  uses their own token, and the API lets them decide, review, confirm or stop only as
  themselves. The engine still checks that they hold the role.

Guard refusals come back as `409` with `guardrail: true` and the reason. A wrong or agent
approver gets `403`.

## Model-backed diagnosis (optional)

The deterministic rule reasoner is the default. A Claude-backed reasoner can sit behind the
same `Reasoner` seam (`agents/model_reasoner.py`):

```bash
pip install -e ".[model]"
```

| `ADEF_REASONER` | Effect |
|---|---|
| `rule` (default) | Rule reasoner only. No data leaves the platform. |
| `challenger` | The rule reasoner decides; the model runs on the same evidence and its conclusion is recorded for comparison (case room, `diagnosis.md`, shadow report). |
| `model` | The model decides; it falls back to the rule reasoner on any failure and records why. |

`ADEF_REASONER_MODEL` defaults to `claude-opus-5-5`, and `ADEF_REASONER_EFFORT` to `high`.
Credentials come from the Anthropic SDK's usual sources.

Safeguards:
- **What is sent.** Metadata-only evidence, with emails, IPs and long numbers redacted and each item size-capped.
- **Untrusted text.** Log and ticket text is marked untrusted; any instructions found in it are reported back, never followed.
- **No tools.** The model has no tools; it returns a JSON document under a strict schema.
- **Same rules.** The agent applies the same contract to the model's output as to the rule reasoner's: citations must exist, no cause without evidence, and a recent change is a lead, not a cause.
- **Refusals.** Server-side refusal fallback is on.
- **Cost.** Charged to the product's daily `model_spend` budget. A stopped product makes no model calls.

Compare the two on the fault library before switching (billed; the command prints an estimate and refuses to run without the flag):

```bash
python -m adef_operate compare-reasoners --confirm-spend
```

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
  api/         fleet.py (service layer) and app.py (section 11 REST API, FastAPI)
  web/         console: index.html, app.js (dependency-free), operate.css; styles.css and the
               Carlito font are shared with Build-With-AI
skills/        eight SKILL.md contracts (validated by adef_operate.skills_check)
tests/         foundation, control, signals, day 6 to day 10, API and console, skills
```
