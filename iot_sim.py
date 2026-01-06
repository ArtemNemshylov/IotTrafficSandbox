#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import random
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from aiomqtt import Client, MqttError


# ============================================================
# Logging
# ============================================================
def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


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


def deep_copy_jsonable(obj: Any) -> Any:
    # fastest safe-ish way for jsonable structures
    return json.loads(json.dumps(obj))


# ============================================================
# Event Bus (event-driven backbone)
# ============================================================
@dataclass
class Event:
    type: str                 # "tick" | "telemetry" | "control" | "security" | ...
    ts: str                   # ISO time
    source: str               # component id, e.g. "clock", "pump_in", "attacker"
    data: Dict[str, Any]      # payload
    seq: int = 0              # event sequence


class EventBus:
    """
    Simple asyncio-based pub/sub event bus.
    Subscribers receive ALL events, can filter locally.
    """
    def __init__(self, max_queue: int = 20000):
        self._subs: List[asyncio.Queue] = []
        self._seq = 0
        self._max_queue = max_queue
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        async with self._lock:
            self._subs.append(q)
        return q

    async def publish(self, ev: Event) -> None:
        self._seq += 1
        ev.seq = self._seq
        # deliver to all; if some queue is full, drop for that subscriber (MVP)
        async with self._lock:
            for q in self._subs:
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    # drop for that subscriber
                    pass


# ============================================================
# Config
# ============================================================
@dataclass
class SimulationConfig:
    # MQTT
    base_topic: str = "waterplant"

    # Timing
    tick_s: float = 0.7
    jitter_s: float = 0.25
    publish_every_ticks: int = 1  # publish telemetry each N ticks

    # Stabilizer
    nominal_vin: float = 230.0
    nominal_vout: float = 230.0

    # Pumps
    pump_max_pressure_bar: float = 5.0
    pump_nominal_rpm: int = 2850
    pump_nominal_lpm: float = 120.0
    pump_power_kW_per_bar: float = 2.25  # your idea: power ~ pressure*const

    # Filter system
    filter_dp_base: float = 0.2
    filter_dp_max: float = 1.7
    filter_wear_growth_per_tick: float = 0.0015  # ~0.15% per tick if tick=0.7s => ~0.2%/s (tune)

    # Water quality baselines
    ntu_base: float = 0.6
    ph_base: float = 7.3
    cond_base_us_cm: float = 420.0

    # Storage
    tank_min_level_pct: float = 15.0
    tank_max_level_pct: float = 95.0
    tank_init_level_pct: float = 55.0
    tank_level_gain_per_lpm_tick: float = 0.006  # how much % level changes per 1 lpm per tick (tune)

    # Noise
    noise_small: float = 0.02

    # Control/security event probabilities
    prob_failed_auth: float = 0.010
    prob_telemetry_burst: float = 0.010
    burst_len_min: int = 8
    burst_len_max: int = 18

    # Adversarial control probabilities (for demo)
    prob_attack_set_rpm: float = 0.006
    prob_attack_toggle_valve: float = 0.004
    prob_attack_spoof_level: float = 0.003

    # Limits for attacks
    attack_rpm_delta_min: int = 300
    attack_rpm_delta_max: int = 1200


