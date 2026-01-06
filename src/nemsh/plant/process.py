# plant/process.py
from __future__ import annotations

import math
from dataclasses import dataclass

from .state import PlantState, clamp


@dataclass
class ProcessConfig:
    # =========================
    # Stabilizer
    # =========================
    normal_v_min: float = 190.0
    normal_v_max: float = 240.0
    bypass_v_min: float = 180.0
    bypass_v_max: float = 260.0
    v_nom: float = 220.0

    # =========================
    # IN pump (nominal точки)
    # =========================
    in_rpm_nom: float = 2500.0
    in_q_nom_lpm: float = 120.0
    in_p_clean_bar: float = 2.7          # тиск "до фільтра" при 2500rpm і wear=0
    in_p_nom_kw: float = 1.5             # потужність при 2500rpm і wear=0
    gamma_wear: float = 0.3333           # вплив wear на тиск/потік

    # =========================
    # OUT pump (nominal точки)
    # =========================
    out_rpm_nom: float = 2500.0
    out_q_nom_lpm: float = 110.0
    out_p_clean_bar: float = 3.0
    out_p_nom_kw: float = 1.6

    # =========================
    # Pump dynamics (actual follows desired)
    # =========================
    rpm_tau_s: float = 1.5  # інерція RPM (сек), dt використовується

    # =========================
    # Filter (MVP)
    # =========================
    raw_ntu_in: float = 2.0
    wear_k: float = 0.00278              # Δwear = (Q/60)*NTU*k*dt
    clean_rate_pct_per_s: float = 1.0    # BACKWASH: -1%/s
    out_pressure_bar_flowing: float = 0.2
    out_pressure_bar_no_flow: float = 0.0

    # Backwash water consumption from tank to waste (MVP)
    backwash_flow_lpm: float = 50.0

    # =========================
    # Thermal (MVP)
    # =========================
    motor_tau_heat_s: float = 120.0
    motor_tau_cool_s: float = 60.0


