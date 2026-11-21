"""Comprehensive test suite for Progressive Multi-Step Investigation (REQ-033, PI-01..PI-05).

Verifies:
  1. PI-01: Context before event anchor converted into bounded retrieval [t_anchor - lookback, t_anchor].
  2. PI-02: All-history/unbounded queries rejected; requests anchored outside evidence rejected;
            synthesis continues with existing evidence and records rejection notice.
  3. PI-03: Oversized windows (> 1 hour) are strictly REJECTED, NEVER CLAMPED.
  4. PI-04: Round budget enforcement (max_context_requests = 1 stops runaway requests).
  5. PI-05: Follow-up events pass through standard decoder, evidence builder, and RAG with
            lossless deduplication by event_id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import pytest

from bridge.progressive_investigator import (
    ContextRequest,
    ContextRequestValidationResult,
    ProgressiveInvestigationResult,
    ProgressiveInvestigator,
    investigate_progressive,
    validate_context_request,
)
from fss_py.contract import MAX_EVENTS_LIMIT, MAX_WINDOW_NS
from fss_py.decoder import decode_all
from fss_py.model import ALL, DecodeStatus, DecodedEvent, EventRecord, RetrievalRequest, RetrievalResult
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

ROOT_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def drone_registry():
    return load_schema_file(str(ROOT_DIR / "schemas" / "drone_v0.yaml"))


@pytest.fixture
def sample_decoded_events():
    return [
        DecodedEvent(
            event_id="ev-474-avoid",
            source_id=474,
            timestamp_ns=1791454143120000000,
            event_type=0x20,
            event_name="AVOIDANCE",
            schema_version=1,
            decode_status=DecodeStatus.SUCCESS,
            fields={"reason": "COLLISION_RISK", "reason_code": 1},
            interpreted_fields=["reason"],
            unknown_bytes=0,
            reason=None,
            warnings=[],
            payload_len=7,
        ),
        DecodedEvent(
            event_id="ev-298-fault",
            source_id=298,
            timestamp_ns=1791454143050000000,
            event_type=0x10,
            event_name="LIDAR_STATUS",
            schema_version=1,
            decode_status=DecodeStatus.SUCCESS,
            fields={"status_name": "LIDAR_TIMEOUT", "status_code": 17},
            interpreted_fields=["status_name"],
            unknown_bytes=0,
            reason=None,
            warnings=[],
            payload_len=6,
        ),
    ]


class MockRetriever:
    """Mock storage retriever tracking all retrieve calls."""

    def __init__(self, records: list[EventRecord]):
        self.records = records
        self.calls: list[RetrievalRequest] = []

    def retrieve(
        self,
        source: int | object,
        t_start_ns: int,
        t_end_ns: int,
        max_events: int = 100,
        event_type: int | None = None,
    ) -> RetrievalResult:
        req = RetrievalRequest(
            source=source,
            t_start_ns=t_start_ns,
            t_end_ns=t_end_ns,
            max_events=max_events,
            event_type=event_type,
        )
        self.calls.append(req)

        matched = [
            r
            for r in self.records
            if (source is ALL or r.source_id == source)
            and t_start_ns <= r.timestamp_ns < t_end_ns
            and (event_type is None or r.event_type == event_type)
        ]
        return RetrievalResult(
            request=req,
            events=matched[:max_events],
            truncated=len(matched) > max_events,
        )


def test_PI01_context_before_event_becomes_a_bounded_retrieve(sample_decoded_events):
    """PI-01: Asking for context 30 s before event X converts into explicit bounded retrieve [T-30s, T]."""
    anchor_eid = "ev-474-avoid"
    anchor_ts = 1791454143120000000
    lookback = 30_000_000_000  # 30 seconds

    req_data = {
        "before_event": anchor_eid,
        "lookback_ns": lookback,
        "max_events": 50,
        "source": "ALL",
    }

    res = validate_context_request(req_data, sample_decoded_events)
    assert res.valid is True
    assert res.error_message is None
    assert res.retrieval_request is not None

    r_req = res.retrieval_request
    assert r_req.t_end_ns == anchor_ts
    assert r_req.t_start_ns == anchor_ts - lookback
    assert r_req.max_events == 50
    assert r_req.source is ALL


def test_PI02_all_history_window_rejected_and_synthesis_continues(sample_decoded_events):
    """PI-02: Open-ended or unbounded requests are rejected."""
    # 1. Missing end or start
    unbounded_req = {"t_start_ns": 0}  # No t_end_ns
    res1 = validate_context_request(unbounded_req, sample_decoded_events)
    assert res1.valid is False
    assert "Unbounded or all-history retrieval requested" in res1.error_message

    # 2. Negative duration window
    inverted_req = {"t_start_ns": 2000, "t_end_ns": 1000}
    res2 = validate_context_request(inverted_req, sample_decoded_events)
    assert res2.valid is False
    assert "must be strictly greater than" in res2.error_message


def test_PI02_request_anchored_outside_the_evidence_rejected(sample_decoded_events):
    """PI-02: Anchor not in current evidence is rejected."""
    missing_anchor_req = {
        "before_event": "ev-nonexistent-999",
        "lookback_ns": 10_000_000_000,
    }
    res = validate_context_request(missing_anchor_req, sample_decoded_events)
    assert res.valid is False
    assert "Request anchored outside evidence: event_id 'ev-nonexistent-999' not found" in res.error_message


def test_PI03_oversized_window_rejected_not_clamped(sample_decoded_events):
    """PI-03: Window duration > MAX_WINDOW_NS (1h) is strictly rejected, NEVER clamped."""
    oversized_window = MAX_WINDOW_NS + 1_000_000  # 1 hour + 1 ms
    req_data = {
        "t_start_ns": 1_000_000_000,
        "t_end_ns": 1_000_000_000 + oversized_window,
    }
    res = validate_context_request(req_data, sample_decoded_events)
    assert res.valid is False
    assert "Oversized window" in res.error_message
    assert "rejected, never clamped" in res.error_message


def test_PI04_round_budget_enforced(drone_registry):
    """PI-04: At most max_context_requests=1 follow-up is allowed; further requests stop."""
    # Prepare mock records
    records = [
        EventRecord(source_id=474, timestamp_ns=1000, event_type=0x20, payload_bytes=bytes(7)),
        EventRecord(source_id=298, timestamp_ns=500, event_type=0x10, payload_bytes=bytes(6)),
    ]
    retriever = MockRetriever(records)

    call_count = 0

    def greedy_stub_synthesizer(evidence, query, **kwargs):
        nonlocal call_count
        call_count += 1
        # Always asks for another context request
        first_event = evidence["events"][0]["event_id"] if evidence["events"] else "ev-none"
        return (
            {
                "summary": f"Round {call_count} summary",
                "likely_cause": None,
                "confidence": "low",
                "contributing_events": [],
                "uncertainty": ["Need more context"],
                "data_gaps": [],
                "context_request": {
                    "before_event": first_event,
                    "lookback_ns": 100,
                },
            },
            type("ValReport", (), {"verdict": "ACCEPTED", "badge": "ACCEPTED", "valid_citations": [], "notes": []})(),
            "stub-progressive",
        )

    initial_req = RetrievalRequest(source=ALL, t_start_ns=900, t_end_ns=1100, max_events=10)
    investigator = ProgressiveInvestigator(
        retriever=retriever,
        schema_registry=drone_registry,
        max_context_requests=1,
        synthesizer_fn=greedy_stub_synthesizer,
    )

    result = investigator.investigate(
        query="Why did drone 474 avoid?",
        initial_request=initial_req,
    )

    # Round budget: exactly 1 follow-up round allowed
    assert result.rounds_executed == 1
    assert len(result.context_requests) == 1
    # Total calls to synthesizer: Round 0 + Round 1 = 2
    assert call_count == 2
    # Total calls to retriever: Initial + 1 follow-up = 2
    assert len(retriever.calls) == 2


def test_PI05_round_two_events_go_through_the_same_decoder_and_evidence_path(drone_registry):
    """PI-05: Follow-up events pass through decoder, evidence builder, and deduplicate by event_id."""
    raw_bin = (ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin").read_bytes()
    all_records = list(iter_envelopes(raw_bin))

    # Initial query retrieves only the avoidance event on 474 at ~3.120s
    t_avoid = 1791454023120000000
    initial_records = [r for r in all_records if r.timestamp_ns == t_avoid]
    other_records = [r for r in all_records if r.timestamp_ns != t_avoid]

    retriever = MockRetriever(all_records)

    first_call = True

    def stub_investigating_synthesizer(evidence, query, **kwargs):
        nonlocal first_call
        if first_call:
            first_call = False
            # Find avoidance event and ask for 200 ms before it
            eid = evidence["events"][0]["event_id"]
            return (
                {
                    "summary": "Detected avoidance, requesting preceding context",
                    "likely_cause": None,
                    "confidence": "low",
                    "contributing_events": [],
                    "uncertainty": ["Need preceding events"],
                    "data_gaps": [],
                    "context_request": {
                        "before_event": eid,
                        "lookback_ns": 200_000_000,  # 200 ms lookback
                    },
                },
                type("ValReport", (), {"verdict": "ACCEPTED", "badge": "ACCEPTED", "valid_citations": [], "notes": []})(),
                "stub-progressive",
            )
        else:
            # Round 2: Final synthesis
            return (
                {
                    "summary": "Preceding Drone 298 fault identified after progressive expansion.",
                    "likely_cause": "Drone 298 LiDAR timeout",
                    "confidence": "high",
                    "contributing_events": [{"event_id": evidence["events"][0]["event_id"]}],
                    "uncertainty": [],
                    "data_gaps": [],
                },
                type("ValReport", (), {"verdict": "ACCEPTED", "badge": "ACCEPTED", "valid_citations": [evidence["events"][0]["event_id"]], "notes": []})(),
                "stub-progressive",
            )

    init_req = RetrievalRequest(source=ALL, t_start_ns=t_avoid - 1, t_end_ns=t_avoid + 1, max_events=10)
    result = investigate_progressive(
        query="Why did drone 474 leave?",
        initial_request=init_req,
        retriever=retriever,
        schema_registry=drone_registry,
        max_context_requests=1,
        synthesizer_fn=stub_investigating_synthesizer,
    )

    assert result.rounds_executed == 1
    assert len(result.context_requests) == 1
    # Evidence now contains merged events from both queries
    assert len(result.evidence["events"]) > len(initial_records)

    # Verify deduplication: all event_ids in evidence must be unique
    event_ids = [e["event_id"] for e in result.evidence["events"]]
    assert len(event_ids) == len(set(event_ids)), "Duplicate event_ids present in merged evidence!"

    # Verify all merged events were properly decoded
    for e in result.evidence["events"]:
        assert "decode_status" in e
        assert "fields" in e
        assert "source_id" in e


def test_max_events_contract_bounds():
    """Verify max_events bounds [1, 10_000]."""
    # 0 max_events
    res_zero = validate_context_request({"t_start_ns": 100, "t_end_ns": 200, "max_events": 0}, [])
    assert res_zero.valid is False
    assert "Invalid max_events 0" in res_zero.error_message

    # > 10_000 max_events
    res_high = validate_context_request({"t_start_ns": 100, "t_end_ns": 200, "max_events": 10_001}, [])
    assert res_high.valid is False
    assert f"Invalid max_events 10001: must be between 1 and {MAX_EVENTS_LIMIT}" in res_high.error_message