# ============================================================
# Shared System State (single source of truth)
# ============================================================
class WaterPlantState:
    """
    Holds the "truth" state. Telemetry is derived from here.
    Event-driven: controllers modify this state based on events.
    """
    def __init__(self, cfg: SimulationConfig):
        self.cfg = cfg

        # --- Stabilizer ---
        self.stab_mode: str = "NORMAL"  # NORMAL/BYPASS/FAULT
        self.vin: float = cfg.nominal_vin
        self.vout: float = cfg.nominal_vout
        self.active_power_w: int = 0
        self.transformer_temp_c: float = 45.0

        # --- Pumps ---
        self.pump_in_state: str = "ON"   # ON/OFF/FAULT
        self.pump_out_state: str = "ON"
        self.pump_in_rpm: int = cfg.pump_nominal_rpm
        self.pump_out_rpm: int = cfg.pump_nominal_rpm - 50

        self.pump_in_pressure_bar: float = 2.2
        self.pump_out_pressure_bar: float = 2.0

        self.pump_in_flow_lpm: float = cfg.pump_nominal_lpm
        self.pump_out_flow_lpm: float = cfg.pump_nominal_lpm

        self.pump_in_power_w: int = 0
        self.pump_out_power_w: int = 0

        self.pump_in_temp_motor_c: float = 50.0
        self.pump_out_temp_motor_c: float = 48.0

        # --- Filter System ---
        self.filter_mode: str = "FILTER"  # FILTER/BACKWASH/IDLE
        self.valves_state: str = "OPEN"   # OPEN/CLOSED (MVP)
        self.filter_wear_pct: float = 12.0  # 0..100
        self.in_pressure_bar: float = 2.2
        self.out_pressure_bar: float = 2.0
        self.delta_pressure_bar: float = 0.2
        self.ntu: float = cfg.ntu_base
        self.ph: float = cfg.ph_base
        self.conductivity_us_cm: float = cfg.cond_base_us_cm
        self.filter_voltage_v: float = cfg.nominal_vout
        self.filter_current_a: float = 0.8
        self.filter_power_w: int = 180

        # --- Water storage ---
        self.level_pct: float = cfg.tank_init_level_pct
        self.min_level_pct: float = cfg.tank_min_level_pct
        self.max_level_pct: float = cfg.tank_max_level_pct
        self.tank_in_flow_lpm: float = cfg.pump_in_nominal_lpm if hasattr(cfg, "pump_in_nominal_lpm") else cfg.pump_nominal_lpm
        self.tank_out_flow_lpm: float = cfg.pump_nominal_lpm
        self.overflow: bool = False
        self.level_sensors_state: str = "OK"  # OK/FAULT/TAMPER
        self.tank_valves_state: str = "OPEN"  # OPEN/CLOSED
        self.storage_time_s: float = 0.0  # derived-ish

        # --- Security/control (latest indicators) ---
        self.failed_auth: int = 0
        self.net_burst: int = 0
        self.last_command: Optional[Dict[str, Any]] = None

        # internal tick counter
        self.tick_n: int = 0

    # --- derived helpers ---
    def compute_filter_dp_from_wear(self) -> float:
        cfg = self.cfg
        wear01 = clamp(self.filter_wear_pct / 100.0, 0.0, 1.0)
        return clamp(cfg.filter_dp_base + wear01 * (cfg.filter_dp_max - cfg.filter_dp_base), cfg.filter_dp_base, cfg.filter_dp_max)

    def potable_flag(self) -> bool:
        # MVP: potable based on NTU & pH
        return (self.ntu <= 1.5) and (6.5 <= self.ph <= 8.5)

    def summarize_power(self) -> None:
        # stabilizer active power roughly equals sum of loads
        total = int(self.pump_in_power_w + self.pump_out_power_w + self.filter_power_w)
        self.active_power_w = max(0, total)


