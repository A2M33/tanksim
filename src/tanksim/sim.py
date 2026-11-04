"""Ties the process and the PLC together and exposes them as Modbus data.

All access goes through one lock, because the simulation loop, the Modbus
server and the web HMI each run in their own thread.
"""

from __future__ import annotations

import threading
from collections import deque

from .plc import ALARM_TEXT, SETPOINT_ORDER, Plc
from .process import Process

HISTORY_EVERY_S = 10.0  # one trend sample per 10 simulated seconds
HISTORY_LEN = 720  # two simulated hours
MAX_DT = 10.0  # larger steps would let the pump skip past its feedback check


class ModbusError(Exception):
    """Raised with a Modbus exception code."""

    ILLEGAL_FUNCTION = 1
    ILLEGAL_ADDRESS = 2
    ILLEGAL_VALUE = 3
    SERVER_FAILURE = 4

    def __init__(self, code: int) -> None:
        super().__init__(f"Modbus exception code {code}")
        self.code = code


# Register map, also documented in the README.
COILS = ("auto", "manual_run", "acknowledge", "reset_faults")
DISCRETE_INPUTS = (
    "pump_running", "pump_cmd", "alarm_LL", "alarm_L", "alarm_H",
    "alarm_HH", "alarm_PUMP", "alarm_SENSOR", "unacknowledged",
)
INPUT_REGISTERS = (
    "level_0p1pct", "level_mm", "inflow_0p1lpm", "outflow_0p1lpm",
    "pump_starts", "pump_run_min", "time_of_day_min", "restart_delay_s",
)
HOLDING_REGISTERS = tuple(f"sp_{k}_0p1pct" for k in SETPOINT_ORDER) + ("demand_pct",)


def _u16(value: float) -> int:
    return max(0, min(0xFFFF, int(round(value))))


class Simulator:
    def __init__(self, process: Process | None = None, plc: Plc | None = None) -> None:
        self.process = process or Process()
        self.plc = plc or Plc()
        self.lock = threading.RLock()
        self.speed = 20.0
        self.paused = False
        self.history: deque[tuple[float, float, float, float]] = deque(maxlen=HISTORY_LEN)
        self._next_sample = self.process.clock_s

    def step(self, dt: float) -> None:
        """Advance by dt simulated seconds, in slices the PLC can keep up with."""
        with self.lock:
            while dt > 1e-9:
                part = min(dt, MAX_DT)
                self._step(part)
                dt -= part

    def _step(self, dt: float) -> None:
        p, plc = self.process, self.plc
        p.pump_cmd = plc.scan(
            level=p.sensor_pct, running=p.pump_running, clock=p.clock_s, dt=dt
        )
        p.step(dt)
        if p.clock_s >= self._next_sample:
            self.history.append((p.clock_s, p.sensor_pct, p.inflow_lpm, p.outflow_lpm))
            self._next_sample = p.clock_s + HISTORY_EVERY_S

    # Modbus data model ---------------------------------------------------

    def read_coils(self, address: int, count: int) -> list[bool]:
        with self.lock:
            values = [self.plc.auto, self.plc.manual_run, False, False]
            return _slice(values, address, count)

    def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        with self.lock:
            a = self.plc.alarms
            values = [
                self.process.pump_running,
                self.plc.pump_cmd,
                *(a[tag].active for tag in ("LL", "L", "H", "HH", "PUMP", "SENSOR")),
                any(not al.acked for al in a.values()),
            ]
            return _slice(values, address, count)

    def read_input_registers(self, address: int, count: int) -> list[int]:
        with self.lock:
            p, plc = self.process, self.plc
            values = [
                _u16(p.sensor_pct * 10),
                _u16(p.sensor_pct / 100 * p.height_m * 1000),
                _u16(p.inflow_lpm * 10),
                _u16(p.outflow_lpm * 10),
                plc.starts & 0xFFFF,
                int(plc.run_s // 60) & 0xFFFF,
                int(p.minute_of_day),
                _u16(plc.restart_delay_left()),
            ]
            return _slice(values, address, count)

    def read_holding_registers(self, address: int, count: int) -> list[int]:
        with self.lock:
            sp = self.plc.setpoints
            values = [_u16(sp[k] * 10) for k in SETPOINT_ORDER]
            values.append(_u16(self.process.demand_scale * 100))
            return _slice(values, address, count)

    def write_coils(self, address: int, values: list[bool]) -> None:
        _check_range(address, len(values), len(COILS))
        with self.lock:
            for offset, value in enumerate(values):
                name = COILS[address + offset]
                if name == "auto":
                    self.plc.set_auto(value)
                elif name == "manual_run":
                    self.plc.set_manual_run(value)
                elif name == "acknowledge" and value:
                    self.plc.acknowledge()
                elif name == "reset_faults" and value:
                    self.plc.reset_faults()

    def write_holding_registers(self, address: int, values: list[int]) -> None:
        _check_range(address, len(values), len(HOLDING_REGISTERS))
        changes: dict[str, float] = {}
        demand = None
        for offset, raw in enumerate(values):
            index = address + offset
            if index < len(SETPOINT_ORDER):
                changes[SETPOINT_ORDER[index]] = raw / 10
            else:
                if not 0 <= raw <= 300:
                    raise ModbusError(ModbusError.ILLEGAL_VALUE)
                demand = raw / 100
        with self.lock:
            try:
                if changes:
                    self.plc.set_setpoints(changes)
            except ValueError:
                raise ModbusError(ModbusError.ILLEGAL_VALUE) from None
            if demand is not None:
                self.process.demand_scale = demand

    # Snapshot for the HMI -------------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            p, plc = self.process, self.plc
            return {
                "clock": p.clock_s,
                "minute": p.minute_of_day,
                "level": p.sensor_pct,
                "level_true": p.level_pct,
                "level_mm": p.sensor_pct / 100 * p.height_m * 1000,
                "capacity_l": p.capacity_l,
                "inflow": p.inflow_lpm,
                "outflow": p.outflow_lpm,
                "overflow": p.overflow_lpm,
                "pump_cmd": plc.pump_cmd,
                "pump_running": p.pump_running,
                "pump_speed": p.pump_speed,
                "auto": plc.auto,
                "manual_run": plc.manual_run,
                "restart_delay": plc.restart_delay_left(),
                "starts": plc.starts,
                "run_s": plc.run_s,
                "setpoints": dict(plc.setpoints),
                "alarms": [
                    {
                        "tag": a.tag,
                        "text": ALARM_TEXT[a.tag],
                        "active": a.active,
                        "acked": a.acked,
                        "since": a.since,
                    }
                    for a in plc.alarms.values()
                    if a.shown
                ],
                "events": [list(e) for e in list(plc.events)[-30:]],
                "sim": {
                    "speed": self.speed,
                    "paused": self.paused,
                    "demand_scale": p.demand_scale,
                    "pump_broken": p.pump_broken,
                    "leak_lpm": p.leak_lpm,
                    "sensor_stuck": p.sensor_stuck,
                },
            }

    def history_list(self) -> list[list[float]]:
        with self.lock:
            return [list(h) for h in self.history]


def _check_range(address: int, count: int, size: int) -> None:
    if count < 1 or address < 0 or address + count > size:
        raise ModbusError(ModbusError.ILLEGAL_ADDRESS)


def _slice(values: list, address: int, count: int) -> list:
    _check_range(address, count, len(values))
    return values[address:address + count]
