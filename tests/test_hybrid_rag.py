"""Tests for knowledge_base-only hybrid retrieval."""

from __future__ import annotations

import os

from bridge import knowledge_base
from bridge.hybrid_rag import HybridRetriever, extract_search_terms


def test_vram_preallocate_environment_guard():
    assert os.environ.get("XLA_PYTHON_CLIENT_PREALLOCATE") == "false"


def test_generic_search_terms_preserve_codes_and_identifiers():
    terms = extract_search_terms("0x10 sensor_status code 17 component_4")

    assert {"0x10", "sensor_status", "17", "component_4"} <= set(terms)


def test_retriever_returns_only_documents_loaded_from_knowledge_base(tmp_path):
    (tmp_path / "reference.txt").write_text(
        "Component state 17 means the device is unavailable.",
        encoding="utf-8",
    )
    retriever = HybridRetriever(knowledge_base_dir=tmp_path)

    results = retriever.retrieve_knowledge(
        "What does component state 17 mean?",
        evidence={"events": []},
    )

    assert results
    assert {doc["source_filename"] for doc in results} == {"reference.txt"}
    assert "device is unavailable" in results[0]["content"]


def test_empty_knowledge_base_has_no_builtin_fallback(tmp_path):
    retriever = HybridRetriever(knowledge_base_dir=tmp_path)

    assert retriever.corpus == []
    assert retriever.retrieve_knowledge("sensor fault") == []


def test_get_instance_reloads_when_knowledge_base_changes(tmp_path, monkeypatch):
    (tmp_path / "reference.txt").write_text("Alpha component reference.", encoding="utf-8")
    monkeypatch.setattr(knowledge_base, "KNOWLEDGE_BASE_DIR", tmp_path)
    monkeypatch.setattr(HybridRetriever, "_instance", None)
    monkeypatch.setattr(HybridRetriever, "_knowledge_base_fingerprint", None)

    first = HybridRetriever.get_instance()
    assert first.retrieve_knowledge("Alpha component")

    (tmp_path / "reference.txt").write_text("Beta component reference.", encoding="utf-8")
    second = HybridRetriever.get_instance()

    assert second is not first
    assert second.retrieve_knowledge("Beta component")