# ============================================================
# Controllers (event-driven logic)
# ============================================================
class PlantController:
    """
    Listens to tick + control events.
    Updates the shared state so that all parameters remain consistent.
    """
    def __init__(self, cfg: SimulationConfig, state: WaterPlantState, bus: EventBus):
        self.cfg = cfg
        self.state = state
        self.bus = bus

    async def run(self, stop_event: asyncio.Event) -> None:
        q = await self.bus.subscribe()
        while not stop_event.is_set():
            try:
                ev: Event = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if ev.type == "tick":
                self._on_tick(ev)
            elif ev.type == "control":
                self._on_control(ev)
            elif ev.type == "security":
                self._on_security(ev)

    def _on_security(self, ev: Event) -> None:
        # update latest security snapshot
        self.state.failed_auth = int(ev.data.get("failed_auth", 0) or 0)
        self.state.net_burst = int(ev.data.get("burst", 0) or 0)

    def _on_control(self, ev: Event) -> None:
        """
        Control events change setpoints/states.
        We keep it MVP: rpm setpoints + valve toggles + filter mode.
        """
        cmd = ev.data.get("command")
        target = ev.data.get("target")
        value = ev.data.get("value")
        auth_ok = bool(ev.data.get("auth_ok", True))

        self.state.last_command = {
            "ts": ev.ts,
            "source": ev.source,
            "command": cmd,
            "target": target,
            "value": value,
            "auth_ok": auth_ok,
        }

        # Apply regardless, but auth_ok is used by detector (and can be used to refuse)
        if cmd == "SET_RPM" and target in ("pump_in", "pump_out"):
            try:
                rpm = int(value)
            except Exception:
                return
            rpm = int(clamp(rpm, 0, 4000))
            if target == "pump_in":
                self.state.pump_in_rpm = rpm
            else:
                self.state.pump_out_rpm = rpm

        elif cmd == "SET_VALVE" and target in ("filters", "tank"):
            # value: "OPEN"/"CLOSED"
            if not isinstance(value, str):
                return
            v = value.upper()
            if v not in ("OPEN", "CLOSED"):
                return
            if target == "filters":
                self.state.valves_state = v
            else:
                self.state.tank_valves_state = v

        elif cmd == "SET_FILTER_MODE":
            if isinstance(value, str) and value.upper() in ("FILTER", "BACKWASH", "IDLE"):
                self.state.filter_mode = value.upper()

    def _on_tick(self, ev: Event) -> None:
        s = self.state
        cfg = self.cfg
        s.tick_n += 1

        # 1) Stabilizer: slight variations on vin; vout depends on mode
        s.vin = clamp(cfg.nominal_vin + random.uniform(-3.0, 3.0), 200.0, 250.0)
        if s.stab_mode == "NORMAL":
            s.vout = clamp(cfg.nominal_vout + random.uniform(-1.0, 1.0), 210.0, 245.0)
        elif s.stab_mode == "BYPASS":
            s.vout = clamp(s.vin + random.uniform(-2.0, 2.0), 190.0, 250.0)
        else:  # FAULT
            s.vout = clamp(s.vin * random.uniform(0.65, 0.85), 120.0, 220.0)

        # 2) Filter wear grows over time in FILTER mode; BACKWASH reduces wear partially
        if s.filter_mode == "FILTER" and s.valves_state == "OPEN":
            s.filter_wear_pct = clamp(s.filter_wear_pct + cfg.filter_wear_growth_per_tick * 100.0, 0.0, 100.0)
        elif s.filter_mode == "BACKWASH":
            s.filter_wear_pct = clamp(s.filter_wear_pct - random.uniform(0.10, 0.35), 0.0, 100.0)

        # 3) Filter delta pressure derived from wear
        s.delta_pressure_bar = s.compute_filter_dp_from_wear()

        # 4) Pumps -> pressure & flow should be consistent with vout, rpm, valve state, dp
        #    MVP consistency:
        #    - rpm scales flow potential
        #    - dp increases resistance -> reduces effective flow
        #    - CLOSED valves -> almost zero flow
        in_ok = (s.pump_in_state == "ON")
        out_ok = (s.pump_out_state == "ON")
        filters_open = (s.valves_state == "OPEN")
        tank_open = (s.tank_valves_state == "OPEN")

        rpm_in_factor = clamp(s.pump_in_rpm / cfg.pump_nominal_rpm, 0.0, 1.5)
        rpm_out_factor = clamp(s.pump_out_rpm / cfg.pump_nominal_rpm, 0.0, 1.5)
        v_factor = clamp(s.vout / cfg.nominal_vout, 0.3, 1.1)

        # pressure before filters is roughly created by IN pump when running and valves open
        if in_ok and filters_open:
            s.in_pressure_bar = clamp(1.0 + 2.0 * rpm_in_factor * v_factor, 0.1, cfg.pump_max_pressure_bar)
        else:
            s.in_pressure_bar = clamp(s.in_pressure_bar - random.uniform(0.2, 0.5), 0.05, cfg.pump_max_pressure_bar)

        # out pressure depends on in pressure minus dp, also affected by OUT pump pulling
        # if OUT pump is aggressive, it may drop out pressure a bit
        pull = (0.15 * rpm_out_factor) if out_ok else 0.0
        s.out_pressure_bar = clamp(s.in_pressure_bar - s.delta_pressure_bar - pull, 0.02, cfg.pump_max_pressure_bar)

        # flow: reduced by dp and closed valves; also needs both pumps ideally
        base_flow = cfg.pump_nominal_lpm * rpm_in_factor * v_factor
        # resistance factor drops as dp grows
        resist = clamp(1.0 - (s.delta_pressure_bar / cfg.filter_dp_max) * 0.75, 0.08, 1.0)
        flow = base_flow * resist

        # if any valve closed -> nearly zero
        if not filters_open or not tank_open:
            flow *= 0.05

        # if any pump off -> strong reduction
        if not in_ok or not out_ok:
            flow *= 0.25

        s.pump_in_flow_lpm = clamp(flow + random.uniform(-1.5, 1.5), 0.0, cfg.pump_nominal_lpm * 1.3)
        s.pump_out_flow_lpm = clamp(s.pump_in_flow_lpm + random.uniform(-2.0, 2.0), 0.0, cfg.pump_nominal_lpm * 1.3)

        # Pump pressures as "their own" reported pressures (MVP)
        s.pump_in_pressure_bar = s.in_pressure_bar
        s.pump_out_pressure_bar = s.out_pressure_bar

        # 5) Pump power: your idea (pressure * const), plus rpm influence slightly
        if in_ok:
            pin = clamp(s.pump_in_pressure_bar, 0.0, cfg.pump_max_pressure_bar)
            pkw = cfg.pump_power_kW_per_bar * pin * clamp(0.7 + 0.3 * rpm_in_factor, 0.3, 1.4)
            s.pump_in_power_w = int(clamp(pkw * 1000.0, 0.0, 12000.0))
        else:
            s.pump_in_power_w = 0

        if out_ok:
            pout = clamp(s.pump_out_pressure_bar, 0.0, cfg.pump_max_pressure_bar)
            pkw = cfg.pump_power_kW_per_bar * pout * clamp(0.7 + 0.3 * rpm_out_factor, 0.3, 1.4)
            s.pump_out_power_w = int(clamp(pkw * 1000.0, 0.0, 12000.0))
        else:
            s.pump_out_power_w = 0

        # 6) Motor temps: rise with power, cool otherwise
        s.pump_in_temp_motor_c = clamp(s.pump_in_temp_motor_c + (0.0025 * s.pump_in_power_w) / 100.0 + random.uniform(-0.3, 0.5), 20.0, 120.0)
        s.pump_out_temp_motor_c = clamp(s.pump_out_temp_motor_c + (0.0025 * s.pump_out_power_w) / 100.0 + random.uniform(-0.3, 0.5), 20.0, 120.0)

        # Simple FAULT if overheat
        if s.pump_in_temp_motor_c > 105.0:
            s.pump_in_state = "FAULT"
        if s.pump_out_temp_motor_c > 105.0:
            s.pump_out_state = "FAULT"

        # 7) Filter quality: as wear grows, NTU tends to rise; BACKWASH improves
        if s.filter_mode == "FILTER" and filters_open:
            s.ntu = clamp(cfg.ntu_base + (s.filter_wear_pct / 100.0) * 1.8 + random.uniform(-0.15, 0.20), 0.1, 10.0)
        elif s.filter_mode == "BACKWASH":
            s.ntu = clamp(s.ntu - random.uniform(0.15, 0.35), 0.1, 10.0)

        # pH drifts slightly; conductivity drifts slightly
        s.ph = round(clamp(s.ph + random.uniform(-0.03, 0.03), 5.5, 9.5), 2)
        s.conductivity_us_cm = clamp(s.conductivity_us_cm + random.uniform(-3.0, 3.0), 150.0, 1500.0)

        # Filter electrical (very simplified): depends on vout + mode
        s.filter_voltage_v = s.vout
        s.filter_power_w = 180 if s.filter_mode == "FILTER" else (420 if s.filter_mode == "BACKWASH" else 90)
        s.filter_current_a = round(clamp(s.filter_power_w / max(s.filter_voltage_v, 1.0), 0.1, 10.0), 2)

        # 8) Tank: in_flow from pump_out, out_flow random consumption; level_rate derived
        s.tank_in_flow_lpm = s.pump_out_flow_lpm
        # consumption: depends on tank valve and random demand
        demand = random.uniform(30.0, 110.0)
        s.tank_out_flow_lpm = demand if tank_open else 0.0

        level_rate = (s.tank_in_flow_lpm - s.tank_out_flow_lpm) * cfg.tank_level_gain_per_lpm_tick
        s.level_pct = clamp(s.level_pct + level_rate, 0.0, 100.0)

        s.overflow = bool(s.level_pct >= s.max_level_pct and s.tank_in_flow_lpm > 5.0)

        # storage time: grows if low exchange (small net flow)
        exchange = abs(s.tank_in_flow_lpm - s.tank_out_flow_lpm)
        if exchange < 5.0:
            s.storage_time_s += cfg.tick_s
        else:
            s.storage_time_s = max(0.0, s.storage_time_s - cfg.tick_s * 2)

        # 9) Stabilizer power and temp
        s.summarize_power()
        # transformer temp rises with power
        s.transformer_temp_c = clamp(s.transformer_temp_c + (s.active_power_w / 50000.0) + random.uniform(-0.2, 0.3), 25.0, 120.0)


