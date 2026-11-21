#!/usr/bin/env python3
"""
scripts/search_differences.py
-----------------------------
Local search engine over differences.md, extracted research papers,
and repository backlog gap items.

Supports:
  - Free-text ranked search (BM25s with pure-Python fallback)
  - Structured filters: --status, --subsystem, --priority, --req, --only-empty-files
  - Structured JSON export: --json
"""
import os
import sys
import re
import json
import math
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent

# Set VRAM guard
os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"

@dataclass
class SearchDocument:
    doc_id: str
    source: str
    category: str  # requirement | drawback | empty_file | paper_section
    title: str
    subsystem: str
    status: str
    priority: str
    content: str
    file_path: str = ""
    req_id: str = ""

class PureBM25:
    """Pure-Python BM25 implementation for zero-dependency local search."""
    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.corpus_size = len(corpus)
        self.k1 = k1
        self.b = b
        self.doc_lens = [len(doc) for doc in corpus]
        self.avg_doc_len = sum(self.doc_lens) / (self.corpus_size or 1)
        self.doc_freqs: dict[str, int] = {}
        self.idf: dict[str, float] = {}
        self.doc_term_freqs: list[dict[str, int]] = []

        for doc in corpus:
            tf: dict[str, int] = {}
            for term in doc:
                tf[term] = tf.get(term, 0) + 1
            self.doc_term_freqs.append(tf)
            for term in tf:
                self.doc_freqs[term] = self.doc_freqs.get(term, 0) + 1

        for term, freq in self.doc_freqs.items():
            self.idf[term] = math.log((self.corpus_size - freq + 0.5) / (freq + 0.5) + 1.0)

    def score(self, query_terms: list[str]) -> list[float]:
        scores = [0.0] * self.corpus_size
        for term in query_terms:
            if term not in self.idf:
                continue
            idf_val = self.idf[term]
            for doc_idx in range(self.corpus_size):
                tf = self.doc_term_freqs[doc_idx].get(term, 0)
                if tf == 0:
                    continue
                doc_len = self.doc_lens[doc_idx]
                numerator = tf * (self.k1 + 1.0)
                denominator = tf + self.k1 * (1.0 - self.b + self.b * (doc_len / self.avg_doc_len))
                scores[doc_idx] += idf_val * (numerator / denominator)
        return scores

def tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in re.findall(r"\b[A-Za-z0-9_\-\.]{2,}\b", text)]

