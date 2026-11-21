#!/usr/bin/env python3
"""Multi-Drone Swarm Fault Simulator (REQ-056, SC1, SC5).

Generates synthetic drone swarm telemetry scenarios with realistic fault cascades,
clock skew traps, telemetry blackouts, and nominal controls.

Emits Contract v0 Little-Endian envelopes (*.fss.bin) and companion ground-truth manifests (*.manifest.json).

Supported Scenarios:
  - scenario_298_474:
      Drone 298 LiDAR fault at t0 -> Drone 474 collision avoidance at t0+120ms -> formation drop at t0+250ms.
      Diagnostic event includes 8 opaque vendor bytes.
  - sc5_clock_skew:
      Agent 12 battery critical fault at t0. Agent 7 replans at t0+50ms.
      Agent 7 recorded timestamp is skewed 150ms earlier due to uncompensated clock skew,
      inverting raw temporal order (causality trap).
  - nominal:
      Normal formation patrol with 4 drones, steady attitudes, nominal batteries, zero faults.
  - blackout_gap:
      Drone 52 experiences a 42-second telemetry blackout gap around a critical reboot/fault.
"""

from __future__ import annotations

import argparse
import json
import math
import random
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

# 2026-10-08T10:07:00Z in ns since Unix epoch
BASE_T0_NS = 1_791_454_020_000_000_000
MS_NS = 1_000_000
SEC_NS = 1_000_000_000

OPAQUE_VENDOR_8 = bytes.fromhex("a73c5e91d208f46b")


def format_iso(ts_ns: int) -> str:
    sec = ts_ns // 1_000_000_000
    nsec = ts_ns % 1_000_000_000
    try:
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{nsec:09d}Z"
    except Exception:
        return f"{ts_ns} ns"


# ---------------------------------------------------------------------------
# Envelope Constructors matching schemas/drone_v0.yaml
# ---------------------------------------------------------------------------

def pack_lidar(source_id: int, ts_ns: int, status_code: int, timeout_count: int) -> EventRecord:
    payload = struct.pack("<HI", status_code & 0xFFFF, timeout_count & 0xFFFFFFFF)
    return EventRecord(source_id, ts_ns, EVENT_LIDAR, payload)


def pack_avoidance(source_id: int, ts_ns: int, reason_code: int, min_sep_m: float, heading_change_deg: int) -> EventRecord:
    payload = struct.pack("<Bfh", reason_code & 0xFF, min_sep_m, max(-32768, min(32767, heading_change_deg)))
    return EventRecord(source_id, ts_ns, EVENT_AVOIDANCE, payload)


def pack_formation(source_id: int, ts_ns: int, state_code: int, formation_id: int) -> EventRecord:
    payload = struct.pack("<BH", state_code & 0xFF, formation_id & 0xFFFF)
    return EventRecord(source_id, ts_ns, EVENT_FORMATION, payload)


def pack_diagnostic(source_id: int, ts_ns: int, status_code: int, temp_c: float, opaque: bytes = b"") -> EventRecord:
    payload = struct.pack("<Hf", status_code & 0xFFFF, temp_c) + opaque
    return EventRecord(source_id, ts_ns, EVENT_DIAGNOSTIC, payload)


def pack_attitude(source_id: int, ts_ns: int, roll_deg: float, pitch_deg: float, yaw_deg: float) -> EventRecord:
    payload = struct.pack(">fff", roll_deg, pitch_deg, yaw_deg)
    return EventRecord(source_id, ts_ns, EVENT_ATTITUDE, payload)


def pack_gps(
    source_id: int,
    ts_ns: int,
    status: int = 3,
    num_sats: int = 14,
    hdop_cm: int = 100,
    lat_e7: int = 377749000,
    lng_e7: int = -1224194000,
    alt_cm: int = 2500,
) -> EventRecord:
    payload = struct.pack(">BBHiii", status & 0xFF, num_sats & 0xFF, hdop_cm & 0xFFFF, lat_e7, lng_e7, alt_cm)
    return EventRecord(source_id, ts_ns, EVENT_GPS, payload)


