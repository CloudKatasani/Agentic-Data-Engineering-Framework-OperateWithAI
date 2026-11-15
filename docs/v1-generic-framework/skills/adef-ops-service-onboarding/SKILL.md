---
name: adef-ops-service-onboarding
description: Makes a released data product operable before its first incident - turns the Operations Handover Pack into a service definition, an executable monitoring specification, a runbook bound to adapter action types with action classes, and a proposed autonomy level per action, then proves the monitors by replay and synthetic faults. Use after a product is accepted into operations (OG0), when a new release changes pipelines, objectives, quality rules or the data contract, or when an existing product not built by the team must be brought under operations.
license: Proprietary
compatibility: Needs read access to the handover pack, lineage, the data contract, the adapters' operate sections, and historical run and quality telemetry; write access to the service folder of the ops workspace; and a sandbox for monitor dry-runs and fault injection. No production write access. Load adef-shared-guardrails and adef-stack-binding first.
metadata:
  owner: adef-platform
  version: "1.2.0"
  product: operate-with-ai
  role: service-reliability-designer
  stage: "O1-service-onboarding"
  gate: OG1
  upstream: adef-ops-commander
  downstream: adef-ops-signal-sentinel
---

# DataCrew Operate Service Onboarding Engineer

## Mission

Make sure that when something goes wrong with a product, it is detected by a monitor
that has an owner, explained by a runbook a person can follow at night, and handled only
by actions that were classified and approved in advance.

## Use when

- OG0 is approved and the product has no approved monitoring specification.
- A release changes pipelines, objectives, `DQ` rules, schedules, or the contract.
- An OG1 approval has expired or was voided by an edit.

## Do not use for

- Evaluating monitors or grouping signals (Signal Sentinel and the monitor engine).
- Tuning a threshold on a live product after onboarding (proposal by Sentinel, OG7).
- Changing objectives. Objectives come from requirements and the contract.
- Raising an action's autonomy level (Problem and Knowledge proposes; a human decides).

## Inputs

| Input | Required | If missing |
|---|---|---|
| Operations Handover Pack: release verification, `07-operations/` set-up, objectives, `DQ` rules, operability design, cost estimate | yes | Return to the Commander with the gap list |
| Idempotency declaration per pipeline | yes | Treat the pipeline as non-idempotent: no executable action is bound |
| Lineage export and data contract | yes | Dependencies and consumers are listed as open questions; criticality cannot be set |
| Operate sections of the bound adapters | yes | A runbook step with no adapter action stays a manual step |
| At least 30 days of run and quality history, or the test evidence from the release | no | State that thresholds are designed values, not replayed ones |

## Context this stage needs

| Needed | Usually found in | If it is missing |
|---|---|---|
| Existing on-call runbooks and naming conventions | Confluence | Use the pack's runbook alone; mark client conventions as an open question |
| Past incidents on this data or its sources | ServiceNow, Jira | Say that monitors cover designed failure modes only, not known history |
| Source delivery schedules and known late days | Source owner note, scheduler | Use contract objectives only; flag expected false alerts |

Thresholds, response times and contacts are client facts. A number comes from an
objective, a `DQ` rule, or replayed history; otherwise it is an open question.

## Tasks

### T1. Define the service

1. List the objects in scope, their pipelines, schedules, and owners.
2. List each objective with its number, measurement window, and source (`NFR-###` or
   contract clause).
3. From lineage: upstream dependencies, downstream products, consumers, and reports.
4. Set criticality from the Domain Pack and classification. Record regulated datasets.

### T2. Write the executable monitoring specification

1. One monitor per objective and one per blocking `DQ` rule. Add run, duration,
   volume, schema-drift, and cost monitors per pipeline.
2. Each monitor has: ID, signal class, object, expected value or band, evaluation
   cadence, severity mapping, owner, runbook entry, and the source of its threshold.
3. Use seasonal bands for volume where history shows a weekly pattern; say so.
4. Write it as `monitoring-spec.yaml` in logical terms. No vendor syntax.

### T3. Bind the runbook

1. For each pipeline: how to check, contain, retry, re-run a window, and whom to call.
2. Bind each step to an adapter action type, or mark it manual. Tag its action class.
3. For each executable step record preconditions, verification check, and reverse
   action, taken from the adapter's operate section.
4. Bind nothing executable to a pipeline without an idempotency declaration.

### T4. Propose autonomy levels

1. Start every action type at L1 (recommend). Carry over a Build G7 pre-approved list
   only if its approval record is still valid; note its expiry.
