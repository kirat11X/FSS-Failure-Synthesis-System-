"""Unit Tests for Hybrid RAG (BM25S + Dense + RRF + Reranker) and Historical RCA Exemplars.

Spec requirement:
  - Set os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false" prior to importing bm25s.
  - bm25s lexical retrieval over domain docs and error codes (handling exact alphanumeric strings
    like 0x10, LIDAR_STATUS, COLLISION_RISK).
  - Local dense embeddings (all-MiniLM-L6-v2 or cached local embedding model) merged via RRF (k = 60).
  - Local reranker (cross-encoder or score weighting) selecting top 5–8 chunks.
  - Condensed historical resolved incidents index/store (RCACopilot pattern: retrieve K = 3–15
    condensed diagnostic exemplars for few-shot CoT).
"""

from __future__ import annotations

import os
import pytest

from bridge.hybrid_rag import (
    DOMAIN_KNOWLEDGE,
    HISTORICAL_INCIDENTS,
    HistoricalIncident,
    HistoricalIncidentsStore,
    HybridRetriever,
    extract_search_terms,
    format_exemplars_cot,
    retrieve_historical_exemplars,
    retrieve_hybrid_knowledge,
)


def test_vram_preallocate_environment_guard():
    """Verify XLA_PYTHON_CLIENT_PREALLOCATE is set to false to prevent JAX VRAM preallocation."""
    assert os.environ.get("XLA_PYTHON_CLIENT_PREALLOCATE") == "false"


def test_alphanumeric_search_terms_extraction():
    """Verify alphanumeric codes, hex values, and domain identifiers are preserved."""
    text = "0x10 LIDAR_STATUS code 17, 18, 19 and 0x20 AVOIDANCE COLLISION_RISK min_separation_m"
    terms = extract_search_terms(text)

    assert "0x10" in terms
    assert "0x20" in terms
    assert "lidar_status" in terms
    assert "collision_risk" in terms
    assert "17" in terms
    assert "18" in terms
    assert "19" in terms


def test_bm25s_exact_error_code_retrieval():
    """Verify BM25S retrieves the exact error code table for 0x10, 17, 18, 19, LIDAR_STATUS."""
    retriever = HybridRetriever.get_instance()
    results = retriever.retrieve_knowledge(query="0x10 LIDAR_STATUS 17 18", top_k=5)

    assert len(results) >= 1
    # Check that error code table or lidar timeout is in top results
    doc_ids = [d["id"] for d in results]
    assert any("lidar_error_codes" in did or "lidar_timeout" in did for did in doc_ids)

    # Verify content explains the codes
    top_doc = results[0]
    assert any(term in top_doc["content"] for term in ("0x10", "17", "LIDAR_STATUS"))


def test_hybrid_rrf_k60_fusion():
    """Verify Reciprocal Rank Fusion merges BM25S and dense embeddings with k=60."""
    retriever = HybridRetriever.get_instance()
    # Query with semantic concept plus exact code
    results = retriever.retrieve_knowledge(
        query="Why did the drone leave formation after evasive avoidance manoeuvre?",
        evidence={
            "events": [
                {
                    "event_name": "AVOIDANCE",
                    "event_type": "0x20",
                    "fields": {"reason": "COLLISION_RISK", "reason_code": 1},
                },
                {
                    "event_name": "FORMATION_STATE",
                    "event_type": "0x21",
                    "fields": {"state": "LEFT_FORMATION", "state_code": 1},
                },
            ]
        },
        top_k=8,
        rrf_k=60,
    )

    assert 1 <= len(results) <= 8
    doc_ids = [d["id"] for d in results]
    # Expect formation and avoidance documents in the top fused results
    assert any("avoidance" in did or "formation" in did for did in doc_ids)


def test_historical_incidents_store_rcacopilot_retrieval():
    """Verify retrieval of K = 3..15 condensed diagnostic exemplars (RCACopilot pattern)."""
    store = HistoricalIncidentsStore.get_instance()
    assert len(store.incidents) >= 8

    # Query for drone formation departure with lidar timeout
    exemplars = store.retrieve_exemplars(
        query="0x10 LIDAR_TIMEOUT Drone 298 missed scans causing Drone 474 avoidance",
        k=5,
    )

    assert len(exemplars) == 5
    assert all(isinstance(ex, HistoricalIncident) for ex in exemplars)

    # Top exemplar should be INC-2026-0811 (LiDAR timeout cascade)
    top_ex = exemplars[0]
    assert top_ex.incident_id == "INC-2026-0811"
    assert "LIDAR_TIMEOUT" in top_ex.symptoms
    assert "Drone 474" in top_ex.symptoms
    assert "stale obstacle map" in top_ex.root_cause.lower()
    assert top_ex.condensed_cot != ""


def test_historical_exemplars_cot_formatting():
    """Verify few-shot Chain-of-Thought markdown formatting."""
    exemplars = retrieve_historical_exemplars(query="COLLISION_RISK AVOIDANCE", k=3)
    assert len(exemplars) == 3

    cot_text = format_exemplars_cot(exemplars)
    assert "=== HISTORICAL RESOLVED INCIDENT EXEMPLARS (RCACopilot Few-Shot CoT) ===" in cot_text
    assert "### Exemplar" in cot_text
    assert "- **Symptoms**:" in cot_text
    assert "- **Chain of Thought**:" in cot_text
    assert "- **Resolved Cause**:" in cot_text


def test_top_chunks_bounded_between_5_and_8():
    """Verify that retrieval strictly respects the top 5–8 chunks constraint."""
    retriever = HybridRetriever.get_instance()
    for requested_k in [5, 6, 7, 8]:
        res = retriever.retrieve_knowledge(query="formation collision avoidance", top_k=requested_k)
        assert len(res) == requested_k