def parse_differences_md(diff_path: Path) -> list[SearchDocument]:
    docs: list[SearchDocument] = []
    if not diff_path.exists():
        return docs

    content = diff_path.read_text(encoding="utf-8")

    # 1. Parse Requirements Table
    # Rows like: | **REQ-001** | Source | Spec | Impl | Test | Status | Gap |
    req_regex = re.compile(
        r"\|\s*\*{0,2}(REQ-[0-9A-Z\-]+)\*{0,2}\s*\|\s*([^|\r\n]+)\|\s*([^|\r\n]+)\|\s*([^|\r\n]+)\|\s*([^|\r\n]+)\|\s*([^|\r\n]+)\|\s*([^|\r\n]+)\|"
    )
    for match in req_regex.finditer(content):
        req_id = match.group(1).strip()
        if req_id.upper() in ("REQ-ID", "REQ-"):
            continue
        source = match.group(2).strip()
        spec = match.group(3).strip()
        impl = match.group(4).strip()
        test_proof = match.group(5).strip()
        raw_status = match.group(6).strip()
        gap = match.group(7).strip()

        # Normalize status
        if "IMPLEMENTED" in raw_status.upper():
            status = "implemented"
        elif "PARTIAL" in raw_status.upper() or "DIVERGED" in raw_status.upper():
            status = "partial"
        else:
            status = "missing"

        # Determine subsystem
        subsystem = "general"
        subsystems = ["storage", "ingestion", "decoding", "ipc", "tracking", "rag", "sufficiency", "llm", "auditor", "ui", "benchmark", "schema"]
        for sub in subsystems:
            if sub in spec.lower() or sub in impl.lower() or sub in gap.lower():
                subsystem = sub
                break

        doc_text = f"{req_id} {source} {spec} {impl} {test_proof} {raw_status} {gap}"
        docs.append(SearchDocument(
            doc_id=req_id,
            source="differences.md:Requirements",
            category="requirement",
            title=f"[{req_id}] {spec[:60]}...",
            subsystem=subsystem,
            status=status,
            priority="P0" if status == "missing" and ("core" in doc_text.lower() or "retrieval" in doc_text.lower()) else "P1",
            content=doc_text,
            req_id=req_id
        ))

    # 2. Parse Staged Empty Files Manifest Table (specifically from Section 5)
    manifest_section_match = re.search(r"## 5\. Staged Empty Files Manifest([\s\S]*?)(?=##|\Z)", content)
    if manifest_section_match:
        manifest_content = manifest_section_match.group(1)
        manifest_regex = re.compile(
            r"\|\s*`?([A-Za-z0-9_\-\.\/]+)`?\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|"
        )
        for match in manifest_regex.finditer(manifest_content):
            fpath = match.group(1).strip()
            if fpath.lower() in ("staged empty file path", "file path", "path", "---", ":---", "") or not ("." in fpath or "/" in fpath):
                continue
            req_id = match.group(2).strip()
            priority = match.group(3).strip()
            acceptance = match.group(4).strip()

            subsystem = "general"
            for sub in ["storage", "ingestion", "decoding", "ipc", "rag", "sufficiency", "llm", "auditor", "ui", "scripts", "tests", "schemas"]:
                if sub in fpath.lower():
                    subsystem = sub
                    break

            doc_text = f"Empty placeholder file: {fpath} for {req_id}. Priority: {priority}. Acceptance: {acceptance}"
            docs.append(SearchDocument(
                doc_id=f"file:{fpath}",
                source="differences.md:Manifest",
                category="empty_file",
                title=f"Empty File: {fpath}",
                subsystem=subsystem,
                status="staged_empty",
                priority=priority if "P" in priority else "P1",
                content=doc_text,
                file_path=fpath,
                req_id=req_id
            ))

    # 3. Parse Technical Drawbacks
    drawback_blocks = re.findall(r"###\s*(GAP-\d+|DRAWBACK-\d+|[0-9]+\.\s+[^:\n]+):?\s*([^\n]+)?\n([\s\S]*?)(?=###|\Z)", content)
    for block in drawback_blocks:
        d_id = block[0].strip()
        d_title = (block[1] or d_id).strip()
        d_body = block[2].strip()

        docs.append(SearchDocument(
            doc_id=d_id,
            source="differences.md:Drawbacks",
            category="drawback",
            title=f"Drawback: {d_title}",
            subsystem="drawback",
            status="open",
            priority="P1",
            content=f"{d_id} {d_title}\n{d_body}",
        ))

    return docs

def parse_extracted_papers(specs_dir: Path) -> list[SearchDocument]:
    docs: list[SearchDocument] = []
    if not specs_dir.exists():
        return docs

    for txt_path in specs_dir.glob("*.txt"):
        text = txt_path.read_text(encoding="utf-8", errors="ignore")
        # Split into logical chunks of ~1500 chars with section titles
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunk = ""
        chunk_idx = 0
        for p in paragraphs:
            if len(chunk) + len(p) > 1500 and chunk:
                docs.append(SearchDocument(
                    doc_id=f"{txt_path.stem}_chunk_{chunk_idx}",
                    source=f"paper:{txt_path.name}",
                    category="paper_section",
                    title=f"[{txt_path.stem}] Section {chunk_idx}",
                    subsystem="research",
                    status="reference",
                    priority="info",
                    content=chunk,
                ))
                chunk_idx += 1
                chunk = p + "\n\n"
            else:
                chunk += p + "\n\n"
        if chunk:
            docs.append(SearchDocument(
                doc_id=f"{txt_path.stem}_chunk_{chunk_idx}",
                source=f"paper:{txt_path.name}",
                category="paper_section",
                title=f"[{txt_path.stem}] Section {chunk_idx}",
                subsystem="research",
                status="reference",
                priority="info",
                content=chunk,
            ))

    return docs

