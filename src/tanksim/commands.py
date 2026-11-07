"""HMI commands. Shared by the HTTP server and the in-browser demo, so this
module must not import anything that needs sockets or threads."""

from __future__ import annotations

from .plc import SETPOINT_NAMES
from .sim import Simulator

SPEEDS = (1, 5, 20, 60)


class CommandError(ValueError):
    pass


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _switch(value) -> bool:
    # bool("false") is True and a missing value would read as False, so a
    # switch takes JSON true or false and nothing else.
    if not isinstance(value, bool):
        raise CommandError("Value must be true or false")
    return value


def _apply_setpoints(sim: Simulator, value) -> None:
    if not isinstance(value, dict):
        raise CommandError("Setpoints must be an object of names and numbers")
    for name, number in value.items():
        if name not in sim.plc.setpoints:
            raise CommandError(f"Unknown setpoint {name!r}")
        if not _is_number(number):
            raise CommandError(f"{SETPOINT_NAMES[name]} must be a number")
    try:
        sim.plc.set_setpoints(value)
    except ValueError as err:
        raise CommandError(str(err)) from None


def apply_command(sim: Simulator, body: dict) -> None:
    cmd = body.get("cmd")
    value = body.get("value")
    plc, p = sim.plc, sim.process
    with sim.lock:
        if cmd == "auto":
            plc.set_auto(_switch(value))
        elif cmd == "manual_run":
            run = _switch(value)
            if plc.auto:
                raise CommandError("Switch the pump to manual first")
            plc.set_manual_run(run)
        elif cmd == "ack":
            plc.acknowledge()
        elif cmd == "reset":
            plc.reset_faults()
        elif cmd == "setpoints":
            _apply_setpoints(sim, value)
        elif cmd == "speed":
            if not _is_number(value) or value not in SPEEDS:  # True == 1 in Python
                raise CommandError(f"Speed must be one of {SPEEDS}")
            sim.speed = float(value)
        elif cmd == "pause":
            sim.paused = _switch(value)
        elif cmd == "demand":
            if not _is_number(value) or not 0 <= value <= 300:
                raise CommandError("Demand must be between 0 and 300 %")
            p.demand_scale = value / 100
        elif cmd == "fault":
            name = body.get("name")
            if name not in ("pump", "sensor", "leak"):
                raise CommandError("Fault must be pump, sensor or leak")
            on = _switch(value)
            if name == "pump":
                p.pump_broken = on
            elif name == "sensor":
                p.sensor_stuck = on
            else:
                p.leak_lpm = 40.0 if on else 0.0
        else:
            raise CommandError(f"Unknown command {cmd!r}")
