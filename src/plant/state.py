# state.py
from dataclasses import dataclass


@dataclass
class PlantState:
    """
    Центральний стан водоочисної станції.
    ТІЛЬКИ дані (state). Уся логіка/фізика/автоматика — в process.py
    """

    # ======================================================
    # SIM
    # ======================================================
    time_seconds: int = 0
    dt_s_default: float = 1.0

    # ======================================================
    # ENVIRONMENT
    # ======================================================
    ambient_temperature_c: float = 20.0  # °C

    # ======================================================
    # ELECTRICAL SUBSYSTEM
    # ======================================================
    grid_voltage_v: float = 220.0

    stabilizer_target_vout_v: float = 220.0
    stabilizer_mode: str = "NORMAL"  # NORMAL / BYPASS / FAULT
    stabilizer_vout_v: float = 220.0

    stabilizer_transformer_temp_c: float = 35.0
    stabilizer_active_power_kw: float = 0.0

    # ======================================================
    # WATER STORAGE (TANK)
    # ======================================================
    tank_capacity_liters: float = 10_000.0
    tank_level_liters: float = 5_000.0

    tank_min_level_pct: float = 6.0                 # <= цього — OUT OFF
    tank_out_limit_level_pct: float = 20.0          # < цього — OUT обмежуємо
    tank_in_emergency_level_pct: float = 20.0       # < цього — IN = 100%
    tank_in_nominal_until_pct: float = 80.0         # ДО цього — IN тримає NOMINAL RPM
    tank_backwash_start_level_pct: float = 75.0
    tank_backwash_stop_level_pct: float = 60.0
    tank_max_level_pct: float = 95.0                # >= цього — IN OFF

    tank_overflow: bool = False
    tank_level_sensors_state: str = "OK"  # OK / FAULT / TAMPER
    tank_valves_state: str = "OPEN"

    tank_in_flow_lpm: float = 0.0
    tank_out_flow_lpm: float = 0.0
    tank_level_pct: float = 50.0
    tank_level_rate_pct_s: float = 0.0

    # ======================================================
    # WATER QUALITY SOURCE (RAW WATER)
    # ======================================================
    ntu_in: float = 1.2
    ph_in: float = 7.2

    # ======================================================
    # FILTER SYSTEM
    # ======================================================
    filter_mode: str = "FILTER"  # FILTER / BACKWASH / IDLE
    filter_wear_pct: float = 10.0  # 0..100
    filter_delta_p_bar: float = 0.15
    filter_in_pressure_bar: float = 0.0
    filter_out_pressure_bar: float = 0.0

    ntu_out: float = 0.8
    ph_out: float = 7.2

    filter_valves_state: str = "OPEN"
    filter_quality_alarm: bool = False

    filter_backwash_elapsed_s: float = 0.0
    filter_backwash_max_s: float = 180.0

    # ======================================================
    # IN PUMP
    # ======================================================
    in_pump_cmd_mode: str = "AUTO"  # AUTO / MANUAL
    in_pump_cmd_state: str = "ON"   # ON / OFF
    in_pump_cmd_rpm_pct: float = 50.0  # для MANUAL

    in_pump_state: str = "OFF"  # ON / OFF / FAULT
    in_pump_fault_code: str = ""  # DRY_RUN / OVERHEAT / ...

    in_pump_voltage_v: float = 0.0
    in_pump_rpm: float = 0.0
    in_pump_flow_lpm: float = 0.0
    in_pump_pressure_bar: float = 0.0
    in_pump_power_kw: float = 0.0
    in_pump_motor_temp_c: float = 25.0

    in_pump_high_rpm_time_s: float = 0.0
    in_pump_cooldown_remaining_s: float = 0.0

    in_pump_rpm_min: float = 900.0
    in_pump_rpm_max: float = 3600.0
    in_pump_rpm_nom: float = 3000.0
    in_pump_flow_nom_lpm: float = 120.0
    in_pump_pressure_nom_bar: float = 2.5
    in_pump_power_nom_kw: float = 1.5

    # ======================================================
    # OUT PUMP  (max RPM must be +10% vs IN)
    # ======================================================
    out_pump_cmd_mode: str = "AUTO"  # AUTO / MANUAL
    out_pump_cmd_state: str = "ON"   # ON / OFF
    out_pump_cmd_rpm_pct: float = 50.0

    out_pump_state: str = "OFF"  # ON / OFF / FAULT
    out_pump_fault_code: str = ""

    out_pump_voltage_v: float = 0.0
    out_pump_rpm: float = 0.0
    out_pump_flow_lpm: float = 0.0
    out_pump_pressure_bar: float = 0.0
    out_pump_power_kw: float = 0.0
    out_pump_motor_temp_c: float = 25.0

    out_pump_high_rpm_time_s: float = 0.0
    out_pump_cooldown_remaining_s: float = 0.0

    out_pump_rpm_min: float = 900.0
    out_pump_rpm_nom: float = 3000.0
    out_pump_rpm_max: float = 3960.0  # 3600 * 1.1
    out_pump_flow_nom_lpm: float = 120.0
    out_pump_pressure_nom_bar: float = 2.5
    out_pump_power_nom_kw: float = 1.5

    # ======================================================
    # DEMAND (OUTFLOW REQUEST)
    # ======================================================
    demand_window_s: int = 3600
    demand_window_remaining_s: int = 3600
    out_demand_factor: float = 0.85  # 0.7..1.1
    out_demand_lpm: float = 0.0

    # ======================================================
    # LATCHES / INTERLOCKS
    # ======================================================
    out_blocked_low_level_filter: bool = False  # latch: level<20 AND wear>=85 => block OUT until wear<=50
    out_block_reason: str = ""                  # UI/debug
