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
| O4-3 | Diagnosis | Done (Day 7) | `test_exit_check_cause_class_on_library_faults`: 8 of 8 faults (12 of 12 fault and pack pairs), 0 unsupported causes |
| O4-4 | Remediation at L1 | Done (Day 7) | `test_options_ranked_with_approval_needs`, `test_l1_recovery_flow_with_verification_record` |
| O4-5 | FinOps | Done (Day 8) | `test_f06_found_with_saving_arithmetic` (both packs), `test_finops_decision_and_realised_saving_in_ledger` |
| O4-6 | Governance | Done (Day 8) | `test_f07_found_and_no_action_taken` (both packs) |
| O4-7 | Problem and Knowledge | Done (Day 8) | `test_post_incident_note_replay_and_close`, `test_autonomy_proposal_from_platform_track_record` |
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

**Day 7 exit check** (at least 7 of 8 causes correct with evidence): passes with 8 of 8.
Options are ranked with their approval needs, and recovery produces a verification record.
`python -m adef_operate demo` prints the diagnosis next to the fault's label.

> **Read the 8 of 8 with care.** The rule reasoner was written knowing the fault library,
> so this result shows the evidence rules are consistent. It does not show real-world
> accuracy. The spec's real measure is shadow mode (O5-3): agent diagnoses compared with
> human conclusions on a real product, with a target of 80 percent.

## Day 7: what the agents do

- **Diagnosis** (`agents/diagnosis.py`, `engine/evidence.py`, `agents/reasoning.py`):
  - States impact first, in business terms (consumers, objects and objectives at risk).
  - Collects read-only, metadata-only evidence, each read charged to the diagnostic budget: run history and logs, freshness and volume with region breakdown, schema against the contract, batch checksums, releases and upstream notices, query history, and grants.
  - Wraps log text as untrusted and scans it. Instructions found there are reported, not followed (OT-06).
  - Refuses raw values outright. A request touching a Restricted or unclassified column is escalated to the Data Steward (OT-05).
  - Every library fault gets seven hypotheses, each recorded as supported, refuted, unsupported or inconclusive with the evidence IDs behind it. Ruled-out causes are listed.
  - The agent, not the reasoner, enforces the contract: at least two hypotheses, citations must exist, and with no supported cause the answer is `unknown` with low confidence. A release counts only when a failing signal ties it to the changed object.
  - S1 diagnoses require a council-of-three review (`engine/council.py`). The diagnosing agent cannot review itself, and options wait for a majority.
- **Reasoner seam:** `Reasoner` is a protocol. `RuleReasoner` is the deterministic default, so evaluations can be repeated. A model-backed reasoner can be plugged in without changing the agent's enforcement. No model API key is configured in this environment, so none is wired yet.
- **Remediation** (`agents/remediation.py`):
  - Contains first: pause on quality or schema failures, quarantine on duplicate batches.
  - Ranks options by safety: contain, wait, catalogue action, data correction, configuration, code fix, rollback, then trade-offs. Each option states its effect, risk, cost, time, reversibility, approval route, and whether the executor can run it now.
  - A re-run on a non-idempotent pipeline is offered only at OG2 and executed by people (OT-01).
  - Resize appears only as a trade-off at OG4, and a request to scale up as a fix is refused (OT-07).
  - At L1 it raises the gate request, then waits for a named person to confirm execution. At L3 it runs the action through the executor.
  - A rejection is returned word for word.
  - Code, design and contract causes raise a `CHG` and emit `change.request`.
  - Verification re-runs the detecting monitors and writes `verification.md` citing monitor result IDs. The result feeds the earned-autonomy track record, so a failure demotes.
- **Scenarios** (`simulation.py`): a reusable golden-product scenario and `diagnosis_accuracy()`, which the Day 9 replay harness will build on.

**Day 8 exit check** (F-06 and F-07 found; replay added): passes. F-06 yields a `FIN` with the
saving arithmetic shown, on both packs. F-07 yields a `GOV` with escalation to Security and
Compliance and no agent action, on both packs. A recovered incident gets a post-incident
note, a scored evaluation replay, and a guarded close.

## Day 8: what the agents do

All three are recommend-only. Every change they want goes to the Commander as a single
gate request: OG4 for FinOps, OG5 for Governance, OG7 for Knowledge. The Commander requests
nothing while stopped, and does not duplicate an open request for the same artifact.

- **FinOps** (`agents/finops.py`) wakes when the Commander routes a cost group (the runtime does this).
  - Compares actual against estimate, split by component and by pipeline, with unallocated spend on its own line and a month forecast.
  - Explains a material variance (over 20 percent) with evidence, or states it is unexplained with the next check.
  - One `FIN` per finding, with the saving arithmetic written out and shown as a range with confidence. Each also states risk to objectives, effort, reversibility, action class and who would execute. Code fixes go to Build as a CHG after OG4.
  - "Unused" needs telemetry, Consume usage and lineage to agree. A drop is A3 at OG4.
  - A cost spike on an object with an open incident is linked to that case, not duplicated.
  - Decisions (accepted, rejected or deferred) and the realised saving after one cycle go into `finops/savings-ledger.csv`.
