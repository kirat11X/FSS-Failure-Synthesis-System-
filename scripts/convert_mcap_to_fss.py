#!/usr/bin/env python3
"""Convert Foxglove / ROS2 MCAP (.mcap) binary files into Contract v0 FSS binary envelopes (*.fss.bin).

Supports both official `mcap` library (if installed) and a standalone pure-Python
fallback parser that decodes MCAP header magic, Schemas (0x03), Channels (0x04),
Messages (0x05), and uncompressed Chunks (0x06).

Translates standard robotics/drone telemetry topics:
  - *attitude* -> 0x40 ATTITUDE (>fff)
  - *gps* -> 0x41 GPS_STATUS (>BBHiii)
  - *bat* / *battery* -> 0x42 BATTERY_STATUS (>fff)
  - *diag* / *diagnostic* -> 0x30 DIAGNOSTIC (<Hf)
  - *lidar* -> 0x10 LIDAR_STATUS (<HI)
  - *avoid* / *avoidance* -> 0x20 AVOIDANCE (<Bfh)
  - *formation* -> 0x21 FORMATION_STATE (<BH)
  - *error* / *fault* -> 0x45 ERROR_EVENT (>HH)

Emits valid Contract v0 Little-Endian envelopes:
  magic u16 (0xF55A) | version u8 (1) | flags u8 (0) | source_id u32 |
  timestamp_ns u64 | event_type u32 | payload_len u32 | payload[payload_len]
"""

from __future__ import annotations

import argparse
import io
import json
import math
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

MCAP_MAGIC = b"\x89MCAP\x30\r\n"

BASE_EPOCH_NS = 1_718_000_000_000_000_000


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
        return base_epoch_ns + int(val * 1_000_000_000 if val < 1000 else val)
    return base_epoch_ns


# MCAP Record Opcodes
OP_HEADER = 0x01
OP_FOOTER = 0x02
OP_SCHEMA = 0x03
OP_CHANNEL = 0x04
OP_MESSAGE = 0x05
OP_CHUNK = 0x06
OP_MESSAGE_INDEX = 0x07
OP_CHUNK_INDEX = 0x08
OP_ATTACHMENT = 0x09
OP_METADATA = 0x0A


@dataclass
class McapSchema:
    id: int
    name: str
    encoding: str
    data: bytes


@dataclass
class McapChannel:
    id: int
    schema_id: int
    topic: str
    message_encoding: str
    metadata: dict[str, str]


@dataclass
class McapMessage:
    channel_id: int
    sequence: int
    log_time: int  # uint64 ns
    publish_time: int  # uint64 ns
    data: bytes


def _read_prefixed_str(buf: bytes, offset: int) -> tuple[str, int]:
    str_len = struct.unpack_from("<I", buf, offset)[0]
    offset += 4
    val = buf[offset : offset + str_len].decode("utf-8", errors="ignore")
    return val, offset + str_len


def _read_prefixed_bytes(buf: bytes, offset: int) -> tuple[bytes, int]:
    byte_len = struct.unpack_from("<I", buf, offset)[0]
    offset += 4
    val = buf[offset : offset + byte_len]
    return val, offset + byte_len


