# app.py — Streamlit control panel (NEW ARCHITECTURE)
from __future__ import annotations

import time
import pandas as pd
import streamlit as st

from plant.state import PlantState, clamp
from plant.controller import PlantController, ControllerConfig
from plant.process.plant_process import PlantProcess
from plant.simulation import PlantSimulator


# ======================================================
# INIT
# ======================================================
st.set_page_config(page_title="Water Plant Simulator", layout="wide")

if "sim" not in st.session_state:
    state = PlantState()
    controller = PlantController(ControllerConfig())
    process = PlantProcess()
    simulator = PlantSimulator(state, controller, process)

    st.session_state.sim = simulator
    st.session_state.running = False
    st.session_state.dt = 1.0
    st.session_state.tick_s = 0.25
    st.session_state.history = []
    st.session_state.max_history = 2000
    st.session_state.controller_enabled = True

sim: PlantSimulator = st.session_state.sim
state: PlantState = sim.state


# ======================================================
# STEP
# ======================================================
def sim_step(dt: float):
    if st.session_state.controller_enabled:
        sim.controller.compute(state, dt)

    sim.process.step(state, dt)

    # time already handled by Simulator
    st.session_state.history.append(
        {
            "t": state.time_s,
            "vin": state.stabilizer.input_voltage,
            "vout": state.stabilizer.output_voltage,
            "stab_mode": state.stabilizer.mode,
            "P_kw": state.stabilizer.active_power_kw,
            "tank_pct": state.tank.level_pct,
            "tank_in": state.tank.in_flow_lpm,
            "tank_out": state.tank.out_flow_lpm,
            "filter_mode": state.filter.mode,
            "wear": state.filter.wear_pct,
            "dp": state.filter.delta_pressure_bar,
            "ntu": state.filter.ntu,
            "in_mode": state.in_pump.mode,
            "in_state": state.in_pump.state,
            "in_rpm_d": state.in_pump.rpm_desired,
            "in_rpm": state.in_pump.rpm_actual,
            "in_flow": state.in_pump.flow_lpm,
            "in_p": state.in_pump.pressure_bar,
            "in_kw": state.in_pump.power_kw,
            "in_temp": state.in_pump.motor_temp,
            "out_mode": state.out_pump.mode,
            "out_state": state.out_pump.state,
            "out_rpm_d": state.out_pump.rpm_desired,
            "out_rpm": state.out_pump.rpm_actual,
            "out_flow": state.out_pump.flow_lpm,
            "out_p": state.out_pump.pressure_bar,
            "out_kw": state.out_pump.power_kw,
            "out_temp": state.out_pump.motor_temp,
        }
    )

    if len(st.session_state.history) > st.session_state.max_history:
        st.session_state.history = st.session_state.history[-st.session_state.max_history :]


# ======================================================
# SIDEBAR
# ======================================================
st.sidebar.title("Controls")

st.session_state.controller_enabled = st.sidebar.checkbox(
    "Controller enabled (AUTO)",
    value=st.session_state.controller_enabled,
)

st.session_state.dt = st.sidebar.slider(
    "dt (seconds)", 0.1, 5.0, float(st.session_state.dt), 0.1
)

st.session_state.tick_s = st.sidebar.slider(
    "UI refresh (seconds)", 0.05, 2.0, float(st.session_state.tick_s), 0.05
)

c1, c2 = st.sidebar.columns(2)
if c1.button("Step once"):
    sim_step(st.session_state.dt)

if c2.button("Reset"):
    st.session_state.clear()
    st.rerun()

st.session_state.running = st.sidebar.toggle(
    "Running", value=st.session_state.running
)

st.sidebar.divider()

# ======================================================
# ELECTRICAL
# ======================================================
st.sidebar.subheader("Stabilizer / Grid")
state.stabilizer.input_voltage = float(
    st.sidebar.slider(
        "input_voltage (V)",
        150,
        270,
        int(state.stabilizer.input_voltage),
    )
)

# ======================================================
# OVERRIDES
# ======================================================
st.sidebar.subheader("Overrides")

if st.sidebar.checkbox("Override filter wear"):
    state.filter.wear_pct = st.sidebar.slider(
        "wear_pct", 0.0, 100.0, float(state.filter.wear_pct), 0.1
    )

