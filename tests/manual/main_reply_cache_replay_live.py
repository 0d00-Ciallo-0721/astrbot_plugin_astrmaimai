from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _preview(text: Any, limit: int = 160) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def _provider_output_dir(root_dir: Path, provider_family: str) -> Path:
    normalized = str(provider_family or "").strip().lower() or "unknown"
    return root_dir / normalized


def _provider_explainer(provider_family: str, model: str) -> dict[str, str]:
    family = str(provider_family or "").strip().lower()
    upstream_model_label = str(model or "").strip()
    if family == "anthropic":
        return {
            "endpoint_kind": "anthropic_messages",
            "upstream_model_label": upstream_model_label,
            "provider_family_explainer": "Provider family `anthropic` means the request path targets Anthropic's native Messages API.",
        }
    if family == "gemini":
        return {
            "endpoint_kind": "gemini_generate_content",
            "upstream_model_label": upstream_model_label,
            "provider_family_explainer": "Provider family `gemini` means the request path targets Gemini's native generateContent API.",
        }
    if family == "native_chat":
        return {
            "endpoint_kind": "openai_compatible",
            "upstream_model_label": upstream_model_label,
            "provider_family_explainer": "Provider family `native_chat` only describes an OpenAI-compatible chat completions transport. It does not identify the upstream vendor by itself.",
        }
    return {
        "endpoint_kind": "unknown",
        "upstream_model_label": upstream_model_label,
        "provider_family_explainer": "Provider family could not be mapped to a known transport category.",
    }


def _dry_run_summary(
    *,
    provider_family: str,
    model: str,
    provider_supports_cache_hint: bool,
    provider_supports_usage_reporting: bool,
    provider_supports_session_id: bool,
    api_key_present: bool,
    base_url_required: bool,
    base_url_present: bool,
) -> dict[str, Any]:
    provider_meta = _provider_explainer(provider_family, model)
    request_execution_possible = bool(api_key_present and (not base_url_required or base_url_present))
    blocking_reason = ""
    if not api_key_present:
        blocking_reason = "missing_api_key"
    elif base_url_required and not base_url_present:
        blocking_reason = "missing_base_url"
    return {
        "run_id": str(provider_family or "unknown").strip().lower() or "unknown",
        "provider_family": provider_family,
        "model": model,
        "dry_run": True,
        "validation_verdict": "dry_run_capability_only",
        "provider_supports_cache_hint": bool(provider_supports_cache_hint),
        "provider_supports_usage_reporting": bool(provider_supports_usage_reporting),
        "provider_supports_session_id": bool(provider_supports_session_id),
        "api_key_present": bool(api_key_present),
        "base_url_required": bool(base_url_required),
        "base_url_present": bool(base_url_present),
        "request_execution_possible": bool(request_execution_possible),
        "blocking_reason": blocking_reason,
        "session_reuse_validation_deferred": True,
        "session_reuse_deferred_reason": "all current live clients report supports_session_id=False",
        "sample_count": 0,
        "cache_ready_count": 0,
        "cache_ready_rate": 0.0,
        "cache_hit_count": 0,
        "cache_hit_rate": 0.0,
        "unsupported_usage_reporting_count": 0,
        "cache_hint_enabled_rate": 0.0,
        "cache_hint_observed_enabled": False,
        "cache_ready_but_hit_miss_count": 0,
        "cache_ready_but_hit_miss_case_ids": [],
        "cache_ready_reason_frequency": {},
        "hash_stable_count": 0,
        "hash_stable_but_cache_miss_count": 0,
        "hash_stable_but_cache_miss_case_ids": [],
        "semantic_hash_stable_count": 0,
        "semantic_stable_but_provider_visible_changed_count": 0,
        "semantic_stable_but_provider_visible_changed_case_ids": [],
        "hook_changed_system_case_ids": [],
        "rows": [],
        **provider_meta,
    }


