#!/usr/bin/env python3
# waterplant_sim.py

import argparse
import asyncio
import json
import os
import random
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

from aiomqtt import Client, MqttError


# ============================================================
# Helpers
# ============================================================
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def ensure_dir_for_file(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ============================================================
# Config
# ============================================================
@dataclass
class SimulationConfig:
    base_topic: str = "waterplant"

    tick_s: float = 0.7
    jitter_s: float = 0.10

    # Stabilizer
    vin_nominal_v: float = 230.0
    vout_default_v: float = 230.0
    vout_min_ok_v: float = 200.0
    vout_min_fault_v: float = 185.0
    vout_max_v: float = 250.0

    # Pumps base characteristics
    pump_nominal_rpm: int = 2850
    pump_nominal_lpm: float = 120.0
    pump_max_rpm: int = 4000

    # Model for pump "voltage at terminals" affected by RPM
    # (user request: increasing RPM increases voltage)
    pump_voltage_gain_per_rpm_factor: float = 12.0  # extra volts when rpm_factor > 1.0

    # Filter wear (slower; you asked ~10x smaller earlier)
    wear_growth_per_tick_pct: float = 0.015  # +0.015% per tick (slow)
    wear_max_pct: float = 100.0

    # Filter influence on inflow (wear -> less inflow)
    # inflow_multiplier = 1 - wear_pct/120 (clamped)
    wear_flow_divisor: float = 120.0
    min_flow_multiplier: float = 0.10

    # Wear influence on inlet pump "pressure/load"
    # load_multiplier = 1 + wear_pct/80 (clamped)
    wear_load_divisor: float = 80.0
    max_load_multiplier: float = 2.50

    # Storage dynamics
    tank_init_level_pct: float = 55.0
    tank_level_gain_per_lpm_tick: float = 0.006  # delta level per (lpm net) per tick
    tank_min_level_pct: float = 0.0
    tank_max_level_pct: float = 100.0

    # Temperatures
    ambient_c: float = 28.0
    motor_heat_per_kw_tick: float = 0.40   # °C per kW per tick
    motor_cool_per_tick: float = 1.00      # °C per tick (when OFF)

    # Power model (rough)
    pump_kw_nominal: float = 2.25          # ~at nominal load
    stabilizer_base_w: int = 60            # own consumption
    stabilizer_heat_per_kw_tick: float = 0.12


# ============================================================
# State
# ============================================================
class PlantState:
    def __init__(self, cfg: SimulationConfig):
        self.cfg = cfg

        # Stabilizer control
        self.stab_mode: str = "NORMAL"   # NORMAL / FAULT
        self.vin_v: float = cfg.vin_nominal_v
        self.vout_set_v: float = cfg.vout_default_v
        self.vout_v: float = cfg.vout_default_v
        self.stab_temp_c: float = 45.0
        self.stab_power_w: int = cfg.stabilizer_base_w

        # Pumps
        self.pump_in_state: str = "ON"   # ON/OFF
        self.pump_out_state: str = "ON"  # ON/OFF
        self.pump_in_rpm: int = cfg.pump_nominal_rpm
        self.pump_out_rpm: int = cfg.pump_nominal_rpm - 50

        self.pump_in_lpm: float = cfg.pump_nominal_lpm
        self.pump_out_lpm: float = cfg.pump_nominal_lpm

        self.pump_in_voltage_v: float = cfg.vout_default_v
        self.pump_out_voltage_v: float = cfg.vout_default_v

        self.pump_in_power_w: int = 0
        self.pump_out_power_w: int = 0

        self.pump_in_temp_c: float = 52.0
        self.pump_out_temp_c: float = 50.0

        # Filter
        self.filter_wear_pct: float = 12.0

        # Storage
        self.level_pct: float = cfg.tank_init_level_pct
        self.in_flow_lpm: float = 0.0
        self.out_flow_lpm: float = 0.0
        self.level_rate: float = 0.0

        # Security/control
        self.last_command: Optional[Dict[str, Any]] = None

        self.tick_n: int = 0


# ============================================================
# Physics step
# ============================================================
def compute_rpm_factor(cfg: SimulationConfig, rpm: int) -> float:
    return clamp(rpm / float(cfg.pump_nominal_rpm), 0.0, cfg.pump_max_rpm / float(cfg.pump_nominal_rpm))


def compute_filter_flow_multiplier(cfg: SimulationConfig, wear_pct: float) -> float:
    m = 1.0 - (wear_pct / cfg.wear_flow_divisor)
    return clamp(m, cfg.min_flow_multiplier, 1.0)


def compute_load_multiplier(cfg: SimulationConfig, wear_pct: float) -> float:
    m = 1.0 + (wear_pct / cfg.wear_load_divisor)
    return clamp(m, 1.0, cfg.max_load_multiplier)


def step_physics(s: PlantState) -> None:
    cfg = s.cfg
    s.tick_n += 1

    # VIN small drift
    s.vin_v = clamp(cfg.vin_nominal_v + random.uniform(-4.0, 4.0), 205.0, 250.0)

    # Filter wear increases slowly over time if there is inflow (pump_in ON)
    if s.pump_in_state == "ON":
        s.filter_wear_pct = clamp(s.filter_wear_pct + cfg.wear_growth_per_tick_pct, 0.0, cfg.wear_max_pct)

    # How wear affects inflow and load
    flow_mult = compute_filter_flow_multiplier(cfg, s.filter_wear_pct)
    load_mult = compute_load_multiplier(cfg, s.filter_wear_pct)

    # Pump factors
    in_rpm_f = compute_rpm_factor(cfg, s.pump_in_rpm)
    out_rpm_f = compute_rpm_factor(cfg, s.pump_out_rpm)

    # "Requested" pump terminal voltage increases with RPM (user request)
    # terminal_v = stabilizer_vout + (rpm_factor-1)*gain
    s.pump_in_voltage_v = clamp(s.vout_v + (in_rpm_f - 1.0) * cfg.pump_voltage_gain_per_rpm_factor, 0.0, cfg.vout_max_v)
    s.pump_out_voltage_v = clamp(s.vout_v + (out_rpm_f - 1.0) * cfg.pump_voltage_gain_per_rpm_factor, 0.0, cfg.vout_max_v)

    # Compute LPM:
    # - Inflow depends on pump_in and filter wear (flow_mult)
    # - Outflow depends only on pump_out
    if s.pump_in_state == "ON":
        s.pump_in_lpm = clamp(cfg.pump_nominal_lpm * in_rpm_f * flow_mult + random.uniform(-1.5, 1.5), 0.0, cfg.pump_nominal_lpm * 1.6)
    else:
        s.pump_in_lpm = clamp(s.pump_in_lpm - random.uniform(30.0, 55.0), 0.0, cfg.pump_nominal_lpm * 1.6)

    if s.pump_out_state == "ON":
        s.pump_out_lpm = clamp(cfg.pump_nominal_lpm * out_rpm_f + random.uniform(-1.5, 1.5), 0.0, cfg.pump_nominal_lpm * 1.6)
    else:
        s.pump_out_lpm = clamp(s.pump_out_lpm - random.uniform(30.0, 55.0), 0.0, cfg.pump_nominal_lpm * 1.6)

    # Storage flow rule (your requirement):
    # in_flow only via pump_in; out_flow only via pump_out
    s.in_flow_lpm = s.pump_in_lpm if s.pump_in_state == "ON" else 0.0
    s.out_flow_lpm = s.pump_out_lpm if s.pump_out_state == "ON" else 0.0

    # Storage level change
    net = s.in_flow_lpm - s.out_flow_lpm
    s.level_rate = net * cfg.tank_level_gain_per_lpm_tick
    s.level_pct = clamp(s.level_pct + s.level_rate, cfg.tank_min_level_pct, cfg.tank_max_level_pct)

    # Power:
    # - pump_in power increases with wear/load (more resistance) + rpm
    # - OFF => 0
    if s.pump_in_state == "ON":
        p_kw = cfg.pump_kw_nominal * in_rpm_f * load_mult
        s.pump_in_power_w = int(clamp(p_kw * 1000.0, 0.0, 20000.0))
    else:
        s.pump_in_power_w = 0

    if s.pump_out_state == "ON":
        # Out pump not impacted by filter wear (only pumping from storage)
        p_kw = cfg.pump_kw_nominal * out_rpm_f
        s.pump_out_power_w = int(clamp(p_kw * 1000.0, 0.0, 20000.0))
    else:
        s.pump_out_power_w = 0

    # Filter power rule (your requirement):
    # If inlet pump OFF => filters consume 0 (no water to filter)
    filter_power_w = 0 if s.pump_in_state != "ON" else 180
    # (we don't publish separate filter power in MVP, but stabilizer sees total load)

    # Stabilizer vout droop from load, but also obey setpoint
    total_load_w = s.pump_in_power_w + s.pump_out_power_w + filter_power_w
    droop_v = clamp(total_load_w / 12000.0, 0.0, 18.0)  # rough
    if s.stab_mode == "FAULT":
        # In fault, output sags heavily
        s.vout_v = clamp(s.vin_v * random.uniform(0.65, 0.85), 120.0, cfg.vout_max_v)
    else:
        s.vout_v = clamp(s.vout_set_v - droop_v + random.uniform(-0.4, 0.4), 0.0, cfg.vout_max_v)

    # Fault condition: too low output
    if s.vout_v < cfg.vout_min_fault_v:
        s.stab_mode = "FAULT"
    elif s.vout_v >= cfg.vout_min_ok_v:
        s.stab_mode = "NORMAL"

    # Stabilizer power + temp
    s.stab_power_w = int(cfg.stabilizer_base_w + total_load_w)
    stab_kw = s.stab_power_w / 1000.0
    s.stab_temp_c = clamp(s.stab_temp_c + stab_kw * cfg.stabilizer_heat_per_kw_tick + random.uniform(-0.1, 0.2), 25.0, 120.0)

    # Motor temperatures
    if s.pump_in_state == "ON":
        s.pump_in_temp_c = clamp(
            s.pump_in_temp_c + (s.pump_in_power_w / 1000.0) * cfg.motor_heat_per_kw_tick + random.uniform(-0.15, 0.25),
            20.0, 130.0
        )
    else:
        s.pump_in_temp_c = clamp(
            s.pump_in_temp_c - cfg.motor_cool_per_tick - (s.pump_in_temp_c - cfg.ambient_c) * 0.02,
            20.0, 130.0
        )

    if s.pump_out_state == "ON":
        s.pump_out_temp_c = clamp(
            s.pump_out_temp_c + (s.pump_out_power_w / 1000.0) * cfg.motor_heat_per_kw_tick + random.uniform(-0.15, 0.25),
            20.0, 130.0
        )
    else:
        s.pump_out_temp_c = clamp(
            s.pump_out_temp_c - cfg.motor_cool_per_tick - (s.pump_out_temp_c - cfg.ambient_c) * 0.02,
            20.0, 130.0
        )


# ============================================================
# Telemetry builders
# ============================================================
def payload_stabilizer(s: PlantState, seq: int) -> Dict[str, Any]:
    return {
        "ts": utc_iso(),
        "device_id": "stabilizer",
        "seq": seq,
        "stabilizer": {
            "mode": s.stab_mode,
            "vin_v": round(s.vin_v, 1),
            "vout_set_v": round(s.vout_set_v, 1),
            "vout_v": round(s.vout_v, 1),
            "power_w": int(s.stab_power_w),
            "temp_c": round(s.stab_temp_c, 2),
        },
        "control": s.last_command,
    }


def payload_pump(s: PlantState, pump_id: str, seq: int) -> Dict[str, Any]:
    if pump_id == "pump_in":
        return {
            "ts": utc_iso(),
            "device_id": "pump_in",
            "seq": seq,
            "pump": {
                "state": s.pump_in_state,
                "rpm": int(s.pump_in_rpm),
                "voltage_v": round(s.pump_in_voltage_v, 1),
                "lpm": round(s.pump_in_lpm, 2),
                "power_w": int(s.pump_in_power_w),
                "temp_motor_c": round(s.pump_in_temp_c, 2),
                "filter_wear_pct": round(s.filter_wear_pct, 2),
            },
            "control": s.last_command,
        }
    else:
        return {
            "ts": utc_iso(),
            "device_id": "pump_out",
            "seq": seq,
            "pump": {
                "state": s.pump_out_state,
                "rpm": int(s.pump_out_rpm),
                "voltage_v": round(s.pump_out_voltage_v, 1),
                "lpm": round(s.pump_out_lpm, 2),
                "power_w": int(s.pump_out_power_w),
                "temp_motor_c": round(s.pump_out_temp_c, 2),
            },
            "control": s.last_command,
        }


def payload_filter(s: PlantState, seq: int) -> Dict[str, Any]:
    # MVP: лише знос, бо впливає на inflow і навантаження
    return {
        "ts": utc_iso(),
        "device_id": "filter_system",
        "seq": seq,
        "filters": {
            "wear_pct": round(s.filter_wear_pct, 2),
            "flow_multiplier": round(compute_filter_flow_multiplier(s.cfg, s.filter_wear_pct), 3),
            "load_multiplier": round(compute_load_multiplier(s.cfg, s.filter_wear_pct), 3),
            "power_w": 0 if s.pump_in_state != "ON" else 180,
        },
        "control": s.last_command,
    }


def payload_storage(s: PlantState, seq: int) -> Dict[str, Any]:
    return {
        "ts": utc_iso(),
        "device_id": "water_storage",
        "seq": seq,
        "storage": {
            "level_pct": round(s.level_pct, 2),
            "in_flow_lpm": round(s.in_flow_lpm, 2),
            "out_flow_lpm": round(s.out_flow_lpm, 2),
            "level_rate": round(s.level_rate, 4),
        },
        "control": s.last_command,
    }


# ============================================================
# Control handling
# ============================================================
def apply_control(s: PlantState, cmd: str, target: str, value: Any) -> None:
    cfg = s.cfg

    if cmd == "PUMP_ON":
        if target == "pump_in":
            s.pump_in_state = "ON"
        elif target == "pump_out":
            s.pump_out_state = "ON"
        return

    if cmd == "PUMP_OFF":
        if target == "pump_in":
            s.pump_in_state = "OFF"
        elif target == "pump_out":
            s.pump_out_state = "OFF"
        return

    if cmd == "SET_RPM":
        try:
            rpm = int(value)
        except Exception:
            return
        rpm = int(clamp(rpm, 0, cfg.pump_max_rpm))
        if target == "pump_in":
            s.pump_in_rpm = rpm
        elif target == "pump_out":
            s.pump_out_rpm = rpm
        return

    if cmd == "STAB_SET_VOUT":
        try:
            v = float(value)
        except Exception:
            return
        s.vout_set_v = clamp(v, 0.0, cfg.vout_max_v)
        return

    if cmd == "STAB_STEP":
        # value is delta (+/-)
        try:
            dv = float(value)
        except Exception:
            return
        s.vout_set_v = clamp(s.vout_set_v + dv, 0.0, cfg.vout_max_v)
        return

    if cmd == "STAB_RESET_FAULT":
        # allow recovery if setpoint ok
        if s.vout_set_v >= cfg.vout_min_ok_v:
            s.stab_mode = "NORMAL"
        return


# ============================================================
# Tasks
# ============================================================
async def control_listener(host: str, port: int, cfg: SimulationConfig, s: PlantState, stop_event: asyncio.Event) -> None:
    topic = f"{cfg.base_topic}/control/commands"

    while not stop_event.is_set():
        try:
            async with Client(hostname=host, port=port) as client:
                await client.subscribe(topic)
                log(f"[CTL] subscribed {topic}")

                async for msg in client.messages:
                    if stop_event.is_set():
                        break
                    try:
                        data = json.loads(msg.payload.decode("utf-8"))
                    except Exception:
                        continue

                    cmd = data.get("command")
                    target = data.get("target")
                    value = data.get("value")

                    if not isinstance(cmd, str) or not isinstance(target, str):
                        continue

                    s.last_command = {
                        "ts": utc_iso(),
                        "source": data.get("source", "HMI"),
                        "command": cmd,
                        "target": target,
                        "value": value,
                        "auth_ok": bool(data.get("auth_ok", True)),
                    }

                    apply_control(s, cmd, target, value)

        except MqttError as e:
            log(f"[CTL] MQTT error: {repr(e)} retry 1s")
            await asyncio.sleep(1.0)
        except Exception as e:
            log(f"[CTL] Unexpected error: {repr(e)} retry 1s")
            await asyncio.sleep(1.0)


async def publisher(host: str, port: int, cfg: SimulationConfig, s: PlantState, stop_event: asyncio.Event, out_jsonl: str) -> None:
    ensure_dir_for_file(out_jsonl)

    seq = {"stabilizer": 0, "pump_in": 0, "pump_out": 0, "filter_system": 0, "water_storage": 0}

    while not stop_event.is_set():
        try:
            async with Client(hostname=host, port=port) as client:
                log(f"[PUB] connected mqtt://{host}:{port}")
                with open(out_jsonl, "a", encoding="utf-8") as f:
                    while not stop_event.is_set():
                        # physics tick
                        step_physics(s)

                        # small jitter
                        if cfg.jitter_s > 0:
                            await asyncio.sleep(random.uniform(0.0, cfg.jitter_s))

                        # publish all devices
                        seq["stabilizer"] += 1
                        p1 = payload_stabilizer(s, seq["stabilizer"])
                        t1 = f"{cfg.base_topic}/stabilizer/telemetry"

                        seq["pump_in"] += 1
                        p2 = payload_pump(s, "pump_in", seq["pump_in"])
                        t2 = f"{cfg.base_topic}/pump_in/telemetry"

                        seq["pump_out"] += 1
                        p3 = payload_pump(s, "pump_out", seq["pump_out"])
                        t3 = f"{cfg.base_topic}/pump_out/telemetry"

                        seq["filter_system"] += 1
                        p4 = payload_filter(s, seq["filter_system"])
                        t4 = f"{cfg.base_topic}/filter_system/telemetry"

                        seq["water_storage"] += 1
                        p5 = payload_storage(s, seq["water_storage"])
                        t5 = f"{cfg.base_topic}/water_storage/telemetry"

                        for topic, payload in [(t1, p1), (t2, p2), (t3, p3), (t4, p4), (t5, p5)]:
                            line = {"topic": topic, "payload": payload}
                            f.write(json.dumps(line, ensure_ascii=False) + "\n")
                            await client.publish(topic, json.dumps(payload).encode("utf-8"), qos=0)

                        f.flush()

                        await asyncio.sleep(cfg.tick_s)

        except MqttError as e:
            log(f"[PUB] MQTT error: {repr(e)} retry 1s")
            await asyncio.sleep(1.0)
        except Exception as e:
            log(f"[PUB] Unexpected error: {repr(e)} retry 1s")
            await asyncio.sleep(1.0)


# ============================================================
# Main
# ============================================================
def install_signal_handlers(stop_event: asyncio.Event) -> None:
    def _h(*_):
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _h)
        signal.signal(signal.SIGTERM, _h)
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="WaterPlant simulator + MQTT control + JSONL")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--base-topic", default="waterplant")
    p.add_argument("--tick", type=float, default=0.7)
    p.add_argument("--jitter", type=float, default=0.10)
    p.add_argument("--out", default="out/gateway_traffic.jsonl")
    return p.parse_args()


async def run() -> None:
    args = parse_args()
    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    cfg = SimulationConfig(base_topic=args.base_topic, tick_s=args.tick, jitter_s=args.jitter)
    s = PlantState(cfg)

    log(f"[MAIN] base_topic={cfg.base_topic} out={os.path.abspath(args.out)}")
    log(f"[MAIN] control topic: {cfg.base_topic}/control/commands")
    log(f"[MAIN] telemetry topics: {cfg.base_topic}/<device>/telemetry")

    tasks = [
        asyncio.create_task(control_listener(args.host, args.port, cfg, s, stop_event)),
        asyncio.create_task(publisher(args.host, args.port, cfg, s, stop_event, args.out)),
    ]

    while not stop_event.is_set():
        await asyncio.sleep(0.2)

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
