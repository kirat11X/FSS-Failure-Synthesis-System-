"""Clock Skew Compensator and Cross-Source Causality Analysis.

Strict adherence to Hard Rule 5 & Section 19:
  - NEVER mutate, alter, or replace the original producer timestamp_ns on any
    EventRecord, DecodedEvent, or evidence dictionary.
  - Detect non-monotonic timestamp ordering across different event sources / producers.
  - Infer Lamport logical order / happens-before constraints across causal pairs
    (e.g., Drone fault triggering avoidance, AGV battery critical vs replan).
  - Estimate bounded pairwise clock skew interval [min_offset_ns, max_offset_ns].
  - Provide structured advisory metadata without inventing global truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from fss_py.model import DecodedEvent, EventRecord

# Typical maximum physical reaction/propagation delay assumed for bounding causal skew (1 second)
DEFAULT_MAX_REACTION_DELAY_NS: int = 1_000_000_000
# Minimum physical reaction/propagation delay (0 ns)
DEFAULT_MIN_REACTION_DELAY_NS: int = 0
# Maximum temporal correlation window across which causal triggers and reactions interact (60 seconds)
MAX_CAUSAL_CORRELATION_WINDOW_NS: int = 60_000_000_000


@dataclass(frozen=True)
class AmbiguousCausalPair:
    """Record of a pair of cross-source events with non-monotonic or inverted timestamps."""
    cause_event_id: str
    effect_event_id: str
    source_cause: int
    source_effect: int
    timestamp_cause_ns: int
    timestamp_effect_ns: int
    raw_offset_ns: int  # timestamp_cause_ns - timestamp_effect_ns (> 0 indicates inversion)
    inversion_detected: bool
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause_event_id": self.cause_event_id,
            "effect_event_id": self.effect_event_id,
            "source_cause": self.source_cause,
            "source_effect": self.source_effect,
            "timestamp_cause_ns": self.timestamp_cause_ns,
            "timestamp_effect_ns": self.timestamp_effect_ns,
            "raw_offset_ns": self.raw_offset_ns,
            "inversion_detected": self.inversion_detected,
            "description": self.description,
        }


@dataclass
class ClockSkewReport:
    """Structured advisory clock skew report (REQ-035, REQ-048).

    Guarantees:
      - Producer timestamps are NEVER mutated.
      - Advisory only: captures ambiguity and bounds without asserting unverified global truth.
    """
    skew_detected: bool
    estimated_offset_bounds_ns: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)
    ambiguous_pairs: list[dict[str, Any]] = field(default_factory=list)
    lamport_order_notes: list[str] = field(default_factory=list)
    clock_caveat: str = ""
    sources: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skew_detected": self.skew_detected,
            "estimated_offset_bounds_ns": {
                f"{k[0]}->{k[1]}": list(v) for k, v in self.estimated_offset_bounds_ns.items()
            },
            "ambiguous_pairs": list(self.ambiguous_pairs),
            "lamport_order_notes": list(self.lamport_order_notes),
            "clock_caveat": self.clock_caveat,
            "sources": list(self.sources),
        }


def _extract_event_info(ev: dict[str, Any] | DecodedEvent | EventRecord) -> dict[str, Any]:
    """Extract standard attributes from dict, DecodedEvent, or EventRecord without mutating."""
    if isinstance(ev, DecodedEvent):
        return {
            "event_id": ev.event_id,
            "source_id": int(ev.source_id),
            "timestamp_ns": int(ev.timestamp_ns),
            "event_name": ev.event_name or f"0x{ev.event_type:02X}",
            "event_type": ev.event_type,
            "fields": dict(ev.fields) if ev.fields else {},
            "decode_status": str(ev.decode_status),
        }
    elif isinstance(ev, EventRecord):
        return {
            "event_id": f"rec-{ev.source_id}-{ev.timestamp_ns}",
            "source_id": int(ev.source_id),
            "timestamp_ns": int(ev.timestamp_ns),
            "event_name": f"0x{ev.event_type:02X}",
            "event_type": ev.event_type,
            "fields": {},
            "decode_status": "RAW_RECORD",
        }
    elif isinstance(ev, dict):
        ts = ev.get("timestamp_ns")
        if ts is None:
            ts = ev.get("timestamp", 0)
        return {
            "event_id": str(ev.get("event_id", f"ev-{ev.get('source_id', 0)}-{ts}")),
            "source_id": int(ev.get("source_id", 0)),
            "timestamp_ns": int(ts),
            "event_name": str(ev.get("event_name") or ev.get("event_type") or "UNKNOWN"),
            "event_type": ev.get("event_type"),
            "fields": dict(ev.get("fields", {})),
            "decode_status": str(ev.get("decode_status", "SUCCESS")),
        }
    else:
        raise TypeError(f"Unsupported event object type: {type(ev).__name__}")


def _is_causal_trigger(ev: dict[str, Any]) -> bool:
    """Determine if an event is a fault, error, alarm, or trigger candidate."""
    name = str(ev.get("event_name", "")).upper()
    fields = ev.get("fields", {})

    # Trigger patterns across multiple domains (drone, robot, PLC, microservice, vehicle)
    trigger_keywords = (
        "TIMEOUT", "LIDAR", "FAULT", "ERROR", "CRITICAL", "ALARM", "WARN",
        "BATTERY", "OVERHEAT", "DRIFT", "DISCONNECT", "DEGRADED", "FAIL",
        "STALL", "LEAK", "PRESSURE", "SENSOR_FAULT", "HEARTBEAT_LOST"
    )
    if any(kw in name for kw in trigger_keywords):
        return True

    # Check fields
    status_name = str(fields.get("status_name", "")).upper()
    status_str = str(fields.get("status", "")).upper()
    if any(kw in status_name for kw in ("TIMEOUT", "FAIL", "ERROR", "CRITICAL", "FAULT")):
        return True
    if any(kw in status_str for kw in ("TIMEOUT", "FAIL", "ERROR", "CRITICAL", "FAULT")):
        return True

    timeout_count = fields.get("timeout_count")
    if isinstance(timeout_count, (int, float)) and timeout_count > 0:
        return True

    status_code = fields.get("status_code")
    if isinstance(status_code, (int, float)) and status_code > 0:
        return True

    battery_pct = fields.get("battery_pct") or fields.get("battery_percent") or fields.get("battery_soc")
    if isinstance(battery_pct, (int, float)) and battery_pct <= 20:
        return True

    error_code = fields.get("error_code") or fields.get("fault_code")
    if isinstance(error_code, (int, float)) and error_code > 0:
        return True

    return False


def _is_causal_reaction(ev: dict[str, Any]) -> bool:
    """Determine if an event is a defensive maneuver, reaction, replan, or consequence candidate."""
    name = str(ev.get("event_name", "")).upper()
    fields = ev.get("fields", {})

    reaction_keywords = (
        "AVOIDANCE", "REPLAN", "FORMATION", "BRAKE", "FAILOVER", "ABORT",
        "EMERGENCY", "REROUTE", "DISENGAGE", "INTERLOCK", "EVACUATE",
        "COLLISION", "FALLBACK", "SAFE_STOP", "DEVIATION"
    )
    if any(kw in name for kw in reaction_keywords):
        return True

    # Check fields
    reason = str(fields.get("reason", "")).upper()
    state = str(fields.get("state", "")).upper()
    action = str(fields.get("action", "")).upper()

    if any(kw in reason for kw in ("COLLISION", "RISK", "PROXIMITY", "OBSTACLE", "EMERGENCY")):
        return True
    if any(kw in state for kw in ("LEFT_FORMATION", "REPLAN", "EMERGENCY", "ABORT", "DEVIATED", "STOPPED")):
        return True
    if any(kw in action for kw in ("AVOID", "REPLAN", "STOP", "FAILOVER", "BRAKE")):
        return True

    reason_code = fields.get("reason_code")
    if isinstance(reason_code, (int, float)) and reason_code > 0:
        return True

    return False


def analyze_clock_skew(
    events: Sequence[dict[str, Any] | DecodedEvent | EventRecord],
    *,
    max_reaction_delay_ns: int = DEFAULT_MAX_REACTION_DELAY_NS,
    min_reaction_delay_ns: int = DEFAULT_MIN_REACTION_DELAY_NS,
    max_correlation_window_ns: int = MAX_CAUSAL_CORRELATION_WINDOW_NS,
) -> ClockSkewReport:
    """Analyze cross-source events for non-monotonic timestamp ordering and clock skew.

    Strictly satisfies:
      - Hard Rule 5 & Section 19: Timestamps on input events are NEVER mutated.
      - Detects apparent time inversions between causal trigger-reaction pairs.
      - Estimates bounded pairwise offset intervals [min_offset_ns, max_offset_ns].
      - Infers Lamport logical order / happens-before constraints across sources.

    Args:
      events: Sequence of DecodedEvents, raw EventRecords, or dicts.
      max_reaction_delay_ns: Upper bound on reaction delay (default 1s).
      min_reaction_delay_ns: Lower bound on reaction delay (default 0ns).
      max_correlation_window_ns: Maximum temporal window for cross-source correlation.

    Returns:
      ClockSkewReport with structured advisory metadata.
    """
    if not events:
        return ClockSkewReport(
            skew_detected=False,
            estimated_offset_bounds_ns={},
            ambiguous_pairs=[],
            lamport_order_notes=[],
            clock_caveat="",
            sources=[],
        )

    # 1. Normalize without modifying originals
    parsed_events = [_extract_event_info(e) for e in events]
    sources = sorted({e["source_id"] for e in parsed_events})

    # If single source, no cross-source clock skew is possible
    if len(sources) <= 1:
        return ClockSkewReport(
            skew_detected=False,
            estimated_offset_bounds_ns={},
            ambiguous_pairs=[],
            lamport_order_notes=[],
            clock_caveat="",
            sources=sources,
        )

    # 2. Identify potential causal triggers and reactions
    triggers = [e for e in parsed_events if _is_causal_trigger(e)]
    reactions = [e for e in parsed_events if _is_causal_reaction(e)]

    skew_detected = False
    offset_bounds: dict[tuple[int, int], tuple[int, int]] = {}
    ambiguous_pairs: list[dict[str, Any]] = []
    lamport_notes: list[str] = []

    # 3. Check pairwise causal correlations across distinct sources
    for trig in triggers:
        s_trig = trig["source_id"]
        t_trig = trig["timestamp_ns"]
        id_trig = trig["event_id"]
        name_trig = trig["event_name"]

        for react in reactions:
            s_react = react["source_id"]
            if s_react == s_trig:
                continue  # Same source: governed by local producer clock monotonicity

            t_react = react["timestamp_ns"]
            id_react = react["event_id"]
            name_react = react["event_name"]

            # Check if within plausible correlation window in either direction
            diff_ns = t_trig - t_react
            if abs(diff_ns) > max_correlation_window_ns:
                continue

            # In Lamport happens-before: trig -> react (cause precedes effect).
            # If t_trig > t_react, producer timestamps show reaction BEFORE trigger!
            # This is a non-monotonic ordering inversion caused by clock skew.
            if diff_ns > 0:
                skew_detected = True
                inversion = True

                # Bound offset: diff_ns = t_trig - t_react > 0.
                # In true time, t_react_true >= t_trig_true + min_delay.
                # So Clock(trig) - Clock(react) >= (t_trig - t_react) + min_delay.
                min_skew = diff_ns + min_reaction_delay_ns
                max_skew = diff_ns + max_reaction_delay_ns

                pair_key = (s_trig, s_react)
                if pair_key not in offset_bounds:
                    offset_bounds[pair_key] = (min_skew, max_skew)
                else:
                    curr_min, curr_max = offset_bounds[pair_key]
                    # To explain the earliest reaction, clock skew must be at least the largest diff_ns
                    offset_bounds[pair_key] = (max(curr_min, min_skew), max(curr_max, max_skew))

                desc = (
                    f"Causal inversion detected: Trigger '{name_trig}' on Source {s_trig} "
                    f"has timestamp {t_trig} ns, which is {diff_ns} ns AFTER reactive event "
                    f"'{name_react}' on Source {s_react} (timestamp {t_react} ns)."
                )

                amb_pair = AmbiguousCausalPair(
                    cause_event_id=id_trig,
                    effect_event_id=id_react,
                    source_cause=s_trig,
                    source_effect=s_react,
                    timestamp_cause_ns=t_trig,
                    timestamp_effect_ns=t_react,
                    raw_offset_ns=diff_ns,
                    inversion_detected=True,
                    description=desc,
                )
                ambiguous_pairs.append(amb_pair.to_dict())

                note = (
                    f"Inferred Lamport logical order: Source {s_trig} ({name_trig}:{id_trig}) -> "
                    f"Source {s_react} ({name_react}:{id_react}) based on fault-reaction semantics. "
                    f"Producer timestamps are inverted (Source {s_trig} clock runs fast by at least {diff_ns} ns)."
                )
                if note not in lamport_notes:
                    lamport_notes.append(note)

    # 4. Formulate clock caveat
    if skew_detected:
        # Highlight the primary skewed pair in the caveat
        primary_skew_items = []
        for (s_a, s_b), (b_min, b_max) in offset_bounds.items():
            primary_skew_items.append(f"Source {s_a} and Source {s_b} (estimated drift: [{b_min}, {b_max}] ns)")
        skew_summary = "; ".join(primary_skew_items)

        clock_caveat = (
            f"⏱️ Non-monotonic ordering detected between {skew_summary}. "
            f"Causation cannot be inferred from raw timestamps alone."
        )
    else:
        # Standard multi-source caveat when multiple sources are present without explicit inverted trigger-reaction
        clock_caveat = (
            f"Events come from {len(sources)} sources ({', '.join(map(str, sources))}). Producer "
            f"clocks are not synchronized: each timestamp_ns reflects only its own producer's "
            f"clock. Comparing timestamps across sources does not establish order or causality, "
            f"so any cross-source sequence must be stated as uncertain."
        )

    return ClockSkewReport(
        skew_detected=skew_detected,
        estimated_offset_bounds_ns=offset_bounds,
        ambiguous_pairs=ambiguous_pairs,
        lamport_order_notes=lamport_notes,
        clock_caveat=clock_caveat,
        sources=sources,
    )
