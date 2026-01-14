from ..state import PlantState, clamp

class TankProcess:
    def step(self, s: PlantState, dt: float) -> None:
        inflow = (
            s.in_pump.flow_lpm
            if s.filter.mode == "FILTER" and s.in_pump.state == "ON"
            else 0.0
        )
        outflow = s.out_pump.flow_lpm if s.out_pump.state == "ON" else 0.0

        s.tank.in_flow_lpm = inflow
        s.tank.out_flow_lpm = outflow

        delta = (inflow - outflow) * dt / 60.0
        s.tank.level_liters = clamp(
            s.tank.level_liters + delta,
            0.0,
            s.tank.capacity_liters,
        )

        s.tank.level_pct = (
            100.0 * s.tank.level_liters / s.tank.capacity_liters
            if s.tank.capacity_liters > 0
            else 0.0
        )
        s.tank.level_rate_lps = (inflow - outflow) / 60.0
