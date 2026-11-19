"""Java-backed Retriever implementation for FSS Python pipeline.

Implements the fss_py.retriever.Retriever protocol by invoking the Java
BlockStore via JavaStoreBridge CLI subprocess, parsing stdout envelopes.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from fss_py.model import (
    ALL,
    EventRecord,
    RetrievalRequest,
    RetrievalRequestError,
    RetrievalResult,
    _AllSources,
)
from fss_py.wire import iter_envelopes


class JavaRetriever:
    """Retriever implementation that queries a Java BlockStore through JavaStoreBridge.

    Conforms to the Retriever Protocol:
        retrieve(source, t_start_ns, t_end_ns, *, max_events, event_type=None) -> RetrievalResult
    """

    def __init__(
        self,
        store_path: str | Path,
        *,
        classpath: str | None = None,
        java_bin: str = "java",
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        self.root = root

        store_p = Path(store_path)
        self.store_path = store_p if store_p.is_absolute() else (root / store_p).resolve()
        self.java_bin = java_bin

        if classpath is None:
            core_classes = root / "fss-core" / "target" / "classes"
            bridge_classes = root / "bridge" / "classes"
            self.classpath = f"{core_classes}:{bridge_classes}"
        else:
            resolved_entries = []
            for entry in classpath.split(":"):
                if entry:
                    ep = Path(entry)
                    resolved_entries.append(str(ep if ep.is_absolute() else (root / ep).resolve()))
            self.classpath = ":".join(resolved_entries)

    @classmethod
    def from_fixtures(
        cls,
        *fixture_paths: str | Path,
        store_path: str | Path | None = None,
        classpath: str | None = None,
        java_bin: str = "java",
    ) -> JavaRetriever:
        """Create a JavaRetriever and ingest one or more fixture files into its store."""
        if store_path is None:
            tmp = tempfile.NamedTemporaryFile(prefix="fss_store_", suffix=".dat", delete=False)
            tmp.close()
            s_path = Path(tmp.name)
        else:
            s_path = Path(store_path)

        retriever = cls(s_path, classpath=classpath, java_bin=java_bin)
        if fixture_paths:
            retriever.ingest(*fixture_paths)
        return retriever

    def ingest(self, *fixture_paths: str | Path) -> None:
        """Ingest wire envelope (.bin) fixtures into this retriever's BlockStore."""
        cmd = [
            self.java_bin,
            "-cp",
            self.classpath,
            "bridge.JavaStoreBridge",
            "ingest",
            "--store",
            str(self.store_path),
        ]
        for p in fixture_paths:
            fp = Path(p)
            resolved_fp = fp if fp.is_absolute() else (self.root / fp).resolve()
            cmd.extend(["--fixture", str(resolved_fp)])

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            cwd=str(self.root),
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"JavaStoreBridge ingest failed (code {proc.returncode}):\n{proc.stderr}"
            )

    def retrieve(
        self,
        source: int | _AllSources,
        t_start_ns: int,
        t_end_ns: int,
        *,
        max_events: int,
        event_type: int | None = None,
    ) -> RetrievalResult:
        """Bounded retrieval against the Java BlockStore."""
        # Contract validation: raises RetrievalRequestError if bounded window or max_events is violated
        request = RetrievalRequest(
            source=source,
            t_start_ns=t_start_ns,
            t_end_ns=t_end_ns,
            max_events=max_events,
            event_type=event_type,
        )

        source_arg = "ALL" if source is ALL else str(source)
        cmd = [
            self.java_bin,
            "-cp",
            self.classpath,
            "bridge.JavaStoreBridge",
            "retrieve",
            "--store",
            str(self.store_path),
            "--source",
            source_arg,
            "--start",
            str(t_start_ns),
            "--end",
            str(t_end_ns),
            "--max-events",
            str(max_events),
        ]
        if event_type is not None:
            cmd.extend(["--event-type", str(event_type)])

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            cwd=str(self.root),
        )
        if proc.returncode != 0:
            err_msg = proc.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"JavaStoreBridge retrieve failed with exit code {proc.returncode}:\n{err_msg}"
            )

        raw = proc.stdout
        # Robust JSON metadata header search: find line starting with b'{"truncated"'
        header_start = raw.find(b'{"truncated"')
        if header_start == -1:
            err_msg = proc.stderr.decode("utf-8", errors="replace")
            stdout_sample = raw[:500].decode("utf-8", errors="replace")
            raise RuntimeError(
                f"JavaStoreBridge output missing JSON metadata header:\nstdout:\n{stdout_sample}\nstderr:\n{err_msg}"
            )

        nl_pos = raw.find(b"\n", header_start)
        if nl_pos == -1:
            err_msg = proc.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"JavaStoreBridge output missing metadata header newline:\n{err_msg}"
            )

        header_bytes = raw[header_start:nl_pos]
        envelope_bytes = raw[nl_pos + 1 :]

        try:
            meta = json.loads(header_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Malformed metadata header from JavaStoreBridge: {header_bytes!r}") from exc

        truncated = bool(meta.get("truncated", False))
        events = list(iter_envelopes(envelope_bytes))

        return RetrievalResult(request=request, events=events, truncated=truncated)
