#!/usr/bin/env python3
"""Convert RflyMAD (multicopter 11-fault dataset) telemetry logs into Contract v0 FSS envelopes + ground truth sidecar.

Supports:
  - CSV files, ULog files (.ulg), or directories containing telemetry and fault metadata.
  - Parsing the 11 multicopter benchmark faults:
      1: Motor Loss of Effectiveness (LOE)
      2: Rotor Damage / Broken Propeller
      3: Motor Total Failure
      4: Accelerometer Bias / Drift
      5: Gyroscope Bias / Noise
      6: Compass / Magnetometer Failure
      7: Barometer Fault
      8: GPS Glitch / Position Jump
      9: Battery Voltage Drop
      10: Payload Shift / CoG Offset
      11: Communication Delay / Packet Loss
  - Mapping channels to schemas/drone_v0.yaml:
      - 0x40 ATTITUDE (>fff)
      - 0x41 GPS_STATUS (>BBHiii)
      - 0x42 BATTERY_STATUS (>fff)
      - 0x43 EKF_STATUS (>fff)
      - 0x44 VIBRATION (>fffH)
      - 0x45 ERROR_EVENT (>HH)
  - Emitting companion .ground_truth.json metadata sidecar.

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
from scripts.convert_ulog_to_fss import PurePythonULogReader, map_ulog_message_to_fss

# Event Type IDs matching schemas/drone_v0.yaml
EVENT_DIAGNOSTIC = 0x30
EVENT_ATTITUDE = 0x40
EVENT_GPS = 0x41
EVENT_BATTERY = 0x42
EVENT_EKF = 0x43
EVENT_VIBRATION = 0x44
EVENT_ERROR = 0x45

BASE_EPOCH_NS = 1_718_000_000_000_000_000

RFLYMAD_11_FAULTS: dict[int, dict[str, str]] = {
    1: {"name": "Motor Loss of Effectiveness (LOE)", "category": "actuator", "description": "Motor thrust efficiency drop"},
    2: {"name": "Rotor Damage", "category": "airframe", "description": "Broken or chipped propeller blade"},
    3: {"name": "Motor Total Failure", "category": "actuator", "description": "Complete motor stoppage / flameout"},
    4: {"name": "Accelerometer Bias", "category": "sensor", "description": "IMU accelerometer measurement drift"},
    5: {"name": "Gyroscope Noise", "category": "sensor", "description": "IMU gyro noise / bias instability"},
    6: {"name": "Compass Failure", "category": "sensor", "description": "Magnetic distortion / compass heading divergence"},
    7: {"name": "Barometer Fault", "category": "sensor", "description": "Barometric pressure altitude drift"},
    8: {"name": "GPS Position Glitch", "category": "sensor", "description": "Satellite loss / spoofed position jump"},
    9: {"name": "Battery Voltage Drop", "category": "power", "description": "Sudden cell voltage collapse / high ESR"},
    10: {"name": "Payload Shift", "category": "dynamics", "description": "Center of Gravity shift during flight"},
    11: {"name": "Communication Delay", "category": "network", "description": "Telemetry drop / packet latency"},
}


def normalize_timestamp_ns(raw_val: Any, base_epoch_ns: int = BASE_EPOCH_NS) -> int:
    """Convert timestamp into unsigned 64-bit nanoseconds."""
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
        return base_epoch_ns + int(val * 1_000_000_000)
    return base_epoch_ns


def format_iso(ts_ns: int) -> str:
    """Format nanoseconds as ISO-8601 UTC string."""
    sec = ts_ns // 1_000_000_000
    nsec = ts_ns % 1_000_000_000
    try:
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{nsec:09d}Z"
    except Exception:
        return f"{ts_ns} ns"


def find_column(headers: list[str], candidates: list[str]) -> str | None:
    h_lower = {h.strip().lower(): h for h in headers}
    for c in candidates:
        if c.lower() in h_lower:
            return h_lower[c.lower()]
    return None


def resolve_fault_id(val_str: str) -> int | None:
    """Resolve fault ID 1..11 from string or number."""
    val_clean = val_str.strip().lower()
    if val_clean in ("0", "nominal", "none", "normal", "ok", "", "false"):
        return None
    try:
        fid = int(val_clean)
        if 1 <= fid <= 11:
            return fid
    except ValueError:
        pass

    for fid, meta in RFLYMAD_11_FAULTS.items():
        fname = meta["name"].lower()
        if fname in val_clean or val_clean in fname:
            return fid
        for token in meta["name"].lower().split():
            if len(token) > 3 and token in val_clean:
                return fid
    return 1  # Generic fault fallback


def parse_rflymad_csv(
    csv_path: Path,
    source_id: int = 1,
    max_records: int | None = None,
) -> tuple[list[EventRecord], list[dict[str, Any]], dict[str, int]]:
    """Parse RflyMAD CSV file into EventRecords and fault metadata."""
    records: list[EventRecord] = []
    failures: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}

    with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return records, failures, type_counts

        fieldnames = [fn.strip() for fn in reader.fieldnames]

        ts_col = find_column(fieldnames, ["timestamp", "time", "timeus", "timestamp_ns", "t", "time_boot_ms"])
        roll_col = find_column(fieldnames, ["roll", "roll_deg", "att_roll"])
        pitch_col = find_column(fieldnames, ["pitch", "pitch_deg", "att_pitch"])
        yaw_col = find_column(fieldnames, ["yaw", "yaw_deg", "att_yaw"])

        lat_col = find_column(fieldnames, ["lat", "latitude", "gps_lat"])
        lon_col = find_column(fieldnames, ["lon", "lng", "longitude", "gps_lon"])
        alt_col = find_column(fieldnames, ["alt", "altitude", "gps_alt"])

        volt_col = find_column(fieldnames, ["voltage", "voltage_v", "bat_volt", "v"])
        curr_col = find_column(fieldnames, ["current", "current_a", "bat_curr", "i"])
        rem_col = find_column(fieldnames, ["remaining", "remaining_pct", "bat_rem"])

        vibe_x_col = find_column(fieldnames, ["vibe_x", "acc_x", "vibex"])
        vibe_y_col = find_column(fieldnames, ["vibe_y", "acc_y", "vibey"])
        vibe_z_col = find_column(fieldnames, ["vibe_z", "acc_z", "vibez"])

        fault_col = find_column(
            fieldnames,
            ["fault_type", "fault_id", "fault_mode", "fault_injection", "label", "failure", "is_fault"],
        )

        active_fault: dict[str, Any] | None = None
        current_fault_id: int | None = None

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

                    if any(abs(v) > 0.0001 for v in (r, p, y)):
                        if all(abs(v) <= 2 * math.pi + 0.1 for v in (r, p, y)):
                            r, p, y = math.degrees(r), math.degrees(pitch), math.degrees(yaw)

                    payload = struct.pack(">fff", r, p, y)
                    records.append(EventRecord(source_id, ts_ns, EVENT_ATTITUDE, payload))
                    type_counts["0x40"] = type_counts.get("0x40", 0) + 1
                except Exception:
                    pass

            # 2. GPS_STATUS (0x41)
            if lat_col and lon_col:
                try:
                    lat_f = float(row[lat_col])
                    lon_f = float(row[lon_col])
                    alt_f = float(row[alt_col]) if alt_col and row.get(alt_col) else 10.0
                    lat_e7 = int(lat_f) if abs(lat_f) > 1000 else int(lat_f * 1e7)
                    lng_e7 = int(lon_f) if abs(lon_f) > 1000 else int(lon_f * 1e7)
                    alt_cm = int(alt_f) if abs(alt_f) > 10000 else int(alt_f * 100)

                    payload = struct.pack(">BBHiii", 3, 14, 100, lat_e7, lng_e7, alt_cm)
                    records.append(EventRecord(source_id, ts_ns + 1_000_000, EVENT_GPS, payload))
                    type_counts["0x41"] = type_counts.get("0x41", 0) + 1
                except Exception:
                    pass

            # 3. BATTERY_STATUS (0x42)
            if volt_col:
                try:
                    v = float(row[volt_col]) if row.get(volt_col) else 16.0
                    c = float(row[curr_col]) if curr_col and row.get(curr_col) else 12.0
                    rem = float(row[rem_col]) if rem_col and row.get(rem_col) else 80.0
                    if rem <= 1.0 and rem >= 0.0:
                        rem *= 100.0
                    payload = struct.pack(">fff", v, c, rem)
                    records.append(EventRecord(source_id, ts_ns + 2_000_000, EVENT_BATTERY, payload))
                    type_counts["0x42"] = type_counts.get("0x42", 0) + 1
                except Exception:
                    pass

            # 4. VIBRATION (0x44)
            if vibe_x_col and vibe_y_col and vibe_z_col:
                try:
                    vx = float(row[vibe_x_col])
                    vy = float(row[vibe_y_col])
                    vz = float(row[vibe_z_col])
                    clip = 0
                    payload = struct.pack(">fffH", vx, vy, vz, clip)
                    records.append(EventRecord(source_id, ts_ns + 3_000_000, EVENT_VIBRATION, payload))
                    type_counts["0x44"] = type_counts.get("0x44", 0) + 1
                except Exception:
                    pass

            # 5. FAULT EVENT (0x45 ERROR_EVENT)
            if fault_col:
                val_raw = str(row.get(fault_col, ""))
                fid = resolve_fault_id(val_raw)
                if fid is not None:
                    meta = RFLYMAD_11_FAULTS.get(fid, {"name": f"Fault_{fid}", "description": ""})
                    payload = struct.pack(">HH", fid, fid * 10)
                    records.append(EventRecord(source_id, ts_ns + 4_000_000, EVENT_ERROR, payload))
                    type_counts["0x45"] = type_counts.get("0x45", 0) + 1

                    if current_fault_id != fid:
                        if active_fault is not None:
                            active_fault["end_time_ns"] = ts_ns
                            active_fault["end_iso"] = format_iso(ts_ns)
                            failures.append(active_fault)
                        current_fault_id = fid
                        active_fault = {
                            "fault_id": fid,
                            "fault_name": meta["name"],
                            "description": meta["description"],
                            "subsystem_code": fid,
                            "error_code": fid * 10,
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
                        current_fault_id = None

            row_idx += 1

        if active_fault is not None:
            failures.append(active_fault)

    return records, failures, type_counts


def parse_rflymad_ulog(
    ulg_path: Path,
    source_id: int = 1,
    max_records: int | None = None,
) -> tuple[list[EventRecord], list[dict[str, Any]], dict[str, int]]:
    """Parse RflyMAD ULog file using pure-Python reader."""
    records: list[EventRecord] = []
    failures: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}

    with open(ulg_path, "rb") as f:
        reader = PurePythonULogReader(f)
        for topic, multi_id, fields in reader.read_messages():
            if max_records and len(records) >= max_records:
                break
            res = map_ulog_message_to_fss(topic, fields)
            if res is not None:
                ts_ns, etype, payload = res
                rec = EventRecord(source_id, ts_ns, etype, payload)
                records.append(rec)
                tname = f"0x{etype:02X}"
                type_counts[tname] = type_counts.get(tname, 0) + 1

    return records, failures, type_counts


def convert_rflymad_to_fss(
    input_path: Path,
    output_path: Path,
    meta_out_path: Path | None = None,
    source_id: int = 1,
    max_records: int | None = None,
) -> dict[str, Any]:
    """Convert RflyMAD file or directory to FSS binary envelopes and companion ground truth metadata."""
    all_records: list[EventRecord] = []
    all_failures: list[dict[str, Any]] = []
    combined_type_counts: dict[str, int] = {}

    files_to_process: list[Path] = []
    if input_path.is_file():
        files_to_process.append(input_path)
    elif input_path.is_dir():
        files_to_process.extend(sorted(input_path.glob("*.csv")))
        files_to_process.extend(sorted(input_path.glob("*.ulg")))
        if not files_to_process:
            files_to_process.extend(sorted(input_path.rglob("*.csv")))
            files_to_process.extend(sorted(input_path.rglob("*.ulg")))

    if not files_to_process:
        raise ValueError(f"No suitable telemetry files (.csv, .ulg) found in '{input_path}'")

    for fpath in files_to_process:
        if max_records and len(all_records) >= max_records:
            break
        limit = (max_records - len(all_records)) if max_records else None
        if fpath.suffix.lower() == ".ulg":
            recs, fails, tcounts = parse_rflymad_ulog(fpath, source_id=source_id, max_records=limit)
        else:
            recs, fails, tcounts = parse_rflymad_csv(fpath, source_id=source_id, max_records=limit)
        all_records.extend(recs)
        all_failures.extend(fails)
        for k, v in tcounts.items():
            combined_type_counts[k] = combined_type_counts.get(k, 0) + v

    # Sort strictly by timestamp_ns
    all_records.sort(key=lambda r: r.timestamp_ns)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    envelopes_bytes = bytearray()
    for rec in all_records:
        envelopes_bytes.extend(encode_envelope(rec))
    output_path.write_bytes(bytes(envelopes_bytes))

    start_ns = all_records[0].timestamp_ns if all_records else 0
    end_ns = all_records[-1].timestamp_ns if all_records else 0

    ground_truth = {
        "dataset": "RflyMAD",
        "source_path": str(input_path),
        "source_id": source_id,
        "total_events": len(all_records),
        "time_start_ns": start_ns,
        "time_end_ns": end_ns,
        "duration_ns": end_ns - start_ns if end_ns >= start_ns else 0,
        "time_start_iso": format_iso(start_ns),
        "time_end_iso": format_iso(end_ns),
        "faults_catalog": {str(k): v["name"] for k, v in RFLYMAD_11_FAULTS.items()},
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
    parser = argparse.ArgumentParser(description="Convert RflyMAD multicopter 11-fault dataset to FSS binary format + ground truth sidecar")
    parser.add_argument("input", type=Path, help="Path to RflyMAD CSV / ULog file or directory")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Path to output .fss.bin file")
    parser.add_argument("--meta-out", type=Path, default=None, help="Path to companion .ground_truth.json (optional)")
    parser.add_argument("--source-id", type=int, default=1, help="Producer / Agent Source ID (default: 1)")
    parser.add_argument("--max-records", type=int, default=None, help="Maximum number of records to convert")

    args = parser.parse_args()

    if not args.input.exists():
        sys.stderr.write(f"Error: Input path '{args.input}' not found.\n")
        sys.exit(1)

    result = convert_rflymad_to_fss(
        args.input,
        args.output,
        meta_out_path=args.meta_out,
        source_id=args.source_id,
        max_records=args.max_records,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
