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
| O4-1 to O4-7 | Agents | Not started. Deterministic onboarding tools exist (`engine/onboarding_tools.py`) | Readiness proofs: every library fault fires its expected class |
| O5-1 to O5-4 | Evaluation and launch | Not started | - |
| Section 11 | API and console | Not started | - |

**Day 2-3 exit check** (OT-01, OT-04, OT-11, OT-12, OT-15): all pass.
**Day 4-5 exit check** (all eight faults raise the right signals): passes on both packs.

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

## Next (Days 6 to 10)

1. O4-1 and O4-2: Commander (severity rules with Domain Pack raise-only overrides, case open, link and merge) and Sentinel grouping. F-03 must yield one group.
2. O4-3 and O4-4: Diagnosis (read-only evidence and hypotheses) and Remediation (ranked options, ACT records through the executor, verification).
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
