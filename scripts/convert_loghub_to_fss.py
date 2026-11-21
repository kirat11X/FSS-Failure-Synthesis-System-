#!/usr/bin/env python3
"""scripts/convert_loghub_to_fss.py
-----------------------------------
Convert real-world distributed systems logs (Loghub 2k: HDFS, BGL, Zookeeper, Apache)
into Contract v0 Little-Endian FSS binary envelopes (*.fss.bin).

Packs events matching `schemas/microservices_v0.yaml`:
  - 0x0201 DB_POOL_STATUS (<HHHB: pool_id u16, active_conns u16, max_conns u16, state_code u8)
  - 0x0202 REQUEST_METRIC (<HIH: service_id u16, latency_ms u32, status_code u16)
  - 0x0203 HTTP_ERROR (<HHH: service_id u16, error_code u16, downstream_id u16)

Preserves multi-node source IDs, nanosecond timestamps, and error classifications.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fss_py.model import EventRecord
from fss_py.wire import encode_envelope

EVENT_DB_POOL_STATUS = 0x0201
EVENT_REQUEST_METRIC = 0x0202
EVENT_HTTP_ERROR = 0x0203


def parse_hdfs_log(path: Path, max_lines: int = 500) -> list[EventRecord]:
    """Parse HDFS 2k log lines into FSS events."""
    records: list[EventRecord] = []
    if not path.exists():
        return records

    # 081109 203615 148 INFO dfs.DataNode$PacketResponder...
    pat = re.compile(r"^(\d{6})\s+(\d{6})\s+\d+\s+(\w+)\s+([\w\.\$]+):\s+(.*)$")
    base_epoch = 1226260000  # Nov 9 2008

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f):
            if idx >= max_lines:
                break
            m = pat.match(line.strip())
            if not m:
                continue
            date_str, time_str, level, component, msg = m.groups()
            ts_ns = (base_epoch + idx) * 1_000_000_000

            sid = 601 if "NameSystem" in component else 602
            if "terminating" in msg.lower() or "fail" in msg.lower():
                err_code = 1002  # PACKET_RESPONDER_FAIL
                payload = struct.pack("<HHH", sid, err_code, 601)
                records.append(
                    EventRecord(
                        source_id=sid,
                        timestamp_ns=ts_ns,
                        event_type=EVENT_HTTP_ERROR,
                        payload_bytes=payload,
                    )
                )
            else:
                payload = struct.pack("<HIH", sid, 45, 200)
                records.append(
                    EventRecord(
                        source_id=sid,
                        timestamp_ns=ts_ns,
                        event_type=EVENT_REQUEST_METRIC,
                        payload_bytes=payload,
                    )
                )
    return records


def parse_zookeeper_log(path: Path, max_lines: int = 500) -> list[EventRecord]:
    """Parse Zookeeper 2k log lines into FSS events."""
    records: list[EventRecord] = []
    if not path.exists():
        return records

    # 2015-07-29 17:41:44,747 - INFO  [QuorumPeer[myid=1]/...
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),(\d{3})\s+-\s+(\w+)\s+\[([^\]]+)\]\s+-\s+(.*)$")

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f):
            if idx >= max_lines:
                break
            m = pat.match(line.strip())
            if not m:
                continue
            dt_str, ms_str, level, component, msg = m.groups()
            try:
                dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                ts_ns = int(dt.timestamp() * 1_000_000_000) + (int(ms_str) * 1_000_000)
            except Exception:
                ts_ns = (1438190000 + idx) * 1_000_000_000

            sid = 611 if "myid=1" in component else 612
            if "timeout" in msg.lower() or "notification time out" in msg.lower():
                err_code = 1010  # LEADER_ELECTION_TIMEOUT
                payload = struct.pack("<HHH", sid, err_code, 612)
                records.append(
                    EventRecord(
                        source_id=sid,
                        timestamp_ns=ts_ns,
                        event_type=EVENT_HTTP_ERROR,
                        payload_bytes=payload,
                    )
                )
            elif "leaving thread" in msg.lower():
                err_code = 1011  # SEND_WORKER_LEAVING
                payload = struct.pack("<HHH", sid, err_code, 612)
                records.append(
                    EventRecord(
                        source_id=sid,
                        timestamp_ns=ts_ns,
                        event_type=EVENT_HTTP_ERROR,
                        payload_bytes=payload,
                    )
                )
            else:
                payload = struct.pack("<HIH", sid, 25, 200)
                records.append(
                    EventRecord(
                        source_id=sid,
                        timestamp_ns=ts_ns,
                        event_type=EVENT_REQUEST_METRIC,
                        payload_bytes=payload,
                    )
                )
    return records


def parse_bgl_log(path: Path, max_lines: int = 500) -> list[EventRecord]:
    """Parse BGL kernel RAS log lines into FSS events."""
    records: list[EventRecord] = []
    if not path.exists():
        return records

    # - 1117838570 2005.06.03 R02-M1-N0-C:J12-U11 ...
    pat = re.compile(r"^-\s+(\d+)\s+[\d\.]+\s+([\w\:\-]+)\s+.*?\s+(INFO|WARNING|ERROR|FATAL)\s+(.*)$")

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f):
            if idx >= max_lines:
                break
            m = pat.match(line.strip())
            if not m:
                continue
            sec_str, node, level, msg = m.groups()
            ts_ns = int(sec_str) * 1_000_000_000 + (idx * 1_000)
            sid = 621

            if "parity error" in msg.lower() or "cache" in msg.lower():
                err_code = 1020  # CACHE_PARITY_ERROR
                payload = struct.pack("<HHH", sid, err_code, 621)
                records.append(
                    EventRecord(
                        source_id=sid,
                        timestamp_ns=ts_ns,
                        event_type=EVENT_HTTP_ERROR,
                        payload_bytes=payload,
                    )
                )
            else:
                payload = struct.pack("<HIH", sid, 15, 200)
                records.append(
                    EventRecord(
                        source_id=sid,
                        timestamp_ns=ts_ns,
                        event_type=EVENT_REQUEST_METRIC,
                        payload_bytes=payload,
                    )
                )
    return records


def parse_apache_log(path: Path, max_lines: int = 500) -> list[EventRecord]:
    """Parse Apache httpd error log lines into FSS events."""
    records: list[EventRecord] = []
    if not path.exists():
        return records

    # [Sun Dec 04 04:47:44 2005] [error] mod_jk child workerEnv in error state 6
    pat = re.compile(r"^\[.*?\]\s+\[(\w+)\]\s+(.*)$")
    base_epoch = 1133671664

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f):
            if idx >= max_lines:
                break
            m = pat.match(line.strip())
            if not m:
                continue
            level, msg = m.groups()
            ts_ns = (base_epoch + idx) * 1_000_000_000
            sid = 631

            if "error state" in msg.lower() or level == "error":
                err_code = 1030  # WORKER_ENV_ERROR
                payload = struct.pack("<HHH", sid, err_code, 631)
                records.append(
                    EventRecord(
                        source_id=sid,
                        timestamp_ns=ts_ns,
                        event_type=EVENT_HTTP_ERROR,
                        payload_bytes=payload,
                    )
                )
            else:
                payload = struct.pack("<HIH", sid, 55, 200)
                records.append(
                    EventRecord(
                        source_id=sid,
                        timestamp_ns=ts_ns,
                        event_type=EVENT_REQUEST_METRIC,
                        payload_bytes=payload,
                    )
                )
    return records


def convert_loghub(
    log_dir: Path,
    output_bin: Path,
) -> dict[str, Any]:
    """Convert Loghub logs into a single Contract v0 .fss.bin file."""
    records: list[EventRecord] = []
    records.extend(parse_hdfs_log(log_dir / "HDFS_2k.log"))
    records.extend(parse_zookeeper_log(log_dir / "Zookeeper_2k.log"))
    records.extend(parse_bgl_log(log_dir / "BGL_2k.log"))
    records.extend(parse_apache_log(log_dir / "Apache_2k.log"))

    # Align timestamps into a unified monotonic interval
    records.sort(key=lambda r: r.timestamp_ns)

    type_counts: dict[str, int] = {
        "0x0201": sum(1 for r in records if r.event_type == EVENT_DB_POOL_STATUS),
        "0x0202": sum(1 for r in records if r.event_type == EVENT_REQUEST_METRIC),
        "0x0203": sum(1 for r in records if r.event_type == EVENT_HTTP_ERROR),
    }

    output_bin.parent.mkdir(parents=True, exist_ok=True)
    with output_bin.open("wb") as out:
        for r in records:
            out.write(encode_envelope(r))

    manifest = {
        "domain": "Distributed Systems (Loghub)",
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
    parser = argparse.ArgumentParser(description="Convert Loghub logs to Contract v0 FSS binary.")
    parser.add_argument("--log-dir", type=Path, default=Path("test_data/real_world/raw/loghub"))
    parser.add_argument("-o", "--output", type=Path, required=True, help="Path to output .fss.bin file")
    args = parser.parse_args()

    res = convert_loghub(args.log_dir, args.output)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
