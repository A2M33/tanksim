"""Physical model: the tank, the fill pump and the people drawing water.

Everything the PLC is not. The PLC only sees what a real one would see:
the level transmitter reading and the pump's running feedback.
"""

from __future__ import annotations

import math
import random

# Water use of a small apartment block in litres per minute, one value per
# hour of the day. Peaks before work and in the evening.
DEMAND_PROFILE = [
    12, 10, 10, 10, 12, 25, 70, 110, 95, 55, 40, 38,
    45, 42, 35, 32, 35, 50, 75, 90, 85, 60, 35, 18,
]

PUMP_RAMP_S = 3.0  # seconds from standstill to full flow
SENSOR_NOISE_PCT = 0.05


def demand_at(minute_of_day: float) -> float:
    """Demand in L/min, interpolated between the hourly profile points."""
    hour = (minute_of_day / 60.0) % 24
    i = int(hour)
    a, b = DEMAND_PROFILE[i], DEMAND_PROFILE[(i + 1) % 24]
    return a + (b - a) * (hour - i)


class Process:
    def __init__(
        self,
        *,
        height_m: float = 3.0,
        diameter_m: float = 2.0,
        pump_lpm: float = 180.0,  # sized above the 110 L/min morning peak
        level_pct: float = 45.0,
        start_minute: float = 6 * 60 + 30,
        seed: int | None = None,
    ) -> None:
        self.height_m = height_m
        self.capacity_l = math.pi * (diameter_m / 2) ** 2 * height_m * 1000
        self.volume_l = self.capacity_l * level_pct / 100
        self.pump_lpm = pump_lpm
        self.clock_s = start_minute * 60.0

        self.pump_cmd = False  # written by the PLC
        self.pump_speed = 0.0  # 0..1
        self.demand_scale = 1.0
        self.inflow_lpm = 0.0
        self.outflow_lpm = 0.0
        self.overflow_lpm = 0.0

        # Faults the user can inject from the simulator panel.
        self.pump_broken = False
        self.leak_lpm = 0.0
        self.sensor_stuck = False

        self._rng = random.Random(seed)
        self._wander = 0.0
        self._sensor = level_pct

    @property
    def level_pct(self) -> float:
        """True level, which the PLC never sees directly."""
        return 100.0 * self.volume_l / self.capacity_l

    @property
    def minute_of_day(self) -> float:
        return (self.clock_s / 60.0) % 1440

    @property
    def sensor_pct(self) -> float:
        """What the level transmitter reports."""
        return self._sensor

    @property
    def pump_running(self) -> bool:
        """Running feedback from the motor contactor's auxiliary contact."""
        return self.pump_speed > 0.5

    def step(self, dt: float) -> None:
        if dt <= 0:
            return
        target = 1.0 if self.pump_cmd and not self.pump_broken else 0.0
        ramp = dt / PUMP_RAMP_S
        self.pump_speed += max(-ramp, min(ramp, target - self.pump_speed))
        self.inflow_lpm = self.pump_lpm * self.pump_speed

        # Demand drifts around the profile so no two days look the same.
        self._wander *= 0.997 ** dt
        self._wander += self._rng.gauss(0, 0.004) * math.sqrt(dt)
        self._wander = max(-0.15, min(0.15, self._wander))
        demand = demand_at(self.minute_of_day) * self.demand_scale * (1 + self._wander)
        demand = max(0.0, demand) + self.leak_lpm

        available_l = self.volume_l + self.inflow_lpm * dt / 60
        self.outflow_lpm = min(demand, available_l * 60 / dt)
        self.volume_l = available_l - self.outflow_lpm * dt / 60

        self.overflow_lpm = 0.0
        if self.volume_l > self.capacity_l:
            self.overflow_lpm = (self.volume_l - self.capacity_l) * 60 / dt
            self.volume_l = self.capacity_l

        self.clock_s += dt
        if not self.sensor_stuck:
            self._sensor = self.level_pct + self._rng.gauss(0, SENSOR_NOISE_PCT)