def _write_dry_run_artifacts(output_dir: Path, summary: dict[str, Any]) -> None:
    _ensure_dir(output_dir)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Main Reply Cache Live Replay",
        "",
        f"- provider_family: `{summary.get('provider_family', '')}`",
        f"- model: `{summary.get('model', '')}`",
        f"- dry_run: `{summary.get('dry_run', False)}`",
        f"- validation_verdict: `{summary.get('validation_verdict', '')}`",
        f"- endpoint_kind: `{summary.get('endpoint_kind', '')}`",
        f"- upstream_model_label: `{summary.get('upstream_model_label', '')}`",
        f"- provider_supports_cache_hint: `{summary.get('provider_supports_cache_hint', False)}`",
        f"- provider_supports_usage_reporting: `{summary.get('provider_supports_usage_reporting', False)}`",
        f"- provider_supports_session_id: `{summary.get('provider_supports_session_id', False)}`",
        f"- api_key_present: `{summary.get('api_key_present', False)}`",
        f"- base_url_required: `{summary.get('base_url_required', False)}`",
        f"- base_url_present: `{summary.get('base_url_present', False)}`",
        f"- request_execution_possible: `{summary.get('request_execution_possible', False)}`",
        f"- blocking_reason: `{summary.get('blocking_reason', '')}`",
        f"- session_reuse_validation_deferred: `{summary.get('session_reuse_validation_deferred', False)}`",
        f"- provider_family_explainer: {summary.get('provider_family_explainer', '')}",
        "",
        "This run did not send a real provider request, so there is no `cache_hit` evidence in this artifact.",
    ]
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    sample_row = {
        "case_id": "dry_run_capability_only",
        "provider_family": summary.get("provider_family", ""),
        "model": summary.get("model", ""),
        "endpoint_kind": summary.get("endpoint_kind", ""),
        "provider_supports_cache_hint": summary.get("provider_supports_cache_hint", False),
        "provider_supports_usage_reporting": summary.get("provider_supports_usage_reporting", False),
        "provider_supports_session_id": summary.get("provider_supports_session_id", False),
        "blocking_reason": summary.get("blocking_reason", ""),
        "request_execution_possible": summary.get("request_execution_possible", False),
    }
    (output_dir / "samples.jsonl").write_text(json.dumps(sample_row, ensure_ascii=False) + "\n", encoding="utf-8")


def _resolve_env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _case_rows() -> list[dict[str, Any]]:
    return [
        {
            "case_id": "same_chat_turn_1",
            "text": "Do you still remember what I said about exams?",
            "chat_id": "default:GroupMessage:cache-live-group-1",
            "is_private": False,
        },
        {
            "case_id": "same_chat_turn_2",
            "text": "Then answer only the key point.",
            "chat_id": "default:GroupMessage:cache-live-group-1",
            "is_private": False,
        },
        {
            "case_id": "same_chat_turn_3",
            "text": "Then keep the wording even shorter.",
            "chat_id": "default:GroupMessage:cache-live-group-1",
            "is_private": False,
        },
        {
            "case_id": "private_turn",
            "text": "今天有点累，陪我聊两句。",
            "chat_id": "default:FriendMessage:cache-live-user-1",
            "is_private": True,
        },
        {
            "case_id": "tool_call_turn",
            "text": "Please check first and then answer briefly.",
            "chat_id": "default:GroupMessage:cache-live-group-1",
            "is_private": False,
        },
        {
            "case_id": "near_context_turn",
            "text": "Why this one exactly?",
            "chat_id": "default:GroupMessage:cache-live-group-1",
            "is_private": False,
        },
    ]


