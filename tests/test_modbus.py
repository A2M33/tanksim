import struct

import pytest

from tanksim.modbus import ModbusClient, ModbusServer, handle_pdu, pack_bits, unpack_bits
from tanksim.process import Process
from tanksim.sim import ModbusError, Simulator


@pytest.fixture
def sim():
    return Simulator(Process(seed=1))


@pytest.fixture
def client(sim):
    server = ModbusServer(sim, port=0)
    server.start()
    c = ModbusClient(port=server.server_address[1])
    yield c
    c.close()
    server.shutdown()
    server.server_close()


def test_bit_packing_round_trip():
    bits = [True, False, True, True, False, False, False, False, True]
    packed = pack_bits(bits)
    assert packed == bytes([0b00001101, 0b00000001])
    assert unpack_bits(packed, len(bits)) == bits


def test_read_holding_setpoints(client):
    assert client.read_holding_registers(0, 7) == [50, 150, 200, 800, 900, 950, 100]


def test_read_input_registers_follow_process(client, sim):
    sim.step(60)
    level, mm = client.read_input_registers(0, 2)
    assert level == round(sim.process.sensor_pct * 10)
    assert mm == round(sim.process.sensor_pct * 30)
    assert client.read_input_registers(6, 1) == [int(sim.process.minute_of_day)]


def test_switch_to_manual_and_start_pump(client, sim):
    assert client.read_coils(0, 2) == [True, False]
    client.write_coil(0, False)
    client.write_coil(1, True)
    sim.step(5)
    assert client.read_discrete_inputs(0, 2) == [True, True]
    assert not sim.plc.auto


def test_write_multiple_setpoints_at_once(client, sim):
    # Moving start above the old stop is only valid together with a new stop.
    client.write_registers(2, [850, 880])
    assert sim.plc.setpoints["start"] == 85.0
    assert sim.plc.setpoints["stop"] == 88.0


def test_bad_setpoint_is_rejected(client, sim):
    with pytest.raises(ModbusError) as err:
        client.write_register(2, 900)  # start above stop
    assert err.value.code == ModbusError.ILLEGAL_VALUE
    assert sim.plc.setpoints["start"] == 20.0


def test_bad_address_and_function(client):
    with pytest.raises(ModbusError) as err:
        client.read_holding_registers(5, 10)
    assert err.value.code == ModbusError.ILLEGAL_ADDRESS
    with pytest.raises(ModbusError) as err:
        client.request(bytes([0x2B, 0x0E, 0x01, 0x00]))
    assert err.value.code == ModbusError.ILLEGAL_FUNCTION


def test_acknowledge_coil(client, sim):
    sim.process.volume_l = sim.process.capacity_l * 0.1
    sim.step(1)  # transmitter picks up the new level
    sim.step(1)  # PLC sees it on the next scan
    assert client.read_discrete_inputs(8, 1) == [True]
    client.write_coils(2, [True])
    assert client.read_discrete_inputs(8, 1) == [False]
    assert client.read_coils(2, 1) == [False]  # momentary


def test_single_coil_needs_ff00_or_0000(sim):
    reply = handle_pdu(sim, struct.pack(">BHH", 0x05, 0, 0x1234))
    assert reply == bytes([0x85, ModbusError.ILLEGAL_VALUE])


def test_truncated_request(sim):
    assert handle_pdu(sim, bytes([0x03, 0x00])) == bytes([0x83, ModbusError.ILLEGAL_VALUE])