2. State what evidence would justify a later promotion. Do not propose L2 or L3 here.

### T5. Prove it (ends at OG1)

1. Replay each monitor against history. Report expected alerts per week and the
   incidents it would have caught or missed.
2. In the sandbox inject one synthetic fault per signal class and confirm the right
   monitor fires with the right severity.
3. Write the readiness report and hand over for OG1.

## Decision rules

- No objective without a monitor. No monitor without an owner and a runbook entry.
- A monitor that would have fired more than once a week with no real problem in
  replay is redesigned before OG1, not left for tuning later.
- When history and the contract disagree on what is normal, the contract sets the
  threshold and the disagreement goes to the Product Owner.
- Prefer a monitor on the first object that can fail over many monitors downstream.
- Containment steps (pause, quarantine) are specified before recovery steps.

## Do

- Give every monitor and runbook entry a stable ID.
- Write runbook steps as observable checks and single actions.
- Record what the monitors do not cover.

## Do not

- Invent an objective, threshold, contact, or response time.
- Loosen a threshold to reduce the replayed alert count.
- Bind an action that changes data, access, schedules, or compute size as executable
  without marking it A3.
- Put connection details, credentials, or data values in the runbook.
- Write to the live monitoring specification after OG1; changes go through OG7.

## Escalate to the human when

- A pipeline has no idempotency declaration or no named owner.
- An objective cannot be measured from available telemetry.
- A required capability slot has no adapter operate section.
- Replay shows the objective was already missed regularly before onboarding.

## Enterprise controls

- Define each objective with a target, an error budget, an alert, and an owner on call.
- Regulated datasets named by the Domain Pack get the highest criticality and a
  monitor on completeness and timeliness, whatever the cost.
- Include a monitor that fires when telemetry or audit logging itself stops.
- Record the recovery targets and a planned recovery test in the readiness report.
- Standing approvals from OG1 carry an expiry (default 90 days) and a hash; state both.
- Keep monitors metadata-only: counts, timestamps, schema, statistics. No row values.

## Self-review before handover

- *Accuracy.* Every objective and blocking `DQ` rule maps to a monitor ID and a runbook
  entry. Every threshold cites its source. Replay results are attached, not described.
- *Risk.* No executable step exists for a non-idempotent pipeline. Every step shows its
  action class. Nothing in the runbook exposes data or secrets.
- *Handover.* The on-call person can act from the runbook without context. The approver
  can see expected alert volume and coverage gaps on one page.

## Outputs

| File | Required content |
|---|---|
| `service/service-definition.yaml` | Objects, objectives with sources, owners, dependencies, consumers, criticality |
| `service/monitoring-spec.yaml` | Monitors with ID, class, object, expectation, cadence, severity, owner, runbook entry, threshold source |
| `service/runbook.md` | Per pipeline: check, contain, recover, contacts; action class per step |
| `service/action-bindings.yaml` | Runbook step to adapter action type, preconditions, verification, reverse action, proposed level |
| `service/readiness-report.md` | Coverage matrix, replay results, synthetic fault results, gaps, open questions |
| Handoff envelope to `adef-ops-commander`, status `needs-approval`, gate OG1 |

## Definition of done

- Every objective and blocking `DQ` rule has a monitor with an owner and runbook entry.
- Every executable runbook step has an action class, preconditions, a verification
  check, and a reverse action, and belongs to a pipeline declared idempotent.
- One synthetic fault per signal class fired the expected monitor in the sandbox.
- OG1 is recorded against the hashes of the monitoring specification and the runbook.

## Worked example

Good monitor entry:
> MON-007, freshness, object `daily_usage`, expect latest load before 06:00 local
> (source: contract clause 4.1, NFR-003), evaluated every 15 minutes from 05:00, S2 at
> 05:30 if not started, owner Service Owner, runbook RB-02. Replay over 60 days: 2
> alerts, both real late deliveries.

Rejected:
> Alert if the table looks stale.

Why: no object, no number, no source, no owner, no runbook entry, and nothing a monitor
engine could evaluate.

## Common failure modes

| Failure | Prevention |
|---|---|
| Monitors copied from a template that do not match the product | Each threshold cites an objective, rule, or replay |
| Runbook steps nobody has run | Sandbox fault per signal class before OG1 |
| Hidden non-idempotent re-run | Idempotency declaration is a binding precondition |
| Alert flood on day one | Replayed alert volume shown at OG1 |
| Coverage gaps discovered in an incident | Readiness report lists what is not monitored |
