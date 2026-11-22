"""Hybrid RAG (BM25S + Dense + RRF + Reranker) & Historical RCA Exemplars for FSS.

Combines:
  1. Lexical retrieval via bm25s over domain docs and error codes (handling exact
     alphanumeric strings like '0x10', 'LIDAR_STATUS', 'COLLISION_RISK', '17', '18', '19').
  2. Local dense semantic embeddings (sentence-transformers or deterministic local embedding)
     merged via Reciprocal Rank Fusion (RRF, k=60).
  3. Local reranker (cross-encoder or calibrated score weighting) selecting top 5-8 chunks.
  4. RCACopilot-pattern historical resolved incidents index/store:
     Retrieves K = 3-15 condensed diagnostic exemplars for few-shot Chain-of-Thought (CoT).
  5. Multi-vendor error catalog indexing and evidence enrichment.
"""

from __future__ import annotations

import os

# CRITICAL: Prevent JAX from preallocating GPU VRAM before bm25s import
os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

import bm25s
import numpy as np

from bridge.error_catalog import (
    VendorErrorEntry,
    extract_error_codes_from_events,
    load_builtin_manual,
)

# ---------------------------------------------------------------------------
# Domain Knowledge Base (Runbooks, Error Codes, Fleet Specs)
# ---------------------------------------------------------------------------