class LiveReplayGateway:
    def __init__(self, provider_client):
        self.provider_client = provider_client
        self.config = self._build_config(getattr(provider_client, "timeout", 20.0))
        self.lane_manager = SimpleNamespace(get_recent_transcript=lambda *args, **kwargs: "")

    @staticmethod
    def _build_config(timeout: float) -> Any:
        return SimpleNamespace(
            system1=SimpleNamespace(nicknames=["AstrMai", "ATRI"]),
            global_settings=SimpleNamespace(debug_mode=False, enable_error_interception=False, admin_ids=[]),
            provider=SimpleNamespace(),
            reply=SimpleNamespace(follow_up_probability=0.0, emotion_mapping={}, fallback_text="(silent)"),
            memory=SimpleNamespace(enable_react_agent=True, auto_recall_probability=0.0),
            agent=SimpleNamespace(max_steps=5, timeout=max(10, int(timeout))),
            persona=SimpleNamespace(persona_id="live-replay"),
            infra=SimpleNamespace(api_timeout=float(timeout)),
        )

    def get_agent_models(self) -> list[str]:
        return [self.provider_client.model]

    def get_models_for_task(self, pool_name: str, models: list[str]) -> list[str]:
        return list(models or [self.provider_client.model])

    @staticmethod
    def _write_request_trace(event, *, system_prompt: str, prompt: str, result, model_id: str):
        if event is None or not hasattr(event, "set_extra"):
            return
        import hashlib

        request_trace = {
            "gateway_system_hash": hashlib.sha256(str(system_prompt or "").encode("utf-8")).hexdigest()[:16] if system_prompt else "",
            "gateway_prompt_hash": hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()[:16] if prompt else "",
            "provider_visible_system_hash": hashlib.sha256(str(system_prompt or "").encode("utf-8")).hexdigest()[:16] if system_prompt else "",
            "provider_visible_prompt_hash": hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()[:16] if prompt else "",
            "post_hook_system_hash": hashlib.sha256(str(system_prompt or "").encode("utf-8")).hexdigest()[:16] if system_prompt else "",
            "request_session_id": str(getattr(result, "request_session_id", "") or ""),
            "request_cache_control": json.dumps(getattr(result, "request_hint_payload", {}) or {}, ensure_ascii=False, sort_keys=True) if getattr(result, "request_hint_payload", None) else "",
            "request_provider_family": str(getattr(result, "raw_provider_family", "") or ""),
            "request_model_id": str(getattr(result, "raw_model_id", "") or model_id or ""),
            "usage_input_tokens": int(getattr(result, "usage_input_tokens", 0) or 0),
            "usage_input_cached": int(getattr(result, "usage_input_cached", 0) or 0),
            "usage_output_tokens": int(getattr(result, "usage_output_tokens", 0) or 0),
        }
        event.set_extra("astrmai_request_trace", request_trace)

    async def call_data_process_task(self, prompt: str, *, system_prompt: str = "", is_json: bool = False, **kwargs):
        user_prompt = str(prompt or "")
        if is_json:
            user_prompt += "\n\nReturn only one valid JSON object. No markdown."
        result = await self.provider_client.complete(
            system_prompt=system_prompt or "You are a concise hidden planning assistant.",
            prompt=user_prompt,
            request_label="data_process",
        )
        text = str(getattr(result, "text", "") or "").strip()
        if is_json:
            try:
                return json.loads(text)
            except Exception:
                return {}
        return text

    async def chat_in_lane_result(self, **kwargs):
        result = await self.provider_client.complete(
            system_prompt=str(kwargs.get("system_prompt", "") or ""),
            prompt=str(kwargs.get("prompt", "") or ""),
            request_label="chat",
        )
        self._write_request_trace(
            kwargs.get("event"),
            system_prompt=str(kwargs.get("system_prompt", "") or ""),
            prompt=str(kwargs.get("prompt", "") or ""),
            result=result,
            model_id=self.provider_client.model,
        )
        if kwargs.get("event") is not None and hasattr(kwargs.get("event"), "set_extra"):
            kwargs.get("event").set_extra("astrmai_cached_usage_supported", bool(getattr(result, "cached_usage_supported", False)))
        return SimpleNamespace(
            text=str(result.text or "").strip(),
            usage={
                "input_tokens": int(result.usage_input_tokens or 0),
                "input_cached": int(result.usage_input_cached or 0),
                "output_tokens": int(result.usage_output_tokens or 0),
                "total_tokens": int(result.usage_input_tokens or 0) + int(result.usage_output_tokens or 0),
            },
            provider_family=str(result.raw_provider_family or ""),
            model_id=str(result.raw_model_id or self.provider_client.model),
            economy={},
        )

    async def tool_chat_in_lane_result(self, **kwargs):
        tools = getattr(kwargs.get("tools"), "tools", None) or []
        tool_names = [str(getattr(tool, "name", "") or tool.__class__.__name__) for tool in tools]
        tool_notice = (
            "\n\nUse available tools only if necessary. "
            f"Available tool names: {', '.join(tool_names) or 'none'}."
        )
        result = await self.provider_client.complete(
            system_prompt=str(kwargs.get("system_prompt", "") or ""),
            prompt=str(kwargs.get("prompt", "") or ""),
            request_label="tool_chat",
            tools_notice=tool_notice,
        )
        self._write_request_trace(
            kwargs.get("event"),
            system_prompt=str(kwargs.get("system_prompt", "") or ""),
            prompt=str(kwargs.get("prompt", "") or "") + tool_notice,
            result=result,
            model_id=self.provider_client.model,
        )
        if kwargs.get("event") is not None and hasattr(kwargs.get("event"), "set_extra"):
            kwargs.get("event").set_extra("astrmai_cached_usage_supported", bool(getattr(result, "cached_usage_supported", False)))
        return SimpleNamespace(
            text=str(result.text or "").strip(),
            usage={
                "input_tokens": int(result.usage_input_tokens or 0),
                "input_cached": int(result.usage_input_cached or 0),
                "output_tokens": int(result.usage_output_tokens or 0),
                "total_tokens": int(result.usage_input_tokens or 0) + int(result.usage_output_tokens or 0),
            },
            provider_family=str(result.raw_provider_family or ""),
            model_id=str(result.raw_model_id or self.provider_client.model),
            economy={},
        )

    async def call_vision_task(self, **kwargs):
        return {"type": "unknown", "description": "vision disabled in live cache replay", "emotion_tags": []}


