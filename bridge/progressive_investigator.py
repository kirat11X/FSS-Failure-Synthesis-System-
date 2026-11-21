"""Progressive Multi-Step Investigation Runner (REQ-033, PI-01..PI-05).

Provides:
  - Bounded progressive investigation allowing AT MOST max_context_requests = 1 follow-up retrieval query.
  - Strict Contract v0 bounds enforcement:
      * t_end_ns > t_start_ns
      * window (t_end_ns - t_start_ns) <= MAX_WINDOW_NS (3,600,000,000,000 ns = 1 hour)
      * 1 <= max_events <= 10_000
      * If request violates bounds, REJECT with explicit error message (never silently clamp).
  - Validation that before_event anchor exists in prior retrieved evidence.
  - Deduplication of follow-up events by event_id when merging with initial evidence.
  - Re-routing of merged events through standard decoder, evidence builder, and hybrid RAG.
  - ProgressiveInvestigator class and investigate_progressive(...) function.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from fss_py.contract import MAX_EVENTS_LIMIT, MAX_WINDOW_NS
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, DecodedEvent, EventRecord, RetrievalRequest, RetrievalResult
from fss_py.schema import load_schema_file

from bridge.clock_skew_compensator import analyze_clock_skew
from bridge.hybrid_rag import retrieve_historical_exemplars, retrieve_hybrid_knowledge
from bridge.synthesizer import (
    DEFAULT_NUM_CTX,
    DEFAULT_THINKING_BUDGET,
    synthesize_and_validate,
)


@dataclass
class ContextRequest:
    """Follow-up context retrieval request from LLM or progressive investigator."""
    before_event: str | None = None
    lookback_ns: int | None = None
    t_start_ns: int | None = None
    t_end_ns: int | None = None
    source: int | str = "ALL"
    max_events: int = 100
    event_type: int | None = None
    reason: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextRequest:
        # Check if wrapped in "context_request"
        cr_data = data.get("context_request", data)
        if not isinstance(cr_data, dict):
            cr_data = {}

        return cls(
            before_event=cr_data.get("before_event"),
            lookback_ns=int(cr_data["lookback_ns"]) if cr_data.get("lookback_ns") is not None else None,
            t_start_ns=int(cr_data["t_start_ns"]) if cr_data.get("t_start_ns") is not None else None,
            t_end_ns=int(cr_data["t_end_ns"]) if cr_data.get("t_end_ns") is not None else None,
            source=cr_data.get("source", "ALL"),
            max_events=int(cr_data.get("max_events", 100)),
            event_type=int(cr_data["event_type"]) if cr_data.get("event_type") is not None else None,
            reason=str(cr_data.get("reason", "")),
        )


@dataclass
class ContextRequestValidationResult:
    """Outcome of validating a context request against Contract v0 bounds and evidence anchors."""
    valid: bool
    error_message: str | None = None
    retrieval_request: RetrievalRequest | None = None


def validate_context_request(
    request: ContextRequest | dict[str, Any],
    current_evidence: dict[str, Any] | Sequence[DecodedEvent],
    *,
    max_window_ns: int = MAX_WINDOW_NS,
    max_events_limit: int = MAX_EVENTS_LIMIT,
) -> ContextRequestValidationResult:
    """Strictly validate a follow-up context request against Contract v0 and evidence anchors.

    Enforces:
      - If before_event is specified:
          * anchor event_id must exist in current evidence (PI-02)
          * lookback_ns must be positive int
          * t_start_ns = anchor_ts - lookback_ns, t_end_ns = anchor_ts (PI-01)
      - If direct window is specified:
          * both t_start_ns and t_end_ns must be provided (no unbounded/all-history) (PI-02)
          * t_end_ns > t_start_ns
      - Window duration <= max_window_ns (1 hour). Violations are REJECTED, NEVER clamped (PI-03).
      - 1 <= max_events <= max_events_limit (10_000).
    """
    if isinstance(request, dict):
        req = ContextRequest.from_dict(request)
    else:
        req = request

    # Resolve evidence events
    if isinstance(current_evidence, dict):
        events_list = current_evidence.get("events", [])
    else:
        events_list = list(current_evidence)

    # 1. Anchor validation
    if req.before_event:
        anchor_event = None
        for ev in events_list:
            eid = ev.event_id if isinstance(ev, DecodedEvent) else ev.get("event_id")
            if eid == req.before_event:
                anchor_event = ev
                break

        if anchor_event is None:
            return ContextRequestValidationResult(
                valid=False,
                error_message=(
                    f"Request anchored outside evidence: event_id '{req.before_event}' "
                    f"not found in currently retrieved evidence."
                ),
            )

        anchor_ts = (
            anchor_event.timestamp_ns
            if isinstance(anchor_event, DecodedEvent)
            else (anchor_event.get("timestamp_ns") or anchor_event.get("timestamp") or 0)
        )

        if req.lookback_ns is None or req.lookback_ns <= 0:
            return ContextRequestValidationResult(
                valid=False,
                error_message=f"lookback_ns must be a strictly positive integer, got {req.lookback_ns!r}.",
            )

        t_end_ns = anchor_ts
        t_start_ns = anchor_ts - req.lookback_ns
        if t_start_ns < 0:
            t_start_ns = 0

    else:
        # Direct interval specification
        if req.t_start_ns is None or req.t_end_ns is None:
            return ContextRequestValidationResult(
                valid=False,
                error_message=(
                    "Unbounded or all-history retrieval requested: both t_start_ns and "
                    "t_end_ns must be explicitly specified. Open-ended queries are rejected."
                ),
            )

        t_start_ns = req.t_start_ns
        t_end_ns = req.t_end_ns

    # 2. Bound ordering validation: t_end_ns > t_start_ns
    if t_end_ns <= t_start_ns:
        return ContextRequestValidationResult(
            valid=False,
            error_message=(
                f"Invalid time bounds: t_end_ns ({t_end_ns}) must be strictly greater than "
                f"t_start_ns ({t_start_ns}). Non-positive duration windows are rejected."
            ),
        )

    # 3. Maximum window limit (1 hour): REJECTED, NEVER CLAMPED (PI-03)
    duration_ns = t_end_ns - t_start_ns
    if duration_ns > max_window_ns:
        return ContextRequestValidationResult(
            valid=False,
            error_message=(
                f"Oversized window: requested window duration {duration_ns} ns "
                f"({duration_ns / 1_000_000_000:.2f} s) exceeds maximum allowed window "
                f"MAX_WINDOW_NS ({max_window_ns} ns = 1 hour). Bounded retrieval policy requires "
                f"windows <= 1 hour (rejected, never clamped)."
            ),
        )

    # 4. Max events limit
    if not (1 <= req.max_events <= max_events_limit):
        return ContextRequestValidationResult(
            valid=False,
            error_message=(
                f"Invalid max_events {req.max_events}: must be between 1 and {max_events_limit}."
            ),
        )

    # 5. Source specification
    src_val = ALL if str(req.source).upper() == "ALL" else int(req.source)

    retrieval_req = RetrievalRequest(
        source=src_val,
        t_start_ns=t_start_ns,
        t_end_ns=t_end_ns,
        max_events=req.max_events,
        event_type=req.event_type,
    )

    return ContextRequestValidationResult(
        valid=True,
        error_message=None,
        retrieval_request=retrieval_req,
    )


@dataclass
class ProgressiveInvestigationResult:
    """Comprehensive result of progressive multi-step investigation."""
    query: str
    rounds_executed: int
    context_requests: list[dict[str, Any]]
    rejections: list[str]
    evidence: dict[str, Any]
    knowledge: list[dict[str, Any]]
    exemplars: list[Any]
    synthesis: dict[str, Any]
    validation_report: Any
    model_name: str
    raw_events_count: int


class ProgressiveInvestigator:
    """Progressive Multi-Step Investigator enforcing PI-01..PI-05 policies."""

    def __init__(
        self,
        retriever: Any,
        schema_registry: Any,
        *,
        max_context_requests: int = 1,
        synthesizer_fn: Callable[..., Any] | None = None,
        vendor_manual_matches: list[dict[str, Any]] | None = None,
    ) -> None:
        self.retriever = retriever
        self.registry = schema_registry
        self.max_context_requests = max_context_requests
        self.synthesizer_fn = synthesizer_fn or synthesize_and_validate
        self.vendor_manual_matches = vendor_manual_matches or []

    def _execute_retrieval(self, req: RetrievalRequest) -> RetrievalResult:
        """Call retriever across various interfaces (method, callable, mock)."""
        if hasattr(self.retriever, "retrieve"):
            return self.retriever.retrieve(
                source=req.source,
                t_start_ns=req.t_start_ns,
                t_end_ns=req.t_end_ns,
                max_events=req.max_events,
                event_type=req.event_type,
            )
        elif callable(self.retriever):
            return self.retriever(req)
        else:
            raise TypeError(f"retriever object {self.retriever!r} has no retrieve method and is not callable")

    def investigate(
        self,
        query: str,
        initial_request: RetrievalRequest,
        *,
        initial_events: Sequence[EventRecord] | None = None,
        exemplars_k: int = 3,
        enable_thinking: bool = False,
        thinking_budget: int = DEFAULT_THINKING_BUDGET,
        num_ctx: int = DEFAULT_NUM_CTX,
    ) -> ProgressiveInvestigationResult:
        """Execute bounded progressive investigation loop.

        Flow:
          1. Round 0: Execute initial retrieval (or take initial_events).
          2. Decode events via registry and build evidence JSON.
          3. Analyze clock skew and populate advisory metadata.
          4. Retrieve hybrid knowledge and historical incident exemplars.
          5. Call synthesizer.
          6. If synthesizer emits a context_request and rounds < max_context_requests:
             - Validate context request against bounds and anchors.
             - If invalid: record rejection and re-synthesize with refusal notice.
             - If valid: retrieve follow-up window, deduplicate events by event_id,
               re-decode, re-build evidence, and synthesize final answer.
          7. Enforce round budget (at most max_context_requests follow-ups).
        """
        rounds_executed = 0
        context_requests: list[dict[str, Any]] = []
        rejections: list[str] = []

        # --- Round 0: Initial retrieval & evidence ---
        if initial_events is not None:
            raw_records = list(initial_events)
            initial_res = RetrievalResult(request=initial_request, events=raw_records, truncated=False)
        else:
            initial_res = self._execute_retrieval(initial_request)
            raw_records = list(initial_res.events)

        decoded_events = decode_all(raw_records, self.registry)
        evidence = build_evidence(decoded_events, query=query, retrieval=initial_res)

        # Clock skew analysis
        skew_report = analyze_clock_skew(decoded_events)
        if skew_report.skew_detected:
            evidence["clock_skew"] = skew_report.to_dict()
            if skew_report.clock_caveat:
                evidence["clock_caveat"] = skew_report.clock_caveat

        if self.synthesizer_fn is synthesize_and_validate:
            knowledge = retrieve_hybrid_knowledge(query, evidence=evidence)
            exemplars = retrieve_historical_exemplars(query, evidence=evidence, k=exemplars_k)
        else:
            knowledge = []
            exemplars = []

        # Synthesize Round 0
        synthesis, val_report, model_name = self.synthesizer_fn(
            evidence,
            query,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            effective_num_ctx=num_ctx,
            exemplars_k=exemplars_k,
            vendor_manual_matches=self.vendor_manual_matches,
        )

        # Check for context_request in synthesis output
        ctx_req_data = None
        if isinstance(synthesis, dict) and "context_request" in synthesis and synthesis["context_request"]:
            ctx_req_data = synthesis["context_request"]

        # Loop if context request emitted
        while ctx_req_data and rounds_executed < self.max_context_requests:
            rounds_executed += 1

            # Validate the context request
            val_res = validate_context_request(ctx_req_data, evidence)
            if not val_res.valid:
                # PI-02 / PI-03: Rejected, never clamped. Synthesis continues with existing evidence.
                rejection_msg = val_res.error_message or "Context request rejected due to policy violation."
                rejections.append(rejection_msg)
                evidence["data_gaps"].append({
                    "kind": "CONTEXT_REQUEST_REJECTED",
                    "event_id": None,
                    "source_id": None,
                    "detail": rejection_msg,
                })
                # Re-synthesize with rejection notice, forbidding further context requests
                synthesis, val_report, model_name = self.synthesizer_fn(
                    evidence,
                    query,
                    enable_thinking=enable_thinking,
                    thinking_budget=thinking_budget,
                    effective_num_ctx=num_ctx,
                    exemplars_k=exemplars_k,
                    vendor_manual_matches=self.vendor_manual_matches,
                )
                break

            # Valid follow-up request (PI-01): Execute bounded retrieve
            context_requests.append(ctx_req_data if isinstance(ctx_req_data, dict) else asdict(ctx_req_data))
            follow_up_req = val_res.retrieval_request
            follow_up_res = self._execute_retrieval(follow_up_req)

            # PI-05: Extra events go through the same decoder and evidence path
            new_raw_records = list(follow_up_res.events)

            # Merge raw records without duplicates
            # Two raw records are duplicates if they share (source_id, timestamp_ns, event_type, payload_bytes)
            seen_record_keys = {
                (r.source_id, r.timestamp_ns, r.event_type, r.payload_bytes) for r in raw_records
            }
            added_records = []
            for r in new_raw_records:
                k = (r.source_id, r.timestamp_ns, r.event_type, r.payload_bytes)
                if k not in seen_record_keys:
                    seen_record_keys.add(k)
                    added_records.append(r)
            raw_records.extend(added_records)

            # Re-decode full merged set
            decoded_events = decode_all(raw_records, self.registry)

            # Deduplicate decoded events by event_id
            seen_eids = set()
            deduped_decoded: list[DecodedEvent] = []
            for dev in decoded_events:
                if dev.event_id not in seen_eids:
                    seen_eids.add(dev.event_id)
                    deduped_decoded.append(dev)
            decoded_events = deduped_decoded

            # Build updated merged evidence
            merged_res = RetrievalResult(
                request=follow_up_req,
                events=raw_records,
                truncated=initial_res.truncated or follow_up_res.truncated,
            )
            evidence = build_evidence(decoded_events, query=query, retrieval=merged_res)

            # Clock skew analysis on merged evidence
            skew_report = analyze_clock_skew(decoded_events)
            if skew_report.skew_detected:
                evidence["clock_skew"] = skew_report.to_dict()
                if skew_report.clock_caveat:
                    evidence["clock_caveat"] = skew_report.clock_caveat

            # Re-retrieve knowledge and exemplars with updated evidence
            if self.synthesizer_fn is synthesize_and_validate:
                knowledge = retrieve_hybrid_knowledge(query, evidence=evidence)
                exemplars = retrieve_historical_exemplars(query, evidence=evidence, k=exemplars_k)

            # Final synthesis round (forcing final answer)
            synthesis, val_report, model_name = self.synthesizer_fn(
                evidence,
                query,
                enable_thinking=enable_thinking,
                thinking_budget=thinking_budget,
                effective_num_ctx=num_ctx,
                exemplars_k=exemplars_k,
                vendor_manual_matches=self.vendor_manual_matches,
            )

            # Clear ctx_req_data to terminate loop after max_context_requests (PI-04)
            ctx_req_data = None

        return ProgressiveInvestigationResult(
            query=query,
            rounds_executed=rounds_executed,
            context_requests=context_requests,
            rejections=rejections,
            evidence=evidence,
            knowledge=knowledge,
            exemplars=exemplars,
            synthesis=synthesis,
            validation_report=val_report,
            model_name=model_name,
            raw_events_count=len(raw_records),
        )


def investigate_progressive(
    query: str,
    initial_request: RetrievalRequest,
    retriever: Any,
    schema_registry: Any,
    *,
    max_context_requests: int = 1,
    initial_events: Sequence[EventRecord] | None = None,
    synthesizer_fn: Callable[..., Any] | None = None,
    vendor_manual_matches: list[dict[str, Any]] | None = None,
) -> ProgressiveInvestigationResult:
    """Convenience functional interface for progressive investigation."""
    investigator = ProgressiveInvestigator(
        retriever=retriever,
        schema_registry=schema_registry,
        max_context_requests=max_context_requests,
        synthesizer_fn=synthesizer_fn,
        vendor_manual_matches=vendor_manual_matches,
    )
    return investigator.investigate(
        query=query,
        initial_request=initial_request,
        initial_events=initial_events,
    )
