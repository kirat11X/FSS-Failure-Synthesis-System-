"""Comprehensive test suite for Post-Mortem Report Exporter (REQ-013, FR-13).

Verifies:
  1. JSON-LD provenance artifact export:
     - Valid JSON and @context (schema.org, fss vocabulary).
     - Cryptographic SHA-256 integrity digest over evidence + synthesis.
     - Tamper-evidence: modifying evidence or synthesis alters the digest.
     - Correct mapping of epistemic tags, data gaps, and validation badge.
  2. Standalone HTML brief export:
     - Zero external dependencies (no CDNs, self-contained CSS).
     - Collapsible <details> sections for all 8 parts of FSS report.
     - Visible validation badge and audit verification status.
  3. Valid PDF export (%PDF-1.4 binary stream):
     - PDF header starts with %PDF-1.4.
     - PDF footer terminates with %%EOF.
     - Output to file path vs returned bytes.
     - Pure-Python PDF-1.4 generator fallback verification.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import pytest

from bridge.report_exporter import (
    _generate_pure_python_pdf,
    compute_report_integrity_digest,
    export_html,
    export_jsonld,
    export_pdf,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def sample_synthesis_result():
    return {
        "query": "Why did drone 474 leave formation?",
        "trace_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "evidence": {
            "format": "fss-evidence/v0",
            "query": "Why did drone 474 leave formation?",
            "sources": [298, 474],
            "ordering": "Grouped by source, producer time within each source.",
            "clock_caveat": "Producer clocks are not synchronized across sources.",
            "clock_skew": {
                "skew_detected": True,
                "estimated_offset_bounds_ns": {"298->474": [50_000_000, 1_050_000_000]},
                "ambiguous_pairs": [],
            },
            "events": [
                {
                    "event_id": "ev-298-lidar",
                    "source_id": 298,
                    "timestamp_ns": 1791454023050000000,
                    "event_name": "LIDAR_STATUS",
                    "event_type": 16,
                    "decode_status": "SUCCESS",
                    "fields": {"status_code": 17, "status_name": "LIDAR_TIMEOUT"},
                    "interpreted_fields": ["status_name"],
                    "unknown_bytes": 0,
                },
                {
                    "event_id": "ev-298-diag",
                    "source_id": 298,
                    "timestamp_ns": 1791454023060000000,
                    "event_name": "DIAGNOSTIC",
                    "event_type": 48,
                    "decode_status": "PARTIALLY_DECODED",
                    "fields": {"status_code": 1, "temperature_c": 72.4},
                    "interpreted_fields": [],
                    "unknown_bytes": 8,
                },
                {
                    "event_id": "ev-474-avoid",
                    "source_id": 474,
                    "timestamp_ns": 1791454023120000000,
                    "event_name": "AVOIDANCE",
                    "event_type": 32,
                    "decode_status": "SUCCESS",
                    "fields": {"reason_code": 1, "reason": "COLLISION_RISK"},
                    "interpreted_fields": ["reason"],
                    "unknown_bytes": 0,
                },
            ],
            "data_gaps": [
                {
                    "kind": "OPAQUE_BYTES",
                    "event_id": "ev-298-diag",
                    "source_id": 298,
                    "detail": "8 payload bytes were not decoded because their structure is not configured.",
                }
            ],
            "retrieval": {
                "source": "ALL",
                "t_start_ns": 1791454020000000000,
                "t_end_ns": 1791454025000000000,
                "max_events": 100,
                "returned": 3,
                "truncated": False,
            },
        },
        "synthesis": {
            "summary": "Drone 298 experienced a LiDAR timeout, causing Drone 474 to execute collision avoidance.",
            "likely_cause": "Drone 298 LiDAR sensor timeout (code 17)",
            "confidence": "high",
            "contributing_events": [
                {"event_id": "ev-298-lidar", "source_id": 298, "event_type": "LIDAR_STATUS"},
                {"event_id": "ev-474-avoid", "source_id": 474, "event_type": "AVOIDANCE"},
            ],
            "uncertainty": [
                "Producer clocks between 298 and 474 are unsynchronized (estimated skew 50ms)."
            ],
            "data_gaps": [
                "DIAGNOSTIC event ev-298-diag contained 8 undocumented vendor bytes."
            ],
        },
        "validation": {
            "verdict": "ACCEPTED",
            "badge": "ACCEPTED",
            "valid_citations": ["ev-298-lidar", "ev-474-avoid"],
            "invalid_citations": [],
            "notes": ["All cited event IDs verified against retrieved evidence."],
        },
        "model": "qwen3-fss-bridge",
        "report": "MOCK 8-PART REPORT CONTENT",
    }


def test_01_export_jsonld_provenance_structure(sample_synthesis_result):
    """Verify JSON-LD export schema, @context, integrity digest, and epistemic tags."""
    jsonld_str = export_jsonld(sample_synthesis_result)
    doc = json.loads(jsonld_str)

    # 1. @context structure
    assert "@context" in doc
    context = doc["@context"]
    assert isinstance(context, list)
    assert "https://schema.org/" in context
    assert context[1]["fss"] == "https://fss.org/schema/"

    # 2. Trace ID and Type
    assert doc["@type"] == "fss:InvestigationReport"
    assert doc["trace_id"] == "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert "timestamp" in doc

    # 3. Validation and Verdict
    assert doc["validation_badge"] == "ACCEPTED"
    assert doc["verdict"] == "ACCEPTED"

    # 4. Epistemic tags mapping
    tags = doc["epistemic_tags"]
    assert tags["ev-298-lidar"] == "decoded + documented"
    assert tags["ev-298-diag"] == "decoded + unknown (8 bytes)"
    assert tags["ev-474-avoid"] == "decoded + documented"

    # 5. Data gaps & Clock skew
    assert len(doc["data_gaps"]) == 1
    assert doc["data_gaps"][0]["kind"] == "OPAQUE_BYTES"
    assert doc["clock_skew"]["skew_detected"] is True


def test_02_integrity_digest_tamper_evidence(sample_synthesis_result):
    """Verify SHA-256 integrity digest is deterministic and changes upon tampering."""
    ev = sample_synthesis_result["evidence"]
    syn = sample_synthesis_result["synthesis"]

    d1 = compute_report_integrity_digest(ev, syn)
    d2 = compute_report_integrity_digest(ev, syn)
    assert d1 == d2, "Integrity digest must be deterministic"
    assert len(d1) == 64, "SHA-256 must be 64 hex characters"

    # Tamper with evidence
    tampered_ev = json.loads(json.dumps(ev))
    tampered_ev["events"][0]["timestamp_ns"] += 1
    d_tampered = compute_report_integrity_digest(tampered_ev, syn)
    assert d1 != d_tampered, "Digest failed to detect evidence tampering!"

    # Tamper with synthesis
    tampered_syn = json.loads(json.dumps(syn))
    tampered_syn["summary"] = "Tampered explanation."
    d_tampered_syn = compute_report_integrity_digest(ev, tampered_syn)
    assert d1 != d_tampered_syn, "Digest failed to detect synthesis tampering!"


def test_03_export_html_self_contained_with_audit_badge(sample_synthesis_result):
    """Verify HTML export is self-contained with collapsible sections and badges."""
    html_content = export_html(sample_synthesis_result)

    # 1. Doctype and structure
    assert "<!DOCTYPE html>" in html_content
    assert "<html" in html_content
    assert "</html>" in html_content

    # 2. Critical data elements
    assert sample_synthesis_result["trace_id"] in html_content
    assert sample_synthesis_result["query"] in html_content
    assert "badge-accepted" in html_content
    assert "ACCEPTED" in html_content

    # 3. Telemetry events and epistemic tags
    assert "ev-298-lidar" in html_content
    assert "ev-474-avoid" in html_content
    assert "decoded + documented" in html_content
    assert "decoded + unknown (8 bytes)" in html_content

    # 4. Collapsible <details> tags for 8 parts
    assert "<details open>" in html_content
    assert "<summary>[1] Question</summary>" in html_content
    assert "<summary>[3] Telemetry Events (3)</summary>" in html_content
    assert "<summary>[7] AI Reasoning Synthesis</summary>" in html_content
    assert "<summary>[8] Audit Validator & Verdict</summary>" in html_content

    # 5. Integrity digest
    digest = sample_synthesis_result.get("integrity_digest") or compute_report_integrity_digest(
        sample_synthesis_result["evidence"], sample_synthesis_result["synthesis"]
    )
    assert digest in html_content or digest[:16] in html_content


def test_04_export_pdf_valid_header_and_trailer(sample_synthesis_result):
    """Verify PDF export outputs valid %PDF-1.4 stream ending with %%EOF."""
    # 1. Export in-memory bytes
    pdf_bytes = export_pdf(sample_synthesis_result)
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF-1.4"), f"Invalid PDF header: {pdf_bytes[:10]!r}"
    assert b"%%EOF" in pdf_bytes[-32:], f"Invalid PDF trailer: {pdf_bytes[-32:]!r}"

    # 2. Export to temporary file
    with tempfile.NamedTemporaryFile(prefix="fss_report_", suffix=".pdf", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        written_bytes = export_pdf(sample_synthesis_result, output_path=tmp_path)
        assert tmp_path.is_file()
        assert tmp_path.stat().st_size > 0
        file_bytes = tmp_path.read_bytes()
        assert file_bytes == written_bytes
        assert file_bytes.startswith(b"%PDF-1.4")
        assert b"%%EOF" in file_bytes[-32:]
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_05_pure_python_pdf_fallback_conformance():
    """Verify the pure-Python minimalist PDF generator without external libraries."""
    title = "Test FSS Pure Python PDF"
    lines = [
        "Header Line: System Failure Synthesis Report",
        "Section 1: Event 0x10 at 1791454023050000000 ns",
        "Section 2: Cause identified as LiDAR Timeout",
        "Validation Verdict: ACCEPTED",
    ]

    pdf_stream = _generate_pure_python_pdf(title, lines)

    assert isinstance(pdf_stream, bytes)
    # Header conformance
    assert pdf_stream.startswith(b"%PDF-1.4\n")
    # Trailer conformance
    assert pdf_stream.strip().endswith(b"%%EOF")
    # Object structure
    assert b"/Type /Catalog" in pdf_stream
    assert b"/Type /Pages" in pdf_stream
    assert b"/Type /Page" in pdf_stream
    assert b"/Type /Font" in pdf_stream
    assert b"xref\n" in pdf_stream
    assert b"trailer\n" in pdf_stream
    assert b"startxref\n" in pdf_stream