async def _build_runtime(provider_family: str, api_key: str, model: str):
    replay_mod = importlib.import_module("tests.manual.kimi_replay_acceptance")
    live_mod = importlib.import_module("tests.manual.main_reply_live_providers")

    base_url = _resolve_env("MAIN_REPLY_LIVE_BASE_URL")
    prompt_cache_key = _resolve_env("MAIN_REPLY_LIVE_PROMPT_CACHE_KEY")
    prompt_cache_retention = _resolve_env("MAIN_REPLY_LIVE_PROMPT_CACHE_RETENTION")
    provider_client = await live_mod.build_live_provider_client(
        provider_family,
        api_key=api_key,
        model=model,
        timeout=20.0,
        base_url=base_url,
        prompt_cache_key=prompt_cache_key,
        prompt_cache_retention=prompt_cache_retention,
    )
    if str(provider_family or "").strip().lower() == "native_chat" and not str(base_url or "").strip():
        raise RuntimeError("MAIN_REPLY_LIVE_BASE_URL is required when MAIN_REPLY_LIVE_PROVIDER_FAMILY=native_chat")
    await provider_client.__aenter__()

    planner_mod = importlib.import_module("astrmai.conversation.planning.planner")
    prompt_refiner_mod = importlib.import_module("astrmai.conversation.planning.prompt_refiner")

    gateway = LiveReplayGateway(provider_client)
    memory_engine = replay_mod._FakeMemoryEngine()
    planner = planner_mod.Planner(
        context=SimpleNamespace(),
        gateway=gateway,
        context_engine=replay_mod._FakeContextEngine(),
        reply_engine=replay_mod._FakeReplyEngine(),
        memory_engine=memory_engine,
        evolution_manager=replay_mod._FakeEvolutionManager(),
        state_engine=replay_mod._FakeStateEngine(),
        prompt_refiner=prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            db_service=None,
            config=gateway.config,
            react_retriever=replay_mod._FakeReactRetriever(),
        ),
        sys3_router=None,
        runtime_coordinator=None,
    )
    planner.cognitive_loop.SOFT_TIMEOUT_SECONDS = max(2.5, min(float(getattr(provider_client, "timeout", 20.0)), 12.0))
    return provider_client, planner, replay_mod


def _event_from_case(replay_mod, case_id: str, text: str, chat_id: str, is_private: bool):
    case = replay_mod.ReplayCase(
        case_id=case_id,
        text=text,
        expected="live cache replay",
        chat_id=chat_id,
        group_id="" if is_private else "cache-live-group-1",
        focus_reason="private" if is_private else "at_bot",
        is_private=is_private,
    )
    event = replay_mod.build_event(case)
    if case_id == "tool_call_turn":
        event.set_extra("judge_action", "TOOL_CALL")
        event.set_extra("astrmai_action_tier", "sys3")
        event.set_extra("retrieve_keys", ["ALL"])
    if case_id == "near_context_turn":
        event.set_extra("astrmai_near_context_priority", True)
    return event


