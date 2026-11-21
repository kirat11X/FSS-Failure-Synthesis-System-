"""End-to-End Verification & Regression Suite for Whole-Dataset Retrieval & Multi-Vendor RAG.

Covers:
  - Test 1: Uploading a .bin file + custom company .txt / .json error-code manual ingests once
            (hash-cached across 3 consecutive Streamlit reruns without re-translating).
  - Test 2: Default analysis covers the entire .bin dataset (e.g. dataflash_sample_3.bin has 3,103 events;
            verify retrieve_full_dataset retrieves all events up to max_events without truncating at 100).
  - Test 3: Invariant start_ns <= end_ns holds on all formatted Retrieval Bounds.
  - Test 4: General failure question ("What caused the anomaly in this log?") on dataflash_sample_3.bin
            passes the sufficiency gate (no false lockout on entity check), retrieves matched error-code
            entries from the digested vendor manual, and produces grounded synthesis with valid [ev-...]
            and [err-...]/[doc-...] citations.
  - Test 5: Querying an explicitly absent entity ("Why did drone 474 leave formation?") on
            dataflash_sample_3.bin (which only contains agent 1) accurately triggers INSUFFICIENT_EVIDENCE
            via the sufficiency gate.
  - Test 6: AppTest from streamlit.testing.v1 on app.py / streamlit_app.py verifying that the app loads cleanly
            across initial render + 3 reruns with the Cyberpunk CSS loaded, and zero unhandled exceptions.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from streamlit.testing.v1 import AppTest

from bridge.bin_ingestor import (
    BinCacheManager,
    compute_sha256,
    condense_telemetry,
    is_critical_event,
    normalize_time_bounds,
)
from bridge.citation_auditor import CitationAuditor
from bridge.error_catalog import (
    VendorErrorEntry,
    extract_error_codes_from_events,
    load_builtin_manual,
    parse_error_manual,
)
from bridge.hybrid_rag import HybridRetriever, enrich_evidence_with_manual
from bridge.java_retriever import JavaRetriever
from bridge.sufficiency_gate import SufficiencyStatus, evaluate_sufficiency
from bridge.synthesizer import (
    deterministic_synthesize,
    preflight_budget_guard,
    synthesize_and_validate,
)
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, RetrievalRequest, RetrievalResult
from fss_py.schema import load_schema_file

ROOT_DIR = Path(__file__).resolve().parent.parent
SAMPLE_3_BIN = ROOT_DIR / "test_data" / "irl" / "dataflash_sample_3.bin"
SCHEMA_PATH = ROOT_DIR / "schemas" / "drone_v0.yaml"
APP_PATH = str(ROOT_DIR / "app.py")


# =============================================================================
# Test 1: Upload Caching Across Consecutive Reruns & Vendor Manual Parsing
# =============================================================================

def test_bin_and_vendor_manual_caching_across_reruns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Test 1: Uploading a .bin file + custom company .txt / .json error-code manual ingests once.

    Hash-cached across 3 consecutive Streamlit reruns without re-translating.
    """
    cache_dir = tmp_path / "uploads"
    cache_mgr = BinCacheManager(cache_dir)

    # 1. Prepare raw ArduPilot binary payload
    ardupilot_bin = b"\xA3\x95" + b"\x10\x20\x30\x40" * 32
    bin_hash = compute_sha256(ardupilot_bin)

    conversion_call_count = 0

    def mock_convert_log(src: Path, dst: Path, agent_id: int = 1):
        nonlocal conversion_call_count
        conversion_call_count += 1
        dst.write_bytes(b"\x00\x00\x00\x01FSS_CONVERTED_LOG_DATA")

    # Patch convert_log to track invocations
    import sys
    mock_convert_mod = type(sys)("scripts.convert_ardupilot_bin_to_fss")
    mock_convert_mod.convert_log = mock_convert_log
    monkeypatch.setitem(sys.modules, "scripts.convert_ardupilot_bin_to_fss", mock_convert_mod)

    # Ingest 1 (Initial upload): must convert and cache
    p1, cached1 = cache_mgr.get_or_ingest(ardupilot_bin, "custom_drone_flight.bin")
    assert not cached1
    assert p1.exists()
    assert conversion_call_count == 1
    assert p1.name.endswith(".fss.bin")

    # Ingest 2, 3, 4 (Simulating 3 consecutive Streamlit widget reruns)
    for rerun_idx in range(1, 4):
        p_rerun, cached_rerun = cache_mgr.get_or_ingest(ardupilot_bin, "custom_drone_flight.bin")
        assert cached_rerun, f"Rerun {rerun_idx} should be served from cache without re-translating"
        assert p_rerun == p1
        assert conversion_call_count == 1, f"Rerun {rerun_idx} triggered redundant re-translation!"

    # 2. Test Custom Company .json Manual Parsing
    company_json_manual = json.dumps([
        {
            "error_code": "0x45",
            "subsystem": "Navigation",
            "severity": "CRITICAL",
            "title": "Navigation Error Flag",
            "description": "Navigation sensor reporting hardware fault condition.",
            "probable_causes": ["IMU bus failure", "Loss of clock sync"],
            "recommended_actions": ["RTL immediately", "Check hardware wiring"],
        },
        {
            "error_code": "0x20",
            "subsystem": "Proximity",
            "severity": "WARNING",
            "title": "Proximity Obstacle Warning",
            "description": "Object breached safe minimum separation.",
            "probable_causes": ["Formation drift", "External intruder"],
            "recommended_actions": ["Execute evasive vector"],
        },
    ])

    json_entries = parse_error_manual(company_json_manual, "company_errors.json", vendor="Acme")
    assert len(json_entries) == 2
    assert json_entries[0].error_code == "0x45"
    assert json_entries[0].doc_chunk_id == "err-acme-0x45"
    assert json_entries[1].error_code == "0x20"
    assert json_entries[1].doc_chunk_id == "err-acme-0x20"

    # 3. Test Custom Company .txt Manual Parsing
    company_txt_manual = """
# SkyGuard Systems Technical Error Manual
## [0x45] Primary Navigation Fault
- **Subsystem**: Navigation
- **Severity**: CRITICAL
- **Description**: Primary navigation filter triggered a critical hardware fault.
- **Probable Causes**: Optical flow desync; Compass magnetic interference
- **Recommended Actions**: Disarm motors; Recalibrate compass

## [0x10] Laser Altimeter Timeout
- **Subsystem**: Rangefinder
- **Severity**: WARNING
- **Description**: Rangefinder failed to respond within timing deadline.
- **Probable Causes**: Ground surface specular absorption; Cable disconnection
- **Recommended Actions**: Fall back to barometric altitude
"""
    txt_entries = parse_error_manual(company_txt_manual, "skyguard_spec.txt", vendor="SkyGuard")
    assert len(txt_entries) == 2
    assert any(e.error_code == "0x45" for e in txt_entries)
    assert any(e.error_code == "0x10" for e in txt_entries)
    nav_entry = next(e for e in txt_entries if e.error_code == "0x45")
    assert nav_entry.subsystem == "Navigation"
    assert nav_entry.doc_chunk_id == "err-skyguard-0x45"