def pack_battery(source_id: int, ts_ns: int, volt_v: float, curr_a: float, rem_pct: float) -> EventRecord:
    payload = struct.pack(">fff", volt_v, curr_a, rem_pct)
    return EventRecord(source_id, ts_ns, EVENT_BATTERY, payload)


def pack_vibration(source_id: int, ts_ns: int, vx: float, vy: float, vz: float, clipping: int = 0) -> EventRecord:
    payload = struct.pack(">fffH", vx, vy, vz, clipping & 0xFFFF)
    return EventRecord(source_id, ts_ns, EVENT_VIBRATION, payload)


def pack_error(source_id: int, ts_ns: int, subsystem_code: int, error_code: int) -> EventRecord:
    payload = struct.pack(">HH", subsystem_code & 0xFFFF, error_code & 0xFFFF)
    return EventRecord(source_id, ts_ns, EVENT_ERROR, payload)


# ---------------------------------------------------------------------------
# Scenario Generators
# ---------------------------------------------------------------------------

def generate_scenario_298_474(seed: int = 42, base_t0: int = BASE_T0_NS) -> tuple[list[EventRecord], dict[str, Any]]:
    """Scenario 1: Drone 298 LiDAR fault -> Drone 474 avoidance maneuver -> formation drop.

    Features:
      - Multi-drone swarm (298, 474, 901)
      - Drone 298 LiDAR timeout (status 17) at t0+3.050s
      - Drone 298 Diagnostic (status 1 DEGRADED, 8 opaque bytes) at t0+3.060s
      - Drone 474 Collision Avoidance maneuver at t0+3.120s
      - Drone 474 Left Formation at t0+3.180s
      - Drone 901 Nominal Control
    """
    rng = random.Random(seed)
    records: list[EventRecord] = []

    # Phase 1: Initial formation heartbeat (t0 + 1.2s)
    records.append(pack_formation(474, base_t0 + 1_200 * MS_NS, 0, 7))
    records.append(pack_formation(298, base_t0 + 1_210 * MS_NS, 0, 7))
    records.append(pack_formation(901, base_t0 + 1_230 * MS_NS, 0, 7))

    # Phase 2: Heartbeats and nominal LiDAR
    records.append(pack_attitude(298, base_t0 + 2_000 * MS_NS, 0.5, -0.2, 85.0))
    records.append(pack_attitude(474, base_t0 + 2_050 * MS_NS, 0.2, -0.1, 85.0))
    records.append(pack_battery(298, base_t0 + 2_200 * MS_NS, 16.1, 11.2, 88.0))
    records.append(pack_battery(474, base_t0 + 2_250 * MS_NS, 16.0, 10.8, 87.5))
    records.append(pack_lidar(298, base_t0 + 2_500 * MS_NS, 0, 0))

    # Phase 3: Root Cause & Cascade
    # Drone 298 LiDAR timeout
    t_fault = base_t0 + 3_050 * MS_NS
    records.append(pack_lidar(298, t_fault, 17, 3))

    # Drone 298 Diagnostic with opaque bytes
    t_diag = base_t0 + 3_060 * MS_NS
    records.append(pack_diagnostic(298, t_diag, 1, 72.4, OPAQUE_VENDOR_8))

    # Drone 474 Avoidance Reaction (+70ms)
    t_avoid = base_t0 + 3_120 * MS_NS
    records.append(pack_avoidance(474, t_avoid, 1, 3.2, -35))

    # Drone 474 leaves formation (+130ms from fault)
    t_form_drop = base_t0 + 3_180 * MS_NS
    records.append(pack_formation(474, t_form_drop, 1, 7))

    # Drone 901 continues formation patrol
    records.append(pack_formation(901, base_t0 + 3_300 * MS_NS, 0, 7))
    records.append(pack_attitude(901, base_t0 + 3_400 * MS_NS, 0.0, 0.0, 85.0))

    # Manifest metadata
    manifest = {
        "scenario": "scenario_298_474",
        "description": "Cross-agent failure cascade: Drone 298 LiDAR timeout causes Drone 474 collision avoidance and formation drop.",
        "seed": seed,
        "base_timestamp_ns": base_t0,
        "agents": [298, 474, 901],
        "root_cause": {
            "source_id": 298,
            "event_type": "LIDAR_STATUS",
            "event_type_id": EVENT_LIDAR,
            "timestamp_ns": t_fault,
            "timestamp_iso": format_iso(t_fault),
            "status_code": 17,
            "status_name": "LIDAR_TIMEOUT",
            "timeout_count": 3,
        },
        "effects": [
            {
                "source_id": 474,
                "event_type": "AVOIDANCE",
                "timestamp_ns": t_avoid,
                "timestamp_iso": format_iso(t_avoid),
                "reason": "COLLISION_RISK",
                "min_separation_m": 3.2,
                "heading_change_deg": -35,
            },
            {
                "source_id": 474,
                "event_type": "FORMATION_STATE",
                "timestamp_ns": t_form_drop,
                "timestamp_iso": format_iso(t_form_drop),
                "state": "LEFT_FORMATION",
                "formation_id": 7,
            },
        ],
        "data_gaps": [
            {
                "source_id": 298,
                "timestamp_ns": t_diag,
                "opaque_bytes_count": 8,
                "description": "Diagnostic record includes 8 unmodeled opaque vendor bytes.",
            }
        ],
    }

    return records, manifest