async def _run_case(planner, replay_mod, row: dict[str, Any]) -> dict[str, Any]:
    event = _event_from_case(
        replay_mod,
        row["case_id"],
        row["text"],
        row["chat_id"],
        bool(row["is_private"]),
    )
    started = time.perf_counter()
    reply_text = await planner.plan_and_execute(event, [event])
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    turn_trace = planner.turn_trace_history[-1] if planner.turn_trace_history else {}
    continuity = dict((turn_trace.get("continuity") or {}))
    request_trace = dict(event.get_extra("astrmai_request_trace", {}) or {})
    cached_usage_supported = bool(event.get_extra("astrmai_cached_usage_supported", False))
    observed_cache_hint = bool(str(continuity.get("request_cache_control", "") or "").strip())
    return {
        "case_id": row["case_id"],
        "chat_id": row["chat_id"],
        "elapsed_ms": elapsed_ms,
        "reply_preview": _preview(reply_text, 180),
        "request_trace": request_trace,
        "turn_trace": turn_trace,
        "continuity": continuity,
        "cache_ready": bool(continuity.get("cache_ready", False)),
        "cache_ready_reasons": list(continuity.get("cache_ready_reasons", []) or []),
        "cache_hit": bool(continuity.get("cache_hit", False)),
        "cache_hit_evidence_supported": bool(
            continuity.get("cache_hit_evidence_supported", False) or cached_usage_supported
        ),
        "cache_hit_evidence_unavailable": not cached_usage_supported,
        "cache_hint_observed_enabled": observed_cache_hint,
        "semantic_hash_stable_vs_previous": False,
        "hook_changed_system": bool(
            str(continuity.get("gateway_system_hash", "") or "")
            and str(continuity.get("provider_visible_system_hash", "") or "")
            and str(continuity.get("gateway_system_hash", "") or "") != str(continuity.get("provider_visible_system_hash", "") or "")
        ),
    }


