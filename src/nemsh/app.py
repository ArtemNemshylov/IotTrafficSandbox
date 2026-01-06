# app.py (Streamlit) — full control panel for the current PlantState + Controller + Process
from __future__ import annotations

import time
import pandas as pd
import streamlit as st

from plant.state import PlantState, clamp
from plant.controller import PlantController, ControllerConfig
from plant.process import PlantProcess, ProcessConfig


# ======================================================
# INIT
# ======================================================
st.set_page_config(page_title="Water Plant Simulator", layout="wide")

if "sim_state" not in st.session_state:
    st.session_state.sim_state = PlantState()
    st.session_state.controller = PlantController(ControllerConfig())
    st.session_state.process = PlantProcess(ProcessConfig())
    st.session_state.running = False
    st.session_state.dt = 1.0
    st.session_state.tick_s = 0.25
    st.session_state.history = []
    st.session_state.max_history = 2000

state: PlantState = st.session_state.sim_state
controller: PlantController = st.session_state.controller
process: PlantProcess = st.session_state.process


# ======================================================
# STEP FUNCTION (manual or auto)
# ======================================================
def sim_step(dt: float):
    # controller may be disabled
    if st.session_state.controller_enabled:
        controller.compute(state, dt)
    process.step(state, dt)

    # keep a consistent int time
    # (process uses dt, controller uses dt; time_s is for display/logging only)
    state.time_s += int(dt)

    # history
    st.session_state.history.append(
        {
            "t": state.time_s,
            "vin": state.stabilizer.vin_v,
            "vout": state.stabilizer.vout_v,
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
            "in_temp": state.in_pump.motor_temp_c,
            "out_mode": state.out_pump.mode,
            "out_state": state.out_pump.state,
            "out_rpm_d": state.out_pump.rpm_desired,
            "out_rpm": state.out_pump.rpm_actual,
            "out_flow": state.out_pump.flow_lpm,
            "out_p": state.out_pump.pressure_bar,
            "out_kw": state.out_pump.power_kw,
            "out_temp": state.out_pump.motor_temp_c,
        }
    )
    if len(st.session_state.history) > st.session_state.max_history:
        st.session_state.history = st.session_state.history[-st.session_state.max_history :]


# ======================================================
# SIDEBAR CONTROLS
# ======================================================
st.sidebar.title("Controls")

# runtime
st.session_state.controller_enabled = st.sidebar.checkbox("Controller enabled (AUTO rules)", value=True)
st.session_state.dt = st.sidebar.slider("dt (simulation step, seconds)", 0.1, 5.0, float(st.session_state.dt), 0.1)
st.session_state.tick_s = st.sidebar.slider("UI refresh (seconds)", 0.05, 2.0, float(st.session_state.tick_s), 0.05)

c1, c2 = st.sidebar.columns(2)
if c1.button("Step once"):
    sim_step(st.session_state.dt)

if c2.button("Reset"):
    st.session_state.sim_state = PlantState()
    st.session_state.controller = PlantController(ControllerConfig())
    st.session_state.process = PlantProcess(ProcessConfig())
    st.session_state.history = []
    st.rerun()

st.session_state.running = st.sidebar.toggle("Running", value=st.session_state.running)

st.sidebar.divider()

# global inputs
st.sidebar.subheader("Electrical / Stabilizer")
state.stabilizer.vin_v = float(st.sidebar.slider("vin_v (grid voltage)", 150, 270, int(state.stabilizer.vin_v)))

st.sidebar.divider()

# overrides
st.sidebar.subheader("Overrides")

override_wear = st.sidebar.checkbox("Override filter wear_pct", value=False)
wear_val = st.sidebar.slider("wear_pct", 0.0, 100.0, float(state.filter.wear_pct), 0.1)

override_tank = st.sidebar.checkbox("Override tank level_pct", value=False)
tank_val = st.sidebar.slider("tank level_pct", 0.0, 100.0, float(state.tank.level_pct), 0.1)

override_filter_mode = st.sidebar.checkbox("Override filter.mode", value=False)
filter_mode_val = st.sidebar.selectbox("filter.mode", ["FILTER", "BACKWASH", "IDLE"], index=["FILTER", "BACKWASH", "IDLE"].index(state.filter.mode))

st.sidebar.divider()

# Pump controls
st.sidebar.subheader("IN Pump")
state.in_pump.mode = st.sidebar.selectbox("IN mode", ["AUTO", "MANUAL"], index=["AUTO", "MANUAL"].index(state.in_pump.mode))
in_state_val = st.sidebar.selectbox("IN state", ["OFF", "ON", "FAULT"], index=["OFF", "ON", "FAULT"].index(state.in_pump.state))
in_rpm_val = st.sidebar.slider("IN rpm_desired", 0.0, float(state.in_pump.rpm_max), float(state.in_pump.rpm_desired), 10.0)