def generate_sc5_clock_skew(seed: int = 42, base_t0: int = BASE_T0_NS) -> tuple[list[EventRecord], dict[str, Any]]:
    """Scenario 2: SC5 Clock Skew Causality Trap.

    Ground Truth:
      - Agent 12 battery cell failure at true t0
      - Agent 7 replans trajectory at true t0 + 50ms
      - Agent 7's hardware clock has -150ms uncompensated skew
      - Recorded timestamp for Agent 7 replan is (t0 + 50ms) - 150ms = t0 - 100ms
      - Raw timestamps invert true causal ordering!
    """
    records: list[EventRecord] = []

    # True times
    t_true_cause = base_t0
    t_true_avoid = base_t0 + 50 * MS_NS
    t_true_form = base_t0 + 120 * MS_NS

    clock_skew_agent_7_ns = -150 * MS_NS

    # Producer recorded timestamps
    ts_agent_12_fault = t_true_cause
    ts_agent_7_avoid = t_true_avoid + clock_skew_agent_7_ns  # t0 - 100ms !
    ts_agent_7_form = t_true_form + clock_skew_agent_7_ns    # t0 - 30ms !

    # Phase 1: Nominal patrol before fault
    records.append(pack_attitude(12, base_t0 - 500 * MS_NS, 0.0, 0.0, 90.0))
    records.append(pack_attitude(7, base_t0 - 450 * MS_NS + clock_skew_agent_7_ns, 0.0, 0.0, 90.0))

    # In recorded producer order:
    # 1. Agent 7 avoidance appears at t0 - 100ms
    records.append(pack_avoidance(7, ts_agent_7_avoid, 1, 2.5, 45))

    # 2. Agent 7 formation state appears at t0 - 30ms
    records.append(pack_formation(7, ts_agent_7_form, 1, 3))

    # 3. Agent 12 battery fault appears at t0!
    records.append(pack_battery(12, ts_agent_12_fault, 10.2, 38.5, 8.0))
    records.append(pack_error(12, ts_agent_12_fault + 5 * MS_NS, 9, 901))  # Battery critical error

    # Secondary symptoms
    records.append(pack_attitude(12, base_t0 + 200 * MS_NS, 15.0, -10.0, 95.0))

    manifest = {
        "scenario": "sc5_clock_skew",
        "description": "Clock skew causality trap: Agent 12 battery failure caused Agent 7 avoidance, but Agent 7 uncompensated clock skew (-150ms) makes Agent 7's action appear earlier in producer timestamps.",
        "seed": seed,
        "base_timestamp_ns": base_t0,
        "agents": [7, 12],
        "clock_offsets_ns": {
            "12": 0,
            "7": clock_skew_agent_7_ns,
        },
        "ground_truth_causality": {
            "cause": {
                "source_id": 12,
                "event": "BATTERY_CRITICAL",
                "true_time_ns": t_true_cause,
                "producer_timestamp_ns": ts_agent_12_fault,
            },
            "effect": {
                "source_id": 7,
                "event": "AVOIDANCE_REPLAN",
                "true_time_ns": t_true_avoid,
                "producer_timestamp_ns": ts_agent_7_avoid,
            },
            "apparent_producer_order": ["7:AVOIDANCE", "7:FORMATION_STATE", "12:BATTERY_CRITICAL"],
            "true_physical_order": ["12:BATTERY_CRITICAL", "7:AVOIDANCE", "7:FORMATION_STATE"],
            "causality_warning": "No causation can be inferred from raw producer timestamps alone without clock synchronization verification.",
        },
    }

    return records, manifest


def generate_nominal(seed: int = 42, base_t0: int = BASE_T0_NS, num_cycles: int = 25) -> tuple[list[EventRecord], dict[str, Any]]:
    """Scenario 3: Nominal formation patrol with 4 drones (101, 102, 103, 104) and zero faults."""
    rng = random.Random(seed)
    records: list[EventRecord] = []
    agents = [101, 102, 103, 104]

    for cycle in range(num_cycles):
        t_cycle = base_t0 + cycle * 200 * MS_NS
        for idx, agent in enumerate(agents):
            t_agent = t_cycle + idx * 10 * MS_NS
            roll = rng.uniform(-1.0, 1.0)
            pitch = rng.uniform(-1.0, 1.0)
            yaw = 90.0 + rng.uniform(-0.5, 0.5)
            records.append(pack_attitude(agent, t_agent, roll, pitch, yaw))

            volt = 16.2 - (cycle / num_cycles) * 0.4
            curr = 11.0 + rng.uniform(-0.5, 0.5)
            rem = 90.0 - (cycle / num_cycles) * 5.0
            records.append(pack_battery(agent, t_agent + 2 * MS_NS, volt, curr, rem))

            records.append(pack_formation(agent, t_agent + 4 * MS_NS, 0, 42))
            records.append(pack_diagnostic(agent, t_agent + 6 * MS_NS, 0, 38.0 + rng.uniform(0, 3.0)))

    manifest = {
        "scenario": "nominal",
        "description": "Nominal formation patrol across 4 drones with zero faults, stable attitudes, and healthy battery telemetry.",
        "seed": seed,
        "base_timestamp_ns": base_t0,
        "agents": agents,
        "faults_injected": 0,
        "status": "NOMINAL",
    }
    return records, manifest


def generate_blackout_gap(seed: int = 42, base_t0: int = BASE_T0_NS) -> tuple[list[EventRecord], dict[str, Any]]:
    """Scenario 4: Drone 52 experiences a 42-second telemetry blackout gap around a crash/reboot."""
    records: list[EventRecord] = []
    agents = [51, 52, 53]

    # Phase 1: Pre-gap nominal telemetry (0 to 10s)
    for s in range(10):
        t = base_t0 + s * SEC_NS
        for a in agents:
            records.append(pack_attitude(a, t, 0.0, 0.0, 180.0))
            records.append(pack_battery(a, t + 10 * MS_NS, 16.0, 12.0, 80.0))

    # Phase 2: Blackout interval (t0 + 10s to t0 + 52s) - 42 seconds
    gap_start_ns = base_t0 + 10 * SEC_NS
    gap_end_ns = base_t0 + 52 * SEC_NS

    for s in range(10, 52, 2):
        t = base_t0 + s * SEC_NS
        # Drone 51 and 53 continue sending telemetry normally
        records.append(pack_attitude(51, t, 0.1, -0.1, 180.0))
        records.append(pack_attitude(53, t, -0.1, 0.1, 180.0))
        # Drone 52 is completely silent (blackout!)

    # Phase 3: Drone 52 suddenly recovers / reboots at gap_end_ns
    records.append(pack_error(52, gap_end_ns, 1, 999))  # Watchdog reboot error
    records.append(pack_attitude(52, gap_end_ns + 10 * MS_NS, 45.0, -35.0, 220.0))  # Tumble recovery
    records.append(pack_battery(52, gap_end_ns + 20 * MS_NS, 13.8, 25.0, 42.0))

    manifest = {
        "scenario": "blackout_gap",
        "description": "Telemetry blackout gap: Drone 52 telemetry cuts out for 42 seconds between nominal flight and reboot recovery.",
        "seed": seed,
        "base_timestamp_ns": base_t0,
        "agents": agents,
        "blackout_gaps": [
            {
                "source_id": 52,
                "gap_start_ns": gap_start_ns,
                "gap_end_ns": gap_end_ns,
                "duration_ns": gap_end_ns - gap_start_ns,
                "duration_sec": 42.0,
                "start_iso": format_iso(gap_start_ns),
                "end_iso": format_iso(gap_end_ns),
                "description": "Complete loss of telemetry signal across crash/reboot interval.",
            }
        ],
    }

    return records, manifest


