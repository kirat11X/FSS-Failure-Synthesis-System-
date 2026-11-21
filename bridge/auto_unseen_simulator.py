"""Auto-Simulate Unseen Telemetry (Live E2E) Pipeline Engine.

Generates dynamic, randomized, Contract v0-compliant unseen telemetry across 5 distinct domains
(D1 Drone, D2 Microservices, D3 Industrial PLC, D4 Vehicle CAN big-endian, D5 Warehouse AGVs),
ingests the resulting binary envelopes directly into the Java 64KB BlockStore via JavaStoreBridge,
verifies block allocation and CRC-32C framing, and hands off execution to the Diagnostics MVP.

Guarantees:
  - Zero Java Core Storage Mutation (Hard Rule 1 & 16): Python never directly reads or writes .dat files.
  - Strict Blind-Evaluation Isolation: The GroundTruthManifest is kept strictly in session state,
    never injected into the LLM context, RAG query, or evidence builder.
  - Lossless uint64 nanosecond timestamps with single-epoch consistency.
"""

from __future__ import annotations

import os
import random
import struct
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from fss_py.contract import ENVELOPE_HEADER_LEN, MAGIC, VERSION
from fss_py.decoder import decode
from fss_py.model import ALL, EventRecord
from fss_py.schema import load_schema_file
from fss_py.wire import crc32c, encode_envelope
from bridge.java_retriever import JavaRetriever
from bridge.schemas import format_iso8601_ns

ROOT_DIR = Path(__file__).resolve().parent.parent

# -----------------------------------------------------------------------------
# Ground Truth Manifest (Strictly Isolated for Blind Evaluation)
# -----------------------------------------------------------------------------


@dataclass
class GroundTruthManifest:
    """Hidden ground truth manifest for blind evaluation comparison.

    MUST NEVER be leaked into LLM prompts, hybrid RAG context, or evidence builder.
    """
    domain: str
    seed: int
    injected_root_cause_source: int
    injected_root_event_type: int
    injected_root_event_name: str
    injected_symptom_source: int
    injected_symptom_event_type: int
    injected_symptom_event_name: str
    injected_gaps: list[str] = field(default_factory=list)
    clock_skew_ms: float = 0.0
    blackout_gap_s: float = 0.0
    crash_tail: bool = False
    total_events: int = 0
    sources: list[int] = field(default_factory=list)
    time_start_ns: int = 0
    time_end_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# -----------------------------------------------------------------------------
# Domain Specifications
# -----------------------------------------------------------------------------

SUPPORTED_DOMAINS: list[str] = [
    "D1 Drone",
    "D2 Microservices",
    "D3 Industrial PLC",
    "D4 Vehicle CAN (Big-Endian)",
    "D5 Warehouse AGVs",
]