class PurePythonMcapReader:
    """Zero-dependency pure-Python Foxglove MCAP reader."""

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.schemas: dict[int, McapSchema] = {}
        self.channels: dict[int, McapChannel] = {}
        self._check_header()

    def _check_header(self) -> None:
        magic = self.stream.read(8)
        if len(magic) < 8 or magic != MCAP_MAGIC:
            raise ValueError(f"Invalid MCAP header magic: {magic!r}")

    def _parse_records(self, data_stream: BinaryIO) -> Iterator[McapMessage]:
        hdr_struct = struct.Struct("<BQ")  # opcode (u8), length (u64)
        while True:
            hdr_bytes = data_stream.read(9)
            if len(hdr_bytes) < 9:
                break
            opcode, record_len = hdr_struct.unpack(hdr_bytes)
            body = data_stream.read(record_len)
            if len(body) < record_len:
                break

            if opcode == OP_SCHEMA:
                if len(body) >= 2:
                    schema_id = struct.unpack_from("<H", body, 0)[0]
                    name, off = _read_prefixed_str(body, 2)
                    encoding, off = _read_prefixed_str(body, off)
                    schema_data, _ = _read_prefixed_bytes(body, off)
                    self.schemas[schema_id] = McapSchema(schema_id, name, encoding, schema_data)

            elif opcode == OP_CHANNEL:
                if len(body) >= 4:
                    channel_id, schema_id = struct.unpack_from("<HH", body, 0)
                    topic, off = _read_prefixed_str(body, 4)
                    encoding, off = _read_prefixed_str(body, off)
                    self.channels[channel_id] = McapChannel(channel_id, schema_id, topic, encoding, {})

            elif opcode == OP_MESSAGE:
                if len(body) >= 22:
                    channel_id, seq, log_time, pub_time = struct.unpack_from("<HIQQ", body, 0)
                    msg_data = body[22:]
                    yield McapMessage(channel_id, seq, log_time, pub_time, msg_data)

            elif opcode == OP_CHUNK:
                if len(body) >= 28:
                    off = 28
                    compression, off = _read_prefixed_str(body, off)
                    records_len = struct.unpack_from("<Q", body, off)[0]
                    records_data = body[off + 8 : off + 8 + records_len]
                    if compression == "" or compression.lower() == "none":
                        chunk_io = io.BytesIO(records_data)
                        yield from self._parse_records(chunk_io)
                    elif compression.lower() == "lz4":
                        try:
                            import lz4.frame
                            decompressed = lz4.frame.decompress(records_data)
                            chunk_io = io.BytesIO(decompressed)
                            yield from self._parse_records(chunk_io)
                        except Exception:
                            pass
                    elif compression.lower() == "zstd":
                        try:
                            import zstandard
                            dctx = zstandard.ZstdDecompressor()
                            decompressed = dctx.decompress(records_data)
                            chunk_io = io.BytesIO(decompressed)
                            yield from self._parse_records(chunk_io)
                        except Exception:
                            pass

    def read_messages(self) -> Iterator[tuple[McapChannel, McapMessage]]:
        """Yield (channel, message) tuples."""
        for msg in self._parse_records(self.stream):
            channel = self.channels.get(
                msg.channel_id,
                McapChannel(msg.channel_id, 0, f"channel_{msg.channel_id}", "unknown", {}),
            )
            yield (channel, msg)


