#!/usr/bin/env python3
"""Convert ArduPilot DataFlash binary logs (.bin) into Contract v0 FSS binary envelopes (*.fss.bin).

Translates ATT, GPS, BAT, EKF, VIBE, ERR, EV, MSG, and MODE messages into canonical
Contract v0 Little-Endian envelopes matching fss_py.contract:
  magic u16 (0xF55A) | version u8 (1) | flags u8 (0) | source_id u32 |
  timestamp_ns u64 | event_type u32 | payload_len u32 | payload[payload_len]

Enforces ONE Unified Clock Base per .bin file (anchored to first valid GPS UTC timestamp)
and guarantees fault-preserving ingestion capping (100% of anomalies/errors preserved).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fss_py.model import EventRecord
from fss_py.wire import encode_envelope
from pymavlink import mavutil

EVENT_TYPE_LIDAR = 0x10
EVENT_TYPE_AVOIDANCE = 0x20
EVENT_TYPE_FORMATION = 0x21
EVENT_TYPE_DIAG = 0x30
EVENT_TYPE_ATT = 0x40
EVENT_TYPE_GPS = 0x41
EVENT_TYPE_BAT = 0x42
EVENT_TYPE_EKF = 0x43
EVENT_TYPE_VIBE = 0x44
EVENT_TYPE_ERR = 0x45

DEFAULT_BASE_EPOCH_NS = 1_718_000_000_000_000_000  # Fallback June 2024 reference epoch


def derive_boot_epoch_ns(in_path: Path) -> int:
    """Scan the log for the first message with valid GPS UTC time to anchor the unified boot clock."""
    mlog = mavutil.mavlink_connection(str(in_path))
    boot_epoch_ns = None

    while True:
        try:
            m = mlog.recv_msg()
        except Exception:
            continue
        if m is None:
            break

        t = getattr(m, "_timestamp", None)
        if t is not None and t > 1e8:  # Valid epoch second timestamp
            tus = getattr(m, "TimeUS", None)
            tms = getattr(m, "TimeMS", None)
            if tus is not None:
                boot_epoch_ns = int((t - (tus / 1e6)) * 1e9)
                break
            elif tms is not None:
                boot_epoch_ns = int((t - (tms / 1e3)) * 1e9)
                break
            else:
                boot_epoch_ns = int(t * 1e9)
                break

    return boot_epoch_ns if boot_epoch_ns is not None else DEFAULT_BASE_EPOCH_NS


def convert_log(in_path: Path, out_path: Path, agent_id: int = 1, max_records: int = 5000) -> dict:
    """Parse ArduPilot DataFlash log and write converted Contract v0 FSS binary envelopes."""
    boot_epoch_ns = derive_boot_epoch_ns(in_path)

    mlog = mavutil.mavlink_connection(str(in_path))

    critical_records: list[EventRecord] = []
    nominal_records: list[EventRecord] = []
    type_counts: dict[str, int] = {}
    last_valid_ts_ns = boot_epoch_ns

    while True:
        try:
            msg = mlog.recv_msg()
        except Exception:
            continue

        if msg is None:
            break

        mtype = msg.get_type()
        if mtype not in ("ATT", "GPS", "BAT", "CURR", "EKF1", "NKF1", "EKF", "VIBE", "ERR", "EV"):
            continue

        # Extract unified timestamp strictly anchored to boot_epoch_ns
        t = getattr(msg, "_timestamp", None)
        time_us = getattr(msg, "TimeUS", None)
        time_ms = getattr(msg, "TimeMS", None)

        if t is not None and t > 1e8:
            ts_ns = int(t * 1e9)
        elif time_us is not None:
            ts_ns = boot_epoch_ns + int(time_us * 1000)
        elif time_ms is not None:
            ts_ns = boot_epoch_ns + int(time_ms * 1_000_000)
        else:
            ts_ns = last_valid_ts_ns

        # Monotonic sanity guard: prevent decade jumps from corrupt/zero timestamps
        if last_valid_ts_ns and ts_ns < last_valid_ts_ns - 5_000_000_000:
            ts_ns = last_valid_ts_ns
        elif last_valid_ts_ns and ts_ns > last_valid_ts_ns + 3_600_000_000_000:
            ts_ns = last_valid_ts_ns + 1_000_000
        last_valid_ts_ns = ts_ns

        payload = None
        ev_type = None
        is_critical = False

        if mtype == "ATT":
            roll = float(getattr(msg, "Roll", 0.0))
            pitch = float(getattr(msg, "Pitch", 0.0))
            yaw = float(getattr(msg, "Yaw", 0.0))
            payload = struct.pack(">fff", roll, pitch, yaw)
            ev_type = EVENT_TYPE_ATT

        elif mtype == "GPS":
            status = int(getattr(msg, "Status", 0)) & 0xFF
            nsats = int(getattr(msg, "NSats", 0)) & 0xFF
            hdop = int(getattr(msg, "HDop", 100) * 100) & 0xFFFF
            lat = int(getattr(msg, "Lat", 0.0) * 1e7)
            lng = int(getattr(msg, "Lng", 0.0) * 1e7)
            alt = int(getattr(msg, "Alt", 0.0) * 100)
            payload = struct.pack(">BBHiii", status, nsats, hdop, lat, lng, alt)
            ev_type = EVENT_TYPE_GPS
            if status < 3 or nsats < 6 or hdop > 200:
                is_critical = True

        elif mtype in ("BAT", "CURR"):
            volt = float(getattr(msg, "Volt", getattr(msg, "V", 0.0)))
            curr = float(getattr(msg, "Curr", getattr(msg, "CurrTot", 0.0)))
            rem = float(getattr(msg, "EnrgTot", 100.0))
            payload = struct.pack(">fff", volt, curr, rem)
            ev_type = EVENT_TYPE_BAT

        elif mtype in ("EKF1", "NKF1", "EKF"):
            c_var = float(getattr(msg, "CC", getattr(msg, "VD", 0.0)))
            v_var = float(getattr(msg, "VN", 0.0))
            p_var = float(getattr(msg, "PD", 0.0))
            payload = struct.pack(">fff", c_var, v_var, p_var)
            ev_type = EVENT_TYPE_EKF
            if max(c_var, v_var, p_var) > 0.8:
                is_critical = True

        elif mtype == "VIBE":
            vx = float(getattr(msg, "VibeX", 0.0))
            vy = float(getattr(msg, "VibeY", 0.0))
            vz = float(getattr(msg, "VibeZ", 0.0))
            clip = int(getattr(msg, "Clip0", 0)) & 0xFFFF
            payload = struct.pack(">fffH", vx, vy, vz, clip)
            ev_type = EVENT_TYPE_VIBE
            if clip > 0 or max(vx, vy, vz) > 30.0:
                is_critical = True

        elif mtype == "ERR":
            subsys = int(getattr(msg, "Subsys", 0)) & 0xFFFF
            ecode = int(getattr(msg, "ECode", 0)) & 0xFFFF
            payload = struct.pack(">HH", subsys, ecode)
            ev_type = EVENT_TYPE_ERR
            is_critical = True

        elif mtype == "EV":
            ev_id = int(getattr(msg, "Id", 0)) & 0xFFFF
            payload = struct.pack(">HH", ev_id, 0)
            ev_type = EVENT_TYPE_ERR
            is_critical = True

        if payload and ev_type is not None:
            rec = EventRecord(source_id=agent_id, timestamp_ns=ts_ns, event_type=ev_type, payload_bytes=payload)
            if is_critical:
                critical_records.append(rec)
            else:
                nominal_records.append(rec)
            type_counts[mtype] = type_counts.get(mtype, 0) + 1

    # Fault-Preserving Downsampling: Retain 100% of critical events
    total_records = len(critical_records) + len(nominal_records)
    if total_records <= max_records:
        selected_records = critical_records + nominal_records
    else:
        nominal_budget = max(0, max_records - len(critical_records))
        if len(nominal_records) <= nominal_budget:
            selected_nominal = nominal_records
        else:
            step = (len(nominal_records) - 1) / max(1, (nominal_budget - 1))
            sampled_indices = {round(i * step) for i in range(nominal_budget)}
            selected_nominal = [nominal_records[i] for i in sorted(sampled_indices) if i < len(nominal_records)]
        selected_records = critical_records + selected_nominal

    # Sort strictly by timestamp_ns
    selected_records.sort(key=lambda r: r.timestamp_ns)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as out_f:
        for rec in selected_records:
            out_f.write(encode_envelope(rec))

    timestamps = [r.timestamp_ns for r in selected_records]
    min_ts = min(timestamps) if timestamps else 0
    max_ts = max(timestamps) if timestamps else 0

    return {
        "input_file": str(in_path),
        "output_file": str(out_path),
        "agent_id": agent_id,
        "total_envelopes": len(selected_records),
        "critical_envelopes": len(critical_records),
        "type_counts": type_counts,
        "time_start_ns": min_ts,
        "time_end_ns": max_ts,
        "duration_s": round((max_ts - min_ts) / 1e9, 2),
        "file_size_bytes": out_path.stat().st_size if out_path.exists() else 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Convert ArduPilot .bin logs to Contract v0 FSS .fss.bin files.")
    parser.add_argument("--input-dir", type=Path, default=Path("test_data/irl"))
    parser.add_argument("--agent-id", type=int, default=1)
    parser.add_argument("--max-records", type=int, default=5000)
    args = parser.parse_args()

    irl_dir = args.input_dir
    bin_files = sorted(irl_dir.glob("*.bin"))
    bin_files = [f for f in bin_files if not f.name.endswith(".fss.bin")]

    manifest = {}
    print(f"Found {len(bin_files)} ArduPilot logs in {irl_dir}")
    for bf in bin_files:
        out_f = bf.with_suffix(".fss.bin")
        print(f"Converting {bf.name} -> {out_f.name}...")
        meta = convert_log(bf, out_f, agent_id=args.agent_id, max_records=args.max_records)
        manifest[bf.name] = meta
        print(f"  -> Converted {meta['total_envelopes']} envelopes in {meta['duration_s']}s ({meta['file_size_bytes']} bytes)")

    manifest_path = irl_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest written to {manifest_path}")


if __name__ == "__main__":
    main()
