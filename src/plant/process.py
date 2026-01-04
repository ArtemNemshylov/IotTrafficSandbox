from .state import PlantState


class PlantProcess:
    """
    Process model of the technological object (water treatment plant).
    All physical and causal logic is implemented here.
    """

    def __init__(self, state: PlantState):
        self.state = state

    # ======================================================
    # MAIN STEP
    # ======================================================

    def step(self, dt: float = 1.0):
        """
        Advance simulation by dt seconds.
        """
        s = self.state
        s.time_seconds += int(dt)

        self._update_electrical()
        self._update_in_pump(dt)
        self._update_filter(dt)
        self._update_out_pump(dt)
        self._update_storage(dt)

    # ======================================================
    # ELECTRICAL SUBSYSTEM
    # ======================================================

    def _update_electrical(self):
        s = self.state

        if s.grid_voltage < 190:
            s.stabilizer_state = "FAULT"
            s.stabilizer_output_voltage = s.grid_voltage
        elif s.grid_voltage > 240:
            s.stabilizer_state = "BYPASS"
            s.stabilizer_output_voltage = s.grid_voltage
        else:
            s.stabilizer_state = "NORMAL"
            s.stabilizer_output_voltage = 220.0

    # ======================================================
    # GENERIC PUMP LOGIC
    # ======================================================

    def _update_pump(
        self,
        power_setpoint: float,
        power_actual: float,
        nominal_rpm: float,
        nominal_flow: float,
        motor_temp: float,
        ambient_temp: float,
        voltage_factor: float,
        heating_coeff: float = 0.02,
        cooling_coeff: float = 0.05,
        inertia: float = 0.2,
    ):
        """
        Generic pump behavior:
        - power_actual smoothly follows power_setpoint
        - RPM and flow depend on power and voltage
        - motor temperature heats up or cools down
        """

        # Smooth power transition (inertia)
        power_actual += (power_setpoint - power_actual) * inertia
        power_actual = max(0.0, min(power_actual, 100.0))

        # Derived mechanical values
        rpm = nominal_rpm * (power_actual / 100.0) * voltage_factor
        flow = nominal_flow * (power_actual / 100.0) * voltage_factor

        # Temperature dynamics
        if power_actual > 0:
            motor_temp += heating_coeff * power_actual * voltage_factor
        else:
            motor_temp -= cooling_coeff * (motor_temp - ambient_temp)

        motor_temp = max(ambient_temp, min(motor_temp, 95.0))

        return power_actual, rpm, flow, motor_temp

    # ======================================================
    # IN-PUMP (WELL PUMP)
    # ======================================================

    def _update_in_pump(self, dt: float):
        s = self.state

        voltage_factor = max(
            0.0,
            min(s.stabilizer_output_voltage / 220.0, 1.1)
        )

        (
            s.in_pump_power_actual,
            s.in_pump_rpm,
            s.in_pump_flow_lpm,
            s.in_pump_motor_temp,
        ) = self._update_pump(
            power_setpoint=s.in_pump_power_setpoint,
            power_actual=s.in_pump_power_actual,
            nominal_rpm=s.in_pump_nominal_rpm,
            nominal_flow=s.in_pump_nominal_flow_lpm,
            motor_temp=s.in_pump_motor_temp,
            ambient_temp=s.ambient_temperature,
            voltage_factor=voltage_factor,
        )

        # Pressure roughly proportional to RPM
        s.in_pump_pressure_bar = 1.5 + 0.002 * s.in_pump_rpm

        # Electrical power consumption
        s.in_pump_power_kw = 1.5 * (s.in_pump_power_actual / 100.0)

    # ======================================================
    # FILTER SUBSYSTEM
    # ======================================================

    def _update_filter(self, dt: float):
        s = self.state

        if s.filter_state == "FILTERING":
            # Clogging proportional to inflow
            s.filter_delta_p += 0.0005 * s.in_pump_flow_lpm * dt
            s.turbidity_out = max(0.5, 2.0 - s.filter_delta_p)

            if s.filter_delta_p >= 1.5:
                s.filter_state = "BACKWASH"

        elif s.filter_state == "BACKWASH":
            # Cleaning phase
            s.filter_delta_p -= 0.07 * dt
            s.turbidity_out = 3.0

            if s.filter_delta_p <= 0.3:
                s.filter_delta_p = 0.3
                s.filter_state = "FILTERING"

        s.filter_delta_p = max(0.2, min(s.filter_delta_p, 2.5))

    # ======================================================
    # OUT-PUMP (DISTRIBUTION PUMP)
    # ======================================================

    def _update_out_pump(self, dt: float):
        s = self.state

        voltage_factor = max(
            0.0,
            min(s.stabilizer_output_voltage / 220.0, 1.1)
        )

        # Tank level limits available power
        if s.tank_level_liters <= 0:
            s.out_pump_power_setpoint = 0.0

        level_factor = min(
            1.0,
            s.tank_level_liters / (0.2 * s.tank_capacity_liters)
            if s.tank_capacity_liters > 0 else 0.0
        )

        (
            s.out_pump_power_actual,
            s.out_pump_rpm,
            s.out_pump_flow_lpm,
            s.out_pump_motor_temp,
        ) = self._update_pump(
            power_setpoint=s.out_pump_power_setpoint * level_factor,
            power_actual=s.out_pump_power_actual,
            nominal_rpm=s.out_pump_nominal_rpm,
            nominal_flow=s.out_pump_nominal_flow_lpm,
            motor_temp=s.out_pump_motor_temp,
            ambient_temp=s.ambient_temperature,
            voltage_factor=voltage_factor,
        )

        s.out_pump_pressure_bar = 1.2 + 0.0015 * s.out_pump_rpm
        s.out_pump_power_kw = 1.2 * (s.out_pump_power_actual / 100.0)

    # ======================================================
    # WATER STORAGE
    # ======================================================

    def _update_storage(self, dt: float):
        s = self.state

        inflow_lpm = (
            s.in_pump_flow_lpm
            if s.filter_state == "FILTERING"
            else 0.0
        )

        outflow_lpm = s.out_pump_flow_lpm

        delta_liters = (inflow_lpm - outflow_lpm) * (dt / 60.0)
        s.tank_level_liters += delta_liters

        s.tank_level_liters = max(
            0.0,
            min(s.tank_level_liters, s.tank_capacity_liters)
        )

        s.tank_level_percent = (
            s.tank_level_liters / s.tank_capacity_liters * 100.0
        )
