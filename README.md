# Operate-With-AI (ADEF suite)

An approval-gated team of AI agents that keeps live data products inside their service,
quality, cost and compliance targets. The specification is [`OPERATE_SPEC.md`](OPERATE_SPEC.md);
the eight agent contracts are in [`skills/`](skills/) (unpacked from
`operate-with-ai-skills.zip`).

**Detection is code and judgement is agents.** The agents are not built yet. This
repository holds the deterministic runtime they will run on: the guardrails in spec
section 8 are enforced here, outside the prompts.

## Status

Days 1 to 5 of the ten-day plan (Epics O1 and O2, plus O3-1 and O3-3) are built and
tested. See [`BUILD_STATUS.md`](BUILD_STATUS.md) for the backlog item by item, the guardrail
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

The `demo` command onboards both golden products (Utilities and Telco), records OG0 and
OG1 as the synthetic approvers named in the packs, and runs each library fault through
the monitor engine.

## Layout

```
adef_operate/
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
    injection.py         treats log and ticket text as untrusted data
    onboarding_tools.py  draft service files, dry run and fault proofs for OG1
  connectors/  adapter contract (operate section) and the Demo Simulator with faults F-01 to F-08
  packs/       synthetic Utilities and Telco Domain Packs and golden handover packs
  api/         (next: section 11 endpoints)
skills/        eight SKILL.md contracts (validated by adef_operate.skills_check)
tests/         foundation, control, signals, skills
```