if st.sidebar.checkbox("Override tank level"):
    pct = st.sidebar.slider(
        "tank level_pct", 0.0, 100.0, float(state.tank.level_pct), 0.1
    )
    state.tank.level_liters = state.tank.capacity_liters * pct / 100.0
    state.tank.level_pct = pct

if st.sidebar.checkbox("Override filter mode"):
    state.filter.mode = st.sidebar.selectbox(
        "filter.mode", ["FILTER", "BACKWASH", "IDLE"]
    )

# ======================================================
# PUMP MANUAL CONTROL
# ======================================================
def manual_pump_control(pump, label: str):
    st.sidebar.subheader(label)

    pump.mode = st.sidebar.selectbox(
        f"{label} mode", ["AUTO", "MANUAL"], index=["AUTO", "MANUAL"].index(pump.mode)
    )

    state_val = st.sidebar.selectbox(
        f"{label} state", ["OFF", "ON", "FAULT"], index=["OFF", "ON", "FAULT"].index(pump.state)
    )

    rpm_val = st.sidebar.slider(
        f"{label} rpm_desired",
        0.0,
        float(pump.rpm_max),
        float(pump.rpm_desired),
        10.0,
    )

    if pump.mode == "MANUAL" or not st.session_state.controller_enabled:
        pump.state = state_val
        pump.rpm_desired = rpm_val if state_val == "ON" else 0.0


manual_pump_control(state.in_pump, "IN Pump")
manual_pump_control(state.out_pump, "OUT Pump")

# ======================================================
# MAIN UI
# ======================================================
st.title("Water Treatment Plant — Simulation")

a, b, c, d, e = st.columns(5)
a.metric("time_s", state.time_s)
b.metric("vin", f"{state.stabilizer.input_voltage:.0f}")
c.metric("vout", f"{state.stabilizer.output_voltage:.0f}")
d.metric("mode", state.stabilizer.mode)
e.metric("P_kw", f"{state.stabilizer.active_power_kw:.2f}")

st.divider()

col1, col2 = st.columns(2)

with col1:
    st.subheader("Tank")
    st.metric("level_pct", f"{state.tank.level_pct:.1f}%")
    st.metric("level_liters", f"{state.tank.level_liters:.0f}")
    st.metric("in_flow_lpm", f"{state.tank.in_flow_lpm:.1f}")
    st.metric("out_flow_lpm", f"{state.tank.out_flow_lpm:.1f}")
    st.write(f"rate_lps: **{state.tank.level_rate_lps:.3f}**")

with col2:
    st.subheader("Filter")
    st.metric("mode", state.filter.mode)
    st.metric("wear_pct", f"{state.filter.wear_pct:.2f}%")
    st.metric("ΔP", f"{state.filter.delta_pressure_bar:.2f}")
    st.metric("NTU", f"{state.filter.ntu:.2f}")
    st.write(f"pH: **{state.filter.ph:.2f}**")

st.divider()

p1, p2 = st.columns(2)

def pump_panel(pump, title: str):
    st.subheader(title)
    st.metric("mode", pump.mode)
    st.metric("state", pump.state)
    st.metric("rpm_desired", f"{pump.rpm_desired:.0f}")
    st.metric("rpm_actual", f"{pump.rpm_actual:.0f}")
    st.metric("flow_lpm", f"{pump.flow_lpm:.1f}")
    st.metric("pressure_bar", f"{pump.pressure_bar:.2f}")
    st.metric("power_kw", f"{pump.power_kw:.2f}")
    st.metric("motor_temp", f"{pump.motor_temp:.1f}")

with p1:
    pump_panel(state.in_pump, "IN Pump")

with p2:
    pump_panel(state.out_pump, "OUT Pump")

# ======================================================
# HISTORY
# ======================================================
if len(st.session_state.history) > 10:
    st.subheader("History")
    df = pd.DataFrame(st.session_state.history).set_index("t")
    st.line_chart(df[["tank_pct", "wear", "in_rpm", "out_rpm"]])
    st.line_chart(df[["vin", "vout", "P_kw"]])
    st.dataframe(df.tail(30), use_container_width=True)

# ======================================================
# LOOP
# ======================================================
if st.session_state.running:
    sim_step(st.session_state.dt)
    time.sleep(st.session_state.tick_s)
    st.rerun()
