"""Streamlit interface for query-driven, evidence-reviewed investigations."""

from __future__ import annotations

import atexit
import json
import re
from pathlib import Path
from typing import Any

import streamlit as st

from bridge.bin_rag_session import (
    BinRagSession,
    discover_bin_files,
    discover_manual_files,
    discover_schema_files,
    prepare_bin_and_manual_session,
)
from bridge.investigation import (
    InvestigationError,
    LocalModelBackend,
    _dedup_text_items,
    _normalize_text_key,
    format_epoch_ns_local,
    run_investigation,
)
from bridge.retrieval_ipc import RetrievalIpcClient, RetrievalIpcError
from fss_py.schema import DEFAULT_SCHEMA_PATH, SchemaError, SchemaRegistry, load_schema_file

ROOT = Path(__file__).resolve().parent
STORAGE_PROPERTIES = ROOT / "storage.properties"


@st.cache_resource
def get_schema_registry() -> SchemaRegistry:
    return load_schema_file(DEFAULT_SCHEMA_PATH)


@st.cache_resource(show_spinner="Starting the configured Java Retrieval IPC process...")
def get_retrieval_client(storage_properties: str) -> RetrievalIpcClient:
    client = RetrievalIpcClient(storage_properties, root=ROOT)
    atexit.register(client.close)
    return client


def main() -> None:
    st.set_page_config(
        page_title="FSS — Evidence Investigation",
        page_icon="📟",
        layout="centered",
    )
    try:
        registry = get_schema_registry()
    except (OSError, SchemaError) as exc:
        st.error(f"Payload schema configuration error: {exc}")
        st.stop()

    st.title("Failure Synthesis System")
    st.caption(
        "Ask a root-cause question with a local-time window of at most one hour. "
        "The system uses the configured Java store, can retrieve at most three times, "
        "and labels observed facts separately from inferences."
    )

    tab_live, tab_bin = st.tabs(["Live / BlockStore RCA", "BIN + Manual RCA"])

    investigations: list[dict[str, Any]] = st.session_state.setdefault("investigations", [])
    with tab_live:
        for idx, item in enumerate(investigations):
            _render_investigation(item, key_prefix=f"hist_{idx}")

    with tab_bin:
        _render_bin_manual_tab()

    submitted_query = st.chat_input(
        "What happened? Include the event timeframe in local time."
    )
    if not submitted_query:
        return

    with tab_live:
        with st.chat_message("user"):
            st.markdown(submitted_query)
        with st.chat_message("assistant"):
            try:
                model = LocalModelBackend.resolve()
                client = get_retrieval_client(str(STORAGE_PROPERTIES))
                with st.spinner("Planning, retrieving, and reviewing evidence..."):
                    result = run_investigation(
                        submitted_query,
                        model=model,
                        retriever=client,
                        registry=registry,
                    )
                investigations.append(result)
                _render_result(result, key_prefix=f"hist_{len(investigations) - 1}")
            except (InvestigationError, RetrievalIpcError, OSError, TimeoutError, ValueError) as exc:
                st.error(f"Investigation stopped: {exc}")


def _run_bin_rca_query(session: BinRagSession, query: str) -> None:
    """Execute one RCA & FSS run against the isolated .bin Java store + Company Manual RAG."""
    bin_investigations: list[dict[str, Any]] = st.session_state.setdefault("bin_investigations", [])
    try:
        model = LocalModelBackend.resolve()
        client = session.get_retriever(ROOT)
        with st.spinner("Planning, querying isolated Java store + manual RAG, and reviewing..."):
            result = run_investigation(
                query,
                model=model,
                retriever=client,
                registry=session.registry,
                knowledge_retriever=session.retrieve_knowledge,
            )
        bin_investigations.append(result)
    except (InvestigationError, RetrievalIpcError, OSError, TimeoutError, ValueError, RuntimeError) as exc:
        bin_investigations.append({"error": f"BIN + Manual RCA stopped: {exc}"})


