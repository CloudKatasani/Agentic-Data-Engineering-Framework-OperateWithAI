# OpsCrew-OperateWithAI

AI agents and a human team running a **data platform managed service** together: monitoring,
incidents, the nightly batch, streaming freshness, service requests and service reporting, for
whatever stack the client runs. ServiceNow stays the system of record.

| Layer | Supported out of the box (28 technologies) |
|---|---|
| Sources and CDC | Oracle, IBM Db2, Oracle GoldenGate, Fivetran (and similar), Kafka / Confluent / MSK / Event Hubs, Pub/Sub, file transfer and object storage landing |
| Ingestion and orchestration | Informatica, Azure Data Factory / Synapse pipelines, AWS Glue, Airflow (MWAA, Composer, Astronomer), Dataflow, anything that emits OpenLineage |
| Warehouse and lakehouse | Snowflake, Amazon Redshift, Google BigQuery, Databricks, dbt, Denodo |
| Data quality | Elementary, Soda, Great Expectations, dbt tests (freshness, volume, schema, test failures) |
| Consumption | Power BI, Tableau, Looker, Cognos, reverse ETL (Hightouch, Census), ML scoring jobs, partner extracts, data APIs |
| Cloud and monitoring | AWS, Azure, Google Cloud, Dynatrace |

A technology is a YAML entry (`knowledge/tech.yaml`): the feeds it must provide, how its events
look, the error patterns to recognise and the action that reruns its jobs. Adding one needs no code.

The agents do the watching, correlating, checking, drafting and the execution of agreed
runbook steps. **People make every decision** that changes the client's systems, spends money,
touches business data or speaks to the client. Each decision is a request to a named seat (for
example the Oracle DBA on call, the Security Lead, the Incident Manager), with a response time
and an escalation path. The demo uses seats, not individuals; a real roster maps each seat to
whoever is on shift.

Part of the ADEF suite next to Build-With-AI; the console shares its look and conventions.

## Try it

```bash
./run.sh
```

Open http://127.0.0.1:8090. It starts a simulated night at a demo client with the
"typical bad night" preset, paused at the first thing the agents detect: an expired Oracle service account, a duplicate vendor file and,
later, a Power BI gateway outage. There is no login or role switch in the demo: every decision button answers as the role the agent asked ("Approve · as the Security Lead"), and you
move the clock with the buttons at the top.

The sidebar follows the demo order: **1 Agents and people** (who does what), **2 Data needed
from tools** (what the client provides), **3 Command center** (the live night), **4 Decisions for
people** (everything waiting, grouped by the role that must decide). The **Scenario simulator** page starts other nights.

```bash
python -m operatewithai scenarios                      # the ten scenarios
python -m operatewithai night --scenarios S01,S06      # run a night in the terminal with nobody answering
pytest -q
```

## Four demo client estates

The simulator runs any of these; pick one on the **Scenario simulator** page or with `--client`.

| Client (synthetic) | Stack | Consumption patterns | Scenarios |
|---|---|---|---|
| `demo` Aldermoor Insurance | Oracle, Db2, GoldenGate, Informatica, Snowflake, dbt, Denodo, Power BI, Cognos | Dashboards, regulated scheduled reports | 10 |
| `lakehouse` Northbridge Retail | ADF, Kafka, Databricks, data quality checks, Power BI on Azure | Dashboard, ML batch scoring, reverse ETL to CRM, near-real-time data API | 5 |
| `awsdw` Calloway Logistics | Fivetran, S3, Glue, Airflow with dbt, Redshift, Tableau on AWS | Dashboard, contractual file extract to partners | 4 |
| `gcp` Verity Clinics | Pub/Sub, Dataflow, BigQuery, Composer with dbt, Looker | Scheduled BI delivery, near-real-time operational feed | 2 |

Two kinds of promise are tracked: a **deadline** ("the dashboard is refreshed by 07:00") and a
**freshness promise** ("the order API is never more than 10 minutes behind"). A failed data
quality check holds the consumer back instead of publishing wrong numbers.

```bash
./run.sh --client lakehouse
python -m operatewithai benchmark     # inject all 21 faults, grade triage and diagnosis against the injected cause
```

## Live run, step by step

For a demonstration, open **Scenario simulator**, pick what goes wrong and start the night.
The **Live run** page then shows each issue as a pipeline:

`Detect → Triage → Diagnose → Plan → Fix and recover → Resolve → Inform the client → Learn`

Each stage shows the agent that did it and what it produced, then waits for the person whose
decision comes next: the L1 Operator confirms the detection, the engineer acknowledges, agrees
with the diagnosis and accepts the plan, the named role approves its step, the engineer confirms
the fix. The next agent starts only after that decision. Buttons on this page answer as the
role the agent asked, so one presenter can walk the whole flow. "Continue to the next event"
moves the clock to the next thing that happens.