# ============================================================
# Attack / Security Generator (event-driven)
# ============================================================
class SecurityAndAttackGenerator:
    """
    Produces SECURITY and CONTROL events into the event bus.
    This is what makes the demo useful for malicious-activity detection.
    """
    def __init__(self, cfg: SimulationConfig, state: WaterPlantState, bus: EventBus):
        self.cfg = cfg
        self.state = state
        self.bus = bus

    async def run(self, stop_event: asyncio.Event) -> None:
        q = await self.bus.subscribe()
        while not stop_event.is_set():
            try:
                ev: Event = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if ev.type != "tick":
                continue

            # Security indicators per tick
            failed_auth = 0
            if random.random() < self.cfg.prob_failed_auth:
                failed_auth = random.randint(1, 8)

            burst = 1 if random.random() < self.cfg.prob_telemetry_burst else 0

            await self.bus.publish(Event(
                type="security",
                ts=utc_iso(),
                source="security_gen",
                data={"failed_auth": failed_auth, "burst": burst},
            ))

            # Attack-like control events (for demo)
            # 1) Unauthorized RPM changes
            if random.random() < self.cfg.prob_attack_set_rpm:
                target = random.choice(["pump_in", "pump_out"])
                delta = random.randint(self.cfg.attack_rpm_delta_min, self.cfg.attack_rpm_delta_max)
                if random.random() < 0.5:
                    delta = -delta
                base = self.state.pump_in_rpm if target == "pump_in" else self.state.pump_out_rpm
                new_rpm = int(clamp(base + delta, 0, 4000))

                await self.bus.publish(Event(
                    type="control",
                    ts=utc_iso(),
                    source="attacker_remote",
                    data={
                        "command": "SET_RPM",
                        "target": target,
                        "value": new_rpm,
                        "auth_ok": False,
                        "source": "REMOTE",
                    },
                ))

            # 2) Valve toggles (filters or tank)
            if random.random() < self.cfg.prob_attack_toggle_valve:
                tgt = random.choice(["filters", "tank"])
                v = "CLOSED" if (random.random() < 0.5) else "OPEN"
                await self.bus.publish(Event(
                    type="control",
                    ts=utc_iso(),
                    source="attacker_remote",
                    data={
                        "command": "SET_VALVE",
                        "target": tgt,
                        "value": v,
                        "auth_ok": False,
                        "source": "REMOTE",
                    },
                ))

            # 3) Level sensor spoofing marker (doesn't change truth, only changes sensor state)
            if random.random() < self.cfg.prob_attack_spoof_level:
                # flip state to TAMPER for a short time (we model via control event)
                await self.bus.publish(Event(
                    type="control",
                    ts=utc_iso(),
                    source="attacker_remote",
                    data={
                        "command": "SET_LEVEL_SENSOR_STATE",
                        "target": "storage",
                        "value": "TAMPER",
                        "auth_ok": False,
                        "source": "REMOTE",
                    },
                ))


