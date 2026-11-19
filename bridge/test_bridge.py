"""Comprehensive integration tests for the Java-to-Python Bridge.

Tests:
  1. JavaStoreBridge CLI compilation & help output.
  2. BlockStore ingestion of binary fixtures via Java.
  3. Bounded retrieval & envelope streaming with JSON metadata header.
  4. JavaRetriever Python protocol conformance (retrieval contract).
  5. Synthesizer reasoning, citation validation & hallucination detection.
  6. Pipeline runner 8-part report generation & JSON output.
  7. End-to-end Java -> Python ProcessBuilder pipeline invocation.
  8. Validator catches invalid/non-hex hallucinated IDs ([ev-fake], [ev-12345], [drone-298]).
  9. Empty (0-event) pipeline execution and deterministic synthesis.
 10. Truncated retrieval data gap handling (event_id is None).
 11. JavaStoreBridge dynamic project root resolution from arbitrary working directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bridge.java_retriever import JavaRetriever
from bridge.synthesizer import (
    build_llm_context,
    deterministic_synthesize,
    retrieve_relevant_knowledge,
    synthesize_and_validate,
    validate_synthesis,
)
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, RetrievalRequest, RetrievalRequestError, RetrievalResult
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes


class TestBridge(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"
        cls.schema_path = ROOT_DIR / "schemas" / "drone_v0.yaml"
        cls.schema = load_schema_file(cls.schema_path)

        # Ensure classes compiled
        subprocess.run(["bash", "bridge/compile.sh"], cwd=ROOT_DIR, check=True)

    def test_01_java_store_bridge_help(self):
        """Verify JavaStoreBridge runs and displays CLI help."""
        cp = f"{ROOT_DIR}/fss-core/target/classes:{ROOT_DIR}/bridge/classes"
        proc = subprocess.run(
            ["java", "-cp", cp, "bridge.JavaStoreBridge", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        self.assertIn("JavaStoreBridge: FSS Java Core & Python Pipeline Linkage", proc.stdout)
        self.assertIn("ingest", proc.stdout)
        self.assertIn("retrieve", proc.stdout)
        self.assertIn("pipeline", proc.stdout)

    def test_02_java_ingest_and_retrieve(self):
        """Verify JavaStoreBridge ingest and retrieve commands."""
        with tempfile.NamedTemporaryFile(prefix="test_store_", suffix=".dat", delete=False) as tmp:
            store_file = Path(tmp.name)

        try:
            cp = f"{ROOT_DIR}/fss-core/target/classes:{ROOT_DIR}/bridge/classes"

            # Ingest
            ingest_proc = subprocess.run(
                [
                    "java",
                    "-cp",
                    cp,
                    "bridge.JavaStoreBridge",
                    "ingest",
                    "--store",
                    str(store_file),
                    "--fixture",
                    str(self.fixture_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            self.assertIn("Successfully ingested 9 events", ingest_proc.stdout)

            # Retrieve
            ret_proc = subprocess.run(
                [
                    "java",
                    "-cp",
                    cp,
                    "bridge.JavaStoreBridge",
                    "retrieve",
                    "--store",
                    str(store_file),
                    "--source",
                    "ALL",
                    "--start",
                    "1791454020000000000",
                    "--end",
                    "1791454030000000000",
                    "--max-events",
                    "100",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            raw = ret_proc.stdout
            nl = raw.find(b"\n")
            self.assertNotEqual(nl, -1)
            meta = json.loads(raw[:nl].decode("utf-8"))
            self.assertEqual(meta["count"], 9)
            self.assertFalse(meta["truncated"])

            envelopes = list(iter_envelopes(raw[nl + 1 :]))
            self.assertEqual(len(envelopes), 9)
        finally:
            if store_file.exists():
                store_file.unlink()

    def test_03_java_retriever_protocol(self):
        """Verify JavaRetriever conforms to Retriever protocol and enforces bounds."""
        retriever = JavaRetriever.from_fixtures(self.fixture_path)
        try:
            # Full retrieval
            res = retriever.retrieve(ALL, 1791454020000000000, 1791454030000000000, max_events=100)
            self.assertEqual(len(res.events), 9)
            self.assertFalse(res.truncated)

            # Filter by single source
            res_298 = retriever.retrieve(298, 1791454020000000000, 1791454030000000000, max_events=100)
            self.assertEqual(len(res_298.events), 4)
            for ev in res_298.events:
                self.assertEqual(ev.source_id, 298)

            # Filter by event_type
            res_avoid = retriever.retrieve(ALL, 1791454020000000000, 1791454030000000000, max_events=100, event_type=0x20)
            self.assertEqual(len(res_avoid.events), 1)
            self.assertEqual(res_avoid.events[0].event_type, 0x20)

            # Max events limit triggers truncation flag
            res_trunc = retriever.retrieve(ALL, 1791454020000000000, 1791454030000000000, max_events=2)
            self.assertEqual(len(res_trunc.events), 2)
            self.assertTrue(res_trunc.truncated)

            # Bounded contract violation: window > 1h raises RetrievalRequestError
            with self.assertRaises(RetrievalRequestError):
                retriever.retrieve(ALL, 0, 3600 * 10**9 + 1, max_events=100)

            # Bounded contract violation: t_end <= t_start
            with self.assertRaises(RetrievalRequestError):
                retriever.retrieve(ALL, 100, 50, max_events=100)
        finally:
            if retriever.store_path.exists():
                retriever.store_path.unlink()

    def test_04_synthesizer_and_validator(self):
        """Verify synthesis formatting, citations, and hallucination rejection."""
        raw_bytes = self.fixture_path.read_bytes()
        events = list(iter_envelopes(raw_bytes))
        decoded = decode_all(events, self.schema)
        evidence = build_evidence(decoded, query="Why did drone 474 leave formation?")

        # 1. Clean deterministic synthesis
        synth, val, model = synthesize_and_validate(evidence, "Why did drone 474 leave formation?")
        self.assertEqual(val.verdict, "ACCEPTED")
        self.assertTrue(val.is_accepted())
        self.assertIn("ev-1c432aca07e2", val.valid_citations)
        self.assertIn("ev-2e92ae032f03", val.valid_citations)
        self.assertIn("ev-722d707b684c", val.valid_citations)
        self.assertEqual(val.invalid_citations, [])

        # 2. Test Validator catches hallucinated event ID
        bad_synth = dict(synth)
        bad_synth["summary"] = "Hallucinated failure occurred [ev-ffffffffffff]."
        bad_synth["contributing_events"] = [{"source_id": 999, "timestamp": 0, "event_type": "FAKE", "event_id": "ev-ffffffffffff"}]
        bad_val = validate_synthesis(bad_synth, evidence)
        self.assertEqual(bad_val.verdict, "REJECTED")
        self.assertIn("ev-ffffffffffff", bad_val.invalid_citations)

    def test_05_pipeline_runner_json_output(self):
        """Verify pipeline_runner --json-out produces valid structured output."""
        proc = subprocess.run(
            [
                sys.executable,
                "bridge/pipeline_runner.py",
                "--file",
                str(self.fixture_path),
                "--query",
                "Why did drone 474 leave formation?",
                "--json-out",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        self.assertIn("evidence", data)
        self.assertIn("synthesis", data)
        self.assertIn("validation", data)
        self.assertEqual(data["validation"]["verdict"], "ACCEPTED")

    def test_06_java_end_to_end_pipeline(self):
        """Verify executing JavaStoreBridge pipeline runs the entire stack end-to-end."""
        cp = f"{ROOT_DIR}/fss-core/target/classes:{ROOT_DIR}/bridge/classes"
        proc = subprocess.run(
            [
                "java",
                "-cp",
                cp,
                "bridge.JavaStoreBridge",
                "pipeline",
                "--fixture",
                str(self.fixture_path),
                "--query",
                "Why did drone 474 leave formation?",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        out = proc.stdout
        self.assertIn("[1] Question", out)
        self.assertIn("[2] Retrieval", out)
        self.assertIn("[3] Events", out)
        self.assertIn("[4] Data gaps", out)
        self.assertIn("[5] Knowledge", out)
        self.assertIn("[6] LLM context", out)
        self.assertIn("[7] Synthesis", out)
        self.assertIn("[8] Validator", out)
        self.assertIn("Verdict:** ACCEPTED", out)

    def test_07_validator_catches_non_hex_and_malformed_hallucinations(self):
        """Verify validator catches [ev-fake], [ev-12345], and [drone-298] as invalid citations."""
        raw_bytes = self.fixture_path.read_bytes()
        events = list(iter_envelopes(raw_bytes))
        decoded = decode_all(events, self.schema)
        evidence = build_evidence(decoded, query="Check hallucinations")

        bad_synth = {
            "summary": "Drone left formation [ev-fake] after collision alert [ev-12345] on drone [drone-298].",
            "likely_cause": "Hypothetical fault [ev-nonhexid0000].",
            "confidence": "high",
            "contributing_events": [
                {"source_id": 298, "timestamp": 0, "event_type": "LIDAR", "event_id": "ev-fake"}
            ],
            "uncertainty": [],
            "data_gaps": [],
        }

        val = validate_synthesis(bad_synth, evidence)
        self.assertEqual(val.verdict, "REJECTED")
        self.assertIn("ev-fake", val.invalid_citations)
        self.assertIn("ev-12345", val.invalid_citations)
        self.assertIn("drone-298", val.invalid_citations)
        self.assertIn("ev-nonhexid0000", val.invalid_citations)

    def test_08_empty_events_pipeline_and_synthesis(self):
        """Verify 0-event retrieval produces a valid, accepted 0-event report."""
        req = RetrievalRequest(
            source=ALL,
            t_start_ns=1000,
            t_end_ns=2000,
            max_events=100,
        )
        ret_res = RetrievalResult(request=req, events=[], truncated=False)
        evidence = build_evidence([], query="Why did drone 474 leave?", retrieval=ret_res)

        synth, val, model = synthesize_and_validate(evidence, "Why did drone 474 leave?")
        self.assertEqual(val.verdict, "ACCEPTED")
        self.assertEqual(synth["summary"], "No events recorded or retrieved in the requested time window.")
        self.assertIsNone(synth["likely_cause"])
        self.assertEqual(synth["contributing_events"], [])

        # Also test via pipeline_runner --json-out with empty stdin
        proc = subprocess.run(
            [
                sys.executable,
                "bridge/pipeline_runner.py",
                "--query",
                "Why did drone leave?",
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
        self.assertEqual(
            data["synthesis"]["summary"],
            "No events recorded or retrieved in the requested time window.",
        )

    def test_09_retrieval_truncated_data_gap_coverage(self):
        """Verify validator handles RETRIEVAL_TRUNCATED data gaps where event_id is None."""
        req = RetrievalRequest(
            source=ALL,
            t_start_ns=1791454020000000000,
            t_end_ns=1791454030000000000,
            max_events=2,
        )
        raw_bytes = self.fixture_path.read_bytes()
        events = list(iter_envelopes(raw_bytes))[:2]
        ret_res = RetrievalResult(request=req, events=events, truncated=True)
        decoded = decode_all(events, self.schema)
        evidence = build_evidence(decoded, query="Truncated test", retrieval=ret_res)

        # Confirm evidence has RETRIEVAL_TRUNCATED gap with event_id=None
        trunc_gaps = [g for g in evidence["data_gaps"] if g.get("kind") == "RETRIEVAL_TRUNCATED"]
        self.assertEqual(len(trunc_gaps), 1)
        self.assertIsNone(trunc_gaps[0]["event_id"])

        # Synthesis that omitted the data gap should be accepted with corrections, restoring it
        incomplete_synth = {
            "summary": "Truncated events observed.",
            "likely_cause": None,
            "confidence": "high",
            "contributing_events": [],
            "uncertainty": [],
            "data_gaps": [],
        }
        val = validate_synthesis(incomplete_synth, evidence)
        self.assertEqual(val.verdict, "ACCEPTED_WITH_CORRECTIONS")
        self.assertTrue(any("RETRIEVAL_TRUNCATED" in note for note in val.notes))
        self.assertTrue(any("RETRIEVAL_TRUNCATED" in g for g in val.corrected_synthesis["data_gaps"]))

    def test_10_java_pipeline_from_arbitrary_cwd(self):
        """Verify JavaStoreBridge resolves projectRoot dynamically when run from /tmp."""
        cp = f"{ROOT_DIR}/fss-core/target/classes:{ROOT_DIR}/bridge/classes"
        proc = subprocess.run(
            [
                "java",
                "-cp",
                cp,
                "bridge.JavaStoreBridge",
                "pipeline",
                "--fixture",
                str(self.fixture_path),
                "--query",
                "Testing from /tmp",
            ],
            cwd="/tmp",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        self.assertIn("FAILURE SYNTHESIS SYSTEM (FSS) END-TO-END REPORT", proc.stdout)
        self.assertIn("Verdict:** ACCEPTED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
