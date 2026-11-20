"""Stage 0 — Pre-Generation Sufficiency Gate (RINSE-Inspired) for FSS.

Implements pre-generation epistemic gating before invoking LLM synthesis:
1. Deterministic Short-Circuit:
   - When JavaRetriever returns status == "EMPTY" (0 events) or unrecorded
     data gaps span the requested failure window, bypass LLM generation
     and return INSUFFICIENT_EVIDENCE directly to Data Gaps Quarantine.
2. Lightweight Local Pre-Generation Sufficiency Scorer (RINSE-Inspired):
   - Question content coverage: entities, subsystems, and actions in query
     must be represented in retrieved evidence or domain knowledge.
   - Candidate span / anomaly detection: detects whether candidate anomaly
     spans or state transitions exist to answer failure queries.
   - Cross-passage latent sufficiency: validates that events + domain
     knowledge contain the requisite causal/explanatory bridge.
3. Structured Quarantine Card:
   - Emits structured quarantine payload with missing entities, unrecorded
     spans, and actionable guidance (e.g., scope widening).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SufficiencyStatus(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class SufficiencyCheckResult:
    name: str
    passed: bool
    score: float
    details: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "score": round(self.score, 4),
            "details": self.details,
        }


@dataclass
class SufficiencyResult:
    is_sufficient: bool
    status: SufficiencyStatus
    score: float
    reason: str
    checks: list[SufficiencyCheckResult] = field(default_factory=list)
    quarantine_payload: dict[str, Any] = field(default_factory=dict)
    bypass_synthesis: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_sufficient": self.is_sufficient,
            "status": str(self.status),
            "score": round(self.score, 4),
            "reason": self.reason,
            "bypass_synthesis": self.bypass_synthesis,
            "checks": [c.to_dict() for c in self.checks],
            "quarantine_payload": self.quarantine_payload,
        }


# Known failure keywords that indicate a causal or diagnostic failure query
FAILURE_QUERY_KEYWORDS = {
    "why",
    "cause",
    "caused",
    "fail",
    "failed",
    "failure",
    "fault",
    "anomaly",
    "leave",
    "left",
    "crash",
    "timeout",
    "error",
    "deviation",
    "drift",
    "diverge",
    "diverged",
    "divergence",
    "disconnect",
    "disconnected",
    "degraded",
    "collision",
    "wrong",
    "issue",
    "problem",
    "alarm",
    "warning",
    "glitch",
    "loss",
    "lost",
    "drop",
    "dropped",
    "abort",
    "aborted",
    "unstable",
    "exceed",
    "exceeded",
    "spike",
    "spiked",
    "root",
}

# Known anomaly event names and status values
ANOMALY_EVENT_KEYWORDS = (
    "AVOIDANCE",
    "COLLISION_RISK",
    "ERROR",
    "FAULT",
    "FAIL",
    "WARN",
    "ALARM",
    "CRASH",
    "EMERGENCY",
    "ABORT",
    "ANOMALY",
    "GLITCH",
    "DROPOUT",
    "DESYNC",
    "DRIFT",
    "DIVERGE",
    "DISARM",
    "ERR",
    "EV",
)

ANOMALY_STATUS_VALUES = {
    "LIDAR_TIMEOUT",
    "SENSOR_DISCONNECTED",
    "INVALID_SCAN",
    "FAULT",
    "DEGRADED",
    "LEFT_FORMATION",
    "ERROR",
    "FAIL",
    "FAILED",
    "FAILURE",
    "CRASH",
    "CRITICAL",
    "WARNING",
    "UNHEALTHY",
    "TIMEOUT",
    "DISCONNECTED",
    "GLITCH",
    "DESYNC",
    "DRIFT",
    "DIVERGED",
    "DIVERGENCE",
    "FAILSAFE",
    "BAD",
}


def _extract_queried_entities(query: str) -> set[int]:
    """Extract explicit entity IDs (e.g. drone 474, agent 12, uav 3, source 298) from query.

    ONLY treats an entity as required when explicitly specified with entity prefixes
    (e.g., drone, agent, uav, source, host, node, vehicle).
    Does NOT match arbitrary 3-4 digit numbers (e.g. 404, 500, 100, 042, 171).
    """
    entities: set[int] = set()
    # Match 'drone 474', 'drone #474', 'source 298', 'agent 12', 'uav 3', 'node 474', 'vehicle 5'
    for match in re.finditer(r"\b(?:drone|agent|uav|source|host|node|vehicle)[^\w\d]*([0-9]{1,10})\b", query, re.IGNORECASE):
        try:
            entities.add(int(match.group(1)))
        except ValueError:
            pass

    return entities


def _check_deterministic_gate(evidence: dict[str, Any], query: str) -> tuple[bool, str, dict[str, Any]]:
    """Deterministic short-circuit gate.

    Returns (is_passed, reason, quarantine_meta).
    """
    events = evidence.get("events", [])
    retrieval = evidence.get("retrieval") or {}
    data_gaps = evidence.get("data_gaps", [])

    # 1. 0 events check (status == EMPTY or 0 events in retrieval)
    ret_returned = retrieval.get("returned")
    ret_status = retrieval.get("status")
    if len(events) == 0 or ret_returned == 0 or ret_status == "EMPTY":
        return (
            False,
            "Deterministic Gate: 0 events retrieved in requested time window (status EMPTY). "
            "Bypassing LLM generation to Data Gaps Quarantine.",
            {
                "gate": "EMPTY_RETRIEVAL",
                "events_count": 0,
                "retrieval_status": ret_status or "EMPTY",
                "recommendation": "Widen time window or remove restrictive source/type filters.",
            },
        )

    # 2. Data gaps spanning entire window / all events opaque or malformed
    decode_statuses = [e.get("decode_status") for e in events]
    unusable_statuses = {"UNKNOWN_EVENT_TYPE", "MALFORMED_PAYLOAD"}
    if all(s in unusable_statuses for s in decode_statuses):
        return (
            False,
            f"Deterministic Gate: All {len(events)} retrieved events are uninterpretable "
            f"({set(decode_statuses)}). Telemetry structure is missing across entire window.",
            {
                "gate": "UNINTERPRETABLE_DATA_GAPS",
                "events_count": len(events),
                "decode_statuses": list(set(decode_statuses)),
                "recommendation": "Ingest missing schema configuration or examine raw wire transport.",
            },
        )

    # 3. Check for unrecorded time gap spanning the failure window in data_gaps
    for g in data_gaps:
        kind = str(g.get("kind", ""))
        detail = str(g.get("detail", ""))
        if (
            "spanning requested failure window" in detail.lower()
            or "unrecorded data gap" in detail.lower()
            or "blackout" in detail.lower()
            or "blackout" in kind.lower()
            or "unrecorded_time_gap" in kind.lower()
        ):
            return (
                False,
                f"Deterministic Gate: Unrecorded telemetry data gap [{kind}] spans the requested failure window. "
                "Causal linkage cannot be grounded.",
                {
                    "gate": "SPANNING_DATA_GAP",
                    "gap_kind": kind,
                    "gap_detail": detail,
                    "recommendation": "Query adjacent time partition or alternate telemetry source.",
                },
            )

    # 4. Check if telemetry events exhibit an unrecorded blackout gap (> 30s) across failure window
    if len(events) >= 2:
        by_source: dict[int, list[int]] = {}
        for ev in events:
            src = ev.get("source_id", 0)
            ts = ev.get("timestamp_ns") or ev.get("timestamp") or 0
            by_source.setdefault(src, []).append(ts)
        for src, ts_list in by_source.items():
            ts_list.sort()
            for i in range(len(ts_list) - 1):
                delta_ns = ts_list[i + 1] - ts_list[i]
                if 30_000_000_000 <= delta_ns <= 3600_000_000_000:
                    gap_sec = delta_ns / 1_000_000_000
                    return (
                        False,
                        f"Deterministic Gate: Genuine telemetry blackout gap of {gap_sec:.1f}s on source {src} covers failure window. "
                        "Causal linkage cannot be grounded.",
                        {
                            "gate": "SPANNING_DATA_GAP",
                            "gap_kind": "BLACKOUT_DATA_GAP",
                            "gap_detail": f"Unrecorded telemetry blackout of {gap_sec:.1f}s between timestamps {ts_list[i]} and {ts_list[i+1]}.",
                            "recommendation": "Examine pre-blackout partition or recover telemetry from alternate recorder.",
                        },
                    )

    return (True, "Deterministic short-circuit passed: Non-empty, decodable telemetry present.", {})


def _check_content_coverage(evidence: dict[str, Any], query: str) -> tuple[bool, float, str, set[int]]:
    """Check whether query entities and topics are covered in evidence or knowledge.

    Returns (passed, score, details, missing_entities).
    """
    queried_entities = _extract_queried_entities(query)
    evidence_sources = set(evidence.get("sources", []))
    events = evidence.get("events", [])
    for e in events:
        if "source_id" in e:
            evidence_sources.add(e["source_id"])

    # If no explicit entity was queried, content coverage passes with 1.0 score
    if not queried_entities:
        return (
            True,
            1.0,
            "General query without explicit entity filter: full content coverage.",
            set(),
        )

    # If an explicit entity was queried and is missing from retrieved sources, fail as missing entity
    missing_entities = queried_entities - evidence_sources
    if missing_entities:
        missing_str = ", ".join(map(str, sorted(missing_entities)))
        present_str = ", ".join(map(str, sorted(evidence_sources))) if evidence_sources else "none"
        return (
            False,
            0.0,
            f"Queried entity/entities [{missing_str}] not present in retrieved evidence sources [{present_str}].",
            missing_entities,
        )

    return (
        True,
        1.0,
        f"Full entity coverage: all queried entities {sorted(queried_entities)} present in evidence.",
        set(),
    )


def _check_candidate_spans(
    evidence: dict[str, Any],
    query: str,
    vendor_manual_matches: list[dict[str, Any]] | None = None,
) -> tuple[bool, float, str, list[str]]:
    """Check whether candidate anomaly spans exist in evidence to support the query.

    Recognizes error events, subsystem codes, status deviations, and vendor-manual
    error matches as candidate anomaly spans so failure questions about logs containing
    error records do not fail.

    Returns (passed, score, details, detected_anomalies).
    """
    query_words = set(re.findall(r"\b[A-Za-z0-9_]+\b", query.lower()))
    is_failure_query = bool(query_words & FAILURE_QUERY_KEYWORDS)
    queried_entities = _extract_queried_entities(query)
    is_open_ended = bool(
        re.search(
            r"\b(what went wrong|analyze all errors|summarize anomalies|root causes|in this log|using the error code manual|error manual|flight status|telemetry)\b",
            query,
            re.IGNORECASE,
        )
    )

    events = evidence.get("events", [])
    anomalies: list[str] = []

    # 1. Telemetry events inspection
    for ev in events:
        ename = (ev.get("event_name") or ev.get("event_type") or "").upper()
        eid = ev.get("event_id", "ev-unknown")
        src = ev.get("source_id", "src-unknown")

        # Check event name against anomaly keywords
        if any(kw in ename for kw in ANOMALY_EVENT_KEYWORDS):
            anomalies.append(f"{ename} [{eid}] on source {src}")

        # Check decode status
        dstatus = ev.get("decode_status")
        if dstatus and dstatus not in ("SUCCESS", "OK", "NOMINAL"):
            anomalies.append(f"{dstatus} [{eid}] on source {src}")

        fields = ev.get("fields", {})
        for k, v in fields.items():
            k_lower = k.lower()
            v_str = str(v).upper()

            # Status / state values
            if v_str in ANOMALY_STATUS_VALUES or any(
                w in v_str for w in ("FAULT", "ERROR", "TIMEOUT", "DISCONNECT", "DEGRADED", "FAIL")
            ):
                anomalies.append(f"{ename}.{k}={v_str} [{eid}] on source {src}")

            # Subsystem codes and error codes
            if k_lower in ("subsys", "subsystem", "ecode", "error_code", "err_code", "err", "status_code"):
                if isinstance(v, (int, float)) and v != 0:
                    anomalies.append(f"{ename}.{k}={v} [{eid}] on source {src}")
                elif isinstance(v, str) and v.upper() not in ("0", "NONE", "OK", "NOMINAL", "SUCCESS", ""):
                    anomalies.append(f"{ename}.{k}={v} [{eid}] on source {src}")

            # EKF variance and drift metrics
            if k_lower in ("comp_var", "vel_var", "pos_var", "variance", "drift", "divergence"):
                try:
                    if float(v) >= 0.5:
                        anomalies.append(f"{ename}.{k}={v:.2f} (divergence) [{eid}] on source {src}")
                except (ValueError, TypeError):
                    pass

        # Check interpreted fields
        for interp in ev.get("interpreted_fields", []):
            if any(w in str(interp).upper() for w in ("FAULT", "ERROR", "TIMEOUT", "WARN")):
                anomalies.append(f"Interpreted: {interp} [{eid}] on source {src}")

    # 2. Check explicit error events in evidence
    for err_ev in evidence.get("error_events", []) or []:
        anomalies.append(f"ErrorEvent: {err_ev}")

    # 3. Check vendor-manual error matches
    all_vendor_matches = list(vendor_manual_matches or [])
    if evidence.get("vendor_manual_matches"):
        all_vendor_matches.extend(evidence["vendor_manual_matches"])
    if evidence.get("error_catalog"):
        ec = evidence["error_catalog"]
        if isinstance(ec, list):
            all_vendor_matches.extend(ec)
        elif isinstance(ec, dict) and "errors" in ec:
            all_vendor_matches.extend(ec["errors"])

    for vm in all_vendor_matches:
        vm_id = vm.get("id") or vm.get("err_id") or vm.get("code") or "err-match"
        vm_title = vm.get("title") or vm.get("name") or "Vendor Error Code Match"
        anomalies.append(f"Vendor Manual Match: {vm_title} [{vm_id}]")

    # 4. Evaluate candidate spans coverage
    if anomalies:
        score = min(1.0, 0.5 + 0.1 * len(anomalies))
        return (
            True,
            score,
            f"Detected {len(anomalies)} candidate anomaly span(s): {', '.join(anomalies[:3])}{'...' if len(anomalies) > 3 else ''}.",
            anomalies,
        )

    # If 0 anomalies were detected:
    # If the user asked a specific failure query asserting a specific entity failed, fail candidate spans
    if is_failure_query and queried_entities and not is_open_ended:
        return (
            False,
            0.2,
            "Failure query asked, but 100% of retrieved events are nominal with zero anomaly spans or state deviations.",
            [],
        )

    # General / exploratory query or altitude / status overview on nominal log
    return (
        True,
        0.8,
        "Retrieved events present nominal telemetry matching general status query (zero anomalies detected).",
        [],
    )


def _check_cross_passage_latent(
    evidence: dict[str, Any],
    query: str,
    knowledge: list[dict[str, Any]] | None,
    vendor_manual_matches: list[dict[str, Any]] | None = None,
) -> tuple[bool, float, str]:
    """Check cross-passage latent sufficiency (warrant bridge between events and domain knowledge).

    Returns (passed, score, details).
    """
    events = evidence.get("events", [])
    knowledge_list = knowledge or []
    vendor_matches = vendor_manual_matches or []

    # If vendor manual matches exist, they provide an explicit warrant mapping for telemetry errors
    if vendor_matches:
        return (
            True,
            1.0,
            f"Cross-passage warrant check verified ({len(vendor_matches)} vendor manual error match(es) grounded).",
        )

    # Check if domain knowledge covers any events or query terms
    event_terms = set()
    for ev in events:
        if ev.get("event_name"):
            event_terms.add(ev["event_name"].upper())
        for v in ev.get("fields", {}).values():
            if isinstance(v, str):
                event_terms.add(v.upper())

    matching_docs = 0
    for doc in knowledge_list:
        explains = [x.strip().upper() for x in doc.get("explains", "").split(",")]
        if any(e in event_terms for e in explains):
            matching_docs += 1

    if knowledge_list and matching_docs == 0 and len(events) > 0:
        return (
            True,
            0.6,
            "Domain knowledge retrieved but contains no direct warrant mapping to observed telemetry events.",
        )

    score = 1.0 if matching_docs > 0 else 0.8
    return (
        True,
        score,
        f"Cross-passage warrant check verified ({matching_docs} domain doc(s) explain observed event types).",
    )


def evaluate_sufficiency(
    evidence: dict[str, Any],
    query: str,
    knowledge: list[dict[str, Any]] | None = None,
    threshold: float = 0.5,
    vendor_manual_matches: list[dict[str, Any]] | None = None,
    error_catalog: list[dict[str, Any]] | dict[str, Any] | None = None,
) -> SufficiencyResult:
    """Pre-generation Sufficiency Gate (Stage 0).

    Gates synthesis before invoking LLM. Returns SufficiencyResult.
    """
    # Normalize vendor manual matches
    all_vendor_matches: list[dict[str, Any]] = []
    if vendor_manual_matches:
        all_vendor_matches.extend(vendor_manual_matches)
    if error_catalog:
        if isinstance(error_catalog, list):
            all_vendor_matches.extend(error_catalog)
        elif isinstance(error_catalog, dict):
            if "errors" in error_catalog and isinstance(error_catalog["errors"], list):
                all_vendor_matches.extend(error_catalog["errors"])
            else:
                for k, v in error_catalog.items():
                    if isinstance(v, dict):
                        all_vendor_matches.append({**v, "id": v.get("id", f"err-{k}")})
                    else:
                        all_vendor_matches.append({"id": f"err-{k}", "title": str(k), "description": str(v)})

    checks: list[SufficiencyCheckResult] = []

    # 1. Deterministic Short-Circuit Check
    det_passed, det_reason, det_meta = _check_deterministic_gate(evidence, query)
    checks.append(
        SufficiencyCheckResult(
            name="deterministic_gate",
            passed=det_passed,
            score=1.0 if det_passed else 0.0,
            details=det_reason,
        )
    )

    if not det_passed:
        quarantine_payload = {
            "status": "QUARANTINED",
            "reason": det_reason,
            "gate_trigger": det_meta.get("gate", "DETERMINISTIC"),
            "retrieval_meta": det_meta,
            "data_gaps": evidence.get("data_gaps", []),
            "recommendation": det_meta.get("recommendation", "Expand retrieval query parameters."),
        }
        return SufficiencyResult(
            is_sufficient=False,
            status=SufficiencyStatus.INSUFFICIENT_EVIDENCE,
            score=0.0,
            reason=det_reason,
            checks=checks,
            quarantine_payload=quarantine_payload,
            bypass_synthesis=True,
        )

    # 2. Content Coverage Check
    cov_passed, cov_score, cov_details, missing_entities = _check_content_coverage(evidence, query)
    checks.append(
        SufficiencyCheckResult(
            name="question_content_coverage",
            passed=cov_passed,
            score=cov_score,
            details=cov_details,
        )
    )

    # 3. Candidate Span / Anomaly Detection Check
    span_passed, span_score, span_details, detected_anomalies = _check_candidate_spans(
        evidence, query, vendor_manual_matches=all_vendor_matches
    )
    checks.append(
        SufficiencyCheckResult(
            name="candidate_span_detection",
            passed=span_passed,
            score=span_score,
            details=span_details,
        )
    )

    # 4. Cross-Passage Latent Sufficiency Check
    lat_passed, lat_score, lat_details = _check_cross_passage_latent(
        evidence, query, knowledge, vendor_manual_matches=all_vendor_matches
    )
    checks.append(
        SufficiencyCheckResult(
            name="cross_passage_latent_sufficiency",
            passed=lat_passed,
            score=lat_score,
            details=lat_details,
        )
    )

    # Composite Score calculation
    # Weights: 40% Content Coverage, 35% Candidate Spans, 25% Cross-Passage
    composite_score = 0.40 * cov_score + 0.35 * span_score + 0.25 * lat_score

    # Hard blocker: If content coverage is 0 (queried entity absent), cannot be sufficient
    if cov_score == 0.0:
        is_sufficient = False
        reason = f"Insufficient evidence: {cov_details}"
    elif composite_score < threshold:
        is_sufficient = False
        reasons = [c.details for c in checks if not c.passed or c.score < 0.5]
        reason = f"Insufficient evidence (sufficiency score {composite_score:.2f} < {threshold}): " + "; ".join(reasons)
    else:
        is_sufficient = True
        reason = f"Evidence meets pre-generation sufficiency threshold ({composite_score:.2f} >= {threshold})."

    quarantine_payload = {}
    if not is_sufficient:
        quarantine_payload = {
            "status": "QUARANTINED",
            "reason": reason,
            "sufficiency_score": round(composite_score, 4),
            "missing_entities": list(missing_entities),
            "detected_anomalies": detected_anomalies,
            "data_gaps": evidence.get("data_gaps", []),
            "recommendation": (
                f"Scope widening required: query sources {[e for e in missing_entities]} or broaden query filter."
                if missing_entities
                else "Broaden time window to capture antecedent events."
            ),
        }

    return SufficiencyResult(
        is_sufficient=is_sufficient,
        status=SufficiencyStatus.SUFFICIENT if is_sufficient else SufficiencyStatus.INSUFFICIENT_EVIDENCE,
        score=composite_score,
        reason=reason,
        checks=checks,
        quarantine_payload=quarantine_payload,
        bypass_synthesis=not is_sufficient,
    )


def create_insufficient_evidence_synthesis(
    sufficiency: SufficiencyResult,
    evidence: dict[str, Any],
    query: str,
) -> dict[str, Any]:
    """Construct an explicit epistemic quarantine synthesis bypassing LLM generation."""
    events = evidence.get("events", [])
    gaps = evidence.get("data_gaps", [])

    data_gap_summaries: list[str] = []
    for g in gaps:
        eid_str = f"Event {g['event_id']}: " if g.get("event_id") else (f"[{g.get('kind')}] " if g.get("kind") else "")
        data_gap_summaries.append(f"{eid_str}{g.get('detail', '')}")

    # Add quarantine notice to data gaps
    if sufficiency.quarantine_payload:
        q = sufficiency.quarantine_payload
        data_gap_summaries.append(f"[DATA_GAPS_QUARANTINE] {q.get('reason', '')} Recommendation: {q.get('recommendation', '')}")

    if not events:
        return {
            "summary": "No events recorded or retrieved in the requested time window.",
            "likely_cause": None,
            "confidence": "high",
            "contributing_events": [],
            "uncertainty": [],
            "data_gaps": data_gap_summaries,
            "quarantine_card": sufficiency.quarantine_payload,
        }

    return {
        "summary": f"INSUFFICIENT EVIDENCE to diagnose query '{query}': {sufficiency.reason}",
        "likely_cause": None,
        "confidence": "low",
        "contributing_events": [],
        "uncertainty": [
            sufficiency.reason,
            "Bypassed LLM generation to prevent ungrounded parametric hallucination per OWASP AISVS & RINSE guidelines.",
        ],
        "data_gaps": data_gap_summaries,
        "quarantine_card": sufficiency.quarantine_payload,
    }


# Convenient alias
evaluate_evidence_sufficiency = evaluate_sufficiency
