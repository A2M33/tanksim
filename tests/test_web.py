import json
import socket
import urllib.error
import urllib.request

import pytest

from tanksim import web
from tanksim.cli import build_parser, format_poll
from tanksim.process import Process
from tanksim.sim import Simulator
from tanksim.web import HmiServer


@pytest.fixture
def server():
    sim = Simulator(Process(seed=1))
    srv = HmiServer(sim, port=0)
    srv.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def url(srv, path):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def get(srv, path):
    with urllib.request.urlopen(url(srv, path), timeout=3) as r:
        return r.status, r.read()


def post(srv, body):
    req = urllib.request.Request(
        url(srv, "/api/command"),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_serves_hmi_files(server):
    for path in ("/", "/hmi.css", "/hmi.js"):
        status, body = get(server, path)
        assert status == 200 and body


def test_state_snapshot(server):
    status, body = get(server, "/api/state")
    state = json.loads(body)
    assert state["auto"] is True
    assert state["setpoints"]["start"] == 20.0


def test_commands_change_the_plc(server):
    assert post(server, {"cmd": "auto", "value": False}) == (200, {"ok": True})
    assert post(server, {"cmd": "manual_run", "value": True})[0] == 200
    assert server.sim.plc.manual_run
    assert post(server, {"cmd": "setpoints", "value": {"start": 30, "stop": 70}})[0] == 200
    assert server.sim.plc.setpoints["stop"] == 70


def test_bad_commands_explain_themselves(server):
    status, body = post(server, {"cmd": "manual_run", "value": True})
    assert status == 400 and "manual" in body["error"]
    status, body = post(server, {"cmd": "setpoints", "value": {"start": 90}})
    assert status == 400 and body["error"] == "Pump start (90 %) must be below pump stop (80 %)"
    assert post(server, {"cmd": "speed", "value": 7})[0] == 400
    assert post(server, {"cmd": "nope"})[0] == 400


@pytest.mark.parametrize(
    "value, error",
    [
        (None, "object"),
        ("abc", "object"),
        ([1, 2], "object"),
        ({"nope": 1}, "Unknown setpoint 'nope'"),
        ({"start": "abc"}, "Pump start must be a number"),
        ({"start": None}, "Pump start must be a number"),
        ({"start": True}, "Pump start must be a number"),
        ({"start": 10**400}, "between 0 and 100"),
        ({"start": float("nan")}, "between 0 and 100"),
    ],
)
def test_malformed_setpoints_get_a_readable_error(server, value, error):
    status, body = post(server, {"cmd": "setpoints", "value": value})
    assert status == 400 and error in body["error"]
    assert server.sim.plc.setpoints["start"] == 20


@pytest.mark.parametrize(
    "body",
    [
        {"cmd": "auto"},
        {"cmd": "auto", "value": "false"},
        {"cmd": "pause", "value": 1},
        {"cmd": "fault", "name": "pump", "value": "yes"},
        {"cmd": "speed", "value": True},
    ],
    ids=["missing", "string", "number", "fault-string", "speed-bool"],
)
def test_switches_take_true_or_false_only(server, body):
    status, reply = post(server, body)
    assert status == 400 and reply["error"]
    sim = server.sim
    assert sim.plc.auto and not sim.paused and not sim.process.pump_broken
    assert sim.speed == 20


def test_demand_must_be_a_number(server):
    assert post(server, {"cmd": "demand", "value": True})[0] == 400
    assert server.sim.process.demand_scale == 1


def raw_post(srv, head, body=b""):
    """Send a hand-made request and return the status line."""
    with socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=3) as s:
        s.sendall(b"POST /api/command HTTP/1.1\r\nHost: x\r\n" + head + b"\r\n\r\n" + body)
        return s.recv(200).split(b"\r\n")[0]


@pytest.mark.parametrize(
    "body",
    [b"{nope", b'\xff\xfe{"cmd":"ack"}', b"[" * 60_000],
    ids=["syntax", "utf8", "nesting"],
)
def test_body_that_is_not_json_gets_400(server, body):
    assert b" 400 " in raw_post(server, b"Content-Length: %d" % len(body), body)


@pytest.mark.parametrize("length", [b"abc", b"-1", b"999999999"])
def test_bad_content_length_gets_400(server, length):
    assert b" 400 " in raw_post(server, b"Content-Length: " + length)


def test_command_path_may_carry_a_query(server):
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=3) as s:
        s.sendall(b'POST /api/command?x=1 HTTP/1.1\r\nContent-Length: 13\r\n\r\n{"cmd":"ack"}')
        assert b" 200 " in s.recv(200)


def test_stalled_body_is_dropped(server, monkeypatch):
    monkeypatch.setattr(web._Handler, "timeout", 0.2)
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=3) as s:
        s.sendall(b"POST /api/command HTTP/1.1\r\nContent-Length: 50\r\n\r\n{")
        assert s.recv(200) == b""  # the server hangs up by itself


def test_closed_tab_leaves_no_traceback(server, capsys):
    try:
        raise ConnectionAbortedError
    except ConnectionError:
        server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""


def test_fault_injection(server):
    post(server, {"cmd": "fault", "name": "leak", "value": True})
    post(server, {"cmd": "fault", "name": "pump", "value": True})
    assert server.sim.process.leak_lpm == 40
    assert server.sim.process.pump_broken


def test_port_in_use_is_an_error(server):
    with pytest.raises(OSError):
        HmiServer(server.sim, port=server.server_address[1])


def test_unknown_path(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        get(server, "/nope")
    assert err.value.code == 404


@pytest.mark.parametrize(
    "argv",
    [
        ["--http-port", "99999"],
        ["--modbus-port", "-1"],
        ["poll", "--port", "abc"],
        ["poll", "--interval", "-1"],
        ["poll", "--interval", "nan"],
    ],
)
def test_bad_numbers_are_usage_errors(argv, capsys):
    with pytest.raises(SystemExit) as err:
        build_parser().parse_args(argv)
    assert err.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_poll_takes_the_host_from_either_side():
    parse = build_parser().parse_args
    assert parse(["poll"]).host == "127.0.0.1"
    assert parse(["--host", "10.0.0.5", "poll"]).host == "10.0.0.5"
    assert parse(["poll", "--host", "10.0.0.6"]).host == "10.0.0.6"


def test_poll_line():
    line = format_poll(
        [True, False],
        [True, True, False, True, False, False, False, False, True],
        [123, 369, 1800, 455, 4, 75, 390, 0],
        [50, 150, 200, 800, 900, 950, 100],
    )
    assert line.startswith("06:30  level  12.3 %   369 mm")
    assert "pump RUN" in line and "run 1h15" in line
    assert line.endswith("ALARM L (unacked)")
