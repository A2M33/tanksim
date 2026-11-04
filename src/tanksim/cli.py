"""Command line: `tanksim` runs the plant, `tanksim poll` reads it over Modbus."""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

from . import __version__
from .commands import SPEEDS
from .modbus import ModbusClient, ModbusServer
from .sim import ModbusError, Simulator
from .web import HmiServer

TICK_S = 0.1


def run_loop(sim: Simulator, stop: threading.Event) -> None:
    last = time.monotonic()
    while not stop.wait(TICK_S):
        now = time.monotonic()
        real, last = min(now - last, 1.0), now
        if not sim.paused:
            sim.step(real * sim.speed)


def cmd_run(args: argparse.Namespace) -> int:
    sim = Simulator()
    sim.speed = float(args.speed)
    try:
        modbus = ModbusServer(sim, args.host, args.modbus_port)
    except OSError as err:
        print(f"Cannot open Modbus port {args.modbus_port}: {err.strerror}. Try --modbus-port.", file=sys.stderr)
        return 1
    try:
        hmi = HmiServer(sim, args.host, args.http_port)
    except OSError as err:
        modbus.server_close()
        print(f"Cannot open HMI port {args.http_port}: {err.strerror}. Try --http-port.", file=sys.stderr)
        return 1

    stop = threading.Event()
    threading.Thread(target=run_loop, args=(sim, stop), name="sim", daemon=True).start()
    modbus.start()
    hmi.start()

    shown_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    url = f"http://{shown_host}:{args.http_port}/"
    print(f"tanksim {__version__}")
    print(f"  HMI          {url}")
    print(f"  Modbus TCP   {args.host}:{args.modbus_port}, unit id ignored")
    print(f"  Speed        {args.speed}x simulated time")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        stop.set()
        hmi.shutdown()
        modbus.shutdown()
        hmi.server_close()
        modbus.server_close()
    return 0


def format_poll(coils, inputs, ir, hr) -> str:
    level, mm, inflow, outflow, starts, run_min, minute, delay = ir
    tags = ("LL", "L", "H", "HH", "PUMP", "SENSOR")
    alarms = [t for t, on in zip(tags, inputs[2:8]) if on]
    pump = "RUN" if inputs[0] else ("START" if inputs[1] else "STOP")
    line = (
        f"{minute // 60:02d}:{minute % 60:02d}  "
        f"level {level / 10:5.1f} % {mm:5d} mm  "
        f"in {inflow / 10:5.1f}  out {outflow / 10:5.1f} L/min  "
        f"pump {pump:<5} {'auto' if coils[0] else 'manual'}  "
        f"starts {starts}  run {run_min // 60}h{run_min % 60:02d}  "
        f"start/stop {hr[2] / 10:g}/{hr[3] / 10:g} %"
    )
    if delay:
        line += f"  restart in {delay}s"
    if alarms:
        line += "  ALARM " + ",".join(alarms) + (" (unacked)" if inputs[8] else "")
    return line


def cmd_poll(args: argparse.Namespace) -> int:
    try:
        client = ModbusClient(args.host, args.port)
    except OSError as err:
        print(f"Cannot reach {args.host}:{args.port}: {err}. Is tanksim running?", file=sys.stderr)
        return 1
    with client:
        try:
            while True:
                print(format_poll(
                    client.read_coils(0, 2),
                    client.read_discrete_inputs(0, 9),
                    client.read_input_registers(0, 8),
                    client.read_holding_registers(0, 7),
                ), flush=True)
                if args.once:
                    return 0
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
        except (OSError, ModbusError) as err:
            print(f"Lost the connection: {err}", file=sys.stderr)
            return 1


def port_number(text: str) -> int:
    if not text.isdecimal() or int(text) > 65535:
        raise argparse.ArgumentTypeError(f"{text} is not a port from 0 to 65535")
    return int(text)


def poll_interval(text: str) -> float:
    try:
        seconds = float(text)
    except ValueError:
        seconds = -1.0
    if not 0 < seconds < 86400:
        raise argparse.ArgumentTypeError(f"{text} is not a number of seconds above 0")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tanksim",
        description="Run a water tank and pump controlled by a PLC program, with a Modbus TCP server and a browser HMI.",
    )
    parser.add_argument("--version", action="version", version=f"tanksim {__version__}")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (0.0.0.0 for the whole network)")
    parser.add_argument("--http-port", type=port_number, default=8080, help="HMI port (default 8080)")
    parser.add_argument("--modbus-port", type=port_number, default=5020, help="Modbus TCP port (default 5020; 502 needs admin rights)")
    parser.add_argument("--speed", type=int, choices=SPEEDS, default=20, help="simulated seconds per real second (default 20)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the HMI in a browser")

    sub = parser.add_subparsers(dest="command")
    poll = sub.add_parser("poll", help="read a running tanksim over Modbus TCP and print one line per second")
    # SUPPRESS, or the default here would override `tanksim --host x poll`.
    poll.add_argument("--host", default=argparse.SUPPRESS)
    poll.add_argument("--port", type=port_number, default=5020)
    poll.add_argument("--interval", type=poll_interval, default=1.0, help="seconds between reads")
    poll.add_argument("--once", action="store_true", help="print one line and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "poll":
        return cmd_poll(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