- **Governance** (`agents/governance.py`) checks live controls against the approved baseline in the handover pack. Each control is reported as matching, drifted, waived or not verifiable:
  - grants against the access model;
  - dormant privileged roles;
  - masking on Restricted columns (policy hash);
  - classification of new objects (unclassified is treated as Restricted);
  - retention;
  - audit-logging gaps.

  Further rules:
  - One `GOV` per drift, with live and approved values, the correction's consumer impact and the decision owner.
  - Suspected exposure is escalated at once and collection stops.
  - Waivers are OG5 approvals tied to the drift's hash. An expired waiver is itself a finding.
  - Corrections are executed by people, then re-checked and recorded.
  - It also produces evidence packs (with hash-chain verification of the control logs and a "not covered" section) and an entitlements CSV.
- **Problem and Knowledge** (`agents/knowledge.py`):
  - **Post-incident notes:** timeline from the case, executor, approval and escalation logs, plus impact and cause. The cause is the human-confirmed one, or the agent's diagnosis marked UNCONFIRMED, and the note records match, partial or miss.
  - **Detection gap and prevention:** the note states the detection gap and lists prevention actions, each with a named owner, a due date and a verification. "What worked" and luck are left for people to write.
  - **Close:** the Commander's close uses the case-store guards.
  - **Replays:** each confirmed incident becomes an evaluation replay under `evals/replays/` with metadata only. Unconfirmed causes are kept but not scored.
  - **Problems:** opens a `PRB` when the same object fails 3 times in 30 days, or a cause class recurs.
  - **Proposals:** runbook changes (with the hash they would replace) and autonomy changes, computed from the platform's action log. Promotion needs 5 approved L2 executions, all verified; any failure or block leads to demotion. These are proposals only: the level changes when OG7 is approved.
  - **Monthly review:** writes the monthly operations review.

## Guardrail scenarios (section 13.2)

| # | Covered by enforcement code now | Test |
|---|---|---|
| OT-01 | Yes: executor never re-runs a non-idempotent pipeline | `test_ot01_non_idempotent_rerun_blocked` |
| OT-02, OT-03 | Partly: monitors can only change through a new OG1 hash; the refusal itself is agent behaviour | `test_engine_refuses_unapproved_or_edited_spec` |
| OT-04 | Yes | `test_ot04_third_retry_blocked_and_escalated` |
| OT-05 | Yes: raw values refused, statistics offered, Restricted columns escalated | `test_ot05_restricted_raw_values_refused_and_escalated` |
| OT-06 | Yes: labelled at detection, reported by Diagnosis, nothing changed | `test_ot06_injection_in_logs_is_data`, `test_ot06_injected_log_text_is_reported_not_followed` |
| OT-07 | Yes: scale-up refused as a fix; offered as a trade-off at OG4 | `test_ot07_scale_up_refused_as_fix` |
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
| A14 | At L1 an approved catalogue action is executed by people, who then confirm it. The executor runs only L2 actions (with approval) and L3 actions. | `agents/remediation.py` |
| A15 | Council of three: three distinct reviewers who are not team agents. A majority of two agreeing unlocks options for an S1. | `engine/council.py` |
| A16 | Diagnosis evidence read costs 0.05 diagnostic-budget units per read. | `engine/evidence.py` |
| A17 | The "wait" option counts as an attempt. If the monitors still fail at verification, that is one failed attempt toward the cap of two. | `agents/remediation.py` |
| A18 | FinOps material variance tolerance is 20 percent. A finding needs a saving of at least 10 per month; smaller ones become a summary line. Saving range: 80 to 100 percent of the excess, times 30 days. | `agents/finops.py` |
| A19 | Governance treats a grant outside the model as suspected exposure when it reaches a Restricted object, uses a privileged role, or carries ALL, OWNER or ADMIN. Dormant means unused for 90 days. | `agents/governance.py` |
| A20 | A human-confirmed cause is recorded on the case by a person named in `service.yaml`. Only confirmed replays are scored. | `agents/knowledge.py` |
| A21 | A recurrence problem is opened at 3 failures of one object in 30 days, or 2 incidents of the same cause class in 30 days. | `agents/knowledge.py` |

## Next (Days 9 and 10)

1. Day 9, O5-1 and O5-2: replay harness over the fault library (building on `simulation.py` and `evals/replays/`), the full OT-01 to OT-16 suite, and O3-4 rebind to a second profile.
2. Sentinel weekly noise report and OG7 tuning proposals. Commander rejection and return counters (2 rejections or 3 returns lead to escalation).
3. Section 11 API and console, O3-2 real adapter.
4. Day 10, O5-3: shadow mode on a real non-production product (needs the START HERE answers).

## Open questions (START HERE FIRST)

These are client facts. The build cannot invent them:

1. First real service and its objectives. The golden products stand in for now.
2. Which tools fill the six required slots, and is there a read-only identity for each?
3. Who is on-call, and who holds each approver role?
4. Which pipelines are documented as idempotent?
5. Baseline time to detect, time to recover, and monthly incident count.
