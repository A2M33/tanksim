"""HTTP server for the browser HMI.

GET  /api/state     one snapshot as JSON
GET  /api/history   trend samples [clock, level, inflow, outflow]
GET  /api/stream    server-sent events, a snapshot four times a second
POST /api/command   {"cmd": ..., ...}
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .commands import SPEEDS, CommandError, apply_command
from .sim import Simulator

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/hmi.css": ("hmi.css", "text/css; charset=utf-8"),
    "/hmi.js": ("hmi.js", "text/javascript; charset=utf-8"),
}
STREAM_INTERVAL_S = 0.25
MAX_BODY = 64 * 1024


class _Handler(BaseHTTPRequestHandler):
    server: "HmiServer"
    timeout = 10  # drops a client that stalls in the middle of a request

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
        if self.path.split("?", 1)[0] != "/api/command":
            self._json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self._json(400, {"error": f"Content-Length must be 0 to {MAX_BODY}"})
            return
        try:
            # ValueError also covers bad UTF-8, RecursionError deep nesting.
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, RecursionError):
            self._json(400, {"error": "Body is not valid JSON"})
            return
        try:
            if not isinstance(body, dict):
                raise CommandError("Send a JSON object")
            apply_command(self.server.sim, body)
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
        except ConnectionError:
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

    def handle_error(self, request, client_address) -> None:
        if not isinstance(sys.exc_info()[1], ConnectionError):  # a closed tab is no error
            super().handle_error(request, client_address)
