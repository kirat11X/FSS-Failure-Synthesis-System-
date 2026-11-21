#!/usr/bin/env python3
"""Convert PX4 ULog binary logs (.ulg) into Contract v0 FSS binary envelopes (*.fss.bin).

Supports both official `pyulog` library (if installed) and a standalone pure-Python
fallback parser that decodes ULog headers, format definitions ('F'), subscriptions ('A'),
and data records ('D').

Translates standard PX4 topics:
  - vehicle_attitude -> 0x40 ATTITUDE (>fff)
  - vehicle_gps_position / gps -> 0x41 GPS_STATUS (>BBHiii)
  - battery_status -> 0x42 BATTERY_STATUS (>fff)
  - sensor_combined / diagnostic -> 0x30 DIAGNOSTIC (<Hf)
  - error_event / fault -> 0x45 ERROR_EVENT (>HH)

Emits valid Contract v0 Little-Endian envelopes:
  magic u16 (0xF55A) | version u8 (1) | flags u8 (0) | source_id u32 |
  timestamp_ns u64 | event_type u32 | payload_len u32 | payload[payload_len]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

# Ensure project root is importable for fss_py
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fss_py.contract import MAGIC, VERSION
from fss_py.model import EventRecord
from fss_py.wire import encode_envelope

# Event Type IDs matching schemas/drone_v0.yaml
EVENT_LIDAR = 0x10
EVENT_AVOIDANCE = 0x20
EVENT_FORMATION = 0x21
EVENT_DIAGNOSTIC = 0x30
EVENT_ATTITUDE = 0x40
EVENT_GPS = 0x41
EVENT_BATTERY = 0x42
EVENT_EKF = 0x43
EVENT_VIBRATION = 0x44
EVENT_ERROR = 0x45

# ULog Constants
ULOG_HEADER_MAGIC = b"ULog\x01\x12\x35"
MSG_TYPE_FORMAT = ord("F")
MSG_TYPE_DATA = ord("D")
MSG_TYPE_INFO = ord("I")
MSG_TYPE_INFO_MULTIPLE = ord("M")
MSG_TYPE_PARAMETER = ord("P")
MSG_TYPE_ADD_LOGGED_MSG = ord("A")
MSG_TYPE_REMOVE_LOGGED_MSG = ord("R")
MSG_TYPE_SYNC = ord("S")
MSG_TYPE_DROPOUT = ord("O")
MSG_TYPE_LOGGING = ord("L")

TYPE_MAP: dict[str, tuple[str, int]] = {
    "int8_t": ("b", 1),
    "uint8_t": ("B", 1),
    "int16_t": ("h", 2),
    "uint16_t": ("H", 2),
    "int32_t": ("i", 4),
    "uint32_t": ("I", 4),
    "int64_t": ("q", 8),
    "uint64_t": ("Q", 8),
    "float": ("f", 4),
    "double": ("d", 8),
    "bool": ("?", 1),
    "char": ("c", 1),
}


@dataclass
class ULogField:
    name: str
    type_name: str
    struct_fmt: str
    offset: int
    size: int
    array_len: int = 1


@dataclass
class ULogFormat:
    name: str
    fields: list[ULogField]
    total_size: int

    def unpack(self, data: bytes) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for f in self.fields:
            if f.offset + f.size > len(data):
                continue
            if f.array_len > 1:
                vals = struct.unpack_from(f"<{f.array_len}{f.struct_fmt}", data, f.offset)
                result[f.name] = list(vals)
            else:
                val = struct.unpack_from(f"<{f.struct_fmt}", data, f.offset)[0]
                result[f.name] = val
        return result


def parse_format_def(format_str: str) -> ULogFormat:
    """Parse ULog 'F' format definition string: 'name:type name;type name;...'"""
    parts = format_str.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid format definition string: {format_str}")
    name = parts[0].strip()
    field_defs = parts[1].split(";")

    fields: list[ULogField] = []
    offset = 0

    for fdef in field_defs:
        fdef = fdef.strip()
        if not fdef:
            continue
        tokens = fdef.split()
        if len(tokens) < 2:
            continue
        raw_type = tokens[0].strip()
        raw_name = tokens[1].strip()

        array_len = 1
        m = re.match(r"([a-zA-Z0-9_]+)\[(\d+)\]", raw_type)
        if m:
            base_type = m.group(1)
            array_len = int(m.group(2))
        else:
            base_type = raw_type

        if base_type not in TYPE_MAP:
            # Unknown / nested type fallback: 4 bytes int
            fmt, elem_size = "i", 4
        else:
            fmt, elem_size = TYPE_MAP[base_type]

        field_size = elem_size * array_len
        fields.append(
            ULogField(
                name=raw_name,
                type_name=base_type,
                struct_fmt=fmt,
                offset=offset,
                size=field_size,
                array_len=array_len,
            )
        )
        offset += field_size

    return ULogFormat(name=name, fields=fields, total_size=offset)


class PurePythonULogReader:
    """Zero-dependency pure-Python PX4 ULog parser."""

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.formats: dict[str, ULogFormat] = {}
        self.subscriptions: dict[int, tuple[str, int]] = {}  # msg_id -> (name, multi_id)
        self.file_timestamp_us = 0
        self._read_header()

    def _read_header(self) -> None:
        header = self.stream.read(16)
        if len(header) < 16:
            raise ValueError(f"ULog file too short ({len(header)} bytes)")
        if not header.startswith(b"ULog"):
            raise ValueError(f"Invalid ULog magic: {header[:8]!r}")
        self.file_timestamp_us = struct.unpack_from("<Q", header, 8)[0]

    def read_messages(self) -> Iterator[tuple[str, int, dict[str, Any]]]:
        """Yield (topic_name, multi_id, fields_dict) for each logged data record."""
        header_struct = struct.Struct("<HB")  # msg_size, msg_type

        while True:
            hdr_bytes = self.stream.read(3)
            if len(hdr_bytes) < 3:
                break
            msg_size, msg_type = header_struct.unpack(hdr_bytes)
            payload = self.stream.read(msg_size)
            if len(payload) < msg_size:
                break

            if msg_type == MSG_TYPE_FORMAT:
                format_str = payload.decode("utf-8", errors="ignore").rstrip("\x00")
                try:
                    fmt = parse_format_def(format_str)
                    self.formats[fmt.name] = fmt
                except Exception:
                    pass

            elif msg_type == MSG_TYPE_ADD_LOGGED_MSG:
                if len(payload) >= 3:
                    multi_id = payload[0]
                    msg_id = struct.unpack_from("<H", payload, 1)[0]
                    msg_name = payload[3:].decode("utf-8", errors="ignore").rstrip("\x00")
                    self.subscriptions[msg_id] = (msg_name, multi_id)

            elif msg_type == MSG_TYPE_REMOVE_LOGGED_MSG:
                if len(payload) >= 2:
                    msg_id = struct.unpack_from("<H", payload, 0)[0]
                    self.subscriptions.pop(msg_id, None)

            elif msg_type == MSG_TYPE_DATA:
                if len(payload) >= 2:
                    msg_id = struct.unpack_from("<H", payload, 0)[0]
                    if msg_id in self.subscriptions:
                        topic_name, multi_id = self.subscriptions[msg_id]
                        if topic_name in self.formats:
                            fmt = self.formats[topic_name]
                            fields = fmt.unpack(payload[2:])
                            yield (topic_name, multi_id, fields)


def quat_to_euler_deg(q: list[float]) -> tuple[float, float, float]:
    """Convert quaternion [w, x, y, z] to Euler angles [roll, pitch, yaw] in degrees."""
    if len(q) < 4:
        return 0.0, 0.0, 0.0
    w, x, y, z = q[0], q[1], q[2], q[3]
    # Roll (x-axis rotation)
    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # Pitch (y-axis rotation)
    sinp = 2.0 * (w * y - z * x)
    if abs(sinp) >= 1.0:
        pitch = math.copysign(math.pi / 2, sinp)
    else:
        pitch = math.asin(sinp)

    # Yaw (z-axis rotation)
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)


def map_ulog_message_to_fss(topic: str, fields: dict[str, Any], base_epoch_ns: int = 1_718_000_000_000_000_000) -> tuple[int, int, bytes] | None:
    """Map a parsed ULog message dictionary to an FSS (timestamp_ns, event_type, payload_bytes) tuple.

    Returns None if the topic does not map to a recognized schema.
    """
    raw_ts = fields.get("timestamp", fields.get("time_us", 0))
    if isinstance(raw_ts, (int, float)) and raw_ts > 0:
        if raw_ts > 100_000_000_000_000:  # already nanoseconds or big epoch us
            ts_ns = int(raw_ts * 1000) if raw_ts < 10**16 else int(raw_ts)
        else:
            # relative flight time in microseconds -> offset from base epoch
            ts_ns = base_epoch_ns + int(raw_ts * 1000)
    else:
        ts_ns = base_epoch_ns

    topic_lower = topic.lower()

    # 1. ATTITUDE (0x40): roll_deg, pitch_deg, yaw_deg (>fff)
    if "attitude" in topic_lower:
        if "q" in fields and isinstance(fields["q"], list) and len(fields["q"]) >= 4:
            roll, pitch, yaw = quat_to_euler_deg(fields["q"])
        else:
            roll = float(fields.get("roll", fields.get("roll_deg", 0.0)))
            pitch = float(fields.get("pitch", fields.get("pitch_deg", 0.0)))
            yaw = float(fields.get("yaw", fields.get("yaw_deg", 0.0)))
            # If values appear to be in radians (all within [-2pi, 2pi] with non-zero), convert to degrees
            if any(abs(v) > 0.0001 for v in (roll, pitch, yaw)):
                if all(abs(v) <= 2 * math.pi + 0.1 for v in (roll, pitch, yaw)):
                    roll, pitch, yaw = math.degrees(roll), math.degrees(pitch), math.degrees(yaw)
        payload = struct.pack(">fff", roll, pitch, yaw)
        return (ts_ns, EVENT_ATTITUDE, payload)

    # 2. GPS_STATUS (0x41): status (u8), num_sats (u8), hdop_cm (u16 big), lat_e7 (i32 big), lng_e7 (i32 big), alt_cm (i32 big)
    elif "gps" in topic_lower:
        status = int(fields.get("fix_type", fields.get("status", 3))) & 0xFF
        num_sats = int(fields.get("satellites_used", fields.get("num_sats", 12))) & 0xFF
        raw_hdop = fields.get("hdop", fields.get("eph", 1.0))
        hdop_cm = int(raw_hdop * 100) if isinstance(raw_hdop, float) else int(raw_hdop)
        hdop_cm = max(0, min(0xFFFF, hdop_cm))

        raw_lat = fields.get("lat", 0)
        raw_lon = fields.get("lon", fields.get("lng", 0))
        lat_e7 = int(raw_lat) if abs(raw_lat) > 1000 else int(raw_lat * 1e7)
        lng_e7 = int(raw_lon) if abs(raw_lon) > 1000 else int(raw_lon * 1e7)

        raw_alt = fields.get("alt", fields.get("altitude", 0))
        alt_cm = int(raw_alt) if abs(raw_alt) > 10000 else int(raw_alt * 100)

        payload = struct.pack(">BBHiii", status, num_sats, hdop_cm, lat_e7, lng_e7, alt_cm)
        return (ts_ns, EVENT_GPS, payload)

    # 3. BATTERY_STATUS (0x42): voltage_v, current_a, remaining_pct (>fff)
    elif "battery" in topic_lower:
        volt = float(fields.get("voltage_v", fields.get("voltage", 16.0)))
        curr = float(fields.get("current_a", fields.get("current", 10.0)))
        rem = float(fields.get("remaining", fields.get("remaining_pct", 0.85)))
        if rem <= 1.0 and rem >= 0.0:
            rem *= 100.0  # normalize 0..1 to percentage 0..100
        payload = struct.pack(">fff", volt, curr, rem)
        return (ts_ns, EVENT_BATTERY, payload)

    # 4. SENSOR_COMBINED / DIAGNOSTIC (0x30): status_code u16, temp f32 (<Hf)
    elif "sensor" in topic_lower or "diag" in topic_lower:
        status_code = int(fields.get("status", fields.get("status_code", 0))) & 0xFFFF
        temp = float(fields.get("temperature", fields.get("temp", 45.0)))
        payload = struct.pack("<Hf", status_code, temp)
        return (ts_ns, EVENT_DIAGNOSTIC, payload)

    # 5. ERROR / FAULT (0x45): subsystem u16, error_code u16 (>HH)
    elif "error" in topic_lower or "fault" in topic_lower:
        subsystem = int(fields.get("subsystem", 1)) & 0xFFFF
        code = int(fields.get("error_code", fields.get("code", 1))) & 0xFFFF
        payload = struct.pack(">HH", subsystem, code)
        return (ts_ns, EVENT_ERROR, payload)

    return None


def convert_ulog_to_fss(
    input_path: Path,
    output_path: Path,
    source_id: int = 1,
    max_records: int | None = None,
) -> dict[str, Any]:
    """Parse PX4 ULog file and write valid Contract v0 Little-Endian envelopes to output_path."""
    records_written = 0
    type_counts: dict[str, int] = {}
    timestamps: list[int] = []

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Try official pyulog if installed
    use_pyulog = False
    try:
        import pyulog
        use_pyulog = True
    except ImportError:
        pass

    envelopes_data = bytearray()

    if use_pyulog:
        try:
            import pyulog
            ulog = pyulog.ULog(str(input_path))
            for dataset in ulog.data_list:
                topic = dataset.name
                num_rows = len(dataset.data.get("timestamp", []))
                for i in range(num_rows):
                    if max_records and records_written >= max_records:
                        break
                    row_fields = {k: dataset.data[k][i] for k in dataset.data}
                    res = map_ulog_message_to_fss(topic, row_fields)
                    if res is not None:
                        ts_ns, etype, payload = res
                        record = EventRecord(source_id=source_id, timestamp_ns=ts_ns, event_type=etype, payload_bytes=payload)
                        envelopes_data.extend(encode_envelope(record))
                        records_written += 1
                        timestamps.append(ts_ns)
                        tname = f"0x{etype:02X}"
                        type_counts[tname] = type_counts.get(tname, 0) + 1
        except Exception:
            # Fall back to pure-Python reader on any error
            use_pyulog = False

    if not use_pyulog:
        with open(input_path, "rb") as f:
            reader = PurePythonULogReader(f)
            for topic, multi_id, fields in reader.read_messages():
                if max_records and records_written >= max_records:
                    break
                res = map_ulog_message_to_fss(topic, fields)
                if res is not None:
                    ts_ns, etype, payload = res
                    record = EventRecord(source_id=source_id, timestamp_ns=ts_ns, event_type=etype, payload_bytes=payload)
                    envelopes_data.extend(encode_envelope(record))
                    records_written += 1
                    timestamps.append(ts_ns)
                    tname = f"0x{etype:02X}"
                    type_counts[tname] = type_counts.get(tname, 0) + 1

    output_path.write_bytes(bytes(envelopes_data))

    return {
        "input": str(input_path),
        "output": str(output_path),
        "source_id": source_id,
        "records_written": records_written,
        "type_counts": type_counts,
        "time_start_ns": min(timestamps) if timestamps else 0,
        "time_end_ns": max(timestamps) if timestamps else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert PX4 ULog (.ulg) to FSS binary envelopes (.fss.bin)")
    parser.add_argument("input", type=Path, help="Path to input .ulg file")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Path to output .fss.bin file")
    parser.add_argument("--source-id", type=int, default=1, help="Producer / Agent Source ID (default: 1)")
    parser.add_argument("--max-records", type=int, default=None, help="Maximum number of records to convert")

    args = parser.parse_args()

    if not args.input.is_file():
        sys.stderr.write(f"Error: Input file '{args.input}' not found.\n")
        sys.exit(1)

    result = convert_ulog_to_fss(args.input, args.output, source_id=args.source_id, max_records=args.max_records)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
