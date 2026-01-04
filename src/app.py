import streamlit as st
import pandas as pd
import time

from plant.state import PlantState
from plant.process import PlantProcess


# ======================================================
# INIT
# ======================================================

if "state" not in st.session_state:
    st.session_state.state = PlantState()
    st.session_state.process = PlantProcess(st.session_state.state)
    st.session_state.history = []

state = st.session_state.state
process = st.session_state.process


# ======================================================
# SIMULATION STEP (FASTER TIME)
# ======================================================

process.step(dt=2.0)  # ⬅️ прискорення процесу


# ======================================================
# SAVE HISTORY
# ======================================================

st.session_state.history.append({
    "time": state.time_seconds,

    "grid_voltage": state.grid_voltage,

    "in_power": state.in_pump_power_actual,
    "in_rpm": state.in_pump_rpm,
    "in_flow": state.in_pump_flow_lpm,
    "in_temp": state.in_pump_motor_temp,

    "out_power": state.out_pump_power_actual,
    "out_rpm": state.out_pump_rpm,
    "out_flow": state.out_pump_flow_lpm,
    "out_temp": state.out_pump_motor_temp,

    "tank_level": state.tank_level_percent,
})

df = pd.DataFrame(st.session_state.history)


# ======================================================
# UI
# ======================================================

st.title("Water Treatment Plant — Process & Network Sandbox")

# ------------------------------------------------------
# GLOBAL STATUS
# ------------------------------------------------------

st.subheader("Global status")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Grid Voltage (V)", f"{state.grid_voltage:.0f}")
c2.metric("Tank Level (%)", f"{state.tank_level_percent:.1f}")
c3.metric("Stabilizer", state.stabilizer_state)
c4.metric("Filter", state.filter_state)

st.line_chart(df.set_index("time")[["grid_voltage", "tank_level"]])

st.divider()

# ======================================================
# IN PUMP
# ======================================================

st.subheader("IN Pump (Well)")

# ---- Controls under pump ----
c1, c2 = st.columns(2)

with c1:
    state.in_pump_power_setpoint = st.slider(
        "IN Pump Power (%)", 0, 100, int(state.in_pump_power_setpoint)
    )

with c2:
    state.grid_voltage = st.slider(
        "Grid Voltage (V)", 180, 250, int(state.grid_voltage)
    )

# ---- Metrics ----
c1, c2, c3 = st.columns(3)
c1.metric("RPM", f"{state.in_pump_rpm:.0f}")
c2.metric("Flow (LPM)", f"{state.in_pump_flow_lpm:.1f}")
c3.metric("Motor Temp (°C)", f"{state.in_pump_motor_temp:.1f}")

# ---- Individual charts ----
st.line_chart(df.set_index("time")[["in_rpm"]])
st.line_chart(df.set_index("time")[["in_flow"]])
st.line_chart(df.set_index("time")[["in_temp"]])

st.divider()

# ======================================================
# OUT PUMP
# ======================================================

st.subheader("OUT Pump (Distribution)")

# ---- Controls under pump ----
c1, c2 = st.columns(2)

with c1:
    state.out_pump_power_setpoint = st.slider(
        "OUT Pump Power (%)", 0, 100, int(state.out_pump_power_setpoint)
    )

with c2:
    state.ambient_temperature = st.slider(
        "Ambient Temp (°C)", 0, 40, int(state.ambient_temperature)
    )

# ---- Metrics ----
c1, c2, c3 = st.columns(3)
c1.metric("RPM", f"{state.out_pump_rpm:.0f}")
c2.metric("Flow (LPM)", f"{state.out_pump_flow_lpm:.1f}")
c3.metric("Motor Temp (°C)", f"{state.out_pump_motor_temp:.1f}")

# ---- Individual charts ----
st.line_chart(df.set_index("time")[["out_rpm"]])
st.line_chart(df.set_index("time")[["out_flow"]])
st.line_chart(df.set_index("time")[["out_temp"]])

st.divider()

# ======================================================
# WATER STORAGE
# ======================================================

st.subheader("Water Storage")

c1, c2 = st.columns(2)
c1.metric("Tank Level (%)", f"{state.tank_level_percent:.1f}")
c2.metric("Capacity (L)", f"{state.tank_capacity_liters:.0f}")

st.line_chart(df.set_index("time")[["tank_level"]])

st.divider()

# ======================================================
# EXTENDED SYSTEM VIEW
# ======================================================

st.subheader("Extended system overview")

st.line_chart(
    df.set_index("time")[[
        "in_power",
        "out_power",
        "in_flow",
        "out_flow",
    ]]
)

# ======================================================
# LOOP
# ======================================================

time.sleep(1)
st.rerun()
