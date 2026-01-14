from ..state import PlantState, clamp

class FilterProcess:
    def step(self, s: PlantState, dt: float) -> None:
        Q = s.in_pump.flow_lpm if s.in_pump.state == "ON" else 0.0

        s.filter.in_pressure_bar = s.in_pump.pressure_bar if Q > 0 else 0.0
        s.filter.out_pressure_bar = 0.2 if Q > 0 else 0.0
        s.filter.delta_pressure_bar = max(
            0.0,
            s.filter.in_pressure_bar - s.filter.out_pressure_bar,
        )

        if s.filter.mode == "FILTER" and Q > 0:
            dw = (Q / 60.0) * 2.0 * 0.00278 * dt
            s.filter.wear_pct = clamp(s.filter.wear_pct + dw, 0.0, 100.0)

            if s.filter.wear_pct <= 50.0:
                s.filter.ntu = 1.0
            else:
                x = (s.filter.wear_pct - 50.0) / 50.0
                s.filter.ntu = 1.0 + 2.0 * clamp(x, 0.0, 1.0)

            s.filter.ph = 7.0

        elif s.filter.mode == "BACKWASH":
            s.filter.wear_pct = max(
                s.filter.min_wear_after_backwash_pct,
                s.filter.wear_pct - 1.0 * dt,
            )
            s.filter.ntu = 3.0
            s.filter.ph = 7.0
