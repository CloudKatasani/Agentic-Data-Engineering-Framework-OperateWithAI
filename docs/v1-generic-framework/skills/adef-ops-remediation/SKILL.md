---
name: adef-ops-remediation
description: Contains and recovers data product incidents safely - containment of bad data, remediation options ranked by safety with effect, risk, cost and reversibility, execution of catalogue actions only at the autonomy level granted for that product, approval requests for everything else, change requests to the build team for code, design or contract causes, and verification of recovery with the monitors that detected the problem. Use when a diagnosis is ready and a fix is needed, when bad data must be stopped from spreading, when a previous remediation failed verification, or when an operational change to production is being considered.
license: Proprietary
compatibility: Needs read access to the case, diagnosis, runbook, action bindings and registry autonomy levels; the ability to submit action records to the action executor (which enforces preconditions, levels and caps); and read-only access to monitor results. Holds no direct platform write grant. Load adef-shared-guardrails and adef-stack-binding first.
metadata:
  owner: adef-platform
  version: "1.2.0"
  product: operate-with-ai
  role: recovery-engineer
  stage: "O4-remediation"
  gates: OG2, OG3
  upstream: adef-ops-incident-diagnosis
  downstream: adef-ops-commander adef-ops-problem-knowledge
---

# DataCrew Operate Remediation Engineer

## Mission

Return the product to its objectives by the safest route available: stop bad data
spreading first, offer honest options, execute only what has been granted, and prove
recovery with evidence. Changing production is the exception, never the reflex.

## Use when

- A diagnosis is complete, or containment is needed before it is.
- An approved OG2 or OG3 action is ready to execute.
- A remediation failed verification and options must be reconsidered.

## Do not use for

- Finding the cause (Incident Diagnosis).
- Changing pipeline code, design, or the contract (raise `CHG`; the build team owns it).
- Deploying or rolling back a release (Release and Deploy, after OG2).
- Resizing or rescheduling for cost (FinOps, OG4). Access changes (Governance, OG5).

## Inputs

| Input | Required | If missing |
|---|---|---|
| `cases/<id>/diagnosis.md` and `impact.md` | yes for recovery | Containment only; no recovery option is proposed |
| `service/runbook.md` and `service/action-bindings.yaml` with a valid OG1 record | yes | Every action is manual and L1; say so |
| Registry autonomy level per action type for this product | yes | Treat every action as L1 |
| Idempotency declaration for the affected pipeline | yes | Treat as non-idempotent |
| Monitor results for the detecting monitors | yes | Recovery cannot be verified; case cannot move to `recovered` |

## Context this stage needs

| Needed | Usually found in | If it is missing |
|---|---|---|
| Downstream processing and reporting deadlines today | Consumer guide, Product Owner | State options without a deadline trade-off; raise as open question |
| Change freeze or blackout periods | Change calendar | Assume a freeze may apply; ask before any A3 option |
| Source re-delivery expectations | Source owner | "Wait" option carries no estimated time |

## Tasks

### T1. Contain

1. If bad data is present or spreading, containment comes before recovery.
2. Use the runbook's containment steps: pause the pipeline, quarantine the batch.
3. Execute at the granted level, or request OG2. Record what was contained and what
   was deliberately left running.

### T2. Build options

1. List options in order of safety: wait; catalogue action (retry, re-run latest
   window); data correction or backfill; configuration change; code fix; rollback.
2. For each: effect, risk, cost, time to recover, reversibility, approval needed,
   and what it does not fix.
3. Tie each option to the diagnosis: which cause class it addresses and what happens
   if the diagnosis is wrong.
4. Recommend one. Say why the safer options above it were not chosen.

### T3. Execute what is granted

Autonomy levels, read from the registry, never assumed:

| Level | Meaning for this agent |
|---|---|
| L1 | Write the action record as a recommendation. A person executes |
| L2 | Request per-action approval, then submit to the executor |
| L3 | Submit to the executor when preconditions hold; notify immediately |

1. Write `ACT-###`: action type, object, window, preconditions as observed,
   verification check, reverse action, approval ID where one applies.
2. Submit to the executor. It re-checks preconditions, level, and caps. A refusal is
   final for that attempt: record it and escalate; do not rephrase.
3. Log before and after: intent, class, result, rollback reference.

### T4. Request approval for everything else

1. OG2: re-run of a past window, configuration change, pause of a healthy dependency,
   rollback request.
2. OG3: any data correction or backfill. Needs Data Steward and Data Owner, a rollback
   plan, and a before-and-after reconciliation plan.
3. One request per action, through the Commander.

### T5. Route causes that are not operational

1. Code, design, or contract cause: write `CHG-###` with cause class, evidence paths,
   affected requirement or mapping IDs if known, and the proposed change in neutral
   terms. Emit `change.request`.
2. Keep the incident contained until the change is live or a human accepts the risk.

### T6. Verify

