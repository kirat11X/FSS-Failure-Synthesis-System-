"""Local document and structured-data ingestion for the Hybrid RAG corpus."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

import yaml

KNOWLEDGE_BASE_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".html"}
CHUNK_SIZE_CHARS = 2_000
CHUNK_OVERLAP_CHARS = 200


class KnowledgeBaseError(RuntimeError):
    """A supported knowledge file could not be read or normalized."""


@dataclass(frozen=True, slots=True)
class _Section:
    section: str
    content: str
    page_number: int | None = None
    schema_version: str | None = None


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {"br", "div", "li", "p", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.meta_title_parts: list[str] = []
        self._title_depth = 0
        self._heading_depth = 0
        self._heading_parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            attr_map = dict(attrs)
            if (attr_map.get("property") or "").casefold() in {"og:title", "twitter:title"}:
                content = attr_map.get("content")
                if content:
                    self.meta_title_parts.append(content)
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag == "title":
            self._title_depth += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self._heading_depth += 1
            self._heading_parts = []
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title" and self._title_depth:
            self._title_depth -= 1
        elif re.fullmatch(r"h[1-6]", tag) and self._heading_depth:
            heading = " ".join("".join(self._heading_parts).split())
            self.parts.append(f"\n## {heading}\n")
            self._heading_depth -= 1
            self._heading_parts = []
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._title_depth:
            self.title_parts.append(data)
        elif self._heading_depth:
            self._heading_parts.append(data)
        else:
            self.parts.append(data)


def knowledge_base_fingerprint(directory: str | Path = KNOWLEDGE_BASE_DIR) -> tuple[tuple[str, int, int], ...]:
    root = Path(directory)
    if not root.is_dir():
        return ()
    fingerprints = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            stat = path.stat()
            fingerprints.append(
                (str(path.relative_to(root)), stat.st_mtime_ns, stat.st_size)
            )
    return tuple(fingerprints)


def load_knowledge_base(directory: str | Path = KNOWLEDGE_BASE_DIR) -> list[dict[str, Any]]:
    """Extract, chunk, and attach stable provenance metadata to supported files."""
    root = Path(directory)
    if not root.is_dir():
        return []

    documents: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        relative_name = path.relative_to(root).as_posix()
        try:
            title, sections, kind = _extract_file(path)
            chunk_index = 0
            for section in sections:
                for content in _chunk_text(section.content):
                    if not content.strip():
                        continue
                    identity = "\0".join(
                        (
                            relative_name,
                            section.section,
                            str(section.page_number or ""),
                            str(chunk_index),
                            content,
                        )
                    )
                    doc_id = f"doc-kb-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"
                    doc: dict[str, Any] = {
                        "id": doc_id,
                        "chunk": doc_id,
                        "kind": kind,
                        "domain": "knowledge_base",
                        "title": title,
                        "explains": f"{path.stem} {section.section}",
                        "content": content,
                        "source_filename": relative_name,
                        "section": section.section,
                        "schema_version": section.schema_version or "unspecified",
                    }
                    if section.page_number is not None:
                        doc["page_number"] = section.page_number
                    documents.append(doc)
                    chunk_index += 1
        except KnowledgeBaseError as exc:
            raise KnowledgeBaseError(f"Could not index {relative_name}: {exc}") from exc
        except (
            OSError,
            ValueError,
            UnicodeError,
            zipfile.BadZipFile,
            ElementTree.ParseError,
            csv.Error,
            yaml.YAMLError,
        ) as exc:
            raise KnowledgeBaseError(f"Could not index {relative_name}: {exc}") from exc
    return documents


def _extract_file(path: Path) -> tuple[str, list[_Section], str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".md":
        text = path.read_text(encoding="utf-8-sig")
        title, sections = _markdown_sections(text, path.stem)
        return title, sections, "knowledge-document"
    if suffix == ".txt":
        text = path.read_text(encoding="utf-8-sig")
        return path.stem, [_Section("Document", text)], "knowledge-document"
    if suffix == ".html":
        parser = _HTMLTextExtractor()
        parser.feed(path.read_text(encoding="utf-8-sig"))
        title = (
            " ".join("".join(parser.title_parts).split())
            or " ".join(" ".join(parser.meta_title_parts).split())
            or path.stem
        )
        clean_text = html.unescape("".join(parser.parts))
        return title, _markdown_sections(clean_text, title)[1], "knowledge-document"
    if suffix in {".json", ".yaml", ".yml"}:
        return _extract_structured(path)
    if suffix == ".csv":
        return _extract_csv(path)
    raise KnowledgeBaseError(f"Unsupported knowledge file extension: {suffix}")


def _extract_pdf(path: Path) -> tuple[str, list[_Section], str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise KnowledgeBaseError("PDF indexing requires `pypdf`; install requirements-app.txt") from exc

    reader = PdfReader(str(path))
    metadata = reader.metadata
    title = str(getattr(metadata, "title", "") or path.stem).strip()
    sections = [
        _Section(f"Page {page_number}", page.extract_text() or "", page_number)
        for page_number, page in enumerate(reader.pages, start=1)
    ]
    return title, sections, "knowledge-document"


def _extract_docx(path: Path) -> tuple[str, list[_Section], str]:
    try:
        from docx import Document
    except ImportError as exc:
        raise KnowledgeBaseError(
            "DOCX indexing requires `python-docx`; install requirements-app.txt"
        ) from exc

    document = Document(str(path))
    title = (document.core_properties.title or "").strip() or path.stem
    sections: list[_Section] = []
    current_heading = "Document"
    paragraphs: list[str] = []

    def flush() -> None:
        nonlocal paragraphs
        if paragraphs:
            sections.append(_Section(current_heading, "\n".join(paragraphs)))
            paragraphs = []

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style_name = (paragraph.style.name if paragraph.style is not None else "") or ""
        if style_name.lower().startswith("heading"):
            flush()
            current_heading = text
        else:
            paragraphs.append(text)
    for table_index, table in enumerate(document.tables, start=1):
        table_text = "\n".join(
            " | ".join(cell.text.strip() for cell in row.cells)
            for row in table.rows
        )
        if table_text.strip():
            flush()
            sections.append(_Section(f"Table {table_index}", table_text))
    flush()
    return title, sections, "knowledge-document"


def _extract_structured(path: Path) -> tuple[str, list[_Section], str]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    title = _get_title(data, path.stem)
    schema_version = _get_schema_version(data)

    sections: list[_Section] = []
    if isinstance(data, list):
        for index, value in enumerate(data):
            sections.append(
                _Section(f"$[{index}]", _normalize_value(value), schema_version=schema_version)
            )
    elif isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in {"title", "name", "schema_version", "schemaversion", "version"}:
                continue
            sections.append(
                _Section(f"$.{key}", _normalize_value(value), schema_version=schema_version)
            )
    else:
        sections.append(_Section("$", _normalize_value(data), schema_version=schema_version))
    return title, sections or [_Section("$", "", schema_version=schema_version)], "structured-reference"


def _extract_csv(path: Path) -> tuple[str, list[_Section], str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header row")
        rows = list(reader)
    schema_version = next(
        (
            str(value)
            for row in rows
            for key, value in row.items()
            if key and key.casefold() in {"schema_version", "schemaversion", "version"}
            and value
        ),
        None,
    )
    sections = [
        _Section(
            f"Row {row_number}",
            _normalize_value(row),
            schema_version=schema_version,
        )
        for row_number, row in enumerate(rows, start=2)
    ]
    return path.stem, sections, "structured-reference"


def _markdown_sections(text: str, default_title: str) -> tuple[str, list[_Section]]:
    title = default_title
    sections: list[_Section] = []
    headings: list[tuple[int, str]] = []
    content: list[str] = []

    def flush() -> None:
        nonlocal content
        body = "\n".join(content).strip()
        if body:
            section_name = " / ".join(name for _, name in headings) or "Overview"
            sections.append(_Section(section_name, body))
        content = []

    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            flush()
            level = len(match.group(1))
            heading = match.group(2).strip()
            if level == 1 and title == default_title:
                title = heading
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, heading))
            content.append(heading)
        else:
            content.append(line)
    flush()
    return title, sections or [_Section("Document", text)]


def _normalize_value(value: Any, prefix: str = "") -> str:
    lines: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else str(key)
            lines.extend(_normalize_value(child, child_path).splitlines())
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{prefix}[{index}]"
            lines.extend(_normalize_value(child, child_path).splitlines())
    elif value is None:
        lines.append(f"{prefix}: null" if prefix else "null")
    else:
        lines.append(f"{prefix}: {value}" if prefix else str(value))
    return "\n".join(lines)


def _get_title(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        for key in ("title", "name", "document_title"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _get_schema_version(data: Any) -> str | None:
    if isinstance(data, dict):
        for key in ("schema_version", "schemaVersion", "version"):
            value = data.get(key)
            if value is not None and not isinstance(value, (dict, list)):
                return str(value)
    return None


def _chunk_text(text: str) -> Iterable[str]:
    normalized = text.strip()
    start = 0
    while start < len(normalized):
        end = min(start + CHUNK_SIZE_CHARS, len(normalized))
        if end < len(normalized):
            split_at = max(
                normalized.rfind("\n", start + CHUNK_SIZE_CHARS // 2, end),
                normalized.rfind(" ", start + CHUNK_SIZE_CHARS // 2, end),
            )
            if split_at > start:
                end = split_at
        chunk = normalized[start:end].strip()
        if chunk:
            yield chunk
        if end >= len(normalized):
            break
        start = max(start + 1, end - CHUNK_OVERLAP_CHARS)
