#!/usr/bin/env python3
"""Generate 6 unseen synthetic test binary files matching envelope.cfg byte-for-byte.

Header layout (20 bytes, Big-Endian):
  0-3:   Agent ID (UINT32)
  4-11:  Timestamp (UINT64)
  12-15: Payload Length (UINT32)
  16-19: Event Type (UINT32)
  20-*:  Payload bytes

Scenarios:
  1. unseen_01_esc_desync_compass_trap.bin (motor thrust drop -> secondary compass spike; causation trap)
  2. unseen_02_blackout_data_gap.bin (42-second unrecorded blackout across crash trigger)
  3. unseen_03_uint64_burst_multi_block.bin (>192KB burst across >3 64KB blocks with uint64 >= 2^63 and 100 ns spacing)
  4. unseen_04_multi_agent_entity_swap.bin (3 agents with concurrent distinct faults)
  5. unseen_05_silent_baro_ekf_drift.bin (400-event gradual drift with no explicit error code)
  6. unseen_06_corrupt_tail_out_of_order.bin (out-of-order timestamps + truncated tail frame at EOF)
"""

from __future__ import annotations

import json
import struct
from pathlib import Path


HEADER_STRUCT = struct.Struct(">IQII")

# Event Types matching schemas/drone_v0.yaml
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


def pack_frame(agent_id: int, timestamp: int, event_type: int, payload: bytes) -> bytes:
    """Pack an FSS envelope frame."""
    return HEADER_STRUCT.pack(agent_id & 0xFFFFFFFF, timestamp & 0xFFFFFFFFFFFFFFFF, len(payload), event_type) + payload


def generate_unseen_01_esc_desync_compass_trap(out_path: Path) -> dict:
    """Scenario 1: ESC desync / thrust drop leading to secondary magnetic compass spike."""
    frames = []
    base_ts = 1719000000000
    agent_id = 1

    # Phase 1: Nominal flight (50 events)
    for i in range(50):
        ts = base_ts + i * 20
        # Attitude: level
        frames.append(pack_frame(agent_id, ts, EVENT_ATTITUDE, struct.pack(">fff", 0.5, -0.2, 90.0)))
        # Battery: 16.0V, 18.0A, 85%
        frames.append(pack_frame(agent_id, ts + 2, EVENT_BATTERY, struct.pack(">fff", 16.0, 18.0, 85.0)))
        # EKF: low variance
        frames.append(pack_frame(agent_id, ts + 5, EVENT_EKF, struct.pack(">fff", 0.02, 0.01, 0.01)))

    # Phase 2: Root cause - Motor 3 ESC desync & severe vibration
    desync_ts = base_ts + 50 * 20
    # Vibration spike (clipping)
    frames.append(pack_frame(agent_id, desync_ts, EVENT_VIBRATION, struct.pack(">fffH", 8.5, 9.2, 48.0, 45)))
    # ESC Error event (Subsystem 9 = MOTOR/ESC, Error 3 = DESYNC)
    frames.append(pack_frame(agent_id, desync_ts + 2, EVENT_ERROR, struct.pack(">HH", 9, 3)))
    # Extreme attitude tumble
    frames.append(pack_frame(agent_id, desync_ts + 5, EVENT_ATTITUDE, struct.pack(">fff", 65.4, -42.1, 145.0)))

    # Phase 3: Consequent secondary symptom - High compass variance caused by electrical back-EMF / tumble
    for i in range(30):
        ts = desync_ts + 10 + i * 20
        # Compass variance spike > 0.90 (secondary symptom trap)
        frames.append(pack_frame(agent_id, ts, EVENT_EKF, struct.pack(">fff", 0.96, 0.82, 0.75)))
        frames.append(pack_frame(agent_id, ts + 5, EVENT_ATTITUDE, struct.pack(">fff", 85.0, -60.0, 210.0)))

    data = b"".join(frames)
    out_path.write_bytes(data)
    return {
        "filename": out_path.name,
        "description": "ESC desync causation trap with secondary compass variance spike",
        "total_events": len(frames),
        "file_size_bytes": len(data),
    }


