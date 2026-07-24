from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Iterable, Mapping


CALL_LEDGER_KEY = "astrmai_llm_call_ledger"
CONTEXT_BLOCK_STATS_KEY = "astrmai_context_block_stats"
REPLY_STATS_KEY = "astrmai_reply_stats"
_MAX_ENTRIES = 128
_MAX_BLOCKS = 64


def _safe_len(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    try:
        return len(value)
    except Exception:
        return len(str(value))


def _text_hash(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _read_list(event: Any, key: str) -> list[dict[str, Any]]:
    if event is None or not hasattr(event, "get_extra"):
        return []
    value = event.get_extra(key, [])
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _write_list(event: Any, key: str, value: Iterable[Mapping[str, Any]]) -> None:
    if event is None or not hasattr(event, "set_extra"):
        return
    event.set_extra(key, [dict(item) for item in list(value)[-_MAX_ENTRIES:]])


def _count_tools(tools: Any) -> int:
    if tools is None:
        return 0
    if isinstance(tools, Mapping):
        for key in ("tools", "functions", "items"):
            value = tools.get(key)
            if isinstance(value, (list, tuple, set)):
                return len(value)
        return len(tools)
    for attr in ("tools", "functions", "items"):
        value = getattr(tools, attr, None)
        if isinstance(value, (list, tuple, set)):
            return len(value)
    try:
        return len(tools)
    except Exception:
        return 0


def begin_llm_call(
    event: Any,
    *,
    stage: str,
    family: str = "",
    pool: str = "",
    prompt: Any = "",
    system_prompt: Any = "",
    contexts: Iterable[Any] | None = None,
    tools: Any = None,
    max_steps: int = 0,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Record a privacy-safe logical LLM call on the current event."""
    if event is None or not hasattr(event, "set_extra") or not hasattr(event, "get_extra"):
        return ""
    context_items = list(contexts or [])
    call_id = uuid.uuid4().hex[:12]
    entry: dict[str, Any] = {
        "call_id": call_id,
        "stage": str(stage or ""),
        "family": str(family or ""),
        "pool": str(pool or ""),
        "status": "pending",
        "started_at": time.time(),
        "elapsed_ms": 0.0,
        "system_chars": _safe_len(system_prompt),
        "prompt_chars": _safe_len(prompt),
        "context_count": len(context_items),
        "context_chars": sum(_safe_len(item) for item in context_items),
        "context_item_max_chars": max((_safe_len(item) for item in context_items), default=0),
        "tool_count": _count_tools(tools),
        "max_steps": max(0, int(max_steps or 0)),
    }
    if metadata:
        entry["metadata"] = {
            str(key): value
            for key, value in metadata.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    entries = _read_list(event, CALL_LEDGER_KEY)
    entries.append(entry)
    _write_list(event, CALL_LEDGER_KEY, entries)
    return call_id


def finish_llm_call(
    event: Any,
    call_id: str,
    *,
    status: str = "success",
    model: str = "",
    provider: str = "",
    output: Any = "",
    error_kind: str = "",
    error: str = "",
    attempts: int = 0,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if not call_id:
        return
    entries = _read_list(event, CALL_LEDGER_KEY)
    now = time.time()
    for entry in reversed(entries):
        if str(entry.get("call_id", "")) != call_id:
            continue
        started_at = float(entry.get("started_at", now) or now)
        entry.update(
            {
                "status": str(status or "success"),
                "finished_at": now,
                "elapsed_ms": round(max(0.0, now - started_at) * 1000, 1),
                "model": str(model or ""),
                "provider": str(provider or ""),
                "output_chars": _safe_len(output),
                "attempts": max(0, int(attempts or 0)),
                "error_kind": str(error_kind or ""),
                "error": str(error or "")[:240],
            }
        )
        if metadata:
            merged = dict(entry.get("metadata") or {})
            merged.update(
                {
                    str(key): value
                    for key, value in metadata.items()
                    if isinstance(value, (str, int, float, bool)) or value is None
                }
            )
            entry["metadata"] = merged
        break
    _write_list(event, CALL_LEDGER_KEY, entries)


def record_context_block_stats(
    event: Any,
    *,
    stage: str,
    blocks: Mapping[str, Any],
    total_chars: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Store block sizes and hashes, never the block contents."""
    if event is None or not hasattr(event, "set_extra") or not hasattr(event, "get_extra"):
        return
    normalized: dict[str, dict[str, Any]] = {}
    for name, value in list(blocks.items())[:_MAX_BLOCKS]:
        text = str(value or "")
        normalized[str(name)] = {
            "chars": len(text),
            "nonempty": bool(text.strip()),
            "hash": _text_hash(text),
        }
    payload = {
        "stage": str(stage or ""),
        "recorded_at": time.time(),
        "total_chars": int(total_chars if total_chars is not None else sum(item["chars"] for item in normalized.values())),
        "nonempty_count": sum(1 for item in normalized.values() if item["nonempty"]),
        "blocks": normalized,
    }
    if metadata:
        payload["metadata"] = {
            str(key): value
            for key, value in metadata.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    existing = _read_list(event, CONTEXT_BLOCK_STATS_KEY)
    existing.append(payload)
    _write_list(event, CONTEXT_BLOCK_STATS_KEY, existing)


def record_reply_stats(
    event: Any,
    *,
    segment_count: int,
    segment_lengths: Iterable[int],
    total_chars: int,
    strategy: str = "",
    send_status: str = "",
    sent_segment_count: int = 0,
) -> None:
    if event is None or not hasattr(event, "set_extra"):
        return
    event.set_extra(
        REPLY_STATS_KEY,
        {
            "segment_count": max(0, int(segment_count or 0)),
            "segment_lengths": [max(0, int(length or 0)) for length in list(segment_lengths)[:32]],
            "total_chars": max(0, int(total_chars or 0)),
            "strategy": str(strategy or ""),
            "send_status": str(send_status or ""),
            "sent_segment_count": max(0, int(sent_segment_count or 0)),
        },
    )


__all__ = [
    "CALL_LEDGER_KEY",
    "CONTEXT_BLOCK_STATS_KEY",
    "REPLY_STATS_KEY",
    "begin_llm_call",
    "finish_llm_call",
    "record_context_block_stats",
    "record_reply_stats",
]
