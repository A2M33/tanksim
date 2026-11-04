"""Modbus TCP server and a small client, written against the spec with the
standard library only.

Frame = MBAP header (transaction id, protocol id 0, length, unit id)
followed by the PDU (function code + data). All numbers are big endian.
"""

from __future__ import annotations

import os
import socket
import socketserver
import struct
import threading

from .sim import ModbusError

READ_COILS = 0x01
READ_DISCRETE_INPUTS = 0x02
READ_HOLDING_REGISTERS = 0x03
READ_INPUT_REGISTERS = 0x04
WRITE_SINGLE_COIL = 0x05
WRITE_SINGLE_REGISTER = 0x06
WRITE_MULTIPLE_COILS = 0x0F
WRITE_MULTIPLE_REGISTERS = 0x10

MAX_READ_BITS = 2000
MAX_READ_REGISTERS = 125
MAX_WRITE_BITS = 1968
MAX_WRITE_REGISTERS = 123


def pack_bits(bits: list[bool]) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def unpack_bits(data: bytes, count: int) -> list[bool]:
    return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]


def handle_pdu(store, pdu: bytes) -> bytes:
    """Run one request PDU against the data store and return the response PDU."""
    if not pdu:
        raise ModbusError(ModbusError.ILLEGAL_FUNCTION)
    fc = pdu[0]
    try:
        return _dispatch(store, fc, pdu[1:])
    except ModbusError as err:
        return bytes([fc | 0x80, err.code])
    except struct.error:
        return bytes([fc | 0x80, ModbusError.ILLEGAL_VALUE])


def _dispatch(store, fc: int, data: bytes) -> bytes:
    if fc in (READ_COILS, READ_DISCRETE_INPUTS):
        address, count = struct.unpack(">HH", data[:4])
        if not 1 <= count <= MAX_READ_BITS:
            raise ModbusError(ModbusError.ILLEGAL_VALUE)
        read = store.read_coils if fc == READ_COILS else store.read_discrete_inputs
        packed = pack_bits(read(address, count))
        return bytes([fc, len(packed)]) + packed

    if fc in (READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS):
        address, count = struct.unpack(">HH", data[:4])
        if not 1 <= count <= MAX_READ_REGISTERS:
            raise ModbusError(ModbusError.ILLEGAL_VALUE)
        read = (
            store.read_holding_registers
            if fc == READ_HOLDING_REGISTERS
            else store.read_input_registers
        )
        values = read(address, count)
        return bytes([fc, 2 * count]) + struct.pack(f">{count}H", *values)

    if fc == WRITE_SINGLE_COIL:
        address, raw = struct.unpack(">HH", data[:4])
        if raw not in (0x0000, 0xFF00):
            raise ModbusError(ModbusError.ILLEGAL_VALUE)
        store.write_coils(address, [raw == 0xFF00])
        return bytes([fc]) + data[:4]

    if fc == WRITE_SINGLE_REGISTER:
        address, value = struct.unpack(">HH", data[:4])
        store.write_holding_registers(address, [value])
        return bytes([fc]) + data[:4]

    if fc == WRITE_MULTIPLE_COILS:
        address, count, nbytes = struct.unpack(">HHB", data[:5])
        if not 1 <= count <= MAX_WRITE_BITS or nbytes != (count + 7) // 8 or len(data) < 5 + nbytes:
            raise ModbusError(ModbusError.ILLEGAL_VALUE)
        store.write_coils(address, unpack_bits(data[5:5 + nbytes], count))
        return bytes([fc]) + data[:4]

    if fc == WRITE_MULTIPLE_REGISTERS:
        address, count, nbytes = struct.unpack(">HHB", data[:5])
        if not 1 <= count <= MAX_WRITE_REGISTERS or nbytes != 2 * count or len(data) < 5 + nbytes:
            raise ModbusError(ModbusError.ILLEGAL_VALUE)
        values = list(struct.unpack(f">{count}H", data[5:5 + nbytes]))
        store.write_holding_registers(address, values)
        return bytes([fc]) + data[:4]

    raise ModbusError(ModbusError.ILLEGAL_FUNCTION)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        while True:
            try:
                header = _recv_exact(sock, 7)
                tid, proto, length, unit = struct.unpack(">HHHB", header)
                if proto != 0 or not 2 <= length <= 254:
                    return  # not Modbus; drop the connection
                pdu = _recv_exact(sock, length - 1)
            except (ConnectionError, OSError):
                return
            reply = handle_pdu(self.server.store, pdu)
            try:
                sock.sendall(struct.pack(">HHHB", tid, 0, len(reply) + 1, unit) + reply)
            except OSError:
                return


class ModbusServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows it lets two servers share a port

    def __init__(self, store, host: str = "127.0.0.1", port: int = 5020) -> None:
        self.store = store
        super().__init__((host, port), _Handler)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="modbus", daemon=True)
        thread.start()
        return thread


class ModbusClient:
    """Just enough of a client for `tanksim poll` and the tests."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5020, unit: int = 1, timeout: float = 3.0) -> None:
        self.unit = unit
        self._tid = 0
        self._sock = socket.create_connection((host, port), timeout=timeout)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def request(self, pdu: bytes) -> bytes:
        self._tid = (self._tid + 1) & 0xFFFF
        self._sock.sendall(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit) + pdu)
        tid, _, length, _ = struct.unpack(">HHHB", _recv_exact(self._sock, 7))
        reply = _recv_exact(self._sock, length - 1)
        if tid != self._tid:
            raise ConnectionError(f"transaction id {tid}, expected {self._tid}")
        if reply[0] & 0x80:
            raise ModbusError(reply[1])
        return reply

    def read_coils(self, address: int, count: int) -> list[bool]:
        reply = self.request(struct.pack(">BHH", READ_COILS, address, count))
        return unpack_bits(reply[2:], count)

    def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        reply = self.request(struct.pack(">BHH", READ_DISCRETE_INPUTS, address, count))
        return unpack_bits(reply[2:], count)

    def read_holding_registers(self, address: int, count: int) -> list[int]:
        reply = self.request(struct.pack(">BHH", READ_HOLDING_REGISTERS, address, count))
        return list(struct.unpack(f">{count}H", reply[2:]))

    def read_input_registers(self, address: int, count: int) -> list[int]:
        reply = self.request(struct.pack(">BHH", READ_INPUT_REGISTERS, address, count))
        return list(struct.unpack(f">{count}H", reply[2:]))

    def write_coil(self, address: int, value: bool) -> None:
        self.request(struct.pack(">BHH", WRITE_SINGLE_COIL, address, 0xFF00 if value else 0))

    def write_register(self, address: int, value: int) -> None:
        self.request(struct.pack(">BHH", WRITE_SINGLE_REGISTER, address, value))

    def write_coils(self, address: int, values: list[bool]) -> None:
        packed = pack_bits(values)
        self.request(struct.pack(">BHHB", WRITE_MULTIPLE_COILS, address, len(values), len(packed)) + packed)

    def write_registers(self, address: int, values: list[int]) -> None:
        body = struct.pack(f">{len(values)}H", *values)
        self.request(struct.pack(">BHHB", WRITE_MULTIPLE_REGISTERS, address, len(values), len(body)) + body)
