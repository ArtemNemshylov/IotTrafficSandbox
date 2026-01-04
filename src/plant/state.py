from dataclasses import dataclass


@dataclass
class PlantState:
    """
    Centralized state of the technological object (water treatment plant).

    This class contains ONLY state parameters.
    No physics, no calculations, no networking logic.
    All behavior is implemented in process.py.
    """

    # ======================================================
    # ENVIRONMENT
    # ======================================================

    ambient_temperature: float = 25.0        # °C (cooling baseline)

    # ======================================================
    # ELECTRICAL SUBSYSTEM
    # ======================================================

    # External power grid
    grid_voltage: float = 220.0              # V (180–240)

    # Stabilizer (derived)
    stabilizer_output_voltage: float = 220.0 # V
    stabilizer_state: str = "NORMAL"         # NORMAL / BYPASS / FAULT

    # ======================================================
    # IN-PUMP (WELL PUMP)
    # ======================================================

    # Control (operator input)
    in_pump_power_setpoint: float = 70.0     # % (0–100)
    in_pump_power_actual: float = 70.0       # % (derived, inertial)

    # Nominal characteristics
    in_pump_nominal_rpm: float = 2900.0      # RPM
    in_pump_nominal_flow_lpm = 120.0   # LPM

    # Actual / derived values
    in_pump_rpm: float = 2030.0              # RPM
    in_pump_flow_lpm: float = 42.0           # LPM
    in_pump_pressure_bar: float = 4.0        # bar
    in_pump_motor_temp: float = 45.0         # °C
    in_pump_power_kw: float = 1.5            # kW

    # ======================================================
    # FILTRATION UNIT
    # ======================================================

    filter_state: str = "FILTERING"           # FILTERING / BACKWASH
    filter_delta_p: float = 0.3               # bar
    turbidity_out: float = 1.2                # NTU

    # ======================================================
    # WATER STORAGE (TANK)
    # ======================================================

    tank_capacity_liters: float = 100.0     # L
    tank_level_liters: float = 5000.0         # L (derived)
    tank_level_percent: float = 50.0          # % (derived)

    # ======================================================
    # OUT-PUMP (DISTRIBUTION PUMP)
    # ======================================================

    # Control (operator input)
    out_pump_power_setpoint: float = 60.0     # % (0–100)
    out_pump_power_actual: float = 60.0       # % (derived, inertial)

    # Nominal characteristics
    out_pump_nominal_rpm: float = 2900.0      # RPM
    out_pump_nominal_flow_lpm: float = 100.0   # LPM

    # Actual / derived values
    out_pump_rpm: float = 1740.0              # RPM
    out_pump_flow_lpm: float = 30.0           # LPM
    out_pump_pressure_bar: float = 3.5        # bar
    out_pump_motor_temp: float = 42.0         # °C
    out_pump_power_kw: float = 1.2            # kW

    # ======================================================
    # SIMULATION / SERVICE
    # ======================================================

    time_seconds: int = 0
