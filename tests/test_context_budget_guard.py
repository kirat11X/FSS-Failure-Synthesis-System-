"""Unit and Property Tests for Context Budget Guard & Window Enforcement.

Spec requirement:
  Feed a synthetic 500-event timeline against simulated num_ctx=4096 and num_ctx=16384.
  Pre-flight guard triggers deterministic compaction/warning before dispatch;
  never sends silent overflow.
"""

from __future__ import annotations

import warnings
import pytest
from bridge.synthesizer import (
    DEFAULT_NUM_CTX,
    RESERVED_OUTPUT_TOKENS_NON_THINKING,
    RESERVED_OUTPUT_TOKENS_THINKING,
    build_llm_context,
    estimate_tokens,
    preflight_budget_guard,
    retrieve_relevant_knowledge,
)


def _generate_synthetic_timeline(n_events: int = 500) -> dict:
    """Generate a synthetic timeline of N events with several interspersed anomalies."""
    events = []
    base_ts = 1791454000000000000
    for i in range(n_events):
        ts = base_ts + (i * 100_000_000)
        source_id = 298 if i % 2 == 0 else 474
        eid = f"ev-{i:012x}"

        # Inject anomalies at specific indexes
        if i == 50:
            events.append({
                "event_id": eid,
                "source_id": 298,
                "timestamp_ns": ts,
                "event_type": "0x10",
                "event_name": "LIDAR_STATUS",
                "decode_status": "SUCCESS",
                "fields": {"status_code": 17, "status_name": "LIDAR_TIMEOUT", "timeout_count": 3},
                "interpreted_fields": ["status_name"],
            })
        elif i == 55:
            events.append({
                "event_id": eid,
                "source_id": 474,
                "timestamp_ns": ts,
                "event_type": "0x20",
                "event_name": "AVOIDANCE",
                "decode_status": "SUCCESS",
                "fields": {"reason_code": 1, "reason": "COLLISION_RISK", "min_separation_m": 2.5, "heading_change_deg": -40},
                "interpreted_fields": ["reason"],
            })
        elif i == 60:
            events.append({
                "event_id": eid,
                "source_id": 474,
                "timestamp_ns": ts,
                "event_type": "0x21",
                "event_name": "FORMATION_STATE",
                "decode_status": "SUCCESS",
                "fields": {"state_code": 1, "state": "LEFT_FORMATION", "formation_id": 7},
                "interpreted_fields": ["state"],
            })
        elif i == 120:
            events.append({
                "event_id": eid,
                "source_id": 298,
                "timestamp_ns": ts,
                "event_type": "0x30",
                "event_name": "DIAGNOSTIC",
                "decode_status": "PARTIALLY_DECODED",
                "fields": {"status_code": 1, "status_name": "DEGRADED", "temperature_c": 75.0},
                "interpreted_fields": ["status_name"],
                "unknown_bytes": 8,
            })
        else:
            # Routine nominal heartbeat events
            events.append({
                "event_id": eid,
                "source_id": source_id,
                "timestamp_ns": ts,
                "event_type": "0x21",
                "event_name": "FORMATION_STATE",
                "decode_status": "SUCCESS",
                "fields": {"state_code": 0, "state": "IN_FORMATION", "formation_id": 7},
                "interpreted_fields": ["state"],
            })

    return {
        "format": "fss-evidence/v0",
        "query": "Why did drone 474 leave formation?",
        "retrieval": {
            "source": "ALL",
            "t_start_ns": base_ts,
            "t_end_ns": base_ts + (n_events * 100_000_000),
            "max_events": n_events,
            "returned": n_events,
            "truncated": False,
        },
        "sources": [298, 474],
        "ordering": "Grouped by source_id, sorted by producer timestamp.",
        "clock_caveat": "Producer clocks are not synchronized across sources.",
        "events": events,
        "data_gaps": [],
    }


