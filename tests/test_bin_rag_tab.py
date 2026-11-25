"""Tests for isolated .bin Java ingestion + Company Manual RAG session and Streamlit tab."""

from __future__ import annotations

from datetime import timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from bridge.bin_rag_session import (
    discover_bin_files,
    discover_manual_files,
    prepare_bin_and_manual_session,
)
from bridge.investigation import (
    ANALYSIS_SCHEMA,
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    run_investigation,
)
from bridge.schemas import format_local_timestamp_ns
from query_app import _claim_text

ROOT = Path(__file__).resolve().parent.parent


def test_discover_bins_and_manuals():
    bins = discover_bin_files(ROOT)
    manuals = discover_manual_files(ROOT)
    assert any(p.name == "swarm_298_474_opaque.fss.bin" for p in bins)
    assert any(p.name == "aerodrone_inc_manual.md" for p in manuals)


def test_invalid_binary_raises_clear_error():
    with pytest.raises(ValueError, match="Unsupported|Invalid|empty"):
        prepare_bin_and_manual_session(("bad.bin", b"NOT_A_VALID_BIN_HEADER_12345"), root=ROOT)


def test_isolated_bin_ingestion_manual_rag_and_investigation_citations():
    bin_path = ROOT / "test_data_and_manuals/06_custom_drone_swarm_298_474/swarm_298_474_opaque.fss.bin"
    manual_bytes = (
        ROOT / "test_data_and_manuals/06_custom_drone_swarm_298_474/aerodrone_inc_manual.md"
    ).read_bytes()

    session = prepare_bin_and_manual_session(
        bin_path,
        ("aerodrone_inc_manual.md", manual_bytes),
        root=ROOT,
    )
    workspace_dir = session.workspace_dir
    try:
        # Must isolate away from root test-data
        assert session.workspace_dir.exists()
        assert session.total_events == 14
        assert session.source_ids == [298, 474, 901]
        assert session.manual_filename == "aerodrone_inc_manual.md"
        assert session.manual_chunks >= 1
        assert (workspace_dir / "store" / "blocks.dat").is_file()

        # Verify retrieval through real Java IPC client
        retriever = session.get_retriever(ROOT)
        from bridge.retrieval_ipc import JavaRetrievalRequest

        ipc_res = retriever.retrieve(
            JavaRetrievalRequest(
                "all", session.min_timestamp_ns, max(session.max_timestamp_ns, session.min_timestamp_ns + 1), None, 5000
            )
        )
        assert len(ipc_res.events) == 14

        # Verify isolated manual chunks and clean citation formatting via deterministic fake model
        docs = session.retrieve_knowledge({}, "LIDAR_TIMEOUT LEFT_FORMATION COLLISION_RISK", top_k=4)
        assert docs
        doc_id = docs[0]["id"]
        target_ev = ipc_res.events[0]
        target_ts_iso = format_local_timestamp_ns(target_ev.timestamp_ns)
        target_schema = session.registry.get(target_ev.event_type)
        target_ev_name = target_schema.name if target_schema else f"0x{target_ev.event_type:02X}"

        class _HybridStubModel:
            model_label = "Stub model"

            def generate(self, system_prompt, user_prompt, json_schema):
                if json_schema is PLAN_SCHEMA:
                    return {
                        "goal": "Diagnose drone deviation using telemetry and company manual.",
                        "timeframe_basis": f"{session.default_start_local} to {session.default_end_local}",
                        "timeframe_is_bounded": True,
                        "start_local": session.default_start_local,
                        "end_local": session.default_end_local,
                        "source": "all",
                        "event_type": None,
                        "clarification": None,
                    }
                if set(json_schema.get("properties", {})) == set(ANALYSIS_SCHEMA["properties"]):
                    ev_cit = {
                        "source_id": target_ev.source_id,
                        "timestamp_iso8601": target_ts_iso,
                        "event_type": target_ev_name,
                    }
                    return {
                        "root_cause_hypothesis": {
                            "statement": "Observed event aligns with documented LIDAR_TIMEOUT and LEFT_FORMATION protocol.",
                            "citations": [ev_cit, doc_id],
                        },
                        "observed_facts": [
                            {"statement": f"Source {target_ev.source_id} emitted {target_ev_name}.", "citations": [ev_cit]}
                        ],
                        "inferences": [
                            {
                                "statement": "Documented thresholds likely explain the transition.",
                                "citations": [ev_cit, doc_id],
                                "confidence": "medium",
                            }
                        ],
                        "uncertainties": [],
                        "data_gaps": [],
                    }
                if json_schema is REVIEW_SCHEMA:
                    assert docs[0]["content"][:30] in user_prompt or doc_id in user_prompt
                    return {
                        "decision": "supported",
                        "rationale": "Telemetry and company manual citations both validate.",
                        "missing_information": [],
                        "clarification": None,
                        "next_retrieval": None,
                    }
                raise AssertionError(f"Unexpected schema: {json_schema}")

        result = run_investigation(
            f"Why did agent deviate between {session.default_start_local} and {session.default_end_local}?",
            model=_HybridStubModel(),
            retriever=retriever,
            registry=session.registry,
            knowledge_retriever=session.retrieve_knowledge,
        )
        assert result["status"] == "supported"
        assert result["audit"]["accepted"] is True
        assert result["audit"]["invalid_citations"] == []
        assert doc_id in result["references"]
        assert result["references"][doc_id]["kind"] == "document"

        # Verify UI document citation rendering
        rendered_hyp = _claim_text(result["root_cause_hypothesis"], references=result["references"])
        assert "Manual · aerodrone_inc_manual.md" in rendered_hyp
        assert f"D{target_ev.source_id}" in rendered_hyp
    finally:
        session.close()
    assert not workspace_dir.exists()