Two modes: **step by step** (the default; a person confirms every hand-off) and **fast**
(agents hand off to each other; people approve plans, fixes and client messages).
`./run.sh --fast` starts in fast mode.

## What a night looks like

| Time | What happens | Who |
|---|---|---|
| 00:35 | SVC_DATA expires in 9 days: preventive rotation request raised | Service Request Agent asks the security lead |
| 01:10 | Password expires. GoldenGate extract abends (OGG-00664), Informatica fails (CMN_1022). Both carry ORA-28001: **one** P2 incident, routed to the Oracle DBA on call | Watch Desk and Triage agents |
| 01:10 | Cause confirmed by read-only checks (DBA_USERS, extract report). Four-step plan from runbook KE0003 | Diagnosis and Resolution agents |
| 01:10 | First stakeholder notice drafted | Comms Agent asks the incident manager |
| | Accepts the plan | **Oracle DBA on call** |
| | Approves the credential rotation | **Security lead** |
| | Rotates through the vault, starts the extract, waits for lag to clear, restarts the failed workflow | Resolution Agent |
| | Confirms resolved; post-incident review approved | **DBA**, **SDM** |
| all night | "Finance Daily Close will be ready 05:30 against 07:00" recalculated every cycle; priority raised if a deadline is threatened | Batch and SLA Agent |

If nobody answers, nothing is changed: the ask escalates to the incident manager and then the
service delivery manager, the breach is predicted hours ahead and the incident becomes P1.

## What the client has to provide

The **Data needed from tools** page is the onboarding checklist: for each tool, the logs,
telemetry, audit data and metadata the agents read, the access that takes, which agent uses
each feed and what that makes possible. For example GoldenGate `ggserr.log` and `INFO ALL`,
Oracle alert log and `DBA_USERS`, Informatica `REP_WFLOW_RUN` and session logs, Snowflake
resource monitors and `QUERY_HISTORY`, dbt `run_results.json` and `sources.json`, CloudWatch,
CloudTrail and the Azure activity log. Everything is read-only status and metadata; no business
rows. The source is `operatewithai/knowledge/signals.yaml`.

## The team

Nine agents, each with a written role, limits and the people it brings in. The full text is in
[`OPERATING_MODEL.md`](OPERATING_MODEL.md), generated from the code and shown in the console.

| Agent | Does | Asks a person when |
|---|---|---|
| Watch Desk | Reads every tool, merges repeats, suppresses alerts during approved changes | An ask is overdue: escalates |
| Triage | One ticket per cause, impact through the data chain, priority, routing | Always: the on-call engineer acknowledges |
| Diagnosis | Known error match and read-only checks with evidence | No known error matches |
| Resolution | Builds the runbook plan, runs approved steps, restarts downstream jobs, verifies | Plan acceptance, every approval-class step, manual steps, confirming resolution |
| Batch and SLA | Projects each morning deliverable through the chain; raises priority | A deadline is at risk; an incident becomes P1 |
| Comms | Drafts stakeholder messages | Always: the incident manager approves every message |
| Service Request | Validates, collects approvals, fulfils access and rerun requests; raises preventive requests | The catalogue calls for the data owner or security lead |
| Problem and Knowledge | Post-incident reviews, problem records for recurring errors, new known errors | Always: the SDM or the resolving engineer approves |
| Service Reporting | Morning health check, shift handover, service review | Never: read-only |

## How it is built

```
operatewithai/
  clients/demo/environment.yaml   the client: systems, data chain, business services and deadlines,
                                  nightly batch, towers, support seats, priority matrix, SLAs, change calendar
  knowledge/kedb.yaml             known errors keyed on real signatures (OGG-01296, ORA-28001, SQL0911N,
                                  DPR-ERR-2109 ...) with diagnostic checks and runbooks
  knowledge/actions.yaml          the only actions an agent may take, with who must approve each
  knowledge/requests.yaml         service request catalogue and approval rules
  knowledge/signals.yaml          the logs, telemetry, audit data and metadata needed from each tool
  connectors/                     parsers for each tool's own output; ServiceNow record mapping
  engine/                         alerts, tickets and SLA clocks, notifications and escalation, batch projection
  agents/                         the nine agents and their role cards
  sim/                            the simulated estate and scenario library
  api/, web/                      FastAPI and the dependency-free console
```

Controls that hold everywhere:

- Nothing changes in the estate until the assigned engineer accepts the plan.
- Approval-class actions (credentials, DDL, cost, data reloads, shared service restarts, access)
  need the named role holder each time. Only the addressed person, or the person it was
  escalated to, can answer a request.
- Agents use only the action catalogue. The "never" list (skipping GoldenGate transactions,
  killing sessions, deleting data, changing code, reading business rows) is not implemented at all.
- Agents work from job status, logs and metadata. Log text is treated as untrusted data.
- Every agent action and human decision is in a hash-chained audit log.