# ============================================================
# Telemetry Builders (5 elements)
# ============================================================
def build_stabilizer_payload(state: WaterPlantState, seq: int) -> Dict[str, Any]:
    return {
        "ts": utc_iso(),
        "device_id": "stabilizer",
        "seq": seq,
        "stabilizer": {
            "vin_v": round(state.vin, 1),
            "vout_v": round(state.vout, 1),
            "mode": state.stab_mode,
            "active_power_w": int(state.active_power_w),
            "transformer_temp_c": round(state.transformer_temp_c, 2),
        },
        "security": {"failed_auth": state.failed_auth, "burst": state.net_burst},
        "control": state.last_command or None,
    }


def build_pump_payload(state: WaterPlantState, pump_id: str, seq: int) -> Dict[str, Any]:
    if pump_id == "pump_in":
        st = state.pump_in_state
        rpm = state.pump_in_rpm
        pressure = state.pump_in_pressure_bar
        flow = state.pump_in_flow_lpm
        power = state.pump_in_power_w
        temp = state.pump_in_temp_motor_c
    else:
        st = state.pump_out_state
        rpm = state.pump_out_rpm
        pressure = state.pump_out_pressure_bar
        flow = state.pump_out_flow_lpm
        power = state.pump_out_power_w
        temp = state.pump_out_temp_motor_c

    return {
        "ts": utc_iso(),
        "device_id": pump_id,
        "seq": seq,
        "pump": {
            "state": st,
            "voltage_v": round(state.vout, 1),
            "rpm": int(rpm),
            "pressure_bar": round(pressure, 3),
            "flow_lpm": round(flow, 2),
            "power_w": int(power),
            "temp_motor_c": round(temp, 2),
        },
        "security": {"failed_auth": state.failed_auth, "burst": state.net_burst},
        "control": state.last_command or None,
    }


