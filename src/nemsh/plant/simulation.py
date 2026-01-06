# plant/simulator.py
from __future__ import annotations

from dataclasses import dataclass

from .state import PlantState
from .controller import PlantController
from .process import PlantProcess


@dataclass
class SimulatorConfig:
    pass


class PlantSimulator:

    def __init__(
        self,
        state: PlantState,
        controller: PlantController | None = None,
        process: PlantProcess | None = None,
        cfg: SimulatorConfig | None = None,
    ):
        self.state = state
        self.controller = controller or PlantController()
        self.process = process or PlantProcess()
        self.cfg = cfg or SimulatorConfig()

        self._time_accum_s: float = 0.0  # для int time_s

    def step(self, dt: float) -> None:
        if dt <= 0:
            return

        # 1) Controller decides desired setpoints/modes (AUTO only)
        self.controller.compute(self.state, dt)

        # 2) Physics applies those setpoints and updates real values
        self.process.step(self.state, dt)

        # 3) Maintain int time_s in state (dt-aware)
        self._time_accum_s += float(dt)
        if self._time_accum_s >= 1.0:
            inc = int(self._time_accum_s)
            self.state.time_s += inc
            self._time_accum_s -= inc
