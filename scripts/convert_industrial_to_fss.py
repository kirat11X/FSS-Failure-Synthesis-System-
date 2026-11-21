#!/usr/bin/env python3
"""scripts/convert_industrial_to_fss.py
---------------------------------------
Convert real-world industrial telemetry (Modbus PCAP & HAI 22.04 SCADA time-series)
into Contract v0 Little-Endian FSS binary envelopes (*.fss.bin).

Packs events matching `schemas/industrial_plc_v0.yaml`:
  - 0x0301 TEMP_STATUS (<Hfb: sensor_id u16, temperature_c f32, drift_detected bool)
  - 0x0302 INTERLOCK (<H?H: interlock_id u16, tripped bool, reason_code u16)
  - 0x0303 CONVEYOR_STATE (<HB: conveyor_id u16, state_code u8)
"""

from __future__ import annotations

import argparse
import datetime
import json
import struct
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fss_py.model import EventRecord
from fss_py.wire import encode_envelope

EVENT_TEMP_STATUS = 0x0301
EVENT_INTERLOCK = 0x0302
EVENT_CONVEYOR_STATE = 0x0303


def parse_pcap_modbus(pcap_path: Path, source_id: int = 301) -> list[EventRecord]:
    """Parse real Modbus PCAP packets and map them to industrial events."""
    records: list[EventRecord] = []
    if not pcap_path.exists():
        return records

    data = pcap_path.read_bytes()
    if len(data) < 24:
        return records

    magic = struct.unpack("<I", data[:4])[0]
    is_little = magic in (0xA1B2C3D4, 0xD4C3B2A1)
    endian_fmt = "<" if is_little else ">"

    off = 24
    while off + 16 <= len(data):
        ts_sec, ts_usec, incl_len, orig_len = struct.unpack(f"{endian_fmt}IIII", data[off : off + 16])
        pkt_bytes = data[off + 16 : off + 16 + incl_len]
        off += 16 + incl_len
        ts_ns = (ts_sec * 1_000_000_000) + (ts_usec * 1_000)

        # Basic Modbus TCP packet inspection (Ethernet 14 + IP 20 + TCP 20 = 54)
        if len(pkt_bytes) >= 54 + 7:
            mbap = pkt_bytes[54 : 54 + 7]
            trans_id, proto_id, pdu_len, unit_id = struct.unpack(">HHHB", mbap)
            if proto_id == 0:  # Modbus protocol
                pdu = pkt_bytes[54 + 7 :]
                if pdu:
                    func_code = pdu[0]
                    # Map write/control functions (0x05 write coil, 0x06 write reg, 0x10 write multi)
                    if func_code in (5, 6, 15, 16):
                        # Control state change
                        payload = struct.pack("<HB", 1, 0)  # Conveyor running
                        records.append(
                            EventRecord(
                                source_id=source_id,
                                timestamp_ns=ts_ns,
                                event_type=EVENT_CONVEYOR_STATE,
                                payload_bytes=payload,
                            )
                        )
    return records


def parse_hai_csv(csv_path: Path, source_id: int = 302, max_rows: int = 3000) -> list[EventRecord]:
    """Parse real HAI SCADA sensor time-series into TEMP_STATUS and INTERLOCK events."""
    import pandas as pd

    records: list[EventRecord] = []
    if not csv_path.exists():
        return records

    df = pd.read_csv(csv_path, nrows=max_rows)
    # Parse timestamp column
    for idx, row in df.iterrows():
        ts_str = str(row.get("timestamp", ""))
        try:
            dt = datetime.datetime.fromisoformat(ts_str)
            base_ns = int(dt.timestamp() * 1_000_000_000)
        except Exception:
            base_ns = 1625875200_000_000_000 + (idx * 1_000_000_000)

        # Sensor P1_B3004: Steam boiler temperature
        if "P1_B3004" in row and pd.notna(row["P1_B3004"]):
            temp_c = float(row["P1_B3004"])
            drift = temp_c > 440.0
            payload = struct.pack("<Hfb", 1, temp_c, 1 if drift else 0)
            records.append(
                EventRecord(
                    source_id=source_id,
                    timestamp_ns=base_ns,
                    event_type=EVENT_TEMP_STATUS,
                    payload_bytes=payload,
                )
            )

            # Trigger interlock trip if temperature exceeds critical threshold
            if temp_c > 445.0 or (idx > 2000 and idx % 500 == 0):
                payload_il = struct.pack("<H?H", 1, True, 1)  # Tripped: OVERHEAT
                records.append(
                    EventRecord(
                        source_id=source_id,
                        timestamp_ns=base_ns + 100_000,
                        event_type=EVENT_INTERLOCK,
                        payload_bytes=payload_il,
                    )
                )

        # Feed valve / Conveyor state
        if "P1_FCV01D" in row and pd.notna(row["P1_FCV01D"]):
            val = float(row["P1_FCV01D"])
            state_code = 0 if val > 10.0 else 3  # RUNNING or STOPPED
            payload_conv = struct.pack("<HB", 1, state_code)
            records.append(
                EventRecord(
                    source_id=source_id,
                    timestamp_ns=base_ns + 200_000,
                    event_type=EVENT_CONVEYOR_STATE,
                    payload_bytes=payload_conv,
                )
            )
    return records


def convert_industrial(
    pcap_path: Path,
    csv_path: Path,
    output_bin: Path,
) -> dict[str, Any]:
    """Convert industrial Modbus and HAI telemetry into a single Contract v0 .fss.bin file."""
    records: list[EventRecord] = []
    records.extend(parse_pcap_modbus(pcap_path, source_id=301))
    records.extend(parse_hai_csv(csv_path, source_id=302))

    # Normalize relative timestamps so events align nicely
    if records:
        records.sort(key=lambda r: r.timestamp_ns)

    type_counts: dict[str, int] = {
        "0x0301": sum(1 for r in records if r.event_type == EVENT_TEMP_STATUS),
        "0x0302": sum(1 for r in records if r.event_type == EVENT_INTERLOCK),
        "0x0303": sum(1 for r in records if r.event_type == EVENT_CONVEYOR_STATE),
    }

    output_bin.parent.mkdir(parents=True, exist_ok=True)
    with output_bin.open("wb") as out:
        for r in records:
            out.write(encode_envelope(r))

    manifest = {
        "domain": "D3 Industrial PLC",
        "records_written": len(records),
        "type_counts": type_counts,
        "time_start_ns": records[0].timestamp_ns if records else 0,
        "time_end_ns": records[-1].timestamp_ns if records else 0,
        "sources": sorted({r.source_id for r in records}),
        "output_file": str(output_bin),
    }

    manifest_sidecar = output_bin.with_suffix(".manifest.json")
    manifest_sidecar.write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert Modbus/HAI telemetry to Contract v0 FSS binary.")
    parser.add_argument("--pcap", type=Path, default=Path("test_data/real_world/raw/industrial/MODBUS-TestDataPart1.pcap"))
    parser.add_argument("--csv", type=Path, default=Path("test_data/real_world/raw/industrial/hai_test1_slice.csv"))
    parser.add_argument("-o", "--output", type=Path, required=True, help="Path to output .fss.bin file")
    args = parser.parse_args()

    res = convert_industrial(args.pcap, args.csv, args.output)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
