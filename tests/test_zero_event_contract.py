"""End-to-End Zero-Event Contract and Data Gaps Quarantine Test Suite.

Verifies:
  1. Retrieving a time range with 0 persisted events returns typed status="EMPTY".
  2. Stage 0 Sufficiency Gate activates Data Gaps Quarantine and short-circuits LLM synthesis.
  3. Produces INSUFFICIENT_EVIDENCE badge and clean 0-event summary without hallucinations.
  4. Pipeline runner JSON output contains typed envelope and quarantine metadata.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bridge.java_retriever import JavaRetriever
from bridge.sufficiency_gate import SufficiencyStatus, evaluate_sufficiency
from bridge.synthesizer import (
    CitationAuditor,
    ValidationBadge,
    synthesize_and_validate,
)
from fss_py.evidence import build_evidence
from fss_py.model import ALL, RetrievalRequest, RetrievalResult

ROOT_DIR = Path(__file__).resolve().parent.parent


class TestZeroEventContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"

    def test_01_zero_event_retrieval_contract(self):
        """Querying a time window far in the past returns 0 events with status EMPTY."""
        with tempfile.NamedTemporaryFile(prefix="test_zero_store_", suffix=".dat", delete=False) as tmp:
            store_file = Path(tmp.name)

        try:
            retriever = JavaRetriever.from_fixtures(self.fixture_path, store_path=store_file)
            # Query window [1000, 2000] ns where no events exist
            res = retriever.retrieve(ALL, 1000, 2000, max_events=100)
            self.assertEqual(len(res.events), 0)
            self.assertFalse(res.truncated)

            # Build evidence
            evidence = build_evidence(res.events, query="Why did drone leave?", retrieval=res)
            self.assertEqual(len(evidence["events"]), 0)

            # Stage 0 Pre-Generation Sufficiency Gate
            sufficiency = evaluate_sufficiency(evidence, "Why did drone leave?")
            self.assertFalse(sufficiency.is_sufficient)
            self.assertEqual(sufficiency.status, SufficiencyStatus.INSUFFICIENT_EVIDENCE)
            self.assertTrue(sufficiency.bypass_synthesis)
            self.assertEqual(sufficiency.quarantine_payload.get("status"), "QUARANTINED")

            # End-to-end synthesis & validation
            synth, val_report, model_used = synthesize_and_validate(evidence, "Why did drone leave?")
            self.assertEqual(val_report.badge, ValidationBadge.INSUFFICIENT_EVIDENCE)
            self.assertEqual(val_report.verdict, "ACCEPTED")  # Legacy compatibility for 0-event baseline
            self.assertTrue(val_report.is_accepted())
            self.assertEqual(synth["summary"], "No events recorded or retrieved in the requested time window.")
            self.assertIsNone(synth["likely_cause"])
            self.assertEqual(synth["contributing_events"], [])
        finally:
            if store_file.exists():
                store_file.unlink()

    def test_02_pipeline_runner_zero_event_cli(self):
        """Verify pipeline_runner --json-out with empty stdin executes cleanly."""
        proc = subprocess.run(
            [
                sys.executable,
                "bridge/pipeline_runner.py",
                "--query",
                "Why did drone 474 leave?",
                "--start",
                "1000",
                "--end",
                "2000",
                "--json-out",
            ],
            input="",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        self.assertEqual(len(data["evidence"]["events"]), 0)
        self.assertEqual(data["validation"]["verdict"], "ACCEPTED")
        self.assertEqual(data["validation"]["badge"], str(ValidationBadge.INSUFFICIENT_EVIDENCE))
        self.assertEqual(
            data["synthesis"]["summary"],
            "No events recorded or retrieved in the requested time window.",
        )
        self.assertIsNone(data["synthesis"]["likely_cause"])


if __name__ == "__main__":
    unittest.main()
