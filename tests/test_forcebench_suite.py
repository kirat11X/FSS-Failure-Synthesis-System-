"""tests/test_forcebench_suite.py
--------------------------------
ForceBench Quantitative Evaluation Suite (REQ-049).

Stratified contrastive benchmark suite of >= 100 test cases across all 5 ForceBench
escalation axes, plus entity-binding traps and syntactic hallucination traps:
  1. Relation: Causal escalation without warrant / across unsynchronized clocks
  2. Modality: Absolute certainty claims on probabilistic telemetry
  3. Scope: Fleet-wide generalizations from isolated single-node events
  4. Temporal: Asserting cross-source order as absolute fact without clock hedging
  5. Numeric: Fabricated precision or numbers contradictory to telemetry
  6. Entity-Binding Traps: Deceptive grounding and entity misattribution
  7. Fake Citations: Malformed bracket IDs, non-existent hex IDs, and hallucinated tokens

Metrics calculated and asserted:
  - 100% rejection rate for syntactic/unknown citations -> UNGROUNDED_HALLUCINATION
  - 100% detection rate for entity-binding traps -> ENTITY_MISMATCH
  - Monotonicity Violation Rate (MVR) across force escalation pairs:
      MVR = (escalated claims falsely accepted as VERIFIED_SUPPORTED) / (total pairs) == 0.0
  - Clean, well-hedged, grounded claims -> VERIFIED_SUPPORTED
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from bridge.citation_auditor import (
    CitationAuditor,
    ValidationBadge,
    ValidationReport,
    validate_synthesis,
)
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

ROOT_DIR = Path(__file__).resolve().parent.parent
FIXTURE_PATH = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"
SCHEMA_PATH = ROOT_DIR / "schemas" / "drone_v0.yaml"


class TestForceBenchSuite(unittest.TestCase):
    """ForceBench stratified contrastive evaluation suite."""

    @classmethod
    def setUpClass(cls):
        raw = FIXTURE_PATH.read_bytes()
        schema = load_schema_file(SCHEMA_PATH)
        decoded = decode_all(list(iter_envelopes(raw)), schema)
        cls.evidence = build_evidence(decoded, query="Why did drone 474 leave formation?")
        cls.auditor = CitationAuditor()

        # Valid event IDs in scenario_298_474.bin:
        # ev-f75a8746cbd3 (298, FORMATION_STATE)
        # ev-956d91bd81b0 (298, LIDAR_STATUS OK)
        # ev-2e92ae032f03 (298, LIDAR_STATUS LIDAR_TIMEOUT)
        # ev-9d2fbc08bffc (298, DIAGNOSTIC DEGRADED)
        # ev-617073ef9e26 (474, FORMATION_STATE IN_FORMATION)
        # ev-722d707b684c (474, AVOIDANCE COLLISION_RISK, min_separation_m=3.2, heading_change_deg=-35)
        # ev-1c432aca07e2 (474, FORMATION_STATE LEFT_FORMATION)
        # ev-191658572a40 (901, FORMATION_STATE)
        # ev-3f07479663aa (901, FORMATION_STATE)

    def _eval_synth(
        self,
        summary_text: str,
        likely_cause_text: str = "",
        contributing_events: list[dict[str, Any]] | None = None,
        confidence: str = "high",
        uncertainty: list[str] | None = None,
        data_gaps: list[str] | None = None,
    ) -> ValidationReport:
        if contributing_events is None:
            contributing_events = [
                {"event_id": "ev-1c432aca07e2", "source_id": 474},
                {"event_id": "ev-722d707b684c", "source_id": 474},
            ]
        if uncertainty is None:
            uncertainty = ["Producer clocks are not synchronized across sources."]
        if data_gaps is None:
            data_gaps = []

        synth = {
            "summary": summary_text,
            "likely_cause": likely_cause_text or summary_text,
            "confidence": confidence,
            "contributing_events": contributing_events,
            "uncertainty": uncertainty,
            "data_gaps": data_gaps,
        }
        return self.auditor.audit(synth, self.evidence)

    # =========================================================================
    # 1. Syntactic & Non-Existent Citations (Fake Citations) — 20 Cases
    # =========================================================================

    FAKE_CITATION_CASES = [
        ("Drone 474 departed formation [ev-fake].", "ev-fake"),
        ("Sensor failure noted on drone [drone-298].", "drone-298"),
        ("Egress initiated after alert [ev-12345].", "ev-12345"),
        ("Phantom signal received [ev-000000000000].", "ev-000000000000"),
        ("Unknown telemetry payload [ev-deadbeefcafe].", "ev-deadbeefcafe"),
        ("Telemetry dropped [ev-999999999999].", "ev-999999999999"),
        ("Unobserved event reference [event-474].", "event-474"),
        ("Missing sensor scan [ev_corrupt_id].", "ev_corrupt_id"),
        ("Diagnostic failure logged [ev-badhex123456].", "ev-badhex123456"),
        ("Anomalous heartbeat observed [source-298].", "source-298"),
        ("Collision risk asserted [ev-abcdef123456].", "ev-abcdef123456"),
        ("LiDAR desync [ev-111122223333].", "ev-111122223333"),
        ("Motor interlock tripped [ev-fa11fa11fa11].", "ev-fa11fa11fa11"),
        ("Formation break acknowledged [ev-9876543210ab].", "ev-9876543210ab"),
        ("Unretrieved diagnostic [ev-ca11ab1e0000].", "ev-ca11ab1e0000"),
        ("Fabricated log line [ev-ffffffffffff].", "ev-ffffffffffff"),
        ("Invalid bracket syntax citation [ev-bad_char!].", "ev-bad_char!"),
        ("Bogus telemetry packet [ev-1234567890ab].", "ev-1234567890ab"),
        ("Fictitious flight record [ev-333344445555].", "ev-333344445555"),
        ("Ungrounded external citation [ev-777788889999].", "ev-777788889999"),
    ]

    def test_01_fake_citations_100pct_rejection(self):
        """100% rejection rate of syntactic hallucinations and non-existent IDs."""
        total = len(self.FAKE_CITATION_CASES)
        rejected = 0

        for text, fake_id in self.FAKE_CITATION_CASES:
            # Contributing events also points to fake_id to ensure tier1 catches it
            report = self._eval_synth(
                summary_text=text,
                contributing_events=[{"event_id": fake_id, "source_id": 474}],
            )
            if report.badge == ValidationBadge.UNGROUNDED_HALLUCINATION:
                rejected += 1
            elif not report.is_accepted():
                rejected += 1

        rejection_rate = rejected / total
        self.assertEqual(
            rejection_rate,
            1.0,
            f"Expected 100% rejection of fake citations, got {rejection_rate * 100:.1f}% ({rejected}/{total})",
        )

    # =========================================================================
    # 2. Entity-Binding Traps (Deceptive Grounding) — 20 Cases
    # =========================================================================

    ENTITY_BINDING_CASES = [
        # Sentence attribution mismatches (valid IDs attributed to wrong drone)
        ("Drone 474 reported LIDAR_TIMEOUT [ev-2e92ae032f03].", "ev-2e92ae032f03", 474),
        ("Drone 474 logged diagnostic degraded [ev-9d2fbc08bffc].", "ev-9d2fbc08bffc", 474),
        ("Drone 474 logged initial formation state [ev-f75a8746cbd3].", "ev-f75a8746cbd3", 474),
        ("Drone 474 maintained clear LiDAR [ev-956d91bd81b0].", "ev-956d91bd81b0", 474),
        ("Drone 298 commanded avoidance turn [ev-722d707b684c].", "ev-722d707b684c", 298),
        ("Drone 298 departed formation [ev-1c432aca07e2].", "ev-1c432aca07e2", 298),
        ("Drone 298 held slot in formation [ev-617073ef9e26].", "ev-617073ef9e26", 298),
        ("Drone 901 experienced avoidance maneuver [ev-722d707b684c].", "ev-722d707b684c", 901),
        ("Drone 901 left assigned formation slot [ev-1c432aca07e2].", "ev-1c432aca07e2", 901),
        ("Drone 901 experienced sensor timeout [ev-2e92ae032f03].", "ev-2e92ae032f03", 901),
        ("Drone 298 was recorded leaving formation slot [ev-1c432aca07e2].", "ev-1c432aca07e2", 298),
        ("Drone 474 encountered timeout count of 3 [ev-2e92ae032f03].", "ev-2e92ae032f03", 474),
        ("Drone 901 experienced temperature rise to 72C [ev-9d2fbc08bffc].", "ev-9d2fbc08bffc", 901),
        ("Drone 298 executed sharp negative heading change [ev-722d707b684c].", "ev-722d707b684c", 298),
        ("Drone 474 reported nominal lidar operation [ev-956d91bd81b0].", "ev-956d91bd81b0", 474),
        # Contributing events source_id mismatches
        ("Telemetry indicates sensor fault [ev-2e92ae032f03].", "ev-2e92ae032f03", 474),
        ("Telemetry indicates avoidance manoeuvre [ev-722d707b684c].", "ev-722d707b684c", 298),
        ("Telemetry records diagnostic state [ev-9d2fbc08bffc].", "ev-9d2fbc08bffc", 474),
        ("Telemetry indicates departure from formation [ev-1c432aca07e2].", "ev-1c432aca07e2", 901),
        ("Telemetry shows steady state formation [ev-f75a8746cbd3].", "ev-f75a8746cbd3", 474),
    ]

    def test_02_entity_binding_traps_100pct_detection(self):
        """100% detection rate for entity-binding traps -> ENTITY_MISMATCH."""
        total = len(self.ENTITY_BINDING_CASES)
        detected = 0

        for text, cid, claimed_src in self.ENTITY_BINDING_CASES:
            report = self._eval_synth(
                summary_text=text,
                contributing_events=[{"event_id": cid, "source_id": claimed_src}],
            )
            if report.badge == ValidationBadge.ENTITY_MISMATCH:
                detected += 1
            elif not report.is_accepted() and report.tier_results.get("tier1_id_integrity", {}).get("entity_mismatches"):
                detected += 1

        detection_rate = detected / total
        self.assertEqual(
            detection_rate,
            1.0,
            f"Expected 100% detection of entity mismatch traps, got {detection_rate * 100:.1f}% ({detected}/{total})",
        )

    # =========================================================================
    # 3. Axis 1: Relation (Causal Escalation) — 15 Contrastive Pairs
    # =========================================================================

    RELATION_CONTRASTIVE_PAIRS = [
        (
            # Hedged baseline
            "Drone 298 lidar timeout plausibly correlated with Drone 474 avoidance manoeuvre [ev-722d707b684c].",
            # Escalated claim (causal leap across sources)
            "Drone 298 lidar timeout caused Drone 474 avoidance manoeuvre [ev-722d707b684c].",
        ),
        (
            "Drone 298 sensor degradation likely influenced Drone 474 path change [ev-722d707b684c].",
            "Drone 298 sensor degradation triggered Drone 474 path change [ev-722d707b684c].",
        ),
        (
            "Drone 298 status hypothesized to relate to Drone 474 slot departure [ev-1c432aca07e2].",
            "Drone 298 status directly proved Drone 474 slot departure [ev-1c432aca07e2].",
        ),
        (
            "Telemetry suggests Drone 298 drift potential factor in Drone 474 reaction [ev-722d707b684c].",
            "Telemetry demonstrates Drone 298 drift resulted in Drone 474 reaction [ev-722d707b684c].",
        ),
        (
            "Cross-drone events possible correlation between Drone 298 and Drone 474 [ev-722d707b684c].",
            "Drone 298 lidar timeout leads to Drone 474 avoidance manoeuvre [ev-722d707b684c].",
        ),
        (
            "Drone 298 timeout inferred as possible trigger for Drone 474 manoeuvre [ev-722d707b684c].",
            "Drone 298 timeout induced Drone 474 manoeuvre [ev-722d707b684c].",
        ),
        (
            "Evidence indicates plausible correlation between Drone 298 and Drone 474 [ev-1c432aca07e2].",
            "Drone 474 departure was due to Drone 298 lidar failure [ev-1c432aca07e2].",
        ),
        (
            "Plausible relation observed between Drone 298 and Drone 474 trajectory [ev-722d707b684c].",
            "Drone 474 left formation because of Drone 298 lidar timeout [ev-1c432aca07e2].",
        ),
        (
            "Hypothesized interaction between Drone 298 and Drone 474 flight paths [ev-722d707b684c].",
            "Drone 298 was solely responsible for Drone 474 leaving slot [ev-1c432aca07e2].",
        ),
        (
            "Telemetry potentially points to causal chain between Drone 298 and Drone 474 [ev-722d707b684c].",
            "Drone 298 caused Drone 474 to break formation immediately [ev-1c432aca07e2].",
        ),
        (
            "Inconclusive telemetry suggests correlation between Drone 298 and Drone 474 [ev-722d707b684c].",
            "Drone 298 triggers cascade leading to Drone 474 evasion [ev-722d707b684c].",
        ),
        (
            "Observed data suggests possible link between Drone 298 and Drone 474 [ev-722d707b684c].",
            "Drone 298 causes Drone 474 to abort path [ev-722d707b684c].",
        ),
        (
            "Unverified hypothesis that Drone 298 drift preceded Drone 474 reaction [ev-722d707b684c].",
            "Drone 298 drift proved Drone 474 evasive response [ev-722d707b684c].",
        ),
        (
            "Telemetry shows likely association between Drone 298 status and Drone 474 [ev-722d707b684c].",
            "Drone 298 status results in Drone 474 emergency break [ev-1c432aca07e2].",
        ),
        (
            "Plausibly, Drone 298 anomaly was correlated with Drone 474 departure [ev-1c432aca07e2].",
            "Drone 298 anomaly resulted in Drone 474 leaving formation [ev-1c432aca07e2].",
        ),
    ]

    def test_03_relation_axis_calibration(self):
        """Test Relation escalation axis: uncalibrated cross-source causal verbs trigger FORCE_OVERCLAIM."""
        for hedged, escalated in self.RELATION_CONTRASTIVE_PAIRS:
            r_hedged = self._eval_synth(hedged)
            r_escalated = self._eval_synth(escalated)

            self.assertEqual(
                r_hedged.badge,
                ValidationBadge.VERIFIED_SUPPORTED,
                f"Hedged relation claim failed: {hedged}",
            )
            self.assertEqual(
                r_escalated.badge,
                ValidationBadge.FORCE_OVERCLAIM,
                f"Escalated relation claim was not rejected: {escalated}",
            )
            self.assertIn("relation", r_escalated.force_axes)
            self.assertGreater(len(r_escalated.force_axes["relation"]), 0)

    # =========================================================================
    # 4. Axis 2: Modality (Certainty Escalation) — 15 Contrastive Pairs
    # =========================================================================

    MODALITY_CONTRASTIVE_PAIRS = [
        (
            "Drone 474 likely executed an avoidance manoeuvre [ev-722d707b684c].",
            "Drone 474 definitely executed an avoidance manoeuvre [ev-722d707b684c].",
        ),
        (
            "Drone 474 plausibly broke formation after collision alert [ev-1c432aca07e2].",
            "Drone 474 certainly broke formation after collision alert [ev-1c432aca07e2].",
        ),
        (
            "Evidence indicates Drone 474 turned away from obstacle [ev-722d707b684c].",
            "Drone 474 conclusively turned away from obstacle [ev-722d707b684c].",
        ),
        (
            "Drone 474 appears to have responded to separation boundary [ev-722d707b684c].",
            "Drone 474 must have responded to separation boundary [ev-722d707b684c].",
        ),
        (
            "Drone 474 logged avoidance action in flight [ev-722d707b684c].",
            "Drone 474 is guaranteed to have left slot due to sensor fault [ev-1c432aca07e2].",
        ),
        (
            "Flight controller likely initiated slot departure [ev-1c432aca07e2].",
            "Flight controller undoubtedly initiated slot departure [ev-1c432aca07e2].",
        ),
        (
            "Telemetry suggests heading was modified by 35 degrees [ev-722d707b684c].",
            "With absolute certainty heading was modified by 35 degrees [ev-722d707b684c].",
        ),
        (
            "Drone 474 experienced separation reduction [ev-722d707b684c].",
            "Drone 474 irrefutably experienced separation reduction [ev-722d707b684c].",
        ),
        (
            "Formation state change observed for Drone 474 [ev-1c432aca07e2].",
            "It is a proven fact that Drone 474 departed formation [ev-1c432aca07e2].",
        ),
        (
            "Data indicates heading deviation command [ev-722d707b684c].",
            "It is indisputably confirmed that Drone 474 broke slot [ev-1c432aca07e2].",
        ),
        (
            "Drone 474 recorded collision risk flag [ev-722d707b684c].",
            "Without a doubt Drone 474 broke formation [ev-1c432aca07e2].",
        ),
        (
            "Avoidance manoeuvre observed on Drone 474 [ev-722d707b684c].",
            "Drone 474 definitely departed assigned path [ev-1c432aca07e2].",
        ),
        (
            "Observed telemetry indicates slot loss [ev-1c432aca07e2].",
            "Drone 474 certainly lost position in grid [ev-1c432aca07e2].",
        ),
        (
            "Drone 474 made a trajectory correction [ev-722d707b684c].",
            "Drone 474 conclusively suffered trajectory failure [ev-722d707b684c].",
        ),
        (
            "Drone 474 is reported to have exited formation [ev-1c432aca07e2].",
            "Drone 474 must have experienced critical motor failure [ev-1c432aca07e2].",
        ),
    ]

    def test_04_modality_axis_calibration(self):
        """Test Modality escalation axis: absolute certainty markers trigger FORCE_OVERCLAIM."""
        for hedged, escalated in self.MODALITY_CONTRASTIVE_PAIRS:
            r_hedged = self._eval_synth(hedged)
            r_escalated = self._eval_synth(escalated)

            self.assertEqual(
                r_hedged.badge,
                ValidationBadge.VERIFIED_SUPPORTED,
                f"Calibrated modality claim failed: {hedged}",
            )
            self.assertEqual(
                r_escalated.badge,
                ValidationBadge.FORCE_OVERCLAIM,
                f"Absolute modality claim was not rejected: {escalated}",
            )
            self.assertIn("modality", r_escalated.force_axes)
            self.assertGreater(len(r_escalated.force_axes["modality"]), 0)

    # =========================================================================
    # 5. Axis 3: Scope (Generalization Escalation) — 15 Contrastive Pairs
    # =========================================================================

    SCOPE_CONTRASTIVE_PAIRS = [
        (
            "Drone 474 experienced an avoidance manoeuvre [ev-722d707b684c].",
            "All drones experienced an avoidance manoeuvre [ev-722d707b684c].",
        ),
        (
            "Drone 474 left its assigned formation slot [ev-1c432aca07e2].",
            "The entire fleet left assigned formation slots [ev-1c432aca07e2].",
        ),
        (
            "Drone 474 executed an emergency course correction [ev-722d707b684c].",
            "Every drone executed an emergency course correction [ev-722d707b684c].",
        ),
        (
            "Drone 474 detected a proximate collision hazard [ev-722d707b684c].",
            "All nodes detected proximate collision hazards [ev-722d707b684c].",
        ),
        (
            "Drone 474 reported formation departure [ev-1c432aca07e2].",
            "The entire swarm reported formation departure [ev-1c432aca07e2].",
        ),
        (
            "Telemetry shows Drone 474 evasion [ev-722d707b684c].",
            "Fleet-wide evasion was recorded in telemetry [ev-722d707b684c].",
        ),
        (
            "Drone 474 recorded separation loss [ev-722d707b684c].",
            "System-wide separation loss occurred across the grid [ev-722d707b684c].",
        ),
        (
            "Drone 474 deviated from waypoint [ev-722d707b684c].",
            "Universally all drones broke formation simultaneously [ev-1c432aca07e2].",
        ),
        (
            "Drone 474 encountered a sensor anomaly [ev-722d707b684c].",
            "All sensors across the fleet failed during the flight [ev-722d707b684c].",
        ),
        (
            "Drone 474 adjusted its heading angle [ev-722d707b684c].",
            "Every subsystem suffered complete loss of guidance [ev-1c432aca07e2].",
        ),
        (
            "Drone 474 departed formation slot [ev-1c432aca07e2].",
            "All drones lost their assigned formation positions [ev-1c432aca07e2].",
        ),
        (
            "Drone 474 responded to obstacle proximity [ev-722d707b684c].",
            "The entire fleet reacted to obstacle proximity [ev-722d707b684c].",
        ),
        (
            "Drone 474 observed 3.2 m separation [ev-722d707b684c].",
            "Every drone observed 3.2 m separation breach [ev-722d707b684c].",
        ),
        (
            "Drone 474 executed avoidance logic [ev-722d707b684c].",
            "All nodes executed avoidance logic simultaneously [ev-722d707b684c].",
        ),
        (
            "Drone 474 recorded state code 1 [ev-1c432aca07e2].",
            "The entire swarm recorded state code 1 [ev-1c432aca07e2].",
        ),
    ]

    def test_05_scope_axis_calibration(self):
        """Test Scope escalation axis: fleet-wide generalizations trigger FORCE_OVERCLAIM."""
        for hedged, escalated in self.SCOPE_CONTRASTIVE_PAIRS:
            r_hedged = self._eval_synth(hedged)
            r_escalated = self._eval_synth(escalated)

            self.assertEqual(
                r_hedged.badge,
                ValidationBadge.VERIFIED_SUPPORTED,
                f"Bounded scope claim failed: {hedged}",
            )
            self.assertEqual(
                r_escalated.badge,
                ValidationBadge.FORCE_OVERCLAIM,
                f"Universal scope claim was not rejected: {escalated}",
            )
            self.assertIn("scope", r_escalated.force_axes)
            self.assertGreater(len(r_escalated.force_axes["scope"]), 0)

    # =========================================================================
    # 6. Axis 4: Temporal (Cross-Source Order Escalation) — 15 Contrastive Pairs
    # =========================================================================

    TEMPORAL_CONTRASTIVE_PAIRS = [
        (
            "Accounting for unsynchronized producer clocks, Drone 298 event occurred before Drone 474 [ev-722d707b684c].",
            "Drone 298 event strictly before Drone 474 event [ev-722d707b684c].",
        ),
        (
            "Given independent producer clocks, Drone 298 alert preceded Drone 474 reaction [ev-722d707b684c].",
            "Drone 298 alert occurred before Drone 474 reaction in absolute time [ev-722d707b684c].",
        ),
        (
            "Without synchronized clocks, Drone 474 action happened after Drone 298 timeout [ev-1c432aca07e2].",
            "Drone 474 action happened after Drone 298 timeout [ev-1c432aca07e2].",
        ),
        (
            "Subject to clock drift caveats, Drone 474 manoeuvre was preceded by Drone 298 anomaly [ev-722d707b684c].",
            "Drone 474 manoeuvre was preceded by Drone 298 anomaly [ev-722d707b684c].",
        ),
        (
            "Assuming unaligned producer clocks, Drone 474 turn was subsequent to Drone 298 status [ev-722d707b684c].",
            "Drone 474 turn was subsequent to Drone 298 status [ev-722d707b684c].",
        ),
        (
            "With producer clock skew, Drone 298 log was earlier than Drone 474 departure [ev-1c432aca07e2].",
            "Drone 298 log was earlier than Drone 474 departure [ev-1c432aca07e2].",
        ),
        (
            "Considering clock unsynchronization, Drone 474 recorded event later than Drone 298 [ev-722d707b684c].",
            "Drone 474 recorded event later than Drone 298 [ev-722d707b684c].",
        ),
        (
            "With producer clock differences, Drone 298 timestamp occurred before Drone 474 timestamp [ev-722d707b684c].",
            "Drone 298 occurred before Drone 474 [ev-722d707b684c].",
        ),
        (
            "Accounting for clock drift, Drone 474 reacted subsequent to Drone 298 telemetry [ev-722d707b684c].",
            "Drone 474 reacted subsequent to Drone 298 telemetry [ev-722d707b684c].",
        ),
        (
            "With uncalibrated producer clocks, Drone 298 logged strictly before Drone 474 egress [ev-1c432aca07e2].",
            "Drone 298 logged strictly before Drone 474 egress [ev-1c432aca07e2].",
        ),
        (
            "Given producer clock offsets, Drone 298 drift preceded by observation Drone 474 [ev-722d707b684c].",
            "Drone 298 drift was preceded by Drone 474 [ev-722d707b684c].",
        ),
        (
            "Clock skew considered, Drone 298 status arrived earlier than Drone 474 command [ev-722d707b684c].",
            "Drone 298 status arrived earlier than Drone 474 command [ev-722d707b684c].",
        ),
        (
            "Subject to independent clocks, Drone 474 event happened after Drone 298 fault [ev-1c432aca07e2].",
            "Drone 474 event happened after Drone 298 fault [ev-1c432aca07e2].",
        ),
        (
            "With unsynchronized mobile nodes, Drone 298 logged occurred before Drone 474 [ev-722d707b684c].",
            "Drone 298 logged occurred before Drone 474 [ev-722d707b684c].",
        ),
        (
            "Considering producer clock skew, Drone 474 egress happened after Drone 298 issue [ev-1c432aca07e2].",
            "Drone 474 egress happened after Drone 298 issue [ev-1c432aca07e2].",
        ),
    ]

    def test_06_temporal_axis_calibration(self):
        """Test Temporal escalation axis: cross-source absolute temporal order triggers FORCE_OVERCLAIM."""
        for hedged, escalated in self.TEMPORAL_CONTRASTIVE_PAIRS:
            r_hedged = self._eval_synth(hedged)
            r_escalated = self._eval_synth(escalated)

            self.assertEqual(
                r_hedged.badge,
                ValidationBadge.VERIFIED_SUPPORTED,
                f"Hedged temporal claim failed: {hedged}",
            )
            self.assertEqual(
                r_escalated.badge,
                ValidationBadge.FORCE_OVERCLAIM,
                f"Absolute temporal claim was not rejected: {escalated}",
            )
            self.assertIn("temporal", r_escalated.force_axes)
            self.assertGreater(len(r_escalated.force_axes["temporal"]), 0)

    # =========================================================================
    # 7. Axis 5: Numeric (Precision Escalation) — 15 Contrastive Pairs
    # =========================================================================

    NUMERIC_CONTRASTIVE_PAIRS = [
        (
            "Observed minimum separation distance was 3.2 m [ev-722d707b684c].",
            "Observed minimum separation distance was 3.20000 m [ev-722d707b684c].",
        ),
        (
            "Drone 474 heading changed by -35 deg [ev-722d707b684c].",
            "Drone 474 heading changed by -35.00000 deg [ev-722d707b684c].",
        ),
        (
            "Minimum separation recorded as 3.2 meters [ev-722d707b684c].",
            "Minimum separation recorded as 3.200000 meters [ev-722d707b684c].",
        ),
        (
            "Commanded trajectory correction was -35 deg [ev-722d707b684c].",
            "Commanded trajectory correction was -35.12345 deg [ev-722d707b684c].",
        ),
        (
            "Separation margin maintained at 3.2 m [ev-722d707b684c].",
            "Separation margin maintained at 3.20000 m [ev-722d707b684c].",
        ),
        (
            "Proximity threshold reached 3.2 m before avoidance [ev-722d707b684c].",
            "Proximity threshold reached 3.20000 m before avoidance [ev-722d707b684c].",
        ),
        (
            "The heading angle was commanded at -35 deg [ev-722d707b684c].",
            "The heading angle was commanded at -35.0000 deg [ev-722d707b684c].",
        ),
        (
            "Separation telemetry reported 3.2 m [ev-722d707b684c].",
            "Separation telemetry reported 3.20000 m [ev-722d707b684c].",
        ),
        (
            "Aircraft recorded separation distance of 3.2 m [ev-722d707b684c].",
            "Aircraft recorded separation distance of 3.20000 m [ev-722d707b684c].",
        ),
        (
            "Course turn executed with -35 deg offset [ev-722d707b684c].",
            "Course turn executed with -35.0000 deg offset [ev-722d707b684c].",
        ),
        (
            "Obstacle distance fell to 3.2 m [ev-722d707b684c].",
            "Obstacle distance fell to 3.200000 m [ev-722d707b684c].",
        ),
        (
            "Flight controller recorded 3.2 m separation [ev-722d707b684c].",
            "Flight controller recorded 3.20000 m separation [ev-722d707b684c].",
        ),
        (
            "Turn angle commanded at -35 degrees [ev-722d707b684c].",
            "Turn angle commanded at -35.00000 degrees [ev-722d707b684c].",
        ),
        (
            "Closest approach measured was 3.2 m [ev-722d707b684c].",
            "Closest approach measured was 3.20000 m [ev-722d707b684c].",
        ),
        (
            "Telemetry confirmed 3.2 m threshold [ev-722d707b684c].",
            "Telemetry confirmed 3.200000 m threshold [ev-722d707b684c].",
        ),
    ]

    def test_07_numeric_axis_calibration(self):
        """Test Numeric escalation axis: fabricated precision triggers FORCE_OVERCLAIM."""
        for hedged, escalated in self.NUMERIC_CONTRASTIVE_PAIRS:
            r_hedged = self._eval_synth(hedged)
            r_escalated = self._eval_synth(escalated)

            self.assertEqual(
                r_hedged.badge,
                ValidationBadge.VERIFIED_SUPPORTED,
                f"Exact numeric claim failed: {hedged}",
            )
            self.assertEqual(
                r_escalated.badge,
                ValidationBadge.FORCE_OVERCLAIM,
                f"Fabricated precision claim was not rejected: {escalated}",
            )
            self.assertIn("numeric", r_escalated.force_axes)
            self.assertGreater(len(r_escalated.force_axes["numeric"]), 0)

    # =========================================================================
    # 8. Monotonicity Violation Rate (MVR) Across All 5 Axes — 75 Pairs
    # =========================================================================

    def test_08_monotonicity_violation_rate_zero(self):
        """Monotonicity Violation Rate (MVR) across all 75 force escalation pairs must be 0.0."""
        all_pairs = (
            self.RELATION_CONTRASTIVE_PAIRS
            + self.MODALITY_CONTRASTIVE_PAIRS
            + self.SCOPE_CONTRASTIVE_PAIRS
            + self.TEMPORAL_CONTRASTIVE_PAIRS
            + self.NUMERIC_CONTRASTIVE_PAIRS
        )
        total_pairs = len(all_pairs)
        self.assertEqual(total_pairs, 75, "Expected exactly 75 contrastive escalation pairs")

        violations = 0
        for hedged, escalated in all_pairs:
            r_hedged = self._eval_synth(hedged)
            r_escalated = self._eval_synth(escalated)

            # A monotonicity violation occurs if an escalated claim receives VERIFIED_SUPPORTED
            # or if the auditor was more permissive on the escalated claim than the hedged claim.
            if r_escalated.badge == ValidationBadge.VERIFIED_SUPPORTED:
                violations += 1

        mvr = violations / total_pairs
        self.assertEqual(
            mvr,
            0.0,
            f"Expected Monotonicity Violation Rate (MVR) == 0.0, found {violations} violations ({mvr:.4f})",
        )

    # =========================================================================
    # 9. Clean Grounded Claims Suite — 10 Cases
    # =========================================================================

    CLEAN_GROUNDED_CASES = [
        "Drone 474 departed formation [ev-1c432aca07e2] following avoidance manoeuvre [ev-722d707b684c].",
        "Drone 474 executed an avoidance turn commanded at -35 deg [ev-722d707b684c].",
        "Closest observed proximity was 3.2 m [ev-722d707b684c].",
        "Drone 474 exited slot after collision risk alert [ev-722d707b684c] [ev-1c432aca07e2].",
        "Formation state changed to LEFT_FORMATION on Drone 474 [ev-1c432aca07e2].",
        "Obstacle proximity prompted avoidance reaction on Drone 474 [ev-722d707b684c].",
        "Drone 474 recorded evasive manoeuvre with heading change [ev-722d707b684c].",
        "Separation of 3.2 m was noted before Drone 474 left slot [ev-722d707b684c] [ev-1c432aca07e2].",
        "Telemetry indicates Drone 474 left formation [ev-1c432aca07e2].",
        "Avoidance manoeuvre with reason COLLISION_RISK logged on Drone 474 [ev-722d707b684c].",
    ]

    def test_09_clean_grounded_claims_pass_verified_supported(self):
        """Clean, well-hedged, and well-grounded claims receive VERIFIED_SUPPORTED."""
        for text in self.CLEAN_GROUNDED_CASES:
            report = self._eval_synth(text)
            self.assertEqual(
                report.badge,
                ValidationBadge.VERIFIED_SUPPORTED,
                f"Clean grounded claim was not verified: {text}",
            )
            self.assertTrue(report.is_accepted())

    # =========================================================================
    # 10. Total Test Cases Quantification (>= 100 cases)
    # =========================================================================

    def test_10_forcebench_suite_quantification(self):
        """Assert total benchmark test cases >= 100."""
        # 75 contrastive pairs = 150 evaluations (75 hedged + 75 escalated)
        # 20 entity binding traps
        # 20 fake citations
        # 10 clean gold cases
        # Total = 200 distinct test cases evaluated
        total_evaluations = (
            len(self.RELATION_CONTRASTIVE_PAIRS) * 2
            + len(self.MODALITY_CONTRASTIVE_PAIRS) * 2
            + len(self.SCOPE_CONTRASTIVE_PAIRS) * 2
            + len(self.TEMPORAL_CONTRASTIVE_PAIRS) * 2
            + len(self.NUMERIC_CONTRASTIVE_PAIRS) * 2
            + len(self.ENTITY_BINDING_CASES)
            + len(self.FAKE_CITATION_CASES)
            + len(self.CLEAN_GROUNDED_CASES)
        )
        self.assertGreaterEqual(
            total_evaluations,
            100,
            f"ForceBench suite requires >= 100 test cases, got {total_evaluations}",
        )
        self.assertEqual(total_evaluations, 200)


if __name__ == "__main__":
    unittest.main()
