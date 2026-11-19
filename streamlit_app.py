"""Streamlit Modernized Front End for Failure Synthesis System (FSS).

Integrates:
  - Java BlockStore bounded retrieval via JavaStoreBridge & JavaRetriever
  - Python Contract v0 payload decoder & evidence builder
  - 3-tier Qwen RAG synthesizer (Ollama, local HuggingFace weights, deterministic fallback)
  - Strict citation validator enforcing 16 FSS Hard Rules and fss-evidence/v0 contract

Renders the complete interactive 8-part UI:
  - Part 1: Question & Part 2: Retrieval Bounds (Header & metadata badges)
  - Part 8: Validator Verdict (Prominent ACCEPTED / ACCEPTED_WITH_CORRECTIONS / REJECTED badge)
  - Part 7: Synthesis (Root cause, contributing event pills, clock caveats, data gaps)
  - Interactive Tabs:
      * Tab 1: Timeline & Epistemic Tags (Decoded fields + Documented enums + Unknown gap bytes)
      * Tab 2: Data Gaps Quarantine (Quarantine cards for OPAQUE_BYTES, UNKNOWN_EVENT_TYPE, etc.)
      * Tab 3: Domain Knowledge RAG (Retrieved documentation chunks)
      * Tab 4: Prompt Context (Audit trail of exact prompt)
      * Tab 5: Citation Validation Audit (Table of verified citations, validity check, and notes)
      * Tab 6: Raw Evidence JSON (Format fss-evidence/v0)
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 1. Project path and environment setup
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ensure local proxy compatibility: set NO_PROXY for local communication
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

# Protobuf compatibility with Python 3.14 (bypass C extension tp_new incompatibility)
sys.modules.setdefault("google._upb._message", None)
sys.modules.setdefault("google.protobuf.pyext._message", None)

uv_archives = Path("/home/kirat/.cache/uv/archive-v0")
if uv_archives.is_dir():
    for d in sorted(uv_archives.iterdir(), reverse=True):
        if d.is_dir() and str(d) not in sys.path:
            has_incompatible = any(
                f.name.startswith("numpy") or f.name.startswith("pyarrow")
                for f in d.iterdir()
            )
            if not has_incompatible:
                sys.path.insert(0, str(d))

import streamlit as st

from fss_py.contract import UNKNOWN_LABEL
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, RetrievalRequestError, RetrievalResult
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

from bridge.java_retriever import JavaRetriever
from bridge.synthesizer import (
    DOMAIN_KNOWLEDGE,
    RULES,
    ValidationReport,
    build_llm_context,
    check_ollama_available,
    retrieve_relevant_knowledge,
    synthesize_and_validate,
)

SCHEMA_PATH = ROOT / "schemas" / "drone_v0.yaml"
GOLDEN_DIR = ROOT / "fixtures" / "golden"
BRIDGE_DIR = ROOT / "bridge"
MAX_WINDOW_NS = 3600 * 10**9  # 1 hour max retrieval window contract


def fmt_ns(ns: int | None) -> str:
    """Format nanoseconds as UTC datetime string with millisecond precision."""
    if ns is None:
        return "N/A"
    dt = datetime.fromtimestamp(ns // 10**9, tz=timezone.utc)
    return f"{dt:%Y-%m-%d %H:%M:%S}.{ns % 10**9 // 10**6:03d}"


def epistemic_tag(ev: dict[str, Any]) -> str:
    """Compute the epistemic status tag (decoded, documented, unknown bytes)."""
    status = ev.get("decode_status", "SUCCESS")
    has_doc = bool(ev.get("interpreted_fields"))
    unknown = ev.get("unknown_bytes", 0)

    if status == "UNKNOWN_EVENT_TYPE":
        return f"unknown ({ev.get('payload_len', 0)} bytes)"
    if status == "MALFORMED_PAYLOAD":
        return "malformed"
    if status == "PARTIALLY_DECODED":
        doc_part = " + documented" if has_doc else ""
        return f"decoded{doc_part} + unknown ({unknown} bytes)"
    if has_doc:
        return "decoded + documented"
    return "decoded"


def render_table(rows: list[dict[str, Any]], headers: list[str] | None = None) -> None:
    """Render tabular data robustly with automatic fallback."""
    if not rows:
        st.info("No records to display.")
        return
    try:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    except Exception:
        try:
            st.table(rows)
        except Exception:
            cols = headers if headers else list(rows[0].keys())
            lines = [
                "| " + " | ".join(cols) + " |",
                "| " + " | ".join(["---"] * len(cols)) + " |",
            ]
            for r in rows:
                line_vals = [str(r.get(c, "")).replace("|", "\\|") for c in cols]
                lines.append("| " + " | ".join(line_vals) + " |")
            st.markdown("\n".join(lines))


@st.cache_resource(show_spinner="Initializing Java BlockStore from fixtures...")
def get_java_retriever(fixture_paths: tuple[str, ...]) -> JavaRetriever:
    """Create JavaRetriever and ingest fixture files into a temporary or cached BlockStore."""
    return JavaRetriever.from_fixtures(*fixture_paths)


@st.cache_resource(show_spinner="Connecting to active Java BlockStore...")
def get_java_retriever_for_store(store_path: str) -> JavaRetriever:
    """Connect JavaRetriever directly to an existing .dat BlockStore."""
    return JavaRetriever(store_path)


@st.cache_resource
def get_schema_registry():
    """Load and cache the schema registry."""
    return load_schema_file(SCHEMA_PATH)


def main() -> None:
    st.set_page_config(
        page_title="Failure Synthesis System (FSS)",
        page_icon="🛸",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Custom styling
    st.markdown(
        """
        <style>
        .metric-card {
            background-color: rgba(28, 131, 225, 0.08);
            border: 1px solid rgba(28, 131, 225, 0.2);
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 10px;
        }
        .pill-verified {
            display: inline-block;
            background-color: #2e7d32;
            color: white;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 0.85em;
            font-weight: 500;
            margin: 2px;
        }
        .pill-unverified {
            display: inline-block;
            background-color: #c62828;
            color: white;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 0.85em;
            font-weight: 500;
            margin: 2px;
        }
        .pill-tag {
            display: inline-block;
            background-color: #424242;
            color: #eeeeee;
            padding: 2px 7px;
            border-radius: 10px;
            font-size: 0.8em;
            margin-right: 4px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("🛸 Failure Synthesis System (FSS)")
    st.caption(
        "Modernized 8-Part Architecture: Java 64KB BlockStore ⟷ Contract v0 Wire ⟷ "
        "Deterministic Decoder ⟷ Qwen RAG Synthesizer ⟷ Grounded Citation Validator"
    )

    # Sidebar: Data Source & Ingestion Configuration
    with st.sidebar:
        st.header("📦 Storage & Data Source")

        # Discover fixtures
        fixture_files = sorted(p.name for p in GOLDEN_DIR.glob("*.bin")) if GOLDEN_DIR.is_dir() else []
        dat_files = sorted(p.name for p in BRIDGE_DIR.glob("*.dat")) if BRIDGE_DIR.is_dir() else []

        source_mode = st.radio(
            "Source Mode",
            ["Preset Binary Fixtures", "Existing Java BlockStore (.dat)", "Upload Custom Binary (.bin)"],
            index=0,
        )

        chosen_fixtures: list[Path] = []
        chosen_store: Path | None = None

        if source_mode == "Preset Binary Fixtures":
            default_selection = ["scenario_298_474.bin"] if "scenario_298_474.bin" in fixture_files else fixture_files[:1]
            selected_names = st.multiselect("Select Golden Fixtures", fixture_files, default=default_selection)
            chosen_fixtures = [GOLDEN_DIR / name for name in selected_names]
        elif source_mode == "Existing Java BlockStore (.dat)":
            if dat_files:
                selected_dat = st.selectbox("Select BlockStore File", dat_files, index=0)
                chosen_store = BRIDGE_DIR / selected_dat
            else:
                st.warning("No .dat BlockStore files found in bridge/")
        else:
            uploads = st.file_uploader("Upload wire envelope .bin files", type=["bin"], accept_multiple_files=True)
            if uploads:
                for up in uploads:
                    tmp_file = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
                    tmp_file.write(up.getvalue())
                    tmp_file.close()
                    chosen_fixtures.append(Path(tmp_file.name))

        if st.button("🔄 Refresh / Re-ingest Store", use_container_width=True):
            st.cache_resource.clear()
            st.rerun()

        st.divider()

    # Ingest / Connect Retriever
    retriever: JavaRetriever
    sample_envelopes = []

    try:
        if chosen_store is not None and chosen_store.exists():
            retriever = get_java_retriever_for_store(str(chosen_store))
        elif chosen_fixtures:
            fixture_strs = tuple(str(p.resolve()) for p in chosen_fixtures)
            retriever = get_java_retriever(fixture_strs)
            for p in chosen_fixtures:
                try:
                    sample_envelopes.extend(iter_envelopes(p.read_bytes()))
                except Exception:
                    pass
        else:
            st.info("👈 Please select at least one fixture or BlockStore file in the sidebar to begin.")
            st.stop()
    except Exception as exc:
        st.error(f"Failed to initialize Java BlockStore: {exc}")
        st.stop()

    # Time bounds inspection
    if sample_envelopes:
        all_sources = sorted({e.source_id for e in sample_envelopes})
        t_min = min(e.timestamp_ns for e in sample_envelopes)
        t_max = max(e.timestamp_ns for e in sample_envelopes)
    else:
        all_sources = []
        t_min = 1791454020000000000
        t_max = 1791454025000000000

    span_s = max(1, -(-(t_max - t_min + 1) // 10**9))

    # Sidebar: Retrieval Bounds
    with st.sidebar:
        st.header("⏱️ Retrieval Bounds (Contract v0)")
        st.caption(f"Data timestamps: **{fmt_ns(t_min)}** → **{fmt_ns(t_max)}** UTC")

        offset = st.slider("Offset (seconds from start)", 0, max(0, span_s - 1), 0) if span_s > 1 else 0
        max_duration = min(3600, max(1, span_s - offset + 5))
        duration = st.slider("Window Length (seconds, max 3600)", 1, 3600, max_duration)

        source_options = ["ALL"] + [str(s) for s in all_sources]
        source_selection = st.selectbox("Source Filter", source_options, index=0)
        max_events = st.number_input("Max Events", min_value=1, max_value=10000, value=100, step=10)

        t_start_ns = t_min + offset * 10**9
        t_end_ns = min(t_start_ns + duration * 10**9, t_start_ns + MAX_WINDOW_NS)

        st.divider()
        st.header("🤖 Qwen Model Resolution")
        ollama_model, ollama_err = check_ollama_available()
        if ollama_model:
            st.success(f"🟢 Ollama Qwen ({ollama_model}) online")
        else:
            st.info("ℹ️ Ollama offline — automatic fallback to Local HF Qwen / Deterministic reasoning")

    # Main Area: Question & Investigation Input
    st.subheader("1. Operator Investigation Query")
    sample_queries = [
        "Why did drone 474 leave formation?",
        "What went wrong across the fleet during this window?",
        "Did drone 298 report any sensor or LiDAR faults?",
        "What is the root cause of the formation divergence?",
    ]
    query_preset = st.selectbox("Quick Preset Questions", ["Custom Query..."] + sample_queries, index=1)
    if query_preset != "Custom Query...":
        default_query = query_preset
    else:
        default_query = "Why did drone 474 leave formation?"

    query = st.text_input("Enter your investigation question:", value=default_query)
    if not query.strip():
        st.warning("Please enter a question to run failure synthesis.")
        st.stop()

    # Execute Bounded Retrieval against Java BlockStore
    source_arg = ALL if source_selection == "ALL" else int(source_selection)
    with st.spinner("Invoking Java BlockStore bounded retrieval..."):
        try:
            retrieval_res: RetrievalResult = retriever.retrieve(
                source=source_arg,
                t_start_ns=t_start_ns,
                t_end_ns=t_end_ns,
                max_events=int(max_events),
            )
        except RetrievalRequestError as exc:
            st.error(f"Retrieval Request Contract Violation: {exc}")
            st.stop()
        except Exception as exc:
            st.error(f"JavaStoreBridge Retrieval Failure: {exc}")
            st.stop()

    # Decode Envelopes & Assemble Evidence
    registry = get_schema_registry()
    decoded_events = decode_all(retrieval_res.events, registry)
    evidence = build_evidence(decoded_events, query=query, retrieval=retrieval_res)

    # Fetch Domain Knowledge RAG & Synthesize + Validate
    with st.spinner("Executing Qwen RAG synthesis & citation validation..."):
        knowledge_chunks = retrieve_relevant_knowledge(evidence, query, top_k=8)
        synthesis, val_report, model_used = synthesize_and_validate(evidence, query)
        llm_context = build_llm_context(evidence, query, knowledge_chunks)

    # -------------------------------------------------------------------------
    # Render Complete 8-Part UI
    # -------------------------------------------------------------------------

    # [Part 1 & 2]: Header and Retrieval Bounds
    col1, col2, col3, col4, col5 = st.columns([2, 2, 2, 2, 2])
    with col1:
        st.metric("Source Filter", "ALL" if source_arg is ALL else str(source_arg))
    with col2:
        st.metric("Events Retrieved", f"{len(retrieval_res.events)} / {max_events}")
    with col3:
        st.metric("Truncated", "⚠️ YES" if retrieval_res.truncated else "NO")
    with col4:
        st.metric("Storage Engine", "Java BlockStore")
    with col5:
        st.metric("Model Tier", model_used.split()[0])

    st.caption(
        f"**Retrieval Bounds:** `[{fmt_ns(t_start_ns)}, {fmt_ns(t_end_ns)}) UTC` "
        f"({(t_end_ns - t_start_ns) / 10**9:.2f} s window) | Storage: 64KB CRC-32C Blocks"
    )

    st.markdown("---")

    # [Part 8]: Validator Verdict Badge (Prominently Rendered)
    verdict = val_report.verdict
    if verdict == "ACCEPTED":
        st.success(
            f"### 🛡️ Part 8: Validator Verdict: ACCEPTED\n"
            f"**Grounded Verification PASSED**: All factual claims strictly verified against evidence. "
            f"**Zero hallucinated citations** ({len(val_report.valid_citations)} verified: "
            f"`{'`, `'.join(val_report.valid_citations)}`)."
        )
    elif verdict == "ACCEPTED_WITH_CORRECTIONS":
        st.warning(
            f"### 🛡️ Part 8: Validator Verdict: ACCEPTED WITH CORRECTIONS\n"
            f"**Grounded Verification PASSED WITH ADJUSTMENTS**: All citations verified ({len(val_report.valid_citations)} valid). "
            f"Automated validator safety rules added {len(val_report.notes)} omission safeguard(s)."
        )
    else:
        st.error(
            f"### 🛡️ Part 8: Validator Verdict: REJECTED\n"
            f"**Hallucination Gate BLOCKED**: Identified invalid citations or ungrounded claims: "
            f"`{val_report.invalid_citations}`."
        )

    # [Part 7]: Synthesis Card
    confidence = (synthesis.get("confidence") or "medium").lower()
    conf_color = "🟢" if confidence == "high" else ("🟡" if confidence == "medium" else "🔴")

    with st.container():
        st.markdown(f"### 🔬 Part 7: Grounded Failure Synthesis ({conf_color} {confidence.upper()} Confidence)")

        summary = synthesis.get("summary", "No summary produced.")
        st.markdown(f"**Executive Summary:** {summary}")

        likely_cause = synthesis.get("likely_cause")
        if likely_cause:
            st.markdown(f"**Root-Cause Sequence:** {likely_cause}")
        else:
            st.markdown("**Root-Cause Sequence:** Nominal behavior — no anomalies detected in this retrieval window.")

        # Contributing Events Pills
        contributing = synthesis.get("contributing_events", [])
        if contributing:
            st.markdown("**Contributing Events (Verified Citations):**")
            pills_html = []
            for ce in contributing:
                eid = ce.get("event_id", "unknown")
                etype = ce.get("event_type", "EVENT")
                src = ce.get("source_id", "?")
                ts = fmt_ns(ce.get("timestamp"))[11:]
                is_valid = eid in val_report.valid_citations
                pill_class = "pill-verified" if is_valid else "pill-unverified"
                pills_html.append(
                    f'<span class="{pill_class}">[{eid}] Drone {src} • {etype} • {ts}</span>'
                )
            st.markdown(" ".join(pills_html), unsafe_allow_html=True)

        # Clock Caveat Notice
        if len(evidence.get("sources", [])) > 1:
            caveat = evidence.get(
                "clock_caveat",
                "Producer clocks are not synchronized across sources. Relative inter-source ordering is inferred.",
            )
            st.info(f"⏱️ **Clock Synchronization Caveat:** {caveat}")

        # Data gaps notice if any
        if synthesis.get("data_gaps"):
            with st.expander(f"⚠️ Quarantined Data Gaps Reported in Synthesis ({len(synthesis['data_gaps'])})"):
                for gap in synthesis["data_gaps"]:
                    st.markdown(f"- {gap}")

    st.markdown("---")

    # Interactive Inspection Tabs (Parts 3, 4, 5, 6, Audit & Raw JSON)
    tab_timeline, tab_gaps, tab_rag, tab_prompt, tab_audit, tab_raw = st.tabs(
        [
            "📑 Tab 1: Timeline & Epistemic Tags",
            "🛡️ Tab 2: Data Gaps Quarantine",
            "📚 Tab 3: Domain Knowledge RAG",
            "🔍 Tab 4: Prompt Context",
            "⚖️ Tab 5: Citation Validation Audit",
            "💾 Tab 6: Raw Evidence JSON",
        ]
    )

    # Tab 1: Timeline & Epistemic Tags (Grouped by Source)
    with tab_timeline:
        st.caption(evidence.get("ordering", "Grouped by source_id, sorted by producer timestamp."))
        events_list = evidence.get("events", [])
        if events_list:
            sources_present = evidence.get("sources", [])
            for src in sources_present:
                src_events = [e for e in events_list if e["source_id"] == src]
                st.subheader(f"Drone Source {src} ({len(src_events)} events)")
                rows = []
                for ev in src_events:
                    fields_str = ", ".join(f"{k}={v}" for k, v in ev.get("fields", {}).items())
                    interp_str = ", ".join(ev.get("interpreted_fields", [])) or "none"
                    rows.append(
                        {
                            "Event ID": ev["event_id"],
                            "Producer Time (UTC)": fmt_ns(ev["timestamp_ns"]),
                            "Event Name": ev.get("event_name") or str(ev.get("event_type")),
                            "Status": ev.get("decode_status", "SUCCESS"),
                            "Epistemic Tag": epistemic_tag(ev),
                            "Decoded Fields": fields_str,
                            "Documented Enums": interp_str,
                            "Unknown Bytes": ev.get("unknown_bytes", 0),
                        }
                    )
                render_table(rows)
        else:
            st.info("No events retrieved in this bounded time window.")

    # Tab 2: Data Gaps Quarantine
    with tab_gaps:
        gaps = evidence.get("data_gaps", [])
        if gaps:
            st.markdown(f"**Quarantined Data Gaps:** {len(gaps)} anomalous condition(s) isolated from LLM speculation.")
            for g in gaps:
                kind = g.get("kind", "DATA_GAP")
                eid = g.get("event_id")
                src = g.get("source_id")
                detail = g.get("detail", "")
                with st.container():
                    st.warning(
                        f"**[{kind}]** {f'Event {eid} • ' if eid else ''}"
                        f"{f'Source {src} • ' if src is not None else ''}{detail}"
                    )
        else:
            st.success("✅ **Zero Data Gaps**: All events were 100% cleanly decoded with known schemas and enums.")

    # Tab 3: Domain Knowledge RAG
    with tab_rag:
        st.markdown(f"**Retrieved {len(knowledge_chunks)} Domain Documentation Chunk(s) via RAG:**")
        for chunk in knowledge_chunks:
            with st.expander(f"📖 {chunk['title']} (`{chunk['chunk']}`) — explains `{chunk.get('explains', '')}`"):
                st.markdown(f"**Domain:** `{chunk['domain']}` | **Kind:** `{chunk['kind']}`")
                st.markdown(f"> {chunk['content']}")

    # Tab 4: Prompt Context Audit
    with tab_prompt:
        st.markdown("**Complete Grounded Prompt Context sent to Model:**")
        st.text_area("Audit Trail (fss-evidence/v0 formatting)", llm_context, height=350)

    # Tab 5: Citation Validation Audit
    with tab_audit:
        st.markdown("### Citation Verification & Hallucination Gate Results")
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Total Valid Citations", len(val_report.valid_citations))
        with c2:
            st.metric("Invalid Citations", len(val_report.invalid_citations))
        with c3:
            st.metric("Validator Status", val_report.verdict)

        st.subheader("Citation Audit Detail")
        audit_rows = []
        for cid in val_report.valid_citations:
            ev = next((e for e in evidence.get("events", []) if e["event_id"] == cid), None)
            audit_rows.append(
                {
                    "Citation": cid,
                    "Status": "✅ VERIFIED GROUNDED",
                    "Source": ev["source_id"] if ev else "N/A",
                    "Event Name": ev.get("event_name", "N/A") if ev else "N/A",
                    "Timestamp": fmt_ns(ev["timestamp_ns"]) if ev else "N/A",
                }
            )
        for cid in val_report.invalid_citations:
            audit_rows.append(
                {
                    "Citation": cid,
                    "Status": "❌ REJECTED / HALLUCINATED",
                    "Source": "UNRECORDED",
                    "Event Name": "UNKNOWN",
                    "Timestamp": "N/A",
                }
            )
        if audit_rows:
            render_table(audit_rows)
        else:
            st.write("No citations were made in this synthesis.")

        if val_report.notes:
            st.subheader("Validator Audit Trail & Omission Adjustments")
            for note in val_report.notes:
                st.markdown(f"- ℹ️ {note}")

    # Tab 6: Raw Evidence JSON
    with tab_raw:
        st.download_button(
            label="📥 Download Evidence JSON (fss-evidence/v0)",
            data=json.dumps(evidence, indent=2),
            file_name=f"fss_evidence_{int(datetime.now().timestamp())}.json",
            mime="application/json",
        )
        st.json(evidence)


if __name__ == "__main__":
    main()
