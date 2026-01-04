# dashboard.py
#!/usr/bin/env python3
"""
Streamlit Control Panel (scaled MVP):
- Stabilizer: set Vout +/- , show mode, fault
- Pumps: ON/OFF + RPM control
- Only chart: storage level (realtime)

Requires:
  pip install streamlit paho-mqtt pandas

Optional (recommended):
  pip install streamlit-autorefresh

Run:
  streamlit run dashboard.py
"""

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st
from paho.mqtt import client as mqtt


# ----------------------------
# Optional autorefresh
# ----------------------------
def try_autorefresh(interval_ms: int) -> bool:
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore
        st_autorefresh(interval=interval_ms, key="__auto_refresh__")
        return True
    except Exception:
        return False


# ----------------------------
# Utils
# ----------------------------
def parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def tail_lines(path: str, max_lines: int = 4000) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return lines[-max_lines:]


def load_storage_series(jsonl_path: str, max_lines: int = 4000) -> pd.DataFrame:
    rows = []
    for line in tail_lines(jsonl_path, max_lines=max_lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("device_id") != "water_storage":
            continue

        ts = payload.get("ts")
        dt = parse_ts(ts) if isinstance(ts, str) else None
        if dt is None:
            continue

        stg = payload.get("storage", {})
        if not isinstance(stg, dict):
            continue

        rows.append({
            "ts": dt,
            "level_pct": stg.get("level_pct"),
            "in_flow_lpm": stg.get("in_flow_lpm"),
            "out_flow_lpm": stg.get("out_flow_lpm"),
            "level_rate": stg.get("level_rate"),
        })

    if not rows:
        return pd.DataFrame(columns=["ts", "level_pct", "in_flow_lpm", "out_flow_lpm", "level_rate"])

    df = pd.DataFrame(rows).sort_values("ts")
    return df


def load_latest_status(jsonl_path: str, max_lines: int = 3000) -> Dict[str, Any]:
    latest: Dict[str, Any] = {}
    for line in reversed(tail_lines(jsonl_path, max_lines=max_lines)):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        dev = payload.get("device_id")
        if not isinstance(dev, str):
            continue
        if dev not in latest:
            latest[dev] = payload
        if len(latest) >= 5:
            break
    return latest


def mqtt_publish(host: str, port: int, topic: str, payload: Dict[str, Any]) -> None:
    c = mqtt.Client()
    c.connect(host, port, keepalive=30)
    c.loop_start()
    c.publish(topic, json.dumps(payload).encode("utf-8"), qos=0)
    c.loop_stop()
    c.disconnect()


def send_cmd(host: str, port: int, base_topic: str, command: str, target: str, value: Any = None) -> None:
    topic = f"{base_topic}/control/commands"
    mqtt_publish(
        host=host,
        port=port,
        topic=topic,
        payload={"command": command, "target": target, "value": value, "auth_ok": True, "source": "HMI"},
    )


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="WaterPlant Panel", layout="wide")
st.title("WaterPlant — Панель керування (scaled MVP)")

top1, top2, top3 = st.columns(3)

with top1:
    st.subheader("MQTT")
    mqtt_host = st.text_input("Host", value="127.0.0.1")
    mqtt_port = st.number_input("Port", value=1883, step=1)
    base_topic = st.text_input("Base topic", value="waterplant")

with top2:
    st.subheader("Data")
    jsonl_path = st.text_input("Telemetry JSONL", value="out/gateway_traffic.jsonl")
    max_lines = st.slider("Read last N lines", 500, 20000, 6000, step=500)

with top3:
    st.subheader("Realtime")
    realtime = st.checkbox("Realtime ON", value=True)
    interval_ms = st.slider("Refresh interval (ms)", 500, 5000, 1200, step=100)

st.divider()

# Realtime refresh
if realtime:
    used = try_autorefresh(int(interval_ms))
    if not used:
        time.sleep(interval_ms / 1000.0)
        try:
            st.experimental_rerun()
        except Exception:
            pass

latest = load_latest_status(jsonl_path, max_lines=max_lines)

# ----------------------------
# Stabilizer control
# ----------------------------
st.subheader("Стабілізатор напруги")

stab_col1, stab_col2, stab_col3, stab_col4 = st.columns([1.2, 1.2, 1.2, 1.2])

stab_payload = latest.get("stabilizer", {})
stab = (stab_payload.get("stabilizer") if isinstance(stab_payload, dict) else {}) or {}
stab_mode = stab.get("mode", "unknown")
vin = stab.get("vin_v", None)
vout = stab.get("vout_v", None)
vout_set = stab.get("vout_set_v", None)
stab_temp = stab.get("temp_c", None)

with stab_col1:
    st.metric("Mode", stab_mode)

with stab_col2:
    st.metric("Vout (actual)", f"{vout}" if vout is not None else "-")

