"""Shared defensive parsing for structured background-model responses."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

try:
    from json_repair import repair_json
except ImportError:  # pragma: no cover - optional dependency in minimal hosts
    repair_json = None


@dataclass(frozen=True)
class JsonParseResult:
    value: Any
    stage: str
    repair_attempted: bool = False


@dataclass(frozen=True)
class JsonContractResult:
    value: Any
    parse_stage: str
    schema_valid: bool
    missing_keys: tuple[str, ...] = ()
    unexpected_keys: tuple[str, ...] = ()
    invalid_type_keys: tuple[str, ...] = ()
    repair_attempted: bool = False
    retry_count: int = 0
    terminal_status: str = "parsed"


def _fenced_candidates(text: str) -> list[tuple[str, str]]:
    normalized = str(text or "").strip()
    candidates = [(normalized, "raw")]
    match = re.search(r"```(?:json)?\s*(.*?)```", normalized, re.DOTALL | re.IGNORECASE)
    if match:
        fenced = match.group(1).strip()
        if fenced and fenced != normalized:
            candidates.insert(0, (fenced, "code_fence_normalized"))
    return candidates


def _wrap_naked_members(text: str, allowed_keys: Iterable[str] | None) -> str | None:
    body = str(text or "").strip()
    keys = [str(key).strip() for key in (allowed_keys or ()) if str(key).strip()]
    if not body or not keys:
        return None
    key_pattern = r'"(?:' + "|".join(re.escape(key) for key in keys) + r')"\s*:'
    first = re.search(key_pattern, body)
    if first is None:
        return None
    body = body[first.start() :].rstrip("` \t\r\n,")
    return "{" + body + "}"


def parse_json_payload(
    raw: Any,
    *,
    allowed_keys: Iterable[str] | None = None,
    allow_naked_members: bool = False,
) -> JsonParseResult:
    """Parse an object/list response without silently accepting prose.

    The parser is intentionally schema-agnostic; callers perform the final
    type/required-key validation for their task.  Naked-member repair is only
    enabled when callers provide an allow-list, preventing ordinary prose from
    becoming a JSON object.
    """
    if isinstance(raw, (dict, list)):
        return JsonParseResult(raw, "native")
    text = str(raw or "").strip()
    if not text:
        raise ValueError("json_empty")

    candidates = _fenced_candidates(text)
    decoder = json.JSONDecoder()
    saw_structured = False
    malformed = ""

    if allow_naked_members:
        for candidate, _stage in candidates:
            wrapped = _wrap_naked_members(candidate, allowed_keys)
            if wrapped is None:
                continue
            try:
                return JsonParseResult(json.loads(wrapped), "naked_members_repaired", True)
            except json.JSONDecodeError:
                malformed = wrapped

    for candidate, stage in candidates:
        try:
            return JsonParseResult(json.loads(candidate), stage)
        except (TypeError, json.JSONDecodeError):
            pass
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            saw_structured = True
            malformed = malformed or candidate[index:]
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            return JsonParseResult(value, "embedded_object" if index else stage)

    # Arrays are checked after naked object members. Otherwise an array-valued
    # member such as `"tags": [...]` would be mistaken for the top-level JSON.
    for candidate, stage in candidates:
        for index, char in enumerate(candidate):
            if char != "[":
                continue
            saw_structured = True
            malformed = malformed or candidate[index:]
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            return JsonParseResult(value, "embedded_array" if index else stage)

    if repair_json is not None:
        repair_input = malformed or candidates[0][0]
        try:
            repaired = repair_json(repair_input, return_objects=True)
        except Exception as exc:
            raise ValueError(f"json_repair_failed:{type(exc).__name__}") from exc
        if isinstance(repaired, (dict, list)):
            return JsonParseResult(repaired, "json_repaired", True)

    raise ValueError("json_malformed" if saw_structured else "json_no_object")


def parse_json_contract(
    raw: Any,
    *,
    required_keys: Iterable[str] = (),
    optional_keys: Iterable[str] = (),
    field_types: Mapping[str, type | tuple[type, ...]] | None = None,
    allow_extra_keys: bool = False,
    allow_naked_members: bool = False,
    retry_count: int = 0,
) -> JsonContractResult:
    """Parse and validate one task-specific structured model response."""
    required = tuple(dict.fromkeys(str(key) for key in required_keys))
    optional = tuple(dict.fromkeys(str(key) for key in optional_keys))
    allowed = tuple(dict.fromkeys((*required, *optional)))
    try:
        parsed = parse_json_payload(
            raw,
            allowed_keys=allowed,
            allow_naked_members=allow_naked_members,
        )
    except ValueError as exc:
        return JsonContractResult(
            value=None,
            parse_stage=str(exc),
            schema_valid=False,
            retry_count=max(0, int(retry_count or 0)),
            terminal_status="parse_failed",
        )
    value = parsed.value
    if not isinstance(value, dict):
        return JsonContractResult(
            value=value,
            parse_stage=parsed.stage,
            schema_valid=False,
            repair_attempted=parsed.repair_attempted,
            retry_count=max(0, int(retry_count or 0)),
            terminal_status="schema_invalid",
        )
    missing = tuple(key for key in required if key not in value)
    unexpected = tuple(
        sorted(str(key) for key in value if allowed and str(key) not in allowed)
    )
    invalid_types: list[str] = []
    for key, expected in dict(field_types or {}).items():
        if key in value and not isinstance(value[key], expected):
            invalid_types.append(str(key))
    valid = not missing and not invalid_types and (allow_extra_keys or not unexpected)
    return JsonContractResult(
        value=value,
        parse_stage=parsed.stage,
        schema_valid=valid,
        missing_keys=missing,
        unexpected_keys=unexpected,
        invalid_type_keys=tuple(invalid_types),
        repair_attempted=parsed.repair_attempted,
        retry_count=max(0, int(retry_count or 0)),
        terminal_status="parsed" if valid else "schema_invalid",
    )


__all__ = [
    "JsonContractResult",
    "JsonParseResult",
    "parse_json_contract",
    "parse_json_payload",
]
