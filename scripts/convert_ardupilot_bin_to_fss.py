#!/usr/bin/env python3
"""Convert ArduPilot DataFlash binary logs (.bin) into FSS binary envelopes (*.fss.bin).

Translates ATT, GPS, BAT, EKF, VIBE, and ERR messages into native Big-Endian
envelopes matching fss-core/envelope.cfg:
  - Agent ID: 4 bytes UINT32
  - Timestamp: 8 bytes INT64 (milliseconds)
  - Payload Length: 4 bytes UINT32
  - Event Type: 4 bytes UINT32
  - Payload: BYTES
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from pathlib import Path

from pymavlink import mavutil


EVENT_TYPE_ATT = 0x40
EVENT_TYPE_GPS = 0x41
EVENT_TYPE_BAT = 0x42
EVENT_TYPE_EKF = 0x43
EVENT_TYPE_VIBE = 0x44
EVENT_TYPE_ERR = 0x45

HEADER_STRUCT = struct.Struct(">IQII")


def pack_fss_envelope(agent_id: int, timestamp_ms: int, event_type: int, payload: bytes) -> bytes:
    """Pack an envelope matching envelope.cfg: 0-3 Agent ID, 4-11 Timestamp, 12-15 Len, 16-19 Type."""
    header = HEADER_STRUCT.pack(agent_id & 0xFFFFFFFF, int(timestamp_ms), len(payload), event_type)
    return header + payload


def convert_log(in_path: Path, out_path: Path, agent_id: int = 1, max_records: int = 5000) -> dict:
    """Parse ArduPilot DataFlash log and write converted FSS binary envelopes."""
    mlog = mavutil.mavlink_connection(str(in_path))

    records_written = 0
    type_counts: dict[str, int] = {}
    base_epoch_ms = 1718000000000  # realistic reference base epoch

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as out_f:
        while records_written < max_records:
            try:
                msg = mlog.recv_msg()
            except Exception:
                continue

            if msg is None:
                break

            mtype = msg.get_type()
            # Extract time in milliseconds
            time_us = getattr(msg, "TimeUS", None)
            if time_us is not None:
                ts_ms = base_epoch_ms + int(time_us // 1000)
            elif hasattr(msg, "TimeMS"):
                ts_ms = base_epoch_ms + int(msg.TimeMS)
            elif getattr(msg, "_timestamp", None) is not None:
                ts_ms = int(msg._timestamp * 1000)
            else:
                continue

            payload = None
            ev_type = None

            if mtype == "ATT":
                # roll, pitch, yaw (float32 each -> 12 bytes)
                roll = float(getattr(msg, "Roll", 0.0))
                pitch = float(getattr(msg, "Pitch", 0.0))
                yaw = float(getattr(msg, "Yaw", 0.0))
                payload = struct.pack(">fff", roll, pitch, yaw)
                ev_type = EVENT_TYPE_ATT

            elif mtype == "GPS":
                # status (u8), nsats (u8), hdop (u16), lat (i32), lng (i32), alt (i32) -> 16 bytes
                status = int(getattr(msg, "Status", 0)) & 0xFF
                nsats = int(getattr(msg, "NSats", 0)) & 0xFF
                hdop = int(getattr(msg, "HDop", 100) * 100) & 0xFFFF
                lat = int(getattr(msg, "Lat", 0.0) * 1e7)
                lng = int(getattr(msg, "Lng", 0.0) * 1e7)
                alt = int(getattr(msg, "Alt", 0.0) * 100)
                payload = struct.pack(">BBHiii", status, nsats, hdop, lat, lng, alt)
                ev_type = EVENT_TYPE_GPS

            elif mtype in ("BAT", "CURR"):
                # volt (f32), curr (f32), remaining (f32) -> 12 bytes
                volt = float(getattr(msg, "Volt", getattr(msg, "V", 0.0)))
                curr = float(getattr(msg, "Curr", getattr(msg, "CurrTot", 0.0)))
                rem = float(getattr(msg, "EnrgTot", 100.0))
                payload = struct.pack(">fff", volt, curr, rem)
                ev_type = EVENT_TYPE_BAT

            elif mtype in ("EKF1", "NKF1", "EKF"):
                # comp_var (f32), vel_var (f32), pos_var (f32) -> 12 bytes
                c_var = float(getattr(msg, "CC", getattr(msg, "VD", 0.0)))
                v_var = float(getattr(msg, "VN", 0.0))
                p_var = float(getattr(msg, "PD", 0.0))
                payload = struct.pack(">fff", c_var, v_var, p_var)
                ev_type = EVENT_TYPE_EKF

            elif mtype == "VIBE":
                # vibe_x (f32), vibe_y (f32), vibe_z (f32), clipping (u16) -> 14 bytes
                vx = float(getattr(msg, "VibeX", 0.0))
                vy = float(getattr(msg, "VibeY", 0.0))
                vz = float(getattr(msg, "VibeZ", 0.0))
                clip = int(getattr(msg, "Clip0", 0)) & 0xFFFF
                payload = struct.pack(">fffH", vx, vy, vz, clip)
                ev_type = EVENT_TYPE_VIBE

            elif mtype in ("ERR", "EV"):
                subsys = int(getattr(msg, "Subsys", getattr(msg, "Id", 0))) & 0xFFFF
                ecode = int(getattr(msg, "ECode", 0)) & 0xFFFF
                payload = struct.pack(">HH", subsys, ecode)
                ev_type = EVENT_TYPE_ERR

            if payload and ev_type is not None:
                packet = pack_fss_envelope(agent_id, ts_ms, ev_type, payload)
                out_f.write(packet)
                records_written += 1
                type_counts[mtype] = type_counts.get(mtype, 0) + 1

    return {
        "input_file": str(in_path),
        "output_file": str(out_path),
        "agent_id": agent_id,
        "total_envelopes": records_written,
        "type_counts": type_counts,
        "file_size_bytes": out_path.stat().st_size if out_path.exists() else 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Convert ArduPilot .bin logs to FSS .fss.bin files.")
    parser.add_argument("--input-dir", type=Path, default=Path("test_data/irl"))
    parser.add_argument("--agent-id", type=int, default=1)
    args = parser.parse_args()

    irl_dir = args.input_dir
    bin_files = sorted(irl_dir.glob("*.bin"))
    bin_files = [f for f in bin_files if not f.name.endswith(".fss.bin")]

    manifest = {}
    print(f"Found {len(bin_files)} ArduPilot logs in {irl_dir}")
    for bf in bin_files:
        out_f = bf.with_suffix(".fss.bin")
        print(f"Converting {bf.name} -> {out_f.name}...")
        meta = convert_log(bf, out_f, agent_id=args.agent_id)
        manifest[bf.name] = meta
        print(f"  -> Converted {meta['total_envelopes']} envelopes ({meta['file_size_bytes']} bytes)")

    manifest_path = irl_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest written to {manifest_path}")


if __name__ == "__main__":
    main()
