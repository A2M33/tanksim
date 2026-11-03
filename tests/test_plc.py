import pytest

from tanksim.plc import FEEDBACK_TIMEOUT_S, MIN_OFF_S, STUCK_TIMEOUT_S, Plc


class Rig:
    """Feeds the PLC a scripted level and a pump that obeys (or not)."""

    def __init__(self, plc=None):
        self.plc = plc or Plc()
        self.clock = 1000.0
        self.level = 50.0
        self.pump_obeys = True
        self.running = False
        self._n = 0

    def scan(self, seconds=1.0, dt=1.0, level=None):
        if level is not None:
            self.level = level
        for _ in range(int(seconds / dt)):
            self.clock += dt
            self._n += 1
            noisy = self.level + (0.01 if self._n % 2 else -0.01)
            self.plc.scan(level=noisy, running=self.running, clock=self.clock, dt=dt)
            if self.pump_obeys:
                self.running = self.plc.pump_cmd
        return self.plc.pump_cmd


def test_hysteresis_starts_low_and_stops_high():
    r = Rig()
    assert not r.scan(level=50)
    assert r.scan(level=19.5)
    assert r.scan(level=50)  # keeps filling inside the band
    assert not r.scan(level=80.5)
    assert not r.scan(level=50)  # keeps off inside the band


def test_restart_delay_blocks_quick_restart():
    r = Rig()
    r.scan(level=10)
    r.scan(level=85)
    assert not r.plc.pump_cmd
    assert not r.scan(level=10)
    assert r.plc.restart_delay_left() > 0
    assert r.scan(seconds=MIN_OFF_S, level=10)
    assert r.plc.starts == 2


def test_high_high_interlocks_manual_pump():
    r = Rig()
    r.plc.set_auto(False)
    r.plc.set_manual_run(True)
    assert r.scan(level=50)
    assert not r.scan(level=96)
    assert r.plc.alarms["HH"].active


def test_pump_fault_latches_until_reset():
    r = Rig()
    r.scan(level=10)
    r.pump_obeys = False
    r.running = False
    r.scan(seconds=FEEDBACK_TIMEOUT_S + 1, level=10)
    assert r.plc.pump_fault
    assert not r.plc.pump_cmd
    assert r.plc.alarms["PUMP"].active
    r.scan(seconds=10, level=10)
    assert not r.plc.pump_cmd  # still latched
    r.pump_obeys = True
    r.plc.reset_faults()
    assert r.scan(seconds=MIN_OFF_S + 1, level=10)


def test_stuck_sensor_trips_fault_and_stops_auto():
    r = Rig()
    r.scan(level=10)
    plc = r.plc
    for _ in range(int(STUCK_TIMEOUT_S) + 1):
        r.clock += 1
        plc.scan(level=42.0, running=r.running, clock=r.clock, dt=1)
        r.running = plc.pump_cmd
    assert plc.sensor_fault
    assert not plc.pump_cmd


def test_out_of_range_sensor_is_a_fault():
    r = Rig()
    r.scan(level=-25)
    assert r.plc.sensor_fault


def test_alarm_life_cycle():
    r = Rig()
    plc = r.plc
    r.scan(level=12)
    low = plc.alarms["L"]
    assert low.active and not low.acked and low.shown
    r.scan(level=50)
    assert not low.active and low.shown  # cleared but not acknowledged
    plc.acknowledge()
    assert not low.shown
    kinds = [kind for _, kind, _ in plc.events]
    assert kinds == ["ALM", "RTN", "ACK"]


def test_alarm_deadband_stops_chatter():
    r = Rig()
    r.scan(level=14.9)
    assert r.plc.alarms["L"].active
    r.scan(seconds=20, level=15.6)  # noise around the limit
    assert r.plc.alarms["L"].active
    r.scan(level=16.5)
    assert not r.plc.alarms["L"].active


def test_switch_to_manual_is_bumpless():
    r = Rig()
    r.scan(level=10)
    r.plc.set_auto(False)
    assert r.scan(level=50)
    r.plc.set_manual_run(False)
    assert not r.scan()


def test_setpoints_must_stay_in_order():
    plc = Plc()
    plc.set_setpoint("start", 30)
    assert plc.setpoints["start"] == 30
    with pytest.raises(ValueError):
        plc.set_setpoint("start", 85)
    with pytest.raises(ValueError):
        plc.set_setpoint("high_high", 101)
    with pytest.raises(KeyError):
        plc.set_setpoint("nope", 1)
    assert plc.setpoints["start"] == 30
