# process.py
import math
import random

from .state import PlantState


class PlantProcess:
    """
    Уся причинно-наслідкова логіка тут.
    """

    # ----------------------------
    # Конфіг політик (параметри)
    # ----------------------------
    NTU_POTABLE_MAX = 1.0
    NTU_ALARM_HYST = 0.15

    # Electrical / Stabilizer
    GRID_V_FAULT_LOW = 190.0
    GRID_V_BYPASS_HIGH = 240.0
    VOUT_MIN_RUN = 160.0

    STAB_TAU_S = 240.0
    STAB_K_TEMP_PER_KW = 12.0
    STAB_T_FAULT = 115.0

    # Filter model
    FILTER_DELTA_P_CLEAN = 0.15
    FILTER_WEAR_MULT = 7.0
    FILTER_WEAR_FLOOR = 8.0

    WEAR_RATE_100PCT_AT_FLOWNOM_S = 8 * 3600
    BACKWASH_CLEAN_RATE_PCT_S = 1.0
    BACKWASH_RPM_PCT = 45.0

    # Pump thermals (ВАЖЛИВО: перегрів тригериться лише на very-high rpm)
    MOTOR_T_MAX = 110.0
    MOTOR_T_FAULT = 110.0

    # Overheat behavior from spec:
    # - на номінальних обертах (rpm_nom) не перегрівається
    # - перегрів/дерейтинг починається після 5 хв, але тільки коли rpm у діапазоні ~3200..max
    HIGH_RPM_TIME_S = 300.0
    COOLDOWN_TIME_S = 180.0
    COOLDOWN_RPM_CAP_PCT = 75.0

    # Thermal model: target(T_eq) + інерція
    # Підганяємо так, щоб:
    # - rpm_nom (~3000) => T_eq ~ 70..80 (не заходить у fault)
    # - rpm_max => T_eq близько до 100+ (після часу може підійти до fault)
    TAU_HEAT_S = 140.0
    TAU_COOL_S = 420.0
    TAU_COOL_ACTIVE_S = 90.0

    # Для T_eq використовуємо piecewise:
    # base: від ambient до ~80 на rpm_nom
    # extra: різко додається тільки вище rpm_nom
    BASE_DELTA_AT_NOM = 60.0     # 20 + 60 = 80°C на номіналі (без сильного опору)
    EXTRA_DELTA_AT_MAX = 35.0    # додатково на max, щоб високі rpm реально гріли

    # Out demand
    DEMAND_MIN = 0.7
    DEMAND_MAX = 1.1

    def __init__(self, state: PlantState):
        self.state = state

    # ======================================================
    # MAIN STEP
    # ======================================================

    def step(self, dt: float = 1.0):
        s = self.state
        dt = float(dt)
        s.time_seconds += int(dt)

        self._update_storage_derived()
        self._update_demand(dt)

        self._update_electrical_pre(dt)

        self._update_latches_and_modes(dt)

        in_rpm_target = self._compute_in_rpm_target()
        out_rpm_target = self._compute_out_rpm_target()

        self._update_in_pump(dt, in_rpm_target)
        self._update_filter(dt)
        self._update_out_pump(dt, out_rpm_target)

        self._update_storage(dt)
        self._update_electrical_post(dt)

        self._apply_hard_power_interlock()

        self._update_storage_derived()

    # ======================================================
    # ELECTRICAL
    # ======================================================

    def _calc_total_power_consumption_kw(self) -> float:
        """
        Total instantaneous electrical power consumption of the plant (kW).
        Single source of truth.
        """
        s = self.state

        total_kw = 0.0

        # Pumps
        total_kw += s.in_pump_power_kw
        total_kw += s.out_pump_power_kw

        # Filter system (auxiliary load)
        if s.filter_mode in ("FILTER", "BACKWASH"):
            total_kw += 0.25

        # Control system (PLC, sensors, gateway)
        total_kw += 0.08  # 80 W

        return total_kw

    def _calc_in_pump_power_kw(self) -> float:
        s = self.state
        if s.in_pump_rpm <= 0 or s.in_pump_state != "ON":
            return 0.0

        rpm_actual = s.in_pump_rpm * (
                s.in_pump_voltage_v / s.stabilizer_nominal_voltage
        )

        rpm_ratio = rpm_actual / s.in_pump_rpm_nom

        wear_ratio = s.filter_wear_pct / 100.0
        wear_multiplier = (1.0 + 0.3333 * (wear_ratio ** 2)) ** 0.5

        power = (
                s.in_pump_power_nom_kw
                * (rpm_ratio ** 3)
                * wear_multiplier
        )

        return power

    def _calc_out_pump_power_kw(self) -> float:
        s = self.state
        if s.out_pump_rpm <= 0 or s.out_pump_state != "ON":
            return 0.0

        rpm_actual = s.out_pump_rpm * (
                s.out_pump_voltage_v / s.stabilizer_nominal_voltage
        )

        rpm_ratio = rpm_actual / s.out_pump_rpm_nom

        power = (
                s.out_pump_power_nom_kw
                * (rpm_ratio ** 3)
        )

        return power

    def _update_grid_voltage(self, dt: float):
        s = self.state

        # 1. Base voltage (most of the time)
        target_v = 220.0 + random.uniform(-1.5, 1.5)

        # 2. Rare events
        p = random.random()
        if p < 0.01:
            target_v += random.choice([-30.0, -20.0, 20.0, 30.0])
        elif p < 0.05:
            target_v += random.choice([-10.0, -8.0, 8.0, 10.0])

        # 3. Inertia
        tau = 8.0  # seconds
        s.stabilizer_input_voltage += (
                target_v - s.stabilizer_input_voltage
                                      ) * (dt / tau)

        # 4. Physical limits
        s.stabilizer_input_voltage = max(
            0.0,
            min(s.stabilizer_input_voltage, 260.0)
        )

    def _update_electrical_pre(self, dt: float):
        s = self.state

        # Severe undervoltage → FAULT
        if s.stabilizer_input_voltage < self.GRID_V_FAULT_LOW:
            s.stabilizer_state = "FAULT"
            s.stabilizer_output_voltage = 0.0
            return

        # Overvoltage → BYPASS
        if s.stabilizer_input_voltage > self.GRID_V_BYPASS_HIGH:
            s.stabilizer_state = "BYPASS"
            s.stabilizer_output_voltage = max(
                0.0, s.stabilizer_input_voltage
            )
            return

        # Normal regulation
        s.stabilizer_state = "NORMAL"
        s.stabilizer_output_voltage = s.stabilizer_nominal_voltage

    def _update_electrical_post(self, dt: float):
        s = self.state

        # Recalculate pump power
        s.in_pump_power_kw = self._calc_in_pump_power_kw()
        s.out_pump_power_kw = self._calc_out_pump_power_kw()

        # Total load
        s.stabilizer_load_kw = self._calc_total_power_consumption_kw()

        # Thermal model
        T_eq = (
                s.ambient_temperature_c
                + self.STAB_K_TEMP_PER_KW * s.stabilizer_load_kw
        )

        s.stabilizer_internal_temperature += (
                                                     T_eq - s.stabilizer_internal_temperature
                                             ) * (dt / self.STAB_TAU_S)

        # Overtemperature protection
        if s.stabilizer_internal_temperature >= self.STAB_T_FAULT:
            s.stabilizer_state = "FAULT"
            s.stabilizer_output_voltage = 0.0

    def _apply_hard_power_interlock(self):
        s = self.state
        if s.stabilizer_mode != "FAULT" and s.stabilizer_vout_v >= self.VOUT_MIN_RUN:
            return

        s.in_pump_state = "OFF"
        s.out_pump_state = "OFF"

        s.in_pump_rpm = s.in_pump_flow_lpm = s.in_pump_pressure_bar = s.in_pump_power_kw = 0.0
        s.out_pump_rpm = s.out_pump_flow_lpm = s.out_pump_pressure_bar = s.out_pump_power_kw = 0.0

        s.filter_mode = "IDLE"
        s.tank_in_flow_lpm = 0.0
        s.tank_out_flow_lpm = 0.0

    # ======================================================
    # MODES / LATCHES / QUALITY
    # ======================================================

    def _update_latches_and_modes(self, dt: float):
        s = self.state

        # Quality alarm hysteresis
        if s.filter_quality_alarm:
            if s.ntu_out <= self.NTU_POTABLE_MAX - self.NTU_ALARM_HYST:
                s.filter_quality_alarm = False
        else:
            if s.ntu_out >= self.NTU_POTABLE_MAX:
                s.filter_quality_alarm = True

        # LATCH: level<20 AND wear>=85 => OUT OFF until wear<=50
        if s.out_blocked_low_level_filter:
            if s.filter_wear_pct <= 50.0:
                s.out_blocked_low_level_filter = False
        else:
            if s.tank_level_pct < s.tank_out_limit_level_pct and s.filter_wear_pct >= 85.0:
                s.out_blocked_low_level_filter = True

        # Block reason (UI)
        if s.tank_level_pct <= s.tank_min_level_pct:
            s.out_block_reason = "LOW_LEVEL_DRY_RUN"
        elif s.out_blocked_low_level_filter:
            s.out_block_reason = "LOW_LEVEL_AND_WEAR_CRITICAL"
        elif s.filter_quality_alarm:
            s.out_block_reason = "WATER_QUALITY_ALARM"
        else:
            s.out_block_reason = ""

        # Filter mode FSM
        if s.stabilizer_mode == "FAULT" or s.stabilizer_vout_v < self.VOUT_MIN_RUN:
            s.filter_mode = "IDLE"
            s.filter_backwash_elapsed_s = 0.0
            return

        if s.in_pump_cmd_state == "OFF":
            s.filter_mode = "IDLE"
            s.filter_backwash_elapsed_s = 0.0
            return

        can_backwash = s.tank_level_pct >= s.tank_backwash_start_level_pct
        need_backwash = (s.filter_wear_pct >= 40.0) or (s.filter_quality_alarm and s.filter_wear_pct >= 50.0)

        if s.filter_mode != "BACKWASH":
            if can_backwash and need_backwash:
                s.filter_mode = "BACKWASH"
                s.filter_backwash_elapsed_s = 0.0
            else:
                s.filter_mode = "FILTER"
        else:
            s.filter_backwash_elapsed_s += dt
            stop_by_level = s.tank_level_pct <= s.tank_backwash_stop_level_pct
            stop_by_wear = s.filter_wear_pct <= 15.0
            stop_by_time = s.filter_backwash_elapsed_s >= s.filter_backwash_max_s
            if stop_by_level or stop_by_wear or stop_by_time:
                s.filter_mode = "FILTER"
                s.filter_backwash_elapsed_s = 0.0

    # ======================================================
    # CONTROLLERS (rpm targets)
    # ======================================================

    def _compute_in_rpm_target(self) -> float:
        s = self.state

        if s.in_pump_cmd_state == "OFF":
            return 0.0

        # overflow / max level => IN OFF
        if s.tank_level_pct >= s.tank_max_level_pct:
            return 0.0

        # BACKWASH: качаємо на промивку (в бак притоку не буде)
        if s.filter_mode == "BACKWASH":
            return s.in_pump_rpm_max * (self.BACKWASH_RPM_PCT / 100.0)

        # MANUAL
        if s.in_pump_cmd_mode == "MANUAL":
            return s.in_pump_rpm_max * (max(0.0, min(100.0, s.in_pump_cmd_rpm_pct)) / 100.0)

        # AUTO (оновлена логіка):
        # 1) якщо <20% => 100%
        if s.tank_level_pct < s.tank_in_emergency_level_pct:
            return s.in_pump_rpm_max

        # 2) якщо <80% => ТІЛЬКИ номінальні оберти
        if s.tank_level_pct < s.tank_in_nominal_until_pct:
            return s.in_pump_rpm_nom

        # 3) якщо >=80% і < max => мінімальні (підтримка/плавно)
        return s.in_pump_rpm_min

    def _compute_out_rpm_target(self) -> float:
        s = self.state

        if s.out_pump_cmd_state == "OFF":
            return 0.0

        # hard dry-run защит
        if s.tank_level_pct <= s.tank_min_level_pct:
            return 0.0

        # latch interlock (має перебивати навіть MANUAL)
        if s.out_blocked_low_level_filter:
            return 0.0

        # quality interlock
        if s.filter_quality_alarm:
            return 0.0

        # MANUAL
        if s.out_pump_cmd_mode == "MANUAL":
            return s.out_pump_rpm_max * (max(0.0, min(100.0, s.out_pump_cmd_rpm_pct)) / 100.0)

        # AUTO: target flow = demand
        target_flow = max(0.0, s.out_demand_lpm)

        # low-level limit: <20% => cap flow
        if s.tank_level_pct < s.tank_out_limit_level_pct:
            target_flow = min(target_flow, 0.5 * s.out_pump_flow_nom_lpm)

        rpm = (target_flow / max(1e-6, s.out_pump_flow_nom_lpm)) * s.out_pump_rpm_nom
        rpm = max(0.0, min(s.out_pump_rpm_max, rpm))
        return rpm

    def _update_demand(self, dt: float):
        s = self.state
        s.demand_window_remaining_s -= int(dt)

        if s.demand_window_remaining_s <= 0:
            s.demand_window_remaining_s = s.demand_window_s
            t = max(1, s.time_seconds)
            u = (math.sin(t * 0.00123) + 1.0) * 0.5
            s.out_demand_factor = self.DEMAND_MIN + (self.DEMAND_MAX - self.DEMAND_MIN) * u

        in_capacity_ref = s.in_pump_flow_nom_lpm * (s.in_pump_rpm_max / s.in_pump_rpm_nom)
        s.out_demand_lpm = s.out_demand_factor * in_capacity_ref

    # ======================================================
    # PUMPS
    # ======================================================

    def _update_in_pump(self, dt: float, rpm_target: float):
        self._update_pump(
            dt=dt,
            rpm_target=rpm_target,
            system_resistance=self._compute_system_resistance_in(),
            state_attr_prefix="in_pump",
        )

    def _update_out_pump(self, dt: float, rpm_target: float):
        self._update_pump(
            dt=dt,
            rpm_target=rpm_target,
            system_resistance=0.35,
            state_attr_prefix="out_pump",
        )

    def _update_pump(self, dt: float, rpm_target: float, system_resistance: float, state_attr_prefix: str):
        s = self.state

        rpm_min = getattr(s, f"{state_attr_prefix}_rpm_min")
        rpm_nom = getattr(s, f"{state_attr_prefix}_rpm_nom")
        rpm_max = getattr(s, f"{state_attr_prefix}_rpm_max")
        flow_nom = getattr(s, f"{state_attr_prefix}_flow_nom_lpm")
        pressure_nom = getattr(s, f"{state_attr_prefix}_pressure_nom_bar")
        power_nom = getattr(s, f"{state_attr_prefix}_power_nom_kw")

        cmd_state = getattr(s, f"{state_attr_prefix}_cmd_state")
        state = getattr(s, f"{state_attr_prefix}_state")
        motor_temp = getattr(s, f"{state_attr_prefix}_motor_temp_c")
        high_rpm_time = getattr(s, f"{state_attr_prefix}_high_rpm_time_s")
        cooldown_rem = getattr(s, f"{state_attr_prefix}_cooldown_remaining_s")

        vout_v = s.stabilizer_vout_v
        ambient_c = s.ambient_temperature_c

        setattr(s, f"{state_attr_prefix}_voltage_v", vout_v)

        # hard off
        if cmd_state == "OFF" or vout_v < self.VOUT_MIN_RUN or s.stabilizer_mode == "FAULT":
            setattr(s, f"{state_attr_prefix}_state", "OFF")
            setattr(s, f"{state_attr_prefix}_fault_code", "")
            setattr(s, f"{state_attr_prefix}_rpm", 0.0)
            setattr(s, f"{state_attr_prefix}_flow_lpm", 0.0)
            setattr(s, f"{state_attr_prefix}_pressure_bar", 0.0)
            setattr(s, f"{state_attr_prefix}_power_kw", 0.0)

            motor_temp = self._cool_to_ambient(motor_temp, ambient_c, dt, active=False)
            setattr(s, f"{state_attr_prefix}_motor_temp_c", motor_temp)

            setattr(s, f"{state_attr_prefix}_high_rpm_time_s", 0.0)
            setattr(s, f"{state_attr_prefix}_cooldown_remaining_s", 0.0)
            return

        # sticky fault (MVP)
        if state == "FAULT":
            setattr(s, f"{state_attr_prefix}_rpm", 0.0)
            setattr(s, f"{state_attr_prefix}_flow_lpm", 0.0)
            setattr(s, f"{state_attr_prefix}_pressure_bar", 0.0)
            setattr(s, f"{state_attr_prefix}_power_kw", 0.0)
            motor_temp = self._cool_to_ambient(motor_temp, ambient_c, dt, active=True)
            setattr(s, f"{state_attr_prefix}_motor_temp_c", motor_temp)
            return

        setattr(s, f"{state_attr_prefix}_state", "ON")
        setattr(s, f"{state_attr_prefix}_fault_code", "")

        # derate: cooldown cap
        rpm_cap_pct = 100.0
        if cooldown_rem > 0:
            rpm_cap_pct = min(rpm_cap_pct, self.COOLDOWN_RPM_CAP_PCT)
            cooldown_rem = max(0.0, cooldown_rem - dt)

        # voltage cap
        voltage_factor = max(0.0, min(vout_v / 220.0, 1.1))
        rpm_cap_voltage = rpm_max * voltage_factor

        rpm_target = max(0.0, min(rpm_max, rpm_target))
        rpm_cap = min(rpm_cap_voltage, rpm_max * (rpm_cap_pct / 100.0))
        rpm_cmd = min(rpm_target, rpm_cap)

        # rpm inertia
        rpm_prev = getattr(s, f"{state_attr_prefix}_rpm")
        rpm = rpm_prev + (rpm_cmd - rpm_prev) * 0.25
        rpm = max(0.0, min(rpm_cap, rpm))

        # flow
        flow_base = flow_nom * (rpm / max(1e-6, rpm_nom))
        flow_eff = 1.0 / (1.0 + 1.8 * max(0.0, system_resistance))
        flow = flow_base * flow_eff

        # pressure
        pressure = pressure_nom * (rpm / max(1e-6, rpm_nom)) ** 2 * (1.0 + 1.2 * system_resistance)

        # power
        power = power_nom * (rpm / max(1e-6, rpm_nom)) ** 3 * (1.0 + 1.0 * system_resistance)
        power = max(0.0, power)

        # -------- thermal target (piecewise, tuned to requirements) --------
        # base term up to nominal
        x_base = min(1.0, rpm / max(1e-6, rpm_nom))
        base_delta = self.BASE_DELTA_AT_NOM * (x_base ** 2)

        # extra term only above nominal
        if rpm <= rpm_nom:
            extra_delta = 0.0
        else:
            x_extra = (rpm - rpm_nom) / max(1e-6, (rpm_max - rpm_nom))
            extra_delta = self.EXTRA_DELTA_AT_MAX * (x_extra ** 2)

        # resistance raises equilibrium a bit
        resist_mult = 1.0 + 0.6 * system_resistance

        T_eq = ambient_c + (base_delta + extra_delta) * resist_mult

        # inertia
        tau = self.TAU_HEAT_S if rpm > 0 else self.TAU_COOL_S
        motor_temp += (T_eq - motor_temp) * (dt / tau)

        # tiny wobble
        t = s.time_seconds
        motor_temp += 0.05 * math.sin(0.09 * t + (0.3 if state_attr_prefix == "in_pump" else 1.0))

        # -------- high rpm timer (ONLY in very-high rpm zone) --------
        # For IN: this matches ~3200..3600. For OUT: zone shifts with its higher max.
        high_rpm_threshold = max(3200.0, 0.9 * rpm_max)

        if rpm >= high_rpm_threshold:
            high_rpm_time += dt
        else:
            high_rpm_time = max(0.0, high_rpm_time - 2.0 * dt)

        # cooldown trigger ONLY by time in high rpm zone
        if high_rpm_time >= self.HIGH_RPM_TIME_S:
            cooldown_rem = max(cooldown_rem, self.COOLDOWN_TIME_S)
            high_rpm_time = 0.0
            motor_temp = self._cool_to_ambient(motor_temp, ambient_c, dt, active=True)

        # fault if max temp exceeded
        if motor_temp >= self.MOTOR_T_FAULT:
            setattr(s, f"{state_attr_prefix}_state", "FAULT")
            setattr(s, f"{state_attr_prefix}_fault_code", "OVERHEAT")
            setattr(s, f"{state_attr_prefix}_rpm", 0.0)
            setattr(s, f"{state_attr_prefix}_flow_lpm", 0.0)
            setattr(s, f"{state_attr_prefix}_pressure_bar", 0.0)
            setattr(s, f"{state_attr_prefix}_power_kw", 0.0)
            setattr(s, f"{state_attr_prefix}_motor_temp_c", motor_temp)
            setattr(s, f"{state_attr_prefix}_high_rpm_time_s", 0.0)
            setattr(s, f"{state_attr_prefix}_cooldown_remaining_s", cooldown_rem)
            return

        setattr(s, f"{state_attr_prefix}_rpm", rpm)
        setattr(s, f"{state_attr_prefix}_flow_lpm", flow)
        setattr(s, f"{state_attr_prefix}_pressure_bar", pressure)
        setattr(s, f"{state_attr_prefix}_power_kw", power)
        setattr(s, f"{state_attr_prefix}_motor_temp_c", max(ambient_c, min(motor_temp, self.MOTOR_T_MAX)))
        setattr(s, f"{state_attr_prefix}_high_rpm_time_s", high_rpm_time)
        setattr(s, f"{state_attr_prefix}_cooldown_remaining_s", cooldown_rem)

    def _cool_to_ambient(self, temp_c: float, ambient_c: float, dt: float, active: bool) -> float:
        tau = self.TAU_COOL_ACTIVE_S if active else self.TAU_COOL_S
        temp_c += (ambient_c - temp_c) * (dt / tau)
        return temp_c

    # ======================================================
    # FILTER
    # ======================================================

    def _compute_system_resistance_in(self) -> float:
        s = self.state
        w = max(0.0, min(100.0, s.filter_wear_pct)) / 100.0
        return 0.25 + 1.2 * (w ** 2)

    def _update_filter(self, dt: float):
        s = self.state

        if s.in_pump_state != "ON" or s.filter_mode == "IDLE":
            s.filter_mode = "IDLE"
            s.filter_in_pressure_bar = 0.0
            s.filter_out_pressure_bar = 0.0
            s.filter_delta_p_bar = max(self.FILTER_DELTA_P_CLEAN, s.filter_delta_p_bar * 0.99)
            s.ntu_out = max(0.3, min(5.0, s.ntu_out + 0.01 * math.sin(0.03 * s.time_seconds)))
            s.ph_out = s.ph_in
            return

        flow = max(0.0, s.in_pump_flow_lpm)

        w = max(0.0, min(100.0, s.filter_wear_pct)) / 100.0
        flow_ratio = flow / max(1e-6, s.in_pump_flow_nom_lpm)
        wear_mult = 1.0 + self.FILTER_WEAR_MULT * (w ** 2)

        s.filter_delta_p_bar = self.FILTER_DELTA_P_CLEAN * (flow_ratio ** 2) * wear_mult
        s.filter_delta_p_bar = max(self.FILTER_DELTA_P_CLEAN, min(2.5, s.filter_delta_p_bar))

        s.filter_in_pressure_bar = s.in_pump_pressure_bar
        s.filter_out_pressure_bar = max(0.0, s.filter_in_pressure_bar - s.filter_delta_p_bar)

        if s.filter_mode == "FILTER":
            ntu_factor = 0.8 + 0.6 * max(0.0, min(5.0, s.ntu_in)) / 5.0
            wear_inc = (100.0 / self.WEAR_RATE_100PCT_AT_FLOWNOM_S) * flow_ratio * ntu_factor * dt
            s.filter_wear_pct = min(100.0, s.filter_wear_pct + wear_inc)
        elif s.filter_mode == "BACKWASH":
            wear_dec = self.BACKWASH_CLEAN_RATE_PCT_S * dt
            s.filter_wear_pct = max(self.FILTER_WEAR_FLOOR, s.filter_wear_pct - wear_dec)

        # NTU out depends on wear
        removal_eff = 0.75 - 0.55 * w
        removal_eff = max(0.05, min(0.9, removal_eff))

        if s.filter_wear_pct >= 50.0 and s.ntu_in >= 1.5:
            removal_eff *= 0.65

        ntu_out = s.ntu_in * (1.0 - removal_eff)
        ntu_out += 0.05 * math.sin(0.11 * s.time_seconds)
        s.ntu_out = max(0.2, min(10.0, ntu_out))
        s.ph_out = s.ph_in

    # ======================================================
    # STORAGE
    # ======================================================

    def _update_storage(self, dt: float):
        s = self.state

        inflow = s.in_pump_flow_lpm if (s.in_pump_state == "ON" and s.filter_mode == "FILTER") else 0.0
        outflow = s.out_pump_flow_lpm if s.out_pump_state == "ON" else 0.0

        s.tank_in_flow_lpm = inflow
        s.tank_out_flow_lpm = outflow

        delta_liters = (inflow - outflow) * (dt / 60.0)
        s.tank_level_liters = max(0.0, min(s.tank_capacity_liters, s.tank_level_liters + delta_liters))
        s.tank_overflow = (s.tank_level_liters >= s.tank_capacity_liters)

    def _update_storage_derived(self):
        s = self.state
        if s.tank_capacity_liters <= 0:
            s.tank_level_pct = 0.0
            s.tank_level_rate_pct_s = 0.0
            return

        prev_pct = s.tank_level_pct
        s.tank_level_pct = (s.tank_level_liters / s.tank_capacity_liters) * 100.0
        s.tank_level_pct = max(0.0, min(100.0, s.tank_level_pct))
        s.tank_level_rate_pct_s = (s.tank_level_pct - prev_pct)