with stab_col3:
    st.metric("Vout set", f"{vout_set}" if vout_set is not None else "-")

with stab_col4:
    st.metric("Temp (°C)", f"{stab_temp}" if stab_temp is not None else "-")

stab_btn1, stab_btn2, stab_btn3, stab_btn4, stab_btn5 = st.columns(5)
with stab_btn1:
    if st.button("-5 V"):
        send_cmd(mqtt_host, int(mqtt_port), base_topic, "STAB_STEP", "stabilizer", -5)
with stab_btn2:
    if st.button("-1 V"):
        send_cmd(mqtt_host, int(mqtt_port), base_topic, "STAB_STEP", "stabilizer", -1)
with stab_btn3:
    if st.button("+1 V"):
        send_cmd(mqtt_host, int(mqtt_port), base_topic, "STAB_STEP", "stabilizer", +1)
with stab_btn4:
    if st.button("+5 V"):
        send_cmd(mqtt_host, int(mqtt_port), base_topic, "STAB_STEP", "stabilizer", +5)
with stab_btn5:
    if st.button("Reset fault"):
        send_cmd(mqtt_host, int(mqtt_port), base_topic, "STAB_RESET_FAULT", "stabilizer", None)

st.divider()

# ----------------------------
# Pump controls
# ----------------------------
st.subheader("Насоси")

p_in = latest.get("pump_in", {})
p_out = latest.get("pump_out", {})
pin = (p_in.get("pump") if isinstance(p_in, dict) else {}) or {}
pout = (p_out.get("pump") if isinstance(p_out, dict) else {}) or {}

p1, p2 = st.columns(2)

with p1:
    st.markdown("### IN pump")
    st.write(f"State: **{pin.get('state', '-') }**")
    st.write(f"RPM: **{pin.get('rpm', '-') }**  | LPM: **{pin.get('lpm', '-') }**")
    st.write(f"Power(W): **{pin.get('power_w', '-') }** | Temp(°C): **{pin.get('temp_motor_c', '-') }**")
    st.write(f"Voltage(V): **{pin.get('voltage_v', '-') }** | Filter wear(%): **{pin.get('filter_wear_pct', '-') }**")

    b1, b2, b3 = st.columns(3)
    with b1:
        if st.button("IN ON"):
            send_cmd(mqtt_host, int(mqtt_port), base_topic, "PUMP_ON", "pump_in", None)
    with b2:
        if st.button("IN OFF"):
            send_cmd(mqtt_host, int(mqtt_port), base_topic, "PUMP_OFF", "pump_in", None)
    with b3:
        rpm_in = st.number_input("Set IN RPM", value=int(pin.get("rpm", 2850) or 2850), step=50)
        if st.button("Apply IN RPM"):
            send_cmd(mqtt_host, int(mqtt_port), base_topic, "SET_RPM", "pump_in", int(rpm_in))

with p2:
    st.markdown("### OUT pump")
    st.write(f"State: **{pout.get('state', '-') }**")
    st.write(f"RPM: **{pout.get('rpm', '-') }**  | LPM: **{pout.get('lpm', '-') }**")
    st.write(f"Power(W): **{pout.get('power_w', '-') }** | Temp(°C): **{pout.get('temp_motor_c', '-') }**")
    st.write(f"Voltage(V): **{pout.get('voltage_v', '-') }**")

    b4, b5, b6 = st.columns(3)
    with b4:
        if st.button("OUT ON"):
            send_cmd(mqtt_host, int(mqtt_port), base_topic, "PUMP_ON", "pump_out", None)
    with b5:
        if st.button("OUT OFF"):
            send_cmd(mqtt_host, int(mqtt_port), base_topic, "PUMP_OFF", "pump_out", None)
    with b6:
        rpm_out = st.number_input("Set OUT RPM", value=int(pout.get("rpm", 2800) or 2800), step=50)
        if st.button("Apply OUT RPM"):
            send_cmd(mqtt_host, int(mqtt_port), base_topic, "SET_RPM", "pump_out", int(rpm_out))

st.divider()

# ----------------------------
# Only chart: storage level
# ----------------------------
st.subheader("Графік: наповнення water storage")

df = load_storage_series(jsonl_path, max_lines=max_lines)
if df.empty:
    st.warning("Нема telemetry по water_storage. Перевір шлях до JSONL та чи запущений симулятор.")
else:
    # show current values
    last = df.iloc[-1].to_dict()
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Level %", f"{last.get('level_pct')}")
    with m2:
        st.metric("In flow (lpm)", f"{last.get('in_flow_lpm')}")
    with m3:
        st.metric("Out flow (lpm)", f"{last.get('out_flow_lpm')}")
    with m4:
        st.metric("Level rate", f"{last.get('level_rate')}")

    st.line_chart(df.set_index("ts")[["level_pct"]], height=320)

st.caption("Control topic: waterplant/control/commands | Telemetry: waterplant/<device>/telemetry")
