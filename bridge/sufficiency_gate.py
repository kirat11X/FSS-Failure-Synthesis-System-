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
    "disconnect",
    "disconnected",
    "degraded",
    "collision",
}

# Known anomaly event names and status values
ANOMALY_EVENT_NAMES = {"AVOIDANCE", "COLLISION_RISK"}
ANOMALY_STATUS_VALUES = {
    "LIDAR_TIMEOUT",
    "SENSOR_DISCONNECTED",
    "INVALID_SCAN",
    "FAULT",
    "DEGRADED",
    "LEFT_FORMATION",
}


def _extract_queried_entities(query: str) -> set[int]:
    """Extract explicit entity IDs (e.g. drone 474, source 298) from query."""
    entities: set[int] = set()
    # Match 'drone 474', 'drone #474', 'source 298', 'node 474', 'drone-474'
    for match in re.finditer(r"(?:drone|source|host|node|uav)[^\w\d]*([0-9]{1,10})", query, re.IGNORECASE):
        try:
            entities.add(int(match.group(1)))
        except ValueError:
            pass

    # If no prefixed entity found, check standalone 3-4 digit numbers that look like source IDs
    if not entities:
        for match in re.finditer(r"\b([0-9]{3,4})\b", query):
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

    # 3. Check for unrecorded time gap spanning the failure window
    for g in data_gaps:
        kind = g.get("kind", "")
        detail = g.get("detail", "")
        if "spanning requested failure window" in detail.lower() or "unrecorded data gap" in detail.lower():
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

    missing_entities = queried_entities - evidence_sources

    if queried_entities and missing_entities == queried_entities:
        # User asked specifically about entities that are completely absent
        missing_str = ", ".join(map(str, sorted(missing_entities)))
        present_str = ", ".join(map(str, sorted(evidence_sources))) if evidence_sources else "none"
        return (
            False,
            0.0,
            f"Queried entity/entities [{missing_str}] not present in retrieved evidence sources [{present_str}].",
            missing_entities,
        )

    if missing_entities:
        # Partial entity coverage
        coverage = (len(queried_entities) - len(missing_entities)) / len(queried_entities)
        missing_str = ", ".join(map(str, sorted(missing_entities)))
        return (
            True,
            coverage,
            f"Partial entity coverage ({coverage:.0%}); missing [{missing_str}] from retrieved sources.",
            missing_entities,
        )

    return (
        True,
        1.0,
        f"Full entity coverage: all queried entities {sorted(queried_entities) if queried_entities else '(unspecified)'} present in evidence.",
        set(),
    )


def _check_candidate_spans(evidence: dict[str, Any], query: str) -> tuple[bool, float, str, list[str]]:
    """Check whether candidate anomaly spans exist in evidence to support the query.

    Returns (passed, score, details, detected_anomalies).
    """
    query_words = set(re.findall(r"\b[A-Za-z0-9_]+\b", query.lower()))
    is_failure_query = bool(query_words & FAILURE_QUERY_KEYWORDS)

    events = evidence.get("events", [])
    anomalies: list[str] = []

    for ev in events:
        ename = (ev.get("event_name") or ev.get("event_type") or "").upper()
        if ename in ANOMALY_EVENT_NAMES:
            anomalies.append(f"{ename} [{ev.get('event_id')}] on source {ev.get('source_id')}")

        fields = ev.get("fields", {})
        for k, v in fields.items():
            v_str = str(v).upper()
            if v_str in ANOMALY_STATUS_VALUES:
                anomalies.append(f"{ename}.{k}={v_str} [{ev.get('event_id')}] on source {ev.get('source_id')}")

        if ev.get("decode_status") in ("PARTIALLY_DECODED", "UNKNOWN_EVENT_TYPE", "MALFORMED_PAYLOAD"):
            anomalies.append(f"{ev.get('decode_status')} [{ev.get('event_id')}] on source {ev.get('source_id')}")

    if is_failure_query and not anomalies:
        return (
            False,
            0.2,
            "Failure query asked, but 100% of retrieved events are nominal with zero anomaly spans or state deviations.",
            [],
        )

    if anomalies:
        score = min(1.0, 0.5 + 0.1 * len(anomalies))
        return (
            True,
            score,
            f"Detected {len(anomalies)} candidate anomaly span(s): {', '.join(anomalies[:3])}{'...' if len(anomalies) > 3 else ''}.",
            anomalies,
        )

    # Non-failure query or general summary query
    return (
        True,
        0.8,
        "Retrieved events present nominal telemetry matching general status query.",
        [],
    )


def _check_cross_passage_latent(
    evidence: dict[str, Any],
    query: str,
    knowledge: list[dict[str, Any]] | None,
) -> tuple[bool, float, str]:
    """Check cross-passage latent sufficiency (warrant bridge between events and domain knowledge).

    Returns (passed, score, details).
    """
    events = evidence.get("events", [])
    knowledge_list = knowledge or []

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
) -> SufficiencyResult:
    """Pre-generation Sufficiency Gate (Stage 0).

    Gates synthesis before invoking LLM. Returns SufficiencyResult.
    """
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
    span_passed, span_score, span_details, detected_anomalies = _check_candidate_spans(evidence, query)
    checks.append(
        SufficiencyCheckResult(
            name="candidate_span_detection",
            passed=span_passed,
            score=span_score,
            details=span_details,
        )
    )

    # 4. Cross-Passage Latent Sufficiency Check
    lat_passed, lat_score, lat_details = _check_cross_passage_latent(evidence, query, knowledge)
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
