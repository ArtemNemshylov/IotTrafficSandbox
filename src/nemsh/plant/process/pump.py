import math
from src.nemsh.plant.state import PlantState, clamp

class PumpProcess:
    def step_in_pump(self, s: PlantState, dt: float) -> None:
        self._step_pump(s, s.in_pump, dt, is_in_pump=True)

    def step_out_pump(self, s: PlantState, dt: float) -> None:
        self._step_pump(s, s.out_pump, dt, is_in_pump=False)

    # ======================================================
    # Core pump physics
    # ======================================================
    def _step_pump(self, s: PlantState, p, dt: float, *, is_in_pump: bool) -> None:
        p.voltage_v = s.stabilizer.output_voltage

        if s.stabilizer.mode == "FAULT":
            p.state = "OFF"

        if p.state == "OFF" or p.rpm_desired <= 0.0:
            self._apply_off(p, s, dt)
            return

        self._update_rpm(p, s, dt)
        self._update_hydraulics(p, s, is_in_pump)
        self._update_thermal(p, s, dt)
        self._apply_hard_fault(p)

    # -------------------------
    # RPM
    # -------------------------
    def _update_rpm(self, p, s: PlantState, dt: float) -> None:
        vf = clamp(
            p.voltage_v / s.stabilizer.nominal_voltage,
            0.0,
            1.25,
        )
        rpm_target = clamp(p.rpm_desired * vf, 0.0, p.rpm_max)

        alpha = clamp(dt / 1.5, 0.0, 1.0)
        p.rpm_actual += (rpm_target - p.rpm_actual) * alpha

    # -------------------------
    # Hydraulics
    # -------------------------
    def _update_hydraulics(self, p, s: PlantState, is_in_pump: bool) -> None:
        rpm_ratio = p.rpm_actual / p.rpm_nom if p.rpm_nom > 0 else 0.0

        if is_in_pump:
            wear = clamp(s.filter.wear_pct / 100.0, 0.0, 1.0)
            wear_factor = 1.0 + 0.3333 * wear ** 2

            p.pressure_bar = 2.7 * rpm_ratio ** 2 * wear_factor
            p.flow_lpm = 120.0 * rpm_ratio / math.sqrt(wear_factor)
            p.power_kw = p.power_nom_kw * rpm_ratio ** 3 * math.sqrt(wear_factor)
        else:
            p.pressure_bar = 3.0 * rpm_ratio ** 2
            p.flow_lpm = 110.0 * rpm_ratio
            p.power_kw = 1.6 * rpm_ratio ** 3

    # -------------------------
    # Thermal
    # -------------------------
    def _update_thermal(self, p, s: PlantState, dt: float) -> None:
        ambient = s.env.ambient_temperature_c

        rpm_norm = clamp(p.rpm_actual / p.rpm_nom, 0.0, 2.0)
        teq = ambient + 70.0 * rpm_norm ** 2

        alpha = clamp(dt / 120.0, 0.0, 1.0)
        p.motor_temp += (teq - p.motor_temp) * alpha
        p.motor_temp = clamp(p.motor_temp, ambient, p.fault_temp)

        if p.motor_temp > p.limit_temp:
            p.overheat_seconds += dt
        else:
            p.overheat_seconds = max(0.0, p.overheat_seconds - dt)

    def _apply_off(self, p, s: PlantState, dt: float) -> None:
        ambient = s.env.ambient_temperature_c

        p.rpm_actual = 0.0
        p.flow_lpm = 0.0
        p.pressure_bar = 0.0
        p.power_kw = 0.0

        alpha = clamp(dt / 60.0, 0.0, 1.0)
        p.motor_temp += (ambient - p.motor_temp) * alpha
        p.motor_temp = clamp(p.motor_temp, ambient, p.fault_temp)
        p.overheat_seconds = max(0.0, p.overheat_seconds - dt)

    # -------------------------
    # Fault
    # -------------------------
    def _apply_hard_fault(self, p) -> None:
        if p.motor_temp >= p.fault_temp:
            p.state = "FAULT"
            p.rpm_actual = 0.0
            p.flow_lpm = 0.0
            p.pressure_bar = 0.0
            p.power_kw = 0.0