DOMAIN_KNOWLEDGE: list[dict[str, str]] = [
    {
        "id": "d1_drone_fleet/avoidance",
        "chunk": "d1_drone_fleet/avoidance#1",
        "kind": "manual",
        "domain": "d1_drone_fleet",
        "title": "Collision avoidance",
        "explains": "AVOIDANCE, COLLISION_RISK, 0x20",
        "content": (
            "AVOIDANCE (event type 0x20) is logged when the flight controller overrides the planned path. "
            "COLLISION_RISK (reason_code 1) means another object came closer than the safety separation; "
            "min_separation_m is the closest distance observed and heading_change_deg the commanded turn. "
            "Reason code 2 corresponds to GEOFENCE and reason code 3 to OPERATOR_COMMAND."
        ),
    },
    {
        "id": "d1_drone_fleet/formation",
        "chunk": "d1_drone_fleet/formation#1",
        "kind": "state-description",
        "domain": "d1_drone_fleet",
        "title": "Leaving formation",
        "explains": "LEFT_FORMATION, FORMATION_STATE, 0x21, REJOINING, IN_FORMATION",
        "content": (
            "A drone reports FORMATION_STATE (event type 0x21). It reports LEFT_FORMATION (state_code 1) "
            "when it can no longer hold its assigned slot, for example after an avoidance manoeuvre. "
            "It then tries to rejoin (REJOINING, state_code 2). State code 0 represents IN_FORMATION."
        ),
    },
    {
        "id": "d1_drone_fleet/lidar_timeout",
        "chunk": "d1_drone_fleet/lidar_timeout#1",
        "kind": "failure-mode",
        "domain": "d1_drone_fleet",
        "title": "LiDAR timeout",
        "explains": "LIDAR_TIMEOUT, 17, 0x10, timeout_count",
        "content": (
            "A LiDAR timeout (status_code 17, LIDAR_TIMEOUT) means the expected scan was not received "
            "within the configured timeout interval. timeout_count is the number of consecutive missed scans. "
            "While scans are missing, the drone's obstacle map is stale and its position-keeping in a formation can drift."
        ),
    },
    {
        "id": "d1_drone_fleet/diagnostics",
        "chunk": "d1_drone_fleet/diagnostics#1",
        "kind": "state-description",
        "domain": "d1_drone_fleet",
        "title": "Diagnostic status",
        "explains": "DEGRADED, FAULT, NOMINAL, DIAGNOSTIC, 0x30",
        "content": (
            "DIAGNOSTIC (event type 0x30) reports subsystem health. NOMINAL (status_code 0) indicates normal operation. "
            "DEGRADED (status_code 1) means a subsystem is working with reduced capability. FAULT (status_code 2) "
            "means a subsystem has failed. The vendor-specific bytes after temperature_c are not documented and "
            "must be treated as opaque data gaps."
        ),
    },
    {
        "id": "d1_drone_fleet/lidar_error_codes",
        "chunk": "d1_drone_fleet/lidar_error_codes#1",
        "kind": "error-code-table",
        "domain": "d1_drone_fleet",
        "title": "LiDAR status error codes",
        "explains": "LIDAR_STATUS, 0x10, 0, 17, 18, 19, SENSOR_DISCONNECTED, INVALID_SCAN",
        "content": (
            "LIDAR_STATUS (event type 0x10) reports the LiDAR driver's health as status_code. "
            "Code 0 is OK. Codes 17 to 19 are fault codes: 17 is a scan timeout (LIDAR_TIMEOUT), "
            "18 is a lost connection to the sensor (SENSOR_DISCONNECTED), 19 is a scan that failed its integrity checks (INVALID_SCAN)."
        ),
    },
    {
        "id": "d1_drone_fleet/clock_skew_policy",
        "chunk": "d1_drone_fleet/clock_skew_policy#1",
        "kind": "architecture-rule",
        "domain": "d1_drone_fleet",
        "title": "Multi-Source Clock Synchronization Policy",
        "explains": "clock_caveat, unsynchronized, timestamps, causality",
        "content": (
            "Producer clocks are NOT synchronized across sources. Timestamps from different drone nodes "
            "cannot be strictly compared using integer order to establish causality. Any cross-source "
            "relationship must be stated as an inferred or plausible hypothesis with explicit uncertainty caveats."
        ),
    },
    {
        "id": "d1_drone_fleet/opaque_data_gaps",
        "chunk": "d1_drone_fleet/opaque_data_gaps#1",
        "kind": "protocol-rule",
        "domain": "d1_drone_fleet",
        "title": "Data Gaps and Opaque Bytes Quarantine",
        "explains": "data_gaps, OPAQUE_BYTES, UNKNOWN_EVENT_TYPE, 0x31, MALFORMED_PAYLOAD",
        "content": (
            "Whenever telemetry contains undecoded or trailing vendor bytes (such as in DIAGNOSTIC 0x30 events), "
            "or unconfigured event types (like 0x31), or malformed frames, these bytes must never be guessed "
            "or confabulated. They must be reported strictly under data_gaps as opaque bytes."
        ),
    },
    {
        "id": "d1_drone_fleet/formation_collision_dynamics",
        "chunk": "d1_drone_fleet/formation_collision_dynamics#1",
        "kind": "root-cause-pattern",
        "domain": "d1_drone_fleet",
        "title": "Formation Drift and Collision Risk Sequence",
        "explains": "formation drift, cascade, LIDAR_TIMEOUT, AVOIDANCE, COLLISION_RISK, LEFT_FORMATION",
        "content": (
            "In multi-drone formation flight, when an upstream drone experiences LiDAR scan timeouts (LIDAR_STATUS code 17), "
            "its internal Kalman position filter drifts due to lack of ranging data. Adjacent slot drones detect "
            "a proximity violation, issuing AVOIDANCE manoeuvres for COLLISION_RISK, followed by leaving formation (LEFT_FORMATION)."
        ),
    },
]

# ---------------------------------------------------------------------------
# Historical Resolved Incidents Store (RCACopilot Pattern)
# ---------------------------------------------------------------------------

@dataclass
class HistoricalIncident:
    incident_id: str
    title: str
    symptoms: str
    root_cause: str
    resolution: str
    contributing_event_types: list[str]
    domain: str
    condensed_cot: str

    def to_doc(self) -> dict[str, str]:
        return {
            "id": f"incident/{self.incident_id}",
            "chunk": f"incident/{self.incident_id}#1",
            "kind": "historical-incident",
            "domain": self.domain,
            "title": f"Incident {self.incident_id}: {self.title}",
            "explains": ", ".join(self.contributing_event_types),
            "content": (
                f"INCIDENT {self.incident_id}: {self.title}\n"
                f"Symptoms: {self.symptoms}\n"
                f"Root Cause: {self.root_cause}\n"
                f"Resolution: {self.resolution}\n"
                f"Few-Shot Chain of Thought: {self.condensed_cot}"
            ),
        }


