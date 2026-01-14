from ..state import PlantState
from .stabilizer import StabilizerProcess
from .pump import PumpProcess
from .filter import FilterProcess
from .tank import TankProcess

class PlantProcess:
    def __init__(self):
        self.stabilizer = StabilizerProcess()
        self.pumps = PumpProcess()
        self.filter = FilterProcess()
        self.tank = TankProcess()

    def step(self, s: PlantState, dt: float) -> None:
        if dt <= 0:
            return

        self.stabilizer.step(s, dt)
        self.pumps.step_in_pump(s, dt)
        self.pumps.step_out_pump(s, dt)
        self.filter.step(s, dt)
        self.tank.step(s, dt)
