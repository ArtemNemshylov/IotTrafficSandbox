from __future__ import annotations

import random

from src.nemsh.plant.state import PlantState, clamp


class FilterProcess:

    def __init__(self, seed: int = 42):
        self._rng = random.Random(seed)

    def step(self, s: PlantState, dt: float) -> None:
        if dt <= 0:
            return

        # NTU updates every tick
        s.filter.ntu = self._rng.uniform(1.0, 4.0)
        s.filter.ph = 7.0

        in_lpm = float(s.in_pump.flow_lpm)
        L = float(s.tank.level_pct)
        C = float(s.filter.wear_pct)

        # IDLE when no flow (unless already BACKWASH)
        if in_lpm <= 0.0 and s.filter.mode != "BACKWASH":
            s.filter.mode = "IDLE"
        elif in_lpm > 0.0 and s.filter.mode == "IDLE":
            s.filter.mode = "FILTER"

        # backwash AUTO thresholds
        Cthr = clamp(85.0 - 0.65 * L, 20.0, 85.0)
        start_backwash = (L >= 100.0) or (C >= max(20.0, Cthr))

        Ctarget = clamp(35.0 - 0.25 * L, 10.0, 35.0)
        stop_backwash = (C <= Ctarget) or (L <= float(s.tank.min_level_pct))

        if s.filter.mode != "BACKWASH":
            if start_backwash:
                s.filter.mode = "BACKWASH"
        else:
            if stop_backwash:
                s.filter.mode = "FILTER"

        s.filter.in_pressure_bar = float(s.in_pump.pressure_bar) if in_lpm > 0.0 else 0.0
        s.filter.out_pressure_bar = 0.2 if in_lpm > 0.0 else 0.0
        s.filter.delta_pressure_bar = max(0.0, float(s.filter.in_pressure_bar) - float(s.filter.out_pressure_bar))

        if s.filter.mode == "BACKWASH":
            s.filter.wear_pct = max(
                float(s.filter.min_wear_after_backwash_pct),
                float(s.filter.wear_pct) - 1.0 * dt,
            )
        elif s.filter.mode == "FILTER":
            dC = (in_lpm / 60.0) * float(s.filter.ntu) * 0.00278 * dt
            s.filter.wear_pct = clamp(float(s.filter.wear_pct) + dC, 0.0, 100.0)
        else:
            # IDLE
            pass