# =============================================================================
# Test 2: Whole-Dataset Retrieval Covers All Events Without Truncation
# =============================================================================

def test_whole_dataset_retrieval_covers_all_events():
    """Test 2: Default analysis covers the entire .bin dataset.

    Verify retrieve_full_dataset retrieves all 3,103 events from dataflash_sample_3.bin
    without truncating at 100 when max_events permits.
    """
    if not SAMPLE_3_BIN.exists():
        pytest.skip(f"Sample file {SAMPLE_3_BIN} not found")

    retriever = JavaRetriever.from_fixtures(SAMPLE_3_BIN)

    # 1. Whole dataset retrieval with default large budget (max_events=10000)
    full_env = retriever.retrieve_full_dataset(source=ALL, max_events=10000)

    assert full_env.status == "OK"
    assert full_env.count == 3103, f"Expected 3,103 events in dataflash_sample_3.bin, found {full_env.count}"
    assert len(full_env.events) == 3103
    assert not full_env.truncated, "Full dataset retrieval should not be marked truncated"
    assert full_env.scope == "full_dataset"
    assert full_env.min_ts_ns is not None
    assert full_env.max_ts_ns is not None
    assert full_env.min_ts_ns <= full_env.max_ts_ns

    # 2. When max_events is capped at 100, truncation flag is properly set
    capped_env = retriever.retrieve_full_dataset(source=ALL, max_events=100)
    assert capped_env.status == "OK"
    assert capped_env.count == 100
    assert len(capped_env.events) == 100
    assert capped_env.truncated is True, "Capped retrieval at 100 events must be marked truncated"

    # 3. Telemetry condensation keeps 100% of critical events
    registry = load_schema_file(SCHEMA_PATH)
    wire_records = [ev.to_event_record() for ev in full_env.events]
    decoded = decode_all(wire_records, registry)

    critical_decoded = [d for d in decoded if is_critical_event(d)]
    assert len(critical_decoded) > 0, "Expected critical events in dataflash_sample_3.bin"

    condensed = condense_telemetry(decoded, max_events_target=1000)
    # Guarantee 100% critical events preserved
    condensed_eids = {d["event_id"] for d in condensed}
    for crit in critical_decoded:
        assert crit.event_id in condensed_eids, f"Critical event {crit.event_id} ({crit.event_name}) was dropped by condensation!"

    # When target budget exceeds critical count, total returned respects target budget
    condensed_large = condense_telemetry(decoded, max_events_target=2500)
    assert len(condensed_large) <= 2500
    assert len(condensed_large) >= len(critical_decoded)


