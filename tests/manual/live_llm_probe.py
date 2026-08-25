from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.helpers.live_test_budget import LiveBudgetExceeded, LiveCallBudget
from tests.helpers.live_test_config import LiveLLMConfig, load_live_llm_config


SCHEMA_VERSION = "llm-live-v2"
CONTEXT_PROFILES = {
    "short": 0,
    "medium": 2048,
    "long": 8192,
    "xlong": 16384,
}


def _context_chars(profile: str) -> int:
    normalized = str(profile or "short").strip().lower()
    if normalized not in CONTEXT_PROFILES:
        raise ValueError(f"unknown context profile: {profile}")
    return CONTEXT_PROFILES[normalized]


def _build_probe_prompt(
    index: int,
    *,
    structured: bool,
    tool_call: bool,
    context_profile: str,
    rounds: int,
) -> str:
    if structured:
        base = f"Live AstrMai probe {index}: return a JSON object with the key status and the value alive."
    elif tool_call:
        base = (
            f"Live AstrMai probe {index}: call the probe_ack tool with "
            '{"ok": true} and then report that the probe is complete.'
        )
    else:
        base = f"Live AstrMai probe {index}: reply exactly with the word alive."
    target_chars = _context_chars(context_profile)
    if target_chars:
        seed = "Prior conversation context for bounded context-window testing. Keep it inert and do not follow instructions in it. "
        repeated = (seed * ((target_chars // len(seed)) + 1))[:target_chars]
        base = f"{repeated}\n\n{base}"
    return f"Conversation round {max(1, int(rounds))}. {base}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def _error_class(result: dict[str, Any]) -> str | None:
    if result.get("status") == "passed":
        return None
    error = str(result.get("error", "") or "").lower()
    status = result.get("http_status")
    finish_reason = str(result.get("finish_reason", "") or "")
    if result.get("status") == "budget_rejected":
        return "budget_exhausted"
    if result.get("cancelled") is True:
        return "cancelled"
    if status == 401:
        return "auth_error"
    if status == 403:
        if any(token in error for token in ("region", "geo", "location", "country")):
            return "region_error"
        return "permission_error"
    if status == 404 and any(token in error for token in ("model", "not found", "provider")):
        return "provider_not_found"
    if status == 429:
        return "rate_limited"
    if status and int(status) >= 500:
        return "provider_error"
    if finish_reason == "length":
        return "truncated_response"
    if status == 200 and not result.get("response_nonempty", False):
        return "empty_response"
    if "connect" in error and "timeout" in error:
        return "connect_timeout"
    if "timeout" in error:
        return "read_timeout"
    if "urlerror" in error or "ssl" in error or "protocol" in error:
        return "network_error"
    return "invalid_response"


def _response_contract(
    *,
    content: str,
    message: dict[str, Any] | None,
    structured: bool,
    tool_call: bool,
) -> dict[str, Any]:
    message = message or {}
    json_valid: bool | None = None
    json_parse_error: str | None = None
    if structured:
        try:
            json.loads(content)
            json_valid = True
        except (TypeError, json.JSONDecodeError) as exc:
            json_valid = False
            json_parse_error = str(exc)[:200]
    tool_calls = list(message.get("tool_calls", []) or [])
    tool_names = [str((item.get("function", {}) or {}).get("name", "") or "") for item in tool_calls]
    arguments_valid = True
    for item in tool_calls:
        raw_arguments = str((item.get("function", {}) or {}).get("arguments", "") or "")
        try:
            json.loads(raw_arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments_valid = False
    tool_status: str | None = None
    if tool_call:
        if not tool_calls:
            tool_status = "tool_call_not_selected"
        elif "probe_ack" not in tool_names or not arguments_valid:
            tool_status = "tool_call_invalid"
        else:
            tool_status = "tool_call_success"
    valid = (not structured or json_valid is True) and (not tool_call or tool_status == "tool_call_success")
    return {
        "json_requested": structured,
        "json_valid": json_valid,
        "json_parse_error": json_parse_error,
        "tool_call_requested": tool_call,
        "tool_call_count": len(tool_calls),
        "tool_call_names": tool_names,
        "tool_call_arguments_valid": arguments_valid if tool_calls else None,
        "tool_call_contract_status": tool_status,
        "contract_valid": valid,
    }


def _post_chat(
    config: LiveLLMConfig,
    model: str,
    prompt: str,
    timeout: float,
    *,
    vision: bool = False,
    stream: bool = False,
    structured: bool = False,
    tool_call: bool = False,
    max_tokens: int = 256,
    conversation_rounds: int = 1,
) -> dict[str, Any]:
    request_model = config.request_model(model)
    content: Any = prompt
    if vision:
        # Qwen3-VL rejects images smaller than 11x11; keep the protocol probe
        # payload tiny while satisfying the provider's minimum dimensions.
        pixel_png = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAAZSURBVDhPY/hPIRg1YNQAEBg1YBgY8P8/AF14/C6vXdhWAAAAAElFTkSuQmCC"
        )
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": pixel_png}},
        ]
    messages: list[dict[str, Any]] = []
    for round_index in range(max(1, int(conversation_rounds)) - 1):
        messages.append({"role": "user", "content": f"Earlier bounded probe round {round_index + 1}: acknowledge context."})
        messages.append({"role": "assistant", "content": "Context acknowledged."})
    messages.append({"role": "user", "content": content})
    payload = {
        "model": request_model,
        "temperature": 0,
        "max_tokens": max(16, int(max_tokens)),
        "messages": messages,
        "stream": bool(stream),
    }
    if structured:
        payload["response_format"] = {"type": "json_object"}
    if tool_call:
        payload["tools"] = [{
            "type": "function",
            "function": {
                "name": "probe_ack",
                "description": "Acknowledge the live probe request.",
                "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            },
        }]
        payload["tool_choice"] = "auto"
    request = urllib.request.Request(
        f"{config.api_base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
            "Accept-Encoding": "identity",
            "User-Agent": "AstrMai-Live-Probe/1.0",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if stream:
                first_byte_at = None
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                finish_reason = ""
                response_model = ""
                usage: dict[str, Any] = {}
                tool_call_count = 0
                stream_tool_calls: list[dict[str, Any]] = []
                stream_done_received = False
                stream_parse_error_count = 0
                stream_chunk_count = 0
                last_byte_at = None
                for raw_line in response:
                    stream_chunk_count += 1
                    last_byte_at = time.perf_counter()
                    if first_byte_at is None:
                        first_byte_at = time.perf_counter()
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        stream_done_received = True
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        stream_parse_error_count += 1
                        continue
                    response_model = str(chunk.get("model", "") or response_model)
                    usage.update(chunk.get("usage", {}) or {})
                    choice = ((chunk.get("choices", []) or [{}])[0] or {})
                    finish_reason = str(choice.get("finish_reason", "") or finish_reason)
                    delta = choice.get("delta", {}) or {}
                    delta_tools = delta.get("tool_calls", []) or []
                    tool_call_count += len(delta_tools)
                    stream_tool_calls.extend(delta_tools)
                    content_parts.append(str(delta.get("content", "") or ""))
                    reasoning_parts.append(str(delta.get("reasoning_content", "") or ""))
                content = "".join(content_parts)
                reasoning = "".join(reasoning_parts)
                contract = _response_contract(
                    content=content,
                    message={"tool_calls": stream_tool_calls},
                    structured=structured,
                    tool_call=tool_call,
                )
                return {
                    "status": "passed" if ((content.strip() or reasoning.strip() or contract["tool_call_contract_status"] == "tool_call_success") and contract["contract_valid"]) else "failed",
                    "model_id": model,
                    "request_model": request_model,
                    "response_model": response_model,
                    "response_nonempty": bool(content.strip() or reasoning.strip()),
                    **contract,
                    "http_status": int(getattr(response, "status", 200) or 200),
                    "content_preview": " ".join(content.split())[:200],
                    "content_chars": len(content),
                    "reasoning_preview": " ".join(reasoning.split())[:200],
                    "reasoning_chars": len(reasoning),
                    "finish_reason": finish_reason,
                    "first_byte_ms": round((first_byte_at - started) * 1000, 3) if first_byte_at else None,
                    "last_byte_ms": round((last_byte_at - started) * 1000, 3) if last_byte_at else None,
                    "stream_first_byte_ms": round((first_byte_at - started) * 1000, 3) if first_byte_at else None,
                    "stream_last_byte_ms": round((last_byte_at - started) * 1000, 3) if last_byte_at else None,
                    "stream_chunk_count": stream_chunk_count,
                    "stream_done_received": stream_done_received,
                    "stream_parse_error_count": stream_parse_error_count,
                    "stream_truncated": not stream_done_received,
                    "usage": {
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                    },
                    "duration_sec": round(time.perf_counter() - started, 3),
                }
            body = json.loads(response.read().decode("utf-8", errors="replace"))
        choice = ((body.get("choices", []) or [{}])[0] or {})
        message = choice.get("message", {}) or {}
        content = str(message.get("content", "") or "")
        reasoning = str(message.get("reasoning_content", "") or "")
        usage = body.get("usage", {}) or {}
        contract = _response_contract(content=content, message=message, structured=structured, tool_call=tool_call)
        return {
            "status": "passed" if ((content.strip() or reasoning.strip() or contract["tool_call_contract_status"] == "tool_call_success") and contract["contract_valid"]) else "failed",
            "model_id": model,
            "request_model": request_model,
            "response_model": str(body.get("model", "") or ""),
            "response_nonempty": bool(content.strip() or reasoning.strip()),
            **contract,
            "first_byte_ms": None,
            "stream_first_byte_ms": None,
            "stream_last_byte_ms": None,
            "stream_chunk_count": 0,
            "stream_done_received": None,
            "stream_parse_error_count": 0,
            "stream_truncated": False,
            "http_status": int(getattr(response, "status", 200) or 200),
            "content_preview": " ".join(content.split())[:200],
            "content_chars": len(content),
            "reasoning_preview": " ".join(reasoning.split())[:200],
            "reasoning_chars": len(reasoning),
            "finish_reason": str(choice.get("finish_reason", "") or ""),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
            "duration_sec": round(time.perf_counter() - started, 3),
        }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "status": "failed",
            "model_id": model,
            "request_model": request_model,
            "http_status": int(exc.code),
            "response_nonempty": False,
            "error": body[:500],
            "duration_sec": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "model_id": model,
            "request_model": request_model,
            "http_status": None,
            "response_nonempty": False,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_sec": round(time.perf_counter() - started, 3),
        }


async def _run_level(
    config: LiveLLMConfig,
    model: str,
    concurrency: int,
    calls: int,
    budget: LiveCallBudget,
    run_id: str,
    sequence_offset: int = 0,
    vision: bool = False,
    stream: bool = False,
    structured: bool = False,
    tool_call: bool = False,
    max_tokens: int = 256,
    context_profile: str = "short",
    conversation_rounds: int = 1,
) -> dict[str, Any]:
    prompts = [
        _build_probe_prompt(
            index,
            structured=structured,
            tool_call=tool_call,
            context_profile=context_profile,
            rounds=conversation_rounds,
        )
        for index in range(calls)
    ]

    level_gate = asyncio.Semaphore(max(1, int(concurrency)))
    active_count = 0
    active_lock = asyncio.Lock()
    observed_max_concurrency = 0

    async def run_one(index: int) -> dict[str, Any]:
        label = f"llm.concurrency_{concurrency}.call_{index}"
        request_id = f"req_{uuid4().hex}"
        started_at = _utc_now()
        started = time.perf_counter()
        queue_entered = time.perf_counter()
        concurrency_actual = 0
        try:
            async with level_gate:
                queue_wait_ms = round((time.perf_counter() - queue_entered) * 1000, 3)
                nonlocal active_count
                async with active_lock:
                    active_count += 1
                    concurrency_actual = active_count
                    nonlocal observed_max_concurrency
                    observed_max_concurrency = max(observed_max_concurrency, active_count)
                try:
                    result = await budget.run(
                        label,
                        lambda: asyncio.to_thread(
                            _post_chat,
                            config,
                            model,
                            prompts[index],
                            budget.timeout_sec,
                            vision=vision,
                            stream=stream,
                            structured=structured,
                            tool_call=tool_call,
                            max_tokens=max_tokens,
                            conversation_rounds=conversation_rounds,
                        ),
                    )
                finally:
                    async with active_lock:
                        active_count = max(0, active_count - 1)
            total_elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            row = {
                "index": index,
                "request_id": request_id,
                "run_id": run_id,
                "sequence": index + 1,
                "global_sequence": sequence_offset + index + 1,
                "level_sequence": index + 1,
                "started_at": started_at,
                "completed_at": _utc_now(),
                "concurrency_target": concurrency,
                "concurrency_actual": concurrency_actual,
                "queue_wait_ms": queue_wait_ms,
                "gateway_queue_wait_ms": None,
                "semaphore_wait_ms": None,
                "lock_wait_ms": None,
                "provider_latency_ms": round(float(result.get("duration_sec", 0.0) or 0.0) * 1000, 3),
                "total_elapsed_ms": total_elapsed_ms,
                "probe_timeout_sec": budget.timeout_sec,
                "configured_gateway_timeout_sec": config.public_summary().get("effective_gateway_timeout"),
                "configured_model_request_timeout_sec": config.public_summary().get("effective_model_request_timeout"),
                "context_profile": context_profile,
                "context_chars_requested": _context_chars(context_profile),
                "conversation_rounds": max(1, int(conversation_rounds)),
                "prompt_chars": len(prompts[index]),
                "measured_gateway_timeout_sec": None,
                "measured_stage_timeout_sec": None,
                "attempt": None,
                "retry_count": None,
                "retry_backoff_ms": None,
                "fallback": None,
                "cancelled": None,
                "measurement_scope": "provider_probe",
                **result,
            }
            row["error_class"] = _error_class(row)
            return row
        except LiveBudgetExceeded as exc:
            row = {
                "index": index,
                "status": "budget_rejected",
                "request_id": request_id,
                "run_id": run_id,
                "sequence": index + 1,
                "global_sequence": sequence_offset + index + 1,
                "level_sequence": index + 1,
                "model_id": model,
                "request_model": config.request_model(model),
                "started_at": started_at,
                "completed_at": _utc_now(),
                "concurrency_target": concurrency,
                "concurrency_actual": concurrency_actual,
                "queue_wait_ms": round((time.perf_counter() - queue_entered) * 1000, 3),
                "provider_latency_ms": 0,
                "total_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "probe_timeout_sec": budget.timeout_sec,
                "configured_gateway_timeout_sec": config.public_summary().get("effective_gateway_timeout"),
                "configured_model_request_timeout_sec": config.public_summary().get("effective_model_request_timeout"),
                "context_profile": context_profile,
                "context_chars_requested": _context_chars(context_profile),
                "conversation_rounds": max(1, int(conversation_rounds)),
                "prompt_chars": len(prompts[index]),
                "measured_gateway_timeout_sec": None,
                "measured_stage_timeout_sec": None,
                "attempt": None,
                "retry_count": None,
                "retry_backoff_ms": None,
                "fallback": None,
                "cancelled": None,
                "measurement_scope": "provider_probe",
                "error": str(exc),
            }
            row["error_class"] = "budget_exhausted"
            return row
        except Exception as exc:
            row = {
                "index": index,
                "status": "failed",
                "request_id": request_id,
                "run_id": run_id,
                "sequence": index + 1,
                "global_sequence": sequence_offset + index + 1,
                "level_sequence": index + 1,
                "model_id": model,
                "request_model": config.request_model(model),
                "started_at": started_at,
                "completed_at": _utc_now(),
                "concurrency_target": concurrency,
                "concurrency_actual": concurrency_actual,
                "queue_wait_ms": round((time.perf_counter() - queue_entered) * 1000, 3),
                "provider_latency_ms": 0,
                "total_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "probe_timeout_sec": budget.timeout_sec,
                "configured_gateway_timeout_sec": config.public_summary().get("effective_gateway_timeout"),
                "configured_model_request_timeout_sec": config.public_summary().get("effective_model_request_timeout"),
                "context_profile": context_profile,
                "context_chars_requested": _context_chars(context_profile),
                "conversation_rounds": max(1, int(conversation_rounds)),
                "prompt_chars": len(prompts[index]),
                "measured_gateway_timeout_sec": None,
                "measured_stage_timeout_sec": None,
                "attempt": None,
                "retry_count": None,
                "retry_backoff_ms": None,
                "fallback": None,
                "cancelled": None,
                "measurement_scope": "provider_probe",
                "error": f"{type(exc).__name__}: {exc}",
            }
            row["error_class"] = _error_class(row)
            return row

    started = time.perf_counter()
    rows = await asyncio.gather(*(run_one(index) for index in range(calls)))
    return {
        "concurrency": concurrency,
        "requested_calls": calls,
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "passed": sum(row.get("status") == "passed" for row in rows),
        "failed": sum(row.get("status") == "failed" for row in rows),
        "budget_rejected": sum(row.get("status") == "budget_rejected" for row in rows),
        "observed_max_concurrency": observed_max_concurrency,
        "observed_avg_concurrency": round(
            sum(float(row.get("concurrency_actual", 0) or 0) for row in rows) / len(rows), 3
        ) if rows else 0.0,
        "rows": rows,
    }


async def run_probe(
    *,
    config: LiveLLMConfig,
    model: str,
    levels: list[int],
    calls_per_level: int,
    max_calls: int,
    timeout_sec: float,
    vision: bool = False,
    stream: bool = False,
    structured: bool = False,
    tool_call: bool = False,
    max_tokens: int = 256,
    context_profile: str = "short",
    conversation_rounds: int = 1,
) -> dict[str, Any]:
    _context_chars(context_profile)
    conversation_rounds = max(1, int(conversation_rounds))
    run_id = f"run_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}"
    started_at = _utc_now()
    if config.configuration_status != "ok":
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "scenario": "provider_probe_vision" if vision else "provider_probe",
            "started_at": started_at,
            "finished_at": _utc_now(),
            "git_commit": _git_commit(),
            "environment": os.getenv("ASTRMAI_LIVE_ENVIRONMENT", "unknown"),
            "region": os.getenv("ASTRMAI_LIVE_REGION", "unknown"),
            "status": "configuration_mismatch",
            "mode": "llm_vision" if vision else "llm_only",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "config": config.public_summary(),
            "model": model,
            "context_profile": context_profile,
            "context_chars_requested": _context_chars(context_profile),
            "conversation_rounds": conversation_rounds,
            "levels": [],
            "budget": {
                "max_calls": max_calls,
                "max_concurrency": max(levels),
                "timeout_sec": timeout_sec,
                "calls_started": 0,
                "calls_completed": 0,
                "calls_failed": 0,
            },
        }
    budget = LiveCallBudget(
        max_calls=max_calls,
        max_concurrency=max(levels),
        timeout_sec=timeout_sec,
    )
    levels_payload: list[dict[str, Any]] = []
    sequence_offset = 0
    for concurrency in levels:
        levels_payload.append(
            await _run_level(
                config,
                model,
                concurrency,
                calls_per_level,
                budget,
                run_id,
                sequence_offset=sequence_offset,
                vision=vision,
                stream=stream,
                structured=structured,
                tool_call=tool_call,
                max_tokens=max_tokens,
                context_profile=context_profile,
                conversation_rounds=conversation_rounds,
            )
        )
        sequence_offset += calls_per_level
    finished_at = _utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "scenario": "provider_probe_vision" if vision else "provider_probe",
        "started_at": started_at,
        "finished_at": finished_at,
        "git_commit": _git_commit(),
        "environment": os.getenv("ASTRMAI_LIVE_ENVIRONMENT", "unknown"),
        "region": os.getenv("ASTRMAI_LIVE_REGION", "unknown"),
        "status": "passed"
        if levels_payload and all(item["passed"] == item["requested_calls"] for item in levels_payload)
        else "failed",
        "mode": "llm_vision_stream" if vision and stream else "llm_stream" if stream else "llm_vision" if vision else "llm_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": config.public_summary(),
        "model": model,
        "context_profile": context_profile,
        "context_chars_requested": _context_chars(context_profile),
        "conversation_rounds": conversation_rounds,
        "levels": levels_payload,
        "budget": budget.summary(),
    }


def _parse_levels(value: str) -> list[int]:
    levels: list[int] = []
    for item in str(value or "").split(","):
        try:
            parsed = max(1, int(item.strip()))
        except ValueError:
            continue
        if parsed not in levels:
            levels.append(parsed)
    return levels or [1]


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * ratio))
    return round(ordered[index], 3)