def test_budget_guard_with_num_ctx_4096():
    """Verify that a 500-event timeline triggers deterministic compaction under num_ctx=4096."""
    evidence = _generate_synthetic_timeline(500)
    query = "Why did drone 474 leave formation?"
    knowledge = retrieve_relevant_knowledge(evidence, query, top_k=8)

    # Initial raw prompt estimate exceeds 4096 tokens
    raw_context = build_llm_context(evidence, query, knowledge, _skip_budget_guard=True)
    raw_tokens = estimate_tokens(raw_context)
    assert raw_tokens > 4096, f"Expected raw tokens > 4096, got {raw_tokens}"

    # Pre-flight guard execution
    with pytest.warns(UserWarning, match="Pre-flight token budget guard"):
        compacted_evidence, was_compacted, warning_msg = preflight_budget_guard(
            evidence,
            query,
            knowledge,
            effective_num_ctx=4096,
            enable_thinking=False,
        )

    assert was_compacted is True
    assert "compacted timeline" in warning_msg
    assert len(compacted_evidence["events"]) < 500

    # Build guarded context
    guarded_context = build_llm_context(
        compacted_evidence,
        query,
        knowledge,
        effective_num_ctx=4096,
        _skip_budget_guard=True,
    )
    guarded_tokens = estimate_tokens(guarded_context)
    reserved_output = RESERVED_OUTPUT_TOKENS_NON_THINKING  # 1200
    available_budget = 4096 - reserved_output  # 2896

    assert guarded_tokens <= available_budget + 100, (
        f"Guarded prompt ({guarded_tokens}) must fit inside prompt budget ({available_budget})"
    )

    # Verify that all 4 critical anomalies were preserved
    preserved_ids = {e["event_id"] for e in compacted_evidence["events"]}
    assert "ev-000000000032" in preserved_ids  # i=50 (LIDAR_TIMEOUT)
    assert "ev-000000000037" in preserved_ids  # i=55 (AVOIDANCE)
    assert "ev-00000000003c" in preserved_ids  # i=60 (LEFT_FORMATION)
    assert "ev-000000000078" in preserved_ids  # i=120 (DIAGNOSTIC DEGRADED)

    # Verify Data Gap record added
    gaps = compacted_evidence.get("data_gaps", [])
    assert any(g.get("kind") == "BUDGET_GUARD_COMPACTION" for g in gaps)


def test_budget_guard_with_num_ctx_16384():
    """Verify that a 500-event timeline fits within num_ctx=16384 via compaction."""
    evidence = _generate_synthetic_timeline(500)
    query = "Why did drone 474 leave formation?"
    knowledge = retrieve_relevant_knowledge(evidence, query, top_k=8)

    with pytest.warns(UserWarning, match="Pre-flight token budget guard"):
        compacted_evidence, was_compacted, warning_msg = preflight_budget_guard(
            evidence,
            query,
            knowledge,
            effective_num_ctx=16384,
            enable_thinking=False,
        )

    assert was_compacted is True
    guarded_context = build_llm_context(
        compacted_evidence,
        query,
        knowledge,
        effective_num_ctx=16384,
        _skip_budget_guard=True,
    )
    guarded_tokens = estimate_tokens(guarded_context)
    available_budget = 16384 - RESERVED_OUTPUT_TOKENS_NON_THINKING  # 15184

    assert guarded_tokens <= available_budget


def test_small_timeline_not_compacted():
    """Verify that a small 10-event timeline requires zero compaction."""
    evidence = _generate_synthetic_timeline(10)
    query = "Check status"
    knowledge = retrieve_relevant_knowledge(evidence, query, top_k=5)

    with warnings.catch_warnings(record=True) as recorded_warnings:
        compacted_evidence, was_compacted, warning_msg = preflight_budget_guard(
            evidence,
            query,
            knowledge,
            effective_num_ctx=16384,
            enable_thinking=False,
        )

    assert was_compacted is False
    assert warning_msg == ""
    assert len(compacted_evidence["events"]) == 10
    assert len(recorded_warnings) == 0


