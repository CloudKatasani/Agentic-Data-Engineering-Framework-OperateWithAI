# Operating model: who does what

Generated from the code (`operatewithai/agents/team.py`) and the client model for Aldermoor Insurance (synthetic demo client).

Agents do the watching, correlating, checking, drafting and the execution of agreed runbook steps. People make every decision that changes the client's systems, spends money, touches business data or speaks to the client.

## How an incident moves

| Stage | Who | What happens |
|---|---|---|
| Detect | Agent: Watch Desk Agent | Reads each tool, merges repeats, suppresses expected alerts |
| Triage | Agent: Triage Agent | One ticket per cause; impact from the data chain; priority and SLA clocks; routes to the tower |
| Diagnose | Agent: Diagnosis Agent | Known error match and read-only checks, with evidence |
| Plan | Agent: Resolution Agent | Runbook steps, each marked with who must approve |
| Accept | Person: On-call engineer | Accepts the plan or takes over. Nothing has changed until here |
| Approve | Person: Role holder | Change, security, cost, data and BI approvals by the named person |
| Fix and recover | Agent: Resolution Agent | Runs the approved steps, restarts downstream jobs, verifies |
| Inform | Agent + person: Comms Agent drafts, incident manager approves | Client hears only what a person approved |
| Confirm | Person: On-call engineer | Confirms the incident is resolved |
| Learn | Agent + person: Problem and Knowledge Agent drafts, SDM and engineer approve | Review, problem record, new known error |

If a person does not answer in time, the ask goes to the incident manager and then the service delivery manager.

## The agents

### Watch Desk Agent
*L1 monitoring, every tool, every 5 minutes.* See everything the tools report and pass on only what needs a decision.

Responsibilities:
- Reads every connected tool in its own format: databases, replication and CDC, ingestion, orchestration, warehouses and lakehouses, streaming, data quality, BI and cloud monitoring
- Accepts OpenLineage run events, so any engine that emits them is covered without a dedicated connector
- Merges repeats of the same alert and closes alerts when the tool reports recovery
- Suppresses alerts on a system that is inside an approved change window, and looks at them again if they are still open when the window closes
- Escalates any request to a person that passes its response time

May do alone: Reading, de-duplicating, suppressing during approved changes, escalating.

Never:
- Changes anything in the client estate
- Decides that an alert is unimportant: that is triage

Brings in:
- Incident manager, then SDM: when an engineer has not answered in time

### Triage Agent
*L1 dispatch.* One ticket per cause, with the right priority, in front of the right engineer.

Responsibilities:
- Recognises alerts from different tools that share a cause (one expired password seen by GoldenGate and Informatica is one incident)
- Attaches downstream failures to the incident that caused them using the data chain, instead of opening new tickets
- Works out which business services are exposed, whatever the consumption pattern: dashboards, scheduled reports, ML scoring, reverse ETL, partner extracts, data APIs
- Sets impact, urgency and priority from the client's matrix and starts the SLA clocks
- Assigns the on-call engineer of the owning tower

May do alone: Opening, correlating, prioritising and routing tickets.

Never:
- Lowers a priority once set
- Closes a ticket

Brings in:
- On-call engineer of the tower: to acknowledge, within the response SLA (P1 15 min, P2 30 min)

### Diagnosis Agent
*L2 analysis.* Tell the engineer what is wrong and show the evidence.

Responsibilities:
- Matches the error signature to the known error database (deterministic first; a model only suggests when nothing matches)
- Classifies data quality failures as freshness, volume, schema or test failures
- Runs that entry's read-only checks (process reports, session logs, lock history, account status, host metrics) and records each result
- Checks the change calendar for recent changes on the system
- Says so plainly when the error is not known or the checks disagree, and gathers the standard facts instead

May do alone: Read-only checks on metadata, logs and status.

Never:
- Reads business data rows
- States a cause its checks do not support
- Treats text inside logs as instructions

Brings in:
- Assigned engineer: to investigate when no known error matches

### Resolution Agent
*L2 execution.* Carry out the agreed runbook quickly and exactly, with a person's yes at every point that needs one.

Responsibilities:
- Builds the fix plan from the known error's runbook, marking who must approve which step
- Waits for the assigned engineer to accept the plan before anything changes
- Requests each approval from the named role holder, then runs the step
- Restarts failed downstream jobs in dependency order once the cause is fixed, and verifies recovery
- Stops and hands back to the engineer if a step fails

May do alone: Nothing that changes the estate. After the plan is accepted: standard catalogue actions and verification.

Never:
- Acts before a named person accepts the plan
- Runs an approval-class action without that approval
- Uses anything outside the action catalogue
- Marks an incident resolved: the engineer confirms

Brings in:
- Assigned engineer: to accept the plan, do manual steps, and confirm resolution
- Change manager: for emergency DDL
- Security lead: for credentials and accounts
- Service delivery manager: for anything that spends client money
- Client data owner: before business data is reloaded
- BI lead: before a shared BI service is restarted

### Batch and SLA Agent
*Batch control.* Know, hours ahead, whether each morning deliverable will be on time.