class PlantProcess:
    """
    Process/Physics.
    - Оновлює rpm_actual/flow/pressure/power/temp/wear/tank.
    - НІЯКИХ бізнес-правил типу "tank<=5 -> out off" тут немає (це controller).
    """

    def __init__(self, cfg: ProcessConfig | None = None):
        self.cfg = cfg or ProcessConfig()

    # ======================================================
    # MAIN STEP (physics only)
    # ======================================================
    def step(self, s: PlantState, dt: float) -> None:
        if dt <= 0:
            return

        self._update_stabilizer(s)
        self._update_pump(s, which="in_pump", dt=dt)
        self._update_pump(s, which="out_pump", dt=dt)

        self._update_filter(s, dt=dt)
        self._update_tank(s, dt=dt)

        self._update_stabilizer_active_power(s)

    # ======================================================
    # Stabilizer
    # ======================================================
    def _update_stabilizer(self, s: PlantState) -> None:
        vin = float(s.stabilizer.vin_v)
        cfg = self.cfg

        if cfg.normal_v_min <= vin <= cfg.normal_v_max:
            s.stabilizer.mode = "NORMAL"
            s.stabilizer.vout_v = cfg.v_nom
        elif cfg.bypass_v_min <= vin <= cfg.bypass_v_max:
            s.stabilizer.mode = "BYPASS"
            s.stabilizer.vout_v = vin
        else:
            s.stabilizer.mode = "FAULT"
            s.stabilizer.vout_v = 0.0

    def _update_stabilizer_active_power(self, s: PlantState) -> None:
        # filter в MVP не споживає, вся енергія на помпах
        s.stabilizer.active_power_kw = max(0.0, float(s.in_pump.power_kw)) + max(0.0, float(s.out_pump.power_kw))

    # ======================================================
    # Pump physics
    # ======================================================
    def _update_pump(self, s: PlantState, which: str, dt: float) -> None:
        cfg = self.cfg
        p = getattr(s, which)

        # supply voltage
        p.voltage_v = float(s.stabilizer.vout_v)
        vf = (p.voltage_v / cfg.v_nom) if cfg.v_nom > 0 else 0.0
        vf = clamp(vf, 0.0, 1.25)

        # if stabilizer fault -> no power
        if s.stabilizer.mode == "FAULT":
            p.state = "OFF"

        # OFF => everything to zero + cool
        if p.state == "OFF" or p.rpm_desired <= 0.0:
            p.rpm_actual = 0.0
            p.flow_lpm = 0.0
            p.pressure_bar = 0.0
            p.power_kw = 0.0
            self._cool_motor(p, s.env.ambient_temperature_c, dt)
            return

        # RPM actual inertia + voltage effect
        rpm_target = float(p.rpm_desired) * vf
        rpm_target = clamp(rpm_target, 0.0, float(p.rpm_max))

        # first-order approach: rpm += (target-rpm)*(dt/tau)
        tau = max(0.05, float(cfg.rpm_tau_s))
        alpha = clamp(dt / tau, 0.0, 1.0)
        p.rpm_actual = float(p.rpm_actual) + (rpm_target - float(p.rpm_actual)) * alpha
        p.rpm_actual = clamp(p.rpm_actual, 0.0, float(p.rpm_max))

        # hydraulics + power
        if which == "in_pump":
            self._compute_in_pump_hydraulics(s, p)
        else:
            self._compute_out_pump_hydraulics(p)

        # temperature update
        self._heat_motor(p, s.env.ambient_temperature_c, dt)

        # hard fault (процесний захист)
        if float(p.motor_temp_c) >= float(p.hard_limit_c):
            p.state = "FAULT"
            p.rpm_actual = 0.0
            p.flow_lpm = 0.0
            p.pressure_bar = 0.0
            p.power_kw = 0.0

    def _compute_in_pump_hydraulics(self, s: PlantState, p) -> None:
        cfg = self.cfg
        rpm = float(p.rpm_actual)

        w = clamp(float(s.filter.wear_pct) / 100.0, 0.0, 1.0)
        wear_factor = 1.0 + cfg.gamma_wear * (w ** 2)

        # Pressure before filter
        p.pressure_bar = cfg.in_p_clean_bar * (rpm / cfg.in_rpm_nom) ** 2 * wear_factor if rpm > 0 else 0.0

        # Flow through filter (опір з wear враховано ОДИН раз через wear_factor)
        p.flow_lpm = cfg.in_q_nom_lpm * (rpm / cfg.in_rpm_nom) / math.sqrt(wear_factor) if rpm > 0 else 0.0

        # Power: affinity ~ rpm^3, + додаткове навантаження через опір (sqrt(wear_factor))
        p.power_kw = cfg.in_p_nom_kw * (rpm / cfg.in_rpm_nom) ** 3 * math.sqrt(wear_factor) if rpm > 0 else 0.0

    def _compute_out_pump_hydraulics(self, p) -> None:
        cfg = self.cfg
        rpm = float(p.rpm_actual)

        p.pressure_bar = cfg.out_p_clean_bar * (rpm / cfg.out_rpm_nom) ** 2 if rpm > 0 else 0.0
        p.flow_lpm = cfg.out_q_nom_lpm * (rpm / cfg.out_rpm_nom) if rpm > 0 else 0.0
        p.power_kw = cfg.out_p_nom_kw * (rpm / cfg.out_rpm_nom) ** 3 if rpm > 0 else 0.0

    # ======================================================
    # Motor temperature (dt-aware)
    # ======================================================
    def _heat_motor(self, p, ambient_c: float, dt: float) -> None:
        cfg = self.cfg
        rpm_norm = (float(p.rpm_actual) / float(p.rpm_nom)) if float(p.rpm_nom) > 0 else 0.0
        rpm_norm = clamp(rpm_norm, 0.0, 2.0)

        # Teq: 2500 rpm -> ~90C (20 + 70*(1^2))
        teq = float(ambient_c) + 70.0 * (rpm_norm ** 2)
        teq = clamp(teq, float(ambient_c), 110.0)

        tau = max(0.05, float(cfg.motor_tau_heat_s))
        alpha = clamp(dt / tau, 0.0, 1.0)
        p.motor_temp_c = float(p.motor_temp_c) + (teq - float(p.motor_temp_c)) * alpha
        p.motor_temp_c = clamp(p.motor_temp_c, float(ambient_c), 120.0)

    def _cool_motor(self, p, ambient_c: float, dt: float) -> None:
        cfg = self.cfg
        tau = max(0.05, float(cfg.motor_tau_cool_s))
        alpha = clamp(dt / tau, 0.0, 1.0)
        p.motor_temp_c = float(p.motor_temp_c) + (float(ambient_c) - float(p.motor_temp_c)) * alpha
        p.motor_temp_c = clamp(p.motor_temp_c, float(ambient_c), 120.0)
        p.overheat_seconds = max(0.0, float(p.overheat_seconds) - dt)

    # ======================================================
    # Filter (dt-aware)
    # ======================================================
    def _update_filter(self, s: PlantState, dt: float) -> None:
        cfg = self.cfg

        Q = float(s.in_pump.flow_lpm) if s.in_pump.state == "ON" else 0.0

        s.filter.out_pressure_bar = cfg.out_pressure_bar_flowing if Q > 0 else cfg.out_pressure_bar_no_flow
        s.filter.in_pressure_bar = float(s.in_pump.pressure_bar) if Q > 0 else 0.0
        s.filter.delta_pressure_bar = max(0.0, float(s.filter.in_pressure_bar) - float(s.filter.out_pressure_bar))

        if s.filter.mode == "FILTER":
            if Q > 0:
                dw = (Q / 60.0) * cfg.raw_ntu_in * cfg.wear_k * dt
                s.filter.wear_pct = clamp(float(s.filter.wear_pct) + dw, 0.0, 100.0)

            # NTU output (MVP): після 50% wear починає погіршуватись
            if float(s.filter.wear_pct) <= 50.0:
                s.filter.ntu = 1.0
            else:
                x = (float(s.filter.wear_pct) - 50.0) / 50.0  # 0..1
                s.filter.ntu = 1.0 + 2.0 * clamp(x, 0.0, 1.0)  # до 3.0
            s.filter.ph = 7.0

        elif s.filter.mode == "BACKWASH":
            s.filter.wear_pct = max(
                float(s.filter.min_wear_after_backwash_pct),
                float(s.filter.wear_pct) - cfg.clean_rate_pct_per_s * dt,
            )
            s.filter.ntu = 3.0
            s.filter.ph = 7.0

        else:  # IDLE
            pass

    # ======================================================
    # Tank (dt-aware)
    # ======================================================
    def _update_tank(self, s: PlantState, dt: float) -> None:
        cfg = self.cfg

        inflow_lpm = float(s.in_pump.flow_lpm) if (s.filter.mode == "FILTER" and s.in_pump.state == "ON") else 0.0
        outflow_lpm = float(s.out_pump.flow_lpm) if (s.out_pump.state == "ON") else 0.0
        backwash_lpm = float(cfg.backwash_flow_lpm) if (s.filter.mode == "BACKWASH") else 0.0

        s.tank.in_flow_lpm = inflow_lpm
        s.tank.out_flow_lpm = outflow_lpm + backwash_lpm

        delta_liters = (s.tank.in_flow_lpm - s.tank.out_flow_lpm) * (dt / 60.0)
        s.tank.level_liters = clamp(float(s.tank.level_liters) + float(delta_liters), 0.0, float(s.tank.capacity_liters))

        if float(s.tank.capacity_liters) > 0:
            s.tank.level_pct = 100.0 * (float(s.tank.level_liters) / float(s.tank.capacity_liters))
        else:
            s.tank.level_pct = 0.0

        s.tank.level_rate_lps = (s.tank.in_flow_lpm - s.tank.out_flow_lpm) / 60.0
