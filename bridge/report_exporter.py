"""Post-Mortem Failure Investigation Report Exporter (REQ-013, FR-13).

Exports 8-part investigation results to:
  1. JSON-LD provenance artifact with @context, trace_id, SHA-256 integrity digest,
     epistemic tags, data gaps, validation badge, and timestamp.
  2. Standalone self-contained HTML brief with clean styling, collapsible sections,
     and audit badge.
  3. Valid PDF export (%PDF-1.4 binary stream) with pure-Python valid PDF 1.4 generator fallback.
"""

from __future__ import annotations

import datetime
import hashlib
import html
import io
import json
import re
from pathlib import Path
from typing import Any

from bridge.schemas import get_or_create_trace_id


def _compute_sha256(canonical_str: str) -> str:
    """Compute standard SHA-256 hexadecimal digest."""
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


def compute_report_integrity_digest(evidence: Any, synthesis: Any) -> str:
    """Compute deterministic SHA-256 integrity digest over canonical JSON of evidence and synthesis."""
    ev_str = json.dumps(evidence, sort_keys=True, default=str)
    syn_str = json.dumps(synthesis, sort_keys=True, default=str)
    return _compute_sha256(f"{ev_str}::{syn_str}")


def _normalize_report_data(data: Any) -> dict[str, Any]:
    """Extract standard failure investigation fields from varied dict/object formats."""
    if hasattr(data, "__dict__"):
        d = vars(data)
    elif isinstance(data, dict):
        d = data
    else:
        d = {"report": str(data)}

    evidence = d.get("evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}

    synthesis = d.get("synthesis", {})
    if not isinstance(synthesis, dict):
        synthesis = {}

    validation = d.get("validation", {})
    if hasattr(validation, "__dict__"):
        val_dict = vars(validation)
    elif isinstance(validation, dict):
        val_dict = validation
    else:
        val_dict = {"verdict": str(validation), "badge": str(validation)}

    query = d.get("query") or evidence.get("query") or "Operator failure investigation"
    trace_id = d.get("trace_id") or evidence.get("trace_id") or get_or_create_trace_id()
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    integrity_digest = d.get("integrity_digest") or compute_report_integrity_digest(evidence, synthesis)

    events = evidence.get("events", [])
    data_gaps = evidence.get("data_gaps", [])
    knowledge = d.get("knowledge", [])

    return {
        "query": query,
        "trace_id": trace_id,
        "timestamp": now_iso,
        "integrity_digest": integrity_digest,
        "evidence": evidence,
        "synthesis": synthesis,
        "validation": val_dict,
        "events": events,
        "data_gaps": data_gaps,
        "knowledge": knowledge,
        "model": d.get("model", "qwen3-fss-bridge"),
        "report_text": d.get("report", ""),
    }


def _compute_epistemic_tag(ev: dict[str, Any]) -> str:
    """Compute the epistemic status tag for an event."""
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


def export_jsonld(synthesis_result: Any) -> str:
    """Export 8-part investigation results to JSON-LD provenance artifact (REQ-013, FR-13).

    Contains:
      - @context with Schema.org and FSS vocabulary
      - trace_id and timestamp
      - SHA-256 integrity digest computed over evidence + synthesis
      - Epistemic tags, data gaps, validation badge
    """
    n = _normalize_report_data(synthesis_result)
    events = n["events"]

    epistemic_tags = {
        ev.get("event_id", f"ev-{i}"): _compute_epistemic_tag(ev)
        for i, ev in enumerate(events)
    }

    doc = {
        "@context": [
            "https://schema.org/",
            {
                "fss": "https://fss.org/schema/",
                "trace_id": "fss:trace_id",
                "integrity_digest": "fss:integrityDigest",
                "epistemic_tags": "fss:epistemicTags",
                "data_gaps": "fss:dataGaps",
                "validation_badge": "fss:validationBadge",
                "clock_skew": "fss:clockSkew",
                "contributing_events": "fss:contributingEvents",
            },
        ],
        "@type": "fss:InvestigationReport",
        "trace_id": n["trace_id"],
        "timestamp": n["timestamp"],
        "integrity_digest": n["integrity_digest"],
        "query": n["query"],
        "model": n["model"],
        "validation_badge": n["validation"].get("badge") or n["validation"].get("verdict", "ACCEPTED"),
        "verdict": n["validation"].get("verdict", "ACCEPTED"),
        "epistemic_tags": epistemic_tags,
        "data_gaps": n["data_gaps"],
        "clock_caveat": n["evidence"].get("clock_caveat"),
        "clock_skew": n["evidence"].get("clock_skew"),
        "synthesis": {
            "summary": n["synthesis"].get("summary"),
            "likely_cause": n["synthesis"].get("likely_cause"),
            "confidence": n["synthesis"].get("confidence"),
            "contributing_events": n["synthesis"].get("contributing_events", []),
            "uncertainty": n["synthesis"].get("uncertainty", []),
            "data_gaps": n["synthesis"].get("data_gaps", []),
        },
        "retrieval_summary": n["evidence"].get("retrieval"),
        "sources": n["evidence"].get("sources", []),
        "events_count": len(events),
        "validation_details": {
            "valid_citations": n["validation"].get("valid_citations", []),
            "invalid_citations": n["validation"].get("invalid_citations", []),
            "notes": n["validation"].get("notes", []),
        },
    }

    return json.dumps(doc, indent=2)


def export_html(synthesis_result: Any) -> str:
    """Export standalone self-contained HTML failure investigation brief with audit badge."""
    n = _normalize_report_data(synthesis_result)
    query_esc = html.escape(str(n["query"]))
    trace_id_esc = html.escape(str(n["trace_id"]))
    digest_esc = html.escape(str(n["integrity_digest"]))
    verdict_esc = html.escape(str(n["validation"].get("verdict", "ACCEPTED")))
    badge_esc = html.escape(str(n["validation"].get("badge") or verdict_esc))
    clock_caveat = n["evidence"].get("clock_caveat")

    # Determine badge color class
    badge_cls = "badge-accepted"
    if "REJECT" in verdict_esc.upper():
        badge_cls = "badge-rejected"
    elif "CORRECTION" in verdict_esc.upper() or "WARN" in verdict_esc.upper():
        badge_cls = "badge-warning"

    events = n["events"]
    gaps = n["data_gaps"]
    knowledge = n["knowledge"]
    synthesis = n["synthesis"]
    val = n["validation"]

    # Build Events Table Rows
    event_rows = []
    for ev in events:
        eid = html.escape(str(ev.get("event_id", "")))
        sid = html.escape(str(ev.get("source_id", "")))
        ts = ev.get("timestamp_ns") or ev.get("timestamp") or 0
        name = html.escape(str(ev.get("event_name") or ev.get("event_type") or ""))
        status = html.escape(str(ev.get("decode_status", "")))
        etag = html.escape(_compute_epistemic_tag(ev))
        f_str = html.escape(", ".join(f"{k}={v}" for k, v in ev.get("fields", {}).items()))

        event_rows.append(f"""
        <tr>
          <td><code>{sid}</code></td>
          <td><code>{ts}</code></td>
          <td><b>{name}</b></td>
          <td><span class="status-tag status-{status.lower()}">{status}</span></td>
          <td><span class="epistemic-tag">{etag}</span></td>
          <td><code>{eid}</code></td>
          <td class="fields-cell">{f_str}</td>
        </tr>
        """)
    event_rows_html = "\n".join(event_rows) if event_rows else "<tr><td colspan='7'>No events retrieved.</td></tr>"

    # Data gaps list
    gap_items = []
    for g in gaps:
        kind = html.escape(str(g.get("kind", "GAP")))
        detail = html.escape(str(g.get("detail", "")))
        gap_items.append(f"<li><b>[{kind}]</b> {detail}</li>")
    gap_html = "<ul>" + "\n".join(gap_items) + "</ul>" if gap_items else "<p class='dim'>None: all events fully decoded without gaps.</p>"

    # Contributing events
    contributing_items = []
    valid_citations = set(val.get("valid_citations", []))
    for ce in synthesis.get("contributing_events", []):
        eid = ce.get("event_id", "")
        eid_esc = html.escape(str(eid))
        valid = eid in valid_citations
        mark = "✓" if valid else "✗ NOT IN EVIDENCE"
        mark_cls = "mark-valid" if valid else "mark-invalid"
        st = html.escape(str(ce.get("source_id", "")))
        et = html.escape(str(ce.get("event_type", "")))
        contributing_items.append(
            f"<li><span class='{mark_cls}'>{mark}</span> <code>{eid_esc}</code> (Source {st}, {et})</li>"
        )
    contributing_html = "<ul>" + "\n".join(contributing_items) + "</ul>" if contributing_items else "<p class='dim'>None cited.</p>"

    # Uncertainties
    unc_items = [f"<li>{html.escape(str(u))}</li>" for u in synthesis.get("uncertainty", [])]
    unc_html = "<ul>" + "\n".join(unc_items) + "</ul>" if unc_items else "<p class='dim'>None reported.</p>"

    # Validation notes
    notes_items = [f"<li>{html.escape(str(note))}</li>" for note in val.get("notes", [])]
    notes_html = "<ul>" + "\n".join(notes_items) + "</ul>" if notes_items else "<p class='dim'>No validation notes.</p>"

    # Clock caveat HTML
    clock_html = ""
    if clock_caveat:
        clock_html = f"""
        <div class="alert-box clock-alert">
          <b>Clock Caveat:</b> {html.escape(clock_caveat)}
        </div>
        """

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>FSS Investigation Brief - {trace_id_esc}</title>
  <style>
    :root {{
      --bg: #0f141c;
      --card-bg: #18202c;
      --border: #263345;
      --text: #e1e7f0;
      --text-muted: #8b9bb4;
      --accent: #3b82f6;
      --accent-hover: #60a5fa;
      --badge-green: #10b981;
      --badge-red: #ef4444;
      --badge-amber: #f59e0b;
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: var(--font-sans);
      line-height: 1.6;
      padding: 24px;
    }}
    .container {{
      max-width: 1200px;
      margin: 0 auto;
    }}
    header {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 24px;
      margin-bottom: 24px;
    }}
    .header-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      margin-bottom: 12px;
    }}
    h1 {{
      font-size: 1.5rem;
      letter-spacing: -0.02em;
      color: #fff;
    }}
    .badge {{
      display: inline-block;
      padding: 6px 14px;
      border-radius: 20px;
      font-weight: 700;
      font-size: 0.85rem;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }}
    .badge-accepted {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }}
    .badge-rejected {{ background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; }}
    .badge-warning {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #f59e0b; }}
    .meta-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
      font-size: 0.85rem;
      color: var(--text-muted);
      font-family: var(--font-mono);
    }}
    .meta-bar span b {{ color: var(--text); }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      margin-bottom: 16px;
      overflow: hidden;
    }}
    details {{
      width: 100%;
    }}
    summary {{
      padding: 16px 20px;
      font-size: 1.05rem;
      font-weight: 600;
      cursor: pointer;
      background: rgba(255, 255, 255, 0.02);
      border-bottom: 1px solid transparent;
      user-select: none;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    details[open] summary {{
      border-bottom: 1px solid var(--border);
    }}
    .content {{
      padding: 20px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      font-family: var(--font-mono);
    }}
    th, td {{
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }}
    th {{
      background: rgba(255, 255, 255, 0.04);
      color: var(--text-muted);
      font-weight: 600;
    }}
    tr:hover td {{
      background: rgba(255, 255, 255, 0.02);
    }}
    code {{
      font-family: var(--font-mono);
      background: rgba(0, 0, 0, 0.3);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 0.85em;
    }}
    .fields-cell {{
      max-width: 320px;
      word-break: break-all;
    }}
    .status-tag {{
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 600;
    }}
    .status-success {{ background: #064e3b; color: #6ee7b7; }}
    .status-partially_decoded {{ background: #78350f; color: #fde68a; }}
    .status-unknown_event_type {{ background: #374151; color: #d1d5db; }}
    .status-malformed_payload {{ background: #7f1d1d; color: #fca5a5; }}
    .epistemic-tag {{
      display: inline-block;
      padding: 2px 8px;
      background: #1e293b;
      border-radius: 4px;
      font-size: 0.75rem;
      color: #94a3b8;
    }}
    .alert-box {{
      padding: 12px 16px;
      border-radius: 6px;
      margin-bottom: 16px;
      font-size: 0.9rem;
    }}
    .clock-alert {{
      background: rgba(245, 158, 11, 0.15);
      border-left: 4px solid var(--badge-amber);
      color: #fde68a;
    }}
    ul {{
      margin-left: 20px;
      margin-bottom: 12px;
    }}
    li {{
      margin-bottom: 6px;
    }}
    .mark-valid {{ color: #34d399; font-weight: bold; }}
    .mark-invalid {{ color: #f87171; font-weight: bold; }}
    .dim {{ color: var(--text-muted); font-style: italic; }}
    pre {{
      background: #090d14;
      padding: 16px;
      border-radius: 6px;
      overflow-x: auto;
      font-family: var(--font-mono);
      font-size: 0.82rem;
    }}
    footer {{
      margin-top: 32px;
      text-align: center;
      color: var(--text-muted);
      font-size: 0.8rem;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="header-top">
        <h1>Failure Synthesis System (FSS) Report</h1>
        <span class="badge {badge_cls}">{badge_esc}</span>
      </div>
      <div class="meta-bar">
        <span>Trace ID: <b>{trace_id_esc}</b></span>
        <span>Generated: <b>{n['timestamp']}</b></span>
        <span>SHA-256 Digest: <b>{digest_esc[:16]}...</b></span>
        <span>Model: <b>{html.escape(n['model'])}</b></span>
      </div>
    </header>

    {clock_html}

    <!-- [1] Question -->
    <div class="card">
      <details open>
        <summary>[1] Question</summary>
        <div class="content">
          <p style="font-size: 1.1rem; font-weight: 500;">{query_esc}</p>
        </div>
      </details>
    </div>

    <!-- [2] Retrieval -->
    <div class="card">
      <details open>
        <summary>[2] Retrieval Parameters & Scope</summary>
        <div class="content">
          <pre>{html.escape(json.dumps(n['evidence'].get('retrieval', {}), indent=2))}</pre>
        </div>
      </details>
    </div>

    <!-- [3] Events -->
    <div class="card">
      <details open>
        <summary>[3] Telemetry Events ({len(events)})</summary>
        <div class="content" style="overflow-x: auto;">
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Timestamp (ns)</th>
                <th>Event</th>
                <th>Status</th>
                <th>Epistemic Tag</th>
                <th>Event ID</th>
                <th>Decoded Fields</th>
              </tr>
            </thead>
            <tbody>
              {event_rows_html}
            </tbody>
          </table>
        </div>
      </details>
    </div>

    <!-- [4] Data Gaps -->
    <div class="card">
      <details open>
        <summary>[4] Data Gaps & Quarantine ({len(gaps)})</summary>
        <div class="content">
          {gap_html}
        </div>
      </details>
    </div>

    <!-- [5] Knowledge -->
    <div class="card">
      <details>
        <summary>[5] Domain Knowledge & Hybrid RAG Chunks ({len(knowledge)})</summary>
        <div class="content">
          <pre>{html.escape(json.dumps(knowledge, indent=2))}</pre>
        </div>
      </details>
    </div>

    <!-- [6] LLM Context -->
    <div class="card">
      <details>
        <summary>[6] LLM Grounding Context</summary>
        <div class="content">
          <pre>{html.escape(n.get('report_text') or json.dumps(n['evidence'], indent=2))}</pre>
        </div>
      </details>
    </div>

    <!-- [7] Synthesis -->
    <div class="card">
      <details open>
        <summary>[7] AI Reasoning Synthesis</summary>
        <div class="content">
          <p><b>Summary:</b> {html.escape(str(synthesis.get('summary', '')))}</p>
          <br>
          <p><b>Likely Cause:</b> {html.escape(str(synthesis.get('likely_cause', 'None')))}</p>
          <br>
          <p><b>Confidence:</b> <span class="badge" style="background:#1f2937; color:#e5e7eb;">{html.escape(str(synthesis.get('confidence', '')))}</span></p>
          <br>
          <p><b>Contributing Events:</b></p>
          {contributing_html}
          <br>
          <p><b>Uncertainty & Caveats:</b></p>
          {unc_html}
        </div>
      </details>
    </div>

    <!-- [8] Validator -->
    <div class="card">
      <details open>
        <summary>[8] Audit Validator & Verdict</summary>
        <div class="content">
          <p><b>Verdict:</b> <span class="badge {badge_cls}">{verdict_esc}</span></p>
          <br>
          <p><b>Audit Notes:</b></p>
          {notes_html}
        </div>
      </details>
    </div>

    <footer>
      Failure Synthesis System (FSS) &bull; Cryptographically Verified Provenance Digest: <code>{digest_esc}</code>
    </footer>
  </div>
</body>
</html>
"""
    return html_doc


def _generate_pure_python_pdf(title: str, text_lines: list[str]) -> bytes:
    """Generate a clean, strictly compliant PDF-1.4 binary stream with zero external dependencies."""
    out = io.BytesIO()

    # Sanitized ASCII lines for Helvetica Type1 font
    safe_lines = []
    for line in text_lines:
        clean = re.sub(r"[^\x20-\x7E]", " ", line)
        clean = clean.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        safe_lines.append(clean)

    # 45 lines per page
    lines_per_page = 45
    pages_lines = [safe_lines[i : i + lines_per_page] for i in range(0, len(safe_lines), lines_per_page)]
    if not pages_lines:
        pages_lines = [["(No report content)"]]

    page_count = len(pages_lines)

    # Catalog obj 1, Pages obj 2
    # Page objs: 3 .. 3 + page_count - 1
    # Content stream objs: 3 + page_count .. 3 + 2*page_count - 1
    # Font obj: 3 + 2*page_count

    font_obj_id = 3 + 2 * page_count
    kids_refs = " ".join(f"{3 + p} 0 R" for p in range(page_count))

    objects: list[bytes] = []

    # Obj 1: Catalog
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    # Obj 2: Pages
    objects.append(f"2 0 obj\n<< /Type /Pages /Kids [{kids_refs}] /Count {page_count} >>\nendobj\n".encode("latin1"))

    # Page objects
    for p in range(page_count):
        content_ref = f"{3 + page_count + p} 0 R"
        page_obj = (
            f"{3 + p} 0 obj\n"
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_ref} /Resources << /Font << /F1 {font_obj_id} 0 R >> >> >>\n"
            f"endobj\n"
        ).encode("latin1")
        objects.append(page_obj)

    # Stream content objects
    for p, plines in enumerate(pages_lines):
        stream_content = ["BT", "/F1 9 Tf", "40 750 Td", "14 TL"]
        # Header line on first page
        if p == 0:
            stream_content.append(f"({title[:70]}) Tj")
            stream_content.append("T*")
            stream_content.append("T*")

        for pl in plines:
            stream_content.append(f"({pl[:95]}) Tj")
            stream_content.append("T*")
        stream_content.append("ET")

        stream_bytes = "\n".join(stream_content).encode("latin1")
        stream_obj = (
            f"{3 + page_count + p} 0 obj\n"
            f"<< /Length {len(stream_bytes)} >>\n"
            f"stream\n"
        ).encode("latin1") + stream_bytes + b"\nendstream\nendobj\n"
        objects.append(stream_obj)

    # Font obj
    objects.append(
        f"{font_obj_id} 0 obj\n"
        f"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\n"
        f"endobj\n".encode("latin1")
    )

    # Write PDF
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for obj in objects:
        offsets.append(out.tell())
        out.write(obj)

    xref_offset = out.tell()
    total_objects = font_obj_id + 1
    out.write(f"xref\n0 {total_objects}\n".encode("latin1"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode("latin1"))

    trailer = (
        f"trailer\n"
        f"<< /Size {total_objects} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("latin1")
    out.write(trailer)

    return out.getvalue()


def export_pdf(
    synthesis_result: Any,
    output_path: str | Path | None = None,
) -> bytes:
    """Export 8-part investigation result to a valid PDF (%PDF-1.4 binary stream).

    Uses ReportLab if available; falls back to pure-Python PDF-1.4 generator.
    Guarantees:
      - Valid %PDF-1.4 header
      - Terminates cleanly with %%EOF
      - Can be written directly to output_path if provided.
    """
    n = _normalize_report_data(synthesis_result)
    title = f"FSS Investigation Brief - {n['trace_id']}"

    # Flatten text lines from report
    lines: list[str] = []
    lines.append("=" * 75)
    lines.append(f"  FAILURE SYNTHESIS SYSTEM (FSS) BRIEF: {n['trace_id']}")
    lines.append(f"  Verdict: {n['validation'].get('verdict', 'ACCEPTED')} | Digest: {n['integrity_digest'][:24]}...")
    lines.append("=" * 75)
    lines.append("")
    lines.append(f"Question: {n['query']}")
    lines.append("")

    if n.get("report_text"):
        for rline in str(n["report_text"]).splitlines():
            lines.append(rline)
    else:
        syn = n["synthesis"]
        lines.append(f"Summary: {syn.get('summary')}")
        lines.append(f"Likely Cause: {syn.get('likely_cause')}")
        lines.append(f"Confidence: {syn.get('confidence')}")
        lines.append("")
        lines.append("Contributing Events:")
        for ce in syn.get("contributing_events", []):
            lines.append(f"  - {ce.get('event_id')}: Source {ce.get('source_id')} {ce.get('event_type')}")
        lines.append("")
        lines.append("Data Gaps:")
        for g in n["data_gaps"]:
            lines.append(f"  - [{g.get('kind')}] {g.get('detail')}")

    pdf_bytes: bytes | None = None

    # Try ReportLab first if available
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        c.setTitle(title)

        y = 750
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, y, title[:70])
        y -= 20

        c.setFont("Courier", 8)
        for line in lines:
            if y < 40:
                c.showPage()
                c.setFont("Courier", 8)
                y = 750
            # Sanitize latin-1 printable
            clean_l = re.sub(r"[^\x20-\x7E]", " ", line)
            c.drawString(40, y, clean_l[:105])
            y -= 11

        c.save()
        pdf_bytes = buf.getvalue()
    except Exception:
        pdf_bytes = None

    # Fallback to pure-Python generator
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF-1.4"):
        pdf_bytes = _generate_pure_python_pdf(title, lines)

    if output_path is not None:
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(pdf_bytes)

    return pdf_bytes
