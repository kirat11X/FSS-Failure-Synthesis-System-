"""Local hybrid retrieval over documents stored in the repository knowledge_base."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"

import bm25s
import numpy as np

from bridge import knowledge_base


def extract_search_terms(text: str) -> list[str]:
    """Tokenize text while retaining numeric, hexadecimal, and underscore terms."""
    return [token.lower() for token in re.findall(r"0x[0-9a-fA-F]+|[A-Za-z0-9_]+", text)]


class LocalDenseEmbedder:
    """Generate local dense vectors, with a deterministic offline fallback."""

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        dim: int = 128,
    ) -> None:
        self.model_name = model_name
        self.dim = dim
        self._st_model: Any = None
        self._st_attempted = False

    def _try_load_st(self) -> Any:
        if self._st_attempted:
            return self._st_model
        self._st_attempted = True
        try:
            import sentence_transformers

            self._st_model = sentence_transformers.SentenceTransformer(
                self.model_name, device="cpu"
            )
        except Exception:
            self._st_model = None
        return self._st_model

    def encode(self, texts: list[str]) -> np.ndarray:
        model = self._try_load_st()
        if model is not None:
            try:
                embeddings = model.encode(
                    texts, convert_to_numpy=True, show_progress_bar=False
                )
                norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                return embeddings / norms
            except Exception:
                pass

        vectors = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            vector = np.zeros(self.dim, dtype=np.float32)
            for term in extract_search_terms(text):
                digest = int(hashlib.md5(term.encode("utf-8")).hexdigest(), 16)
                vector[digest % self.dim] += 1.0 if (digest >> 8) & 1 else -1.0
                for index in range(max(0, len(term) - 2)):
                    gram = term[index : index + 3]
                    gram_digest = int(hashlib.sha256(gram.encode("utf-8")).hexdigest(), 16)
                    vector[gram_digest % self.dim] += (
                        0.5 if (gram_digest >> 8) & 1 else -0.5
                    )
            norm = np.linalg.norm(vector)
            vectors[row] = vector / (norm if norm else 1.0)
        return vectors


class HybridRetriever:
    """Hybrid lexical and dense retrieval over one knowledge-base directory."""

    _instance: HybridRetriever | None = None
    _knowledge_base_fingerprint: tuple[tuple[str, int, int], ...] | None = None

    @classmethod
    def get_instance(cls) -> HybridRetriever:
        fingerprint = knowledge_base.knowledge_base_fingerprint()
        if cls._instance is None or cls._knowledge_base_fingerprint != fingerprint:
            cls._instance = cls(knowledge_base_dir=knowledge_base.KNOWLEDGE_BASE_DIR)
            cls._knowledge_base_fingerprint = fingerprint
        return cls._instance

    def __init__(self, knowledge_base_dir: str | Path | None = None) -> None:
        root = knowledge_base_dir or knowledge_base.KNOWLEDGE_BASE_DIR
        self.corpus = knowledge_base.load_knowledge_base(root)
        self.embedder = LocalDenseEmbedder()
        self.bm25_model: Any = None
        self.corpus_texts: list[str] = []
        self.corpus_embeddings = np.empty((0, self.embedder.dim), dtype=np.float32)
        if self.corpus:
            self.corpus_texts = [
                " ".join(
                    str(doc.get(key, ""))
                    for key in ("title", "explains", "content")
                )
                for doc in self.corpus
            ]
            tokenized_corpus = [extract_search_terms(text) for text in self.corpus_texts]
            self.bm25_model = bm25s.BM25()
            self.bm25_model.index(tokenized_corpus, show_progress=False)
            self.corpus_embeddings = self.embedder.encode(self.corpus_texts)

    def retrieve_knowledge(
        self,
        query: str,
        evidence: dict[str, Any] | None = None,
        top_k: int = 8,
        rrf_k: int = 60,
    ) -> list[dict[str, Any]]:
        """Rank only knowledge-base documents against the query and observed telemetry."""
        if not self.corpus or top_k <= 0:
            return []

        query_terms = extract_search_terms(query)
        if evidence:
            for event in evidence.get("events", []):
                for key in ("event_name", "event_type"):
                    value = event.get(key)
                    if value is not None:
                        query_terms.extend(extract_search_terms(str(value)))
                for value in event.get("fields", {}).values():
                    if isinstance(value, (str, int, float)):
                        query_terms.extend(extract_search_terms(str(value)))
        expanded_query = " ".join(dict.fromkeys(query_terms))
        query_tokens = [extract_search_terms(expanded_query)]
        bm25_results, _ = self.bm25_model.retrieve(
            query_tokens,
            k=min(len(self.corpus), 30),
            show_progress=False,
        )
        bm25_ranks = {
            int(doc_index): rank
            for rank, doc_index in enumerate(bm25_results[0], start=1)
        }

        query_embedding = self.embedder.encode([expanded_query])[0]
        dense_indices = np.argsort(-np.dot(self.corpus_embeddings, query_embedding))
        dense_ranks = {
            int(doc_index): rank
            for rank, doc_index in enumerate(dense_indices, start=1)
        }

        scores: list[tuple[int, float]] = []
        for index in set(bm25_ranks) | set(dense_ranks):
            score = 1.0 / (rrf_k + bm25_ranks.get(index, 1000))
            score += 1.0 / (rrf_k + dense_ranks.get(index, 1000))
            scores.append((index, score))
        scores.sort(key=lambda result: result[1], reverse=True)
        return [self.corpus[index] for index, _ in scores[:top_k]]


def retrieve_hybrid_knowledge(
    query: str,
    evidence: dict[str, Any] | None = None,
    top_k: int = 8,
) -> list[dict[str, Any]]:
    """Retrieve domain references exclusively from the repository knowledge_base."""
    return HybridRetriever.get_instance().retrieve_knowledge(
        query, evidence=evidence, top_k=top_k
    )


HybridRAGRetriever = HybridRetriever