def test_streamlit_tabs_render_and_bin_manual_ingest():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    assert len(at.exception) == 0, at.exception
    assert len(at.chat_input) == 1
    assert len(at.tabs) == 2

    # Defaults must NOT preselect a .bin or manual: manual upload modes are default
    assert at.radio(key="bin_source_mode").value == "Upload .bin file"
    assert at.radio(key="manual_source_mode").value == "Upload Company Manual"

    # Clicking ingest without uploading or selecting shows a clear prompt instead of ingesting
    at.button(key="ingest_prepare_btn").click()
    at.run()
    assert len(at.exception) == 0, at.exception
    assert any("Select or upload" in err.value for err in at.error)
    assert at.session_state["bin_rag_session"] is None

    # Switching to existing selectors must start unselected (value=None)
    at.radio(key="bin_source_mode").set_value("Use existing .bin file")
    at.radio(key="manual_source_mode").set_value("Use existing manual")
    at.run()
    assert len(at.exception) == 0, at.exception
    bin_sel = at.selectbox(key="bin_existing_select")
    manual_sel = at.selectbox(key="manual_existing_select")
    assert bin_sel.value is None
    assert manual_sel.value is None

    bin_sel.set_value("test_data_and_manuals/06_custom_drone_swarm_298_474/swarm_298_474_opaque.fss.bin")
    manual_sel.set_value("test_data_and_manuals/06_custom_drone_swarm_298_474/aerodrone_inc_manual.md")
    at.button(key="ingest_prepare_btn").click()
    at.run()
    assert len(at.exception) == 0, at.exception
    session = at.session_state["bin_rag_session"]
    try:
        assert session is not None
        assert session.total_events == 14
        # Verify another rerun does NOT lose or re-create session unnecessarily
        prev_dir = session.workspace_dir
        at.run()
        assert len(at.exception) == 0, at.exception
        assert at.session_state["bin_rag_session"].workspace_dir == prev_dir
    finally:
        if at.session_state["bin_rag_session"] is not None:
            at.session_state["bin_rag_session"].close()
