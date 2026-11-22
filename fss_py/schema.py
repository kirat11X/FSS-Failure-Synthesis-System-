"""YAML decoder schemas: event_type -> fields(name, offset, type, size, endian) + enums.

Schemas tell the decoder HOW to read bytes. Every schema is validated at load
time, so the decoder never has to guess about a bad definition.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .contract import MAX_PAYLOAD_LEN, U32_MAX, UNKNOWN_LABEL

# type -> (size in bytes, struct code)
PRIMITIVES: dict[str, tuple[int, str]] = {
    "u8": (1, "B"), "u16": (2, "H"), "u32": (4, "I"), "u64": (8, "Q"),
    "i8": (1, "b"), "i16": (2, "h"), "i32": (4, "i"), "i64": (8, "q"),
    "f32": (4, "f"), "f64": (8, "d"),
    "bool": (1, "B"),
}
TYPE_ALIASES = {
    "uint8": "u8", "uint16": "u16", "uint32": "u32", "uint64": "u64",
    "int8": "i8", "int16": "i16", "int32": "i32", "int64": "i64",
    "float32": "f32", "float64": "f64",
}
INTEGER_TYPES = frozenset({"u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64"})
ENDIANS = {"little": "<", "big": ">"}
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "config" / "payload_schemas.yaml"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FIELD_KEYS = {"name", "offset", "type", "size", "endian", "enum", "enum_field"}
_EVENT_KEYS = {"name", "version", "fields", "payload_length", "strict_payload_length"}
_TOP_KEYS = {"schema_set", "enums", "event_types"}
_PAYLOAD_TOP_KEYS = {"schema_version", "deployment", "defaults", "enums", "events"}
_PAYLOAD_DEFAULT_KEYS = {"byte_order", "offset_base", "strict_payload_length"}
_PAYLOAD_EVENT_KEYS = {
    "name", "description", "payload_length", "strict_payload_length", "byte_order", "fields",
}
_PAYLOAD_FIELD_KEYS = {
    "name", "type", "offset", "size", "endian", "byte_order", "description", "enum",
    "enum_field",
}


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class FieldDef:
    name: str
    offset: int
    type: str
    size: int
    endian: str | None
    enum: str | None = None
    enum_field: str | None = None
    enum_map: Mapping[int, str] | None = None

    @property
    def struct_format(self) -> str:
        return ENDIANS[self.endian or "little"] + PRIMITIVES[self.type][1]


@dataclass(frozen=True)
class EventSchema:
    event_type: int
    name: str
    version: int | str
    fields: tuple[FieldDef, ...]  # sorted by offset
    payload_length: int | None = None
    strict_payload_length: bool = False


class SchemaRegistry:
    def __init__(self, schemas: Iterable[EventSchema], schema_set: str | None = None) -> None:
        self.schema_set = schema_set
        self._by_type: dict[int, EventSchema] = {}
        self._by_name: dict[str, int] = {}
        for s in schemas:
            if s.event_type in self._by_type:
                raise SchemaError(f"duplicate event_type 0x{s.event_type:X}")
            if s.name in self._by_name:
                raise SchemaError(f"duplicate event name {s.name!r}")
            self._by_type[s.event_type] = s
            self._by_name[s.name] = s.event_type

    def get(self, event_type: int) -> EventSchema | None:
        return self._by_type.get(event_type)

    def type_id(self, name: str) -> int:
        """Map an event name (e.g. "LIDAR_STATUS") to the numeric id retrieval needs."""
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"no event type named {name!r} in schema") from None

    @property
    def event_schemas(self) -> tuple[EventSchema, ...]:
        """Return configured event types for query planning and user-facing labels."""
        return tuple(self._by_type.values())

    def __contains__(self, event_type: object) -> bool:
        return event_type in self._by_type

    def __len__(self) -> int:
        return len(self._by_type)


# ------------------------------------------------------------------ loading
class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last one."""


