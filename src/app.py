# app.py
import time
import streamlit as st
import pandas as pd

from plant.state import PlantState
from plant.process import PlantProcess


# ======================================================
# INIT
# ======================================================
st.set_page_config(page_title="Water Treatment Plant — Sandbox", layout="wide")

if "state" not in st.session_state:
    st.session_state.state = PlantState()
    st.session_state.process = PlantProcess(st.session_state.state)
    st.session_state.history = []
    st.session_state.run = True
    st.session_state.dt = 2.0

state: PlantState = st.session_state.state
process: PlantProcess = st.session_state.process


# ======================================================
# SIDEBAR (SIM CONTROLS)
# ======================================================
with st.sidebar:
    st.header("Simulation")
    st.session_state.run = st.toggle("Run", value=st.session_state.run)
    st.session_state.dt = st.slider("dt (seconds per tick)", 1.0, 10.0, float(st.session_state.dt), 1.0)

    st.divider()
    st.header("Environment")
    state.ambient_temperature_c = st.slider(
        "Ambient Temperature (°C)", 0, 40, int(state.ambient_temperature_c)
    )
    state.grid_voltage_v = st.slider(
        "Grid Voltage (V)", 170, 260, int(state.grid_voltage_v)
    )

    st.divider()
    st.header("Water quality (raw)")
    state.ntu_in = st.slider("NTU in", 0.2, 10.0, float(state.ntu_in), 0.1)
    state.ph_in = st.slider("pH in", 6.0, 8.5, float(state.ph_in), 0.05)

    st.divider()
    st.header("Tank config")
    state.tank_capacity_liters = st.number_input(
        "Tank capacity (L)", min_value=100.0, value=float(state.tank_capacity_liters), step=500.0
    )
    state.tank_level_liters = st.number_input(
        "Tank level (L)",
        min_value=0.0,
        max_value=float(state.tank_capacity_liters),
        value=float(min(state.tank_level_liters, state.tank_capacity_liters)),
        step=100.0,
    )

    st.divider()
    st.header("Filter (manual override)")
    # Дозволяє вручну задати зношеність/забруднення фільтра
    state.filter_wear_pct = st.slider(
        "Filter wear / clogging (%)", 0.0, 100.0, float(state.filter_wear_pct), 1.0
    )


# ======================================================
# PUMP CONTROLS (TOP)
# ======================================================
st.title("Water Treatment Plant — Process Sandbox")

cA, cB = st.columns(2)

with cA:
    st.subheader("IN Pump controls")
    state.in_pump_cmd_state = st.radio(
        "IN Pump state", ["ON", "OFF"], horizontal=True,
        index=0 if state.in_pump_cmd_state == "ON" else 1
    )
    state.in_pump_cmd_mode = st.radio(
        "IN Pump mode", ["AUTO", "MANUAL"], horizontal=True,
        index=0 if state.in_pump_cmd_mode == "AUTO" else 1
    )
    if state.in_pump_cmd_mode == "MANUAL":
        state.in_pump_cmd_rpm_pct = st.slider(
            "IN Pump RPM (%)", 0, 100, int(state.in_pump_cmd_rpm_pct)
        )

with cB:
    st.subheader("OUT Pump controls")
    state.out_pump_cmd_state = st.radio(
        "OUT Pump state", ["ON", "OFF"], horizontal=True,
        index=0 if state.out_pump_cmd_state == "ON" else 1
    )
    state.out_pump_cmd_mode = st.radio(
        "OUT Pump mode", ["AUTO", "MANUAL"], horizontal=True,
        index=0 if state.out_pump_cmd_mode == "AUTO" else 1
    )
    if state.out_pump_cmd_mode == "MANUAL":
        state.out_pump_cmd_rpm_pct = st.slider(
            "OUT Pump RPM (%)", 0, 100, int(state.out_pump_cmd_rpm_pct)
        )


# ======================================================
# SIMULATION STEP
# ======================================================
if st.session_state.run:
    process.step(dt=float(st.session_state.dt))


# ======================================================
# SAVE HISTORY (optional; keep short)
# ======================================================
st.session_state.history.append({
    "time": state.time_seconds,
    "grid_voltage_v": state.grid_voltage_v,
    "vout_v": state.stabilizer_vout_v,
    "stab_mode": state.stabilizer_mode,
    "stab_temp": state.stabilizer_transformer_temp_c,
    "tank_pct": state.tank_level_pct,
    "tank_in_lpm": state.tank_in_flow_lpm,
    "tank_out_lpm": state.tank_out_flow_lpm,
    "filter_mode": state.filter_mode,
    "filter_wear": state.filter_wear_pct,
    "filter_dp": state.filter_delta_p_bar,
    "ntu_in": state.ntu_in,
    "ntu_out": state.ntu_out,
    "quality_alarm": int(state.filter_quality_alarm),
    "out_block_latch": int(state.out_blocked_low_level_filter),
    "in_state": state.in_pump_state,
    "in_rpm": state.in_pump_rpm,
    "in_flow": state.in_pump_flow_lpm,
    "in_temp": state.in_pump_motor_temp_c,
    "in_kw": state.in_pump_power_kw,
    "out_state": state.out_pump_state,
    "out_rpm": state.out_pump_rpm,
    "out_flow": state.out_pump_flow_lpm,
    "out_temp": state.out_pump_motor_temp_c,
    "out_kw": state.out_pump_power_kw,
})

