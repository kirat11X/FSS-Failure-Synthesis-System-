from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bridge.retrieval_ipc import (
    MAX_RECORDS,
    MAX_WINDOW_NS,
    JavaRetrievalRequest,
    RetrievalIpcClient,
    RetrievalIpcError,
    _parse_retrieval_metadata,
    _parse_total_matches,
    java_classpath,
)

ROOT = Path(__file__).resolve().parents[1]


def test_java_request_accepts_inclusive_point_and_rejects_unbounded_windows():
    point = JavaRetrievalRequest("all", 1_700_000_000_000_000_000, 1_700_000_000_000_000_000)
    assert point.start_time_ns == point.end_time_ns

    with pytest.raises(ValueError, match="one hour"):
        JavaRetrievalRequest("all", 0, MAX_WINDOW_NS + 1)
    with pytest.raises(ValueError, match="max_records"):
        JavaRetrievalRequest("all", 0, 0, max_records=MAX_RECORDS + 1)


@pytest.mark.parametrize("value", [2, "2"])
def test_total_matches_accepts_integer_count_and_decimal_string(value):
    assert _parse_total_matches(value, 2, ["records", "totalMatches"]) == 2


@pytest.mark.parametrize("value", ["two", True, 1])
def test_total_matches_rejects_invalid_count_with_response_diagnostics(value):
    with pytest.raises(RetrievalIpcError, match="response_keys=.*totalMatches"):
        _parse_total_matches(value, 2, ["records", "totalMatches"])


def test_missing_java_count_metadata_is_reported_as_unknown():
    assert _parse_retrieval_metadata(
        {"status": "OK", "records": [{}] * 882, "error": None},
        records_returned=882,
    ) == (None, None)


def test_partial_java_count_metadata_preserves_reported_truncation():
    assert _parse_retrieval_metadata(
        {"status": "OK", "records": [{}], "truncated": True},
        records_returned=1,
    ) == (None, True)


def test_total_match_metadata_derives_truncation_when_count_is_reported():
    assert _parse_retrieval_metadata(
        {"status": "OK", "records": [{}], "totalMatches": "3"},
        records_returned=1,
    ) == (3, True)


@pytest.fixture(scope="module")
def populated_java_store(tmp_path_factory):
    mvn = shutil.which("mvn")
    java = shutil.which("java")
    if not mvn or not java:
        pytest.skip("Java and Maven are required for the Retrieval IPC integration test")

    subprocess.run(
        [
            mvn,
            "-q",
            "-DskipTests",
            "compile",
            "dependency:build-classpath",
            "-Dmdep.outputFile=fss-core/target/dependency-classpath.txt",
        ],
        cwd=ROOT,
        check=True,
    )

    temp_root = tmp_path_factory.mktemp("retrieval_ipc")
    properties = temp_root / "storage.properties"
    store_root = temp_root / "store"
    properties.write_text(
        "\n".join(
            [
                f"root={store_root.as_posix()}",
                f"envelopeConfig={(ROOT / 'envelope.cfg').as_posix()}",
                "blockSize=65536",
                "bufferCapacityBytes=1048576",
                "maxMessageSize=4096",
                "segmentDurationMillis=60000",
                "byteOrder=BIG_ENDIAN",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            java,
            "-cp",
            java_classpath(ROOT),
            "DirectIngestionRetrievalTest",
            str(properties),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return properties


def test_persistent_ipc_caps_latest_records_and_preserves_filters(populated_java_store):
    client = RetrievalIpcClient(populated_java_store)
    try:
        latest = client.retrieve(
            JavaRetrievalRequest(
                source="all",
                start_time_ns=0,
                end_time_ns=MAX_WINDOW_NS,
                max_records=2,
            )
        )
        assert latest.total_matches == 15
        assert latest.truncated is True
        assert len(latest.events) == 2
        assert latest.events[-1].timestamp_ns > latest.events[0].timestamp_ns
        assert latest.events[-1].payload_bytes.endswith(b"agent-3-message-5")

        filtered = client.retrieve(
            JavaRetrievalRequest(
                source=1,
                start_time_ns=0,
                end_time_ns=MAX_WINDOW_NS,
                event_type=2,
            )
        )
        assert filtered.total_matches == 2
        assert filtered.truncated is False
        assert all(event.source_id == 1 and event.event_type == 2 for event in filtered.events)
    finally:
        client.close()
