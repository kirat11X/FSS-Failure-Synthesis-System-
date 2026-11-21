#!/usr/bin/env python3
"""FSS Pipeline Runner: End-to-end decoding, evidence building, and synthesis.

Accepts wire envelopes from standard input (passed by JavaStoreBridge) or from a file,
decodes all events via fss_py, builds evidence JSON, invokes the LLM/reasoning synthesizer,
and outputs the complete 8-part synthesis report.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, RetrievalRequest, RetrievalResult
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

from bridge.clock_skew_compensator import analyze_clock_skew
from bridge.hybrid_rag import (
    HistoricalIncident,
    format_exemplars_cot,
    retrieve_historical_exemplars,
    retrieve_hybrid_knowledge,
)
from bridge.progressive_investigator import ProgressiveInvestigator, investigate_progressive
from bridge.report_exporter import export_html, export_jsonld, export_pdf
from bridge.synthesizer import (
    DEFAULT_NUM_CTX,
    DEFAULT_THINKING_BUDGET,
    build_llm_context,
    retrieve_relevant_knowledge,
    synthesize_and_validate,
)


def fmt_ns(ns: int) -> str:
    """Format nanoseconds as UTC datetime string with millisecond precision."""
    dt = datetime.fromtimestamp(ns // 10**9, tz=timezone.utc)
    return f"{dt:%Y-%m-%d %H:%M:%S}.{ns % 10**9 // 10**6:03d}"


def epistemic_tag(ev: dict[str, Any]) -> str:
    """Compute the epistemic tag (decoded, documented, unknown bytes)."""
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


def render_report(
    query: str,
    retrieval_res: RetrievalResult,
    evidence: dict[str, Any],
    knowledge: list[dict[str, Any]],
    synthesis: dict[str, Any],
    val_report: Any,
    model_name: str,
    exemplars: Sequence[HistoricalIncident] | None = None,
    vendor_matches: list[dict[str, Any]] | None = None,
) -> str:
    """Render the official 8-part FSS Failure Synthesis Report."""
    ret_req = retrieval_res.request
    events = evidence.get("events", [])
    sources = evidence.get("sources", [])
    gaps = evidence.get("data_gaps", [])

    lines = []
    line_div = "=" * 80
    lines.append(line_div)
    lines.append("  FAILURE SYNTHESIS SYSTEM (FSS) END-TO-END REPORT")
    lines.append(f"  Model: {model_name}")
    lines.append(line_div)
    lines.append("")

    # [1] Question
    lines.append("[1] Question")
    lines.append(f"> {query}")
    lines.append("")

    # [2] Retrieval
    lines.append("[2] Retrieval")
    src_label = "ALL" if ret_req.source is ALL else str(ret_req.source)
    lines.append(
        f"- Call: retrieve(source={src_label}, [{fmt_ns(ret_req.t_start_ns)}, {fmt_ns(ret_req.t_end_ns)}) UTC, "
        f"max_events={ret_req.max_events})"
    )
    lines.append(
        f"- Result: returned {len(retrieval_res.events)} events, truncated: {'YES (narrow query)' if retrieval_res.truncated else 'no'}"
    )
    lines.append("")

    # [3] Events
    lines.append("[3] Events")
    lines.append(f"Sources involved: {', '.join(map(str, sources)) if sources else 'none'}")
    lines.append(evidence.get("ordering", "Grouped by source, producer time within each source."))
    if evidence.get("clock_caveat"):
        lines.append(f"⏱️ {evidence['clock_caveat']}")
    lines.append("")

    if events:
        col_fmt = "{:<10} | {:<23} | {:<16} | {:<18} | {:<26} | {:<15} | {}"
        lines.append(
            col_fmt.format(
                "Source",
                "Producer Time (UTC)",
                "Event Type",
                "Decode Status",
                "Epistemic Tag",
                "Event ID",
                "Fields",
            )
        )
        lines.append("-" * 125)

        for ev in events:
            f_str = ", ".join(f"{k}={v}" for k, v in ev.get("fields", {}).items())
            ts = ev.get("timestamp_ns") or ev.get("timestamp") or 0
            lines.append(
                col_fmt.format(
                    str(ev["source_id"]),
                    fmt_ns(ts),
                    ev.get("event_name") or ev.get("event_type"),
                    str(ev.get("decode_status")),
                    epistemic_tag(ev),
                    ev["event_id"],
                    f_str,
                )
            )
    else:
        lines.append("(No events retrieved in this time window)")
    lines.append("")

    # [4] Data gaps
    lines.append("[4] Data gaps")
    if gaps:
        for g in gaps:
            lines.append(f"- [{g.get('kind', 'GAP')}] {g.get('detail')}")
    else:
        lines.append("- None: all events fully decoded without gaps.")
    lines.append("")

    # [5] Knowledge
    lines.append("[5] Knowledge")
    lines.append(f"Retrieved {len(knowledge)} relevant reference chunks (BM25S + Dense + RRF + Reranker):")
    for k in knowledge:
        lines.append(f"- `{k['chunk']}` ({k['kind']}, {k['domain']}; explains {k.get('explains', 'N/A')}):")
        for kline in k["content"].splitlines():
            lines.append(f"    {kline}")
    if vendor_matches:
        lines.append("")
        lines.append(f"Digested Vendor Error-Code Manual Matches ({len(vendor_matches)} matches):")
        for vm in vendor_matches:
            vm_id = vm.get("id") or vm.get("err_id") or vm.get("code") or "err-match"
            if not str(vm_id).startswith(("err-", "doc-")):
                vm_id = f"err-{vm_id}"
            lines.append(f"- `{vm_id}`: {vm.get('title', 'Error')} (code={vm.get('code', 'N/A')}, subsys={vm.get('subsystem', 'N/A')}):")
            if vm.get("description"):
                lines.append(f"    Description: {vm['description']}")
            if vm.get("remedy") or vm.get("runbook"):
                lines.append(f"    Runbook: {vm.get('remedy') or vm.get('runbook')}")
    if exemplars:
        lines.append("")
        lines.append(f"Retrieved {len(exemplars)} Historical Incident Exemplars (RCACopilot Few-Shot CoT):")
        for ex in exemplars:
            lines.append(f"- `incident/{ex.incident_id}`: {ex.title}")
            lines.append(f"    Symptoms: {ex.symptoms}")
            lines.append(f"    Chain-of-Thought: {ex.condensed_cot}")
            lines.append(f"    Root Cause: {ex.root_cause}")
    lines.append("")

    # [6] LLM Context / Evidence JSON
    lines.append("[6] LLM context")
    llm_context = build_llm_context(
        evidence,
        query,
        knowledge,
        exemplars=exemplars,
        vendor_manual_matches=vendor_matches,
    )
    lines.append("```text")
    lines.append(llm_context)
    lines.append("```")
    lines.append("")

    # [7] Synthesis
    lines.append("[7] Synthesis")
    lines.append(f"**Summary:** {synthesis.get('summary')}")
    lines.append("")
    if synthesis.get("likely_cause"):
        lines.append(f"**Likely cause:** {synthesis.get('likely_cause')}")
        lines.append("")
    lines.append(f"**Confidence:** {synthesis.get('confidence')}")
    lines.append("")

    if synthesis.get("reasoning_truncated"):
        lines.append("⚠️ **Reasoning Status:** Truncated by Qwen3 Think-Budget Guard (> 1024 tokens)")
        lines.append("")

    lines.append("**Contributing events** (verified against evidence):")
    valid_set = set(val_report.valid_citations)
    contributing = synthesis.get("contributing_events", [])
    if contributing:
        for ce in contributing:
            eid = ce.get("event_id", "")
            mark = "✓" if eid in valid_set else "✗ NOT IN EVIDENCE"
            ts = ce.get("timestamp") or ce.get("timestamp_ns")
            ts_str = f" at {fmt_ns(ts)}" if ts else ""
            lines.append(f"- {mark} {eid}: Source {ce.get('source_id')} {ce.get('event_type')}{ts_str}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("**Uncertainty:**")
    uncertainties = synthesis.get("uncertainty", [])
    if uncertainties:
        for u in uncertainties:
            lines.append(f"- {u}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("**Reported Data Gaps:**")
    data_gaps = synthesis.get("data_gaps", [])
    if data_gaps:
        for dg in data_gaps:
            lines.append(f"- {dg}")
    else:
        lines.append("- None")
    lines.append("")

    # [8] Validator
    lines.append("[8] Validator")
    lines.append(f"**Verdict:** {val_report.verdict}")
    badge = getattr(val_report, "badge", None)
    if badge:
        lines.append(f"**Validation Badge:** {badge}")
    if val_report.verdict == "ACCEPTED":
        lines.append("✓ All cited event IDs verified against retrieved evidence.")
        lines.append("✓ Multi-source clock caveats present and verified.")
        lines.append("✓ Data gaps accurately covered.")
    elif val_report.verdict == "ACCEPTED_WITH_CORRECTIONS":
        lines.append("⚠️ Synthesis accepted with automatic corrections:")
        for note in val_report.notes:
            lines.append(f"  - {note}")
    else:
        badge_str = f" ({badge})" if badge else ""
        lines.append(f"❌ Synthesis REJECTED{badge_str}:")
        for note in val_report.notes:
            lines.append(f"  - {note}")
        if val_report.invalid_citations:
            lines.append(f"  - Hallucinated event IDs: {val_report.invalid_citations}")
        entity_mismatches = getattr(val_report, "entity_bindings", [])
        if entity_mismatches:
            lines.append(f"  - Entity Mismatches (Deceptive Grounding): {len(entity_mismatches)} detected")
        force_axes = getattr(val_report, "force_axes", {})
        overclaims = [oc for ocs in force_axes.values() for oc in ocs]
        if overclaims:
            lines.append(f"  - ForceBench Overclaims: {len(overclaims)} detected across 5 axes")
    lines.append("")
    lines.append(line_div)

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FSS Pipeline Runner (Python side)")
    parser.add_argument("--query", default="Why did drone 474 leave formation?", help="Operator query")
    parser.add_argument("--schema", default="schemas/drone_v0.yaml", help="Path to schema YAML")
    parser.add_argument("--file", help="Path to envelope .bin file (defaults to reading sys.stdin)")
    parser.add_argument("--start", type=int, default=0, help="Retrieval t_start_ns")
    parser.add_argument("--end", type=int, default=1, help="Retrieval t_end_ns")
    parser.add_argument("--source", default="ALL", help="Retrieval source (ALL or int)")
    parser.add_argument("--max-events", type=int, default=100, help="Retrieval max_events")
    parser.add_argument("--truncated", type=lambda s: s.lower() == "true", default=False, help="Whether retrieval was truncated")
    parser.add_argument("--event-type", type=int, default=None, help="Retrieval event_type filter")
    parser.add_argument("--enable-thinking", action="store_true", default=False, help="Enable Qwen3 thinking mode with sampling")
    parser.add_argument("--thinking-budget", type=int, default=DEFAULT_THINKING_BUDGET, help="Token budget for thinking phase")
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX, help="Context window length (num_ctx >= 16384)")
    parser.add_argument("--exemplars-k", type=int, default=3, help="Number of historical incident exemplars to retrieve (3-15)")
    parser.add_argument("--vendor-manual", default=None, help="Path to vendor error manual JSON file or inline JSON string")
    parser.add_argument("--error-catalog", default=None, help="Path to error catalog JSON file or inline JSON string")
    parser.add_argument(
        "--json-out",
        nargs="?",
        const=True,
        default=False,
        help="Print raw JSON or write to specified file path",
    )
    parser.add_argument(
        "--progressive",
        action="store_true",
        default=False,
        help="Enable bounded progressive multi-step investigation (REQ-033, PI-01..PI-05)",
    )
    parser.add_argument(
        "--max-context-requests",
        type=int,
        default=1,
        help="Maximum follow-up context retrieval requests allowed (default: 1)",
    )
    parser.add_argument(
        "--export-jsonld",
        nargs="?",
        const=True,
        default=None,
        help="Export JSON-LD provenance artifact (print to stdout or write to file path)",
    )
    parser.add_argument(
        "--export-html",
        nargs="?",
        const=True,
        default=None,
        help="Export standalone HTML brief (print to stdout or write to file path)",
    )
    parser.add_argument(
        "--export-pdf",
        nargs="?",
        const=True,
        default=None,
        help="Export PDF report (write to file path)",
    )

    args = parser.parse_args(argv)

    # Load vendor manual matches or error catalog if provided
    vendor_matches: list[dict[str, Any]] = []
    for source_arg in (args.vendor_manual, args.error_catalog):
        if not source_arg:
            continue
        src_path = Path(source_arg)
        if not src_path.is_absolute():
            src_path = ROOT_DIR / src_path
        if src_path.is_file():
            try:
                parsed_data = json.loads(src_path.read_text(encoding="utf-8"))
                if isinstance(parsed_data, list):
                    vendor_matches.extend(parsed_data)
                elif isinstance(parsed_data, dict):
                    if "errors" in parsed_data and isinstance(parsed_data["errors"], list):
                        vendor_matches.extend(parsed_data["errors"])
                    else:
                        vendor_matches.append(parsed_data)
            except Exception:
                pass
        else:
            try:
                parsed_data = json.loads(source_arg)
                if isinstance(parsed_data, list):
                    vendor_matches.extend(parsed_data)
                elif isinstance(parsed_data, dict):
                    if "errors" in parsed_data and isinstance(parsed_data["errors"], list):
                        vendor_matches.extend(parsed_data["errors"])
                    else:
                        vendor_matches.append(parsed_data)
            except Exception:
                pass

    # 1. Read envelope stream
    if args.file:
        file_path = Path(args.file)
        if not file_path.is_absolute():
            file_path = ROOT_DIR / file_path
        raw_bytes = file_path.read_bytes() if file_path.exists() else b""
    else:
        raw_bytes = sys.stdin.buffer.read()

    # 2. Decode wire envelopes if data exists; otherwise allow 0-event retrieval
    if raw_bytes:
        events = list(iter_envelopes(raw_bytes))
    else:
        events = []

    # 3. Load schema & decode payloads (schema resolved relative to project root)
    schema_path = Path(args.schema)
    if not schema_path.is_absolute():
        schema_path = ROOT_DIR / schema_path
    registry = load_schema_file(str(schema_path))
    decoded = decode_all(events, registry)

    # 4. Reconstruct RetrievalResult for evidence context
    src_val = ALL if str(args.source).upper() == "ALL" else int(args.source)
    t_start = args.start
    t_end = args.end
    if t_end <= t_start:
        if events:
            t_start = min(e.timestamp_ns for e in events)
            t_end = max(e.timestamp_ns for e in events) + 1_000_000_000
        else:
            t_end = t_start + 1_000_000_000

    req = RetrievalRequest(
        source=src_val,
        t_start_ns=t_start,
        t_end_ns=t_end,
        max_events=args.max_events,
        event_type=args.event_type,
    )
    retrieval_res = RetrievalResult(request=req, events=events, truncated=args.truncated)

    # 5. Build evidence JSON
    evidence = build_evidence(decoded, query=args.query, retrieval=retrieval_res)

    # 5b. Clock skew compensation & cross-source causality analysis (REQ-035, REQ-048)
    skew_report = analyze_clock_skew(decoded)
    if skew_report.skew_detected:
        evidence["clock_skew"] = skew_report.to_dict()
        if skew_report.clock_caveat:
            evidence["clock_caveat"] = skew_report.clock_caveat

    if args.progressive:
        def local_retriever(r_req: RetrievalRequest) -> RetrievalResult:
            matched = [
                e for e in events
                if (r_req.source is ALL or e.source_id == r_req.source)
                and r_req.t_start_ns <= e.timestamp_ns < r_req.t_end_ns
                and (r_req.event_type is None or e.event_type == r_req.event_type)
            ]
            return RetrievalResult(
                request=r_req,
                events=matched[: r_req.max_events],
                truncated=len(matched) > r_req.max_events,
            )

        prog_res = investigate_progressive(
            query=args.query,
            initial_request=req,
            retriever=local_retriever,
            schema_registry=registry,
            max_context_requests=args.max_context_requests,
            initial_events=events,
            vendor_manual_matches=vendor_matches,
        )
        evidence = prog_res.evidence
        knowledge = prog_res.knowledge
        exemplars = prog_res.exemplars
        synthesis = prog_res.synthesis
        val_report = prog_res.validation_report
        model_name = prog_res.model_name
    else:
        # Standard single-shot retrieval & synthesis
        # 6. Retrieve knowledge & historical exemplars via Hybrid RAG
        knowledge = retrieve_relevant_knowledge(evidence, args.query)
        exemplars = retrieve_historical_exemplars(args.query, evidence=evidence, k=args.exemplars_k)

        # 7. Synthesize & validate with context window enforcement & think budget guard
        synthesis, val_report, model_name = synthesize_and_validate(
            evidence,
            args.query,
            enable_thinking=args.enable_thinking,
            thinking_budget=args.thinking_budget,
            effective_num_ctx=args.num_ctx,
            exemplars_k=args.exemplars_k,
            vendor_manual_matches=vendor_matches,
        )

    # 8. Output report or JSON
    report = render_report(
        args.query,
        retrieval_res,
        evidence,
        knowledge,
        synthesis,
        val_report,
        model_name,
        exemplars=exemplars,
        vendor_matches=vendor_matches,
    )

    out = {
        "evidence": evidence,
        "knowledge": knowledge,
        "vendor_matches": vendor_matches,
        "exemplars": [asdict(ex) for ex in exemplars],
        "synthesis": synthesis,
        "validation": {
            "verdict": val_report.verdict,
            "badge": str(getattr(val_report, "badge", val_report.verdict)),
            "valid_citations": val_report.valid_citations,
            "invalid_citations": val_report.invalid_citations,
            "notes": val_report.notes,
            "tier_results": getattr(val_report, "tier_results", {}),
            "force_axes": getattr(val_report, "force_axes", {}),
            "entity_bindings": getattr(val_report, "entity_bindings", []),
        },
        "model": model_name,
        "report": report,
    }
    if "clock_skew" in evidence:
        out["clock_skew"] = evidence["clock_skew"]

    if args.json_out:
        json_str = json.dumps(out, indent=2)
        if isinstance(args.json_out, str):
            out_file = Path(args.json_out)
            if not out_file.is_absolute():
                out_file = ROOT_DIR / out_file
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(json_str, encoding="utf-8")
            print(f"Report JSON written to {out_file}")
        else:
            print(json_str)
    else:
        print(report)

    # Post-Mortem Report Exporters (REQ-013, FR-13)
    if args.export_jsonld is not None:
        jsonld_str = export_jsonld(out)
        if isinstance(args.export_jsonld, str) and args.export_jsonld not in ("True", "true", ""):
            p = Path(args.export_jsonld)
            if not p.is_absolute():
                p = ROOT_DIR / p
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(jsonld_str, encoding="utf-8")
            print(f"JSON-LD provenance artifact written to {p}")
        else:
            print(jsonld_str)

    if args.export_html is not None:
        html_str = export_html(out)
        if isinstance(args.export_html, str) and args.export_html not in ("True", "true", ""):
            p = Path(args.export_html)
            if not p.is_absolute():
                p = ROOT_DIR / p
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(html_str, encoding="utf-8")
            print(f"HTML brief written to {p}")
        else:
            print(html_str)

    if args.export_pdf is not None:
        target_path = args.export_pdf if isinstance(args.export_pdf, str) and args.export_pdf not in ("True", "true", "") else "fss_report.pdf"
        p = Path(target_path)
        if not p.is_absolute():
            p = ROOT_DIR / p
        pdf_bytes = export_pdf(out, output_path=p)
        print(f"PDF report ({len(pdf_bytes)} bytes) written to {p}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
