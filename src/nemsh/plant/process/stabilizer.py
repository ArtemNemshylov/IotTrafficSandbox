import random
from ..state import PlantState, clamp

class StabilizerProcess:
    # =========================
    # Grid simulation params
    # =========================
    GRID_V_MIN = 0.0
    GRID_V_MAX = 280.0

    GRID_FAULT_LOW = 170.0
    GRID_BYPASS_HIGH = 240.0

    TAU_NORMAL = 6.0 # τ показує, за який час величина “встигає” відреагувати на зміну.
    TAU_DISTURBANCE = 1.5

    NOISE_V = 0.5  # ± volts

    def __init__(self):
        # внутрішній стан "зовнішньої мережі"
        self._grid_regime: str = "NORMAL"
        self._grid_time_left_s: float = 0.0
        self._grid_target_voltage: float | None = None

    # ======================================================
    # MAIN STEP
    # ======================================================
    def step(self, s: PlantState, dt: float) -> None:
        self._update_grid_voltage(s, dt)
        self._update_stabilizer_mode(s)

        # активна потужність
        s.stabilizer.active_power_kw = (
            max(0.0, s.in_pump.power_kw) +
            max(0.0, s.out_pump.power_kw) +
            0.25 if s.filter.mode in ("FILTER", "BACKWASH") else 0 +
            0.08  # 80 Ватт на обробку датчиків, контролерів і всього іншого
        )

    # ======================================================
    # GRID (external network)
    # ======================================================
    def _update_grid_voltage(self, s: PlantState, dt: float) -> None:
        vnom = s.stabilizer.nominal_voltage

        # -------------------------
        # 1. вибір режиму мережі
        # -------------------------
        if self._grid_time_left_s <= 0.0:
            r = random.random()

            if r < 0.90:
                self._grid_regime = "NORMAL"
                self._grid_time_left_s = random.uniform(60.0, 300.0)
                dev_pct = random.uniform(0.02, 0.04)
            elif r < 0.98:
                self._grid_regime = "DEGRADED"
                self._grid_time_left_s = random.uniform(30.0, 120.0)
                dev_pct = random.uniform(0.05, 0.13)
            else:
                self._grid_regime = "DISTURBANCE"
                self._grid_time_left_s = random.uniform(3.0, 12.0)
                dev_pct = random.uniform(0.15, 0.30)

            sign = random.choice([-1.0, 1.0])
            self._grid_target_voltage = vnom * (1.0 + sign * dev_pct)

        self._grid_time_left_s -= dt

        # -------------------------
        # 2. інерція до цілі
        # -------------------------
        tau = (
            self.TAU_DISTURBANCE
            if self._grid_regime == "DISTURBANCE"
            else self.TAU_NORMAL
        )

        vin = s.stabilizer.input_voltage
        vin += (self._grid_target_voltage - vin) * clamp(dt / tau, 0.0, 1.0)

        # -------------------------
        # 3. шум
        # -------------------------
        vin += random.uniform(-self.NOISE_V, self.NOISE_V)

        # -------------------------
        # 4. фізичні межі
        # -------------------------
        s.stabilizer.input_voltage = clamp(
            vin,
            self.GRID_V_MIN,
            self.GRID_V_MAX,
        )

    # ======================================================
    # STABILIZER LOGIC
    # ======================================================
    def _update_stabilizer_mode(self, s: PlantState) -> None:
        vin = s.stabilizer.input_voltage
        vnom = s.stabilizer.nominal_voltage

        if vin < self.GRID_FAULT_LOW:
            s.stabilizer.mode = "FAULT"
            s.stabilizer.output_voltage = 0.0

        elif vin > self.GRID_BYPASS_HIGH:
            s.stabilizer.mode = "BYPASS"
            s.stabilizer.output_voltage = vin

        else:
            s.stabilizer.mode = "NORMAL"
            s.stabilizer.output_voltage = vnom