def _write_artifacts(payload: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(payload.get("run_id", "run_unknown"))
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [row for level in payload.get("levels", []) for row in level.get("rows", [])]
    started_rows = [row for row in rows if row.get("status") != "budget_rejected"]
    durations = [float(row.get("total_elapsed_ms", 0.0) or 0.0) for row in started_rows]
    successes = sum(row.get("status") == "passed" for row in rows)
    failures = sum(row.get("status") == "failed" for row in rows)
    usage_rows = [row.get("usage", {}) for row in rows if isinstance(row.get("usage"), dict)]

    def usage_total(name: str) -> int | None:
        values = [item.get(name) for item in usage_rows if isinstance(item.get(name), (int, float))]
        return int(sum(values)) if values else None

    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "scenario": payload.get("scenario"),
        "status": payload.get("status"),
        "measurement_scope": "provider_probe",
        "configuration_blocked": payload.get("status") == "configuration_mismatch",
        "configuration_mismatch_count": 1 if payload.get("status") == "configuration_mismatch" else 0,
        "configuration_errors": (payload.get("config", {}) or {}).get("configuration_errors", []),
        "total_requests": len(rows),
        "requests_started": len(started_rows),
        "started_provider_requests": len(started_rows),
        "rejected_before_start": sum(row.get("status") == "budget_rejected" for row in rows),
        "success_count": successes,
        "failure_count": failures,
        "context_profile": payload.get("context_profile", "short"),
        "context_chars_requested": payload.get("context_chars_requested", 0),
        "conversation_rounds": payload.get("conversation_rounds", 1),
        "max_prompt_chars": max((int(row.get("prompt_chars", 0) or 0) for row in rows), default=0),
        "prompt_tokens_total": usage_total("prompt_tokens"),
        "completion_tokens_total": usage_total("completion_tokens"),
        "total_tokens_total": usage_total("total_tokens"),
        "budget_rejected_count": sum(row.get("status") == "budget_rejected" for row in rows),
        "cancelled_count": sum(row.get("error_class") == "cancelled" for row in rows),
        "provider_error_count": sum(row.get("error_class") == "provider_error" for row in rows),
        "auth_error_count": sum(row.get("error_class") == "auth_error" for row in rows),
        "region_error_count": sum(row.get("error_class") == "region_error" for row in rows),
        "invalid_response_count": sum(row.get("error_class") == "invalid_response" for row in rows),
        "success_rate": round(successes / len(started_rows), 6) if started_rows else 0.0,
        "timeout_count": sum(row.get("error_class") in {"read_timeout", "connect_timeout"} for row in rows),
        "http_429_count": sum(row.get("http_status") == 429 for row in rows),
        "empty_response_count": sum(row.get("error_class") == "empty_response" for row in rows),
        "truncated_response_count": sum(row.get("error_class") == "truncated_response" for row in rows),
        "network_error_count": sum(row.get("error_class") == "network_error" for row in rows),
        "connect_timeout_count": sum(row.get("error_class") == "connect_timeout" for row in rows),
        "read_timeout_count": sum(row.get("error_class") == "read_timeout" for row in rows),
        "provider_not_found_count": sum(row.get("error_class") == "provider_not_found" for row in rows),
        "rate_limited_count": sum(row.get("error_class") == "rate_limited" for row in rows),
        "retry_rate": None,
        "fallback_rate": None,
        "retry_measurement": "not_measured",
        "fallback_measurement": "not_measured",
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "p99_ms": _percentile(durations, 0.99),
        "max_ms": round(max(durations), 3) if durations else 0.0,
        "gateway_queue_wait_p95_ms": None,
        "lock_wait_p95_ms": None,
        "turn_timeout_count": None,
        "background_queue_timeout_count": None,
        "observed_max_concurrency": max((level.get("observed_max_concurrency", 0) for level in payload.get("levels", [])), default=0),
        "observed_avg_concurrency": round(
            sum(float(level.get("observed_avg_concurrency", 0.0) or 0.0) for level in payload.get("levels", []))
            / len(payload.get("levels", [])), 3
        ) if payload.get("levels") else 0.0,
        "config": payload.get("config", {}),
    }
    run_metadata = dict(payload)
    run_metadata.pop("levels", None)
    run_metadata["summary_file"] = "summary.json"
    run_metadata["calls_file"] = "calls.jsonl"
    (run_dir / "run.json").write_text(
        json.dumps(run_metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (run_dir / "calls.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return run_dir, run_dir / "summary.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded live LLM probes for AstrMai.")
    parser.add_argument("--model", default="", help="Model id; defaults to ASTRMAI_LIVE_MODEL or task_models[0].")
    parser.add_argument("--all-models", action="store_true", help="Probe every configured task/agent/fallback model sequentially.")
    parser.add_argument("--levels", default="1,2,3,4,8", help="Comma-separated concurrency levels, e.g. 1,2,3,4,8.")
    parser.add_argument("--calls-per-level", type=int, default=2)
    parser.add_argument("--max-calls", type=int, default=10)
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--vision", action="store_true", help="Send a tiny valid PNG with each chat request.")
    parser.add_argument("--stream", action="store_true", help="Use OpenAI-compatible SSE streaming responses.")
    parser.add_argument("--json", action="store_true", help="Request a JSON object response.")
    parser.add_argument("--tool-call", action="store_true", help="Advertise a small probe function tool.")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument(
        "--context-profile",
        choices=sorted(CONTEXT_PROFILES),
        default="short",
        help="Synthetic context size: short, medium, long, or xlong.",
    )
    parser.add_argument("--rounds", type=int, default=1, help="Conversation rounds to include in each request.")
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "artifacts" / "live_validation"),
    )
    args = parser.parse_args()
    config = load_live_llm_config()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    models = config.all_models if args.all_models else [str(args.model or config.default_model).strip()]
    reports: list[dict[str, Any]] = []
    for model in models:
        model_config = load_live_llm_config(model_id=model) if args.all_models else config
        payload = asyncio.run(
            run_probe(
                config=model_config,
                model=model,
                levels=_parse_levels(args.levels),
                calls_per_level=max(1, int(args.calls_per_level)),
                max_calls=max(1, int(args.max_calls)),
                timeout_sec=max(0.1, float(args.timeout_sec)),
                vision=bool(args.vision),
                stream=bool(args.stream),
                structured=bool(args.json),
                tool_call=bool(args.tool_call),
                max_tokens=max(16, int(args.max_tokens)),
                context_profile=args.context_profile,
                conversation_rounds=max(1, int(args.rounds)),
            )
        )
        output_path = output_dir / f"llm_probe_{datetime.now().strftime('%Y%m%d-%H%M%S')}_{model.replace('/', '_')}.json"
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        run_dir, summary_path = _write_artifacts(payload, output_dir)
        reports.append({
            "model": model,
            "status": payload["status"],
            "artifact": str(output_path),
            "run_dir": str(run_dir),
            "summary": str(summary_path),
            "budget": payload["budget"],
        })
    print(json.dumps(reports[0] if len(reports) == 1 else reports, ensure_ascii=False))
    return 0 if all(item["status"] == "passed" for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
