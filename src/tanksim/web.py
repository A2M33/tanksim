"""HTTP server for the browser HMI.

GET  /api/state     one snapshot as JSON
GET  /api/history   trend samples [clock, level, inflow, outflow]
GET  /api/stream    server-sent events, a snapshot four times a second
POST /api/command   {"cmd": ..., ...}

Commands only come from the HMI's own page. Another site open in the same
browser could otherwise post to localhost (CSRF), or point its own domain
at 127.0.0.1 and post as same-origin (DNS rebinding). So a command needs a
Host that names this machine and, when the browser sends one, an Origin that
matches the Host. Tools like curl send no Origin and still work.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlsplit

from .commands import CommandError, apply_command
from .sim import Simulator

FONTS = [
    "Barlow-Regular", "Barlow-Medium", "Barlow-SemiBold",
    "BarlowSemiCondensed-Medium", "BarlowSemiCondensed-SemiBold",
]
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/tokens.css": ("tokens.css", "text/css; charset=utf-8"),
    "/hmi.css": ("hmi.css", "text/css; charset=utf-8"),
    "/hmi.js": ("hmi.js", "text/javascript; charset=utf-8"),
    **{f"/fonts/{name}.woff2": (f"fonts/{name}.woff2", "font/woff2") for name in FONTS},
    **{f"/icons/tanksim-{size}.png": (f"icons/tanksim-{size}.png", "image/png") for size in (16, 32, 180)},
}
STREAM_INTERVAL_S = 0.25
MAX_BODY = 64 * 1024


def _host_port(netloc: str) -> tuple[str, int] | None:
    """Split "name:port" from a Host header or an Origin, port 80 by default."""
    if not netloc or any(c in netloc for c in "@/\\?# "):
        return None
    try:
        parts = urlsplit("//" + netloc)
        port = parts.port or 80
    except ValueError:  # port not a number or out of range
        return None
    return (parts.hostname, port) if parts.hostname else None


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
            data = resources.files("tanksim").joinpath("static", *name.split("/")).read_bytes()
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
        # Read the body even to refuse it: closing on unread data resets the
        # connection on Windows, and the client never sees the answer.
        raw = self.rfile.read(length)
        refused = self._foreign_request()
        if refused:
            self._json(403, {"error": refused})
            return
        try:
            # ValueError also covers bad UTF-8, RecursionError deep nesting.
            body = json.loads(raw or b"{}")
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

    def _foreign_request(self) -> str | None:
        """Why this request may not come from the HMI page, or None."""
        hosts = self.headers.get_all("Host") or []
        origins = self.headers.get_all("Origin") or []
        if len(hosts) > 1 or len(origins) > 1:
            return "Send one Host and at most one Origin header"
        host = _host_port(hosts[0]) if hosts else None
        if hosts and (host is None or not self.server.is_own_host(host[0])):
            return "Open the HMI at localhost or by IP address to send commands"
        if origins:
            origin = urlsplit(origins[0])
            if origin.scheme != "http" or host is None or _host_port(origin.netloc) != host:
                return "Commands from other sites are refused"
        return None

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
    allow_reuse_address = os.name != "nt"  # on Windows it lets two servers share a port

    def __init__(self, sim: Simulator, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.sim = sim
        self.stopping = threading.Event()
        # Names no outside DNS server answers for: localhost, this machine's
        # own name and whatever name the user chose with --host.
        machine = socket.gethostname().lower()
        self.own_names = {"localhost", machine, machine + ".local", host.lower()}
        super().__init__((host, port), _Handler)

    def is_own_host(self, name: str) -> bool:
        try:
            ipaddress.ip_address(name)
            return True  # an address is not looked up, so it cannot be rebound
        except ValueError:
            return name in self.own_names or name.endswith(".localhost")

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
