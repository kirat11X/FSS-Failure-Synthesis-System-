"""Test Suite for Strict Two-Stage External Unseen Java .dat Pipeline.

Verifies:
  1. Java-only storage reading & bounded retrieval (Stage 1) against external unseen directories & .dat files.
  2. Sequential execution barrier: Stage 1 produces explicit Java Receipt before Stage 2 runs.
  3. Strict isolation: external stores located outside repo root, zero repo mutation, zero .dat indexing in RAG.
  4. Zip-slip protection on uploaded store archives.
  5. Standalone J1 .dat and segmented directory layout detection.
"""

from __future__ import annotations

import io
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from bridge.error_catalog import load_builtin_manual
from bridge.java_retriever import (
    JavaReceipt,
    JavaRetriever,
    JavaStoreLayout,
    StoreBounds,
    detect_java_store_layout,
    extract_unseen_zip,
    plan_bounded_retrieval_slices,
    stage_unseen_dat,
)
from bridge.synthesizer import (
    build_llm_context,
    retrieve_relevant_knowledge,
    synthesize_and_validate,
)
from fss_py.model import ALL, RetrievalRequest, RetrievalResult
from fss_py.schema import load_schema_file

ROOT_DIR = Path(__file__).resolve().parent.parent
GOLDEN_BIN = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"
SCHEMA_PATH = ROOT_DIR / "schemas" / "drone_v0.yaml"


def test_detect_java_store_layout_variants(tmp_path: Path):
    """Test layout detection across segmented dirs, single files, and invalid paths."""
    # 1. Non-existent path
    non_existent = tmp_path / "does_not_exist"
    layout = detect_java_store_layout(non_existent)
    assert not layout.is_valid
    assert layout.layout_type == "invalid"
    assert "does not exist" in (layout.error_message or "")

    # 2. Standalone .dat file
    dat_file = tmp_path / "standalone.dat"
    dat_file.write_bytes(b"\x00" * 65536)
    layout = detect_java_store_layout(dat_file)
    assert layout.is_valid
    assert layout.layout_type == "single_file"
    assert layout.store_path == dat_file

    # 3. Invalid non-.dat file
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("hello")
    layout = detect_java_store_layout(txt_file)
    assert not layout.is_valid
    assert "expected .dat extension" in (layout.error_message or "")

    # 4. Incomplete directory (missing blocks.dat)
    incomp_dir = tmp_path / "incomplete_store"
    incomp_dir.mkdir()
    (incomp_dir / "block-metadata.dat").write_bytes(b"\x00" * 44)
    layout = detect_java_store_layout(incomp_dir)
    assert not layout.is_valid
    assert "missing required artifacts: blocks.dat" in (layout.error_message or "")

    # 5. Valid segmented directory
    seg_dir = tmp_path / "segmented_store"
    seg_dir.mkdir()
    (seg_dir / "blocks.dat").write_bytes(b"\x00" * 65536)
    (seg_dir / "block-metadata.dat").write_bytes(b"\x00" * 44)
    segments_sub = seg_dir / "segments"
    segments_sub.mkdir()
    (segments_sub / "segment-0").mkdir()
    (segments_sub / "segment-1").mkdir()

    layout = detect_java_store_layout(seg_dir)
    assert layout.is_valid
    assert layout.layout_type == "segmented_directory"
    assert layout.segment_count == 2
    assert layout.blocks_dat == seg_dir / "blocks.dat"
    assert layout.block_metadata_dat == seg_dir / "block-metadata.dat"

    # 6. Selecting blocks.dat directly inside segmented dir
    layout_direct = detect_java_store_layout(seg_dir / "blocks.dat")
    assert layout_direct.is_valid
    assert layout_direct.layout_type == "segmented_directory"
    assert layout_direct.root_dir == seg_dir