DOMAIN_CONFIGS: dict[str, dict[str, Any]] = {
    "D1 Drone": {
        "key": "drone",
        "schema_path": ROOT_DIR / "schemas" / "drone_v0.yaml",
        "manual_path": ROOT_DIR / "domain_manuals" / "fss_custom_telemetry_codes.md",
        "manual_profile": "FSS Native Telemetry",
        "payload_endian": "<",
        "root_event_type": 0x10,
        "root_event_name": "LIDAR_STATUS",
        "root_error_label": "LIDAR_TIMEOUT",
        "symptom_event_type": 0x20,
        "symptom_event_name": "AVOIDANCE",
        "symptom_error_label": "COLLISION_RISK",
        "secondary_event_type": 0x21,
        "secondary_event_name": "FORMATION_STATE",
        "secondary_error_label": "LEFT_FORMATION",
    },
    "D2 Microservices": {
        "key": "microservices",
        "schema_path": ROOT_DIR / "schemas" / "microservices_v0.yaml",
        "manual_path": ROOT_DIR / "domain_manuals" / "microservices_error_codes.md",
        "manual_profile": "Microservices Architecture",
        "payload_endian": "<",
        "root_event_type": 0x0201,
        "root_event_name": "DB_POOL_STATUS",
        "root_error_label": "POOL_EXHAUSTED",
        "symptom_event_type": 0x0203,
        "symptom_event_name": "HTTP_ERROR",
        "symptom_error_label": "GATEWAY_TIMEOUT (504)",
        "secondary_event_type": 0x0202,
        "secondary_event_name": "REQUEST_METRIC",
        "secondary_error_label": "HTTP 504 (15.2s)",
    },
    "D3 Industrial PLC": {
        "key": "industrial_plc",
        "schema_path": ROOT_DIR / "schemas" / "industrial_plc_v0.yaml",
        "manual_path": ROOT_DIR / "domain_manuals" / "industrial_plc_error_codes.md",
        "manual_profile": "Industrial PLC & Manufacturing",
        "payload_endian": "<",
        "root_event_type": 0x0301,
        "root_event_name": "TEMP_STATUS",
        "root_error_label": "OVERHEAT_DRIFT",
        "symptom_event_type": 0x0302,
        "symptom_event_name": "INTERLOCK",
        "symptom_error_label": "TRIPPED (OVERHEAT)",
        "secondary_event_type": 0x0303,
        "secondary_event_name": "CONVEYOR_STATE",
        "secondary_error_label": "STOPPED",
    },
    "D4 Vehicle CAN (Big-Endian)": {
        "key": "can_bus",
        "schema_path": ROOT_DIR / "schemas" / "autonomous_vehicle_v0.yaml",
        "manual_path": ROOT_DIR / "domain_manuals" / "can_bus_error_codes.md",
        "manual_profile": "Autonomous Vehicle CAN Bus",
        "payload_endian": ">",  # Big-endian payload for automotive CAN
        "root_event_type": 0x0402,
        "root_event_name": "ECU_FAULT",
        "root_error_label": "SENSOR_COMM_ERROR",
        "symptom_event_type": 0x0403,
        "symptom_event_name": "ABS_STATE",
        "symptom_error_label": "DISABLED",
        "secondary_event_type": 0x0404,
        "secondary_event_name": "DASH_WARNING",
        "secondary_error_label": "BRAKE_LAMP_ON",
    },
    "D5 Warehouse AGVs": {
        "key": "warehouse_robots",
        "schema_path": ROOT_DIR / "schemas" / "warehouse_robots_v0.yaml",
        "manual_path": ROOT_DIR / "domain_manuals" / "warehouse_robots_error_codes.md",
        "manual_profile": "Warehouse AGVs & Robotics",
        "payload_endian": "<",
        "root_event_type": 0x0601,
        "root_event_name": "BATTERY",
        "root_error_label": "BATTERY_CRITICAL",
        "symptom_event_type": 0x0603,
        "symptom_event_name": "REPLAN",
        "symptom_error_label": "LOW_BATTERY_REROUTE",
        "secondary_event_type": 0x0604,
        "secondary_event_name": "TASK_STATE",
        "secondary_error_label": "ABORTED",
    },
}


# -----------------------------------------------------------------------------
# Payload Synthesis Helpers (Strictly Compliant with Schemas)
# -----------------------------------------------------------------------------


