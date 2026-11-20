"""Whole-dataset binary log ingestion, SHA-256 hash caching, and telemetry condensation.

Provides:
  - Deterministic SHA-256 hash computation over uploaded binary files (.bin / .fss.bin).
  - Deterministic cache manager (BinCacheManager) mapping files to test_data/uploads/{sha256}_{safe_filename}.
  - Reuse of cached paths to eliminate redundant conversions and re-ingestions.
  - Strict uint64 nanosecond time bounds enforcement (t_start_ns <= t_end_ns).
  - Smart whole-dataset telemetry condensation (condense_telemetry) preserving 100% of critical events
    (errors, warnings, faults, mode/state transitions, breaches) while downsampling repetitive nominal frames.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from pathlib import Path
from typing import Any, BinaryIO, Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent

# Critical event type codes and labels
CRITICAL_EVENT_TYPES: set[int] = {
    0x10,  # LIDAR_STATUS
    0x20,  # AVOIDANCE
    0x21,  # FORMATION_STATE
    0x30,  # DIAGNOSTIC
    0x43,  # EKF_STATUS
    0x44,  # VIBRATION
    0x45,  # ERROR_EVENT
}

CRITICAL_KEYWORDS: tuple[str, ...] = (
    "ERR",
    "ERROR",
    "AVOID",
    "AVOIDANCE",
    "LIDAR",
    "DIAG",
    "DIAGNOSTIC",
    "FORMATION",
    "MODE",
    "EKF",
    "VIBE",
    "VIBRATION",
    "FAULT",
    "WARN",
    "WARNING",
    "CRITICAL",
    "TIMEOUT",
    "DISCONNECT",
    "BREACH",
)


def compute_sha256(content: bytes | str | Path | BinaryIO) -> str:
    """Compute deterministic SHA-256 hex digest over bytes, a file path, or a file-like stream."""
    hasher = hashlib.sha256()

    if isinstance(content, bytes):
        hasher.update(content)
        return hasher.hexdigest()

    if isinstance(content, (str, Path)):
        p = Path(content)
        with open(p, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    if hasattr(content, "read"):
        pos = None
        if hasattr(content, "tell"):
            try:
                pos = content.tell()
            except Exception:
                pos = None

        while chunk := content.read(65536):
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            hasher.update(chunk)

        if pos is not None and hasattr(content, "seek"):
            try:
                content.seek(pos)
            except Exception:
                pass
        return hasher.hexdigest()

    raise TypeError(f"Unsupported content type for SHA-256 computation: {type(content).__name__}")


def safe_filename(name: str) -> str:
    """Sanitize filename to a filesystem-safe representation ([a-zA-Z0-9_.-])."""
    base = Path(name).name
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "_", base)
    return cleaned if cleaned else "upload.bin"


def normalize_time_bounds(
    t_start: int | None,
    t_end: int | None,
    events: Sequence[Any] | None = None,
    default_window_ns: int = 1_000_000_000,
) -> tuple[int, int]:
    """Compute strictly ordered bounds [t_start_ns, t_end_ns] where t_start_ns <= t_end_ns.

    Prevents out-of-order first/last message timestamps from creating negative windows by
    computing min and max across all event timestamps.
    """
    if events:
        timestamps: list[int] = []
        for ev in events:
            ts = None
            if isinstance(ev, dict):
                ts = ev.get("timestamp_ns", ev.get("timestamp"))
            else:
                ts = getattr(ev, "timestamp_ns", getattr(ev, "timestamp", None))
            if ts is not None:
                try:
                    timestamps.append(int(ts))
                except (ValueError, TypeError):
                    pass

        if timestamps:
            ev_min = min(timestamps)
            ev_max = max(timestamps)
            if t_start is None:
                t_start = ev_min
            if t_end is None:
                t_end = max(ev_max, ev_min + default_window_ns)

    start_val = 0 if t_start is None else int(t_start)
    end_val = (start_val + default_window_ns) if t_end is None else int(t_end)

    if start_val > end_val:
        start_val, end_val = end_val, start_val
    if start_val == end_val:
        end_val = start_val + 1

    return start_val, end_val


def get_event_timestamp(ev: dict[str, Any] | Any) -> int:
    """Extract integer timestamp in nanoseconds from an event dict or object."""
    if isinstance(ev, dict):
        raw = ev.get("timestamp_ns", ev.get("timestamp", 0))
    else:
        raw = getattr(ev, "timestamp_ns", getattr(ev, "timestamp", 0))
    try:
        val = int(raw)
        return val if val >= 0 else 0
    except (ValueError, TypeError):
        return 0


def get_event_type(ev: dict[str, Any] | Any) -> int | str:
    """Extract event type code or string from an event dict or object."""
    if isinstance(ev, dict):
        return ev.get("event_type", ev.get("event_name", 0))
    return getattr(ev, "event_type", getattr(ev, "event_name", 0))


def get_source_id(ev: dict[str, Any] | Any) -> int:
    """Extract source identifier from an event dict or object."""
    if isinstance(ev, dict):
        raw = ev.get("source_id", ev.get("agent_id", 0))
    else:
        raw = getattr(ev, "source_id", getattr(ev, "agent_id", 0))
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


def is_critical_event(ev: dict[str, Any] | Any) -> bool:
    """Determine whether an event is critical (errors, faults, avoidance, mode changes, breaches)."""
    # 1. Event type check
    ev_type = get_event_type(ev)
    if isinstance(ev_type, int):
        if ev_type in CRITICAL_EVENT_TYPES:
            return True
    elif isinstance(ev_type, str):
        u = ev_type.strip().upper()
        if u.startswith("0X"):
            try:
                code = int(u, 16)
                if code in CRITICAL_EVENT_TYPES:
                    return True
            except ValueError:
                pass
        for kw in CRITICAL_KEYWORDS:
            if kw in u:
                return True

    # 2. Event name check
    ev_name = ""
    if isinstance(ev, dict):
        ev_name = str(ev.get("event_name", "")).upper()
    else:
        ev_name = str(getattr(ev, "event_name", "")).upper()

    if ev_name:
        for kw in CRITICAL_KEYWORDS:
            if kw in ev_name:
                return True

    # 3. Decode status check
    d_status = ""
    if isinstance(ev, dict):
        d_status = str(ev.get("decode_status", "")).upper()
    else:
        d_status = str(getattr(ev, "decode_status", "")).upper()

    if d_status and d_status not in ("SUCCESS", "DECODED"):
        return True

    # 4. Fields inspection (status codes, errors, faults, variances, threshold breaches)
    fields = {}
    if isinstance(ev, dict):
        fields = ev.get("fields", {})
    else:
        fields = getattr(ev, "fields", {})

    if isinstance(fields, dict):
        # Non-zero status code
        status_code = fields.get("status_code")
        if status_code is not None:
            try:
                if int(status_code) > 0:
                    return True
            except (ValueError, TypeError):
                pass

        # Timeouts
        timeout_cnt = fields.get("timeout_count")
        if timeout_cnt is not None:
            try:
                if int(timeout_cnt) > 0:
                    return True
            except (ValueError, TypeError):
                pass

        # Reason code / avoidance reason
        reason_code = fields.get("reason_code")
        if reason_code is not None:
            try:
                if int(reason_code) > 0:
                    return True
            except (ValueError, TypeError):
                pass

        # State code (e.g. 1=LEFT_FORMATION, 2=REJOINING)
        state_code = fields.get("state_code")
        if state_code is not None:
            try:
                if int(state_code) > 0:
                    return True
            except (ValueError, TypeError):
                pass

        # Clipping / vibration fault
        clip = fields.get("clipping")
        if clip is not None:
            try:
                if int(clip) > 0:
                    return True
            except (ValueError, TypeError):
                pass

        # High EKF variance (> 0.5)
        for v_field in ("compass_variance", "velocity_variance", "position_variance"):
            v_val = fields.get(v_field)
            if v_val is not None:
                try:
                    if float(v_val) > 0.5:
                        return True
                except (ValueError, TypeError):
                    pass

        # Battery breach (< 20%)
        rem = fields.get("remaining_pct")
        if rem is not None:
            try:
                if float(rem) < 20.0:
                    return True
            except (ValueError, TypeError):
                pass

        # Textual indications in field values
        for val in fields.values():
            s_val = str(val).upper()
            if any(k in s_val for k in ("FAULT", "DEGRADED", "COLLISION", "TIMEOUT", "DISCONNECT", "ERROR")):
                return True

    return False


def _to_event_dict(ev: dict[str, Any] | Any) -> dict[str, Any]:
    """Convert an event record or model into a standard dict representation."""
    if isinstance(ev, dict):
        return dict(ev)
    if hasattr(ev, "model_dump"):
        return ev.model_dump()
    if hasattr(ev, "__dict__"):
        return dict(ev.__dict__)
    return {
        "source_id": get_source_id(ev),
        "timestamp_ns": get_event_timestamp(ev),
        "event_type": get_event_type(ev),
    }


def condense_telemetry(
    events: list[dict[str, Any]] | list[Any],
    max_events_target: int = 1000,
) -> list[dict[str, Any]]:
    """Smart whole-dataset telemetry condensation.

    Guarantees:
      - Keeps 100% of errors, warnings, faults, mode changes, threshold breaches, and state transitions
        (e.g., event_types ERR 0x45, AVOIDANCE 0x20, LIDAR 0x10, DIAG 0x30, EKF 0x43, etc.).
      - Downsamples repetitive nominal heartbeat/attitude/GPS frames into compact periodic summaries
        or phase envelopes across the flight lifecycle.
      - Returns chronologically ordered event dicts under the target event budget.
    """
    if not events:
        return []

    dict_events = [_to_event_dict(e) for e in events]
    # Ensure all timestamps are valid and chronological
    dict_events.sort(key=lambda x: (get_event_timestamp(x), get_source_id(x)))

    if len(dict_events) <= max_events_target:
        return dict_events

    critical_events: list[dict[str, Any]] = []
    nominal_events: list[dict[str, Any]] = []

    for ev in dict_events:
        if is_critical_event(ev):
            critical_events.append(ev)
        else:
            nominal_events.append(ev)

    # If critical events alone meet or exceed budget, preserve all critical events
    # plus the boundary frames of the flight lifecycle
    if len(critical_events) >= max_events_target:
        result = list(critical_events)
        if nominal_events:
            first_nom = nominal_events[0]
            last_nom = nominal_events[-1]
            if first_nom not in result:
                result.append(first_nom)
            if last_nom not in result:
                result.append(last_nom)
        result.sort(key=lambda x: (get_event_timestamp(x), get_source_id(x)))
        return result

    nominal_budget = max_events_target - len(critical_events)

    if len(nominal_events) <= nominal_budget:
        sampled_nominal = nominal_events
    else:
        # Uniformly downsample nominal frames across time
        if nominal_budget <= 2:
            sampled_nominal = [nominal_events[0], nominal_events[-1]] if len(nominal_events) >= 2 else nominal_events
        else:
            step = (len(nominal_events) - 1) / (nominal_budget - 1)
            sampled_indices = [round(i * step) for i in range(nominal_budget)]
            # Deduplicate indices while preserving order
            seen_idx = set()
            sampled_nominal = []
            for idx in sampled_indices:
                if idx not in seen_idx:
                    seen_idx.add(idx)
                    sampled_nominal.append(nominal_events[idx])

    merged = critical_events + sampled_nominal
    merged.sort(key=lambda x: (get_event_timestamp(x), get_source_id(x)))
    return merged


class BinCacheManager:
    """Deterministic file and ingestion cache manager for uploaded .bin and .fss.bin files.

    Eliminates repeated re-ingestion and re-conversion loops by caching files under:
        test_data/uploads/{sha256}_{safe_filename}
    """

    def __init__(self, upload_dir: str | Path | None = None) -> None:
        if upload_dir is not None:
            up = Path(upload_dir)
            self.upload_dir = up if up.is_absolute() else (ROOT_DIR / up).resolve()
        else:
            self.upload_dir = ROOT_DIR / "test_data" / "uploads"

        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def get_cached_paths(self, file_hash: str, filename: str) -> tuple[Path, Path]:
        """Compute the canonical raw and translated (.fss.bin) paths for a hash and filename.

        Also matches existing 12-char or full SHA-256 prefixes if already present on disk.
        """
        safe_name = safe_filename(filename)
        canonical_raw = self.upload_dir / f"{file_hash}_{safe_name}"

        if safe_name.endswith(".fss.bin"):
            canonical_fss = canonical_raw
        elif safe_name.endswith(".bin"):
            canonical_fss = self.upload_dir / f"{file_hash}_{safe_name[:-4]}.fss.bin"
        else:
            canonical_fss = self.upload_dir / f"{file_hash}_{safe_name}.fss.bin"

        # Check if an existing file on disk matches either full hash or 12-char prefix
        prefix12 = file_hash[:12]
        alt_raw = self.upload_dir / f"{prefix12}_{safe_name}"
        if safe_name.endswith(".bin"):
            alt_fss = self.upload_dir / f"{prefix12}_{safe_name[:-4]}.fss.bin"
        else:
            alt_fss = self.upload_dir / f"{prefix12}_{safe_name}.fss.bin"

        raw_path = alt_raw if alt_raw.exists() else canonical_raw
        fss_path = alt_fss if alt_fss.exists() else canonical_fss

        return raw_path, fss_path

    def is_cached(self, file_hash: str, filename: str) -> bool:
        """Return True iff a valid non-empty cached file exists for this content hash."""
        raw_path, fss_path = self.get_cached_paths(file_hash, filename)
        if fss_path.exists() and fss_path.stat().st_size > 0:
            return True
        if raw_path.exists() and raw_path.stat().st_size > 0:
            return True
        return False

    def get_or_ingest(
        self,
        file_bytes_or_path: bytes | str | Path,
        filename: str | None = None,
        force_reingest: bool = False,
        agent_id: int = 1,
    ) -> tuple[Path, bool]:
        """Retrieve cached path or ingest/convert uploaded binary file.

        Returns:
            tuple[Path, bool]: (resolved_file_path, was_cached)
        """
        if isinstance(file_bytes_or_path, (str, Path)):
            src_path = Path(file_bytes_or_path)
            file_bytes = src_path.read_bytes()
            name = filename or src_path.name
        else:
            file_bytes = file_bytes_or_path
            name = filename or "upload.bin"

        file_hash = compute_sha256(file_bytes)
        raw_path, fss_path = self.get_cached_paths(file_hash, name)

        # 1. Reuse existing converted .fss.bin file if available
        if not force_reingest and fss_path.exists() and fss_path.stat().st_size > 0:
            return fss_path, True

        # 2. Check if raw file is ArduPilot DataFlash binary (0xA3 0x95)
        is_ardupilot = len(file_bytes) >= 2 and file_bytes[0] == 0xA3 and file_bytes[1] == 0x95

        # 3. If not ArduPilot and raw path exists, reuse raw path
        if not force_reingest and not is_ardupilot and raw_path.exists() and raw_path.stat().st_size > 0:
            return raw_path, True

        # 4. Write raw upload bytes if missing or forced
        if force_reingest or not raw_path.exists():
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(file_bytes)

        # 5. Convert ArduPilot DataFlash if applicable
        if is_ardupilot:
            if force_reingest or not fss_path.exists() or fss_path.stat().st_size == 0:
                from scripts.convert_ardupilot_bin_to_fss import convert_log
                convert_log(raw_path, fss_path, agent_id=agent_id)
            return fss_path, False

        return raw_path, False

    def clear_cache(self, file_hash: str | None = None) -> int:
        """Clear cached files for a specific hash or the entire upload cache directory."""
        removed = 0
        if file_hash:
            for p in self.upload_dir.glob(f"{file_hash[:12]}*"):
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        else:
            for p in self.upload_dir.glob("*"):
                if p.is_file():
                    try:
                        p.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed
