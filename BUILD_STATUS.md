# Build status - Operate-With-AI

Against `OPERATE_SPEC.md` v0.1. The ten-day plan is followed in order: Epic O2 (control and
enforcement) is in place before any identity gets a tool that can change anything
(START HERE step 3). The fault library runs in the simulator before any real telemetry is
connected (step 4). Every action type starts at L1 (step 5).

## Backlog

| ID | Task | State | Proof |
|---|---|---|---|
| O1-1 | Repository and package, skills validated | Done (adef-core stubbed, see A1) | `tests/test_skills.py`, `python -m adef_operate.skills_check` |
| O1-2 | Ops workspace and write allowlists | Done | `test_diagnosis_cannot_write_actions`, `test_remediation_cannot_write_diagnosis`, allowlist matrix |
| O1-3 | Case store with lifecycle state machine | Done | `test_illegal_transition_has_specific_message` |
| O1-4 | SIG, case, ACT schemas | Done | `test_invalid_signal_rejected`, `test_invalid_case_and_act_rejected` |
| O1-5 | Receiver check and `product.released` consumer | Done | `test_pack_missing_idempotency_rejected_with_all_gaps` |
| O2-1 | Autonomy levels and hook check | Done | `test_l1_action_cannot_execute`, `test_unknown_action_type_treated_as_a3_l1` |
| O2-2 | Executor with precondition re-check and caps | Done | `test_ot04_...`, `test_ot01_...` |
| O2-3 | Standing approvals with expiry and hash | Done | `test_ot11_runbook_edit_voids_og1`, `test_og1_expires_after_90_days` |
| O2-4 | Earned autonomy and automatic demotion | Done | `test_ot12_failed_verification_demotes_to_l1` |
| O2-5 | Close and recover guards | Done | `test_close_rejected_when_monitor_disabled`, `..._threshold_loosened` |
| O2-6 | Daily budgets and emergency stop | Done | `test_ot15_...`, `test_action_count_budget`, `test_emergency_stop` |
| O3-1 | Monitor engine reading the approved spec | Done | `test_each_fault_raises_the_right_signal_classes`, `test_engine_refuses_unapproved_or_edited_spec` |
| O3-2 | Collector and adapter operate sections for the launch stack | Contract and simulator adapter done; real adapter blocked on START HERE Q2 | - |
| O3-3 | Fault injector and the eight library faults | Done | `test_faults_are_deterministic_and_resettable` |
| O3-4 | Rebind to a second profile | Not started | - |
| O4-1 | Commander and Onboarding to OG1 | Done (Day 6) | `test_released_golden_product_becomes_operable`, `test_dry_run_alert_volume_is_deterministic` |
| O4-2 | Sentinel | Done (Day 6) | `test_f03_one_group_not_one_per_downstream_table`, `test_one_group_per_fault` (12 fault and pack pairs) |
| O4-3 to O4-7 | Diagnosis, Remediation, FinOps, Governance, Problem and Knowledge | Not started | - |
| O5-1 to O5-4 | Evaluation and launch | Not started | - |
| Section 11 | API and console | Not started | - |

**Day 2-3 exit check** (OT-01, OT-04, OT-11, OT-12, OT-15): all pass.
**Day 4-5 exit check** (all eight faults raise the right signals): passes on both packs.
**Day 6 exit check** (OG1 reached; one group per fault): passes. All 12 fault and pack pairs give
exactly one group with the right first-failing object, and no signal is dropped.

## Day 6: what the agents do

The Day 6 agents (`adef_operate/agents/`) are rule-based runtimes of their skill contracts,
because the spec decides severity, grouping and onboarding by rule. They write only
inside their allowlists, and no model is served yet (the health summary says so).

- **Onboarding** (`agents/onboarding.py`) does not start until OG0 is approved. It drafts
  the service definition, monitoring spec, runbook and action bindings, then applies its
  decision rules: no monitor without an owner, no objective without a monitor, and no
  executable re-run on a non-idempotent pipeline. It dry-runs every monitor over 28 days of
  simulated history (expected alerts per week), proves each library fault in a sandbox,
  and requests OG1.
- **Sentinel** (`agents/sentinel.py`, `engine/correlator.py`) groups signals by product,
  lineage and time. The most upstream object is the first-failing object. Every signal is
  labelled `primary`, `downstream-echo` or `duplicate`, and none is dropped. A blocking
  quality failure or schema drift heads a group of its own. Repeats on later ticks join the
  same group as duplicates. Upstream notices and recent releases are attached as leads.
- **Commander** (`agents/commander.py`, `engine/severity.py`) searches before opening: a
  group with the same first-failing object or detecting monitor is attached to the
  existing case. A case that only shares the lineage neighbourhood is linked, with the
  reason. Severity is set by rule and the rule is recorded with what would lower it;
  Domain Pack overrides can only raise it. Trust status goes through the executor
  (`set_trust_status`). On-call is paged for S1 and S2. S1 asks for council review, and
  blocking quality or schema groups ask Remediation to contain. Cost groups route to
  FinOps. Access groups route to Governance and are escalated at once to Security and
  Compliance, with no agent action. S4 groups are batched for the weekly review. Nothing
  is routed while stopped. The Commander also publishes `health/<date>.md`.
- **Runtime** (`runtime.py`): one tick runs the monitor engine, then Sentinel, then Commander.

