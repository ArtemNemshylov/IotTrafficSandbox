#!/usr/bin/env python3
"""
Plot time-series from two JSONL files produced by the simulator:
- out/gateway_traffic.jsonl (publisher JSONL)
- out/mqtt_viewer.jsonl     (optional viewer JSONL)

Each line is expected as:
{"topic": "...", "payload": {...}}

This script:
- loads both files (if exist)
- normalizes into a flat table
- builds simple time-series plots (one plot per metric)

Usage:
  python plot_jsonl.py --a out/gateway_traffic.jsonl --b out/mqtt_viewer.jsonl --outdir out/plots

Notes:
- Uses matplotlib only (no seaborn).
- No fixed colors.
"""

import argparse
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.pyplot as plt


# ----------------------------
# Helpers
# ----------------------------
def parse_ts(ts: str) -> Optional[datetime]:
    # expects ISO like "2026-01-04T13:38:53.134046+00:00"
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def safe_get(d: Dict[str, Any], path: str) -> Any:
    cur: Any = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path or not os.path.exists(path):
        return rows

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            topic = obj.get("topic")
            payload = obj.get("payload")
            if not isinstance(topic, str) or not isinstance(payload, dict):
                continue

            ts_raw = payload.get("ts")
            ts = parse_ts(ts_raw) if isinstance(ts_raw, str) else None
            if ts is None:
                continue

            device_id = payload.get("device_id")
            seq = payload.get("seq")
            if not isinstance(device_id, str):
                # fallback: try extract from topic
                # e.g. waterplant/pump_in/telemetry
                parts = topic.split("/")
                device_id = parts[1] if len(parts) >= 2 else "unknown"

            rows.append({
                "ts": ts,
                "topic": topic,
                "device_id": device_id,
                "seq": seq,
                "payload": payload,
            })
    return rows


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def extract_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flatten a subset of metrics we care about.
    Returns dict of {metric_name: value}.
    """
    payload = row["payload"]
    dev = row["device_id"]

    out: Dict[str, Any] = {}

    # Stabilizer
    if dev == "stabilizer":
        out["stabilizer.vin_v"] = safe_get(payload, "stabilizer.vin_v")
        out["stabilizer.vout_v"] = safe_get(payload, "stabilizer.vout_v")
        out["stabilizer.active_power_w"] = safe_get(payload, "stabilizer.active_power_w")
        out["stabilizer.transformer_temp_c"] = safe_get(payload, "stabilizer.transformer_temp_c")

    # Pumps
    if dev in ("pump_in", "pump_out"):
        out[f"{dev}.rpm"] = safe_get(payload, "pump.rpm")
        out[f"{dev}.pressure_bar"] = safe_get(payload, "pump.pressure_bar")
        out[f"{dev}.flow_lpm"] = safe_get(payload, "pump.flow_lpm")
        out[f"{dev}.power_w"] = safe_get(payload, "pump.power_w")
        out[f"{dev}.temp_motor_c"] = safe_get(payload, "pump.temp_motor_c")
        out[f"{dev}.voltage_v"] = safe_get(payload, "pump.voltage_v")

    # Filter system
    if dev == "filter_system":
        out["filters.in_pressure_bar"] = safe_get(payload, "filters.in_pressure_bar")
        out["filters.out_pressure_bar"] = safe_get(payload, "filters.out_pressure_bar")
        out["filters.delta_pressure_bar"] = safe_get(payload, "filters.delta_pressure_bar")
        out["filters.wear_pct"] = safe_get(payload, "filters.wear_pct")
        out["filters.ntu"] = safe_get(payload, "filters.ntu")
        out["filters.ph"] = safe_get(payload, "filters.ph")
        out["filters.is_potable"] = safe_get(payload, "filters.is_potable")

    # Water storage
    if dev == "water_storage":
        out["storage.level_pct"] = safe_get(payload, "storage.level_pct")
        out["storage.in_flow_lpm"] = safe_get(payload, "storage.in_flow_lpm")
        out["storage.out_flow_lpm"] = safe_get(payload, "storage.out_flow_lpm")
        out["storage.level_rate"] = safe_get(payload, "storage.level_rate")
        out["storage.overflow"] = safe_get(payload, "storage.overflow")
        out["storage.level_sensors_state"] = safe_get(payload, "storage.level_sensors_state")

    # Security signals (available for all)
    out["security.failed_auth"] = safe_get(payload, "security.failed_auth")
    out["security.burst"] = safe_get(payload, "security.burst")

    # Optional: control.auth_ok / control.command (if present)
    ctrl = payload.get("control")
    if isinstance(ctrl, dict):
        out["control.auth_ok"] = ctrl.get("auth_ok")
        out["control.command"] = ctrl.get("command")
        out["control.target"] = ctrl.get("target")

    return out


def is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def plot_series(
    ts: List[datetime],
    ys: List[float],
    title: str,
    outpath: str,
) -> None:
    plt.figure()
    plt.plot(ts, ys)
    plt.title(title)
    plt.xlabel("time")
    plt.ylabel(title)
    plt.tight_layout()
    plt.savefig(outpath, dpi=150)
    plt.close()


def plot_step_series(
    ts: List[datetime],
    ys: List[int],
    title: str,
    outpath: str,
) -> None:
    plt.figure()
    plt.step(ts, ys, where="post")
    plt.title(title)
    plt.xlabel("time")
    plt.ylabel(title)
    plt.tight_layout()
    plt.savefig(outpath, dpi=150)
    plt.close()


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="out/gateway_traffic.jsonl", help="First JSONL path")
    ap.add_argument("--b", default="out/mqtt_viewer.jsonl", help="Second JSONL path (optional)")
    ap.add_argument("--outdir", default="out/plots", help="Where to save PNG plots")
    ap.add_argument("--max-points", type=int, default=5000, help="Cap points per metric (simple downsample)")
    args = ap.parse_args()

    rows_a = load_jsonl(args.a)
    rows_b = load_jsonl(args.b)
    rows = rows_a + rows_b
    rows.sort(key=lambda r: r["ts"])

    if not rows:
        print("No data found. Check JSONL paths.")
        return

    ensure_dir(args.outdir)

    # Build per-metric time-series
    series: Dict[str, List[Tuple[datetime, Any]]] = {}

    for r in rows:
        ts = r["ts"]
        metrics = extract_metrics(r)
        for k, v in metrics.items():
            if v is None:
                continue
            series.setdefault(k, []).append((ts, v))

    # Downsample helper
    def downsample(points: List[Tuple[datetime, Any]], max_points: int) -> List[Tuple[datetime, Any]]:
        if len(points) <= max_points:
            return points
        step = max(1, len(points) // max_points)
        return points[::step]

    # Plot numeric series; for booleans use step plot
    made = 0
    for metric, pts in series.items():
        pts = downsample(pts, args.max_points)
        ts_list = [p[0] for p in pts]
        vals = [p[1] for p in pts]

        outpath = os.path.join(args.outdir, metric.replace("/", "_").replace(" ", "_").replace(":", "_") + ".png")

        # boolean
        if all(isinstance(v, bool) for v in vals):
            ys = [1 if v else 0 for v in vals]
            plot_step_series(ts_list, ys, metric, outpath)
            made += 1
            continue

        # numeric
        if all(is_number(v) for v in vals):
            ys2 = [float(v) for v in vals]
            plot_series(ts_list, ys2, metric, outpath)
            made += 1
            continue

        # for string series (command/target/state) we skip plotting by default
        # (can be extended later)
        continue

    # Also create 2 quick combined plots (useful MVP view)
    # 1) pressures & dp
    def try_plot_combo(name: str, metrics_list: List[str], filename: str) -> None:
        data = []
        for m in metrics_list:
            pts = series.get(m)
            if not pts:
                continue
            pts = downsample(pts, args.max_points)
            data.append((m, [p[0] for p in pts], [p[1] for p in pts]))
        if not data:
            return

        plt.figure()
        for m, tss, vss in data:
            # numeric only
            if all(is_number(v) for v in vss):
                plt.plot(tss, [float(v) for v in vss], label=m)
        plt.title(name)
        plt.xlabel("time")
        plt.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(args.outdir, filename), dpi=150)
        plt.close()

    try_plot_combo(
        "Pressures and DP",
        ["filters.in_pressure_bar", "filters.out_pressure_bar", "filters.delta_pressure_bar"],
        "combo_pressures_dp.png",
    )

    try_plot_combo(
        "Flows and Tank Level",
        ["pump_in.flow_lpm", "pump_out.flow_lpm", "storage.level_pct"],
        "combo_flow_level.png",
    )

    print(f"Plots saved to: {os.path.abspath(args.outdir)} (generated {made} metric plots + combo plots)")


if __name__ == "__main__":
    main()