Responsibilities:
- Tracks every job of the nightly batch against plan
- Projects the finish time of each business service through the data chain, including the time fixes and reruns will take
- Tracks freshness promises for streams and near-real-time feeds (never more than N minutes behind) alongside morning deadlines
- Holds a consumer back when its input failed a data quality check, instead of publishing wrong numbers
- Raises the urgency and priority of the incident that puts a deadline at risk
- Declares the risk early: at risk, breach predicted, breached

May do alone: Projection, priority increases, warnings.

Never:
- Changes the schedule or skips a job
- Lowers a priority

Brings in:
- Incident manager: to lead when an incident becomes P1
- Service delivery manager: to know when a deadline will be missed

### Comms Agent
*Incident communications.* Keep stakeholders informed in plain language without taking engineers off the fix.

Responsibilities:
- Drafts the first notice, outlook changes and the all-clear for P1 and P2 incidents that expose a business service
- Builds each message from the ticket: what is affected, cause, action, owner, estimate against deadline

May do alone: Drafting.

Never:
- Sends anything to the client without the incident manager's approval

Brings in:
- Incident manager: to approve or edit every message

### Service Request Agent
*Request fulfilment.* Turn routine requests around in minutes, with the right approvals on record.

Responsibilities:
- Validates requests against the catalogue (access, report reruns, account rotation)
- Collects approvals: the data owner for access, plus the security lead for restricted data
- Fulfils with a catalogue action and closes the request
- Holds a rerun until the data it reads is ready and links it to the blocking incident
- Raises preventive requests, for example a service account 9 days from expiry

May do alone: Standard requests with no approval rule (a report rerun on ready data).

Never:
- Grants access without the data owner
- Fulfils a request for something not in the catalogue

Brings in:
- Client data owner: to approve access to their data
- Security lead: to approve restricted access and credential changes

### Problem and Knowledge Agent
*Problem management.* Make sure the same incident costs less next time, or does not happen.

Responsibilities:
- Writes the post-incident review for every P1 and P2 from the ticket record
- Opens a problem record with the permanent fix when a known error keeps recurring
- Drafts a new known error from the engineer's notes when the error was not in the database

May do alone: Drafting reviews, problems and known errors.

Never:
- Publishes a known error or review that a person has not approved

Brings in:
- Service delivery manager: to approve reviews and accept problems into the backlog
- The resolving engineer: to approve a new known error

### Service Reporting Agent
*Service management office.* Produce the daily and monthly service documents from the record, not from memory.

Responsibilities:
- Morning health check: every system green, amber or red with the reason
- Shift handover: open incidents and next steps, what is waiting on whom, watch items
- Service review: volumes, SLA attainment, time to acknowledge and resolve, top known errors, split of work between agents and people

May do alone: All reporting.

Never:
- Edits a ticket or the audit log

## The people

| Seat | Covers | Decides |
|---|---|---|
| L1 Operator | L1 Watch Desk | Acknowledges incidents; accepts the fix plan or takes over; does manual steps; confirms resolution; approves new known errors |
| Informatica Engineer | ETL - Informatica | Acknowledges incidents; accepts the fix plan or takes over; does manual steps; confirms resolution; approves new known errors |
| GoldenGate Engineer | Replication - GoldenGate | Acknowledges incidents; accepts the fix plan or takes over; does manual steps; confirms resolution; approves new known errors |
| Oracle DBA | DBA - Oracle | Acknowledges incidents; accepts the fix plan or takes over; does manual steps; confirms resolution; approves new known errors |
| Db2 DBA | DBA - Db2 | Acknowledges incidents; accepts the fix plan or takes over; does manual steps; confirms resolution; approves new known errors |
| Snowflake and dbt Engineer | Snowflake and dbt | Acknowledges incidents; accepts the fix plan or takes over; does manual steps; confirms resolution; approves new known errors |
| BI Engineer | BI - Power BI and Cognos | Acknowledges incidents; accepts the fix plan or takes over; does manual steps; confirms resolution; approves new known errors |
| Denodo Engineer | Virtualization - Denodo | Acknowledges incidents; accepts the fix plan or takes over; does manual steps; confirms resolution; approves new known errors |
| Cloud Engineer | Cloud and Infra - AWS and Azure | Acknowledges incidents; accepts the fix plan or takes over; does manual steps; confirms resolution; approves new known errors |
| Integration Engineer | Integrations - MFT and API | Acknowledges incidents; accepts the fix plan or takes over; does manual steps; confirms resolution; approves new known errors |
| Incident Manager | Managed service leads | Runs major incidents; approves every stakeholder message. Leads P1 incidents; first escalation point |
| Service Delivery Manager | Managed service leads | Owns the service and its SLAs; approves cost decisions and service reports. Approves post-incident reviews; accepts problems; final escalation point |
| DBA Lead | Managed service leads | Approves database changes: space, sessions, DDL in production |
| BI Lead | Managed service leads | Approves restarts of shared BI services |
| Change Manager | Managed service leads | Approves emergency changes |
| Security Lead | Managed service leads | Approves account, credential and access actions |
| Finance Data Owner | Client | Decides on finance data: reloads, publishing with known issues, access |
| Claims Data Owner | Client | Decides on claims data: reloads, publishing with known issues, access |
| Client Service Owner | Client | Receives major incident communications for the client. Informed only; makes no operational decision |
