from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


PumpMode = Literal["AUTO", "MANUAL"]
PumpStateEnum = Literal["OFF", "ON", "FAULT"]
StabilizerMode = Literal["NORMAL", "BYPASS", "FAULT"]
FilterMode = Literal["FILTER", "BACKWASH", "IDLE"]
SensorState = Literal["OK", "FAULT", "TAMPER"]

# default clamp function
def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass
class EnvironmentState:
    ambient_temperature_c: float = 20.0

# до виправлення
@dataclass
class StabilizerState:
    vin_v: float = 220.0
    vout_v: float = 220.0
    mode: StabilizerMode = "NORMAL"
    active_power_kw: float = 0.0
    transformer_temp_c: float = 25.0


@dataclass
class PumpState:
    mode: PumpMode = "AUTO"
    state: PumpStateEnum = "OFF"
    rpm_desired: float = 2500.0  # set by server based on water level
    rpm_actual: float = 0.0  # rpm_actual = rpm_desired * voltage_v/220
    rpm_min: float = 1000.0
    rpm_nom: float = 2500.0
    rpm_max: float = 4000.0

    # Electrical
    voltage_v: float = 220.0
    power_kw: float = 0.0

    # Hydraulics
    pressure_bar: float = 0.0
    flow_lpm: float = 0.0

    # Thermal
    motor_temp_c: float = 20.0
    overheat_limit_c: float = 105.0
    hard_limit_c: float = 110.0
    overheat_seconds: float = 0.0


@dataclass
class FilterState:
    mode: FilterMode = "FILTER"

    # Pressures
    in_pressure_bar: float = 0.0
    out_pressure_bar: float = 0.2
    delta_pressure_bar: float = 0.0  # derived

    # Water quality (output after filtration)
    ntu: float = 1.0
    ph: float = 7.0

    # Clogging
    wear_pct: float = 0.0          # 0 only at system start (new filter)
    min_wear_after_backwash_pct: float = 10.0


@dataclass
class TankState:
    capacity_liters: float = 1000.0
    level_liters: float = 500.0  # base water level (when system starts)

    min_level_pct: float = 5.0
    low_level_pct: float = 20.0
    max_level_pct: float = 100.0

    in_flow_lpm: float = 0.0
    out_flow_lpm: float = 0.0
    level_pct: float = 50.0          # derived
    level_rate_lps: float = 0.0      # derived (liters per second)


@dataclass
class PlantState:
    time_s: int = 0

    env: EnvironmentState = field(default_factory=EnvironmentState)
    stabilizer: StabilizerState = field(default_factory=StabilizerState)

    # pumps
    in_pump: PumpState = field(default_factory=PumpState)
    out_pump: PumpState = field(default_factory=PumpState)

    filter: FilterState = field(default_factory=FilterState)
    tank: TankState = field(default_factory=TankState)
