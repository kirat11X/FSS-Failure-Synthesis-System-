"""Streamlit interface for query-driven, evidence-reviewed investigations."""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Any

import streamlit as st

from bridge.investigation import (
    InvestigationError,
    LocalModelBackend,
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

    investigations: list[dict[str, Any]] = st.session_state.setdefault("investigations", [])
    for item in investigations:
        _render_investigation(item)

    submitted_query = st.chat_input(
        "What happened? Include the event timeframe in local time."
    )
    if not submitted_query:
        return

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
            _render_result(result)
        except (InvestigationError, RetrievalIpcError, OSError, TimeoutError, ValueError) as exc:
            st.error(f"Investigation stopped: {exc}")


def _render_investigation(item: dict[str, Any]) -> None:
    if "query" in item:
        with st.chat_message("user"):
            st.markdown(item["query"])
        with st.chat_message("assistant"):
            _render_result(item)
    elif "error" in item:
        with st.chat_message("assistant"):
            st.error(item["error"])


def _render_result(result: dict[str, Any]) -> None:
    status = result.get("status")
    if status == "supported":
        st.success("The independent reviewer found the hypothesis adequately supported.")
    elif status == "needs_clarification":
        st.info("More detail is needed before retrieval can continue.")
    elif status == "citation_review_failed":
        st.error("The reviewer accepted a draft, but the deterministic citation audit did not.")
    else:
        st.warning("The available evidence does not establish a supported root cause.")

    if result.get("goal"):
        st.markdown(f"**Investigation goal:** {result['goal']}")
    if result.get("clarification"):
        st.markdown(f"**Please clarify:** {result['clarification']}")
    hypothesis = result.get("root_cause_hypothesis")
    if isinstance(hypothesis, dict) and hypothesis.get("statement"):
        heading = (
            "Root-cause hypothesis"
            if status == "supported"
            else "Unconfirmed working hypothesis"
        )
        st.markdown(f"### {heading}")
        st.markdown(_claim_text(hypothesis))

    _render_claims("Observed facts", result.get("observed_facts", []))
    _render_claims("Inferences", result.get("inferences", []))
    _render_text_list("Uncertainties", result.get("uncertainties", []))
    _render_text_list("Data gaps", result.get("data_gaps", []))
    _render_text_list("Evidence still needed", result.get("missing_information", []))

    if result.get("reviewer_rationale"):
        st.markdown(f"**Reviewer:** {result['reviewer_rationale']}")
    _render_retrieval_history(result.get("retrieval_history", []))

    references = result.get("references", {})
    if references:
        with st.expander("Evidence provenance"):
            st.json(references)
    documents = result.get("knowledge_documents", [])
    if documents:
        with st.expander("Retrieved knowledge documents"):
            st.dataframe(documents, use_container_width=True, hide_index=True)
    audit = result.get("audit")
    if audit:
        with st.expander("Citation audit"):
            st.json(audit)
    if result.get("model"):
        st.caption(f"Local model: {result['model']}")


def _render_claims(title: str, claims: Any) -> None:
    if not isinstance(claims, list) or not claims:
        return
    st.markdown(f"### {title}")
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        confidence = claim.get("confidence")
        suffix = f" — confidence: {confidence}" if confidence else ""
        st.markdown(f"- {_claim_text(claim)}{suffix}")


def _claim_text(claim: dict[str, Any]) -> str:
    statement = str(claim.get("statement") or "")
    citations = claim.get("citations", [])
    rendered_citations = []
    for citation in citations:
        if isinstance(citation, dict) and {
            "source_id",
            "timestamp_ns",
            "event_type",
        } <= set(citation):
            rendered_citations.append(
                f"source_id={citation['source_id']}, "
                f"timestamp_ns={citation['timestamp_ns']}, "
                f"event_type={citation['event_type']}"
            )
        else:
            rendered_citations.append(str(citation))
    rendered = " ".join(f"[{citation}]" for citation in rendered_citations)
    if rendered and not all(
        f"[{citation}]" in statement for citation in rendered_citations
    ):
        return f"{statement} {rendered}"
    return statement


def _render_text_list(title: str, values: Any) -> None:
    if not isinstance(values, list) or not values:
        return
    st.markdown(f"**{title}:**")
    for value in values:
        st.markdown(f"- {value}")


def _render_retrieval_history(history: Any) -> None:
    if not isinstance(history, list) or not history:
        return
    with st.expander(f"Java retrievals ({len(history)}/3)"):
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