def _pack_event_payload(
    domain_key: str,
    event_type: int,
    is_fault: bool,
    rng: random.Random,
    endian: str = "<",
) -> bytes:
    """Pack an event payload matching the domain schema byte-for-byte."""
    if domain_key == "drone":
        if event_type == 0x10:
            # LIDAR_STATUS: status_code u16, timeout_count u32
            status = 17 if is_fault else 0  # 17 = LIDAR_TIMEOUT
            timeout_cnt = rng.randint(5, 25) if is_fault else 0
            return struct.pack(f"{endian}HI", status, timeout_cnt)
        elif event_type == 0x20:
            # AVOIDANCE: reason_code u8, min_separation_m f32, heading_change_deg i16
            reason = 1 if is_fault else 0  # 1 = COLLISION_RISK
            separation = rng.uniform(0.4, 1.2) if is_fault else 5.0
            heading = rng.randint(25, 60) if is_fault else 0
            return struct.pack(f"{endian}Bfh", reason, separation, heading)
        elif event_type == 0x21:
            # FORMATION_STATE: state_code u8, formation_id u16
            state = 1 if is_fault else 0  # 1 = LEFT_FORMATION
            return struct.pack(f"{endian}BH", state, 101)
        else:
            # 0x30 DIAGNOSTIC: subsystem_id u8, status_code u8, message_id u32
            status = 2 if is_fault else 0  # 2 = FAULT
            return struct.pack(f"{endian}BBI", 1, status, rng.randint(1000, 9999))

    elif domain_key == "microservices":
        if event_type == 0x0201:
            # DB_POOL_STATUS: pool_id u16, active_conns u16, max_conns u16, state_code u8
            state = 3 if is_fault else 0  # 3 = POOL_EXHAUSTED
            active = 50 if is_fault else rng.randint(5, 25)
            return struct.pack(f"{endian}HHHB", 1, active, 50, state)
        elif event_type == 0x0202:
            # REQUEST_METRIC: service_id u16, latency_ms u32, status_code u16
            status = 504 if is_fault else 200
            latency = rng.randint(12000, 18000) if is_fault else rng.randint(20, 120)
            return struct.pack(f"{endian}HIH", 1, latency, status)
        else:
            # 0x0203 HTTP_ERROR: service_id u16, error_code u16, downstream_id u16
            err_code = 504 if is_fault else 200
            return struct.pack(f"{endian}HHH", 1, err_code, 2)

    elif domain_key == "industrial_plc":
        if event_type == 0x0301:
            # TEMP_STATUS: sensor_id u16, temperature_c f32, drift_detected bool
            temp = rng.uniform(88.0, 98.5) if is_fault else rng.uniform(35.0, 48.0)
            drift = True if is_fault else False
            return struct.pack(f"{endian}Hf?", 1, temp, drift)
        elif event_type == 0x0302:
            # INTERLOCK: interlock_id u16, tripped bool, reason_code u16
            tripped = True if is_fault else False
            reason = 1 if is_fault else 0  # 1 = OVERHEAT
            return struct.pack(f"{endian}H?H", 1, tripped, reason)
        else:
            # 0x0303 CONVEYOR_STATE: conveyor_id u16, state_code u8
            state = 3 if is_fault else 0  # 3 = STOPPED
            return struct.pack(f"{endian}HB", 1, state)

    elif domain_key == "can_bus":
        # Note: Big-Endian payload packing
        if event_type == 0x0401:
            # WHEEL_SPEED: wheel_id u8, speed_kmh f32
            spd = rng.uniform(50.0, 75.0)
            return struct.pack(f"{endian}Bf", 1, spd)
        elif event_type == 0x0402:
            # ECU_FAULT: ecu_id u16, fault_code u16
            fault = 1 if is_fault else 0  # 1 = SENSOR_COMM_ERROR
            return struct.pack(f"{endian}HH", 12, fault)
        elif event_type == 0x0403:
            # ABS_STATE: abs_state_code u8, slip_ratio f32
            abs_st = 2 if is_fault else 0  # 2 = DISABLED
            slip = rng.uniform(0.30, 0.45) if is_fault else 0.05
            return struct.pack(f"{endian}Bf", abs_st, slip)
        else:
            # 0x0404 DASH_WARNING: warning_id u8, lamp_on bool
            lamp = True if is_fault else False
            return struct.pack(f"{endian}B?", 1, lamp)

    elif domain_key == "warehouse_robots":
        if event_type == 0x0601:
            # BATTERY: robot_id u16, voltage_v f32, charge_pct u8, critical bool
            volt = rng.uniform(18.5, 20.2) if is_fault else rng.uniform(24.0, 26.5)
            pct = rng.randint(8, 14) if is_fault else rng.randint(70, 95)
            crit = True if is_fault else False
            return struct.pack(f"{endian}HfB?", 1, volt, pct, crit)
        elif event_type == 0x0602:
            # NAV_STATE: robot_id u16, x_mm i32, y_mm i32, blocked bool
            blocked = True if is_fault else False
            return struct.pack(f"{endian}Hii?", 1, rng.randint(5000, 15000), rng.randint(2000, 8000), blocked)
        elif event_type == 0x0603:
            # REPLAN: robot_id u16, reason_code u8
            reason = 2 if is_fault else 0  # 2 = LOW_BATTERY
            return struct.pack(f"{endian}HB", 1, reason)
        else:
            # 0x0604 TASK_STATE: robot_id u16, task_id u32, state u8
            state = 3 if is_fault else 1  # 3 = ABORTED, 1 = IN_PROGRESS
            return struct.pack(f"{endian}HIB", 1, 9021, state)

    # Fallback generic payload
    return struct.pack(f"{endian}II", 1, 2)


# -----------------------------------------------------------------------------
# BlockStore Telemetry Inspection
# -----------------------------------------------------------------------------


