import json
import urllib.error
import urllib.request

import pytest

from tanksim.cli import format_poll
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
    assert status == 400 and "rise" in body["error"]
    assert post(server, {"cmd": "speed", "value": 7})[0] == 400
    assert post(server, {"cmd": "nope"})[0] == 400


def test_fault_injection(server):
    post(server, {"cmd": "fault", "name": "leak", "value": True})
    post(server, {"cmd": "fault", "name": "pump", "value": True})
    assert server.sim.process.leak_lpm == 40
    assert server.sim.process.pump_broken


def test_unknown_path(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        get(server, "/nope")
    assert err.value.code == 404


def test_poll_line():
    line = format_poll(
        [True, False],
        [True, True, False, True, False, False, False, False, True],
        [123, 369, 1200, 455, 4, 75, 390, 0],
        [50, 150, 200, 800, 900, 950, 100],
    )
    assert line.startswith("06:30  level  12.3 %   369 mm")
    assert "pump RUN" in line and "run 1h15" in line
    assert line.endswith("ALARM L (unacked)")