# =============================================================================
# Test 3: Invariant start_ns <= end_ns Holds on All Retrieval Bounds
# =============================================================================

def test_retrieval_bounds_invariant_start_le_end():
    """Test 3: Invariant start_ns <= end_ns holds on all formatted Retrieval Bounds."""
    # 1. Empty / None cases
    s1, e1 = normalize_time_bounds(None, None)
    assert s1 <= e1
    assert e1 == s1 + 1_000_000_000

    # 2. Reversed bounds (start > end) must be flipped and ordered
    s2, e2 = normalize_time_bounds(2_000_000_000, 1_000_000_000)
    assert s2 <= e2
    assert s2 == 1_000_000_000
    assert e2 == 2_000_000_000

    # 3. Identical bounds (start == end) must be adjusted to prevent empty window
    s3, e3 = normalize_time_bounds(1_500_000_000, 1_500_000_000)
    assert s3 < e3
    assert e3 == s3 + 1

    # 4. Out-of-order event timestamps: min and max across all events
    events = [
        {"timestamp_ns": 1_700_000_050},
        {"timestamp_ns": 1_700_000_010},  # Min
        {"timestamp_ns": 1_700_000_090},  # Max
        {"timestamp_ns": 1_700_000_030},
    ]
    s4, e4 = normalize_time_bounds(None, None, events=events)
    assert s4 <= e4
    assert s4 == 1_700_000_010
    assert e4 >= 1_700_000_090

    # 5. Invariant holds on JavaRetriever envelope outputs
    if SAMPLE_3_BIN.exists():
        retriever = JavaRetriever.from_fixtures(SAMPLE_3_BIN)
        env = retriever.retrieve_full_dataset(max_events=1000)
        assert env.min_ts_ns is not None
        assert env.max_ts_ns is not None
        assert env.min_ts_ns <= env.max_ts_ns


# =============================================================================
# Test 4: General Failure Query Passes Sufficiency Gate & Grounds Citations
# =============================================================================

def test_general_failure_question_passes_gate_and_grounds_citations():
    """Test 4: General failure question ("What caused the anomaly in this log?") on dataflash_sample_3.bin.

    Passes the sufficiency gate (no false lockout on entity check), retrieves matched error-code entries
    from the digested vendor manual, and produces grounded synthesis with valid [ev-...] and [err-...]/[doc-...]
    citations.
    """
    if not SAMPLE_3_BIN.exists():
        pytest.skip(f"Sample file {SAMPLE_3_BIN} not found")

    retriever = JavaRetriever.from_fixtures(SAMPLE_3_BIN)
    envelope = retriever.retrieve_full_dataset(max_events=10000)
    wire_records = [ev.to_event_record() for ev in envelope.events]
    registry = load_schema_file(SCHEMA_PATH)
    decoded = decode_all(wire_records, registry)

    ret_res = RetrievalResult(
        request=RetrievalRequest(
            source=ALL,
            t_start_ns=envelope.min_ts_ns,
            t_end_ns=envelope.max_ts_ns,
            max_events=10000,
        ),
        events=wire_records,
        truncated=envelope.truncated,
    )

    query = "What caused the anomaly in this log?"
    evidence = build_evidence(decoded, query=query, retrieval=ret_res)

    # Ingest vendor manual specifying error event 0x45
    company_manual = """
# ArduPilot Fleet Error Catalog
## [0x45] Hardware Failure Status Error
- **Subsystem**: Flight Controller
- **Severity**: CRITICAL
- **Description**: Subsystem flagged critical hardware status during operation.
- **Probable Causes**: Main board power fluctuation; Sensor communication timeout
- **Recommended Actions**: Disarm immediately; Inspect flight controller power distribution
"""
    vendor_entries = parse_error_manual(company_manual, "ardupilot_errors.txt", vendor="ArduPilot")
    vendor_matches = enrich_evidence_with_manual(evidence, vendor_entries)
    assert len(vendor_matches) >= 1, "Expected error event 0x45 to match vendor manual"

    # 1. Verify Sufficiency Gate: must pass without false lockout on entity check
    suff = evaluate_sufficiency(evidence, query, vendor_manual_matches=vendor_matches)
    assert suff.is_sufficient is True
    assert suff.status == SufficiencyStatus.SUFFICIENT
    # Ensure question content coverage passed (no missing entity false positive)
    q_check = next(c for c in suff.checks if c.name == "question_content_coverage")
    assert q_check.passed is True

    # 2. Run synthesis and citation audit
    synth = deterministic_synthesize(evidence, query, [], vendor_manual_matches=vendor_matches)
    assert synth["summary"] is not None
    assert synth["likely_cause"] is not None

    auditor = CitationAuditor()
    report = auditor.audit(synth, evidence, query=query, vendor_manual_matches=vendor_matches)

    assert report.verdict in ("ACCEPTED", "ACCEPTED_WITH_CORRECTIONS", "VERIFIED_SUPPORTED")
    assert len(report.invalid_citations) == 0, f"Ungrounded citations found: {report.invalid_citations}"

    # Verify presence of valid event citation [ev-...] and vendor citation [err-...]
    has_ev_citation = any(c.startswith("ev-") for c in report.valid_citations)
    has_err_citation = any(c.startswith("err-") or "0x45" in c for c in report.valid_citations)
    assert has_ev_citation, f"Expected [ev-...] citations in valid citations: {report.valid_citations}"
    assert has_err_citation, f"Expected [err-...] citation in valid citations: {report.valid_citations}"


