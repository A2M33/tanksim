"""HTTP server for the browser HMI.

GET  /api/state     one snapshot as JSON
GET  /api/history   trend samples [clock, level, inflow, outflow]
GET  /api/stream    server-sent events, a snapshot four times a second
POST /api/command   {"cmd": ..., ...}
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .sim import Simulator

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/hmi.css": ("hmi.css", "text/css; charset=utf-8"),
    "/hmi.js": ("hmi.js", "text/javascript; charset=utf-8"),
}
SPEEDS = (1, 5, 20, 60)
STREAM_INTERVAL_S = 0.25


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


class _Handler(BaseHTTPRequestHandler):
    server: "HmiServer"

    def log_message(self, format, *args) -> None:  # keep the console quiet
        pass

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        sim = self.server.sim
        if path in STATIC:
            name, ctype = STATIC[path]
            data = resources.files("tanksim").joinpath("static", name).read_bytes()
            self._send(200, data, ctype)
        elif path == "/api/state":
            self._json(200, sim.snapshot())
        elif path == "/api/history":
            self._json(200, sim.history_list())
        elif path == "/api/stream":
            self._stream()
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if self.path != "/api/command":
            self._json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise CommandError("Send a JSON object")
            apply_command(self.server.sim, body)
        except json.JSONDecodeError:
            self._json(400, {"error": "Body is not valid JSON"})
        except CommandError as err:
            self._json(400, {"error": str(err)})
        else:
            self._json(200, {"ok": True})

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        stop = self.server.stopping
        try:
            while not stop.is_set():
                payload = json.dumps(self.server.sim.snapshot())
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                time.sleep(STREAM_INTERVAL_S)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _send(self, status: int, data: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


class HmiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, sim: Simulator, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.sim = sim
        self.stopping = threading.Event()
        super().__init__((host, port), _Handler)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="hmi", daemon=True)
        thread.start()
        return thread

    def shutdown(self) -> None:
        self.stopping.set()
        super().shutdown()