def _render_bin_manual_tab() -> None:
    """Render the isolated .bin telemetry ingestion + Company Manual RAG investigation tab."""
    st.session_state.setdefault("bin_rag_session", None)
    st.subheader(".bin Telemetry + Company Manual RCA")
    st.caption(
        "Ingest any `.bin` telemetry log into an isolated Java BlockStore and optionally index a "
        "Company Manual into a session RAG corpus without modifying the default dataset."
    )

    available_bins = discover_bin_files(ROOT)
    bin_labels = {str(p.relative_to(ROOT)): p for p in available_bins}
    available_manuals = discover_manual_files(ROOT)
    manual_labels = {str(p.relative_to(ROOT)): p for p in available_manuals}
    schema_files = discover_schema_files(ROOT)
    schema_options: dict[str, Path | None] = {"Auto-detect from .bin": None}
    for sp in schema_files:
        schema_options[str(sp.relative_to(ROOT))] = sp

    col_bin, col_manual = st.columns(2)
    with col_bin:
        bin_mode = st.radio(
            "Telemetry (.bin) source",
            options=["Upload .bin file", "Use existing .bin file"],
            horizontal=True,
            key="bin_source_mode",
        )
        selected_bin_path: Path | None = None
        uploaded_bin = None
        if bin_mode == "Use existing .bin file":
            if bin_labels:
                chosen_bin_label = st.selectbox(
                    "Select repository .bin file",
                    options=list(bin_labels.keys()),
                    index=None,
                    placeholder="Choose an existing .bin file...",
                    key="bin_existing_select",
                )
                if chosen_bin_label:
                    selected_bin_path = bin_labels[chosen_bin_label]
            else:
                st.warning("No existing .bin files found under data folders.")
        else:
            uploaded_bin = st.file_uploader(
                "Upload telemetry binary (.bin / .fss.bin)",
                type=["bin"],
                key="bin_uploader",
            )

    with col_manual:
        manual_mode = st.radio(
            "Company Manual (RAG)",
            options=["Upload Company Manual", "Use existing manual", "Telemetry-only (no manual)"],
            horizontal=False,
            key="manual_source_mode",
        )
        selected_manual_path: Path | None = None
        uploaded_manual = None
        if manual_mode == "Upload Company Manual":
            uploaded_manual = st.file_uploader(
                "Upload Company Manual (.pdf, .md, .txt, .docx)",
                type=["pdf", "md", "txt", "docx"],
                key="manual_uploader",
            )
        elif manual_mode == "Use existing manual":
            if manual_labels:
                chosen_manual_label = st.selectbox(
                    "Select repository manual",
                    options=list(manual_labels.keys()),
                    index=None,
                    placeholder="Choose an existing manual...",
                    key="manual_existing_select",
                )
                if chosen_manual_label:
                    selected_manual_path = manual_labels[chosen_manual_label]
            else:
                st.warning("No manuals found in test_data_and_manuals/ or knowledge_base/.")

    selected_schema_label = st.selectbox(
        "Payload decoder schema",
        options=list(schema_options.keys()),
        key="bin_schema_override",
    )

    if st.button("Ingest & Prepare Workspace", type="primary", key="ingest_prepare_btn"):
        bin_input: Path | tuple[str, bytes] | None = None
        if bin_mode == "Use existing .bin file":
            bin_input = selected_bin_path
        elif uploaded_bin is not None:
            bin_input = (uploaded_bin.name, uploaded_bin.getvalue())

        if bin_input is None:
            st.error("Select or upload a `.bin` telemetry file before ingesting.")
        elif manual_mode == "Use existing manual" and selected_manual_path is None:
            st.error("Choose a repository manual, or switch to Upload / Telemetry-only.")
        else:
            manual_input: Path | tuple[str, bytes] | None = None
            if manual_mode == "Upload Company Manual" and uploaded_manual is not None:
                manual_input = (uploaded_manual.name, uploaded_manual.getvalue())
            elif manual_mode == "Use existing manual":
                manual_input = selected_manual_path

            prior_session: BinRagSession | None = st.session_state.get("bin_rag_session")
            if prior_session is not None:
                prior_session.close()
                st.session_state["bin_rag_session"] = None

            try:
                with st.spinner("Converting, ingesting into Java BlockStore, and indexing manual..."):
                    session = prepare_bin_and_manual_session(
                        bin_input,
                        manual_input,
                        root=ROOT,
                        schema_override=schema_options[selected_schema_label],
                    )
                atexit.register(session.close)
                st.session_state["bin_rag_session"] = session
                st.session_state["bin_investigations"] = []
                st.session_state["bin_window_start"] = session.default_start_local
                st.session_state["bin_window_end"] = session.default_end_local
                st.success(f"Workspace ready for `{session.source_bin_name}`.")
            except Exception as exc:
                st.error(f"Workspace preparation failed: {exc}")

    active_session: BinRagSession | None = st.session_state.get("bin_rag_session")
    if active_session is None:
        return

    st.markdown("---")
    m1, m2, m3 = st.columns(3)
    m1.metric("Ingested Events", str(active_session.total_events))
    m2.metric("Detected Sources", ", ".join(str(s) for s in active_session.source_ids) or "None")
    m3.metric(
        "Manual RAG Status",
        f"{active_session.manual_chunks} chunks" if active_session.manual_filename else "Telemetry-only",
    )
    event_desc = ", ".join(f"{et['hex']} {et['name']}" for et in active_session.event_types)
    st.caption(
        f"**File:** `{active_session.source_bin_name}` · "
        f"**Schema:** `{active_session.registry.schema_set or active_session.schema_path.name}` · "
        f"**Event types:** {event_desc}"
    )
    st.caption(
        f"**Observed Local Span:** `{format_epoch_ns_local(active_session.min_timestamp_ns)}` "
        f"through `{format_epoch_ns_local(active_session.max_timestamp_ns)}`"
        + (f" · **Manual:** `{active_session.manual_filename}`" if active_session.manual_filename else "")
    )

    wcol1, wcol2 = st.columns(2)
    start_val = wcol1.text_input(
        "Investigation start (local ISO-8601, <= 1h span)",
        value=st.session_state.get("bin_window_start", active_session.default_start_local),
        key="bin_window_start",
    )
    end_val = wcol2.text_input(
        "Investigation end (local ISO-8601)",
        value=st.session_state.get("bin_window_end", active_session.default_end_local),
        key="bin_window_end",
    )

    if st.button("Run Full RCA & FSS", type="primary", key="bin_run_full_rca_btn"):
        auto_query = (
            f"Perform a full root-cause analysis and failure synthesis between {start_val} and {end_val}. "
            "Reconstruct the chronological sequence of events across all retrieved sources and event types, "
            "cross-reference relevant operating limits, fault codes, or recovery procedures from the manual, "
            "compare affected agents against peers if present, and clearly separate observed facts from hypotheses."
        )
        _run_bin_rca_query(active_session, auto_query)

    with st.form("bin_custom_rca_form"):
        custom_q = st.text_input(
            "Ask a custom RCA / FSS question about this .bin & manual",
            placeholder="Why did fault/avoidance occur, and what manual procedure applies?",
            key="bin_custom_q_input",
        )
        submitted_custom = st.form_submit_button("Ask Custom RCA Question")
    if submitted_custom and custom_q.strip():
        scoped_q = custom_q.strip()
        if not re.search(r"\b\d{4}-\d{2}-\d{2}", scoped_q) and not re.search(r"\b(?:am|pm)\b", scoped_q, re.IGNORECASE):
            scoped_q = f"{scoped_q} (Analyze local window {start_val} to {end_val})"
        _run_bin_rca_query(active_session, scoped_q)

    bin_investigations: list[dict[str, Any]] = st.session_state.get("bin_investigations", [])
    for idx, item in enumerate(bin_investigations):
        _render_investigation(item, key_prefix=f"bin_{idx}")


