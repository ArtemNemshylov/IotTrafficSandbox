# src/nemsh/plant/pump_process.py
from __future__ import annotations

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
        if dt <= 0:
            return

        # voltage from stabilizer
        p.voltage_v = float(s.stabilizer.output_voltage)

        # -------------------------
        # state rules (your logic)
        # -------------------------
        # FAULT: energy shortage OR motor_temp >= 110
        energy_shortage = (s.stabilizer.mode == "FAULT") or (p.voltage_v <= 0.0)

        if energy_shortage:
            p.state = "FAULT"
            self._apply_fault_zero(p)
            return

        if float(p.motor_temp) >= float(p.fault_temp):
            p.state = "FAULT"
            self._apply_fault_zero(p)
            return

        # OFF: MANUAL and user set OFF
        if p.mode == "MANUAL" and p.state == "OFF":
            self._apply_off(p, s, dt)
            return

        # IN pump OFF when tank=100% and filter clean (assume clean <=20%)
        if is_in_pump:
            filter_clean = float(s.filter.wear_pct) <= 20.0
            if float(s.tank.level_pct) >= 100.0 and filter_clean:
                p.state = "OFF"
                p.rpm_desired = 0.0
                self._apply_off(p, s, dt)
                return

        # OFF if rpm_desired <= 0
        if float(p.rpm_desired) <= 0.0:
            p.state = "OFF"
            self._apply_off(p, s, dt)
            return

        # ON: all other cases
        p.state = "ON"

        # -------------------------
        # rpm_actual (your rule)
        # -------------------------
        self._update_rpm(p, s)

        # -------------------------
        # hydraulics + power
        # -------------------------
        self._update_hydraulics(p, s, is_in_pump)

        # -------------------------
        # thermal
        # -------------------------
        self._update_thermal(p, s, dt)

        # hard fault after thermal update
        if float(p.motor_temp) >= float(p.fault_temp):
            p.state = "FAULT"
            self._apply_fault_zero(p)

    # -------------------------
    # RPM: rpm_actual = rpm_desired * (voltage/220)
    # -------------------------
    def _update_rpm(self, p, s: PlantState) -> None:
        vf = clamp(
            float(p.voltage_v) / float(s.stabilizer.nominal_voltage),
            0.0,
            1.25,
        )
        p.rpm_actual = clamp(float(p.rpm_desired) * vf, 0.0, float(p.rpm_max))

    # -------------------------
    # Hydraulics + power
    # -------------------------
    def _update_hydraulics(self, p, s: PlantState, is_in_pump: bool) -> None:
        rpm = float(p.rpm_actual)
        rpm_nom = float(p.rpm_nom) if float(p.rpm_nom) > 0 else 1.0

        if rpm <= 0.0:
            p.pressure_bar = 0.0
            p.flow_lpm = 0.0
            p.power_kw = 0.0
            return

        if is_in_pump:
            # constants (from your spec)
            p_clean = 2.7
            q_nom = 120.0
            y = 0.3333

            w = clamp(float(s.filter.wear_pct) / 100.0, 0.0, 1.0)
            wear_factor = 1.0 + y * (w ** 2)

            # pressure before filter
            p_base = p_clean * (rpm / rpm_nom) ** 2
            p.pressure_bar = p_base * wear_factor

            # flow
            p.flow_lpm = q_nom * (rpm / rpm_nom) * (1.0 / math.sqrt(wear_factor))

            # power
            p.power_kw = float(p.power_nom_kw) * (rpm / rpm_nom) ** 3 * math.sqrt(wear_factor)
        else:
            # OUT pump constants (from earlier; no wear)
            p_clean = 3.0
            q_nom = 110.0

            p.pressure_bar = p_clean * (rpm / rpm_nom) ** 2
            p.flow_lpm = q_nom * (rpm / rpm_nom)
            p.power_kw = float(p.power_nom_kw) * (rpm / rpm_nom) ** 3

    # -------------------------
    # Thermal: Teq = Tamb + 0.03*rpm, ΔT = (Teq - T)*dt/120
    # -------------------------
    def _update_thermal(self, p, s: PlantState, dt: float) -> None:
        ambient = float(s.env.ambient_temperature_c)

        teq = ambient + 0.03 * float(p.rpm_actual)
        alpha = clamp(dt / 120.0, 0.0, 1.0)

        p.motor_temp = float(p.motor_temp) + (teq - float(p.motor_temp)) * alpha
        p.motor_temp = clamp(float(p.motor_temp), ambient, float(p.fault_temp))

        if float(p.motor_temp) > float(p.limit_temp):
            p.overheat_seconds = float(p.overheat_seconds) + dt
        else:
            p.overheat_seconds = max(0.0, float(p.overheat_seconds) - dt)

    # -------------------------
    # OFF / FAULT helpers
    # -------------------------
    def _apply_off(self, p, s: PlantState, dt: float) -> None:
        ambient = float(s.env.ambient_temperature_c)

        p.rpm_actual = 0.0
        p.flow_lpm = 0.0
        p.pressure_bar = 0.0
        p.power_kw = 0.0

        # cooling to ambient: ΔT = (Tamb - T)*dt/60
        alpha = clamp(dt / 60.0, 0.0, 1.0)
        p.motor_temp = float(p.motor_temp) + (ambient - float(p.motor_temp)) * alpha
        p.motor_temp = clamp(float(p.motor_temp), ambient, float(p.fault_temp))

        p.overheat_seconds = max(0.0, float(p.overheat_seconds) - dt)

    def _apply_fault_zero(self, p) -> None:
        p.rpm_actual = 0.0
        p.flow_lpm = 0.0
        p.pressure_bar = 0.0
        p.power_kw = 0.0
