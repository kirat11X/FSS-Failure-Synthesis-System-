"""Vendor & Custom Error-Code Manual Parser, Ingestion & Catalog Module.

Provides:
  - Dataclass `VendorErrorEntry` representing parsed vendor error catalog entries.
  - Multi-format manual parsers (.json, .csv, .md, .txt) preserving exact alphanumeric error codes.
  - Built-in vendor reference manual loader (`ardupilot`, `px4`, `fss_native`, `auto`).
  - Telemetry event fault code extractor (`extract_error_codes_from_events`).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

# Directory path for domain reference manuals
MANUALS_DIR = Path(__file__).resolve().parent.parent / "domain_manuals"


@dataclass
class VendorErrorEntry:
    """Represents a vendor or system error/status code manual specification entry."""
    vendor: str
    error_code: str
    subsystem: str
    severity: str
    title: str
    description: str
    probable_causes: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    doc_chunk_id: str = ""
    aliases: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.doc_chunk_id:
            norm_code = re.sub(r"[^a-zA-Z0-9_\-]", "_", self.error_code).strip("_")
            norm_vendor = re.sub(r"[^a-zA-Z0-9_\-]", "_", self.vendor.lower()).strip("_")
            self.doc_chunk_id = f"err-{norm_vendor}-{norm_code}"
        if not self.aliases:
            self.aliases = [self.error_code]
        elif self.error_code not in self.aliases:
            self.aliases.insert(0, self.error_code)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_doc(self) -> dict[str, str]:
        """Convert entry into standard HybridRetriever domain knowledge document chunk."""
        causes_str = "; ".join(self.probable_causes) if self.probable_causes else "Not specified"
        actions_str = "; ".join(self.recommended_actions) if self.recommended_actions else "Not specified"
        alias_tokens = [self.error_code, self.subsystem, self.severity] + [a for a in self.aliases if a != self.error_code]
        explains_str = ", ".join(dict.fromkeys(alias_tokens))
        return {
            "id": self.doc_chunk_id,
            "chunk": f"{self.doc_chunk_id}#1",
            "kind": "vendor-error-code",
            "domain": self.vendor,
            "title": f"[{self.vendor.upper()}] {self.error_code}: {self.title}",
            "explains": explains_str,
            "content": (
                f"Vendor: {self.vendor}\n"
                f"Error Code: {self.error_code}\n"
                f"Aliases: {', '.join(self.aliases)}\n"
                f"Subsystem: {self.subsystem}\n"
                f"Severity: {self.severity}\n"
                f"Title: {self.title}\n"
                f"Description: {self.description}\n"
                f"Probable Causes: {causes_str}\n"
                f"Recommended Actions: {actions_str}"
            ),
        }


def _detect_vendor_from_text_or_filename(filename: str, content_sample: str) -> str:
    """Infer vendor identifier from filename and content headers."""
    fn_lower = filename.lower()
    if "ardupilot" in fn_lower or "dataflash" in fn_lower:
        return "ardupilot"
    if "px4" in fn_lower or "uorb" in fn_lower:
        return "px4"
    if "fss" in fn_lower or "telemetry" in fn_lower:
        return "fss_native"

    sample_lower = content_sample[:2000].lower()
    if "ardupilot" in sample_lower:
        return "ardupilot"
    if "px4" in sample_lower:
        return "px4"
    if "fss" in sample_lower or "telemetry" in sample_lower:
        return "fss_native"

    stem = Path(filename).stem.lower()
    clean_stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", stem)
    return clean_stem or "custom"


def _clean_str_list(val: Any) -> list[str]:
    """Parse list of strings or delimited string into clean list of strings."""
    if not val:
        return []
    if isinstance(val, list):
        return [str(item).strip() for item in val if str(item).strip()]
    if isinstance(val, str):
        items = re.split(r"[;\n|]", val)
        return [item.strip() for item in items if item.strip()]
    return [str(val).strip()]


def _parse_json_manual(data: Any, vendor: str) -> list[VendorErrorEntry]:
    """Parse JSON structure (list of dicts or code-keyed map)."""
    entries: list[VendorErrorEntry] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            code = str(item.get("error_code") or item.get("code") or item.get("ecode") or item.get("id") or "").strip()
            if not code:
                continue
            v = str(item.get("vendor") or vendor).strip()
            aliases = _clean_str_list(item.get("aliases"))
            if code not in aliases:
                aliases.insert(0, code)
            entry = VendorErrorEntry(
                vendor=v,
                error_code=code,
                subsystem=str(item.get("subsystem") or item.get("subsys") or item.get("system") or "General").strip(),
                severity=str(item.get("severity") or item.get("level") or "ERROR").strip().upper(),
                title=str(item.get("title") or item.get("meaning") or item.get("name") or code).strip(),
                description=str(item.get("description") or item.get("desc") or "").strip(),
                probable_causes=_clean_str_list(item.get("probable_causes") or item.get("causes")),
                recommended_actions=_clean_str_list(item.get("recommended_actions") or item.get("actions")),
                doc_chunk_id=str(item.get("doc_chunk_id") or "").strip(),
                aliases=aliases,
            )
            entries.append(entry)
    elif isinstance(data, dict):
        if "errors" in data and isinstance(data["errors"], list):
            return _parse_json_manual(data["errors"], vendor)
        for code, details in data.items():
            if not isinstance(details, dict):
                continue
            c = str(details.get("error_code") or details.get("code") or code).strip()
            v = str(details.get("vendor") or vendor).strip()
            aliases = _clean_str_list(details.get("aliases"))
            if c not in aliases:
                aliases.insert(0, c)
            entry = VendorErrorEntry(
                vendor=v,
                error_code=c,
                subsystem=str(details.get("subsystem") or details.get("subsys") or "General").strip(),
                severity=str(details.get("severity") or details.get("level") or "ERROR").strip().upper(),
                title=str(details.get("title") or details.get("meaning") or c).strip(),
                description=str(details.get("description") or details.get("desc") or "").strip(),
                probable_causes=_clean_str_list(details.get("probable_causes") or details.get("causes")),
                recommended_actions=_clean_str_list(details.get("recommended_actions") or details.get("actions")),
                doc_chunk_id=str(details.get("doc_chunk_id") or "").strip(),
                aliases=aliases,
            )
            entries.append(entry)
    return entries


def _parse_csv_manual(text: str, vendor: str) -> list[VendorErrorEntry]:
    """Parse CSV text with flexible column mapping."""
    entries: list[VendorErrorEntry] = []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return entries

    header_map: dict[str, str] = {}
    for h in reader.fieldnames:
        clean = h.strip().lower()
        if clean in ("code", "error_code", "ecode", "id", "error code", "error", "code name"):
            header_map["code"] = h
        elif clean in ("subsystem", "subsys", "system", "subsystem name"):
            header_map["subsystem"] = h
        elif clean in ("severity", "level", "sev"):
            header_map["severity"] = h
        elif clean in ("title", "meaning", "name", "title / meaning", "error name"):
            header_map["title"] = h
        elif clean in ("description", "desc", "details"):
            header_map["description"] = h
        elif clean in ("causes", "probable_causes", "probable causes", "root cause"):
            header_map["causes"] = h
        elif clean in ("actions", "recommended_actions", "recommended actions", "resolution"):
            header_map["actions"] = h
        elif clean in ("vendor", "manufacturer"):
            header_map["vendor"] = h
        elif clean in ("aliases", "alias"):
            header_map["aliases"] = h

    for row in reader:
        code_col = header_map.get("code")
        if not code_col or not row.get(code_col):
            continue
        code = str(row[code_col]).strip()
        v = str(row.get(header_map.get("vendor", ""), "")).strip() or vendor
        aliases = _clean_str_list(row.get(header_map.get("aliases", "")))
        if code not in aliases:
            aliases.insert(0, code)
        entry = VendorErrorEntry(
            vendor=v,
            error_code=code,
            subsystem=str(row.get(header_map.get("subsystem", ""), "General")).strip() or "General",
            severity=str(row.get(header_map.get("severity", ""), "ERROR")).strip().upper() or "ERROR",
            title=str(row.get(header_map.get("title", ""), code)).strip() or code,
            description=str(row.get(header_map.get("description", ""), "")).strip(),
            probable_causes=_clean_str_list(row.get(header_map.get("causes", ""))),
            recommended_actions=_clean_str_list(row.get(header_map.get("actions", ""))),
            aliases=aliases,
        )
        entries.append(entry)
    return entries


def _parse_markdown_tables(text: str, vendor: str) -> list[VendorErrorEntry]:
    """Parse Markdown tables with headers like Code, Subsystem, Severity, Title, Causes, Actions."""
    entries: list[VendorErrorEntry] = []
    lines = text.splitlines()
    table_lines: list[str] = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines.append(stripped)
            in_table = True
        else:
            if in_table:
                if len(table_lines) >= 3:
                    parsed = _parse_single_markdown_table(table_lines, vendor)
                    entries.extend(parsed)
                table_lines = []
                in_table = False

    if in_table and len(table_lines) >= 3:
        parsed = _parse_single_markdown_table(table_lines, vendor)
        entries.extend(parsed)

    return entries


def _parse_single_markdown_table(table_lines: list[str], vendor: str) -> list[VendorErrorEntry]:
    """Parse an individual markdown table block."""
    raw_headers = [c.strip().strip("`").lower() for c in table_lines[0].split("|")[1:-1]]
    if not re.match(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$", table_lines[1]):
        return []

    col_map: dict[str, int] = {}

    for idx, h in enumerate(raw_headers):
        if h in ("error code string", "code name", "error code", "event symbol"):
            col_map["code"] = idx
        elif h in ("code", "event id", "ecode") and "code" not in col_map:
            col_map["code"] = idx
        elif h in ("subsystem", "subsys name", "subsys", "system"):
            col_map["subsystem"] = idx
        elif h in ("severity", "level"):
            col_map["severity"] = idx
        elif h in ("title / meaning", "title", "meaning", "name", "title/meaning"):
            col_map["title"] = idx
        elif h in ("description", "desc", "details"):
            col_map["description"] = idx
        elif h in ("probable causes", "causes", "probable causes / triggers"):
            col_map["causes"] = idx
        elif h in ("recommended actions", "actions", "recommended action"):
            col_map["actions"] = idx
        elif h in ("vendor",):
            col_map["vendor"] = idx

    if "code" not in col_map:
        for idx, h in enumerate(raw_headers):
            if ("code" in h or "symbol" in h or "error" in h or h == "id" or h.endswith("_id")) and not any(term in h for term in ("source", "agent", "node", "sensor", "device", "stream")):
                col_map["code"] = idx
                break

    if "code" not in col_map:
        return []

    # If the table doesn't have ANY error metadata columns (causes, actions, severity, description), it is not an error table
    if not any(k in col_map for k in ("causes", "actions", "severity", "description")):
        return []

    entries: list[VendorErrorEntry] = []
    for line in table_lines[2:]:
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) <= col_map["code"]:
            continue
        raw_code = cells[col_map["code"]].strip("`").strip()
        if not raw_code:
            continue

        code = raw_code.strip("`").strip("'").strip('"')

        subsys = cells[col_map["subsystem"]].strip("`").strip() if "subsystem" in col_map and len(cells) > col_map["subsystem"] else "General"
        sev = cells[col_map["severity"]].strip("`").strip().upper() if "severity" in col_map and len(cells) > col_map["severity"] else "ERROR"
        title = cells[col_map["title"]].strip("`").strip() if "title" in col_map and len(cells) > col_map["title"] else code
        desc = cells[col_map["description"]].strip("`").strip() if "description" in col_map and len(cells) > col_map["description"] else ""
        causes_raw = cells[col_map["causes"]].strip("`").strip() if "causes" in col_map and len(cells) > col_map["causes"] else ""
        actions_raw = cells[col_map["actions"]].strip("`").strip() if "actions" in col_map and len(cells) > col_map["actions"] else ""

        if not desc:
            desc = title

        aliases: list[str] = [code]

        # Specific alias generation per vendor/structure
        if vendor == "ardupilot":
            # If code is SUBSYS_14_ECODE_2, add ERR_14_2, [ERR-14-2], SUBSYS 14 CODE 2, SUBSYS_14, Subsys 14
            m = re.match(r"^SUBSYS_(\d+)_ECODE_(\d+)$", code)
            if m:
                s_id, e_id = m.group(1), m.group(2)
                aliases.extend([
                    f"ERR_{s_id}_{e_id}",
                    f"[ERR-{s_id}-{e_id}]",
                    f"SUBSYS {s_id} CODE {e_id}",
                    f"SUBSYS_{s_id}",
                    f"Subsys {s_id}",
                    f"Subsystem {s_id}",
                    f"EV_{s_id}",
                    f"EV {s_id}",
                ])
                if e_id == "0":
                    aliases.extend([
                        f"SUBSYS_{s_id}_CLEARED",
                        "ERROR_RESOLVED",
                        "FAILSAFE_CLEARED",
                        "ECODE_0",
                    ])
            # If EV_10, add EV 10, SUBSYS_10, Subsys 10
            ev_m = re.match(r"^EV_(\d+)$", code)
            if ev_m:
                ev_id = ev_m.group(1)
                aliases.extend([
                    f"EV {ev_id}",
                    f"SUBSYS_{ev_id}",
                    f"Subsys {ev_id}",
                    f"Subsystem {ev_id}",
                    f"SUBSYS_{ev_id}_ECODE_0",
                    f"ERR_{ev_id}_0",
                ])
            if code in ("ECODE_0", "ERROR_RESOLVED"):
                aliases.extend(["ECODE 0", "CODE 0", "ERROR_RESOLVED", "FAILSAFE_CLEARED", "RESOLVED"])
        elif vendor == "fss_native":
            # Add composite tags if event symbol and code are available
            ev_sym = cells[raw_headers.index("event symbol")].strip("`").strip() if "event symbol" in raw_headers else ""
            ev_hex = cells[raw_headers.index("event type (hex)")].strip("`").strip() if "event type (hex)" in raw_headers else ""
            st_code = cells[raw_headers.index("status / reason code")].strip("`").strip() if "status / reason code" in raw_headers else ""
            if st_code:
                if ev_sym:
                    aliases.extend([f"{ev_sym}_{st_code}", f"{ev_sym}:{st_code}"])
                if ev_hex:
                    aliases.extend([f"{ev_hex}:{st_code}", f"{ev_hex}_{st_code}"])
            else:
                if ev_sym and ev_sym not in aliases:
                    aliases.append(ev_sym)
                if ev_hex and ev_hex not in aliases:
                    aliases.append(ev_hex)

        entry = VendorErrorEntry(
            vendor=vendor,
            error_code=code,
            subsystem=subsys or "General",
            severity=sev or "ERROR",
            title=title or code,
            description=desc,
            probable_causes=_clean_str_list(causes_raw),
            recommended_actions=_clean_str_list(actions_raw),
            aliases=aliases,
        )
        entries.append(entry)

    return entries


def _parse_markdown_sections(text: str, vendor: str) -> list[VendorErrorEntry]:
    """Parse Markdown headings like `### [ERR-...]`, `### Error 0x...`, or `### SUBSYS n CODE m`."""
    entries: list[VendorErrorEntry] = []
    section_pattern = re.compile(
        r"^(#{2,4})\s+(.*)$",
        re.MULTILINE,
    )

    matches = list(section_pattern.finditer(text))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        header_text = match.group(2).strip()
        block = text[start:end].strip()

        # Extract code candidates: [TAG], Error 0x..., SUBSYS n CODE m, or alphanumeric word
        code = ""
        title_hint = ""
        aliases: list[str] = []

        bracket_m = re.search(r"\[([A-Za-z0-9_\-]+)\]", header_text)
        if bracket_m:
            code = bracket_m.group(1).strip()
            aliases.append(code)
            # Remove bracket part for remaining title
            rem = header_text[:bracket_m.start()] + header_text[bracket_m.end():]
        else:
            subsys_m = re.search(r"(SUBSYS\s+\d+\s+CODE\s+\d+|SUBSYS_\d+_ECODE_\d+|ERR_\d+_\d+)", header_text, re.IGNORECASE)
            if subsys_m:
                code = subsys_m.group(1).strip().replace(" ", "_")
                aliases.append(code)
                rem = header_text[:subsys_m.start()] + header_text[subsys_m.end():]
            else:
                err_m = re.search(r"(?:Error|ERR)\s+([A-Za-z0-9_xX\-]+)", header_text, re.IGNORECASE)
                if err_m:
                    code = err_m.group(1).strip()
                    aliases.append(code)
                    rem = header_text[:err_m.start()] + header_text[err_m.end():]
                else:
                    words = header_text.split()
                    if words and (words[0].isupper() or "_" in words[0] or words[0].startswith("0x")):
                        code = words[0].strip()
                        aliases.append(code)
                        rem = " ".join(words[1:])
                    else:
                        continue

        # Look for additional compound codes in rem
        sub_m = re.search(r"SUBSYS\s+(\d+)\s+CODE\s+(\d+)", header_text, re.IGNORECASE)
        if sub_m:
            s_num, c_num = sub_m.group(1), sub_m.group(2)
            aliases.extend([f"SUBSYS_{s_num}_ECODE_{c_num}", f"ERR_{s_num}_{c_num}", f"SUBSYS {s_num} CODE {c_num}"])

        # Title hint is remaining stripped after separator
        title_hint = re.sub(r"^[\s:\-–—]+", "", rem).strip()

        subsys = "General"
        sev = "ERROR"
        desc = ""
        causes: list[str] = []
        actions: list[str] = []

        for line in block.splitlines():
            line_s = line.strip()
            if re.match(r"^-\s*\*\*(?:Subsystem|System)\*\*:\s*(.*)$", line_s, re.IGNORECASE):
                m = re.match(r"^-\s*\*\*(?:Subsystem|System)\*\*:\s*(.*)$", line_s, re.IGNORECASE)
                if m:
                    subsys = m.group(1).strip()
            elif re.match(r"^-\s*\*\*(?:Severity|Level)\*\*:\s*(.*)$", line_s, re.IGNORECASE):
                m = re.match(r"^-\s*\*\*(?:Severity|Level)\*\*:\s*(.*)$", line_s, re.IGNORECASE)
                if m:
                    sev = m.group(1).strip().upper()
            elif re.match(r"^-\s*\*\*(?:Description|Details)\*\*:\s*(.*)$", line_s, re.IGNORECASE):
                m = re.match(r"^-\s*\*\*(?:Description|Details)\*\*:\s*(.*)$", line_s, re.IGNORECASE)
                if m:
                    desc = m.group(1).strip()
            elif re.match(r"^-\s*\*\*(?:Probable Causes|Causes)\*\*:\s*(.*)$", line_s, re.IGNORECASE):
                m = re.match(r"^-\s*\*\*(?:Probable Causes|Causes)\*\*:\s*(.*)$", line_s, re.IGNORECASE)
                if m:
                    causes = _clean_str_list(m.group(1))
            elif re.match(r"^-\s*\*\*(?:Recommended Actions|Actions)\*\*:\s*(.*)$", line_s, re.IGNORECASE):
                m = re.match(r"^-\s*\*\*(?:Recommended Actions|Actions)\*\*:\s*(.*)$", line_s, re.IGNORECASE)
                if m:
                    actions = _clean_str_list(m.group(1))

        if not desc:
            desc = block.splitlines()[0].strip() if block else title_hint

        entry = VendorErrorEntry(
            vendor=vendor,
            error_code=code,
            subsystem=subsys,
            severity=sev,
            title=title_hint or code,
            description=desc,
            probable_causes=causes,
            recommended_actions=actions,
            aliases=aliases,
        )
        entries.append(entry)

    return entries


def parse_error_manual(content: str | bytes, filename: str, vendor: str = "auto") -> list[VendorErrorEntry]:
    """Parse error manual content across formats (.json, .csv, .md, .txt).

    Guarantees exact alphanumeric error codes are never fragmented.
    """
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = content

    resolved_vendor = vendor
    if not resolved_vendor or resolved_vendor == "auto":
        resolved_vendor = _detect_vendor_from_text_or_filename(filename, text)

    ext = Path(filename).suffix.lower()

    # 1. JSON
    if ext == ".json":
        try:
            data = json.loads(text)
            return _parse_json_manual(data, resolved_vendor)
        except Exception:
            pass

    # 2. CSV
    if ext == ".csv":
        entries = _parse_csv_manual(text, resolved_vendor)
        if entries:
            return entries

    # 3. Markdown / Plain Text
    table_entries = _parse_markdown_tables(text, resolved_vendor)
    section_entries = _parse_markdown_sections(text, resolved_vendor)

    # Merge entries, preferring table definitions or updating with section details
    by_code: dict[str, VendorErrorEntry] = {}
    for e in table_entries:
        by_code[e.error_code] = e

    for se in section_entries:
        matched_key = None
        for k, existing in by_code.items():
            if se.error_code == k or se.error_code in existing.aliases or any(a in se.aliases for a in existing.aliases):
                matched_key = k
                break

        if matched_key:
            existing = by_code[matched_key]
            if se.description and (not existing.description or existing.description == existing.title):
                existing.description = se.description
            if se.probable_causes and not existing.probable_causes:
                existing.probable_causes = se.probable_causes
            if se.recommended_actions and not existing.recommended_actions:
                existing.recommended_actions = se.recommended_actions
            for a in se.aliases:
                if a not in existing.aliases:
                    existing.aliases.append(a)
        else:
            by_code[se.error_code] = se

    return list(by_code.values())


def load_builtin_manual(vendor_name: str = "auto") -> list[VendorErrorEntry]:
    """Load built-in technical reference manuals from domain_manuals/.

    Supported vendor_name options: 'ardupilot', 'px4', 'fss_native' (or 'fss'), 'auto'.
    If 'auto' is passed, all available built-in manuals are loaded and consolidated.
    """
    v_norm = (vendor_name or "auto").strip().lower()
    manual_files: list[Path] = []

    if v_norm in ("ardupilot", "all", "auto"):
        manual_files.append(MANUALS_DIR / "ardupilot_error_codes.md")
    if v_norm in ("px4", "all", "auto"):
        manual_files.append(MANUALS_DIR / "px4_error_codes.md")
    if v_norm in ("fss_native", "fss", "all", "auto"):
        manual_files.append(MANUALS_DIR / "fss_custom_telemetry_codes.md")

    entries: list[VendorErrorEntry] = []
    for mpath in manual_files:
        if mpath.exists():
            content = mpath.read_text(encoding="utf-8")
            vendor = "ardupilot" if "ardupilot" in mpath.name else ("px4" if "px4" in mpath.name else "fss_native")
            parsed = parse_error_manual(content, mpath.name, vendor=vendor)
            entries.extend(parsed)

    return entries


# Known FSS protocol mapping for status/reason codes
FSS_STATUS_MAP: dict[tuple[str, int], str] = {
    ("LIDAR_STATUS", 0): "OK",
    ("LIDAR_STATUS", 17): "LIDAR_TIMEOUT",
    ("LIDAR_STATUS", 18): "SENSOR_DISCONNECTED",
    ("LIDAR_STATUS", 19): "INVALID_SCAN",
    ("AVOIDANCE", 1): "COLLISION_RISK",
    ("AVOIDANCE", 2): "GEOFENCE",
    ("AVOIDANCE", 3): "OPERATOR_COMMAND",
    ("FORMATION_STATE", 0): "IN_FORMATION",
    ("FORMATION_STATE", 1): "LEFT_FORMATION",
    ("FORMATION_STATE", 2): "REJOINING",
    ("DIAG_STATUS", 0): "NOMINAL",
    ("DIAG_STATUS", 1): "DEGRADED",
    ("DIAG_STATUS", 2): "FAULT",
    ("DIAGNOSTIC", 0): "NOMINAL",
    ("DIAGNOSTIC", 1): "DEGRADED",
    ("DIAGNOSTIC", 2): "FAULT",
    ("HEARTBEAT", 0): "HEARTBEAT_OK",
    ("HEARTBEAT", 1): "HEARTBEAT_TIMEOUT",
    ("HEARTBEAT", 2): "BUS_RESET",
    ("MOTOR_ESC", 0): "ESC_NOMINAL",
    ("MOTOR_ESC", 1): "ESC_OVERCURRENT",
    ("MOTOR_ESC", 2): "ESC_OVERTEMP",
    ("MOTOR_ESC", 3): "ESC_PHASE_LOSS",
    ("MOTOR_ESC", 4): "ESC_DESYNC",
    ("BARO_DRIFT", 0): "BARO_NOMINAL",
    ("BARO_DRIFT", 1): "BARO_EXCESSIVE_VARIANCE",
    ("BARO_DRIFT", 2): "BARO_ALTITUDE_JUMP",
    ("COMPASS_SPIKE", 0): "COMPASS_NOMINAL",
    ("COMPASS_SPIKE", 1): "COMPASS_EMI_INTERFERENCE",
    ("COMPASS_SPIKE", 2): "COMPASS_CALIBRATION_LOST",
    ("ERROR_RECORD", 0): "ERR_NOTICE",
    ("ERROR_RECORD", 1): "ERR_RECOVERABLE_FAULT",
    ("ERROR_RECORD", 2): "ERR_CRITICAL_SAFETY_STOP",
}


def extract_error_codes_from_events(events: list[dict[str, Any]]) -> list[str]:
    """Extract error codes, subsystem IDs, and status codes present in decoded telemetry events.

    Extracts:
      - Hex event types ('0x10', '0x20', '0x30', etc.) and integer equivalents ('16', '32', etc.)
      - Status / reason / state codes ('17', '18', '19', '1', '2', etc.)
      - Named symbols ('LIDAR_TIMEOUT', 'COLLISION_RISK', 'LEFT_FORMATION', 'DEGRADED', etc.)
      - ArduPilot DataFlash compound codes ('SUBSYS_14_ECODE_1', 'EV_28', etc.)
    """
    codes: list[str] = []

    def _add(code: Any) -> None:
        if code is None:
            return
        s = str(code).strip()
        if s and s not in codes:
            codes.append(s)

    for ev in events:
        if not isinstance(ev, dict):
            continue

        etype = ev.get("event_type")
        ename = ev.get("event_name") or ""

        if etype is not None:
            _add(str(etype))
            if isinstance(etype, int):
                _add(f"0x{etype:02x}")
                _add(f"0x{etype:02X}")
            elif isinstance(etype, str) and etype.startswith("0x"):
                try:
                    _add(str(int(etype, 16)))
                except ValueError:
                    pass

        if ename:
            _add(str(ename))

        fields = ev.get("fields") or {}
        if isinstance(fields, dict):
            for k in ("status_code", "reason_code", "state_code", "code", "error_code", "ecode", "subsys", "subsystem", "subsystem_code"):
                if k in fields:
                    v = fields[k]
                    _add(str(v))
                    if isinstance(v, int):
                        _add(f"0x{v:02x}")
                        _add(f"0x{v:02X}")

                    # Check FSS symbolic map
                    if isinstance(v, int) and ename:
                        sym = FSS_STATUS_MAP.get((ename, v))
                        if sym:
                            _add(sym)
                            _add(f"{ename}_{v}")
                            _add(f"{ename}:{v}")

            # ArduPilot compound error code & event code
            sub = fields.get("subsystem_code") if fields.get("subsystem_code") is not None else fields.get("subsys")
            if sub is None:
                sub = fields.get("subsystem")
            ecode = fields.get("error_code") if fields.get("error_code") is not None else fields.get("ecode")

            if sub is not None:
                _add(f"SUBSYS_{sub}")
                _add(f"Subsys {sub}")
                _add(f"Subsystem {sub}")
                _add(f"EV_{sub}")
                _add(f"EV {sub}")

            if sub is not None and ecode is not None:
                _add(f"SUBSYS_{sub}_ECODE_{ecode}")
                _add(f"ERR_{sub}_{ecode}")
                _add(f"[ERR-{sub}-{ecode}]")
                _add(f"SUBSYS {sub} CODE {ecode}")
                if ecode == 0:
                    _add("ERROR_RESOLVED")
                    _add("FAILSAFE_CLEARED")
                    _add("ECODE_0")
                    _add(f"SUBSYS_{sub}_CLEARED")
                    _add(f"SUBSYS_{sub}_ECODE_0")
            elif ecode == 0:
                _add("ERROR_RESOLVED")
                _add("FAILSAFE_CLEARED")
                _add("ECODE_0")

            # ArduPilot Event message (EV)
            if "id" in fields and (ename == "EV" or str(etype) == "EV"):
                ev_id = fields["id"]
                _add(f"EV_{ev_id}")
                _add(f"EV {ev_id}")
                _add(f"SUBSYS_{ev_id}")
                _add(f"Subsys {ev_id}")
                _add(f"Subsystem {ev_id}")

            # Common symbolic flags / PX4 error strings
            for val in fields.values():
                if isinstance(val, str) and (val.isupper() or "_" in val or val.startswith("0x")):
                    _add(val)

        # Interpreted fields
        interpreted = ev.get("interpreted_fields") or []
        if isinstance(interpreted, list):
            for item in interpreted:
                if isinstance(item, str):
                    _add(item)
                elif isinstance(item, dict):
                    for v in item.values():
                        if isinstance(v, (str, int)):
                            _add(str(v))

    return codes


def enrich_evidence_with_manual(
    evidence: dict[str, Any],
    catalog: list[VendorErrorEntry],
) -> list[dict[str, Any]]:
    """Extract error codes from evidence['events'], match against catalog, and return enriched fault descriptions."""
    from bridge.hybrid_rag import enrich_evidence_with_manual as _enrich
    return _enrich(evidence, catalog)