if len(st.session_state.history) > 800:
    st.session_state.history = st.session_state.history[-800:]

df = pd.DataFrame(st.session_state.history)


# ======================================================
# UI METRICS
# ======================================================
st.divider()
st.subheader("Global status")

g1, g2, g3, g4, g5, g6 = st.columns(6)
g1.metric("Time (s)", f"{state.time_seconds}")
g2.metric("Grid (V)", f"{state.grid_voltage_v:.0f}")
g3.metric("Vout (V)", f"{state.stabilizer_vout_v:.0f}")
g4.metric("Stabilizer", f"{state.stabilizer_mode}")
g5.metric("Stab Temp (°C)", f"{state.stabilizer_transformer_temp_c:.1f}")
g6.metric("Tank Level (%)", f"{state.tank_level_pct:.1f}")

st.subheader("Tank flows")
t1, t2, t3, t4 = st.columns(4)
t1.metric("Inflow (LPM)", f"{state.tank_in_flow_lpm:.1f}")
t2.metric("Outflow (LPM)", f"{state.tank_out_flow_lpm:.1f}")
t3.metric("Overflow", f"{state.tank_overflow}")
t4.metric("Sensors", f"{state.tank_level_sensors_state}")

st.divider()
st.subheader("Filter")

f1, f2, f3, f4, f5, f6 = st.columns(6)
f1.metric("Mode", state.filter_mode)
f2.metric("Wear (%)", f"{state.filter_wear_pct:.1f}")
f3.metric("ΔP (bar)", f"{state.filter_delta_p_bar:.3f}")
f4.metric("NTU in", f"{state.ntu_in:.2f}")
f5.metric("NTU out", f"{state.ntu_out:.2f}")
f6.metric("Quality alarm", str(state.filter_quality_alarm))

i1, i2 = st.columns(2)
i1.metric("OUT blocked latch", str(state.out_blocked_low_level_filter))
i2.metric("Backwash elapsed (s)", f"{state.filter_backwash_elapsed_s:.0f}")

st.divider()
st.subheader("IN Pump")

p1, p2, p3, p4, p5, p6 = st.columns(6)
p1.metric("Cmd", f"{state.in_pump_cmd_state} / {state.in_pump_cmd_mode}")
p2.metric("State", state.in_pump_state)
p3.metric("RPM", f"{state.in_pump_rpm:.0f}")
p4.metric("Flow (LPM)", f"{state.in_pump_flow_lpm:.1f}")
p5.metric("Temp (°C)", f"{state.in_pump_motor_temp_c:.1f}")
p6.metric("Power (kW)", f"{state.in_pump_power_kw:.2f}")

p7, p8, p9 = st.columns(3)
p7.metric("Cooldown (s)", f"{state.in_pump_cooldown_remaining_s:.0f}")
p8.metric("High RPM time (s)", f"{state.in_pump_high_rpm_time_s:.0f}")
p9.metric("Fault", state.in_pump_fault_code or "-")

st.divider()
st.subheader("OUT Pump")

o1, o2, o3, o4, o5, o6 = st.columns(6)
o1.metric("Cmd", f"{state.out_pump_cmd_state} / {state.out_pump_cmd_mode}")
o2.metric("State", state.out_pump_state)
o3.metric("RPM", f"{state.out_pump_rpm:.0f}")
o4.metric("Flow (LPM)", f"{state.out_pump_flow_lpm:.1f}")
o5.metric("Temp (°C)", f"{state.out_pump_motor_temp_c:.1f}")
o6.metric("Power (kW)", f"{state.out_pump_power_kw:.2f}")

o7, o8, o9 = st.columns(3)
o7.metric("Cooldown (s)", f"{state.out_pump_cooldown_remaining_s:.0f}")
o8.metric("High RPM time (s)", f"{state.out_pump_high_rpm_time_s:.0f}")
o9.metric("Fault", state.out_pump_fault_code or "-")

st.divider()

with st.expander("Recent telemetry (last 20 ticks)", expanded=False):
    st.dataframe(df.tail(20), use_container_width=True)


# ======================================================
# LOOP
# ======================================================
time.sleep(0.5)
st.rerun()