class SearchEngine:
    def __init__(self, docs: list[SearchDocument]):
        self.docs = docs
        self.tokenized_corpus = [tokenize(d.content + " " + d.title) for d in docs]
        self.bm25 = PureBM25(self.tokenized_corpus)

    def search(
        self,
        query: str = "",
        status: str | None = None,
        subsystem: str | None = None,
        priority: str | None = None,
        req_id: str | None = None,
        only_empty_files: bool = False,
        top_k: int = 15,
    ) -> list[tuple[SearchDocument, float]]:
        # Filter candidate indices
        matching_indices: list[int] = []
        for idx, doc in enumerate(self.docs):
            if only_empty_files and doc.category != "empty_file":
                continue
            if status and doc.status.lower() != status.lower():
                continue
            if subsystem and subsystem.lower() not in doc.subsystem.lower():
                continue
            if priority and priority.lower() not in doc.priority.lower():
                continue
            if req_id and req_id.lower() not in (doc.req_id.lower() + " " + doc.doc_id.lower()):
                continue
            matching_indices.append(idx)

        if not matching_indices:
            return []

        if query.strip():
            query_tokens = tokenize(query)
            scores = self.bm25.score(query_tokens)
            results = [(self.docs[i], scores[i]) for i in matching_indices]
            # Filter results with score > 0 or if all zero keep top matches
            results.sort(key=lambda x: x[1], reverse=True)
            non_zero = [r for r in results if r[1] > 0.0]
            if non_zero:
                return non_zero[:top_k]
            return results[:top_k]
        else:
            return [(self.docs[i], 1.0) for i in matching_indices[:top_k]]

def main():
    parser = argparse.ArgumentParser(description="Failure Synthesis System — Local Differences & Papers Search Engine")
    parser.add_argument("query", nargs="?", default="", help="Free-text search query")
    parser.add_argument("--status", choices=["implemented", "partial", "missing", "staged_empty"], help="Filter by implementation status")
    parser.add_argument("--subsystem", help="Filter by subsystem (storage, ipc, rag, ui, auditor, etc.)")
    parser.add_argument("--priority", choices=["P0", "P1", "P2"], help="Filter by priority")
    parser.add_argument("--req", help="Filter by specific REQ-ID (e.g., REQ-001)")
    parser.add_argument("--only-empty-files", action="store_true", help="Show only staged empty placeholder files")
    parser.add_argument("--top-k", type=int, default=10, help="Number of results to return")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")

    args = parser.parse_args()

    diff_path = ROOT_DIR / "differences.md"
    specs_dir = ROOT_DIR / "extracted_specs"

    docs = parse_differences_md(diff_path)
    docs.extend(parse_extracted_papers(specs_dir))

    if not docs:
        if args.json:
            print(json.dumps({"error": "No documents indexed", "results": []}))
        else:
            print("No documents found to index. Ensure differences.md or extracted_specs/ exists.")
        sys.exit(0)

    engine = SearchEngine(docs)
    results = engine.search(
        query=args.query,
        status=args.status,
        subsystem=args.subsystem,
        priority=args.priority,
        req_id=args.req,
        only_empty_files=args.only_empty_files,
        top_k=args.top_k,
    )

    if args.json:
        out = [
            {
                "score": round(score, 4),
                **asdict(doc)
            }
            for doc, score in results
        ]
        print(json.dumps(out, indent=2))
        return

    print(f"\n================================================================================")
    print(f"  FSS Search Engine — Query: '{args.query or '*'}' | Results: {len(results)}")
    print(f"================================================================================")
    if not results:
        print("  No matching documents found.")
        return

    for i, (doc, score) in enumerate(results, start=1):
        score_str = f"[Score: {score:.3f}]" if args.query else ""
        print(f"\n[{i}] {doc.title} {score_str}")
        print(f"    Source:    {doc.source} | Category: {doc.category} | Subsystem: {doc.subsystem}")
        print(f"    Status:    {doc.status.upper()} | Priority: {doc.priority}")
        if doc.file_path:
            print(f"    Target File: {doc.file_path}")
        snippet = " ".join(doc.content.split()[:40])
        print(f"    Preview:   {snippet}...")

if __name__ == "__main__":
    main()