# ---------------------------------------------------------------------------
# Simulator Runner
# ---------------------------------------------------------------------------

SCENARIOS = {
    "scenario_298_474": generate_scenario_298_474,
    "sc5_clock_skew": generate_sc5_clock_skew,
    "nominal": generate_nominal,
    "blackout_gap": generate_blackout_gap,
}


def simulate_scenario(scenario_name: str, out_path: Path, seed: int = 42) -> dict[str, Any]:
    """Simulate a swarm scenario, write .fss.bin and companion .manifest.json."""
    if scenario_name not in SCENARIOS:
        raise ValueError(f"Unknown scenario '{scenario_name}'. Available: {list(SCENARIOS.keys())}")

    gen_fn = SCENARIOS[scenario_name]
    records, manifest = gen_fn(seed=seed)

    # Sort records by timestamp_ns unless it's the clock skew scenario testing arrival order
    if scenario_name != "sc5_clock_skew":
        records.sort(key=lambda r: r.timestamp_ns)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    envelopes_bytes = bytearray()
    for r in records:
        envelopes_bytes.extend(encode_envelope(r))
    out_path.write_bytes(bytes(envelopes_bytes))

    start_ns = records[0].timestamp_ns if records else 0
    end_ns = records[-1].timestamp_ns if records else 0

    manifest.update({
        "output_file": str(out_path),
        "total_events": len(records),
        "time_start_ns": start_ns,
        "time_end_ns": end_ns,
        "time_start_iso": format_iso(start_ns),
        "time_end_iso": format_iso(end_ns),
    })

    manifest_path = out_path.with_name(f"{out_path.stem}.manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "scenario": scenario_name,
        "output": str(out_path),
        "manifest": str(manifest_path),
        "events_written": len(records),
        "time_start_ns": start_ns,
        "time_end_ns": end_ns,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Drone Swarm Fault Simulator (REQ-056, SC1, SC5)")
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        default="scenario_298_474",
        help="Scenario to simulate (default: scenario_298_474)",
    )
    parser.add_argument("--output", type=Path, default=None, help="Output .fss.bin path")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for generation")
    parser.add_argument("--all", action="store_true", help="Generate all scenarios into target directory")

    args = parser.parse_args()

    default_dir = PROJECT_ROOT / "test_data" / "simulated"

    if args.all:
        target_dir = args.output if (args.output and args.output.is_dir()) else default_dir
        results = []
        for sname in SCENARIOS:
            out_file = target_dir / f"{sname}.fss.bin"
            res = simulate_scenario(sname, out_file, seed=args.seed)
            results.append(res)
        print(json.dumps(results, indent=2))
    else:
        out_file = args.output if args.output else (default_dir / f"{args.scenario}.fss.bin")
        res = simulate_scenario(args.scenario, out_file, seed=args.seed)
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
