# plant/controller.py
from __future__ import annotations

import random
from dataclasses import dataclass

from .state import PlantState, clamp


@dataclass
class ControllerConfig:
    # =========================
    # IN pump RPM limits
    # =========================
    in_rpm_min: float = 1000.0
    in_rpm_nom: float = 2500.0
    in_rpm_max: float = 4000.0

    # IN behavior by tank level
    in_max_rpm_below_pct: float = 30.0      # <=30% -> max rpm
    in_nominal_until_pct: float = 80.0      # <80% -> nominal rpm (except <=30%)
    in_min_rpm_from_pct: float = 95.0       # >=95% -> min rpm
    in_off_at_full_pct: float = 100.0       # >=100% -> OFF

    # rpm change speed (STRICT dt usage)
    in_rpm_slew_per_s: float = 800.0        # rpm per second

    # =========================
    # OUT pump RPM limits (+10%)
    # =========================
    out_rpm_min: float = 1000.0
    out_rpm_nom: float = 2500.0
    out_rpm_max: float = 4400.0

    out_off_at_pct: float = 5.0             # <=5% -> OFF
    out_limit_at_pct: float = 20.0          # <=20% -> clamp to min rpm

    out_rpm_slew_per_s: float = 900.0       # rpm per second

    # =========================
    # OUT блокування по фільтру
    # =========================
    out_block_level_pct: float = 20.0
    out_block_wear_pct: float = 85.0
    out_unblock_wear_pct: float = 50.0

    # confirm times (STRICT dt usage)
    out_block_confirm_s: float = 3.0
    out_unblock_confirm_s: float = 3.0

    # =========================
    # OUT demand factor (70..110% of IN capacity)
    # =========================
    demand_factor_min: float = 0.70
    demand_factor_max: float = 1.10
    demand_change_period_s: float = 3600.0  # 1 hour (STRICT dt usage)

    # reference capacities (MVP constants)
    in_capacity_lpm_at_nom: float = 120.0   # IN capacity at 2500 rpm, wear=0
    out_nom_flow_lpm: float = 110.0         # OUT flow at 2500 rpm (MVP)

    # =========================
    # FILTER BACKWASH
    # =========================
    backwash_min_start_wear_pct: float = 20.0
    backwash_force_at_full_pct: float = 100.0

    # Start threshold: C_thr(L) = clamp(thr_a - thr_b*L, 20, 85)
    thr_a: float = 85.0
    thr_b: float = 0.65

    # Stop target: C_target(L) = clamp(target_a - target_b*L, 10, 35)
    target_a: float = 35.0
    target_b: float = 0.25

    backwash_min_level_pct: float = 30.0
    backwash_min_duration_s: float = 10.0  # anti-chatter (STRICT dt usage)


