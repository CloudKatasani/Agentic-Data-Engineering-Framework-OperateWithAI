"""Epic O3: monitor engine and fault injector."""

import pytest

from adef_operate.connectors.simulator import FAULTS, SimulatorAdapter
from adef_operate.core.errors import GuardViolation
from adef_operate.schemas import validate_record


def test_healthy_product_raises_no_signals(util):
    out = util.engine.run()
    assert out["signals"] == [] and all(r["status"] == "pass" for r in out["results"])


@pytest.mark.parametrize("fault_id", sorted(FAULTS))
def test_each_fault_raises_the_right_signal_classes(fault_id, home, request):
    fault = FAULTS[fault_id]
    p = request.getfixturevalue("util" if fault.packs[0] == "utilities" else "telco")
    p.sim.inject(fault_id, p.pack_name)
    sigs = p.engine.run()["signals"]
    for s in sigs:
        validate_record("signal", s)
        assert s["simulated"] is True
    assert set(fault.signal_classes) <= {s["signal_class"] for s in sigs}
    assert fault.first_failing[p.pack_name] in {s["object"] for s in sigs}


def test_both_pack_faults_also_fire_on_telco(telco):
    for fid in ("F-05", "F-06", "F-07", "F-08"):
        telco.sim.inject(fid, "telco")
    classes = {s["signal_class"] for s in telco.engine.run()["signals"]}
    assert {"quality", "cost", "access", "run"} <= classes


def test_faults_are_deterministic_and_resettable():
    a, b = SimulatorAdapter(), SimulatorAdapter()
    for fid, f in FAULTS.items():
        a.inject(fid, f.packs[0])
        b.inject(fid, f.packs[0])
    assert a.snapshot() == b.snapshot()
    a.reset()
    assert a.snapshot() == SimulatorAdapter().snapshot()


def test_engine_refuses_unapproved_or_edited_spec(util):
    spec = util.ws.read_yaml("service/monitoring-spec.yaml")
    spec["monitors"][0]["rule"]["kind"] = "run_state"
    spec["version"] = 2
    util.ws.write_yaml("onboarding", "service/monitoring-spec.yaml", spec)
    with pytest.raises(GuardViolation, match="approved hash"):
        util.engine.run()


def test_ot06_injection_in_logs_is_data(util):
    util.sim.inject("F-08", "utilities")
    sig = next(s for s in util.engine.run()["signals"] if s["signal_class"] == "run")
    assert "treated as data" in sig["detail"]
    # Nothing changed: monitors still enabled, levels unchanged, no grants added.
    spec = util.ws.read_yaml("service/monitoring-spec.yaml")
    assert all(m["enabled"] for m in spec["monitors"])
    assert util.registry.load()["levels"]["retry_failed_task"] == "L1"


def test_f03_echoes_downstream(telco):
    telco.sim.inject("F-03", "telco")
    objs = {s["object"] for s in telco.engine.run()["signals"] if s["signal_class"] == "volume"}
    assert objs == {"bronze.usage_records", "silver.usage_by_region", "gold.usage_daily"}


def test_signal_ids_are_unique_across_runs(util):
    util.sim.inject("F-07", "utilities")
    ids = [s["id"] for _ in range(3) for s in util.engine.run()["signals"]]
    assert len(ids) == len(set(ids))