def build_filter_payload(state: WaterPlantState, seq: int) -> Dict[str, Any]:
    return {
        "ts": utc_iso(),
        "device_id": "filter_system",
        "seq": seq,
        "filters": {
            "mode": state.filter_mode,
            "valves_state": state.valves_state,
            "wear_pct": round(state.filter_wear_pct, 2),
            "in_pressure_bar": round(state.in_pressure_bar, 3),
            "out_pressure_bar": round(state.out_pressure_bar, 3),
            "delta_pressure_bar": round(state.delta_pressure_bar, 3),
            "ntu": round(state.ntu, 2),
            "ph": round(state.ph, 2),
            "conductivity_us_cm": round(state.conductivity_us_cm, 1),
            "voltage_v": round(state.filter_voltage_v, 1),
            "current_a": round(state.filter_current_a, 2),
            "power_w": int(state.filter_power_w),
            "is_potable": bool(state.potable_flag()),
        },
        "security": {"failed_auth": state.failed_auth, "burst": state.net_burst},
        "control": state.last_command or None,
    }


def build_storage_payload(state: WaterPlantState, seq: int) -> Dict[str, Any]:
    # level_rate is derived here for transparency
    level_rate = (state.tank_in_flow_lpm - state.tank_out_flow_lpm) * state.cfg.tank_level_gain_per_lpm_tick
    return {
        "ts": utc_iso(),
        "device_id": "water_storage",
        "seq": seq,
        "storage": {
            "level_pct": round(state.level_pct, 2),
            "min_level_pct": round(state.min_level_pct, 2),
            "max_level_pct": round(state.max_level_pct, 2),
            "in_flow_lpm": round(state.tank_in_flow_lpm, 2),
            "out_flow_lpm": round(state.tank_out_flow_lpm, 2),
            "level_rate": round(level_rate, 4),
            "overflow": bool(state.overflow),
            "valves_state": state.tank_valves_state,
            "level_sensors_state": state.level_sensors_state,
            "ntu": round(state.ntu, 2),
            "ph": round(state.ph, 2),
            "storage_time_s": int(state.storage_time_s),
        },
        "security": {"failed_auth": state.failed_auth, "burst": state.net_burst},
        "control": state.last_command or None,
    }