def parse_mcap_payload_to_fss(
    topic: str,
    encoding: str,
    data: bytes,
    timestamp_ns: int,
) -> tuple[int, int, bytes] | None:
    """Parse message payload and map to FSS (timestamp_ns, event_type, payload_bytes)."""
    topic_lower = topic.lower()

    # Try parsing as JSON if encoding says json or data starts with '{'
    obj: dict[str, Any] | None = None
    if "json" in encoding.lower() or (data and data.strip()[:1] in (b"{", b"[")):
        try:
            parsed = json.loads(data.decode("utf-8", errors="ignore"))
            if isinstance(parsed, dict):
                obj = parsed
        except Exception:
            obj = None

    # 1. ATTITUDE (0x40): roll_deg, pitch_deg, yaw_deg (>fff)
    if "attitude" in topic_lower:
        if obj is not None:
            roll = float(obj.get("roll_deg", obj.get("roll", 0.0)))
            pitch = float(obj.get("pitch_deg", obj.get("pitch", 0.0)))
            yaw = float(obj.get("yaw_deg", obj.get("yaw", 0.0)))
            # If radians, convert to deg
            if any(abs(v) > 0.0001 for v in (roll, pitch, yaw)):
                if all(abs(v) <= 2 * math.pi + 0.1 for v in (roll, pitch, yaw)):
                    roll, pitch, yaw = math.degrees(roll), math.degrees(pitch), math.degrees(yaw)
            payload = struct.pack(">fff", roll, pitch, yaw)
        elif len(data) >= 12:
            # Assume 3 floats
            payload = data[:12]
        else:
            payload = struct.pack(">fff", 0.0, 0.0, 0.0)
        return (timestamp_ns, EVENT_ATTITUDE, payload)

    # 2. GPS_STATUS (0x41): status (u8), num_sats (u8), hdop_cm (u16 big), lat_e7 (i32 big), lng_e7 (i32 big), alt_cm (i32 big)
    elif "gps" in topic_lower:
        if obj is not None:
            status = int(obj.get("status", obj.get("fix_type", 3))) & 0xFF
            num_sats = int(obj.get("num_sats", obj.get("satellites_used", 12))) & 0xFF
            raw_hdop = obj.get("hdop", 1.0)
            hdop_cm = int(raw_hdop * 100) if isinstance(raw_hdop, float) else int(raw_hdop)
            hdop_cm = max(0, min(0xFFFF, hdop_cm))

            raw_lat = obj.get("lat", obj.get("lat_e7", 0))
            raw_lon = obj.get("lon", obj.get("lng", obj.get("lng_e7", 0)))
            lat_e7 = int(raw_lat) if abs(raw_lat) > 1000 else int(raw_lat * 1e7)
            lng_e7 = int(raw_lon) if abs(raw_lon) > 1000 else int(raw_lon * 1e7)

            raw_alt = obj.get("alt", obj.get("alt_cm", 0))
            alt_cm = int(raw_alt) if abs(raw_alt) > 10000 else int(raw_alt * 100)

            payload = struct.pack(">BBHiii", status, num_sats, hdop_cm, lat_e7, lng_e7, alt_cm)
        elif len(data) >= 16:
            payload = data[:16]
        else:
            payload = struct.pack(">BBHiii", 3, 10, 100, 0, 0, 0)
        return (timestamp_ns, EVENT_GPS, payload)

    # 3. BATTERY_STATUS (0x42): voltage_v, current_a, remaining_pct (>fff)
    elif "bat" in topic_lower:
        if obj is not None:
            volt = float(obj.get("voltage_v", obj.get("voltage", 16.0)))
            curr = float(obj.get("current_a", obj.get("current", 10.0)))
            rem = float(obj.get("remaining_pct", obj.get("remaining", 85.0)))
            if rem <= 1.0 and rem >= 0.0:
                rem *= 100.0
            payload = struct.pack(">fff", volt, curr, rem)
        elif len(data) >= 12:
            payload = data[:12]
        else:
            payload = struct.pack(">fff", 16.0, 10.0, 85.0)
        return (timestamp_ns, EVENT_BATTERY, payload)

    # 4. LIDAR_STATUS (0x10): status_code u16, timeout_count u32 (<HI)
    elif "lidar" in topic_lower:
        if obj is not None:
            code = int(obj.get("status_code", 0)) & 0xFFFF
            tcount = int(obj.get("timeout_count", 0)) & 0xFFFFFFFF
            payload = struct.pack("<HI", code, tcount)
        elif len(data) >= 6:
            payload = data[:6]
        else:
            payload = struct.pack("<HI", 0, 0)
        return (timestamp_ns, EVENT_LIDAR, payload)

    # 5. AVOIDANCE (0x20): reason_code u8, min_sep f32, heading i16 (<Bfh)
    elif "avoid" in topic_lower:
        if obj is not None:
            reason = int(obj.get("reason_code", 1)) & 0xFF
            min_sep = float(obj.get("min_separation_m", 3.0))
            heading = max(-32768, min(32767, int(obj.get("heading_change_deg", 0))))
            payload = struct.pack("<Bfh", reason, min_sep, heading)
        elif len(data) >= 7:
            payload = data[:7]
        else:
            payload = struct.pack("<Bfh", 1, 3.0, 0)
        return (timestamp_ns, EVENT_AVOIDANCE, payload)

    # 6. FORMATION_STATE (0x21): state_code u8, formation_id u16 (<BH)
    elif "formation" in topic_lower:
        if obj is not None:
            state = int(obj.get("state_code", 0)) & 0xFF
            fid = int(obj.get("formation_id", 1)) & 0xFFFF
            payload = struct.pack("<BH", state, fid)
        elif len(data) >= 3:
            payload = data[:3]
        else:
            payload = struct.pack("<BH", 0, 1)
        return (timestamp_ns, EVENT_FORMATION, payload)

    # 7. DIAGNOSTIC (0x30): status_code u16, temperature_c f32 (<Hf)
    elif "diag" in topic_lower:
        if obj is not None:
            status = int(obj.get("status_code", 0)) & 0xFFFF
            temp = float(obj.get("temperature_c", 35.0))
            payload = struct.pack("<Hf", status, temp)
        elif len(data) >= 6:
            payload = data[:6]
        else:
            payload = struct.pack("<Hf", 0, 35.0)
        return (timestamp_ns, EVENT_DIAGNOSTIC, payload)

    # 8. ERROR_EVENT (0x45): subsystem_code u16, error_code u16 (>HH)
    elif "error" in topic_lower or "fault" in topic_lower:
        if obj is not None:
            sub = int(obj.get("subsystem_code", obj.get("subsystem", 1))) & 0xFFFF
            err = int(obj.get("error_code", obj.get("code", 1))) & 0xFFFF
            payload = struct.pack(">HH", sub, err)
        elif len(data) >= 4:
            payload = data[:4]
        else:
            payload = struct.pack(">HH", 1, 1)
        return (timestamp_ns, EVENT_ERROR, payload)

    # 9. Generic robotics message fallback -> DIAGNOSTIC (0x30)
    return (timestamp_ns, EVENT_DIAGNOSTIC, struct.pack("<Hf", 0, 25.0))


