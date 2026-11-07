"""Talks to the Modbus server with pymodbus, a client written by other people,
so the server is not only checked against its own client. pymodbus is a test
tool only; tanksim itself needs no packages. Skipped where it is missing:

    pip install pymodbus
"""

import pytest

from tanksim.modbus import ModbusServer
from tanksim.process import Process
from tanksim.sim import Simulator

pytest.importorskip("pymodbus")
from pymodbus.client import ModbusTcpClient  # noqa: E402


@pytest.fixture
def sim():
    return Simulator(Process(seed=1))


@pytest.fixture
def client(sim):
    server = ModbusServer(sim, port=0)
    server.start()
    c = ModbusTcpClient("127.0.0.1", port=server.server_address[1], timeout=3, retries=0)
    assert c.connect()
    yield c
    c.close()
    server.shutdown()
    server.server_close()


def ok(response):
    assert not response.isError(), response
    return response


def test_reads_every_table(client, sim):
    sim.step(60)
    assert ok(client.read_coils(0, count=4)).bits[:4] == [True, False, False, False]
    inputs = ok(client.read_discrete_inputs(0, count=9)).bits[:9]
    assert inputs == sim.read_discrete_inputs(0, 9)
    assert ok(client.read_input_registers(0, count=8)).registers == sim.read_input_registers(0, 8)
    assert ok(client.read_holding_registers(0, count=7)).registers == [50, 150, 200, 800, 900, 950, 100]


def test_writes_reach_the_plc(client, sim):
    ok(client.write_coil(0, False))  # manual
    ok(client.write_coil(1, True))  # start the pump
    sim.step(5)
    assert ok(client.read_discrete_inputs(0, count=2)).bits[:2] == [True, True]

    ok(client.write_register(6, 150))  # demand 150 %
    ok(client.write_registers(2, [300, 700]))  # start and stop together
    assert ok(client.read_holding_registers(2, count=5)).registers == [300, 700, 900, 950, 150]
    assert sim.plc.setpoints["start"] == 30 and sim.process.demand_scale == 1.5

    ok(client.write_coils(0, [True, False]))
    assert sim.plc.auto


def exception_code(response):
    assert response.isError(), response
    return response.exception_code


def test_exception_responses(client, sim):
    assert exception_code(client.read_holding_registers(5, count=10)) == 2
    assert exception_code(client.read_input_registers(8, count=1)) == 2
    assert exception_code(client.write_coil(4, True)) == 2
    assert exception_code(client.write_register(2, 900)) == 3  # start above stop
    assert exception_code(client.write_register(6, 301)) == 3  # demand above 300 %
    assert exception_code(client.diag_read_diagnostic_register()) == 1  # FC 8
    assert exception_code(client.read_device_information()) == 1  # FC 43
    assert sim.plc.setpoints["start"] == 20
    # The connection survives all of that.
    assert ok(client.read_coils(0, count=1)).bits[0] is True