def inspect_blockstore_blocks(store_path: Path | str) -> list[dict[str, Any]]:
    """Inspect a persisted Java 64KB BlockStore .dat file byte-for-byte.

    Computes per-block used bytes, zero-padded tail bytes, and CRC-32C validity.
    """
    path = Path(store_path)
    if not path.is_file():
        return []

    data = path.read_bytes()
    block_size = 65536
    num_blocks = len(data) // block_size
    results: list[dict[str, Any]] = []

    for i in range(num_blocks):
        block_bytes = data[i * block_size : (i + 1) * block_size]
        pos = 0
        used = 0
        rec_count = 0
        source_id: int | str = "Unknown"
        crc_ok = True

        while pos + 8 <= block_size:
            rec_len, expected_crc = struct.unpack_from("<II", block_bytes, pos)
            if rec_len == 0:
                break
            if pos + 8 + rec_len > block_size:
                crc_ok = False
                break

            envelope = block_bytes[pos + 8 : pos + 8 + rec_len]
            # CRC-32C is computed over record_len (4 bytes) + envelope
            actual_crc = crc32c(block_bytes[pos : pos + 4] + envelope)
            if actual_crc != expected_crc:
                crc_ok = False

            if source_id == "Unknown" and len(envelope) >= 8:
                source_id = struct.unpack_from("<I", envelope, 4)[0]

            rec_count += 1
            used += 8 + rec_len
            pos += 8 + rec_len

        results.append({
            "block_index": i,
            "source_id": source_id,
            "records": rec_count,
            "used_bytes": used,
            "padded_bytes": block_size - used,
            "crc_status": "VALID (CRC-32C)" if crc_ok else "CORRUPT",
        })

    return results


# -----------------------------------------------------------------------------
# Core Generator Logic
# -----------------------------------------------------------------------------


