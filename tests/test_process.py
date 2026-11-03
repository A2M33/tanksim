import pytest

from tanksim.process import DEMAND_PROFILE, Process, demand_at


def run(p, seconds, dt=1.0):
    for _ in range(int(seconds / dt)):
        p.step(dt)


def test_demand_follows_profile_and_wraps_midnight():
    assert demand_at(7 * 60) == DEMAND_PROFILE[7]
    assert demand_at(7 * 60 + 30) == pytest.approx((DEMAND_PROFILE[7] + DEMAND_PROFILE[8]) / 2)
    assert demand_at(23 * 60 + 30) == pytest.approx((DEMAND_PROFILE[23] + DEMAND_PROFILE[0]) / 2)
    assert demand_at(1440 + 60) == DEMAND_PROFILE[1]


def test_tank_drains_without_pump():
    p = Process(seed=1)
    start = p.level_pct
    run(p, 600)
    assert p.level_pct < start
    assert p.inflow_lpm == 0


def test_pump_ramps_up_and_fills():
    p = Process(seed=1, start_minute=3 * 60)  # night, low demand
    p.pump_cmd = True
    p.step(1.0)
    assert 0 < p.inflow_lpm < p.pump_lpm
    assert not p.pump_running
    p.step(1.0)
    assert p.pump_running
    start = p.level_pct
    run(p, 600)
    assert p.level_pct > start
    assert p.inflow_lpm == pytest.approx(p.pump_lpm)


def test_volume_is_conserved():
    p = Process(seed=2)
    p.pump_cmd = True
    v0 = p.volume_l
    moved = 0.0
    for _ in range(300):
        p.step(2.0)
        moved += (p.inflow_lpm - p.outflow_lpm - p.overflow_lpm) * 2.0 / 60
    assert p.volume_l == pytest.approx(v0 + moved)


def test_empty_tank_cannot_go_negative():
    p = Process(seed=3, level_pct=0.5)
    run(p, 1200, dt=5.0)
    assert p.volume_l == pytest.approx(0, abs=1e-9)
    assert p.outflow_lpm == pytest.approx(0, abs=1e-9)


def test_full_tank_overflows():
    p = Process(seed=4, level_pct=99.9, start_minute=3 * 60)
    p.pump_cmd = True
    run(p, 300)
    assert p.level_pct == pytest.approx(100)
    assert p.overflow_lpm > 0


def test_broken_pump_stops_and_drops_feedback():
    p = Process(seed=5)
    p.pump_cmd = True
    run(p, 10)
    assert p.pump_running
    p.pump_broken = True
    run(p, 10)
    assert not p.pump_running
    assert p.inflow_lpm == 0


def test_stuck_sensor_freezes_reading():
    p = Process(seed=6)
    p.sensor_stuck = True
    frozen = p.sensor_pct
    run(p, 600)
    assert p.sensor_pct == frozen
    assert abs(p.level_pct - frozen) > 1


def test_leak_adds_to_outflow():
    a, b = Process(seed=7), Process(seed=7)
    b.leak_lpm = 30
    a.step(1.0)
    b.step(1.0)
    assert b.outflow_lpm == pytest.approx(a.outflow_lpm + 30)