HISTORICAL_INCIDENTS: list[HistoricalIncident] = [
    HistoricalIncident(
        incident_id="INC-2026-0811",
        title="LiDAR Scan Timeout on Drone 298 Leading to Drone 474 Evasive Action",
        symptoms="Drone 298 reported LIDAR_TIMEOUT (code 17) with timeout_count=3. Simultaneously, Drone 474 issued AVOIDANCE for COLLISION_RISK and LEFT_FORMATION.",
        root_cause="Stale obstacle map on Drone 298 led to formation slot drift into adjacent corridor, forcing Drone 474 to execute automated safety avoidance.",
        resolution="Reset LiDAR bus driver on 298; commanded Drone 474 to REJOINING formation once separation was restored.",
        contributing_event_types=["LIDAR_STATUS", "AVOIDANCE", "FORMATION_STATE", "0x10", "0x20", "0x21"],
        domain="d1_drone_fleet",
        condensed_cot=(
            "1. Inspect Drone 298 status: status_code=17 (LIDAR_TIMEOUT), timeout_count=3. "
            "2. Inspect Drone 474 status: AVOIDANCE manoeuvre executed (reason=COLLISION_RISK), followed by LEFT_FORMATION. "
            "3. Assess multi-source clock caveat: Clocks across 298 and 474 are unsynchronized, so exact nanosecond ordering is inferred. "
            "4. Domain manual d1_drone_fleet/lidar_timeout#1 establishes that missed scans cause position drift. "
            "5. Synthesize likely cause: Drone 298's drift plausibly prompted Drone 474's collision avoidance."
        ),
    ),
    HistoricalIncident(
        incident_id="INC-2026-0812",
        title="LiDAR Physical Sensor Disconnection (Status 18) During High Vibration Flight",
        symptoms="Drone 105 emitted LIDAR_STATUS code 18 (SENSOR_DISCONNECTED) and zero timeout_count.",
        root_cause="Cable harness unseated during turbulent flight segment, terminating serial communications between flight controller and LiDAR.",
        resolution="Drone entered safe loiter mode; ground crew reseated hardware connector and replaced mounting dampers.",
        contributing_event_types=["LIDAR_STATUS", "0x10", "SENSOR_DISCONNECTED"],
        domain="d1_drone_fleet",
        condensed_cot=(
            "1. Identify LIDAR_STATUS with status_code=18. "
            "2. Consult error code table: code 18 signifies SENSOR_DISCONNECTED (hardware communication loss). "
            "3. Rule out software timeout (code 17) or invalid scan (code 19). "
            "4. Conclude root cause is physical link disconnection."
        ),
    ),
    HistoricalIncident(
        incident_id="INC-2026-0813",
        title="Diagnostic Degraded Status with Opaque Vendor Bytes",
        symptoms="Drone 312 logged DIAGNOSTIC (0x30) status_code=1 (DEGRADED), temperature_c=68.5, followed by 8 undocumented trailing bytes.",
        root_cause="Subsystem thermal throttling under sustained high ambient temperature. Trailing vendor payload unmapped in drone_v0 schema.",
        resolution="Throttled propulsion power; quarantined the 8 opaque payload bytes under data_gaps as required by protocol.",
        contributing_event_types=["DIAGNOSTIC", "0x30", "DEGRADED"],
        domain="d1_drone_fleet",
        condensed_cot=(
            "1. Check DIAGNOSTIC status_code=1 -> DEGRADED. "
            "2. Note temperature_c=68.5 indicates thermal elevation. "
            "3. Notice PARTIALLY_DECODED status with 8 unknown bytes. "
            "4. Enforce Hard Rule 12: do not guess payload bytes; log as opaque data gap. "
            "5. Conclude subsystem degradation with thermal contributing factor."
        ),
    ),
    HistoricalIncident(
        incident_id="INC-2026-0814",
        title="Geofence Boundary Avoidance Without Slot Departure",
        symptoms="Drone 204 logged AVOIDANCE with reason_code=2 (GEOFENCE), heading_change_deg=45, min_separation_m=12.0. No LEFT_FORMATION.",
        root_cause="Flight perimeter warning line approached due to wind shear, triggering proactive heading change.",
        resolution="Drone remained in formation after corrective turn.",
        contributing_event_types=["AVOIDANCE", "0x20", "GEOFENCE"],
        domain="d1_drone_fleet",
        condensed_cot=(
            "1. Check AVOIDANCE reason_code=2 -> GEOFENCE. "
            "2. Observe min_separation_m is large (12.0 m), confirming no inter-drone collision risk. "
            "3. Absence of LEFT_FORMATION confirms slot retention."
        ),
    ),
    HistoricalIncident(
        incident_id="INC-2026-0815",
        title="Invalid Scan Integrity Failure (Status 19) in Optical Glare",
        symptoms="Drone 410 logged LIDAR_STATUS code 19 (INVALID_SCAN) for 2 consecutive cycles.",
        root_cause="Direct solar incidence blinded optical receiver, failing scan CRC integrity checks.",
        resolution="Optical filter recalibrated; drone maintained formation via backup IMU dead reckoning.",
        contributing_event_types=["LIDAR_STATUS", "0x10", "INVALID_SCAN"],
        domain="d1_drone_fleet",
        condensed_cot=(
            "1. Read status_code=19 -> INVALID_SCAN. "
            "2. Distinguish from timeout (17) and disconnect (18): packets arrived but failed checksum/range filters. "
            "3. Validate against error code table."
        ),
    ),
    HistoricalIncident(
        incident_id="INC-2026-0816",
        title="Critical Diagnostic FAULT (Status 2) Requiring Emergency Egress",
        symptoms="Drone 501 logged DIAGNOSTIC status_code=2 (FAULT), followed immediately by LEFT_FORMATION.",
        root_cause="Internal motor controller FET breakdown triggering hardware safety interlock.",
        resolution="Emergency landing at nearest waypoint; drone grounded for repairs.",
        contributing_event_types=["DIAGNOSTIC", "FORMATION_STATE", "0x30", "0x21", "FAULT", "LEFT_FORMATION"],
        domain="d1_drone_fleet",
        condensed_cot=(
            "1. DIAGNOSTIC status_code=2 is FAULT (highest severity). "
            "2. Immediate LEFT_FORMATION confirms emergency egress. "
            "3. Attribute cause directly to internal subsystem fault on drone 501."
        ),
    ),
    HistoricalIncident(
        incident_id="INC-2026-0817",
        title="Unsynchronized Multi-Drone Telemetry with Clock Skew",
        symptoms="Drones 101 and 102 reported telemetry where timestamp order differed by 12 seconds from physical radio arrival.",
        root_cause="Producer clocks drifting independently without PTP synchronization across mobile nodes.",
        resolution="Enforced FSS Rule 4: never assert inter-source causality from raw timestamps; report explicit clock caveat.",
        contributing_event_types=["clock_caveat", "timestamp_ns"],
        domain="d1_drone_fleet",
        condensed_cot=(
            "1. Multiple sources present in evidence. "
            "2. Re-verify clock caveat: producer clocks are not synchronized. "
            "3. Hedge all inter-node causal claims with 'plausibly' or 'inferred'."
        ),
    ),
    HistoricalIncident(
        incident_id="INC-2026-0818",
        title="Retrieval Truncated on High-Volume Telemetry Burst",
        symptoms="Query returned exactly max_events=100 with retrieval.truncated=True; potential dropped events.",
        root_cause="Search window was wider than fleet event density allowed under bounded memory budget.",
        resolution="Data gap RETRIEVAL_TRUNCATED logged; operator prompted to narrow time window.",
        contributing_event_types=["RETRIEVAL_TRUNCATED", "data_gaps"],
        domain="d1_drone_fleet",
        condensed_cot=(
            "1. Detect retrieval.truncated is True. "
            "2. Highlight data gap: further contributing events may exist outside the 100 returned events. "
            "3. State confidence as medium or low due to incomplete window."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Alphanumeric Tokenizer & Exact Matching
# ---------------------------------------------------------------------------

def extract_search_terms(text: str) -> list[str]:
    """Extract alphanumerics, hex codes (0x..), and domain tokens preserving exact cases & numbers."""
    pattern = re.compile(r"0x[0-9a-fA-F]+|[A-Za-z0-9_]+|\b\d+\b")
    tokens = pattern.findall(text)
    # Return normalized lowercase tokens while preserving exact hex representation
    return [t.lower() for t in tokens if len(t) > 0]


# ---------------------------------------------------------------------------
# Dense Semantic Embedding (Local / Offline Robust)
# ---------------------------------------------------------------------------

class LocalDenseEmbedder:
    """Provides dense semantic vectors.

    Attempts sentence-transformers (all-MiniLM-L6-v2) first; if unavailable
    or offline, falls back to a deterministic semantic projection embedding.
    """

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2", dim: int = 128) -> None:
        self.model_name = model_name
        self.dim = dim
        self._st_model: Any = None
        self._st_attempted: bool = False

    def _try_load_st(self) -> Any:
        if self._st_attempted:
            return self._st_model
        self._st_attempted = True
        try:
            import sentence_transformers
            self._st_model = sentence_transformers.SentenceTransformer(self.model_name, device="cpu")
        except Exception:
            self._st_model = None
        return self._st_model

    def encode(self, texts: list[str]) -> np.ndarray:
        model = self._try_load_st()
        if model is not None:
            try:
                embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
                # Normalize
                norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                return embeddings / norms
            except Exception:
                pass

        # Deterministic semantic hash projection (offline fallback)
        vectors = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            words = extract_search_terms(text)
            if not words:
                continue
            vec = np.zeros(self.dim, dtype=np.float32)
            for w in words:
                # 3-gram character hashes for semantic robustness
                h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
                idx = h % self.dim
                sign = 1.0 if ((h >> 8) & 1) else -1.0
                vec[idx] += sign

                # Substring character grams for partial word overlap
                for c_idx in range(len(w) - 2):
                    sub = w[c_idx : c_idx + 3]
                    sub_h = int(hashlib.sha256(sub.encode("utf-8")).hexdigest(), 16)
                    vec[sub_h % self.dim] += 0.5 * (1.0 if ((sub_h >> 8) & 1) else -1.0)

            norm = np.linalg.norm(vec)
            vectors[i] = vec / (norm if norm > 0 else 1.0)
        return vectors


# ---------------------------------------------------------------------------
# Hybrid Retriever (BM25S + Dense + RRF + Reranker)
# ---------------------------------------------------------------------------

class HybridRetriever:
    """Local hybrid retriever combining BM25S lexical search and Dense embeddings via RRF (k=60).

    Supports dual-source indexing: default DOMAIN_KNOWLEDGE and dynamic VendorErrorEntry chunks.
    """

    _instance: HybridRetriever | None = None
    _knowledge_base_fingerprint: tuple[tuple[str, int, int], ...] | None = None

    @classmethod
    def get_instance(cls) -> HybridRetriever:
        from bridge.knowledge_base import knowledge_base_fingerprint, load_knowledge_base

        fingerprint = knowledge_base_fingerprint()
        if cls._instance is None or cls._knowledge_base_fingerprint != fingerprint:
            corpus = [dict(doc) for doc in DOMAIN_KNOWLEDGE]
            corpus.extend(load_knowledge_base())
            cls._instance = cls(
                corpus=corpus,
                vendor_entries=load_builtin_manual("auto"),
            )
            cls._knowledge_base_fingerprint = fingerprint
        return cls._instance

    def __init__(
        self,
        corpus: list[dict[str, str]] | None = None,
        vendor_entries: Sequence[VendorErrorEntry] | None = None,
    ) -> None:
        self.corpus: list[dict[str, str]] = list(corpus) if corpus is not None else [dict(d) for d in DOMAIN_KNOWLEDGE]
        self.embedder = LocalDenseEmbedder()
        if vendor_entries:
            for entry in vendor_entries:
                self._add_vendor_entry_doc(entry)
        self._init_indices()

    def _add_vendor_entry_doc(self, entry: VendorErrorEntry) -> None:
        doc = entry.to_doc()
        doc_id = doc.get("id") or f"err-{entry.vendor}-{entry.error_code}"
        doc["id"] = doc_id
        doc["chunk"] = f"{doc_id}#1"

        # Check if already in corpus
        for idx, existing in enumerate(self.corpus):
            if existing.get("id") == doc_id:
                self.corpus[idx] = doc
                return
        self.corpus.append(doc)

    def index_vendor_manual(self, entries: Sequence[VendorErrorEntry]) -> None:
        """Dynamically index vendor manual entries as first-class RAG chunks."""
        for entry in entries:
            self._add_vendor_entry_doc(entry)
        self._init_indices()

    def load_builtin_vendor_manuals(self, vendor_name: str = "auto") -> None:
        """Load and index built-in vendor reference manuals."""
        entries = load_builtin_manual(vendor_name)
        self.index_vendor_manual(entries)

    def _init_indices(self) -> None:
        """Build BM25S index and pre-compute dense embeddings for the corpus."""
        # 1. Corpus text preparation
        self.corpus_texts = [
            f"{doc.get('title', '')} {doc.get('explains', '')} {doc.get('content', '')}"
            for doc in self.corpus
        ]

        # 2. BM25S Lexical indexing
        tokenized_corpus = [extract_search_terms(t) for t in self.corpus_texts]
        self.bm25_model = bm25s.BM25()
        self.bm25_model.index(tokenized_corpus, show_progress=False)

        # 3. Dense embeddings pre-computation
        self.corpus_embeddings = self.embedder.encode(self.corpus_texts)

    def retrieve_knowledge(
        self,
        query: str,
        evidence: dict[str, Any] | None = None,
        top_k: int = 8,
        rrf_k: int = 60,
    ) -> list[dict[str, str]]:
        """Retrieve top_k domain knowledge chunks via BM25S + Dense + RRF + Reranker."""
        # Augment query with terms from evidence anomalies, error codes, and event names
        expanded_query_terms: list[str] = extract_search_terms(query)
        evidence_error_codes: list[str] = []
        if evidence:
            ev_list = evidence.get("events", [])
            evidence_error_codes = extract_error_codes_from_events(ev_list)
            for code in evidence_error_codes:
                expanded_query_terms.extend(extract_search_terms(code))
            for ev in ev_list:
                if ev.get("event_name"):
                    expanded_query_terms.extend(extract_search_terms(ev["event_name"]))
                if ev.get("event_type"):
                    expanded_query_terms.extend(extract_search_terms(str(ev["event_type"])))
                for k, v in ev.get("fields", {}).items():
                    if isinstance(v, (str, int)):
                        expanded_query_terms.extend(extract_search_terms(str(v)))

        expanded_query = " ".join(dict.fromkeys(expanded_query_terms))
        n_docs = len(self.corpus)
        if n_docs == 0:
            return []

        # 1. BM25S Lexical Ranking
        query_tokens = [extract_search_terms(expanded_query)]
        bm25_res, bm25_scores = self.bm25_model.retrieve(query_tokens, k=min(n_docs, 30), show_progress=False)
        bm25_indices = bm25_res[0] if len(bm25_res) > 0 else []

        bm25_rank_map: dict[int, int] = {}
        for rank, doc_idx in enumerate(bm25_indices):
            bm25_rank_map[int(doc_idx)] = rank + 1

        # 2. Dense Semantic Ranking
        query_emb = self.embedder.encode([expanded_query])[0]
        # Cosine similarities
        dense_scores = np.dot(self.corpus_embeddings, query_emb)
        dense_indices = np.argsort(-dense_scores)

        dense_rank_map: dict[int, int] = {}
        for rank, doc_idx in enumerate(dense_indices):
            dense_rank_map[int(doc_idx)] = rank + 1

        # 3. Reciprocal Rank Fusion (RRF, k=60)
        # RRF_score(d) = sum_{l in {BM25, Dense}} 1 / (k + rank_l(d))
        rrf_scores: dict[int, float] = {}
        all_candidates = set(bm25_rank_map.keys()) | set(dense_rank_map.keys())

        for idx in all_candidates:
            b_rank = bm25_rank_map.get(idx, 1000)
            d_rank = dense_rank_map.get(idx, 1000)
            score = (1.0 / (rrf_k + b_rank)) + (1.0 / (rrf_k + d_rank))
            rrf_scores[idx] = score

        # 4. Local Reranker (Score Weighting with Alphanumeric Error-Code Prioritization)
        reranked_scores: list[tuple[int, float]] = []
        raw_query_tokens = set(extract_search_terms(query))
        evidence_critical_terms = set(expanded_query_terms)
        norm_evidence_codes = {c.lower() for c in evidence_error_codes}

        for idx, base_rrf in rrf_scores.items():
            doc = self.corpus[idx]
            doc_text = self.corpus_texts[idx].lower()
            doc_id = doc.get("id", "").lower()
            doc_explains = doc.get("explains", "").lower()

            # Boost exact alphanumeric matches (e.g. '0x10', '17', 'LIDAR_TIMEOUT', 'COLLISION_RISK')
            alpha_boost = 0.0
            for term in evidence_critical_terms:
                if term in doc_text:
                    if term.startswith("0x") or term.isdigit() or "_" in term:
                        alpha_boost += 0.05
                    else:
                        alpha_boost += 0.01

            # Exact error code match boost from evidence
            error_code_boost = 0.0
            for code in norm_evidence_codes:
                if code in doc_id or code in doc_explains or f" {code} " in f" {doc_text} ":
                    error_code_boost += 0.15

            # Exact query title bonus
            title_boost = 0.05 if any(t in doc.get("title", "").lower() for t in raw_query_tokens) else 0.0

            final_score = base_rrf + alpha_boost + error_code_boost + title_boost
            reranked_scores.append((idx, final_score))

        # Sort by final reranked score descending
        reranked_scores.sort(key=lambda x: x[1], reverse=True)
        top_indices = [idx for idx, _ in reranked_scores[:top_k]]

        return [self.corpus[i] for i in top_indices]


# ---------------------------------------------------------------------------
# Historical Incidents Index & RCACopilot Exemplar Retrieval
# ---------------------------------------------------------------------------

class HistoricalIncidentsStore:
    """Store of resolved historical incidents for few-shot Chain-of-Thought exemplars."""

    _instance: HistoricalIncidentsStore | None = None

    @classmethod
    def get_instance(cls) -> HistoricalIncidentsStore:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, incidents: list[HistoricalIncident] | None = None) -> None:
        self.incidents = incidents if incidents is not None else list(HISTORICAL_INCIDENTS)
        self.docs = [inc.to_doc() for inc in self.incidents]
        self.retriever = HybridRetriever(corpus=self.docs)

    def retrieve_exemplars(
        self,
        query: str,
        evidence: dict[str, Any] | None = None,
        k: int = 3,
    ) -> list[HistoricalIncident]:
        """Retrieve K (3 to 15) most relevant resolved historical incidents for few-shot CoT."""
        k = max(1, min(k, 15))
        matched_docs = self.retriever.retrieve_knowledge(query, evidence=evidence, top_k=k)
        matched_ids = {doc["id"].replace("incident/", ""): doc for doc in matched_docs}

        results: list[HistoricalIncident] = []
        for inc in self.incidents:
            if inc.incident_id in matched_ids:
                results.append(inc)

        # Fallback to first k incidents if none matched
        if not results:
            results = self.incidents[:k]

        return results[:k]

    def format_exemplars_for_prompt(self, exemplars: Sequence[HistoricalIncident]) -> str:
        """Format historical incident exemplars into few-shot Chain-of-Thought markdown."""
        if not exemplars:
            return ""

        lines = [
            "=== HISTORICAL RESOLVED INCIDENT EXEMPLARS (RCACopilot Few-Shot CoT) ===",
            "Use the diagnostic reasoning pattern of these resolved incidents as few-shot reference:",
            "",
        ]
        for inc in exemplars:
            lines.append(f"### Exemplar {inc.incident_id}: {inc.title}")
            lines.append(f"- **Symptoms**: {inc.symptoms}")
            lines.append(f"- **Contributing Types**: {', '.join(inc.contributing_event_types)}")
            lines.append(f"- **Chain of Thought**: {inc.condensed_cot}")
            lines.append(f"- **Resolved Cause**: {inc.root_cause}")
            lines.append(f"- **Resolution**: {inc.resolution}")
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Vendor Manual Evidence Enrichment
# ---------------------------------------------------------------------------

def enrich_evidence_with_manual(
    evidence: dict[str, Any] | list[Any],
    catalog: list[VendorErrorEntry],
) -> list[dict[str, Any]]:
    """Extract error codes from evidence['events'] (or evidence list), match against catalog, and return enriched fault descriptions.

    Ready to be injected into prompt context.
    """
    if not evidence or not catalog:
        return []

    if isinstance(evidence, list):
        events = evidence
    elif isinstance(evidence, dict):
        events = evidence.get("events", [])
    else:
        events = []
    extracted_codes = extract_error_codes_from_events(events)
    norm_codes = {c.strip().lower() for c in extracted_codes if c.strip()}

    matched: list[VendorErrorEntry] = []
    seen_ids: set[str] = set()

    def _match_entry(entry: VendorErrorEntry) -> bool:
        # Check error_code
        e_code = entry.error_code.strip().lower()
        if e_code in norm_codes:
            return True
        # Check aliases
        for alias in entry.aliases:
            if alias.strip().lower() in norm_codes:
                return True
        return False

    for entry in catalog:
        if _match_entry(entry):
            if entry.doc_chunk_id not in seen_ids:
                seen_ids.add(entry.doc_chunk_id)
                matched.append(entry)

    # Format enriched fault descriptions
    enriched: list[dict[str, Any]] = []
    for m in matched:
        enriched.append({
            "error_code": m.error_code,
            "vendor": m.vendor,
            "subsystem": m.subsystem,
            "severity": m.severity,
            "title": m.title,
            "description": m.description,
            "probable_causes": list(m.probable_causes),
            "recommended_actions": list(m.recommended_actions),
            "doc_chunk_id": m.doc_chunk_id,
        })

    return enriched


# ---------------------------------------------------------------------------
# Convenience Module-Level APIs
# ---------------------------------------------------------------------------

def retrieve_hybrid_knowledge(
    query: str,
    evidence: dict[str, Any] | None = None,
    top_k: int = 8,
) -> list[dict[str, str]]:
    """Retrieve top 5-8 knowledge chunks using Hybrid BM25S + Dense + RRF + Reranker."""
    return HybridRetriever.get_instance().retrieve_knowledge(query, evidence=evidence, top_k=top_k)


def retrieve_historical_exemplars(
    query: str,
    evidence: dict[str, Any] | None = None,
    k: int = 3,
) -> list[HistoricalIncident]:
    """Retrieve K (3 to 15) condensed diagnostic exemplars for few-shot CoT."""
    return HistoricalIncidentsStore.get_instance().retrieve_exemplars(query, evidence=evidence, k=k)


def format_exemplars_cot(exemplars: Sequence[HistoricalIncident]) -> str:
    """Format historical exemplars into prompt text."""
    return HistoricalIncidentsStore.get_instance().format_exemplars_for_prompt(exemplars)


# Convenient alias for external callers
HybridRAGRetriever = HybridRetriever
