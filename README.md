# tanksim

A water tank, a fill pump and the PLC program that runs them. It speaks Modbus TCP, so real SCADA and HMI tools can connect to it, and it ships with its own browser HMI.

![The tanksim HMI after the pump broke: the pump symbol has a red ring, the alarm list shows a pump fault and the trend shows the tank filling to the stop level](docs/hmi-fault.png)

I wanted something to practise PLC logic and Modbus on without buying a PLC. tanksim runs on any computer with Python 3.10 or newer and needs no other packages.

## Run it

```
pip install git+https://github.com/A2M33/tanksim
tanksim
```

Your browser opens the HMI at http://localhost:8080. The Modbus server listens on port 5020. Time runs 20 times faster than real time, so you see a full fill cycle in a few minutes. Change that with `--speed 1`, `5`, `20` or `60`.

In a second terminal you can read the plant over Modbus:

```
$ tanksim poll
07:12  level  23.4 %   702 mm  in 180.0  out 108.9 L/min  pump RUN   auto  starts 1  run 0h03  start/stop 20/80 %
```

## Things to try

- **Break the pump** in the simulator panel. The PLC waits 5 seconds for the running signal, then latches a pump fault and raises an alarm. Press Reset faults to clear it.
- **Freeze the level sensor.** A live transmitter never sends the same value twice in a row for long. After 2 minutes of identical readings the PLC calls it a sensor fault and stops the pump in auto, because it can no longer trust the level.
- **Switch to manual and run the pump** until the tank passes 95 %. The high high interlock stops it anyway.
- **Stop the pump and start it again** at once. The PLC holds a 60 second restart delay so the motor does not overheat from short cycling.
- **Set the start level above the stop level.** The HMI refuses, and so does the Modbus server (exception code 3).

## How the PLC decides

The program in [`plc.py`](src/tanksim/plc.py) runs once per scan, like the main cycle of a real PLC. It only sees what a real one would see: the level transmitter and the pump's running feedback.

| Situation | What the PLC does |
| --- | --- |
| Level falls to the start level (20 %) | Starts the pump (auto) |
| Level reaches the stop level (80 %) | Stops the pump (auto) |
| Level at or above high high (95 %) | Stops the pump in auto and manual |
| Pump feedback differs from the command for 5 s | Latches a pump fault, stops the pump |
| Level reading frozen for 2 min or outside -2 to 102 % | Latches a sensor fault, stops the pump in auto |
| Pump stopped less than 60 s ago | Holds off the next start |
| Switch from auto to manual | Keeps the pump as it is (bumpless) |

Alarms follow ISA-18.2. An alarm arrives unacknowledged and flashes. It leaves the list once it has cleared and you have acknowledged it. Level alarms have a 1 % deadband so sensor noise cannot make them chatter.

The HMI follows ISA-101: a grey screen, no colour for normal states, red and amber for alarms only. A running pump is filled dark and a stopped pump is hollow.

## Modbus register map

Addresses start at 0. Percentages travel as tenths of a percent, so 805 means 80.5 %.

**Coils** (read and write, function codes 1, 5, 15)

| Address | Name | Meaning |
| --- | --- | --- |
| 0 | auto | 1 = auto, 0 = manual |
| 1 | manual_run | Pump command in manual |
| 2 | acknowledge | Write 1 to acknowledge all alarms |
| 3 | reset_faults | Write 1 to reset pump and sensor faults |

**Discrete inputs** (read only, function code 2)

| Address | Name |
| --- | --- |
| 0 | pump running feedback |
| 1 | pump command |
| 2 to 7 | alarms LL, L, H, HH, pump fault, sensor fault |
| 8 | any alarm not acknowledged |

**Input registers** (read only, function code 4)

| Address | Name | Unit |
| --- | --- | --- |
| 0 | level | 0.1 % |
| 1 | level | mm |
| 2 | inflow | 0.1 L/min |
| 3 | outflow | 0.1 L/min |
| 4 | pump starts | count |
| 5 | pump run time | minutes |
| 6 | plant time of day | minutes after midnight |
| 7 | restart delay left | seconds |

**Holding registers** (read and write, function codes 3, 6, 16)

| Address | Name | Default |
| --- | --- | --- |
| 0 | low low alarm | 50 (5 %) |
| 1 | low alarm | 150 |
| 2 | pump start | 200 |
| 3 | pump stop | 800 |
| 4 | high alarm | 900 |
| 5 | high high alarm | 950 |
| 6 | water demand | 100 % |

The setpoints must rise from address 0 to 5. To move several at once, write them in one function code 16 request; the server checks them together.

## Connect other tools

Point any Modbus TCP client at `127.0.0.1`, port `5020`. I tested with the built-in `tanksim poll`. To reach tanksim from another machine, start it with `--host 0.0.0.0`.

From Python, without extra packages:

```python
from tanksim.modbus import ModbusClient

with ModbusClient("127.0.0.1", 5020) as plc:
    print(plc.read_input_registers(0, 1)[0] / 10, "%")
    plc.write_coil(0, False)  # manual
    plc.write_coil(1, True)   # start the pump
```

## The model

The tank holds about 9,400 litres (2 m wide, 3 m high). A 180 L/min pump fills it from a cistern. An apartment block draws water along a daily curve with peaks around 07:00 and 20:00, plus some random drift. The plant clock starts at 06:30, just before the morning peak.

## Development

```
git clone https://github.com/A2M33/tanksim
cd tanksim
pip install -e ".[dev]"
pytest
```

The tests cover the physics, every PLC rule above, the Modbus frames and the HTTP API. `tools/screenshot.py` takes the README images from a running tanksim with Playwright.

## License

MIT