## Patterns taken from open-source projects

Reviewed on GitHub and applied where they fit (the review was limited to what those projects
publish in their repositories):

| Project | Pattern adopted here |
|---|---|
| keephq/keep | Fingerprint de-duplication where firing and resolved states share a fingerprint; maintenance windows that are re-checked when they end (an alert still open after a change window goes to triage) |
| OpenLineage, Marquez | `POST /api/ingest/openlineage` accepts RunEvents (START, COMPLETE, FAIL, ABORT with the errorMessage facet), so any engine that emits them is covered |
| Elementary, Soda, Great Expectations | Data quality failures classified as freshness, volume, schema or test, each mapped to a known error |
| DataHub | A consumer is held back when its input has an open quality incident |
| k8sgpt, HolmesGPT | Deterministic checks first, a model only afterwards and only as a suggestion; read-only investigation; client text redacted before any model call |
| StackStorm, Grafana OnCall | Approvals addressed to a role with a response time, then an ordered escalation chain |
| Arvo-AI/aurora | Fault-injection benchmark: inject a known fault, give the agents only the alerts, grade against the injected cause |

## Using it for a real client

The demo runs against a simulated estate so it can be tried without access to anything. For a
client engagement:

1. Copy the closest client under `clients/` and describe the real systems, lineage, deliverables
   and support towers. Technologies not yet in `knowledge/tech.yaml` are added there.
2. Give the agents the feeds listed on the **Data needed from tools** page (for example `REP_WFLOW_RUN`, GoldenGate Admin Service, dbt Cloud webhooks,
   Power BI refresh history, the Cognos audit database, Dynatrace problem notifications). The
   parsers already accept those tools' formats; the transport and the action adapters are the
   integration work and are not included.
3. Load the client's known errors and runbooks into `knowledge/kedb.yaml` and agree the action
   catalogue with them.
4. Send notifications to the team's channel and tickets to ServiceNow (the record payload is built).

## AI models

Detection, correlation, priority, runbook execution and SLA projection are rules and
runbooks, which is what makes them predictable and auditable. A language model is used only
for six language tasks, each **off by default** and configured on the **AI models** page with
its own provider and model:

| Agent | Task |
|---|---|
| Triage | Suggest the owning team for an alert no known error matches |
| Diagnosis | Hypotheses and next checks for an unknown error |
| Comms | Write the stakeholder message in plain business language |
| Problem and Knowledge | Executive summary of the post-incident review; known error wording from the engineer's notes |
| Service Reporting | Summary paragraph of the shift handover |

Providers: Anthropic (Claude), OpenAI, Azure OpenAI, Google Gemini, Amazon Bedrock and any
OpenAI-compatible endpoint (Ollama, vLLM, a gateway). Model lists are suggestions; any model
id can be typed. Keys are read from environment variables (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`, `GOOGLE_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`,
`OWAI_COMPATIBLE_API_KEY`) and never stored. Model output is always a draft or a suggestion
that the named person reviews; a model never selects or runs an action; on any failure the
rule-based output is used. Prompts carry status, log and ticket text with emails, IP addresses
and long numbers removed. The provider clients are covered by tests with a stand-in model
only: none of them has been run against a live provider API from this repository.

## Integration endpoints

**Data needed from tools > Integration endpoints** lists, for each tool:

- **Connection**: the address the collectors use and the *name* of the credential in the
  client's vault. These are saved; the collectors that pull on a schedule and the adapters
  that carry out actions are the per-client integration work and are not included.
- **Inbound endpoints**: `POST /api/ingest/<feed>` accepts the tool's own format and feeds the
  Watch Desk Agent immediately. Feeds: `oracle`, `oracle_alert`, `goldengate`, `goldengate_log`,
  `informatica`, `db2`, `snowflake`, `dbt`, `denodo`, `powerbi`, `cognos`, `dynatrace`, `aws`,
  `azure`, `mft`, `servicenow_change`. Each has a sample and a "Send sample" button.

```bash
curl -X POST http://127.0.0.1:8090/api/ingest/goldengate_log -H "Content-Type: text/plain" \
  --data "2026-03-03T03:10:00.000-0500  ERROR   OGG-01224  Oracle GoldenGate Capture for Oracle, pmp_erp.prm:  TCP/IP error 111"
```

Settings are kept in `.owai/settings.json` (`OWAI_HOME` to move it); it holds no secrets.

## Security

- `ADEF_API_TOKEN`: when set, every API call needs it as a bearer token.
- `ADEF_IDENTITY_FILE`: YAML of `sha256(token): Roster Name`. Each person then acts only as themselves.
  Without it the console answers as the role each request is addressed to, which is suitable for demos only.

## Earlier version

The first version of this repository was a generic data-product operations framework. Its
specification and documents are kept under [`docs/v1-generic-framework`](docs/v1-generic-framework);
the code is in the git history.