def generate_unseen_02_blackout_data_gap(out_path: Path) -> dict:
    """Scenario 2: 42-second unrecorded blackout telemetry gap across crash trigger."""
    frames = []
    base_ts = 1719001000000
    agent_id = 2

    # Pre-gap nominal operation (60 events)
    for i in range(60):
        ts = base_ts + i * 50
        frames.append(pack_frame(agent_id, ts, EVENT_GPS, struct.pack(">BBHiii", 3, 14, 120, 377749000, -1224194000, 15000)))
        frames.append(pack_frame(agent_id, ts + 10, EVENT_BATTERY, struct.pack(">fff", 15.8, 14.5, 72.0)))

    # 42-second blackout gap (42,000 ms)
    post_gap_ts = base_ts + 60 * 50 + 42000

    # Post-gap crash state
    frames.append(pack_frame(agent_id, post_gap_ts, EVENT_ERROR, struct.pack(">HH", 1, 99)))  # Crash / power-cut recovery
    frames.append(pack_frame(agent_id, post_gap_ts + 10, EVENT_BATTERY, struct.pack(">fff", 0.0, 0.0, 0.0)))
    frames.append(pack_frame(agent_id, post_gap_ts + 20, EVENT_DIAGNOSTIC, struct.pack("<Hf", 2, 85.0)))  # FAULT status

    data = b"".join(frames)
    out_path.write_bytes(data)
    return {
        "filename": out_path.name,
        "description": "42-second unrecorded telemetry blackout across crash event",
        "total_events": len(frames),
        "file_size_bytes": len(data),
        "gap_seconds": 42.0,
    }


def generate_unseen_03_uint64_burst_multi_block(out_path: Path) -> dict:
    """Scenario 3: >192KB burst across >3 64KB blocks with uint64 >= 2^63 and 100 ns spacing."""
    frames = []
    # 2^63 = 9223372036854775808
    base_ts_uint64 = 9223372036854775808 + 1000000
    agent_id = 3

    # Generate ~6000 frames to easily exceed 3 * 64KB (196,608 bytes)
    # Each frame: 20-byte header + 16-byte payload = 36 bytes. 6000 * 36 = 216,000 bytes (~210 KB).
    target_frames = 6000
    for i in range(target_frames):
        ts = base_ts_uint64 + (i * 100)  # 100 ns increments
        # GPS status frame (16-byte payload)
        payload = struct.pack(">BBHiii", 3, 16, 95, 377749000 + i, -1224194000 + i, 12000 + (i % 500))
        frames.append(pack_frame(agent_id, ts, EVENT_GPS, payload))

    data = b"".join(frames)
    out_path.write_bytes(data)
    return {
        "filename": out_path.name,
        "description": "Large multi-block burst (>192KB) across >3 64KB blocks with uint64 >= 2^63 timestamps",
        "total_events": len(frames),
        "file_size_bytes": len(data),
        "blocks_crossed": len(data) // 65536 + 1,
    }


def generate_unseen_04_multi_agent_entity_swap(out_path: Path) -> dict:
    """Scenario 4: 3 distinct agents with concurrent distinct failure modes."""
    frames = []
    base_ts = 1719003000000

    # 100 interleaved rounds
    for i in range(100):
        ts = base_ts + i * 50

        # Agent 101: GPS Glitch
        if i < 40:
            frames.append(pack_frame(101, ts, EVENT_GPS, struct.pack(">BBHiii", 3, 12, 110, 370000000, -1220000000, 10000)))
        else:
            frames.append(pack_frame(101, ts, EVENT_GPS, struct.pack(">BBHiii", 0, 3, 9999, 370000000, -1220000000, 10000)))
            frames.append(pack_frame(101, ts + 2, EVENT_ERROR, struct.pack(">HH", 5, 2)))  # GPS Loss

        # Agent 102: Battery Exhaustion / Voltage Collapse
        if i < 60:
            frames.append(pack_frame(102, ts + 10, EVENT_BATTERY, struct.pack(">fff", 15.2 - (i * 0.05), 16.0, 60.0 - (i * 0.6))))
        else:
            frames.append(pack_frame(102, ts + 10, EVENT_BATTERY, struct.pack(">fff", 9.1, 28.0, 2.0)))
            frames.append(pack_frame(102, ts + 12, EVENT_ERROR, struct.pack(">HH", 2, 4)))  # Undervoltage

        # Agent 103: Lidar obstruction / collision warning
        if i < 50:
            frames.append(pack_frame(103, ts + 20, EVENT_LIDAR, struct.pack("<HI", 0, 0)))
        else:
            frames.append(pack_frame(103, ts + 20, EVENT_LIDAR, struct.pack("<HI", 17, i - 40)))  # LIDAR_TIMEOUT
            frames.append(pack_frame(103, ts + 22, EVENT_AVOIDANCE, struct.pack("<Bfh", 1, 0.45, -35)))

    data = b"".join(frames)
    out_path.write_bytes(data)
    return {
        "filename": out_path.name,
        "description": "3 agents with concurrent isolated failure modes (GPS loss, undervoltage, lidar obstacle)",
        "total_events": len(frames),
        "file_size_bytes": len(data),
        "agents": [101, 102, 103],
    }


