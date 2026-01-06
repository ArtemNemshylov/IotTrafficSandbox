# src/nemsh/plant/tank_process.py
from __future__ import annotations

from src.nemsh.plant.state import PlantState, clamp


class TankProcess:
    def step(self, s: PlantState, dt: float) -> None:
        if dt <= 0:
            return

        in_flow = float(s.tank.in_flow_lpm)
        out_flow = float(s.tank.out_flow_lpm)

        delta_liters = (in_flow - out_flow) * (dt / 60.0)
        s.tank.level_liters = clamp(
            float(s.tank.level_liters) + delta_liters,
            0.0,
            float(s.tank.capacity_liters),
        )

        if float(s.tank.capacity_liters) > 0.0:
            s.tank.level_pct = 100.0 * float(s.tank.level_liters) / float(s.tank.capacity_liters)
        else:
            s.tank.level_pct = 0.0

        s.tank.level_rate_lps = (in_flow - out_flow) / 60.0

        # keep bounds consistent
        s.tank.level_pct = clamp(float(s.tank.level_pct), 0.0, 100.0)