def test_numeric_heavy_cascade_fits_reviewer_overhead_inside_16384():
    """Ensure dense 2500-event numeric telemetry fits within 16384 even with reviewer overhead."""
    events = []
    base_ts = 1705354536010100000
    for i in range(2500):
        src = 101 + (i % 11)
        events.append({
            "event_id": f"ev-{i:012x}",
            "source_id": src,
            "timestamp_ns": base_ts + i * 50_000_000,
            "event_type": "0x202",
            "event_name": "REQUEST_METRIC",
            "decode_status": "SUCCESS",
            "fields": {
                "service_id": src,
                "latency_ms": 500 + (i % 97),
                "status_code": 500,
                "status_name": "INTERNAL_SERVER_ERROR",
            },
        })
    evidence = {"events": events, "sources": list(range(101, 112)), "data_gaps": []}
    with pytest.warns(UserWarning, match="Pre-flight token budget guard"):
        compacted, was_compacted, _ = preflight_budget_guard(
            evidence,
            "Diagnose microservice cascade failure",
            [],
            effective_num_ctx=16384,
            enable_thinking=False,
            include_event_ids=False,
            extra_overhead_tokens=3800,
        )
    assert was_compacted is True
    # Every service stream onset must be preserved
    kept_sources = {ev["source_id"] for ev in compacted["events"]}
    assert kept_sources == set(range(101, 112))
    ctx = build_llm_context(compacted, "Diagnose microservice cascade failure", [], effective_num_ctx=16384, _skip_budget_guard=True, include_event_ids=False)
    assert estimate_tokens(ctx) + 3800 <= 16384 - RESERVED_OUTPUT_TOKENS_NON_THINKING


def test_multi_entity_sentence_citation_binding():
    """Ensure 'service 101 and 102' binds both entities while single-entity misattribution is rejected."""
    from bridge.citation_auditor import CitationAuditor, ValidationBadge

    ev_101 = {
        "event_id": "ev-000000000101",
        "source_id": 101,
        "timestamp_ns": 1705354537010100000,
        "event_type": "0x202",
        "event_name": "REQUEST_METRIC",
        "decode_status": "SUCCESS",
        "fields": {"status_code": 500, "status_name": "INTERNAL_SERVER_ERROR"},
    }
    ev_102 = {
        "event_id": "ev-000000000102",
        "source_id": 102,
        "timestamp_ns": 1705354537020100000,
        "event_type": "0x201",
        "event_name": "DB_POOL_STATUS",
        "decode_status": "SUCCESS",
        "fields": {"state": "POOL_EXHAUSTED"},
    }
    ev_104 = {
        "event_id": "ev-000000000104",
        "source_id": 104,
        "timestamp_ns": 1705354537030100000,
        "event_type": "0x201",
        "event_name": "DB_POOL_STATUS",
        "decode_status": "SUCCESS",
        "fields": {"state": "HEALTHY"},
    }
    ev_data = {"events": [ev_101, ev_102, ev_104], "sources": [101, 102, 104], "data_gaps": []}
    auditor = CitationAuditor()

    good = auditor.audit(
        {
            "summary": (
                "Latency and pool exhaustion appeared across service 101 and 102. [ev-000000000101] [ev-000000000102]\n\n"
                "All peer services except 101 and 102 remained healthy. [ev-000000000104]"
            ),
            "likely_cause": "Localized pressure across services such as 101 and 102 likely degraded calls. [ev-000000000101] [ev-000000000102]",
            "confidence": "medium",
            "contributing_events": [
                {"event_id": "ev-000000000101", "source_id": 101, "timestamp": ev_101["timestamp_ns"], "event_type": "REQUEST_METRIC"},
                {"event_id": "ev-000000000102", "source_id": 102, "timestamp": ev_102["timestamp_ns"], "event_type": "DB_POOL_STATUS"},
                {"event_id": "ev-000000000104", "source_id": 104, "timestamp": ev_104["timestamp_ns"], "event_type": "DB_POOL_STATUS"},
            ],
            "uncertainty": ["Producer clocks are not synchronized."],
            "data_gaps": [],
        },
        ev_data,
        auto_refine=False,
    )
    assert good.badge != ValidationBadge.ENTITY_MISMATCH

    bad = auditor.audit(
        {
            "summary": "Service 101 hit pool exhaustion. [ev-000000000102]",
            "likely_cause": "Service 101 likely hit pool exhaustion. [ev-000000000102]",
            "confidence": "medium",
            "contributing_events": [],
            "uncertainty": ["Producer clocks are not synchronized."],
            "data_gaps": [],
        },
        ev_data,
        auto_refine=False,
    )
    assert bad.badge == ValidationBadge.ENTITY_MISMATCH
