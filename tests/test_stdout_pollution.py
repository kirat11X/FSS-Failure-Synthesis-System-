"""Test Suite 4: JVM Stdout Pollution and Wire Framing Robustness.

Spec requirement:
  Launch JavaRetriever with JAVA_TOOL_OPTIONS="-Dtest.flag=1" injected into child env;
  Pydantic v2 TypeAdapter parses framed response with zero JSON decode errors.

Background:
  Setting JAVA_TOOL_OPTIONS or _JAVA_OPTIONS causes OpenJDK to print:
  "Picked up JAVA_TOOL_OPTIONS: ..."
  If stdout is not cleanly isolated from stderr, or if wire framing is unresilient,
  stray JVM banners desynchronize the stream parser and raise JSONDecodeError.
  JavaStoreBridge isolates FileDescriptor.out and redirects System.out to System.err,
  while JavaRetriever and Pydantic v2 validate strictly framed NDJSON envelopes.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from bridge.java_retriever import JavaRetriever
from bridge.schemas import RETRIEVAL_ENVELOPE_ADAPTER, RetrievalEnvelope
from fss_py.model import ALL

ROOT_DIR = Path(__file__).resolve().parent.parent


class TestStdoutPollution(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"
        cls.store_path = ROOT_DIR / "bridge" / "store_scenario_298_474.dat"
        if not cls.store_path.exists():
            # Ingest to ensure store exists
            retriever = JavaRetriever.from_fixtures(cls.fixture_path, store_path=cls.store_path)

    def test_01_java_tool_options_env_injection(self):
        """Retrieve with JAVA_TOOL_OPTIONS="-Dtest.flag=1" in env; zero JSON decode errors."""
        retriever = JavaRetriever(self.store_path, mode="cli")

        # Inject JAVA_TOOL_OPTIONS into process environment
        old_val = os.environ.get("JAVA_TOOL_OPTIONS")
        os.environ["JAVA_TOOL_OPTIONS"] = "-Dtest.flag=1"
        try:
            # Execute retrieval
            envelope = retriever.retrieve_envelope(
                ALL,
                1791454021200000000,
                1791454024300000000,
                max_events=50,
            )
            self.assertIsInstance(envelope, RetrievalEnvelope)
            self.assertEqual(envelope.status, "OK")
            self.assertGreater(len(envelope.events), 0)
            self.assertEqual(envelope.count, len(envelope.events))
            # Verify events parsed cleanly without corruption
            first_event = envelope.events[0]
            self.assertIn(first_event.source_id, [298, 474])
            self.assertGreaterEqual(first_event.timestamp_ns, 1791454021200000000)
        finally:
            if old_val is not None:
                os.environ["JAVA_TOOL_OPTIONS"] = old_val
            else:
                os.environ.pop("JAVA_TOOL_OPTIONS", None)

    def test_02_multiple_java_options_banners(self):
        """Retrieve with both JAVA_TOOL_OPTIONS and _JAVA_OPTIONS; zero JSON decode errors."""
        retriever = JavaRetriever(self.store_path, mode="cli")

        old_tool = os.environ.get("JAVA_TOOL_OPTIONS")
        old_java = os.environ.get("_JAVA_OPTIONS")
        os.environ["JAVA_TOOL_OPTIONS"] = "-Dtest.tool=abc -Dtest.mode=hardened"
        os.environ["_JAVA_OPTIONS"] = "-Dtest.extra=xyz"
        try:
            envelope = retriever.retrieve_envelope(
                ALL,
                1791454021200000000,
                1791454024300000000,
                max_events=10,
            )
            self.assertEqual(envelope.status, "OK")
            self.assertGreater(len(envelope.events), 0)
        finally:
            if old_tool is not None:
                os.environ["JAVA_TOOL_OPTIONS"] = old_tool
            else:
                os.environ.pop("JAVA_TOOL_OPTIONS", None)
            if old_java is not None:
                os.environ["_JAVA_OPTIONS"] = old_java
            else:
                os.environ.pop("_JAVA_OPTIONS", None)

    def test_03_simulated_stdout_banner_pollution(self):
        """Simulate stray banner lines preceding the NDJSON envelope; TypeAdapter parses frame cleanly."""
        polluted_stdout = (
            b"Picked up JAVA_TOOL_OPTIONS: -Dtest.flag=1\n"
            b"Picked up _JAVA_OPTIONS: -Dtest.extra=xyz\n"
            b'{"status":"OK","trace_id":"1234567890abcdef1234567890abcdef","truncated":false,"count":1,'
            b'"events":[{"source_id":474,"timestamp_ns":"1791454021200000000","event_type":16,"payload_b64":"AQID"}],'
            b'"data_gaps":[]}\n'
        )

        # Extract candidates just as JavaRetriever does
        raw_line = polluted_stdout.strip()
        if b"\n" in raw_line:
            for line in reversed(raw_line.splitlines()):
                candidate = line.strip()
                if candidate.startswith(b"{") and candidate.endswith(b"}"):
                    raw_line = candidate
                    break

        envelope = RETRIEVAL_ENVELOPE_ADAPTER.validate_json(raw_line)
        self.assertEqual(envelope.status, "OK")
        self.assertEqual(envelope.count, 1)
        self.assertEqual(len(envelope.events), 1)
        self.assertEqual(envelope.events[0].source_id, 474)
        self.assertEqual(envelope.events[0].timestamp_ns, 1791454021200000000)
