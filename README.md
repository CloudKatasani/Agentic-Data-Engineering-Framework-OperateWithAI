# Operate-With-AI (ADEF suite)

An approval-gated team of AI agents that keeps live data products inside their service,
quality, cost and compliance targets. The specification is [`OPERATE_SPEC.md`](OPERATE_SPEC.md);
the eight agent contracts are in [`skills/`](skills/) (unpacked from
`operate-with-ai-skills.zip`).

**Detection is code and judgement is agents.** The deterministic runtime enforces the
guardrails in spec section 8 outside the prompts. Five agents are built so far:
Commander, Service Onboarding, Signal Sentinel, Incident Diagnosis and Remediation.

## Status

Days 1 to 7 of the ten-day plan are built and tested: Epics O1 and O2, O3-1, O3-3, and O4-1 to O4-4. See [`BUILD_STATUS.md`](BUILD_STATUS.md) for the backlog item by item, the guardrail
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

The `demo` command releases and onboards both golden products (Utilities and Telco), records
OG0 and OG1 as the synthetic approvers named in the packs, then injects each library fault
and runs one tick. A tick runs the monitor engine, then Sentinel grouping, then the
Commander opening, routing or batching the case.

## Layout

```
adef_operate/
  agents/      commander.py, onboarding.py, sentinel.py, diagnosis.py, remediation.py,
               reasoning.py (Reasoner seam and deterministic RuleReasoner)
  simulation.py  golden-product scenarios and the diagnosis accuracy evaluation
  runtime.py   one control-plane tick: monitor engine -> Sentinel -> Commander
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
    injection.py         treats log and ticket text as untrusted data
    onboarding_tools.py  draft service files, dry run and fault proofs for OG1
  connectors/  adapter contract (operate section) and the Demo Simulator with faults F-01 to F-08
  packs/       synthetic Utilities and Telco Domain Packs and golden handover packs
  api/         (next: section 11 endpoints)
skills/        eight SKILL.md contracts (validated by adef_operate.skills_check)
tests/         foundation, control, signals, day 6 and day 7 agents, skills
```
