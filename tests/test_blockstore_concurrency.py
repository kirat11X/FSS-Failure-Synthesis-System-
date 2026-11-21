"""Test Suite 6: Concurrency Safety and File Locking on 64KB BlockStore.

Spec requirement:
  Spawn 8 concurrent Python threads/processes issuing overlapping BlockStore writes and bounded reads;
  0 OverlappingFileLockException, 0 torn 64KB blocks, 100% read-back integrity.

Background:
  Java NIO FileChannel.lock() is held per-JVM, not per-thread. Multi-threaded access to the same
  file channel triggers OverlappingFileLockException. Furthermore, mixing flock(2) and fcntl(2)
  without sidecar lockfiles allows torn writes inside 64KB blocks.
  StoreLockManager pairs an in-JVM ReentrantReadWriteLock with an exclusive sidecar .lock file
  and fcntl locking to serialize access cleanly.
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from bridge.java_retriever import JavaRetriever
from fss_py.model import ALL, EventRecord
from fss_py.wire import encode_envelope

ROOT_DIR = Path(__file__).resolve().parent.parent
BLOCK_SIZE = 65536


class TestBlockStoreConcurrency(unittest.TestCase):

    def test_01_eight_concurrent_workers_writes_and_reads(self):
        """Spawn 8 concurrent threads issuing overlapping BlockStore writes and bounded reads."""
        with tempfile.NamedTemporaryFile(prefix="test_concurrent_store_", suffix=".dat", delete=False) as f:
            store_path = Path(f.name)

        lock_path = store_path.with_suffix(store_path.suffix + ".lock")
        fixture_files: list[tuple[int, Path, EventRecord]] = []
        base_ts = 1791454020000000000
        num_workers = 8

        try:
            retriever = JavaRetriever(store_path, mode="cli")

            # Prepare 8 unique fixture files with distinct sources and timestamps
            for worker_id in range(num_workers):
                f_path = store_path.parent / f"fixture_w{worker_id}_{int(time.time()*1000)}.bin"
                ev = EventRecord(
                    source_id=200 + worker_id,
                    timestamp_ns=base_ts + (worker_id * 1_000_000),
                    event_type=0x10,
                    payload_bytes=f"concurrent_payload_{worker_id}".encode("utf-8"),
                )
                f_path.write_bytes(encode_envelope(ev))
                fixture_files.append((worker_id, f_path, ev))

            errors: list[Exception] = []

            def worker_task(item: tuple[int, Path, EventRecord]) -> dict:
                wid, fp, expected_ev = item
                # 1. Ingest fixture
                retriever.ingest(fp)
                # 2. Interleaved bounded retrieval
                env = retriever.retrieve_envelope(
                    ALL,
                    base_ts,
                    base_ts + 100_000_000,
                    max_events=100,
                )
                return {
                    "worker_id": wid,
                    "retrieved_count": len(env.events),
                    "status": env.status,
                }

            # Run 8 concurrent worker threads
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(worker_task, item) for item in fixture_files]
                results = []
                for fut in futures:
                    try:
                        results.append(fut.result())
                    except Exception as exc:
                        errors.append(exc)

            # Verification 1: Zero OverlappingFileLockException or lock errors
            for err in errors:
                self.assertNotIn("OverlappingFileLockException", str(err))
            self.assertEqual(len(errors), 0, f"Encountered concurrency exceptions: {errors}")

            # Verification 2: Zero torn 64KB blocks (file size is an exact multiple of 65,536 bytes)
            file_size = store_path.stat().st_size
            self.assertGreater(file_size, 0)
            self.assertEqual(
                file_size % BLOCK_SIZE,
                0,
                f"Torn 64KB block detected! File size {file_size} is not a multiple of {BLOCK_SIZE}",
            )

            # Verification 3: 100% read-back integrity across all 8 ingested records
            final_env = retriever.retrieve_envelope(
                ALL,
                base_ts,
                base_ts + 100_000_000,
                max_events=100,
            )
            self.assertEqual(final_env.status, "OK")
            self.assertEqual(len(final_env.events), num_workers)

            retrieved_map = {ev.source_id: ev for ev in final_env.events}
            for wid, _, expected_ev in fixture_files:
                self.assertIn(expected_ev.source_id, retrieved_map)
                actual_ev = retrieved_map[expected_ev.source_id]
                self.assertEqual(actual_ev.timestamp_ns, expected_ev.timestamp_ns)
                self.assertEqual(actual_ev.payload_bytes, expected_ev.payload_bytes)

        finally:
            store_path.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)
            for _, fp, _ in fixture_files:
                fp.unlink(missing_ok=True)

    def test_02_concurrent_read_readers_under_load(self):
        """Concurrent readers querying the same store during active reads do not conflict."""
        fixture_path = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"
        with tempfile.NamedTemporaryFile(prefix="test_read_store_", suffix=".dat", delete=False) as f:
            store_path = Path(f.name)
        lock_path = store_path.with_suffix(store_path.suffix + ".lock")

        try:
            retriever = JavaRetriever.from_fixtures(fixture_path, store_path=store_path)

            def reader_task(reader_id: int):
                return retriever.retrieve_envelope(
                    ALL,
                    1791454021200000000,
                    1791454024300000000,
                    max_events=50,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(reader_task, i) for i in range(8)]
                results = [f.result() for f in futures]

            for env in results:
                self.assertEqual(env.status, "OK")
                self.assertEqual(len(env.events), 9)
        finally:
            store_path.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)

    def test_segment_compaction(self):
        """DynamicSegmentCompactor correctly compacts sealed segments, recycles expired data, preserves block integrity and does not interfere with active operations."""
        with tempfile.NamedTemporaryFile(prefix="test_compaction_store_", suffix=".dat", delete=False) as f:
            store_path = Path(f.name)
        lock_path = store_path.with_suffix(store_path.suffix + ".lock")

        fixtures: list[Path] = []
        base_ts = 1791454020000000000

        try:
            retriever = JavaRetriever(store_path, mode="cli")

            # 1. Create 3 blocks for source 200 by writing 3 records each ~40KB (> 65536 / 2 bytes)
            # Blocks 0 and 1 will be sealed; Block 2 will be the active block.
            p1 = b"SEALED_EXPIRED_PAYLOAD_A" * 1500  # ~36 KB
            p2 = b"SEALED_RETAINED_PAYLOAD_B" * 1500 # ~36 KB
            p3 = b"ACTIVE_BLOCK_PAYLOAD_C" * 1500     # ~36 KB

            f1 = store_path.parent / f"fix_c1_{int(time.time()*1000)}.bin"
            f1.write_bytes(encode_envelope(EventRecord(200, base_ts, 1, p1)))
            fixtures.append(f1)

            f2 = store_path.parent / f"fix_c2_{int(time.time()*1000)}.bin"
            f2.write_bytes(encode_envelope(EventRecord(200, base_ts + 2000, 1, p2)))
            fixtures.append(f2)

            f3 = store_path.parent / f"fix_c3_{int(time.time()*1000)}.bin"
            f3.write_bytes(encode_envelope(EventRecord(200, base_ts + 4000, 1, p3)))
            fixtures.append(f3)

            retriever.ingest(f1, f2, f3)

            initial_size = store_path.stat().st_size
            self.assertEqual(
                initial_size % BLOCK_SIZE,
                0,
                f"Initial store size {initial_size} must be aligned to {BLOCK_SIZE} bytes",
            )
            self.assertEqual(initial_size // BLOCK_SIZE, 3, "Expected 3 allocated 64KB blocks")

            # 2. Run DynamicSegmentCompactor with cutoff = base_ts + 1000
            # f1 (ts = base_ts) is prior to cutoff -> expired and purged from sealed block.
            # f2 (ts = base_ts + 2000) >= cutoff -> retained in sealed block.
            # f3 (ts = base_ts + 4000) -> active block, preserved untouched.
            cutoff = base_ts + 1000
            cmd = [
                retriever.java_bin,
                "-cp",
                retriever.classpath,
                "Storage.DynamicSegmentCompactor",
                "compact",
                "--store",
                str(store_path),
                "--cutoff",
                str(cutoff),
            ]
            comp_res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            res_json = json.loads(comp_res.stdout.strip())

            self.assertEqual(res_json.get("status"), "OK")
            self.assertEqual(res_json.get("initial_blocks"), 3)
            self.assertEqual(res_json.get("compacted_blocks"), 2)
            self.assertEqual(res_json.get("bytes_reclaimed"), BLOCK_SIZE)
            self.assertEqual(res_json.get("records_preserved"), 2)
            self.assertEqual(res_json.get("segments_recycled"), 1)

            # 3. Verify block boundary integrity after compaction
            compacted_size = store_path.stat().st_size
            self.assertEqual(
                compacted_size % BLOCK_SIZE,
                0,
                f"Compacted store size {compacted_size} must be an exact multiple of {BLOCK_SIZE}",
            )
            self.assertEqual(compacted_size, 2 * BLOCK_SIZE)

            # 4. Read back and verify retention and CRC-32C integrity
            env = retriever.retrieve_envelope(200, base_ts, base_ts + 10000, max_events=10)
            self.assertEqual(env.status, "OK")
            self.assertEqual(len(env.events), 2)
            self.assertEqual(env.events[0].timestamp_ns, base_ts + 2000)
            self.assertEqual(env.events[0].payload_bytes, p2)
            self.assertEqual(env.events[1].timestamp_ns, base_ts + 4000)
            self.assertEqual(env.events[1].payload_bytes, p3)

            # 5. Non-interference with active operations: append a new record to the compacted store
            f4 = store_path.parent / f"fix_c4_{int(time.time()*1000)}.bin"
            p4 = b"POST_COMPACTION_ACTIVE_RECORD"
            f4.write_bytes(encode_envelope(EventRecord(200, base_ts + 6000, 1, p4)))
            fixtures.append(f4)

            retriever.ingest(f4)

            # Verify all unexpired plus newly appended records exist
            env_after = retriever.retrieve_envelope(200, base_ts, base_ts + 10000, max_events=10)
            self.assertEqual(env_after.status, "OK")
            self.assertEqual(len(env_after.events), 3)
            self.assertEqual(
                [e.timestamp_ns for e in env_after.events],
                [base_ts + 2000, base_ts + 4000, base_ts + 6000],
            )
            self.assertEqual(env_after.events[2].payload_bytes, p4)

        finally:
            store_path.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)
            for fix_p in fixtures:
                fix_p.unlink(missing_ok=True)