def convert_mcap_to_fss(
    input_path: Path,
    output_path: Path,
    source_id: int = 1,
    max_records: int | None = None,
) -> dict[str, Any]:
    """Parse Foxglove / ROS2 MCAP file and write valid Contract v0 Little-Endian envelopes."""
    records_written = 0
    type_counts: dict[str, int] = {}
    timestamps: list[int] = []

    output_path.parent.mkdir(parents=True, exist_ok=True)
    envelopes_data = bytearray()

    use_mcap_lib = False
    try:
        import mcap.reader
        use_mcap_lib = True
    except ImportError:
        pass

    if use_mcap_lib:
        try:
            import mcap.reader
            with open(input_path, "rb") as f:
                reader = mcap.reader.make_reader(f)
                for schema, channel, message in reader.iter_messages():
                    if max_records and records_written >= max_records:
                        break
                    ts_raw = message.log_time if message.log_time > 0 else message.publish_time
                    ts_ns = normalize_timestamp_ns(ts_raw)
                    res = parse_mcap_payload_to_fss(
                        channel.topic, channel.message_encoding, message.data, ts_ns
                    )
                    if res is not None:
                        ev_ts, etype, payload = res
                        record = EventRecord(source_id=source_id, timestamp_ns=ev_ts, event_type=etype, payload_bytes=payload)
                        envelopes_data.extend(encode_envelope(record))
                        records_written += 1
                        timestamps.append(ev_ts)
                        tname = f"0x{etype:02X}"
                        type_counts[tname] = type_counts.get(tname, 0) + 1
        except Exception:
            use_mcap_lib = False

    if not use_mcap_lib:
        with open(input_path, "rb") as f:
            reader = PurePythonMcapReader(f)
            for channel, msg in reader.read_messages():
                if max_records and records_written >= max_records:
                    break
                ts_raw = msg.log_time if msg.log_time > 0 else msg.publish_time
                ts_ns = normalize_timestamp_ns(ts_raw)
                res = parse_mcap_payload_to_fss(
                    channel.topic, channel.message_encoding, msg.data, ts_ns
                )
                if res is not None:
                    ev_ts, etype, payload = res
                    record = EventRecord(source_id=source_id, timestamp_ns=ev_ts, event_type=etype, payload_bytes=payload)
                    envelopes_data.extend(encode_envelope(record))
                    records_written += 1
                    timestamps.append(ev_ts)
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
    parser = argparse.ArgumentParser(description="Convert Foxglove / ROS2 MCAP (.mcap) to FSS binary envelopes (.fss.bin)")
    parser.add_argument("input", type=Path, help="Path to input .mcap file")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Path to output .fss.bin file")
    parser.add_argument("--source-id", type=int, default=1, help="Producer / Agent Source ID (default: 1)")
    parser.add_argument("--max-records", type=int, default=None, help="Maximum number of records to convert")

    args = parser.parse_args()

    if not args.input.is_file():
        sys.stderr.write(f"Error: Input file '{args.input}' not found.\n")
        sys.exit(1)

    result = convert_mcap_to_fss(args.input, args.output, source_id=args.source_id, max_records=args.max_records)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
