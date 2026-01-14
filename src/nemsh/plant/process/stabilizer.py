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
        # =========================
        # БАЗОВА "ЖИВА" МЕРЕЖА
        # =========================

        # 1. якщо нема активної аварії — повільно гуляємо між 210 і 225
        if self._grid_time_left_s <= 0.0:
            # шанс аварії ~1% на крок
            if random.random() < 0.01:
                # аварійна подія
                self._grid_time_left_s = random.uniform(2.0, 8.0)

                if random.random() < 0.5:
                    # просадка
                    self._grid_target_voltage = random.uniform(180.0, 200.0)
                else:
                    # перенапруга
                    self._grid_target_voltage = random.uniform(240.0, 260.0)

                self._grid_regime = "DISTURBANCE"
            else:
                # нормальне постійне коливання
                self._grid_time_left_s = random.uniform(3.0, 8.0)
                self._grid_target_voltage = random.uniform(210.0, 225.0)
                self._grid_regime = "NORMAL"

        self._grid_time_left_s -= dt

        # =========================
        # ІНЕРЦІЯ
        # =========================
        tau = 1.0 if self._grid_regime == "DISTURBANCE" else 5.0

        vin = s.stabilizer.input_voltage
        vin += (self._grid_target_voltage - vin) * clamp(dt / tau, 0.0, 1.0)

        # =========================
        # ПОСТІЙНИЙ ШУМ
        # =========================
        vin += random.uniform(-0.8, 0.8)

        # =========================
        # ФІЗИЧНІ МЕЖІ
        # =========================
        s.stabilizer.input_voltage = clamp(vin, 0.0, 280.0)

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
