"""Regression test verifying mixed telemetry ([ev-...]) and Domain RAG ([doc-...]) citations."""

import pytest
from bridge.citation_auditor import EpistemicCitationAuditor, ValidationBadge


@pytest.fixture
def auditor():
    return EpistemicCitationAuditor()


def test_mixed_telemetry_and_domain_rag_citations(auditor):
    """Ensure synthesis with both telemetry events and domain doc citations validates cleanly without KeyError."""
    eid = "ev-2e92ae032f03"
    doc_id = "doc-d1_formation#1"

    guarded_evidence = {
        "events": [
            {
                "event_id": eid,
                "source_id": 298,
                "timestamp_ns": 1718000000000000000,
                "event_name": "LIDAR_STATUS",
                "event_type": 16,
                "fields": {"status_code": 17, "timeout_count": 5},
            }
        ]
    }

    known_docs = {
        doc_id: "D1 formation rule: loss of lidar requires immediate safe distance separation."
    }

    synthesis = {
        "summary": (
            f"Drone 298 detected lidar sensor timeouts [{eid}], which under formation flight rules [{doc_id}] "
            f"necessitated an immediate fallback manoeuvre."
        ),
        "likely_cause": f"Primary lidar sensor timeout on Drone 298 [{eid}].",
        "contributing_events": [
            {
                "event_id": eid,
                "source_id": 298,
                "timestamp": 1718000000000000000,
                "event_type": "LIDAR_STATUS",
            }
        ],
        "uncertainty": [],
    }

    report = auditor.audit(synthesis, guarded_evidence, known_docs)

    assert report is not None
    assert eid in report.valid_citations
    assert doc_id in report.valid_citations
    assert len(report.invalid_citations) == 0
    assert report.tier_results["tier1_id_integrity"]["passed"] is True
    assert report.badge in (ValidationBadge.VERIFIED_SUPPORTED, ValidationBadge.FORCE_OVERCLAIM)


def test_invalid_domain_doc_citation_rejected(auditor):
    """Ensure hallucinated domain doc citations are caught and marked as invalid citations."""
    eid = "ev-2e92ae032f03"
    fake_doc_id = "doc-nonexistent_manual#99"

    guarded_evidence = {
        "events": [
            {
                "event_id": eid,
                "source_id": 298,
                "timestamp_ns": 1718000000000000000,
                "event_name": "LIDAR_STATUS",
                "event_type": 16,
                "fields": {"status_code": 17, "timeout_count": 5},
            }
        ]
    }

    known_docs = {
        "doc-d1_formation#1": "Valid rule."
    }

    synthesis = {
        "summary": f"Drone 298 reported lidar failure [{eid}] as documented in [{fake_doc_id}].",
        "likely_cause": f"Sensor timeout [{eid}].",
        "contributing_events": [
            {
                "event_id": eid,
                "source_id": 298,
                "timestamp": 1718000000000000000,
                "event_type": "LIDAR_STATUS",
            }
        ],
        "uncertainty": [],
    }

    report = auditor.audit(synthesis, guarded_evidence, known_docs)

    assert fake_doc_id in report.invalid_citations
    assert report.tier_results["tier1_id_integrity"]["passed"] is False
