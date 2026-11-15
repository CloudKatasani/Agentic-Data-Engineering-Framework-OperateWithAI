# DataCrew- OperateWithAI

AI agents and a human team running a **data platform managed service** together: monitoring,
incidents, the nightly batch, service requests and service reporting for a client estate of
**Oracle, IBM Db2, Oracle GoldenGate, Informatica, Snowflake, dbt, Denodo, Power BI, Cognos,
Dynatrace, AWS/Azure and file-transfer integrations**, with ServiceNow as the system of record.

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

Open http://127.0.0.1:8090. It starts a simulated night at a demo client at 01:20 with the
"typical bad night" preset: an expired Oracle service account, a duplicate vendor file and,
later, a Power BI gateway outage. Choose which seat you are in the sidebar (Oracle DBA, Incident Manager, Security Lead ...), answer that inbox, and
move the clock with the buttons at the top.

The sidebar follows the demo order: **1 Agents and people** (who does what), **2 Data needed
from tools** (what the client provides), **3 Command center** (the live night), **4 My inbox**
(answer as the Oracle DBA, then as the Security Lead). The **Scenario simulator** page starts other nights.

```bash
python -m operatewithai scenarios                      # the ten scenarios
python -m operatewithai night --scenarios S01,S06      # run a night in the terminal with nobody answering
pytest -q
```

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

## Using it for a real client

The demo runs against a simulated estate so it can be tried without access to anything. For a
client engagement:

1. Replace `clients/demo/environment.yaml` with the client's systems, lineage, deliverables,
   support seats and SLAs.
2. Give the agents the feeds listed on the **Data needed from tools** page (for example `REP_WFLOW_RUN`, GoldenGate Admin Service, dbt Cloud webhooks,
   Power BI refresh history, the Cognos audit database, Dynatrace problem notifications). The
   parsers already accept those tools' formats; the transport and the action adapters are the
   integration work and are not included.
3. Load the client's known errors and runbooks into `knowledge/kedb.yaml` and agree the action
   catalogue with them.
4. Send notifications to the team's channel and tickets to ServiceNow (the record payload is built).

Agent decisions here are rules and runbooks, which is what makes them predictable and
auditable. A language model is optional and narrow: with `OWAI_LLM=1` and Anthropic
credentials, the Diagnosis Agent asks it for hypotheses when no known error matches, and shows
the answer as an unverified suggestion. It is off by default and has not been exercised against
the live API in this repository's tests.

## Security

- `ADEF_API_TOKEN`: when set, every API call needs it as a bearer token.
- `ADEF_IDENTITY_FILE`: YAML of `sha256(token): Roster Name`. Each person then acts only as themselves.
  Without it the caller names themselves (`X-Actor`), which is suitable for demos only.

## Earlier version

The first version of this repository was a generic data-product operations framework. Its
specification and documents are kept under [`docs/v1-generic-framework`](docs/v1-generic-framework);
the code is in the git history.