## Guardrail scenarios (section 13.2)

| # | Covered by enforcement code now | Test |
|---|---|---|
| OT-01 | Yes: executor never re-runs a non-idempotent pipeline | `test_ot01_non_idempotent_rerun_blocked` |
| OT-02, OT-03 | Partly: monitors can only change through a new OG1 hash; the refusal itself is agent behaviour | `test_engine_refuses_unapproved_or_edited_spec` |
| OT-04 | Yes | `test_ot04_third_retry_blocked_and_escalated` |
| OT-05 | No: needs the diagnosis identity and profiling tool | - |
| OT-06 | Detection yes (injection scanner labels the signal); agent handling pending | `test_ot06_injection_in_logs_is_data` |
| OT-07 | Partly: `resize` is A3 and needs OG4; the refusal as a fix is agent behaviour | - |
| OT-08 | Yes | `test_ot08_drop_from_telemetry_alone_blocked` |
| OT-09 | Executor side yes (`revoke_access` is never executable); escalation routing is the agent's job | `test_ot09_revoke_never_executed_by_agent` |
| OT-10 | Yes | `test_ot10_edited_notice_blocked` |
| OT-11 | Yes | `test_ot11_runbook_edit_voids_og1` |
| OT-12 | Yes | `test_ot12_failed_verification_demotes_to_l1` |
| OT-13 | Yes | `test_two_failed_attempts_stop_and_escalate` |
| OT-14 | Yes | `test_close_needs_post_incident_note` |
| OT-15 | Yes | `test_ot15_model_budget_exceeded_sets_l0` |
| OT-16 | Not yet: workspace isolation is per product; client isolation needs the multi-client registry | - |

## Assumptions made during the build (please confirm)

| # | Assumption | Where |
|---|---|---|
| A1 | `SUITE_SPEC.md` and `adef-core` are not in this repository. `adef_operate/core/` is a minimal stand-in (hashing, hash-chained log, levels, errors) to replace with adef-core. | `core/` |
| A2 | Autonomy levels: L0 stopped; L1 recommend only (executor refuses); L2 execute with a per-action gate approval; L3 execute within OG1 standing approval when preconditions pass. Classes: A1 read-only, A2 reversible, A3 high-impact. | `core/autonomy.py` |
| A3 | Gate approvers. All named roles must approve: OG0 (Product and Platform Owner), OG3 (Data Steward and Data Owner), OG7 (Platform Owner and Tech Lead). Any one is enough for OG2 (Service or Platform Owner). Rollback uses OG2 with the Release Manager. | `engine/approvals.py` |
| A4 | The retry cap is 2 per object per day, counted across all re-run types. | `connectors/base.py` limits |
| A5 | The executor never re-runs a non-idempotent pipeline, even with OG2. People execute that re-run. | `engine/executor.py` |
| A6 | "Monitor unchanged" at close means that monitor's own definition hash is unchanged. Other monitors in the spec can change. | `engine/case_store.py` |
| A7 | Incident state machine extras: `failed -> options-ready or diagnosing`; `options-ready -> verifying` for the "wait" option. | `engine/case_store.py` |
| A8 | Default daily budgets: model 50, diagnostic query 20, actions 20. A pack can override them. Warn at 80%. | `engine/handover.py` |
| A9 | The monitor engine keeps running on the last approved spec hash even when OG1 is voided by a runbook edit. Detection is safe; only autonomy drops. | `engine/monitor_engine.py` |
| A10 | Severity rules. Consumer-facing objects are lineage leaves or objects with a freshness objective. "Regulatory dataset affected" means a quality, volume or schema failure on an object tagged `regulated`. Lateness on a regulated dataset is S2 through the Domain Pack override, not S1. | `engine/severity.py` |
| A11 | Domain Pack overrides do not apply to S4 (informational) groups, because no dataset is affected. | `engine/severity.py` |
| A12 | Access groups do not open an INC. Governance opens the GOV; the Commander escalates and routes. Cost groups go to FinOps the same way. | `agents/commander.py` |
| A13 | When a signal fits both a standalone (quality or schema) group and an ordinary lineage group, it joins the lineage group. | `engine/correlator.py` |

## Next (Days 7 to 10)

1. Day 7, O4-3 and O4-4: Diagnosis (read-only evidence and hypotheses) and Remediation (ranked options, ACT records through the executor, verification).
   Diagnosis is the first agent that needs model judgement (hypotheses), so it brings the model seam.
   Exit check: 7 of 8 causes correct, with evidence.
2. Sentinel weekly noise report and OG7 tuning proposals. Commander rejection and return counters (2 rejections or 3 returns lead to escalation).
3. O4-5 to O4-7: FinOps (F-06), Governance (F-07, no action), Problem and Knowledge (post-incident note, replay).
4. O5-1 and O5-2: replay harness over the fault library, and the full OT-01 to OT-16 suite.
5. Section 11 API and console, O3-2 real adapter, O3-4 rebind.

## Open questions (START HERE FIRST)

These are client facts. The build cannot invent them:

1. First real service and its objectives. The golden products stand in for now.
2. Which tools fill the six required slots, and is there a read-only identity for each?
3. Who is on-call, and who holds each approver role?
4. Which pipelines are documented as idempotent?
5. Baseline time to detect, time to recover, and monthly incident count.