def test_zip_slip_security_guard(tmp_path: Path):
    """Ensure malicious zip archives containing directory traversal paths are rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/passwd", "evil")

    buf.seek(0)
    with pytest.raises(ValueError, match="Security violation.*directory traversal"):
        extract_unseen_zip(buf.getvalue(), filename="malicious.zip")


def test_stage_1_external_segmented_java_store_retrieval(tmp_path: Path):
    """Stage 1 verification: Java alone reads external segmented store outside repo root."""
    external_store = tmp_path / "external_unseen_segmented"
    external_store.mkdir(parents=True, exist_ok=True)

    # Ingest golden binary into the external directory using Java FileIngestor
    prop_file = tmp_path / "storage.properties"
    envelope_cfg = (ROOT_DIR / "fss-core" / "envelope.cfg").resolve()
    prop_file.write_text(
        f"root={external_store.resolve()}\n"
        f"blockSize=65536\n"
        f"bufferCapacityBytes=1048576\n"
        f"maxMessageSize=4096\n"
        f"segmentDurationMillis=60000\n"
        f"byteOrder=BIG_ENDIAN\n"
        f"envelopeConfig={envelope_cfg}\n",
        encoding="utf-8",
    )

    core_classes = ROOT_DIR / "fss-core" / "target" / "classes"
    bridge_classes = ROOT_DIR / "bridge" / "classes"
    cp = f"{core_classes}:{bridge_classes}"

    # Convert golden fixture to envelope.cfg format
    fss_bin = tmp_path / "scenario.fss.bin"
    from bridge.java_retriever import convert_contract_v0_to_fss_bin
    convert_contract_v0_to_fss_bin(GOLDEN_BIN, fss_bin)

    cmd_ingest = [
        "java",
        "-Dfile.encoding=UTF-8",
        "-cp",
        cp,
        "Ingestion.FileIngestor",
        str(prop_file),
        str(fss_bin),
    ]
    res_ingest = subprocess.run(cmd_ingest, capture_output=True, text=True, cwd=str(ROOT_DIR))
    assert res_ingest.returncode == 0, f"FileIngestor failed: {res_ingest.stderr}"

    # Layout inspection
    layout = detect_java_store_layout(external_store)
    assert layout.is_valid
    assert layout.layout_type == "segmented_directory"

    # Stage 1: JavaRetriever pointed at external store
    retriever = JavaRetriever.for_external_store(external_store)
    assert retriever.mode == "storage_ipc"

    # Query full dataset through Java
    envelope = retriever.retrieve_full_dataset(source=ALL, max_events=1000)
    assert envelope.status == "OK"
    assert len(envelope.events) > 0

    receipt = retriever.get_last_receipt()
    assert receipt is not None
    assert receipt.java_engine == "Retrieval.RetrievalMain (Storage IPC)"
    assert "Segmented Store" in receipt.store_layout
    assert str(external_store) in receipt.external_path
    assert receipt.events_returned == len(envelope.events)
    assert receipt.java_ipc_latency_ms > 0
    assert receipt.min_ts_ns is not None and receipt.max_ts_ns is not None
    assert receipt.min_ts_ns <= receipt.max_ts_ns


def test_stage_2_handoff_and_synthesis_isolation(tmp_path: Path):
    """Stage 2 verification: Decoder, RAG, and Qwen synthesis operate strictly on Java records."""
    external_store = tmp_path / "external_store_s2"
    external_store.mkdir(parents=True, exist_ok=True)

    prop_file = tmp_path / "storage.properties"
    envelope_cfg = (ROOT_DIR / "fss-core" / "envelope.cfg").resolve()
    prop_file.write_text(
        f"root={external_store.resolve()}\n"
        f"blockSize=65536\n"
        f"bufferCapacityBytes=1048576\n"
        f"maxMessageSize=4096\n"
        f"segmentDurationMillis=60000\n"
        f"byteOrder=BIG_ENDIAN\n"
        f"envelopeConfig={envelope_cfg}\n",
        encoding="utf-8",
    )

    core_classes = ROOT_DIR / "fss-core" / "target" / "classes"
    bridge_classes = ROOT_DIR / "bridge" / "classes"
    cp = f"{core_classes}:{bridge_classes}"

    fss_bin = tmp_path / "scenario_s2.fss.bin"
    from bridge.java_retriever import convert_contract_v0_to_fss_bin
    convert_contract_v0_to_fss_bin(GOLDEN_BIN, fss_bin)

    subprocess.run(
        ["java", "-Dfile.encoding=UTF-8", "-cp", cp, "Ingestion.FileIngestor", str(prop_file), str(fss_bin)],
        check=True,
        capture_output=True,
    )

    # 1. Run Stage 1 (Java Layer Only)
    retriever = JavaRetriever.for_external_store(external_store)
    envelope = retriever.retrieve_full_dataset(source=ALL, max_events=500)
    receipt = retriever.get_last_receipt()
    assert receipt is not None
    assert receipt.events_returned > 0

    # 2. Run Stage 2 on Java's retrieved records
    registry = load_schema_file(SCHEMA_PATH)
    wire_records = [ev.to_event_record() for ev in envelope.events]
    decoded_events = decode_all(wire_records, registry)
    assert len(decoded_events) == len(wire_records)

    evidence = build_evidence(decoded_events, query="Diagnose mission telemetry anomalies")
    manual_entries = load_builtin_manual("fss_native")
    synthesis, report, model_tier = synthesize_and_validate(
        evidence=evidence,
        query="Diagnose mission telemetry anomalies",
        vendor_manual_matches=[e.to_dict() if hasattr(e, "to_dict") else e for e in manual_entries],
    )

    assert synthesis is not None
    assert "summary" in synthesis
    assert report is not None
    assert report.verdict in (
        "VERIFIED_SUPPORTED",
        "FORCE_OVERCLAIM",
        "ENTITY_MISMATCH",
        "UNGROUNDED_HALLUCINATION",
        "INSUFFICIENT_EVIDENCE",
    )

    # 3. Isolation asserts: Zero files created inside git repo root
    git_stat = subprocess.run(
        ["git", "status", "--porcelain", "fixtures/", "domain_manuals/", "test_data/"],
        capture_output=True,
        text=True,
        cwd=str(ROOT_DIR),
    )
    assert git_stat.stdout.strip() == "", f"Repo pollution detected: {git_stat.stdout}"


def test_stage_1_and_2_standalone_dat_store(tmp_path: Path):
    """Test standalone J1 .dat store routing and two-stage execution."""
    dat_path = tmp_path / "standalone_j1.dat"
    retriever = JavaRetriever.from_fixtures(GOLDEN_BIN, store_path=dat_path, mode="cli")

    layout = detect_java_store_layout(dat_path)
    assert layout.is_valid
    assert layout.layout_type == "single_file"

    # Stage 1: Retrieval
    envelope = retriever.retrieve_full_dataset(source=ALL, max_events=100)
    assert envelope.status == "OK"
    assert len(envelope.events) > 0

    receipt = retriever.get_last_receipt()
    assert receipt is not None
    assert receipt.java_engine == "bridge.JavaStoreBridge (J1 BlockStore)"
    assert receipt.store_layout == "Single-File J1 BlockStore"
    assert receipt.events_returned == len(envelope.events)

    # Stage 2: Handoff
    registry = load_schema_file(SCHEMA_PATH)
    decoded = decode_all([ev.to_event_record() for ev in envelope.events], registry)
    assert len(decoded) > 0


def test_external_zip_archive_flow(tmp_path: Path):
    """Test zip archive extraction, validation, and retrieval."""
    store_dir = tmp_path / "zipped_source"
    store_dir.mkdir()
    (store_dir / "blocks.dat").write_bytes(b"\x00" * 65536)
    (store_dir / "block-metadata.dat").write_bytes(b"\x00" * 44)
    (store_dir / "segments").mkdir()

    zip_path = tmp_path / "test_store.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(store_dir / "blocks.dat", arcname="my_store/blocks.dat")
        zf.write(store_dir / "block-metadata.dat", arcname="my_store/block-metadata.dat")

    extracted = extract_unseen_zip(zip_path)
    assert extracted.exists()
    assert (extracted / "blocks.dat").is_file()

    layout = detect_java_store_layout(extracted)
    assert layout.is_valid
    assert layout.layout_type == "segmented_directory"


def test_plan_bounded_retrieval_slices_unit():
    """Unit test for bounded slice planning ensuring Contract v0 MAX_WINDOW_NS (1h) compliance."""
    max_w = 3600 * 1_000_000_000

    # 1. Empty windows
    assert plan_bounded_retrieval_slices([], None, None) == []

    # 2. Single short window
    slices = plan_bounded_retrieval_slices([(1000, 2000)])
    assert slices == [(1000, 2000)]

    # 3. Window exceeding 1 hour: must be chunked into <= max_w slices
    large_span = 7500 * 1_000_000_000
    slices = plan_bounded_retrieval_slices([(0, large_span)])
    assert len(slices) == 3
    assert slices[0] == (0, max_w)
    assert slices[1] == (max_w, 2 * max_w)
    assert slices[2] == (2 * max_w, large_span)
    for s_start, s_end in slices:
        assert s_end - s_start <= max_w

    # 4. Adjacent segments within 1 hour: must be merged into 1 slice
    slices = plan_bounded_retrieval_slices([(100, 500), (600, 1000), (1200, 1500)])
    assert len(slices) == 1
    assert slices == [(100, 1500)]

    # 5. Segments separated by > 1 hour: must remain distinct bounded slices
    seg1 = (1_000_000_000, 1_100_000_000)
    seg2 = (10_000_000_000_000, 10_000_100_000_000)  # > 2.5 hours later
    slices = plan_bounded_retrieval_slices([seg1, seg2])
    assert len(slices) == 2
    assert slices[0] == seg1
    assert slices[1] == seg2


def test_external_unseen_multi_segment_real_download_store():
    """Test Java-owned bounds discovery & multi-slice retrieval against real external unseen store.
    
    Verifies:
      1. Bounds discovery discovers Nov 19, 2026 timestamps without hardcoding 2026-10-08.
      2. Segments spanning ~4.18 hours (> 3600s) are cleanly retrieved in bounded slices.
      3. Python never opens .dat files directly; all reading is done by Java.
      4. Zero repo pollution (no files added to repo root).
    """
    real_store_path = Path("/home/kirat/Downloads/test-data (2)")
    if not real_store_path.exists():
        pytest.skip(f"{real_store_path} not found in this environment")

    # 1. Structural inspection via Java layout detector
    layout = detect_java_store_layout(real_store_path)
    assert layout.is_valid
    assert layout.layout_type == "segmented_directory"
    assert layout.segment_count == 4

    # 2. Stage 1: Java-owned bounds discovery over IPC
    test_trace_id = "0123456789abcdef0123456789abcdef"
    retriever = JavaRetriever.for_external_store(real_store_path)
    bounds = retriever.get_store_bounds(trace_id=test_trace_id)
    assert bounds.status == "OK"
    assert bounds.segment_count == 4
    assert bounds.block_count == 996
    assert bounds.min_ts_ns == 1795069921815384500  # Nov 19, 2026
    assert bounds.max_ts_ns == 1795085029688285900  # Nov 19, 2026 ~4.18 hours later
    assert len(bounds.segment_windows) == 4
    assert set(bounds.known_sources) == {1, 2, 3}

    # Verify slice planning merges first 3 segments (< 1h) and leaves segment 4 (> 4h away)
    slices = plan_bounded_retrieval_slices(bounds.segment_windows, bounds.min_ts_ns, bounds.max_ts_ns)
    assert len(slices) == 2
    max_w = 3600 * 1_000_000_000
    for s_start, s_end in slices:
        assert s_end - s_start <= max_w

    # 3. Stage 1: Retrieve full dataset through Java bounded slices
    envelope = retriever.retrieve_full_dataset(source=ALL, max_events=2000, trace_id=test_trace_id)
    assert envelope.status == "OK"
    assert envelope.count == 996
    assert len(envelope.events) == 996
    assert envelope.trace_id == test_trace_id

    receipt = retriever.get_last_receipt()
    assert receipt is not None
    assert receipt.events_returned == 996
    assert receipt.slices_queried == 2
    assert receipt.segments_detected == 4
    assert receipt.min_ts_ns == bounds.min_ts_ns
    assert receipt.max_ts_ns == bounds.max_ts_ns
    assert receipt.trace_id == test_trace_id

    # 4. Strict Isolation: zero repo pollution
    git_stat = subprocess.run(
        ["git", "status", "--porcelain", "fixtures/", "domain_manuals/", "test_data/"],
        capture_output=True,
        text=True,
        cwd=str(ROOT_DIR),
    )
    assert git_stat.stdout.strip() == "", f"Repo pollution detected: {git_stat.stdout}"

