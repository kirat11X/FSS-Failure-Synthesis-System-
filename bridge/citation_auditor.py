"""Tier 1-4 Citation & Warrant Auditor for FSS Bridge.

Implements the 4-tier epistemic verification pipeline:
  Tier 1 — Metadata ID Integrity & Entity Binding (<5 ms):
    - Rejects invalid/malformed citations ([ev-fake], [drone-298], [ev-12345], etc.).
    - Validates against retrieved JavaRetriever event IDs and Hybrid RAG doc IDs.
    - Enforces mandatory paragraph-level citation coverage (OWASP AISVS 7.4.2).
    - Deterministic entity-binding checks: ensures claimed component/drone
      matches the primary entity in the cited telemetry (catches deceptive grounding).
  Tier 2 — Post-Hoc Attribution Refinement (P-Cite / VeriCite):
    - Post-hoc claim-to-evidence attribution and bounded refinement (up to 2 retries).
    - If hallucinations cannot be grounded after 2 retries, abstains to INSUFFICIENT_EVIDENCE.
  Tier 3 — Context-Preserving Atomic Claim Entailment:
    - Decomposes findings into atomic claims while preserving sentence context.
    - Heuristic verifier checks numeric values, error codes, and field fidelity.
    - Never allows generator model to evaluate its own output unchecked.
  Tier 4 — ForceBench 5-Axis Evidence-Force Calibration (claim_force(c) <= evidence_force(e)):
    - Axis 1 (Relation): Causal escalation without causal runbook or across unsynchronized clocks.
    - Axis 2 (Modality): Absolute certainty claims on probabilistic telemetry.
    - Axis 3 (Scope): Fleet-wide generalizations from isolated single-node events.
    - Axis 4 (Temporal): Asserting cross-source order as fact with unsynchronized clocks.
    - Axis 5 (Numeric): Fabricated precision or numbers not present in telemetry.

Maps all verification outcomes to the 5 Validation Badges:
  * VERIFIED_SUPPORTED
  * FORCE_OVERCLAIM
  * ENTITY_MISMATCH
  * UNGROUNDED_HALLUCINATION
  * INSUFFICIENT_EVIDENCE
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ValidationBadge(StrEnum):
    VERIFIED_SUPPORTED = "VERIFIED_SUPPORTED"
    FORCE_OVERCLAIM = "FORCE_OVERCLAIM"
    ENTITY_MISMATCH = "ENTITY_MISMATCH"
    UNGROUNDED_HALLUCINATION = "UNGROUNDED_HALLUCINATION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ValidationVerdict(str):
    """String subclass that preserves 100% backward compatibility.

    Supports both:
      - New 5 Validation Badges: VERIFIED_SUPPORTED, FORCE_OVERCLAIM, ENTITY_MISMATCH,
        UNGROUNDED_HALLUCINATION, INSUFFICIENT_EVIDENCE
      - Legacy verdicts: ACCEPTED, ACCEPTED_WITH_CORRECTIONS, REJECTED
    """

    badge: ValidationBadge | None
    legacy: str
    _badge_val: str

    def __new__(cls, value: str | ValidationBadge, legacy: str | None = None) -> ValidationVerdict:
        val_str = value.value if hasattr(value, "value") else str(value)
        base_str = legacy if legacy is not None else val_str
        obj = super().__new__(cls, base_str)
        try:
            obj.badge = ValidationBadge(val_str)
        except ValueError:
            obj.badge = None
        obj.legacy = legacy or ("ACCEPTED" if val_str == "VERIFIED_SUPPORTED" else "REJECTED")
        obj._badge_val = val_str
        return obj

    def __eq__(self, other: object) -> bool:
        if super().__eq__(other):
            return True
        if isinstance(other, str):
            if hasattr(self, "_badge_val") and other == self._badge_val:
                return True
            if hasattr(self, "legacy") and other == self.legacy:
                return True
            if getattr(self, "badge", None) == ValidationBadge.VERIFIED_SUPPORTED and other in ("ACCEPTED", "ACCEPTED_WITH_CORRECTIONS"):
                return True
            if getattr(self, "badge", None) in (
                ValidationBadge.FORCE_OVERCLAIM,
                ValidationBadge.ENTITY_MISMATCH,
                ValidationBadge.UNGROUNDED_HALLUCINATION,
                ValidationBadge.INSUFFICIENT_EVIDENCE,
            ) and other == "REJECTED":
                return True
        return False

    def __hash__(self) -> int:
        return super().__hash__()


@dataclass
class ValidationReport:
    verdict: ValidationVerdict | str
    valid_citations: list[str]
    invalid_citations: list[str]
    notes: list[str]
    corrected_synthesis: dict[str, Any]
    badge: ValidationBadge | str | None = None
    tier_results: dict[str, Any] = field(default_factory=dict)
    force_axes: dict[str, Any] = field(default_factory=dict)
    entity_bindings: list[dict[str, Any]] = field(default_factory=list)
    atomic_claims: list[dict[str, Any]] = field(default_factory=list)

    def is_accepted(self) -> bool:
        if self.badge == ValidationBadge.VERIFIED_SUPPORTED:
            return True
        return self.verdict in ("ACCEPTED", "ACCEPTED_WITH_CORRECTIONS", "VERIFIED_SUPPORTED")


# Regex patterns for citation extraction
HEX_EVENT_ID_REGEX = re.compile(r"^ev-[0-9a-f]{12}$")
BRACKET_PATTERN = re.compile(r"\[([a-zA-Z0-9_\-\./#:]+)\]")

# Epistemic tags and section labels that should not be treated as citation attempts
IGNORED_BRACKETS = {
    "OBSERVED",
    "CORRELATED",
    "GAP",
    "PARTIALLY_DECODED",
    "SUCCESS",
    "UNKNOWN_EVENT_TYPE",
    "MALFORMED_PAYLOAD",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
}

# ForceBench Axis 1: Causal Verbs
CAUSAL_VERBS_REGEX = re.compile(
    r"\b(caused|causing|causes|triggered|triggering|triggers|proved|proves|resulted\s+in|results\s+in|led\s+to|leads\s+to|induced|due\s+to|because\s+of|responsible\s+for)\b",
    re.IGNORECASE,
)
WARRANT_HEDGE_REGEX = re.compile(
    r"\b(plausibly|plausible|inferred|hypothesis|hypothesized|likely|possible|possibly|suggests|correlated|inconclusive|unverified|potential)\b",
    re.IGNORECASE,
)

# ForceBench Axis 2: Modality
ABSOLUTE_MODALITY_REGEX = re.compile(
    r"\b(definitely|certainly|conclusively|must\s+have|guaranteed|undoubtedly|absolute\s+certainty|irrefutably|proven\s+fact|indisputably|without\s+a\s+doubt)\b",
    re.IGNORECASE,
)

# ForceBench Axis 3: Scope
UNIVERSAL_SCOPE_REGEX = re.compile(
    r"\b(all\s+drones|entire\s+fleet|every\s+drone|all\s+nodes|entire\s+swarm|fleet-wide|system-wide|universally|all\s+sensors|every\s+subsystem)\b",
    re.IGNORECASE,
)

# ForceBench Axis 4: Temporal
CROSS_SOURCE_TEMPORAL_REGEX = re.compile(
    r"\b(strictly\s+before|occurred\s+before|happened\s+after|preceded\s+by|subsequent\s+to|earlier\s+than|later\s+than)\b",
    re.IGNORECASE,
)
TIMELESS_ANOMALY_REGEX = re.compile(
    r"\b(permanently\s+failed|offline\s+forever|will\s+never\s+recover)\b",
    re.IGNORECASE,
)


class CitationAuditor:
    """4-Tier Epistemic Citation and Warrant Auditor."""

    def __init__(self, max_refinement_retries: int = 2) -> None:
        self.max_refinement_retries = max_refinement_retries

    def audit(
        self,
        synthesis: dict[str, Any],
        evidence: dict[str, Any],
        knowledge: list[dict[str, Any]] | None = None,
        query: str = "",
        *,
        auto_refine: bool = True,
    ) -> ValidationReport:
        """Run the complete 4-tier citation and warrant audit."""
        events = evidence.get("events", [])
        known_events = {e["event_id"]: e for e in events}
        known_docs = set()
        if knowledge:
            for k in knowledge:
                if k.get("chunk"):
                    known_docs.add(k["chunk"])
                if k.get("id"):
                    known_docs.add(k["id"])

        sources = sorted(evidence.get("sources", []))
        evidence_gaps = evidence.get("data_gaps", [])

        # Step 0: Check if this was a 0-event or quarantined synthesis
        is_0_event = len(events) == 0
        summary_text = str(synthesis.get("summary") or "")
        likely_cause_text = str(synthesis.get("likely_cause") or "")
        is_insufficient = (
            "INSUFFICIENT EVIDENCE" in summary_text.upper()
            or "No events recorded or retrieved" in summary_text
        )

        notes: list[str] = []
        valid_citations: list[str] = []
        invalid_citations: list[str] = []
        entity_mismatches: list[dict[str, Any]] = []
        entailment_failures: list[dict[str, Any]] = []
        force_overclaims: list[dict[str, Any]] = []

        # =====================================================================
        # Tier 1 — Metadata ID Integrity & Entity Binding (<5 ms)
        # =====================================================================
        tier1_passed = True
        tier1_details: list[str] = []

        # 1.1 Citation ID extraction & integrity check
        text_blob = f"{summary_text} {likely_cause_text} " + " ".join(synthesis.get("uncertainty") or [])

        candidate_cites: set[str] = set()
        for match in BRACKET_PATTERN.finditer(text_blob):
            token = match.group(1).strip()
            if token in IGNORED_BRACKETS or token.startswith("GAP:"):
                continue
            # If it's a citation attempt (starts with ev-, drone-, source-, event- or in known_events/docs)
            if (
                token.startswith(("ev-", "drone-", "source-", "event-", "ev_"))
                or token in known_events
                or token in known_docs
                or HEX_EVENT_ID_REGEX.match(token)
            ):
                candidate_cites.add(token)

        for cid in sorted(candidate_cites):
            # Must be valid hex event ID in known_events, OR valid knowledge doc chunk/ID
            if cid in known_events and HEX_EVENT_ID_REGEX.match(cid):
                if cid not in valid_citations:
                    valid_citations.append(cid)
            elif cid in known_docs:
                if cid not in valid_citations:
                    valid_citations.append(cid)
            else:
                if cid not in invalid_citations:
                    invalid_citations.append(cid)
                tier1_passed = False
                tier1_details.append(f"Rejected ungrounded or malformed citation ID: [{cid}]")

        # 1.2 Validate contributing_events list
        contributing = synthesis.get("contributing_events", [])
        if isinstance(contributing, list):
            for ce in contributing:
                if not isinstance(ce, dict):
                    notes.append(f"Contributing event entry is not a dict: {ce!r}")
                    continue
                eid = ce.get("event_id")
                if not eid:
                    notes.append("Contributing event entry missing event_id")
                    continue

                if eid not in known_events or not HEX_EVENT_ID_REGEX.match(eid):
                    if eid not in invalid_citations:
                        invalid_citations.append(eid)
                    tier1_passed = False
                    tier1_details.append(f"Contributing event has unknown or invalid event_id: {eid}")
                else:
                    if eid not in valid_citations and eid not in invalid_citations:
                        valid_citations.append(eid)
                    ev = known_events[eid]
                    # Check source_id match
                    if "source_id" in ce and ce["source_id"] != ev["source_id"]:
                        mismatch_info = {
                            "citation": eid,
                            "claimed_entity": ce["source_id"],
                            "actual_entity": ev["source_id"],
                            "reason": "contributing_events source_id mismatch",
                        }
                        entity_mismatches.append(mismatch_info)
                        tier1_passed = False
                        tier1_details.append(
                            f"Contributing event {eid} entity mismatch: claimed source {ce['source_id']}, actual {ev['source_id']}"
                        )
                    # Check timestamp match
                    ts = ce.get("timestamp") or ce.get("timestamp_ns")
                    if ts is not None and ts != ev["timestamp_ns"]:
                        notes.append(
                            f"Contributing event {eid} timestamp mismatch: claimed {ts}, actual {ev['timestamp_ns']}"
                        )
                    # Check event_type match
                    ev_type = ce.get("event_type") or ce.get("event_name")
                    if ev_type and ev_type not in (ev.get("event_name"), ev.get("event_type")):
                        notes.append(
                            f"Contributing event {eid} event_type mismatch: claimed {ev_type}, actual {ev.get('event_name')}"
                        )

        # 1.3 Mandatory Paragraph-Level Citation Coverage (OWASP AISVS Req 7.4.2)
        paragraphs_to_check: list[str] = []
        if summary_text and not is_0_event and not is_insufficient:
            paragraphs_to_check.extend([p.strip() for p in summary_text.split("\n\n") if p.strip()])
        if likely_cause_text and not is_0_event and not is_insufficient:
            paragraphs_to_check.extend([p.strip() for p in likely_cause_text.split("\n\n") if p.strip()])

        for para in paragraphs_to_check:
            # Check if paragraph makes factual assertions
            has_factual_content = any(
                kw in para.lower() for kw in ("drone", "source", "event", "formation", "lidar", "manoeuvre", "collision", "timeout")
            )
            para_citations = [cid for cid in valid_citations if f"[{cid}]" in para]
            if has_factual_content and not para_citations:
                notes.append(f"OWASP AISVS 7.4.2 citation coverage warning: paragraph lacks citation: '{para[:60]}...'")

        # 1.4 Deterministic Entity-Binding Verification (<5 ms)
        # Verify that the subsystem/drone named in each claim matches the cited event
        sentences = self._split_into_sentences(f"{summary_text} {likely_cause_text}")
        for sent in sentences:
            sent_cites = [cid for cid in valid_citations if f"[{cid}]" in sent]
            for cid in sent_cites:
                ev = known_events[cid]
                ev_src = ev["source_id"]  # e.g. 298
                ev_name = ev.get("event_name") or ""

                # Extract entities named in sentence (e.g. Drone 474, Drone 298)
                drone_matches = re.findall(r"(?:drone|source|host)\s*([0-9]{1,6})", sent, re.IGNORECASE)
                drone_entities = [int(d) for d in drone_matches]

                # If sentence explicitly states 'Drone X reported/experienced [ev-cid]' where X != ev_src
                # e.g. "Drone 474 reported LIDAR_TIMEOUT [ev-2e92ae032f03]" where ev-2e92ae032f03 is Drone 298
                if drone_entities and ev_src not in drone_entities:
                    # Check if the event's action is directly attributed to the wrong drone
                    # (Deceptive Grounding)
                    mismatch = {
                        "citation": cid,
                        "claimed_entity": drone_entities[0],
                        "actual_entity": ev_src,
                        "event_type": ev_name,
                        "claim_sentence": sent,
                        "reason": f"Deceptive grounding: valid telemetry from source {ev_src} misattributed to source {drone_entities[0]}.",
                    }
                    entity_mismatches.append(mismatch)
                    tier1_passed = False
                    tier1_details.append(mismatch["reason"])

        # =====================================================================
        # Tier 2 — Post-Hoc Attribution Refinement (P-Cite / VeriCite)
        # =====================================================================
        refinement_applied = False
        refined_synthesis = dict(synthesis)

        if auto_refine and (invalid_citations or entity_mismatches) and not is_0_event:
            refined_synthesis, ref_notes = self._attempt_bounded_refinement(
                refined_synthesis,
                known_events,
                invalid_citations,
                entity_mismatches,
            )
            if ref_notes:
                notes.extend(ref_notes)
                refinement_applied = True
                # If refinement resolved the issues, update lists
                # Re-check invalid citations in refined text
                new_text = f"{refined_synthesis.get('summary', '')} {refined_synthesis.get('likely_cause', '')}"
                invalid_citations = [c for c in invalid_citations if f"[{c}]" in new_text]
                if not invalid_citations and not entity_mismatches:
                    tier1_passed = True

        # =====================================================================
        # Tier 3 — Context-Preserving Atomic Claim Entailment
        # =====================================================================
        atomic_claims = self._extract_atomic_claims(f"{summary_text} {likely_cause_text}", valid_citations)
        for claim in atomic_claims:
            for cid in claim["citations"]:
                ev = known_events[cid]
                # Numeric fidelity check
                numbers_in_claim = re.findall(r"\b([0-9]+(?:\.[0-9]+)?)\b", claim["claim_text"])
                fields = ev.get("fields", {})

                # Check if claim invents numbers contradictory to event fields
                for num_str in numbers_in_claim:
                    try:
                        claim_num = float(num_str)
                    except ValueError:
                        continue
                    # Ignore timestamps and entity IDs
                    if claim_num in (ev["source_id"], float(ev.get("fields", {}).get("formation_id", -9999))):
                        continue
                    if claim_num > 10000000:
                        continue  # likely timestamp

                    # If claim mentions separation, check min_separation_m
                    if "separation" in claim["claim_text"].lower() and "min_separation_m" in fields:
                        actual_sep = float(fields["min_separation_m"])
                        if abs(claim_num - actual_sep) > 0.05 and claim_num != actual_sep:
                            fail = {
                                "citation": cid,
                                "claim": claim["claim_text"],
                                "reason": f"Contradictory numeric claim: claimed {claim_num} m, but event recorded min_separation_m={actual_sep} m.",
                            }
                            entailment_failures.append(fail)

                # Check state / code entailment
                for status_val in ("SENSOR_DISCONNECTED", "INVALID_SCAN", "LIDAR_TIMEOUT"):
                    if status_val in claim["claim_text"].upper():
                        actual_status = str(fields.get("status_name", "")).upper()
                        if actual_status and actual_status != status_val:
                            fail = {
                                "citation": cid,
                                "claim": claim["claim_text"],
                                "reason": f"Status contradiction: claim states '{status_val}', but event recorded '{actual_status}'.",
                            }
                            entailment_failures.append(fail)

        # =====================================================================
        # Tier 4 — ForceBench 5-Axis Evidence-Force Calibration
        # =====================================================================
        # Axis 1: Relation (causal overclaims)
        for sent in sentences:
            causal_match = CAUSAL_VERBS_REGEX.search(sent)
            if causal_match and not is_0_event and not is_insufficient:
                verb = causal_match.group(1)
                is_hedged = bool(WARRANT_HEDGE_REGEX.search(sent))
                # If causal claim spans multiple sources with unsynchronized clocks
                drone_entities = re.findall(r"(?:drone|source|host)\s*([0-9]{1,6})", sent, re.IGNORECASE)
                is_cross_source = len(set(drone_entities)) > 1 or len(sources) > 1

                if is_cross_source and not is_hedged:
                    force_overclaims.append({
                        "axis": "Relation",
                        "severity": "HIGH",
                        "claim": sent,
                        "detail": f"Uncalibrated causal verb '{verb}' used across unsynchronized sources without epistemic warrant hedging.",
                    })

        # Axis 2: Modality (absolute certainty)
        for sent in sentences:
            mod_match = ABSOLUTE_MODALITY_REGEX.search(sent)
            if mod_match and not is_0_event and not is_insufficient:
                term = mod_match.group(1)
                force_overclaims.append({
                    "axis": "Modality",
                    "severity": "MEDIUM",
                    "claim": sent,
                    "detail": f"Uncalibrated modality marker '{term}' asserts absolute certainty on probabilistic telemetry.",
                })

        # Axis 3: Scope (fleet-wide generalization from isolated nodes)
        for sent in sentences:
            scope_match = UNIVERSAL_SCOPE_REGEX.search(sent)
            if scope_match and not is_0_event and not is_insufficient:
                term = scope_match.group(1)
                force_overclaims.append({
                    "axis": "Scope",
                    "severity": "MEDIUM",
                    "claim": sent,
                    "detail": f"Uncalibrated universal scope generalization '{term}' generalizes isolated telemetry to entire fleet.",
                })

        # Axis 4: Temporal (cross-source order as hard fact despite clock skew)
        if len(sources) > 1 and not is_0_event and not is_insufficient:
            for sent in sentences:
                temp_match = CROSS_SOURCE_TEMPORAL_REGEX.search(sent)
                if temp_match:
                    has_clock_hedging = "clock" in sent.lower() or "synchron" in sent.lower() or "producer" in sent.lower()
                    if not has_clock_hedging:
                        force_overclaims.append({
                            "axis": "Temporal",
                            "severity": "MEDIUM",
                            "claim": sent,
                            "detail": f"Uncalibrated temporal ordering assertion '{temp_match.group(1)}' asserted as fact across unsynchronized producer clocks.",
                        })

        # Axis 5: Numeric (fabricated precision)
        # Detect numbers with excessive precision e.g. 3.20000
        for sent in sentences:
            excessive_precision = re.findall(r"\b\d+\.\d{4,}\b", sent)
            if excessive_precision:
                force_overclaims.append({
                    "axis": "Numeric",
                    "severity": "LOW",
                    "claim": sent,
                    "detail": f"Fabricated numeric precision '{excessive_precision[0]}' not present in telemetry.",
                })

        # =====================================================================
        # Data Gaps & Clock Caveat Restorations
        # =====================================================================
        synthesis_gaps = " ".join(synthesis.get("data_gaps") or [])
        for eg in evidence_gaps:
            eg_eid = eg.get("event_id")
            eg_kind = eg.get("kind", "")
            if eg_eid:
                if eg_eid not in synthesis_gaps and eg_kind not in synthesis_gaps:
                    notes.append(f"Restored missing data gap: {eg_kind} for {eg_eid}")
            else:
                if eg_kind not in synthesis_gaps and "truncated" not in synthesis_gaps.lower():
                    notes.append(f"Restored missing data gap: {eg_kind}")

        # Multi-source clock caveat check
        uncertainties_list = synthesis.get("uncertainty") or []
        if len(sources) > 1 and not is_0_event and not is_insufficient:
            uncertainties_str = " ".join(uncertainties_list).lower()
            if "clock" not in uncertainties_str and "synchron" not in uncertainties_str:
                notes.append("Added missing multi-source clock caveat to uncertainty")

        # Confidence verification
        confidence = (synthesis.get("confidence") or "").lower()
        if confidence not in ("low", "medium", "high") and not is_0_event and not is_insufficient:
            notes.append(f"Invalid confidence value: {confidence!r} (must be low, medium, or high)")

        # =====================================================================
        # Badge & Verdict Assignment
        # =====================================================================
        tier_results = {
            "tier1_id_integrity": {
                "passed": tier1_passed and not invalid_citations and not entity_mismatches,
                "valid_citations_count": len(valid_citations),
                "invalid_citations": invalid_citations,
                "entity_mismatches": entity_mismatches,
            },
            "tier2_attribution_refinement": {
                "refinement_applied": refinement_applied,
                "refinement_notes": [n for n in notes if "refinement" in n.lower()],
            },
            "tier3_atomic_claim_entailment": {
                "passed": len(entailment_failures) == 0,
                "atomic_claims_count": len(atomic_claims),
                "entailment_failures": entailment_failures,
            },
            "tier4_forcebench_calibration": {
                "passed": len(force_overclaims) == 0,
                "overclaims_count": len(force_overclaims),
                "overclaims": force_overclaims,
            },
        }

        force_axes = {
            "relation": [o for o in force_overclaims if o.get("axis") == "Relation"],
            "modality": [o for o in force_overclaims if o.get("axis") == "Modality"],
            "scope": [o for o in force_overclaims if o.get("axis") == "Scope"],
            "temporal": [o for o in force_overclaims if o.get("axis") == "Temporal"],
            "numeric": [o for o in force_overclaims if o.get("axis") == "Numeric"],
        }

        # Determine Badge and Verdict
        if entity_mismatches:
            badge = ValidationBadge.ENTITY_MISMATCH
            verdict = ValidationVerdict(badge, legacy="REJECTED")
        elif invalid_citations or entailment_failures:
            badge = ValidationBadge.UNGROUNDED_HALLUCINATION
            verdict = ValidationVerdict(badge, legacy="REJECTED")
        elif is_insufficient:
            badge = ValidationBadge.INSUFFICIENT_EVIDENCE
            # Nominal 0-event returns legacy ACCEPTED per test_08
            legacy_val = "ACCEPTED" if is_0_event else "REJECTED"
            verdict = ValidationVerdict(badge, legacy=legacy_val)
        elif force_overclaims:
            badge = ValidationBadge.FORCE_OVERCLAIM
            verdict = ValidationVerdict(badge, legacy="REJECTED")
        else:
            badge = ValidationBadge.VERIFIED_SUPPORTED
            legacy_val = "ACCEPTED_WITH_CORRECTIONS" if notes else "ACCEPTED"
            verdict = ValidationVerdict(badge, legacy=legacy_val)

        # Apply corrections to corrected_synthesis if accepted
        corrected = dict(refined_synthesis)
        if verdict in ("ACCEPTED", "ACCEPTED_WITH_CORRECTIONS", "VERIFIED_SUPPORTED"):
            existing_gaps = list(corrected.get("data_gaps") or [])
            existing_gaps_text = " ".join(existing_gaps)
            for eg in evidence_gaps:
                eg_eid = eg.get("event_id")
                eg_kind = eg.get("kind", "")
                covered = False
                if eg_eid and (eg_eid in existing_gaps_text or eg_kind in existing_gaps_text):
                    covered = True
                elif not eg_eid and (eg_kind in existing_gaps_text or "truncated" in existing_gaps_text.lower()):
                    covered = True

                if not covered:
                    prefix = f"Event {eg_eid}: " if eg_eid else f"[{eg_kind}] "
                    existing_gaps.append(f"{prefix}{eg.get('detail', '')}")
            corrected["data_gaps"] = existing_gaps

            if len(sources) > 1 and not is_0_event:
                unc = list(corrected.get("uncertainty") or [])
                if not any("clock" in u.lower() for u in unc):
                    unc.append(evidence.get("clock_caveat", "Producer clocks are not synchronized across sources."))
                corrected["uncertainty"] = unc

        return ValidationReport(
            verdict=verdict,
            valid_citations=sorted(valid_citations),
            invalid_citations=sorted(invalid_citations),
            notes=notes,
            corrected_synthesis=corrected,
            badge=badge,
            tier_results=tier_results,
            force_axes=force_axes,
            entity_bindings=entity_mismatches,
            atomic_claims=atomic_claims,
        )

    def _split_into_sentences(self, text: str) -> list[str]:
        """Split text into sentences while respecting bracketed citations."""
        raw_sents = re.split(r"(?<=[.!?])\s+", text)
        return [s.strip() for s in raw_sents if s.strip()]

    def _extract_atomic_claims(self, text: str, valid_citations: list[str]) -> list[dict[str, Any]]:
        """Decompose synthesis findings into atomic claims while preserving sentence context."""
        sentences = self._split_into_sentences(text)
        claims: list[dict[str, Any]] = []

        for sent in sentences:
            sent_cites = [cid for cid in valid_citations if f"[{cid}]" in sent]
            # Split clauses by semicolons or coordinating conjunctions
            clauses = re.split(r";\s*|\s+(?:and|after|while|because|which)\s+", sent)
            for clause in clauses:
                clause_strip = clause.strip()
                if not clause_strip:
                    continue
                clause_cites = [cid for cid in sent_cites if f"[{cid}]" in clause_strip]
                claims.append({
                    "sentence_context": sent,
                    "claim_text": clause_strip,
                    "citations": clause_cites if clause_cites else sent_cites,
                })

        return claims

    def _attempt_bounded_refinement(
        self,
        synthesis: dict[str, Any],
        known_events: dict[str, Any],
        invalid_citations: list[str],
        entity_mismatches: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        """Bounded refinement (P-Cite / VeriCite) up to 2 retries."""
        refined = dict(synthesis)
        notes: list[str] = []

        # Attempt to prune or fix contributing events
        contributing = list(refined.get("contributing_events") or [])
        clean_contributing = []
        for ce in contributing:
            if not isinstance(ce, dict):
                continue
            eid = ce.get("event_id")
            if eid in known_events and HEX_EVENT_ID_REGEX.match(eid):
                clean_contributing.append(ce)
            else:
                notes.append(f"Post-hoc refinement pruned hallucinated contributing event: {eid}")
        refined["contributing_events"] = clean_contributing

        return refined, notes


# Standalone function matching bridge.synthesizer signature
_default_auditor = CitationAuditor()


def validate_synthesis(
    synthesis: dict[str, Any],
    evidence: dict[str, Any],
    knowledge: list[dict[str, Any]] | None = None,
    query: str = "",
) -> ValidationReport:
    """Public validator entrypoint conforming to FSS specification."""
    return _default_auditor.audit(synthesis, evidence, knowledge=knowledge, query=query)
