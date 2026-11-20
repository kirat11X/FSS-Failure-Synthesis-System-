"""Tests for the Storage & Retrieval IPC Java integration."""

import os
from pathlib import Path
import pytest

from bridge.java_retriever import JavaRetriever
from fss_py.model import ALL, EventRecord


@pytest.fixture(scope="module")
def storage_env(tmp_path_factory):
    """Ensure Storage classes are compiled and test data exists in an isolated directory."""
    root = Path(__file__).resolve().parent.parent
    test_dir = tmp_path_factory.mktemp("storage_test")
    props_path = test_dir / "storage.properties"

    content = f"""root={test_dir / "data"}
envelopeConfig={root / "fss-core" / "envelope.cfg"}
blockSize=65536
maxMessageSize=4096
bufferCapacityBytes=1048576
segmentDurationMillis=60000
byteOrder=BIG_ENDIAN
"""
    props_path.write_text(content)

    retriever = JavaRetriever()
    cmd = [
        retriever.java_bin,
        "-cp",
        retriever.classpath,
        "DirectIngestionRetrievalTest",
        str(props_path),
    ]
    import subprocess
    subprocess.run(cmd, cwd=root, check=True)
    return props_path


def test_storage_ipc_retriever_all(storage_env):
    """Test unbounded agent query across the Storage/Retrieval IPC bridge."""
    retriever = JavaRetriever(
        mode="storage_ipc",
        storage_properties=storage_env,
    )
    result = retriever.retrieve(ALL, 0, 2_000_000_000_000, max_events=100)
    assert result is not None
    assert len(result.events) >= 15
    assert all(isinstance(ev, EventRecord) for ev in result.events)


def test_storage_ipc_retriever_filter_agent(storage_env):
    """Test filtering by specific agent ID."""
    retriever = JavaRetriever(
        mode="storage_ipc",
        storage_properties=storage_env,
    )
    result = retriever.retrieve(1, 0, 2_000_000_000_000, max_events=100)
    assert len(result.events) == 5
    assert all(ev.source_id == 1 for ev in result.events)


def test_storage_ipc_retriever_filter_event_type(storage_env):
    """Test filtering by event type."""
    retriever = JavaRetriever(
        mode="storage_ipc",
        storage_properties=storage_env,
    )
    result = retriever.retrieve(ALL, 0, 2_000_000_000_000, max_events=100, event_type=2)
    assert len(result.events) == 6
    assert all(ev.event_type == 2 for ev in result.events)
