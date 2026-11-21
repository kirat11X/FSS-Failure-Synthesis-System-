#!/usr/bin/env python3
"""Convert ALFA (Air Lab Failure and Anomaly) fixed-wing dataset logs into Contract v0 FSS envelopes + ground truth sidecar.

Supports:
  - Single CSV files or directories containing ALFA topic exports / flight logs.
  - Normalizing timestamps to unsigned 64-bit nanoseconds (uint64 ns).
  - Mapping attitude columns (roll, pitch, yaw) -> 0x40 ATTITUDE (>fff).
  - Mapping battery columns (voltage, current, remaining) -> 0x42 BATTERY_STATUS (>fff).
  - Mapping actuator / motor diagnostics -> 0x30 DIAGNOSTIC (<Hf).
  - Detecting failure modes/labels -> 0x45 ERROR_EVENT (>HH) + companion .ground_truth.json metadata.

Emits valid Contract v0 Little-Endian envelopes:
  magic u16 (0xF55A) | version u8 (1) | flags u8 (0) | source_id u32 |
  timestamp_ns u64 | event_type u32 | payload_len u32 | payload[payload_len]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is importable for fss_py
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fss_py.contract import MAGIC, VERSION
from fss_py.model import EventRecord
from fss_py.wire import encode_envelope

# Event Type IDs matching schemas/drone_v0.yaml
EVENT_DIAGNOSTIC = 0x30
EVENT_ATTITUDE = 0x40
EVENT_BATTERY = 0x42
EVENT_ERROR = 0x45

BASE_EPOCH_NS = 1_718_000_000_000_000_000

# Subsystem codes for ALFA failure modes
SUBSYSTEM_MAP = {
    "motor": 1,
    "engine": 1,
    "prop": 1,
    "thrust": 1,
    "aileron": 2,
    "elevator": 3,
    "rudder": 4,
    "flap": 5,
    "actuator": 6,
    "sensor": 7,
    "generic": 9,
}


def normalize_timestamp_ns(raw_val: Any, base_epoch_ns: int = BASE_EPOCH_NS) -> int:
    """Normalize timestamp from seconds, milliseconds, microseconds, or nanoseconds into uint64 ns."""
    if isinstance(raw_val, str):
        raw_val = raw_val.strip()
        if not raw_val:
            return base_epoch_ns
        try:
            val = float(raw_val)
        except ValueError:
            return base_epoch_ns
    else:
        val = float(raw_val)

    if val >= 1e18:
        return int(val)
    elif val >= 1e15:
        return int(val * 1_000)
    elif val >= 1e12:
        return int(val * 1_000_000)
    elif val >= 1e9:
        return int(val * 1_000_000_000)
    elif val >= 0:
        # Relative flight time in seconds since boot
        return base_epoch_ns + int(val * 1_000_000_000)
    return base_epoch_ns


def format_iso(ts_ns: int) -> str:
    """Format nanosecond timestamp as ISO-8601 UTC string."""
    sec = ts_ns // 1_000_000_000
    nsec = ts_ns % 1_000_000_000
    try:
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{nsec:09d}Z"
    except Exception:
        return f"{ts_ns} ns"


def find_column(headers: list[str], candidates: list[str]) -> str | None:
    """Find the first matching header column name (case-insensitive)."""
    h_lower = {h.strip().lower(): h for h in headers}
    for c in candidates:
        if c.lower() in h_lower:
            return h_lower[c.lower()]
    return None


def parse_alfa_csv(
    csv_path: Path,
    source_id: int = 1,
    max_records: int | None = None,
) -> tuple[list[EventRecord], list[dict[str, Any]], dict[str, int]]:
    """Parse an ALFA CSV file into EventRecords, failure intervals, and type counts."""
    records: list[EventRecord] = []
    failures: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}

    with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return records, failures, type_counts

        fieldnames = [fn.strip() for fn in reader.fieldnames]

        ts_col = find_column(
            fieldnames,
            ["timestamp", "time", "%time", "time_boot_ms", "timeus", "timestamp_ns", "t", "timestamp_sec"],
        )
        roll_col = find_column(fieldnames, ["roll", "roll_deg", "attitude.roll", "phi"])
        pitch_col = find_column(fieldnames, ["pitch", "pitch_deg", "attitude.pitch", "theta"])
        yaw_col = find_column(fieldnames, ["yaw", "yaw_deg", "attitude.yaw", "psi"])

        volt_col = find_column(fieldnames, ["voltage", "voltage_v", "battery.voltage", "batt_volt", "v"])
        curr_col = find_column(fieldnames, ["current", "current_a", "battery.current", "batt_curr", "i"])
        rem_col = find_column(fieldnames, ["remaining", "remaining_pct", "battery.remaining", "batt_rem", "soc"])

        thrust_col = find_column(fieldnames, ["thrust", "motor_thrust", "throttle", "motor_cmd"])
        elev_col = find_column(fieldnames, ["elevator", "elevator_cmd"])
        ail_col = find_column(fieldnames, ["aileron", "aileron_cmd"])
        rud_col = find_column(fieldnames, ["rudder", "rudder_cmd"])

        fault_col = find_column(
            fieldnames,
            ["failure_mode", "failure", "fault", "fault_type", "label", "is_failure", "anomaly", "failure_status"],
        )

        active_fault: dict[str, Any] | None = None
        current_fault_name: str | None = None

        row_idx = 0
        for row in reader:
            if max_records and len(records) >= max_records:
                break

            ts_raw = row.get(ts_col, row_idx * 0.02) if ts_col else row_idx * 0.02
            ts_ns = normalize_timestamp_ns(ts_raw)

            # 1. ATTITUDE (0x40)
            if roll_col or pitch_col or yaw_col:
                try:
                    r = float(row[roll_col]) if roll_col and row.get(roll_col) else 0.0
                    p = float(row[pitch_col]) if pitch_col and row.get(pitch_col) else 0.0
                    y = float(row[yaw_col]) if yaw_col and row.get(yaw_col) else 0.0

                    # Convert radians to degrees if values are within [-2pi, 2pi]
                    if any(abs(v) > 0.0001 for v in (r, p, y)):
                        if all(abs(v) <= 2 * math.pi + 0.1 for v in (r, p, y)):
                            r, p, y = math.degrees(r), math.degrees(p), math.degrees(y)

                    payload = struct.pack(">fff", r, p, y)
                    records.append(EventRecord(source_id, ts_ns, EVENT_ATTITUDE, payload))
                    type_counts["0x40"] = type_counts.get("0x40", 0) + 1
                except Exception:
                    pass

            # 2. BATTERY_STATUS (0x42)
            if volt_col:
                try:
                    v = float(row[volt_col]) if row.get(volt_col) else 16.0
                    c = float(row[curr_col]) if curr_col and row.get(curr_col) else 10.0
                    rem = float(row[rem_col]) if rem_col and row.get(rem_col) else 85.0
                    if rem <= 1.0 and rem >= 0.0:
                        rem *= 100.0
                    payload = struct.pack(">fff", v, c, rem)
                    records.append(EventRecord(source_id, ts_ns + 1_000_000, EVENT_BATTERY, payload))
                    type_counts["0x42"] = type_counts.get("0x42", 0) + 1
                except Exception:
                    pass

            # 3. DIAGNOSTIC / ACTUATORS (0x30)
            if thrust_col or elev_col or ail_col:
                try:
                    th = float(row[thrust_col]) if thrust_col and row.get(thrust_col) else 0.5
                    # Map thrust into temp/load f32
                    status_code = 0
                    payload = struct.pack("<Hf", status_code, th * 100.0)
                    records.append(EventRecord(source_id, ts_ns + 2_000_000, EVENT_DIAGNOSTIC, payload))
                    type_counts["0x30"] = type_counts.get("0x30", 0) + 1
                except Exception:
                    pass

            # 4. FAILURE DETECTION (0x45 ERROR_EVENT)
            if fault_col:
                val_str = str(row.get(fault_col, "")).strip().lower()
                is_fault = val_str not in ("0", "nominal", "none", "normal", "ok", "", "false")
                if is_fault:
                    subsystem_id = 9
                    for k, sid in SUBSYSTEM_MAP.items():
                        if k in val_str:
                            subsystem_id = sid
                            break

                    try:
                        err_code = int(val_str)
                    except ValueError:
                        err_code = 100 + subsystem_id

                    payload = struct.pack(">HH", subsystem_id, err_code)
                    records.append(EventRecord(source_id, ts_ns + 3_000_000, EVENT_ERROR, payload))
                    type_counts["0x45"] = type_counts.get("0x45", 0) + 1

                    if current_fault_name != val_str:
                        if active_fault is not None:
                            active_fault["end_time_ns"] = ts_ns
                            active_fault["end_iso"] = format_iso(ts_ns)
                            failures.append(active_fault)
                        current_fault_name = val_str
                        active_fault = {
                            "failure_type": val_str,
                            "subsystem_id": subsystem_id,
                            "error_code": err_code,
                            "start_time_ns": ts_ns,
                            "start_iso": format_iso(ts_ns),
                            "end_time_ns": ts_ns,
                            "end_iso": format_iso(ts_ns),
                        }
                    else:
                        if active_fault is not None:
                            active_fault["end_time_ns"] = ts_ns
                            active_fault["end_iso"] = format_iso(ts_ns)
                else:
                    if active_fault is not None:
                        failures.append(active_fault)
                        active_fault = None
                        current_fault_name = None

            row_idx += 1

        if active_fault is not None:
            failures.append(active_fault)

    return records, failures, type_counts


def convert_alfa_to_fss(
    input_path: Path,
    output_path: Path,
    meta_out_path: Path | None = None,
    source_id: int = 1,
    max_records: int | None = None,
) -> dict[str, Any]:
    """Convert ALFA CSV or directory into FSS binary envelopes and companion ground truth metadata."""
    csv_files: list[Path] = []
    if input_path.is_file():
        csv_files.append(input_path)
    elif input_path.is_dir():
        csv_files.extend(sorted(input_path.glob("*.csv")))
        if not csv_files:
            csv_files.extend(sorted(input_path.rglob("*.csv")))

    if not csv_files:
        raise ValueError(f"No CSV files found in '{input_path}'")

    all_records: list[EventRecord] = []
    all_failures: list[dict[str, Any]] = []
    combined_type_counts: dict[str, int] = {}

    for cpath in csv_files:
        if max_records and len(all_records) >= max_records:
            break
        limit = (max_records - len(all_records)) if max_records else None
        recs, fails, tcounts = parse_alfa_csv(cpath, source_id=source_id, max_records=limit)
        all_records.extend(recs)
        all_failures.extend(fails)
        for k, v in tcounts.items():
            combined_type_counts[k] = combined_type_counts.get(k, 0) + v

    # Sort records strictly by timestamp_ns
    all_records.sort(key=lambda r: r.timestamp_ns)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    envelopes_bytes = bytearray()
    for rec in all_records:
        envelopes_bytes.extend(encode_envelope(rec))
    output_path.write_bytes(bytes(envelopes_bytes))

    start_ns = all_records[0].timestamp_ns if all_records else 0
    end_ns = all_records[-1].timestamp_ns if all_records else 0

    ground_truth = {
        "dataset": "ALFA",
        "source_path": str(input_path),
        "source_id": source_id,
        "total_events": len(all_records),
        "time_start_ns": start_ns,
        "time_end_ns": end_ns,
        "duration_ns": end_ns - start_ns if end_ns >= start_ns else 0,
        "time_start_iso": format_iso(start_ns),
        "time_end_iso": format_iso(end_ns),
        "failures": all_failures,
        "type_counts": combined_type_counts,
    }

    if meta_out_path is None:
        meta_out_path = output_path.with_name(f"{output_path.stem}.ground_truth.json")
    meta_out_path.parent.mkdir(parents=True, exist_ok=True)
    meta_out_path.write_text(json.dumps(ground_truth, indent=2), encoding="utf-8")

    return {
        "input": str(input_path),
        "output": str(output_path),
        "meta_output": str(meta_out_path),
        "records_written": len(all_records),
        "type_counts": combined_type_counts,
        "failures_detected": len(all_failures),
        "time_start_ns": start_ns,
        "time_end_ns": end_ns,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert ALFA fixed-wing dataset CSVs to FSS binary format + ground truth sidecar")
    parser.add_argument("input", type=Path, help="Path to ALFA CSV file or directory of CSVs")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Path to output .fss.bin file")
    parser.add_argument("--meta-out", type=Path, default=None, help="Path to companion .ground_truth.json (optional)")
    parser.add_argument("--source-id", type=int, default=1, help="Producer / Agent Source ID (default: 1)")
    parser.add_argument("--max-records", type=int, default=None, help="Maximum number of records to convert")

    args = parser.parse_args()

    if not args.input.exists():
        sys.stderr.write(f"Error: Input path '{args.input}' not found.\n")
        sys.exit(1)

    result = convert_alfa_to_fss(
        args.input,
        args.output,
        meta_out_path=args.meta_out,
        source_id=args.source_id,
        max_records=args.max_records,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
