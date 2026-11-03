"""The PLC program. `scan()` runs once per cycle, like OB1 on a real PLC.

Inputs:  level transmitter (%), pump running feedback.
Outputs: pump run command.

Alarms follow the ISA-18.2 life cycle: an alarm comes in unacknowledged,
the operator acknowledges it, and it leaves the list only once it has both
cleared and been acknowledged.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

FEEDBACK_TIMEOUT_S = 5.0  # pump must confirm a start or stop within this
MIN_OFF_S = 60.0  # restart delay that protects the motor from short cycling
STUCK_TIMEOUT_S = 120.0  # a live transmitter never repeats the exact value this long
ALARM_DEADBAND_PCT = 1.0

# Ordered from bottom of the tank to top; each must be below the next.
SETPOINT_ORDER = ("low_low", "low", "start", "stop", "high", "high_high")
DEFAULT_SETPOINTS = {
    "low_low": 5.0,
    "low": 15.0,
    "start": 20.0,
    "stop": 80.0,
    "high": 90.0,
    "high_high": 95.0,
}

ALARM_TEXT = {
    "LL": "Level low low, consumers may lose water",
    "L": "Level low",
    "H": "Level high",
    "HH": "Level high high, pump interlocked off",
    "PUMP": "Pump P-101 did not follow its command",
    "SENSOR": "Level transmitter LT-101 frozen or out of range",
}


@dataclass
class Alarm:
    tag: str
    active: bool = False
    acked: bool = True
    since: float = 0.0

    @property
    def shown(self) -> bool:
        return self.active or not self.acked


class Plc:
    def __init__(self) -> None:
        self.auto = True
        self.manual_run = False
        self.setpoints = dict(DEFAULT_SETPOINTS)
        self.pump_cmd = False
        self.pump_fault = False
        self.sensor_fault = False
        self.starts = 0
        self.run_s = 0.0
        self.clock = 0.0
        self.alarms = {tag: Alarm(tag) for tag in ALARM_TEXT}
        self.events: deque[tuple[float, str, str]] = deque(maxlen=200)
        self._auto_demand = False
        self._mismatch_s = 0.0
        self._off_since = -MIN_OFF_S
        self._last_level: float | None = None
        self._same_s = 0.0

    # Operator commands -------------------------------------------------

    def set_auto(self, auto: bool) -> None:
        if auto != self.auto:
            self.auto = auto
            self.manual_run = self.pump_cmd  # bumpless: keep the pump as it is
            self._log("OPR", "Mode set to " + ("auto" if auto else "manual"))

    def set_manual_run(self, run: bool) -> None:
        if run != self.manual_run:
            self.manual_run = run
            if not self.auto:
                self._log("OPR", "Pump " + ("start" if run else "stop") + " requested")

    def set_setpoint(self, name: str, value: float) -> None:
        self.set_setpoints({name: value})

    def set_setpoints(self, changes: dict[str, float]) -> None:
        """Apply several setpoints at once; all or nothing."""
        for name in changes:
            if name not in self.setpoints:
                raise KeyError(name)
        trial = dict(self.setpoints, **{k: float(v) for k, v in changes.items()})
        values = [trial[k] for k in SETPOINT_ORDER]
        if not 0 <= values[0] or not values[-1] <= 100:
            raise ValueError("setpoints must lie between 0 and 100 %")
        if any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError("setpoints must rise: " + " < ".join(SETPOINT_ORDER))
        changed = [k for k in SETPOINT_ORDER if trial[k] != self.setpoints[k]]
        self.setpoints = trial
        for name in changed:
            self._log("OPR", f"Setpoint {name} set to {trial[name]:g} %")

    def acknowledge(self) -> None:
        pending = [a for a in self.alarms.values() if not a.acked]
        for alarm in pending:
            alarm.acked = True
        if pending:
            self._log("ACK", "Acknowledged " + ", ".join(a.tag for a in pending))

    def reset_faults(self) -> None:
        if self.pump_fault or self.sensor_fault:
            self.pump_fault = False
            self.sensor_fault = False
            self._mismatch_s = 0.0
            self._same_s = 0.0
            self._log("OPR", "Faults reset")

    # Scan cycle ---------------------------------------------------------

    def scan(self, *, level: float, running: bool, clock: float, dt: float) -> bool:
        self.clock = clock
        sp = self.setpoints

        # Transmitter health: out of range like a broken 4-20 mA loop,
        # or the exact same value for too long.
        if self._last_level is not None and level == self._last_level:
            self._same_s += dt
        else:
            self._same_s = 0.0
        self._last_level = level
        if not self.sensor_fault and (
            level < -2 or level > 102 or self._same_s >= STUCK_TIMEOUT_S
        ):
            self.sensor_fault = True

        # Pump feedback must follow the command.
        if self.pump_cmd != running:
            self._mismatch_s += dt
            if self._mismatch_s >= FEEDBACK_TIMEOUT_S and not self.pump_fault:
                self.pump_fault = True
        else:
            self._mismatch_s = 0.0

        # Two-point control with hysteresis.
        if level <= sp["start"]:
            self._auto_demand = True
        elif level >= sp["stop"]:
            self._auto_demand = False

        want = self._auto_demand if self.auto else self.manual_run
        hh = self._update_alarms(level)

        interlocked = hh or self.pump_fault or (self.auto and self.sensor_fault)
        if interlocked:
            want = False
            if self.pump_fault:
                self.manual_run = False
        if want and not self.pump_cmd and clock - self._off_since < MIN_OFF_S:
            want = False  # restart delay still running

        if want and not self.pump_cmd:
            self.starts += 1
        if not want and self.pump_cmd:
            self._off_since = clock
        self.pump_cmd = want

        if running:
            self.run_s += dt
        return self.pump_cmd

    def restart_delay_left(self) -> float:
        if self.pump_cmd:
            return 0.0
        return max(0.0, MIN_OFF_S - (self.clock - self._off_since))

    def _update_alarms(self, level: float) -> bool:
        sp, db = self.setpoints, ALARM_DEADBAND_PCT
        a = self.alarms
        conditions = {
            "LL": _band(a["LL"].active, level <= sp["low_low"], level > sp["low_low"] + db),
            "L": _band(a["L"].active, level <= sp["low"], level > sp["low"] + db),
            "H": _band(a["H"].active, level >= sp["high"], level < sp["high"] - db),
            "HH": _band(a["HH"].active, level >= sp["high_high"], level < sp["high_high"] - db),
            "PUMP": self.pump_fault,
            "SENSOR": self.sensor_fault,
        }
        for tag, active in conditions.items():
            alarm = a[tag]
            if active and not alarm.active:
                alarm.active, alarm.acked, alarm.since = True, False, self.clock
                self._log("ALM", f"{tag}  {ALARM_TEXT[tag]}")
            elif not active and alarm.active:
                alarm.active = False
                self._log("RTN", f"{tag}  back to normal")
        return a["HH"].active

    def _log(self, kind: str, text: str) -> None:
        self.events.append((self.clock, kind, text))


def _band(was_active: bool, trip: bool, clear: bool) -> bool:
    """Alarm with a deadband so sensor noise cannot make it chatter."""
    if was_active:
        return not clear
    return trip