def _render_investigation(item: dict[str, Any], *, key_prefix: str = "0") -> None:
    if "query" in item:
        with st.chat_message("user"):
            st.markdown(item["query"])
        with st.chat_message("assistant"):
            _render_result(item, key_prefix=key_prefix)
    elif "error" in item:
        with st.chat_message("assistant"):
            st.error(item["error"])


def _serialize_json(payload: Any) -> str:
    """Serialize evidence payloads to pretty-printed UTF-8 JSON."""
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _format_retrieval_history_export(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich Java retrieval requests with formatted local timestamps for export."""
    exported: list[dict[str, Any]] = []
    for request in history:
        entry = dict(request)
        if "start_time_ns" in request:
            entry["start_local"] = format_epoch_ns_local(request["start_time_ns"])
        if "end_time_ns" in request:
            entry["end_local"] = format_epoch_ns_local(request["end_time_ns"])
        exported.append(entry)
    return exported


def _render_result(result: dict[str, Any], *, key_prefix: str = "0") -> None:
    status = result.get("status")
    retrieval_history = result.get("retrieval_history", [])
    has_retrieved = isinstance(retrieval_history, list) and len(retrieval_history) > 0
    clarification_text = str(result.get("clarification") or "").strip()
    is_contradictory_tf = has_retrieved and any(
        kw in clarification_text.lower() for kw in ("timeframe", "iso-8601")
    )

    if status == "supported":
        st.success("The independent reviewer found the hypothesis adequately supported.")
    elif status == "needs_clarification" and not is_contradictory_tf:
        st.info("More detail is needed before retrieval can continue.")
    elif status == "citation_review_failed":
        st.error("The reviewer accepted a draft, but the deterministic citation audit did not.")
    else:
        st.warning("The available evidence does not establish a supported root cause.")

    if result.get("goal"):
        st.markdown(f"**Investigation goal:** {result['goal']}")
    if clarification_text and not is_contradictory_tf:
        st.markdown(f"**Please clarify:** {clarification_text}")
    references = result.get("references", {})
    hypothesis = result.get("root_cause_hypothesis")
    if isinstance(hypothesis, dict) and hypothesis.get("statement"):
        heading = (
            "Root-cause hypothesis"
            if status == "supported"
            else "Unconfirmed working hypothesis"
        )
        st.markdown(f"### {heading}")
        st.markdown(_claim_text(hypothesis, references=references))

    _render_claims("Observed facts", result.get("observed_facts", []), references=references)
    _render_claims("Inferences", result.get("inferences", []), references=references)
    uncertainties = _dedup_text_items(list(result.get("uncertainties") or []))
    data_gaps = _dedup_text_items(list(result.get("data_gaps") or []))
    excluded_keys = {_normalize_text_key(item) for item in uncertainties}
    excluded_keys.update(_normalize_text_key(item) for item in data_gaps)
    missing_info = _dedup_text_items(
        list(result.get("missing_information") or []), exclude_keys=excluded_keys
    )
    _render_text_list("Uncertainties", uncertainties)
    _render_text_list("Data gaps", data_gaps)
    if missing_info:
        _render_text_list("Evidence still needed", missing_info)

    if result.get("reviewer_rationale"):
        st.markdown(f"**Reviewer:** {result['reviewer_rationale']}")
    retrieval_history = result.get("retrieval_history", [])
    _render_retrieval_history(retrieval_history, key_prefix=key_prefix)

    if references:
        with st.expander("Evidence provenance"):
            st.download_button(
                label="Download Evidence provenance JSON",
                data=_serialize_json(references),
                file_name=f"evidence_provenance_{key_prefix}.json",
                mime="application/json",
                key=f"dl_provenance_{key_prefix}",
            )
            st.json(references)
    documents = result.get("knowledge_documents", [])
    if documents:
        with st.expander("Retrieved knowledge documents"):
            st.dataframe(documents, use_container_width=True, hide_index=True)
    audit = result.get("audit")
    if audit:
        with st.expander("Citation audit"):
            st.json(audit)
    if retrieval_history or references:
        combined_payload = {
            "query": result.get("query"),
            "status": result.get("status"),
            "goal": result.get("goal"),
            "model": result.get("model"),
            "java_retrievals": (
                _format_retrieval_history_export(retrieval_history)
                if isinstance(retrieval_history, list)
                else retrieval_history
            ),
            "evidence_provenance": references,
            "observed_facts": result.get("observed_facts", []),
            "inferences": result.get("inferences", []),
            "root_cause_hypothesis": result.get("root_cause_hypothesis"),
            "uncertainties": result.get("uncertainties", []),
            "data_gaps": result.get("data_gaps", []),
            "missing_information": result.get("missing_information", []),
            "reviewer_rationale": result.get("reviewer_rationale"),
            "audit": result.get("audit"),
        }
        st.download_button(
            label="Download combined evidence JSON (Java retrievals + provenance)",
            data=_serialize_json(combined_payload),
            file_name=f"fss_evidence_{key_prefix}.json",
            mime="application/json",
            key=f"dl_combined_{key_prefix}",
        )
    if result.get("model"):
        st.caption(f"Local model: {result['model']}")


def _render_claims(
    title: str, claims: Any, *, references: dict[str, Any] | None = None
) -> None:
    if not isinstance(claims, list) or not claims:
        return
    st.markdown(f"### {title}")
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        confidence = claim.get("confidence")
        suffix = f" — confidence: {confidence}" if confidence else ""
        st.markdown(f"- {_claim_text(claim, references=references)}{suffix}")


def _short_citation_time(raw_iso: str) -> str:
    """Format 'YYYY-MM-DD[T ]HH:MM:SS.nnnnnnnnn[ TZ]' cleanly as 'HH:MM:SS.mmm'."""
    match = re.search(r"(\d{2}:\d{2}:\d{2})(?:\.(\d+))?", raw_iso.strip())
    if not match:
        return raw_iso.strip()
    hms = match.group(1)
    frac = match.group(2)
    if frac:
        return f"{hms}.{frac[:3]}"
    return hms


def _format_document_citation_label(
    citation_id: str, references: dict[str, Any] | None
) -> str:
    """Format a manual/document citation ID cleanly as [Manual · filename · p.X/section]."""
    if isinstance(references, dict):
        doc = references.get(citation_id)
        if not isinstance(doc, dict) and citation_id.startswith("doc-"):
            doc = references.get(citation_id[4:])
        if isinstance(doc, dict) and doc.get("kind") == "document":
            fname = Path(str(doc.get("source_filename") or "Manual")).name
            page = doc.get("page_number")
            section = str(doc.get("section") or "").strip()
            if page is not None:
                return f"Manual · {fname} · p.{page}"
            if section and section.lower() != "document":
                return f"Manual · {fname} · {section}"
            return f"Manual · {fname}"
    return citation_id


def _claim_text(
    claim: dict[str, Any], *, references: dict[str, Any] | None = None
) -> str:
    statement = str(claim.get("statement") or "")
    citations = claim.get("citations", [])
    rendered_citations = []
    for citation in citations:
        if isinstance(citation, dict) and {"source_id", "event_type"} <= set(citation):
            if "timestamp_iso8601" in citation:
                short_ts = _short_citation_time(str(citation["timestamp_iso8601"]))
            elif "timestamp_ns" in citation:
                short_ts = _short_citation_time(format_epoch_ns_local(int(citation["timestamp_ns"])))
            else:
                short_ts = "?"
            rendered_citations.append(
                f"D{citation['source_id']} · {short_ts} · {citation['event_type']}"
            )
        else:
            cit_str = str(citation)
            label = _format_document_citation_label(cit_str, references)
            if label != cit_str and f"[{cit_str}]" in statement:
                statement = statement.replace(f"[{cit_str}]", f"`[{label}]`")
            rendered_citations.append(label)
    unique_citations = list(dict.fromkeys(rendered_citations))
    missing = [label for label in unique_citations if f"[{label}]" not in statement]
    if missing:
        rendered = " ".join(f"`[{label}]`" for label in missing)
        return f"{statement} {rendered}"
    return statement


def _render_text_list(title: str, values: Any) -> None:
    if not isinstance(values, list) or not values:
        return
    st.markdown(f"**{title}:**")
    for value in values:
        st.markdown(f"- {value}")


def _render_retrieval_history(history: Any, *, key_prefix: str = "0") -> None:
    if not isinstance(history, list) or not history:
        return
    with st.expander(f"Java retrievals ({len(history)}/3)"):
        st.download_button(
            label="Download Java retrievals JSON",
            data=_serialize_json(_format_retrieval_history_export(history)),
            file_name=f"java_retrievals_{key_prefix}.json",
            mime="application/json",
            key=f"dl_retrievals_{key_prefix}",
        )
        for request in history:
            start = format_epoch_ns_local(request["start_time_ns"])
            end = format_epoch_ns_local(request["end_time_ns"])
            event_filter = request.get("event_type")
            returned_count = len(request["returned_event_ids"])
            total_matches = request.get("total_matches")
            if total_matches is None:
                count_summary = f"returned {returned_count}; exact total unavailable"
            else:
                count_summary = f"returned {returned_count} of {total_matches} matches"
            truncation = request.get("truncated")
            if truncation is True:
                truncation_summary = " (truncated)"
            elif truncation is None:
                truncation_summary = " (truncation status unavailable)"
            else:
                truncation_summary = ""
            st.markdown(
                f"- **Request {request['request_id']}** — source `{request['source']}`, "
                f"inclusive local window `{start}` through `{end}`, "
                f"event type `{event_filter if event_filter is not None else 'all'}`; "
                f"{count_summary}{truncation_summary}."
            )


if __name__ == "__main__":
    main()
