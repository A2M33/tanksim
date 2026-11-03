"""HMI commands. Shared by the HTTP server and the in-browser demo, so this
module must not import anything that needs sockets or threads."""

from __future__ import annotations

from .sim import Simulator

SPEEDS = (1, 5, 20, 60)


class CommandError(ValueError):
    pass


def apply_command(sim: Simulator, body: dict) -> None:
    cmd = body.get("cmd")
    value = body.get("value")
    plc, p = sim.plc, sim.process
    with sim.lock:
        if cmd == "auto":
            plc.set_auto(bool(value))
        elif cmd == "manual_run":
            if plc.auto:
                raise CommandError("Switch the pump to manual first")
            plc.set_manual_run(bool(value))
        elif cmd == "ack":
            plc.acknowledge()
        elif cmd == "reset":
            plc.reset_faults()
        elif cmd == "setpoints":
            try:
                plc.set_setpoints({k: float(v) for k, v in dict(value).items()})
            except (KeyError, TypeError) as err:
                raise CommandError(f"Unknown setpoint {err}") from None
            except ValueError as err:
                raise CommandError(str(err).capitalize()) from None
        elif cmd == "speed":
            if value not in SPEEDS:
                raise CommandError(f"Speed must be one of {SPEEDS}")
            sim.speed = float(value)
        elif cmd == "pause":
            sim.paused = bool(value)
        elif cmd == "demand":
            if not isinstance(value, (int, float)) or not 0 <= value <= 300:
                raise CommandError("Demand must be between 0 and 300 %")
            p.demand_scale = value / 100
        elif cmd == "fault":
            name = body.get("name")
            if name == "pump":
                p.pump_broken = bool(value)
            elif name == "sensor":
                p.sensor_stuck = bool(value)
            elif name == "leak":
                p.leak_lpm = 40.0 if value else 0.0
            else:
                raise CommandError("Fault must be pump, sensor or leak")
        else:
            raise CommandError(f"Unknown command {cmd!r}")
