from __future__ import annotations

from pathlib import Path

import pypdf
from docx import Document

from bridge import knowledge_base
from bridge.knowledge_base import load_knowledge_base
from bridge.synthesizer import build_llm_context, retrieve_relevant_knowledge


def test_knowledge_base_indexes_document_and_structured_formats_with_provenance(tmp_path):
    (tmp_path / "flight.md").write_text(
        "# Flight Manual\n## Sensor Checks\nCheck the LiDAR timeout counter.",
        encoding="utf-8",
    )
    (tmp_path / "events.yaml").write_text(
        "title: Event Reference\nschema_version: telemetry-v2\n"
        "codes:\n  17: scan timeout\n",
        encoding="utf-8",
    )
    (tmp_path / "events.csv").write_text(
        "code,meaning,schema_version\n17,scan timeout,events-v1\n",
        encoding="utf-8",
    )
    (tmp_path / "guide.html").write_text(
        "<html><head><title>HTML Guide</title></head><body>"
        "<h2>Recovery</h2><p>Restart the sensor driver.</p>"
        "<script>ignore this</script></body></html>",
        encoding="utf-8",
    )
    docx_path = tmp_path / "procedure.docx"
    document = Document()
    document.core_properties.title = "Recovery Procedure"
    document.add_heading("Restart", level=1)
    document.add_paragraph("Restart the flight controller.")
    document.save(docx_path)

    pdf_path = tmp_path / "sensor.pdf"
    pdf_path.write_bytes(b"test pdf")

    class _Page:
        def extract_text(self):
            return "Inspect the sensor wiring."

    class _Metadata:
        title = "Sensor PDF"

    pdf_reader_paths: list[Path] = []

    class _PdfReader:
        metadata = _Metadata()
        pages = [_Page()]

        def __init__(self, path):
            pdf_reader_paths.append(Path(path))

    original_reader = pypdf.PdfReader
    pypdf.PdfReader = _PdfReader
    try:
        documents = load_knowledge_base(tmp_path)
    finally:
        pypdf.PdfReader = original_reader

    by_filename = {doc["source_filename"]: doc for doc in documents}
    assert set(by_filename) == {
        "flight.md",
        "events.yaml",
        "events.csv",
        "guide.html",
        "procedure.docx",
        "sensor.pdf",
    }
    assert by_filename["events.yaml"]["schema_version"] == "telemetry-v2"
    assert by_filename["events.csv"]["schema_version"] == "events-v1"
    assert by_filename["sensor.pdf"]["page_number"] == 1
    assert by_filename["sensor.pdf"]["title"] == "Sensor PDF"
    assert pdf_reader_paths == [pdf_path]
    assert by_filename["events.csv"]["section"] == "Row 2"
    assert by_filename["guide.html"]["title"] == "HTML Guide"
    assert "ignore this" not in "\n".join(doc["content"] for doc in documents)


def test_knowledge_base_is_auto_indexed_and_retrievable_as_prompt_context(
    tmp_path, monkeypatch
):
    from bridge.hybrid_rag import HybridRetriever

    manual = tmp_path / "calibration.md"
    manual.write_text(
        "# Kestrel Calibration\n## Quasar Drift\n"
        "Quasar gyroscope drift requires recalibration after a cold start.",
        encoding="utf-8",
    )
    monkeypatch.setattr(knowledge_base, "KNOWLEDGE_BASE_DIR", tmp_path)
    monkeypatch.setattr(HybridRetriever, "_instance", None)
    monkeypatch.setattr(HybridRetriever, "_knowledge_base_fingerprint", None)

    evidence = {"events": [], "sources": [], "data_gaps": []}
    documents = retrieve_relevant_knowledge(
        evidence, "Quasar gyroscope drift after cold start", top_k=4
    )
    assert any(doc["source_filename"] == "calibration.md" for doc in documents)

    context = build_llm_context(
        evidence,
        "How should Quasar drift be handled?",
        documents,
        effective_num_ctx=4096,
        _skip_budget_guard=True,
    )
    assert 'source_filename="calibration.md"' in context
    assert "Quasar gyroscope drift requires recalibration" in context


def test_context_omits_synthetic_event_ids_and_preserves_zero_timestamp():
    evidence = {
        "events": [
            {
                "event_id": "ev-0123456789ab",
                "source_id": 7,
                "timestamp_ns": 0,
                "timestamp": 123,
                "event_type": "0x20",
                "event_name": "sensor_event",
                "decode_status": "decoded",
                "fields": {"status": "ok"},
                "interpreted_fields": [],
            }
        ],
        "sources": [7],
        "data_gaps": [],
    }

    context = build_llm_context(
        evidence,
        "What happened?",
        [],
        _skip_budget_guard=True,
        include_event_ids=False,
    )

    assert '"timestamp_ns": 0' in context
    assert '"event_type": 32' in context
    assert "ev-0123456789ab" not in context