class PlantController:
    """
    Controller (AUTO rules):
    - dt MUST be used: all key decisions include time-based ramp/timers.
    - Writes only setpoints and modes (no physics).
    """

    def __init__(self, cfg: ControllerConfig | None = None, seed: int = 42):
        self.cfg = cfg or ControllerConfig()
        self._rng = random.Random(seed)

        # internal timers (STRICT dt usage)
        self._demand_timer_s: float = self.cfg.demand_change_period_s
        self._backwash_elapsed_s: float = 0.0

        self._out_blocked_by_filter: bool = False
        self._out_block_timer_s: float = 0.0
        self._out_unblock_timer_s: float = 0.0

        self._out_demand_factor: float = 0.90

    # ======================================================
    # MAIN ENTRY
    # ======================================================
    def compute(self, s: PlantState, dt: float) -> None:
        if dt <= 0:
            return

        L = float(s.tank.level_pct)
        C = float(s.filter.wear_pct)

        self._update_out_demand_factor(dt)
        self._control_filter_mode(s, L, C, dt)
        self._control_in_pump(s, L, dt)
        self._control_out_pump(s, L, C, dt)

    # ======================================================
    # Helpers (STRICT dt usage)
    # ======================================================
    @staticmethod
    def _slew_to(current: float, target: float, slew_per_s: float, dt: float) -> float:
        max_step = abs(slew_per_s) * dt
        delta = target - current
        if abs(delta) <= max_step:
            return target
        return current + (max_step if delta > 0 else -max_step)

    # ======================================================
    # FILTER thresholds
    # ======================================================
    def _start_threshold_wear(self, L: float) -> float:
        thr = self.cfg.thr_a - self.cfg.thr_b * L
        return clamp(thr, 20.0, 85.0)

    def _stop_target_wear(self, L: float) -> float:
        tgt = self.cfg.target_a - self.cfg.target_b * L
        return clamp(tgt, 10.0, 35.0)

    # ======================================================
    # FILTER control (STRICT dt usage)
    # ======================================================
    def _control_filter_mode(self, s: PlantState, L: float, C: float, dt: float) -> None:
        cfg = self.cfg

        if s.filter.mode == "BACKWASH":
            self._backwash_elapsed_s += dt

            target = self._stop_target_wear(L)
            can_stop = self._backwash_elapsed_s >= cfg.backwash_min_duration_s

            if can_stop and ((C <= target) or (L <= cfg.backwash_min_level_pct)):
                s.filter.mode = "FILTER"
                self._backwash_elapsed_s = 0.0
            return

        # not in backwash
        self._backwash_elapsed_s = 0.0

        thr = self._start_threshold_wear(L)
        start = (L >= cfg.backwash_force_at_full_pct) or (C >= max(cfg.backwash_min_start_wear_pct, thr))

        if start:
            s.filter.mode = "BACKWASH"
            self._backwash_elapsed_s = 0.0

    # ======================================================
    # IN pump RPM target
    # ======================================================
    def _in_rpm_target_by_level(self, L: float) -> float:
        cfg = self.cfg

        if L >= cfg.in_off_at_full_pct:
            return 0.0

        if L <= cfg.in_max_rpm_below_pct:
            return cfg.in_rpm_max

        if L < cfg.in_nominal_until_pct:
            return cfg.in_rpm_nom

        if L >= cfg.in_min_rpm_from_pct:
            return cfg.in_rpm_min

        # Stepdown only in [80..95): from 2500 -> 1000 in 5% steps
        # bands: [80-85), [85-90), [90-95)
        band_start = cfg.in_nominal_until_pct
        band_end = cfg.in_min_rpm_from_pct
        step = 5.0
        steps_count = int((band_end - band_start) // step)  # 3
        idx = int((L - band_start) // step)                 # 0..2
        idx = max(0, min(idx, steps_count - 1))

        frac = idx / (steps_count - 1) if steps_count > 1 else 1.0
        rpm = cfg.in_rpm_nom - (cfg.in_rpm_nom - cfg.in_rpm_min) * frac
        return float(int(rpm))

    # ======================================================
    # IN pump control (STRICT dt usage: rpm ramp)
    # ======================================================
    def _control_in_pump(self, s: PlantState, L: float, dt: float) -> None:
        if s.in_pump.mode != "AUTO":
            return

        # during backwash: stop IN (MVP)
        target = 0.0 if s.filter.mode == "BACKWASH" else self._in_rpm_target_by_level(L)

        target = clamp(target, 0.0, s.in_pump.rpm_max)
        target = 0.0 if target <= 0 else clamp(target, s.in_pump.rpm_min, s.in_pump.rpm_max)

        # STRICT dt usage: ramp rpm_desired to target
        s.in_pump.rpm_desired = self._slew_to(
            current=float(s.in_pump.rpm_desired),
            target=float(target),
            slew_per_s=self.cfg.in_rpm_slew_per_s,
            dt=float(dt),
        )

        s.in_pump.state = "ON" if s.in_pump.rpm_desired >= s.in_pump.rpm_min else "OFF"

    # ======================================================
    # OUT demand factor (STRICT dt usage)
    # ======================================================
    def _update_out_demand_factor(self, dt: float) -> None:
        self._demand_timer_s -= dt
        if self._demand_timer_s <= 0.0:
            self._out_demand_factor = self._rng.uniform(self.cfg.demand_factor_min, self.cfg.demand_factor_max)
            # keep drift stable even if dt is large
            self._demand_timer_s = self.cfg.demand_change_period_s + self._demand_timer_s

    # ======================================================
    # OUT pump control (STRICT dt usage: block timers + rpm ramp)
    # ======================================================
    def _control_out_pump(self, s: PlantState, L: float, C: float, dt: float) -> None:
        if s.out_pump.mode != "AUTO":
            return

        cfg = self.cfg

        # ---- block/unblock confirmation (STRICT dt usage) ----
        want_block = (L < cfg.out_block_level_pct) and (C >= cfg.out_block_wear_pct)
        want_unblock = (C <= cfg.out_unblock_wear_pct)

        if not self._out_blocked_by_filter:
            if want_block:
                self._out_block_timer_s += dt
                if self._out_block_timer_s >= cfg.out_block_confirm_s:
                    self._out_blocked_by_filter = True
                    self._out_block_timer_s = 0.0
            else:
                self._out_block_timer_s = 0.0
        else:
            if want_unblock:
                self._out_unblock_timer_s += dt
                if self._out_unblock_timer_s >= cfg.out_unblock_confirm_s:
                    self._out_blocked_by_filter = False
                    self._out_unblock_timer_s = 0.0
            else:
                self._out_unblock_timer_s = 0.0

        # ---- target rpm based on rules ----
        if (L <= cfg.out_off_at_pct) or self._out_blocked_by_filter:
            target = 0.0
        elif L <= cfg.out_limit_at_pct:
            target = cfg.out_rpm_min
        else:
            # demand-based
            desired_flow_lpm = cfg.in_capacity_lpm_at_nom * self._out_demand_factor
            target = cfg.out_rpm_nom * (desired_flow_lpm / cfg.out_nom_flow_lpm)
            target = clamp(target, cfg.out_rpm_min, cfg.out_rpm_max)

        # ensure target bounds
        target = clamp(target, 0.0, s.out_pump.rpm_max)
        target = 0.0 if target <= 0 else clamp(target, s.out_pump.rpm_min, s.out_pump.rpm_max)

        # STRICT dt usage: ramp rpm_desired to target
        s.out_pump.rpm_desired = self._slew_to(
            current=float(s.out_pump.rpm_desired),
            target=float(target),
            slew_per_s=cfg.out_rpm_slew_per_s,
            dt=float(dt),
        )

        s.out_pump.state = "ON" if s.out_pump.rpm_desired >= s.out_pump.rpm_min else "OFF"