def _construct_unique_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> dict:
    loader.flatten_mapping(node)
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in mapping
        except TypeError:
            raise SchemaError(f"unhashable mapping key at line {key_node.start_mark.line + 1}") from None
        if duplicate:
            raise SchemaError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                              _construct_unique_mapping)


def load_schema_text(text: str) -> SchemaRegistry:
    try:
        doc = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise SchemaError(f"invalid YAML: {exc}") from None
    return parse_schema(doc)


def load_schema_file(path: str | Path) -> SchemaRegistry:
    return load_schema_text(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------- validation
def _is_int(value: object) -> bool:
    return type(value) is int  # rejects bool (YAML `yes` -> True)


def _require_int(value: object, what: str, lo: int, hi: int) -> int:
    if not _is_int(value) or not lo <= value <= hi:
        raise SchemaError(f"{what} must be an integer in [{lo}, {hi}], got {value!r}")
    return value


def _require_ident(value: object, what: str) -> str:
    if not isinstance(value, str) or not _IDENT.match(value):
        raise SchemaError(f"{what} must be an identifier string, got {value!r}")
    return value


def _only_keys(mapping: dict, allowed: set[str], where: str) -> None:
    extra = set(mapping) - allowed
    if extra:
        raise SchemaError(f"{where}: unknown key(s) {sorted(map(str, extra))}; allowed: "
                          f"{sorted(allowed)}")


def _type_range(type_name: str) -> tuple[int, int]:
    bits = PRIMITIVES[type_name][0] * 8
    if type_name.startswith("u"):
        return 0, 2**bits - 1
    return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1


def _parse_enums(raw: object) -> dict[str, Mapping[int, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SchemaError("enums must be a mapping of enum name -> {code: LABEL}")
    enums: dict[str, Mapping[int, str]] = {}
    for enum_name, values in raw.items():
        _require_ident(enum_name, "enum name")
        if not isinstance(values, dict) or not values:
            raise SchemaError(f"enum {enum_name!r} must be a non-empty mapping of code -> label")
        for code, label in values.items():
            if not _is_int(code):
                raise SchemaError(f"enum {enum_name!r}: code {code!r} must be an integer")
            if not isinstance(label, str) or not label:
                raise SchemaError(f"enum {enum_name!r}: label for {code} must be a non-empty "
                                  f"string, got {label!r} (quote YAML words like ON/NO)")
            if label == UNKNOWN_LABEL:
                raise SchemaError(f"enum {enum_name!r}: label {UNKNOWN_LABEL!r} is reserved for "
                                  f"unmapped codes (code {code})")
        enums[enum_name] = MappingProxyType(dict(values))
    return enums


def _parse_field(raw: object, where: str, enums: dict[str, Mapping[int, str]]) -> FieldDef:
    if not isinstance(raw, dict):
        raise SchemaError(f"{where}: each field must be a mapping")
    _only_keys(raw, _FIELD_KEYS, where)
    missing = {"name", "offset", "type", "size"} - set(raw)
    if missing:
        raise SchemaError(f"{where}: missing required key(s) {sorted(missing)}")
    name = _require_ident(raw["name"], f"{where} name")
    where = f"{where} {name!r}"
    type_name = raw["type"]
    if type_name not in PRIMITIVES:
        raise SchemaError(f"{where}: unknown type {type_name!r}; supported: {sorted(PRIMITIVES)}")
    type_size = PRIMITIVES[type_name][0]
    size = _require_int(raw["size"], f"{where} size", 1, MAX_PAYLOAD_LEN)
    if size != type_size:
        raise SchemaError(f"{where}: size {size} does not match type {type_name} "
                          f"({type_size} bytes)")
    offset = _require_int(raw["offset"], f"{where} offset", 0, MAX_PAYLOAD_LEN - size)
    endian = raw.get("endian")
    if endian is None and size > 1:
        raise SchemaError(f"{where}: endian (little/big) is required for {size}-byte types")
    if endian is not None and endian not in ENDIANS:
        raise SchemaError(f"{where}: endian must be 'little' or 'big', got {endian!r}")

    enum_name = raw.get("enum")
    enum_field = raw.get("enum_field")
    enum_map = None
    if enum_name is None:
        if enum_field is not None:
            raise SchemaError(f"{where}: enum_field given without enum")
    else:
        if type_name not in INTEGER_TYPES:
            raise SchemaError(f"{where}: enum is only allowed on integer types, not {type_name}")
        if enum_name not in enums:
            raise SchemaError(f"{where}: enum {enum_name!r} is not defined")
        enum_map = enums[enum_name]
        lo, hi = _type_range(type_name)
        for code in enum_map:
            if not lo <= code <= hi:
                raise SchemaError(f"{where}: enum {enum_name!r} code {code} does not fit "
                                  f"{type_name} [{lo}, {hi}]")
        enum_field = _require_ident(enum_field if enum_field is not None else f"{name}_name",
                                    f"{where} enum_field")
    return FieldDef(name, offset, type_name, size, endian, enum_name, enum_field, enum_map)


def _parse_event(type_id: object, raw: object,
                 enums: dict[str, Mapping[int, str]]) -> EventSchema:
    event_type = _require_int(type_id, f"event type id {type_id!r}", 0, U32_MAX)
    where = f"event_type 0x{event_type:X}"
    if not isinstance(raw, dict):
        raise SchemaError(f"{where}: definition must be a mapping")
    _only_keys(raw, _EVENT_KEYS, where)
    name = _require_ident(raw.get("name"), f"{where} name")
    where = f"{where} ({name})"
    raw_version = raw.get("version")
    if _is_int(raw_version):
        version: int | str = _require_int(raw_version, f"{where} version", 0, U32_MAX)
    elif isinstance(raw_version, str) and raw_version.strip():
        version = raw_version
    else:
        raise SchemaError(f"{where} version must be an integer or non-empty string, "
                          f"got {raw_version!r}")
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list):
        raise SchemaError(f"{where}: fields must be a list (it may be empty)")
    fields = sorted((_parse_field(f, f"{where} field #{i}", enums) for i, f in enumerate(raw_fields)),
                    key=lambda f: f.offset)

    seen: set[str] = set()
    for f in fields:
        for out_name in (f.name, f.enum_field):
            if out_name is None:
                continue
            if out_name in seen:
                raise SchemaError(f"{where}: output field name {out_name!r} is used twice")
            seen.add(out_name)
    for a, b in zip(fields, fields[1:]):
        if a.offset + a.size > b.offset:
            raise SchemaError(f"{where}: fields {a.name!r} (bytes {a.offset}..{a.offset + a.size - 1})"
                              f" and {b.name!r} (bytes {b.offset}..{b.offset + b.size - 1}) overlap")
    payload_length = raw.get("payload_length")
    if payload_length is not None:
        payload_length = _require_int(payload_length, f"{where} payload_length", 0, MAX_PAYLOAD_LEN)
        if fields and fields[-1].offset + fields[-1].size > payload_length:
            last = fields[-1]
            raise SchemaError(f"{where}: field {last.name!r} ends at byte "
                              f"{last.offset + last.size - 1}, beyond payload_length "
                              f"{payload_length}")
    strict_payload_length = raw.get("strict_payload_length", False)
    if type(strict_payload_length) is not bool:
        raise SchemaError(f"{where} strict_payload_length must be a boolean")
    if strict_payload_length and payload_length is None:
        raise SchemaError(f"{where}: strict_payload_length requires payload_length")
    return EventSchema(event_type, name, version, tuple(fields), payload_length,
                       strict_payload_length)


def parse_schema(doc: Any) -> SchemaRegistry:
    if not isinstance(doc, dict):
        raise SchemaError("schema document must be a mapping")
    if "events" in doc:
        return _parse_payload_schema(doc)
    _only_keys(doc, _TOP_KEYS, "top level")
    schema_set = doc.get("schema_set")
    if schema_set is not None and not isinstance(schema_set, str):
        raise SchemaError("schema_set must be a string")
    enums = _parse_enums(doc.get("enums"))
    event_types = doc.get("event_types")
    if not isinstance(event_types, dict) or not event_types:
        raise SchemaError("event_types must be a non-empty mapping of event_type id -> definition")
    return SchemaRegistry((_parse_event(k, v, enums) for k, v in event_types.items()), schema_set)


def _parse_payload_schema(doc: dict[str, Any]) -> SchemaRegistry:
    _only_keys(doc, _PAYLOAD_TOP_KEYS, "top level")

    schema_version = doc.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise SchemaError("schema_version must be a non-empty string")
    deployment = doc.get("deployment")
    if not isinstance(deployment, str) or not deployment.strip():
        raise SchemaError("deployment must be a non-empty string")

    defaults = doc.get("defaults", {})
    if not isinstance(defaults, dict):
        raise SchemaError("defaults must be a mapping")
    _only_keys(defaults, _PAYLOAD_DEFAULT_KEYS, "defaults")
    default_byte_order = defaults.get("byte_order", "little")
    if not isinstance(default_byte_order, str) or default_byte_order not in ENDIANS:
        raise SchemaError("defaults byte_order must be 'little' or 'big'")
    offset_base = _require_int(defaults.get("offset_base", 0), "defaults offset_base", 0, 1)
    default_strict_length = defaults.get("strict_payload_length", True)
    if type(default_strict_length) is not bool:
        raise SchemaError("defaults strict_payload_length must be a boolean")

    enum_maps = _parse_payload_enums(doc.get("enums"))
    raw_events = doc.get("events")
    if not isinstance(raw_events, dict) or not raw_events:
        raise SchemaError("events must be a non-empty mapping of event id -> definition")

    normalized_events: list[EventSchema] = []
    seen_event_ids: set[int] = set()
    for type_id, raw_event in raw_events.items():
        event_type = _require_int(type_id, f"event type id {type_id!r}", 0, U32_MAX)
        if event_type in seen_event_ids:
            raise SchemaError(f"duplicate event_type 0x{event_type:X}")
        seen_event_ids.add(event_type)
        where = f"event_type 0x{event_type:X}"
        if not isinstance(raw_event, dict):
            raise SchemaError(f"{where}: definition must be a mapping")
        _only_keys(raw_event, _PAYLOAD_EVENT_KEYS, where)
        if "description" in raw_event and not isinstance(raw_event["description"], str):
            raise SchemaError(f"{where} description must be a string")
        name = _require_ident(raw_event.get("name"), f"{where} name")
        payload_length = _require_int(raw_event.get("payload_length"),
                                      f"{where} payload_length", 0, MAX_PAYLOAD_LEN)
        strict_length = raw_event.get("strict_payload_length", default_strict_length)
        if type(strict_length) is not bool:
            raise SchemaError(f"{where} strict_payload_length must be a boolean")

        event_byte_order = raw_event.get("byte_order", default_byte_order)
        if not isinstance(event_byte_order, str) or event_byte_order not in ENDIANS:
            raise SchemaError(f"{where} byte_order must be 'little' or 'big'")
        raw_fields = raw_event.get("fields")
        if not isinstance(raw_fields, list):
            raise SchemaError(f"{where}: fields must be a list (it may be empty)")

        fields: list[dict[str, Any]] = []
        for index, raw_field in enumerate(raw_fields):
            field_where = f"{where} field #{index}"
            if not isinstance(raw_field, dict):
                raise SchemaError(f"{field_where}: each field must be a mapping")
            _only_keys(raw_field, _PAYLOAD_FIELD_KEYS, field_where)
            if "description" in raw_field and not isinstance(raw_field["description"], str):
                raise SchemaError(f"{field_where} description must be a string")
            field_name = _require_ident(raw_field.get("name"), f"{field_where} name")
            raw_type = raw_field.get("type")
            if not isinstance(raw_type, str):
                raise SchemaError(f"{field_where} type must be a supported type name")
            type_name = TYPE_ALIASES.get(raw_type, raw_type)
            if type_name not in PRIMITIVES:
                raise SchemaError(f"{field_where}: unknown type {raw_type!r}; supported: "
                                  f"{sorted(set(PRIMITIVES) | set(TYPE_ALIASES))}")
            type_size = PRIMITIVES[type_name][0]
            size = _require_int(raw_field.get("size", type_size), f"{field_where} size",
                                1, MAX_PAYLOAD_LEN)
            if size != type_size:
                raise SchemaError(f"{field_where}: size {size} does not match type {raw_type} "
                                  f"({type_size} bytes)")
            raw_offset = _require_int(raw_field.get("offset"), f"{field_where} offset",
                                      offset_base, MAX_PAYLOAD_LEN - size + offset_base)
            offset = raw_offset - offset_base

            if "endian" in raw_field and "byte_order" in raw_field:
                if raw_field["endian"] != raw_field["byte_order"]:
                    raise SchemaError(f"{field_where}: endian and byte_order conflict")
            endian = raw_field.get("endian", raw_field.get("byte_order", event_byte_order))
            if not isinstance(endian, str) or endian not in ENDIANS:
                raise SchemaError(f"{field_where}: endian/byte_order must be 'little' or 'big'")

            field_def: dict[str, Any] = {
                "name": field_name,
                "offset": offset,
                "type": type_name,
                "size": size,
                "endian": endian,
            }
            if "enum_field" in raw_field:
                field_def["enum_field"] = raw_field["enum_field"]
            raw_enum = raw_field.get("enum")
            if isinstance(raw_enum, dict):
                enum_name = f"event_{event_type}_{field_name}"
                if enum_name in enum_maps:
                    raise SchemaError(f"{field_where}: duplicate inline enum name {enum_name!r}")
                enum_maps[enum_name] = _parse_payload_enum(raw_enum, field_where)
                field_def["enum"] = enum_name
            elif raw_enum is not None:
                enum_name = _require_ident(raw_enum, f"{field_where} enum")
                if enum_name not in enum_maps:
                    raise SchemaError(f"{field_where}: enum {enum_name!r} is not defined")
                field_def["enum"] = enum_name
            fields.append(field_def)

        normalized_events.append(
            _parse_event(
                event_type,
                {
                    "name": name,
                    "version": schema_version,
                    "fields": fields,
                    "payload_length": payload_length,
                    "strict_payload_length": strict_length,
                },
                enum_maps,
            )
        )

    return SchemaRegistry(normalized_events, deployment)


def _parse_payload_enums(raw: object) -> dict[str, Mapping[int, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SchemaError("enums must be a mapping of enum name -> {code: LABEL}")
    enums: dict[str, Mapping[int, str]] = {}
    for enum_name, values in raw.items():
        _require_ident(enum_name, "enum name")
        enums[enum_name] = _parse_payload_enum(values, f"enum {enum_name!r}")
    return enums


def _parse_payload_enum(raw: object, where: str) -> Mapping[int, str]:
    if not isinstance(raw, dict) or not raw:
        raise SchemaError(f"{where}: enum must be a non-empty mapping of code -> label")
    values: dict[int, str] = {}
    for code, label in raw.items():
        if not _is_int(code):
            raise SchemaError(f"{where}: enum code {code!r} must be an integer")
        if not isinstance(label, str) or not label:
            raise SchemaError(f"{where}: enum label for {code} must be a non-empty string "
                              f"(quote YAML words like ON/NO)")
        values[code] = label
    return MappingProxyType(values)