def generate_unseen_05_silent_baro_ekf_drift(out_path: Path) -> dict:
    """Scenario 5: 400-event gradual altitude / EKF drift without any explicit error code."""
    frames = []
    base_ts = 1719004000000
    agent_id = 5

    for i in range(400):
        ts = base_ts + i * 25

        if i < 100:
            # Nominal
            alt = 20000
            comp_var = 0.02
            vel_var = 0.01
            pos_var = 0.01
        else:
            # Gradual silent divergence
            drift_step = i - 100
            alt = 20000 + (drift_step * 35)  # Baro drift
            comp_var = min(0.99, 0.02 + (drift_step * 0.003))
            vel_var = min(0.99, 0.01 + (drift_step * 0.003))
            pos_var = min(0.99, 0.01 + (drift_step * 0.0032))

        frames.append(pack_frame(agent_id, ts, EVENT_GPS, struct.pack(">BBHiii", 3, 15, 100, 377749000, -1224194000, alt)))
        frames.append(pack_frame(agent_id, ts + 5, EVENT_EKF, struct.pack(">fff", comp_var, vel_var, pos_var)))

    data = b"".join(frames)
    out_path.write_bytes(data)
    return {
        "filename": out_path.name,
        "description": "400-event silent baro/EKF drift without explicit error events",
        "total_events": len(frames),
        "file_size_bytes": len(data),
    }


def generate_unseen_06_corrupt_tail_out_of_order(out_path: Path) -> dict:
    """Scenario 6: Out-of-order timestamps + truncated partial frame at EOF."""
    frames = []
    base_ts = 1719005000000
    agent_id = 6

    # 150 events with jittered/reversed timestamps
    timestamps = [base_ts + i * 20 for i in range(150)]
    # Create non-monotonic sequence: swap adjacent pairs
    for i in range(10, 140, 4):
        timestamps[i], timestamps[i + 1] = timestamps[i + 1], timestamps[i]
        timestamps[i + 2], timestamps[i - 2] = timestamps[i - 2], timestamps[i + 2]

    for i, ts in enumerate(timestamps):
        frames.append(pack_frame(agent_id, ts, EVENT_ATTITUDE, struct.pack(">fff", 1.0, -1.0, float(i % 360))))

    complete_data = b"".join(frames)

    # Append corrupt tail: 7 bytes of a header claiming 16 bytes of payload, then truncated
    corrupt_tail = struct.pack(">IQ", agent_id, base_ts + 50000)[:7]
    corrupted_data = complete_data + corrupt_tail

    out_path.write_bytes(corrupted_data)
    return {
        "filename": out_path.name,
        "description": "Non-monotonic out-of-order timestamps followed by truncated tail frame at EOF",
        "total_events": len(frames),
        "file_size_bytes": len(corrupted_data),
        "truncated_tail_bytes": len(corrupt_tail),
    }


def main():
    unseen_dir = Path("test_data/unseen")
    unseen_dir.mkdir(parents=True, exist_ok=True)

    manifest = {}

    scenarios = [
        ("unseen_01_esc_desync_compass_trap.bin", generate_unseen_01_esc_desync_compass_trap),
        ("unseen_02_blackout_data_gap.bin", generate_unseen_02_blackout_data_gap),
        ("unseen_03_uint64_burst_multi_block.bin", generate_unseen_03_uint64_burst_multi_block),
        ("unseen_04_multi_agent_entity_swap.bin", generate_unseen_04_multi_agent_entity_swap),
        ("unseen_05_silent_baro_ekf_drift.bin", generate_unseen_05_silent_baro_ekf_drift),
        ("unseen_06_corrupt_tail_out_of_order.bin", generate_unseen_06_corrupt_tail_out_of_order),
    ]

    print("Generating unseen binary test files in test_data/unseen/...")
    for fname, generator_fn in scenarios:
        out_path = unseen_dir / fname
        meta = generator_fn(out_path)
        manifest[fname] = meta
        print(f"  -> Generated {fname}: {meta['total_events']} events ({meta['file_size_bytes']} bytes)")

    manifest_path = unseen_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {manifest_path}")


if __name__ == "__main__":
    main()
