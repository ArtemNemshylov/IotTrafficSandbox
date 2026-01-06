from src.nemsh.plant.state import PlantState

class StabilizerProcess:
    def step(self, s: PlantState, dt: float) -> None:
        vin = s.stabilizer.input_voltage
        vnom = s.stabilizer.nominal_voltage

        if 190.0 <= vin <= 240.0:
            s.stabilizer.mode = "NORMAL"
            s.stabilizer.output_voltage = vnom
        elif 180.0 <= vin <= 260.0:
            s.stabilizer.mode = "BYPASS"
            s.stabilizer.output_voltage = vin
        else:
            s.stabilizer.mode = "FAULT"
            s.stabilizer.output_voltage = 0.0

        s.stabilizer.active_power_kw = (
            max(0.0, s.in_pump.power_kw) +
            max(0.0, s.out_pump.power_kw)
        )