# =============================================================================
# Test 5: Querying Absent Entity Accurately Triggers INSUFFICIENT_EVIDENCE
# =============================================================================

def test_absent_entity_query_triggers_insufficient_evidence():
    """Test 5: Querying an explicitly absent entity ("Why did drone 474 leave formation?") on

    dataflash_sample_3.bin (which only contains agent 1) accurately triggers INSUFFICIENT_EVIDENCE
    via the sufficiency gate.
    """
    if not SAMPLE_3_BIN.exists():
        pytest.skip(f"Sample file {SAMPLE_3_BIN} not found")

    retriever = JavaRetriever.from_fixtures(SAMPLE_3_BIN)
    envelope = retriever.retrieve_full_dataset(max_events=10000)
    wire_records = [ev.to_event_record() for ev in envelope.events]
    registry = load_schema_file(SCHEMA_PATH)
    decoded = decode_all(wire_records, registry)

    ret_res = RetrievalResult(
        request=RetrievalRequest(
            source=ALL,
            t_start_ns=envelope.min_ts_ns,
            t_end_ns=envelope.max_ts_ns,
            max_events=10000,
        ),
        events=wire_records,
        truncated=envelope.truncated,
    )

    absent_query = "Why did drone 474 leave formation?"
    evidence = build_evidence(decoded, query=absent_query, retrieval=ret_res)

    # Verify agent 1 is the only agent present
    assert evidence["sources"] == [1]

    # Evaluate sufficiency gate
    suff = evaluate_sufficiency(evidence, absent_query)

    assert suff.is_sufficient is False
    assert suff.status == SufficiencyStatus.INSUFFICIENT_EVIDENCE

    # Verify that the specific check failure was question_content_coverage
    q_check = next(c for c in suff.checks if c.name == "question_content_coverage")
    assert q_check.passed is False
    assert "474" in q_check.details


# =============================================================================
# Test 6: AppTest Initial Render + 3 Reruns with Research CRT / CSS Loaded
# =============================================================================

def test_streamlit_apptest_initial_render_and_three_reruns_cyberpunk_css():
    """Test 6: AppTest from streamlit.testing.v1 on app.py / streamlit_app.py.

    Verifies that the app loads cleanly across initial render + 3 reruns with the Research CRT
    CSS loaded, and zero unhandled exceptions.
    """
    at = AppTest.from_file(APP_PATH, default_timeout=120)

    # Initial Run
    at.run()
    assert len(at.exception) == 0, f"Encountered exception on initial render: {[e.value for e in at.exception]}"

    # Verify Research CRT CSS is injected into the DOM
    css_loaded = any(
        "CRT" in m.value or "CYBERPUNK" in m.value or "--phosphor-green" in m.value or "--bg-void" in m.value or "--neon-cyan" in m.value
        for m in at.markdown
    )
    assert css_loaded, "Research CRT CSS styling was not loaded or injected into page markdown"

    # Verify Core UI elements exist
    assert len(at.title) > 0
    assert "Failure Synthesis System" in at.title[0].value

    # Verify Tabs include the Vendor Manual / Domain Knowledge / RAG Inspector
    tab_labels = [t.label for t in at.tabs]
    assert any("Vendor Manual" in t or "Domain Knowledge" in t or "Domain RAG" in t or "Error Codes" in t for t in tab_labels)

    # 3 Consecutive Reruns
    for rerun_idx in range(1, 4):
        at.run()
        exceptions = [e.value for e in at.exception]
        assert len(exceptions) == 0, f"Rerun {rerun_idx} threw unexpected exceptions: {exceptions}"