def _write_summary(
    output_dir: Path,
    rows: list[dict[str, Any]],
    provider_family: str,
    model: str,
    *,
    provider_supports_cache_hint: bool,
    provider_supports_usage_reporting: bool,
    provider_supports_session_id: bool,
) -> None:
    provider_meta = _provider_explainer(provider_family, model)
    cache_ready_count = sum(1 for row in rows if row.get("cache_ready"))
    hash_stable_count = 0
    hash_stable_but_cache_miss_count = 0
    hash_stable_but_cache_miss_case_ids: list[str] = []
    cache_ready_but_hit_miss_count = 0
    cache_ready_but_hit_miss_case_ids: list[str] = []
    cache_ready_reason_frequency: dict[str, int] = {}
    semantic_hash_stable_count = 0
    semantic_stable_but_provider_visible_changed_count = 0
    semantic_stable_but_provider_visible_changed_case_ids: list[str] = []
    hook_changed_system_case_ids: list[str] = []
    previous_hash_by_chat: dict[str, str] = {}
    previous_semantic_hash_by_chat: dict[str, str] = {}
    for row in rows:
        continuity = row.get("continuity") or {}
        chat_id = str(row.get("chat_id", "") or "")
        current_hash = str(continuity.get("provider_visible_system_hash", "") or "")
        previous_hash = previous_hash_by_chat.get(chat_id, "")
        hash_stable_vs_previous = bool(previous_hash and current_hash and previous_hash == current_hash)
        row["hash_stable_vs_previous"] = hash_stable_vs_previous
        semantic_hash = str(continuity.get("semantic_system_hash", "") or "")
        previous_semantic_hash = previous_semantic_hash_by_chat.get(chat_id, "")
        semantic_hash_stable = bool(previous_semantic_hash and semantic_hash and previous_semantic_hash == semantic_hash)
        row["semantic_hash_stable_vs_previous"] = semantic_hash_stable
        for reason in list(row.get("cache_ready_reasons", []) or []):
            normalized_reason = str(reason or "").strip()
            if normalized_reason:
                cache_ready_reason_frequency[normalized_reason] = cache_ready_reason_frequency.get(normalized_reason, 0) + 1
        if row.get("cache_ready") and not row.get("cache_hit"):
            cache_ready_but_hit_miss_count += 1
            cache_ready_but_hit_miss_case_ids.append(str(row.get("case_id", "") or ""))
        if hash_stable_vs_previous:
            hash_stable_count += 1
            if not row.get("cache_hit"):
                hash_stable_but_cache_miss_count += 1
                hash_stable_but_cache_miss_case_ids.append(str(row.get("case_id", "") or ""))
        if semantic_hash_stable:
            semantic_hash_stable_count += 1
            if current_hash and previous_hash and current_hash != previous_hash:
                semantic_stable_but_provider_visible_changed_count += 1
                semantic_stable_but_provider_visible_changed_case_ids.append(str(row.get("case_id", "") or ""))
        if current_hash:
            previous_hash_by_chat[chat_id] = current_hash
        if semantic_hash:
            previous_semantic_hash_by_chat[chat_id] = semantic_hash
        if row.get("hook_changed_system"):
            hook_changed_system_case_ids.append(str(row.get("case_id", "") or ""))

    if any(row.get("cache_hit") for row in rows):
        validation_verdict = "observed_cache_hit"
    elif any(row.get("cache_hit_evidence_unavailable") for row in rows):
        validation_verdict = "unsupported_usage_reporting"
    else:
        validation_verdict = "supported_but_no_observed_hit"
    summary = {
        "run_id": output_dir.name,
        "provider_family": provider_family,
        "model": model,
        "started_at": int(time.time()),
        "usage_reporting_supported": any(row.get("cache_hit_evidence_supported") for row in rows),
        "provider_supports_cache_hint": bool(provider_supports_cache_hint),
        "provider_supports_usage_reporting": bool(provider_supports_usage_reporting),
        "provider_supports_session_id": bool(provider_supports_session_id),
        "session_reuse_validation_deferred": True,
        "session_reuse_deferred_reason": "all current live clients report supports_session_id=False",
        "cache_hint_observed_enabled": any(row.get("cache_hint_observed_enabled") for row in rows),
        "hook_present": any(bool((row.get("continuity") or {}).get("post_hook_system_hash", "")) for row in rows),
        "sample_count": len(rows),
        "cache_ready_count": cache_ready_count,
        "cache_ready_rate": round(cache_ready_count / max(1, len(rows)), 4),
        "cache_hit_count": sum(1 for row in rows if row.get("cache_hit")),
        "cache_hit_rate": round(sum(1 for row in rows if row.get("cache_hit")) / max(1, len(rows)), 4),
        "unsupported_usage_reporting_count": sum(1 for row in rows if row.get("cache_hit_evidence_unavailable")),
        "cache_hint_enabled_rate": round(sum(1 for row in rows if row.get("cache_hint_observed_enabled")) / max(1, len(rows)), 4),
        "validation_verdict": validation_verdict,
        "cache_ready_but_hit_miss_count": cache_ready_but_hit_miss_count,
        "cache_ready_but_hit_miss_case_ids": cache_ready_but_hit_miss_case_ids,
        "cache_ready_reason_frequency": dict(sorted(cache_ready_reason_frequency.items())),
        "hash_stable_count": hash_stable_count,
        "hash_stable_but_cache_miss_count": hash_stable_but_cache_miss_count,
        "hash_stable_but_cache_miss_case_ids": hash_stable_but_cache_miss_case_ids,
        "semantic_hash_stable_count": semantic_hash_stable_count,
        "semantic_stable_but_provider_visible_changed_count": semantic_stable_but_provider_visible_changed_count,
        "semantic_stable_but_provider_visible_changed_case_ids": semantic_stable_but_provider_visible_changed_case_ids,
        "hook_changed_system_case_ids": hook_changed_system_case_ids,
        "rows": [
            {
                "case_id": row["case_id"],
                "chat_id": row["chat_id"],
                "cache_ready": row["cache_ready"],
                "cache_ready_reasons": list(row.get("cache_ready_reasons", []) or []),
                "cache_hit": row["cache_hit"],
                "cache_hit_evidence_supported": row["cache_hit_evidence_supported"],
                "cache_hint_observed_enabled": row["cache_hint_observed_enabled"],
                "hash_stable_vs_previous": row.get("hash_stable_vs_previous", False),
                "semantic_hash_stable_vs_previous": row.get("semantic_hash_stable_vs_previous", False),
                "hook_changed_system": row["hook_changed_system"],
                "semantic_system_hash": str((row.get("continuity") or {}).get("semantic_system_hash", "") or ""),
                "semantic_system_length": int((row.get("continuity") or {}).get("semantic_system_length", 0) or 0),
                "usage_input_cached": int((row.get("continuity") or {}).get("usage_input_cached", 0) or 0),
                "gateway_system_hash": str((row.get("continuity") or {}).get("gateway_system_hash", "") or ""),
                "gateway_prompt_hash": str((row.get("continuity") or {}).get("gateway_prompt_hash", "") or ""),
                "request_cache_control": str((row.get("continuity") or {}).get("request_cache_control", "") or ""),
                "request_session_id": str((row.get("continuity") or {}).get("request_session_id", "") or ""),
                "provider_visible_system_hash": str((row.get("continuity") or {}).get("provider_visible_system_hash", "") or ""),
                "post_hook_system_hash": str((row.get("continuity") or {}).get("post_hook_system_hash", "") or ""),
                "provider_visible_prompt_hash": str((row.get("continuity") or {}).get("provider_visible_prompt_hash", "") or ""),
                "prefix_hash": str((row.get("continuity") or {}).get("prefix_hash", "") or ""),
                "frozen_prefix_length": int((row.get("continuity") or {}).get("frozen_prefix_length", 0) or 0),
                "semi_stable_length": int((row.get("continuity") or {}).get("semi_stable_length", 0) or 0),
                "dynamic_prompt_length": int((row.get("continuity") or {}).get("dynamic_prompt_length", 0) or 0),
            }
            for row in rows
        ],
        **provider_meta,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Main Reply Cache Live Replay",
        "",
        f"- provider_family: `{provider_family}`",
        f"- model: `{model}`",
        f"- validation_verdict: `{summary['validation_verdict']}`",
        f"- endpoint_kind: `{summary['endpoint_kind']}`",
        f"- upstream_model_label: `{summary['upstream_model_label']}`",
        f"- provider_family_explainer: {summary['provider_family_explainer']}",
        f"- provider_supports_cache_hint: `{summary['provider_supports_cache_hint']}`",
        f"- provider_supports_usage_reporting: `{summary['provider_supports_usage_reporting']}`",
        f"- provider_supports_session_id: `{summary['provider_supports_session_id']}`",
        f"- session_reuse_validation_deferred: `{summary['session_reuse_validation_deferred']}`",
        f"- sample_count: `{summary['sample_count']}`",
        f"- cache_ready_count: `{summary['cache_ready_count']}`",
        f"- cache_ready_rate: `{summary['cache_ready_rate']}`",
        f"- cache_hit_count: `{summary['cache_hit_count']}`",
        f"- cache_hit_rate: `{summary['cache_hit_rate']}`",
        f"- unsupported_usage_reporting_count: `{summary['unsupported_usage_reporting_count']}`",
        f"- cache_hint_enabled_rate: `{summary['cache_hint_enabled_rate']}`",
        f"- cache_hint_observed_enabled: `{summary['cache_hint_observed_enabled']}`",
        f"- cache_ready_but_hit_miss_count: `{summary['cache_ready_but_hit_miss_count']}`",
        f"- cache_ready_reason_frequency: `{summary['cache_ready_reason_frequency']}`",
        f"- hash_stable_count: `{summary['hash_stable_count']}`",
        f"- hash_stable_but_cache_miss_count: `{summary['hash_stable_but_cache_miss_count']}`",
        f"- semantic_hash_stable_count: `{summary['semantic_hash_stable_count']}`",
        f"- semantic_stable_but_provider_visible_changed_count: `{summary['semantic_stable_but_provider_visible_changed_count']}`",
        f"- hook_changed_system_case_ids: `{summary['hook_changed_system_case_ids']}`",
        "",
        "| Case | Cache Ready | Ready Reasons | Cache Hit | input_cached | semantic_system_hash | cache_control | session_id | gateway_system_hash | provider_visible_system_hash | post_hook_system_hash | semantic_hash_stable_vs_previous | hash_stable_vs_previous | hook_changed_system |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["rows"]:
        lines.append(
            f"| {row['case_id']} | {row['cache_ready']} | {','.join(row.get('cache_ready_reasons', []) or [])} | {row['cache_hit']} | {row['usage_input_cached']} | {row['semantic_system_hash']} | {row['request_cache_control']} | {row['request_session_id']} | {row['gateway_system_hash']} | {row['provider_visible_system_hash']} | {row['post_hook_system_hash']} | {row['semantic_hash_stable_vs_previous']} | {row['hash_stable_vs_previous']} | {row['hook_changed_system']} |"
        )
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


async def _run_live(output_root_dir: Path) -> int:
    api_key = _resolve_env("MAIN_REPLY_LIVE_API_KEY")
    model = _resolve_env("MAIN_REPLY_LIVE_MODEL", "kimi-k2.6")
    provider_family = _resolve_env("MAIN_REPLY_LIVE_PROVIDER_FAMILY", "kimi")
    case_filter = {item.strip() for item in _resolve_env("MAIN_REPLY_LIVE_CASE_FILTER").split(",") if item.strip()}
    base_url = _resolve_env("MAIN_REPLY_LIVE_BASE_URL")
    provider_family_lower = str(provider_family or "").strip().lower()
    output_dir = _provider_output_dir(output_root_dir, provider_family)

    if not api_key:
        capability_map = {
            "anthropic": {"supports_cache_hint": True, "supports_usage_reporting": True, "supports_session_id": False, "base_url_required": False},
            "gemini": {"supports_cache_hint": False, "supports_usage_reporting": False, "supports_session_id": False, "base_url_required": False},
            "native_chat": {"supports_cache_hint": True, "supports_usage_reporting": False, "supports_session_id": False, "base_url_required": True},
        }
        capability = capability_map.get(
            provider_family_lower,
            {"supports_cache_hint": False, "supports_usage_reporting": False, "supports_session_id": False, "base_url_required": False},
        )
        dry_run = _dry_run_summary(
            provider_family=provider_family,
            model=model,
            provider_supports_cache_hint=bool(capability["supports_cache_hint"]),
            provider_supports_usage_reporting=bool(capability["supports_usage_reporting"]),
            provider_supports_session_id=bool(capability["supports_session_id"]),
            api_key_present=False,
            base_url_required=bool(capability["base_url_required"]),
            base_url_present=bool(str(base_url or "").strip()),
        )
        _write_dry_run_artifacts(output_dir, dry_run)
        return 0

    client = None
    try:
        client, planner, replay_mod = await _build_runtime(provider_family, api_key, model)
        _ensure_dir(output_dir)
        rows = []
        for row in _case_rows():
            if case_filter and row["case_id"] not in case_filter:
                continue
            rows.append(await _run_case(planner, replay_mod, row))

        samples_path = output_dir / "samples.jsonl"
        with samples_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        _write_summary(
            output_dir,
            rows,
            provider_family,
            model,
            provider_supports_cache_hint=bool(getattr(client, "supports_cache_hint", False)),
            provider_supports_usage_reporting=bool(getattr(client, "supports_usage_reporting", False)),
            provider_supports_session_id=bool(getattr(client, "supports_session_id", False)),
        )
        return 0
    finally:
        if client is not None:
            await client.__aexit__(None, None, None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live cache replay for the main reply chain.")
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "artifacts" / "main_reply_cache_live"),
        help="Directory receiving live replay artifacts.",
    )
    args = parser.parse_args()
    output_dir = Path(args.output_dir).resolve()
    _ensure_dir(output_dir)
    return asyncio.run(_run_live(output_dir))


if __name__ == "__main__":
    raise SystemExit(main())