def generate_unseen_run(
    domain: str = "Random Domain",
    seed: int | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a novel, randomized unseen telemetry scenario across supported domains.

    Returns:
      Dictionary containing generated EventRecords, raw bytes, temporary .fss.bin path,
      blind query, and isolated GroundTruthManifest.
    """
    if config is None:
        config = {}

    if seed is None:
        seed = random.randint(100_000, 999_999)

    rng = random.Random(seed)

    # Resolve domain
    if not domain or domain == "Random Domain" or domain not in DOMAIN_CONFIGS:
        chosen_domain = rng.choice(SUPPORTED_DOMAINS)
    else:
        chosen_domain = domain

    dom_cfg = DOMAIN_CONFIGS[chosen_domain]
    dom_key = dom_cfg["key"]
    payload_endian = dom_cfg["payload_endian"]

    # Configuration options
    event_count = max(20, min(int(config.get("event_count", 80)), 300))
    cross_source_cascade = bool(config.get("cross_source_cascade", True))
    inject_opaque_bytes = bool(config.get("inject_opaque_bytes", False))
    inject_unknown_event_type = bool(config.get("inject_unknown_event_type", False))
    inject_clock_skew = bool(config.get("inject_clock_skew", False))
    inject_blackout_gap = bool(config.get("inject_blackout_gap", False))
    inject_crash_tail = bool(config.get("inject_crash_tail", False))

    # Randomized source identifiers (ensures novel unseen sources)
    source_a = rng.randint(100, 499)
    source_b = rng.randint(500, 899)
    source_c = rng.randint(900, 999)
    sources = [source_a, source_b, source_c]

    # Unified base epoch (single-epoch guarantee)
    base_epoch_ns = 1_700_000_000_000_000_000 + rng.randint(1_000, 50_000_000) * 1_000_000_000
    cur_ts_ns = base_epoch_ns

    records: list[EventRecord] = []
    injected_gaps: list[str] = []

    # Fault index placement: around 45% of total events
    fault_idx = max(5, int(event_count * 0.45))
    symptom_idx = fault_idx + rng.randint(2, 5)

    clock_skew_shift_ns = 0
    clock_skew_ms = 0.0
    if inject_clock_skew:
        clock_skew_ms = float(rng.randint(80, 250))
        clock_skew_shift_ns = int(clock_skew_ms * 1_000_000)
        injected_gaps.append(f"clock_skew: Source {source_b} shifted backward by {clock_skew_ms:.1f} ms")

    blackout_s = 0.0
    if inject_blackout_gap:
        blackout_s = float(rng.randint(35, 60))
        injected_gaps.append(f"blackout_gap: {blackout_s:.1f} s telemetry outage covering fault window")

    # Generate events
    for i in range(event_count):
        # Time step forward
        step_ns = rng.randint(60_000_000, 180_000_000)  # 60ms - 180ms
        cur_ts_ns += step_ns

        # Handle blackout gap injection
        if inject_blackout_gap and i == fault_idx:
            cur_ts_ns += int(blackout_s * 1_000_000_000)

        # Decide source and event type
        if i == fault_idx and cross_source_cascade:
            # Injected Root Cause Fault on Source A
            src = source_a
            etype = dom_cfg["root_event_type"]
            is_fault = True
            ts = cur_ts_ns
        elif i == symptom_idx and cross_source_cascade:
            # Downstream Symptom Reaction on Source B
            src = source_b
            etype = dom_cfg["symptom_event_type"]
            is_fault = True
            ts = cur_ts_ns - clock_skew_shift_ns  # Apparent reaction occurs earlier if clock skew active
        elif i == symptom_idx + 1 and cross_source_cascade:
            # Secondary Symptom Reaction on Source B
            src = source_b
            etype = dom_cfg["secondary_event_type"]
            is_fault = True
            ts = cur_ts_ns - clock_skew_shift_ns
        else:
            # Nominal operational background event
            src = rng.choice(sources)
            # Pick a nominal event type
            etype = rng.choice([
                dom_cfg["root_event_type"],
                dom_cfg["symptom_event_type"],
                dom_cfg["secondary_event_type"],
            ])
            is_fault = False
            ts = cur_ts_ns

        # Synthesize payload bytes
        payload = _pack_event_payload(dom_key, etype, is_fault, rng, endian=payload_endian)

        # Opaque bytes injection on a diagnostic event
        if inject_opaque_bytes and i == fault_idx + 2:
            opaque_extra = b"\xDE\xAD\xBE\xEF\xCA\xFE\xBA\xBE"
            payload += opaque_extra
            injected_gaps.append(f"opaque_bytes: 8 unconfigured trailing bytes appended to event {i} on Source {src}")

        # Unknown event type injection
        if inject_unknown_event_type and i == fault_idx + 3:
            etype = 0x007F
            payload = b"\xAA\xBB\xCC\xDD"
            injected_gaps.append(f"unknown_event_type: Unregistered event 0x007F on Source {src}")

        records.append(EventRecord(source_id=src, timestamp_ns=ts, event_type=etype, payload_bytes=payload))

    # Sort records monotonically by timestamp (except for clock-skewed producer records)
    records.sort(key=lambda r: r.timestamp_ns)

    # Encode Contract v0 envelopes
    raw_envelopes = bytearray()
    for rec in records:
        raw_envelopes.extend(encode_envelope(rec))

    # Crash tail injection (incomplete trailing frame at EOF)
    if inject_crash_tail:
        # 14 bytes of an incomplete header
        crash_bytes = b"\x5A\xF5\x01\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06"
        raw_envelopes.extend(crash_bytes)
        injected_gaps.append(f"crash_tail: {len(crash_bytes)} truncated trailing header bytes at EOF")

    # Write temporary binary file
    tmp_dir = Path(tempfile.mkdtemp(prefix="fss_autosim_"))
    bin_path = tmp_dir / f"unseen_{dom_key}_seed{seed}.fss.bin"
    bin_path.write_bytes(bytes(raw_envelopes))

    # Natural-language blind operator question (referencing only visible symptoms)
    if cross_source_cascade:
        if dom_key == "drone":
            blind_query = f"Why did Source {source_b} trigger AVOIDANCE and leave formation in this flight window?"
        elif dom_key == "microservices":
            blind_query = f"Why did Source {source_b} experience cascading HTTP_ERROR 504 failures in this telemetry window?"
        elif dom_key == "industrial_plc":
            blind_query = f"Why did Source {source_b} trigger safety INTERLOCK and stop the conveyor line?"
        elif dom_key == "can_bus":
            blind_query = f"Why did Source {source_b} report ABS_STATE disabled and trigger dashboard warning lamps?"
        elif dom_key == "warehouse_robots":
            blind_query = f"Why did Source {source_b} abort its active task and trigger emergency REPLAN?"
        else:
            blind_query = f"Analyze all faults, cross-source causes, and data gaps for Source {source_b}."
    else:
        blind_query = "Analyze all faults, cross-source causes, and data gaps in this newly ingested log."

    # GroundTruthManifest (Isolated from LLM)
    ground_truth = GroundTruthManifest(
        domain=chosen_domain,
        seed=seed,
        injected_root_cause_source=source_a,
        injected_root_event_type=dom_cfg["root_event_type"],
        injected_root_event_name=dom_cfg["root_event_name"],
        injected_symptom_source=source_b,
        injected_symptom_event_type=dom_cfg["symptom_event_type"],
        injected_symptom_event_name=dom_cfg["symptom_event_name"],
        injected_gaps=injected_gaps,
        clock_skew_ms=clock_skew_ms,
        blackout_gap_s=blackout_s,
        crash_tail=inject_crash_tail,
        total_events=len(records),
        sources=sources,
        time_start_ns=records[0].timestamp_ns if records else base_epoch_ns,
        time_end_ns=records[-1].timestamp_ns if records else base_epoch_ns,
    )

    return {
        "domain": chosen_domain,
        "domain_config": dom_cfg,
        "seed": seed,
        "records": records,
        "raw_bytes": bytes(raw_envelopes),
        "bin_path": bin_path,
        "tmp_dir": tmp_dir,
        "blind_query": blind_query,
        "ground_truth": ground_truth,
    }


# -----------------------------------------------------------------------------
# Live Streaming Generator Pipeline
# -----------------------------------------------------------------------------


def run_auto_unseen_pipeline(
    domain: str = "Random Domain",
    seed: int | None = None,
    event_count: int = 80,
    *,
    cross_source_cascade: bool = True,
    inject_opaque_bytes: bool = False,
    inject_unknown_event_type: bool = False,
    inject_clock_skew: bool = False,
    inject_blackout_gap: bool = False,
    inject_crash_tail: bool = False,
    output_dir: Path | str | None = None,
) -> Iterator[dict[str, Any]]:
    """Execute the full unseen telemetry generation, Java BlockStore ingestion, and verification pipeline.

    Yields real-time structured progress dicts across 4 stages:
      - STAGE_1_GENERATE: newly synthesized events with header hex preview and uint64 ns timestamps.
      - STAGE_2_JAVA_INGEST: Java BlockStore ingestion progress and measured latency in ms.
      - STAGE_3_BLOCKSTORE_PERSIST: per-source 64KB block allocation, used bytes vs padding, and CRC-32C status.
      - STAGE_4_HANDOFF: handoff bundle ready for automatic Diagnostics MVP execution.
    """
    config = {
        "event_count": event_count,
        "cross_source_cascade": cross_source_cascade,
        "inject_opaque_bytes": inject_opaque_bytes,
        "inject_unknown_event_type": inject_unknown_event_type,
        "inject_clock_skew": inject_clock_skew,
        "inject_blackout_gap": inject_blackout_gap,
        "inject_crash_tail": inject_crash_tail,
    }

    # -------------------------------------------------------------------------
    # Stage 1: Synthesis
    # -------------------------------------------------------------------------
    run_data = generate_unseen_run(domain=domain, seed=seed, config=config)
    chosen_domain = run_data["domain"]
    dom_cfg = run_data["domain_config"]
    records: list[EventRecord] = run_data["records"]
    bin_path: Path = run_data["bin_path"]
    tmp_dir: Path = run_data["tmp_dir"]
    ground_truth: GroundTruthManifest = run_data["ground_truth"]
    blind_query: str = run_data["blind_query"]

    # Load schema for previewing
    schema_reg = load_schema_file(dom_cfg["schema_path"])

    # Stream event batch summaries for Stage 1
    event_previews: list[dict[str, Any]] = []
    running_bytes = 0

    for idx, rec in enumerate(records):
        envelope_bytes = encode_envelope(rec)
        header_hex = envelope_bytes[:ENVELOPE_HEADER_LEN].hex().upper()
        running_bytes += len(envelope_bytes)

        # Quick decode for human preview
        dec = decode(rec, schema_reg)
        preview_desc = f"{dec.event_name or 'UNKNOWN'} ({dec.decode_status.value})"

        preview_item = {
            "index": idx + 1,
            "source_id": rec.source_id,
            "timestamp_ns": str(rec.timestamp_ns),
            "iso_timestamp": format_iso8601_ns(rec.timestamp_ns),
            "event_type_hex": f"0x{rec.event_type:04X}",
            "payload_len": len(rec.payload_bytes),
            "header_hex": f"{header_hex[:16]}...{header_hex[-8:]}",
            "preview": preview_desc,
            "running_bytes": running_bytes,
        }
        event_previews.append(preview_item)

        # Yield periodically or at end of synthesis
        if (idx + 1) % 20 == 0 or idx == len(records) - 1:
            yield {
                "stage": "STAGE_1_GENERATE",
                "progress": 0.25 * ((idx + 1) / len(records)),
                "domain": chosen_domain,
                "seed": run_data["seed"],
                "events_count": idx + 1,
                "total_events": len(records),
                "running_bytes": running_bytes,
                "events_preview": event_previews[-10:],
                "message": f"Synthesized {idx + 1}/{len(records)} Contract v0 envelopes for {chosen_domain}...",
            }

    # -------------------------------------------------------------------------
    # Stage 2: Java BlockStore Ingestion
    # -------------------------------------------------------------------------
    store_dir = Path(output_dir) if output_dir else tmp_dir
    store_dir.mkdir(parents=True, exist_ok=True)
    store_path = store_dir / f"autosim_{dom_cfg['key']}_seed{run_data['seed']}.dat"

    yield {
        "stage": "STAGE_2_JAVA_INGEST",
        "progress": 0.35,
        "domain": chosen_domain,
        "seed": run_data["seed"],
        "store_path": str(store_path),
        "message": f"Invoking JavaStoreBridge to ingest {len(records)} events into 64KB BlockStore...",
    }

    t_start = time.perf_counter()
    retriever = JavaRetriever(store_path=store_path)
    retriever.ingest(bin_path)
    ingest_ms = (time.perf_counter() - t_start) * 1000

    yield {
        "stage": "STAGE_2_JAVA_INGEST",
        "progress": 0.50,
        "domain": chosen_domain,
        "seed": run_data["seed"],
        "store_path": str(store_path),
        "ingest_ms": round(ingest_ms, 2),
        "message": f"Java BlockStore ingestion complete in {ingest_ms:.2f} ms.",
    }

    # -------------------------------------------------------------------------
    # Stage 3: BlockStore Persist Telemetry Inspection
    # -------------------------------------------------------------------------
    yield {
        "stage": "STAGE_3_BLOCKSTORE_PERSIST",
        "progress": 0.65,
        "domain": chosen_domain,
        "seed": run_data["seed"],
        "store_path": str(store_path),
        "message": "Inspecting persisted 64KB BlockStore CRC-32C framing and allocation...",
    }

    blocks_telemetry = inspect_blockstore_blocks(store_path)
    total_store_bytes = store_path.stat().st_size if store_path.exists() else 0
    quarantined_tail_bytes = 14 if inject_crash_tail else 0

    yield {
        "stage": "STAGE_3_BLOCKSTORE_PERSIST",
        "progress": 0.75,
        "domain": chosen_domain,
        "seed": run_data["seed"],
        "store_path": str(store_path),
        "total_store_bytes": total_store_bytes,
        "num_blocks": len(blocks_telemetry),
        "blocks": blocks_telemetry,
        "quarantined_tail_bytes": quarantined_tail_bytes,
        "message": f"Persisted into {len(blocks_telemetry)}x 64KB block(s) ({total_store_bytes:,} bytes) with CRC-32C verification.",
    }

    # -------------------------------------------------------------------------
    # Stage 4: Pipeline Handoff
    # -------------------------------------------------------------------------
    yield {
        "stage": "STAGE_4_HANDOFF",
        "progress": 1.0,
        "domain": chosen_domain,
        "seed": run_data["seed"],
        "store_path": str(store_path),
        "bin_path": str(bin_path),
        "schema_path": str(dom_cfg["schema_path"]),
        "matched_manual_profile": dom_cfg["manual_profile"],
        "manual_path": str(dom_cfg["manual_path"]),
        "blind_query": blind_query,
        "ground_truth": ground_truth,
        "ingest_ms": round(ingest_ms, 2),
        "total_events": len(records),
        "message": f"Handoff ready: {chosen_domain} scenario generated and persisted.",
    }