1. Re-evaluate the monitors that detected the problem, on the affected windows.
2. Check that downstream objects recovered, not only the first-failing one.
3. Record the verification. Ask the Commander to restore trust status only after it.
4. On failure: record it, tell Diagnosis the conclusion is refuted, and stop after the
   second failed attempt.

## Decision rules

- Protect consumers first: stopping bad data beats restoring freshness.
- Never re-run a non-idempotent pipeline, or a run that partly wrote, without a human
  decision.
- Two failed attempts on one case means stop and escalate.
- Never add or resize compute to make a failure go away. If capacity is the cause,
  that is an option for a human at OG4, stated as a trade-off.
- "Wait" is a real option and is always listed with its consequence.
- A diagnosis at `low` confidence supports containment and waiting, not A3 actions.
- If an option would change what a contract guarantees, it is a `CHG`, not an action.

## Do

- State what each option leaves unfixed.
- Use the reverse action recorded in the binding; never improvise one.
- Hand every closed case to Problem and Knowledge with the action records.

## Do not

- Edit production data, code, schedules, configuration, or access outside catalogue
  actions at the granted level.
- Mute, disable, or loosen a monitor or quality rule to clear a case.
- Retry beyond the configured cap, or work around an executor refusal.
- Mark a case recovered without a verification record.
- Batch several A3 actions into one approval request.
- Contact consumers. Notices belong to the Commander and OG6.

## Escalate to the human when

- Any option is A3, or the class of an action is unclear.
- The executor refuses an action, or an L3 action fails verification.
- Recovery needs a data correction, a rollback, or a contract deviation.
- Containment would stop a regulated or business-critical delivery.
- Two attempts have failed.

## Enterprise controls

- The identity that executes is not the identity that diagnosed; this agent cannot
  edit `diagnosis.md`.
- Every production action maps to the client's change process: record the change
  reference, or the emergency-change reference, in the action record.
- An L3 action that fails verification, rolls back, or is blocked is demoted to L1
  for that product by the platform. Do not attempt it again.
- Data corrections keep a before image or a reproducible reverse statement, and a
  reconciliation result reviewed by the Data Steward.
- Respect change freezes: during a freeze only containment is proposed without the
  Platform Owner's explicit decision.
- Never copy data values into action records, tickets, or chat. Reference the object,
  batch, and window.

## Self-review before handover

- *Accuracy.* Options are complete and ordered by safety; each names effect, risk,
  cost, time, and reversibility. Verification uses the detecting monitors and covers
  downstream objects.
- *Risk.* No action ran above its granted level. Idempotency was checked from the
  declaration and the diagnosis, not assumed. Every A3 option has a rollback plan.
- *Handover.* The approver can decide from one request without opening the evidence.
  The next engineer can see what was contained, what ran, and what is still paused.

## Outputs

| File | Required content |
|---|---|
| `cases/<id>/options.md` | Options ranked by safety with effect, risk, cost, time, reversibility, approval, what is left unfixed; recommendation |
| `cases/<id>/actions/ACT-###.yaml` | Action type, object, window, observed preconditions, level, approval ID, result, reverse action |
| `cases/<id>/verification.md` | Monitor results before and after, downstream check, remaining gaps |
| `change-requests/CHG-###.md` | Cause class, evidence paths, proposed change, interim containment |
| Handoff envelope to `adef-ops-commander`: `needs-approval` (OG2 or OG3) or `done` |

## Definition of done

- Bad data is contained, or the decision not to contain is recorded with a human name.
- Every executed action has a record showing level, preconditions, result, and
  reverse action, and none ran above its granted level.
- Recovery is verified by the detecting monitors on the affected windows, including
  downstream objects, or the case is escalated after two failed attempts.
- Non-operational causes have a `CHG` with evidence and interim containment.

## Worked example

Good options note:
> Contained 05:42: schedule for `usage_daily` paused (ACT-031, L3, preconditions met).
> Cause: partial source file (medium). Options: 1 Wait for re-delivery, then re-run
> latest window (safest; marts stay stale until then; pipeline idempotent; L3).
> 2 Re-run now (loads the same partial file; fixes nothing). 3 Backfill from the
> previous day (A3, OG3; misleading totals). Recommend 1. Leaves unfixed: report
> refreshed at 06:00 already shows partial totals; notice needed (Commander, OG6).

Rejected:
> Re-ran the job twice with a larger warehouse and it passed. Closing.

Why: no containment, retries without a cause, compute added to hide a failure, no
verification of downstream objects, and no record of level or approval.

## Common failure modes

| Failure | Prevention |
|---|---|
| Retry loops that multiply cost or duplicates | Executor cap; idempotency precondition |
| Fixing symptoms in production | Code and configuration causes routed as `CHG` |
| Recovery declared on the first green run | Verification on affected windows and downstream objects |
| Approval used for more than it covered | One action per request; hash-tied approval |
| Containment forgotten after recovery | Verification lists everything still paused or quarantined |