# ============================================================
# Publisher: listens to bus and publishes telemetry (event-driven)
# ============================================================
async def mqtt_publisher(
    host: str,
    port: int,
    base_topic: str,
    bus: EventBus,
    stop_event: asyncio.Event,
    state: WaterPlantState,
    out_jsonl: str,
    elements: List[str],
    publish_every_ticks: int,
    jitter_s: float,
    enable_mqtt: bool = True,
) -> None:
    """
    Subscribes to EventBus. On each tick, emits telemetry events for chosen elements,
    writes to out_jsonl, and optionally publishes to MQTT.
    """
    ensure_dir_for_file(out_jsonl)
    q = await bus.subscribe()

    # per-element counters
    seq_map: Dict[str, int] = {e: 0 for e in elements}

    # MQTT connection loop
    while not stop_event.is_set():
        try:
            mqtt_client: Optional[Client] = None
            if enable_mqtt:
                log(f"[PUB] connecting to mqtt://{host}:{port}")
                mqtt_client = Client(hostname=host, port=port)
                await mqtt_client.__aenter__()
                log(f"[PUB] connected")

            with open(out_jsonl, "a", encoding="utf-8") as f:
                log(f"[PUB] writing to {os.path.abspath(out_jsonl)}")

                while not stop_event.is_set():
                    try:
                        ev: Event = await asyncio.wait_for(q.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue

                    if ev.type != "tick":
                        # apply special control for spoof sensor state
                        if ev.type == "control":
                            cmd = ev.data.get("command")
                            if cmd == "SET_LEVEL_SENSOR_STATE":
                                v = str(ev.data.get("value", "")).upper()
                                if v in ("OK", "FAULT", "TAMPER"):
                                    state.level_sensors_state = v
                        continue

                    # publish telemetry every N ticks
                    if publish_every_ticks > 1 and (state.tick_n % publish_every_ticks != 0):
                        continue

                    # optional tiny jitter so messages aren't perfectly aligned
                    if jitter_s > 0:
                        await asyncio.sleep(random.uniform(0.0, jitter_s))

                    # build all element payloads and emit
                    for element in elements:
                        seq_map[element] += 1
                        if element == "stabilizer":
                            payload = build_stabilizer_payload(state, seq_map[element])
                        elif element == "pump_in":
                            payload = build_pump_payload(state, "pump_in", seq_map[element])
                        elif element == "pump_out":
                            payload = build_pump_payload(state, "pump_out", seq_map[element])
                        elif element == "filter_system":
                            payload = build_filter_payload(state, seq_map[element])
                        elif element == "water_storage":
                            payload = build_storage_payload(state, seq_map[element])
                        else:
                            continue

                        topic = f"{base_topic}/{element}/telemetry"

                        # write JSONL
                        f.write(json.dumps({"topic": topic, "payload": payload}, ensure_ascii=False) + "\n")
                        f.flush()

                        # publish to MQTT
                        if mqtt_client is not None:
                            await mqtt_client.publish(topic, json.dumps(payload).encode("utf-8"), qos=0)

                    # if security says burst -> emit extra telemetry bursts
                    if state.net_burst == 1:
                        burst_len = random.randint(state.cfg.burst_len_min, state.cfg.burst_len_max)
                        for _ in range(burst_len):
                            if stop_event.is_set():
                                break
                            element = random.choice(elements)
                            seq_map[element] += 1
                            if element == "stabilizer":
                                payload = build_stabilizer_payload(state, seq_map[element])
                            elif element == "pump_in":
                                payload = build_pump_payload(state, "pump_in", seq_map[element])
                            elif element == "pump_out":
                                payload = build_pump_payload(state, "pump_out", seq_map[element])
                            elif element == "filter_system":
                                payload = build_filter_payload(state, seq_map[element])
                            elif element == "water_storage":
                                payload = build_storage_payload(state, seq_map[element])
                            else:
                                continue
                            topic = f"{base_topic}/{element}/telemetry"
                            f.write(json.dumps({"topic": topic, "payload": payload}, ensure_ascii=False) + "\n")
                            f.flush()
                            if mqtt_client is not None:
                                await mqtt_client.publish(topic, json.dumps(payload).encode("utf-8"), qos=0)
                            await asyncio.sleep(0.03)

        except MqttError as e:
            log(f"[PUB] MQTT error: {repr(e)} (retry in 1s)")
            await asyncio.sleep(1.0)
        except Exception as e:
            log(f"[PUB] Unexpected error: {repr(e)} (retry in 1s)")
            await asyncio.sleep(1.0)
        finally:
            # close mqtt client if open
            try:
                if 'mqtt_client' in locals() and mqtt_client is not None:
                    await mqtt_client.__aexit__(None, None, None)
            except Exception:
                pass


# ============================================================
# Clock: emits tick events into bus
# ============================================================
async def clock_task(bus: EventBus, stop_event: asyncio.Event, tick_s: float) -> None:
    n = 0
    while not stop_event.is_set():
        n += 1
        await bus.publish(Event(type="tick", ts=utc_iso(), source="clock", data={"tick": n}))
        await asyncio.sleep(tick_s)


# ============================================================
# Simple Gateway: subscribes MQTT and logs to console (optional)
# ============================================================
async def mqtt_gateway_viewer(
    host: str,
    port: int,
    base_topic: str,
    out_jsonl: str,
    stop_event: asyncio.Event,
    subscribe_all: bool = False,
) -> None:
    """
    Optional: reads MQTT and appends to out_jsonl.
    If you're already writing JSONL in publisher, you can skip this.
    """
    ensure_dir_for_file(out_jsonl)
    topic_filter = "#" if subscribe_all else f"{base_topic}/+/telemetry"

    while not stop_event.is_set():
        try:
            log(f"[GW] connecting to mqtt://{host}:{port}")
            async with Client(hostname=host, port=port) as client:
                log(f"[GW] connected, subscribing to '{topic_filter}'")
                await client.subscribe(topic_filter)

                with open(out_jsonl, "a", encoding="utf-8") as f:
                    async for msg in client.messages:
                        if stop_event.is_set():
                            break

                        topic = str(msg.topic)
                        if not subscribe_all:
                            if not topic.startswith(f"{base_topic}/") or not topic.endswith("/telemetry"):
                                continue

                        try:
                            payload = json.loads(msg.payload.decode("utf-8"))
                        except Exception:
                            payload = {"raw": msg.payload.decode("utf-8", errors="replace")}

                        f.write(json.dumps({"topic": topic, "payload": payload}, ensure_ascii=False) + "\n")
                        f.flush()

                        dev = payload.get("device_id", "unknown")
                        seq = payload.get("seq", "?")

                        # compact preview
                        preview = ""
                        if dev in ("pump_in", "pump_out") and isinstance(payload.get("pump"), dict):
                            p = payload["pump"]
                            preview = f"state={p.get('state')} rpm={p.get('rpm')} flow={p.get('flow_lpm')} pressure={p.get('pressure_bar')} temp={p.get('temp_motor_c')}"
                        elif dev == "filter_system" and isinstance(payload.get("filters"), dict):
                            fl = payload["filters"]
                            preview = f"mode={fl.get('mode')} dp={fl.get('delta_pressure_bar')} wear={fl.get('wear_pct')} ntu={fl.get('ntu')} potable={fl.get('is_potable')}"
                        elif dev == "water_storage" and isinstance(payload.get("storage"), dict):
                            st = payload["storage"]
                            preview = f"level={st.get('level_pct')} in={st.get('in_flow_lpm')} out={st.get('out_flow_lpm')} overflow={st.get('overflow')} sensor={st.get('level_sensors_state')}"
                        elif dev == "stabilizer" and isinstance(payload.get("stabilizer"), dict):
                            sb = payload["stabilizer"]
                            preview = f"vin={sb.get('vin_v')} vout={sb.get('vout_v')} mode={sb.get('mode')} P={sb.get('active_power_w')} T={sb.get('transformer_temp_c')}"

                        log(f"[GW] {dev} seq={seq} {preview}")

        except MqttError as e:
            log(f"[GW] MQTT error: {repr(e)} (retry in 1s)")
            await asyncio.sleep(1.0)
        except Exception as e:
            log(f"[GW] Unexpected error: {repr(e)} (retry in 1s)")
            await asyncio.sleep(1.0)


# ============================================================
# Main
# ============================================================
def install_signal_handlers(stop_event: asyncio.Event) -> None:
    def _handler(*_):
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Event-driven Water Plant IoT simulator (5 elements) over MQTT + JSONL")
    p.add_argument("--host", default="127.0.0.1", help="MQTT broker host")
    p.add_argument("--port", default=1883, type=int, help="MQTT broker port")
    p.add_argument("--base-topic", default="waterplant", help="Base topic")
    p.add_argument("--mode", choices=["all", "sim", "viewer"], default="all", help="Run mode")

    p.add_argument("--tick", type=float, default=0.7, help="Tick seconds")
    p.add_argument("--jitter", type=float, default=0.25, help="Publish jitter seconds")
    p.add_argument("--publish-every", type=int, default=1, help="Publish telemetry every N ticks")

    p.add_argument(
        "--elements",
        default="stabilizer,pump_in,pump_out,filter_system,water_storage",
        help="Comma-separated element list"
    )

    p.add_argument("--out", default="out/gateway_traffic.jsonl", help="Output JSONL")
    p.add_argument("--viewer-out", default="out/mqtt_viewer.jsonl", help="Viewer JSONL (only if mode=viewer/all)")
    p.add_argument("--subscribe-all", action="store_true", help="Viewer subscribes to '#'")

    p.add_argument("--no-mqtt", action="store_true", help="Disable MQTT publish (still writes JSONL)")
    return p.parse_args()


async def run_all(args: argparse.Namespace) -> None:
    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    cfg = SimulationConfig(
        base_topic=args.base_topic,
        tick_s=args.tick,
        jitter_s=args.jitter,
        publish_every_ticks=max(1, args.publish_every),
    )
    bus = EventBus()
    state = WaterPlantState(cfg)

    elements = [e.strip() for e in args.elements.split(",") if e.strip()]

    controller = PlantController(cfg, state, bus)
    secgen = SecurityAndAttackGenerator(cfg, state, bus)

    tasks: List[asyncio.Task] = []

    if args.mode in ("all", "sim"):
        tasks.append(asyncio.create_task(clock_task(bus, stop_event, tick_s=cfg.tick_s)))
        tasks.append(asyncio.create_task(controller.run(stop_event)))
        tasks.append(asyncio.create_task(secgen.run(stop_event)))
        tasks.append(asyncio.create_task(
            mqtt_publisher(
                host=args.host,
                port=args.port,
                base_topic=args.base_topic,
                bus=bus,
                stop_event=stop_event,
                state=state,
                out_jsonl=args.out,
                elements=elements,
                publish_every_ticks=cfg.publish_every_ticks,
                jitter_s=cfg.jitter_s,
                enable_mqtt=(not args.no_mqtt),
            )
        ))

    if args.mode in ("all", "viewer"):
        # viewer reads from MQTT, useful to see traffic even if you don't tail JSONL
        tasks.append(asyncio.create_task(
            mqtt_gateway_viewer(
                host=args.host,
                port=args.port,
                base_topic=args.base_topic,
                out_jsonl=args.viewer_out,
                stop_event=stop_event,
                subscribe_all=args.subscribe_all,
            )
        ))

    log(f"[MAIN] mode={args.mode} host={args.host} port={args.port} base_topic={args.base_topic}")
    log(f"[MAIN] out={os.path.abspath(args.out)} viewer_out={os.path.abspath(args.viewer_out)} elements={elements}")
    if args.no_mqtt:
        log("[MAIN] MQTT publishing disabled (--no-mqtt). Writing JSONL only.")

    while not stop_event.is_set():
        await asyncio.sleep(0.2)

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run_all(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