st.sidebar.subheader("OUT Pump")
state.out_pump.mode = st.sidebar.selectbox("OUT mode", ["AUTO", "MANUAL"], index=["AUTO", "MANUAL"].index(state.out_pump.mode))
out_state_val = st.sidebar.selectbox("OUT state", ["OFF", "ON", "FAULT"], index=["OFF", "ON", "FAULT"].index(state.out_pump.state))
out_rpm_val = st.sidebar.slider("OUT rpm_desired", 0.0, float(state.out_pump.rpm_max), float(state.out_pump.rpm_desired), 10.0)


# apply manual changes safely:
def apply_manual_pump(pump, desired_rpm: float, desired_state: str):
    # if MANUAL: user can set state and rpm
    if pump.mode == "MANUAL" or (not st.session_state.controller_enabled):
        pump.state = desired_state  # OFF/ON/FAULT
        pump.rpm_desired = float(desired_rpm)

        # normalize consistency
        if pump.state == "OFF":
            pump.rpm_desired = 0.0
        elif pump.state == "ON":
            if pump.rpm_desired < pump.rpm_min:
                pump.rpm_desired = pump.rpm_min
        # FAULT -> keep rpm_desired but process will enforce OFF-like behavior
    else:
        # AUTO: ignore user state/rpm inputs
        pass


apply_manual_pump(state.in_pump, in_rpm_val, in_state_val)
apply_manual_pump(state.out_pump, out_rpm_val, out_state_val)


# apply overrides
if override_wear:
    state.filter.wear_pct = clamp(float(wear_val), 0.0, 100.0)

if override_tank:
    pct = clamp(float(tank_val), 0.0, 100.0)
    state.tank.level_liters = state.tank.capacity_liters * (pct / 100.0)
    state.tank.level_pct = pct

if override_filter_mode:
    state.filter.mode = filter_mode_val


# ======================================================
# MAIN UI
# ======================================================
st.title("Water Treatment Plant — Simulation (Streamlit)")

# Status row
a, b, c, d, e = st.columns(5)
a.metric("time_s", f"{state.time_s}")
b.metric("vin_v", f"{state.stabilizer.vin_v:.0f}")
c.metric("vout_v", f"{state.stabilizer.vout_v:.0f}")
d.metric("stabilizer.mode", f"{state.stabilizer.mode}")
e.metric("active_power_kw", f"{state.stabilizer.active_power_kw:.2f}")

st.divider()

# Tank + Filter
col1, col2 = st.columns(2)

with col1:
    st.subheader("Tank")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("level_pct", f"{state.tank.level_pct:.1f}%")
    c2.metric("level_liters", f"{state.tank.level_liters:.0f} L")
    c3.metric("in_flow_lpm", f"{state.tank.in_flow_lpm:.1f}")
    c4.metric("out_flow_lpm", f"{state.tank.out_flow_lpm:.1f}")
    st.write(f"level_rate_lps: **{state.tank.level_rate_lps:.3f}**")

with col2:
    st.subheader("Filter")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("mode", state.filter.mode)
    c2.metric("wear_pct", f"{state.filter.wear_pct:.2f}%")
    c3.metric("ΔP (bar)", f"{state.filter.delta_pressure_bar:.2f}")
    c4.metric("NTU", f"{state.filter.ntu:.2f}")
    st.write(f"pH: **{state.filter.ph:.2f}**")

st.divider()

# Pumps
p1, p2 = st.columns(2)

with p1:
    st.subheader("IN Pump")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("mode", state.in_pump.mode)
    c2.metric("state", state.in_pump.state)
    c3.metric("rpm_desired", f"{state.in_pump.rpm_desired:.0f}")
    c4.metric("rpm_actual", f"{state.in_pump.rpm_actual:.0f}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("flow_lpm", f"{state.in_pump.flow_lpm:.1f}")
    c2.metric("pressure_bar", f"{state.in_pump.pressure_bar:.2f}")
    c3.metric("power_kw", f"{state.in_pump.power_kw:.2f}")
    c4.metric("motor_temp_c", f"{state.in_pump.motor_temp_c:.1f}")

with p2:
    st.subheader("OUT Pump")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("mode", state.out_pump.mode)
    c2.metric("state", state.out_pump.state)
    c3.metric("rpm_desired", f"{state.out_pump.rpm_desired:.0f}")
    c4.metric("rpm_actual", f"{state.out_pump.rpm_actual:.0f}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("flow_lpm", f"{state.out_pump.flow_lpm:.1f}")
    c2.metric("pressure_bar", f"{state.out_pump.pressure_bar:.2f}")
    c3.metric("power_kw", f"{state.out_pump.power_kw:.2f}")
    c4.metric("motor_temp_c", f"{state.out_pump.motor_temp_c:.1f}")

st.divider()

# History (optional charts)
if len(st.session_state.history) > 5:
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
