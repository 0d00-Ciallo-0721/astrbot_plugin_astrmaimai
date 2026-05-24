from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _short_hash(text: str) -> str:
    normalized = str(text or "")
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _preview(text: str, limit: int = 160) -> str:
    normalized = _normalize_text(text)
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def _ensure_artifact_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)


class _ReplayClient:
    def __init__(self) -> None:
        self.timeout = 10.0
        self.model = "offline-audit-model"

    async def chat_completion(self, *, messages, request_label, allow_thinking_fallback=True):
        last = messages[-1]["content"] if messages else ""
        if "Return only one valid JSON object" in last:
            content = "{}"
        else:
            content = "Brief offline reply."
        return {"choices": [{"message": {"content": content}}]}


def _build_planner():
    from tests.manual import kimi_replay_acceptance as replay_mod

    client = _ReplayClient()
    return replay_mod.build_planner(client)


def _build_event(case_id: str, text: str, *, chat_id: str = "default:GroupMessage:group-1", is_private: bool = False):
    from tests.manual import kimi_replay_acceptance as replay_mod

    case = replay_mod.ReplayCase(
        case_id=case_id,
        text=text,
        expected="offline audit",
        chat_id=chat_id,
        group_id="" if is_private else "group-1",
        focus_reason="private" if is_private else "at_bot",
        is_private=is_private,
    )
    return replay_mod.build_event(case)


async def _run_case(planner, case_id: str, text: str, *, chat_id: str = "default:GroupMessage:group-1", is_private: bool = False) -> dict[str, Any]:
    event = _build_event(case_id, text, chat_id=chat_id, is_private=is_private)
    reply_text = await planner.plan_and_execute(event, [event])
    turn_trace = planner.turn_trace_history[-1] if planner.turn_trace_history else {}
    continuity = dict((turn_trace.get("continuity") or {}))
    request_trace = dict(event.get_extra("astrmai_request_trace", {}) or {})
    prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
    return {
        "case_id": case_id,
        "chat_id": chat_id,
        "reply_preview": _preview(reply_text, 120),
        "turn_trace": turn_trace,
        "request_trace": request_trace,
        "prompt_envelope": {
            "focus_message_text": getattr(prompt_envelope, "focus_message_text", ""),
            "cognitive_drive_block": getattr(prompt_envelope, "cognitive_drive_block", ""),
            "soft_background_block": getattr(prompt_envelope, "soft_background_block", ""),
            "situational_context_block": getattr(prompt_envelope, "situational_context_block", ""),
            "planner_runtime_instruction_block": getattr(prompt_envelope, "planner_runtime_instruction_block", ""),
        },
        "continuity": continuity,
    }


def _build_injection_matrix(row: dict[str, Any]) -> list[dict[str, Any]]:
    continuity = dict(row.get("continuity", {}) or {})
    prompt_envelope = dict(row.get("prompt_envelope", {}) or {})
    request_trace = dict(row.get("request_trace", {}) or {})
    return [
        {
            "injection_site": "context_engine.frozen_prefix",
            "target": "system",
            "changes_every_turn": False,
            "provider_visible": True,
            "counted_by_prefix_hash": True,
            "affects_lane_or_cache": "yes",
            "evidence": {
                "prefix_hash": continuity.get("prefix_hash", ""),
                "semantic_system_hash": continuity.get("semantic_system_hash", ""),
                "frozen_prefix_length": continuity.get("frozen_prefix_length", 0),
            },
        },
        {
            "injection_site": "context_engine.soft_background_block",
            "target": "prompt",
            "changes_every_turn": bool(prompt_envelope.get("soft_background_block")),
            "provider_visible": True,
            "counted_by_prefix_hash": False,
            "affects_lane_or_cache": "dynamic_background_tail",
            "evidence": {
                "semi_stable_length": continuity.get("semi_stable_length", 0),
                "length": len(str(prompt_envelope.get("soft_background_block", "") or "")),
            },
        },
        {
            "injection_site": "prompt_refiner.time_anchor",
            "target": "prompt",
            "changes_every_turn": True,
            "provider_visible": True,
            "counted_by_prefix_hash": False,
            "affects_lane_or_cache": "dynamic_tail_only",
            "evidence": {},
        },
        {
            "injection_site": "prompt_refiner.cognitive_drive_block",
            "target": "prompt",
            "changes_every_turn": bool(prompt_envelope.get("cognitive_drive_block")),
            "provider_visible": True,
            "counted_by_prefix_hash": False,
            "affects_lane_or_cache": "dynamic_tail_only",
            "evidence": {
                "length": len(str(prompt_envelope.get("cognitive_drive_block", "") or "")),
            },
        },
        {
            "injection_site": "prompt_refiner.soft_background_block",
            "target": "prompt",
            "changes_every_turn": bool(prompt_envelope.get("soft_background_block")),
            "provider_visible": True,
            "counted_by_prefix_hash": False,
            "affects_lane_or_cache": "budgeted_background_tail",
            "evidence": {
                "length": len(str(prompt_envelope.get("soft_background_block", "") or "")),
            },
        },
        {
            "injection_site": "prompt_refiner.situational_context_block",
            "target": "prompt",
            "changes_every_turn": bool(prompt_envelope.get("situational_context_block")),
            "provider_visible": True,
            "counted_by_prefix_hash": False,
            "affects_lane_or_cache": "dynamic_tail_only",
            "evidence": {
                "length": len(str(prompt_envelope.get("situational_context_block", "") or "")),
            },
        },
        {
            "injection_site": "planner_side_inputs.planner_runtime_instruction_block",
            "target": "runtime_prompt",
            "changes_every_turn": bool(prompt_envelope.get("planner_runtime_instruction_block")),
            "provider_visible": True,
            "counted_by_prefix_hash": False,
            "affects_lane_or_cache": "runtime_control_tail",
            "evidence": {
                "length": len(str(prompt_envelope.get("planner_runtime_instruction_block", "") or "")),
            },
        },
        {
            "injection_site": "gateway.request_kwargs",
            "target": "request_kwargs",
            "changes_every_turn": bool(request_trace.get("request_session_id") or request_trace.get("request_cache_control")),
            "provider_visible": True,
            "counted_by_prefix_hash": False,
            "affects_lane_or_cache": "session_or_cache_hint",
            "evidence": {
                "request_session_id": request_trace.get("request_session_id", ""),
                "request_cache_control": request_trace.get("request_cache_control", ""),
            },
        },
        {
            "injection_site": "main.on_llm_request.gemini_reverse_session",
            "target": "system",
            "changes_every_turn": "provider_dependent",
            "provider_visible": True,
            "counted_by_prefix_hash": False,
            "affects_lane_or_cache": "post_hook_system_change",
            "evidence": {
                "post_hook_system_hash": continuity.get("post_hook_system_hash", ""),
                "provider_visible_system_hash": continuity.get("provider_visible_system_hash", ""),
            },
        },
    ]


def _build_provider_family_matrix(sample: dict[str, Any]) -> list[dict[str, Any]]:
    continuity = dict(sample.get("continuity", {}) or {})
    return [
        {
            "provider_family": "anthropic",
            "explicit_cache_hint": True,
            "remote_session_reuse": False,
            "native_prompt_cache": True,
            "main_reply_current_code_path": "cache_control available on chat/tool lane request kwargs",
            "verdict": "best-prepared among current main-reply families",
        },
        {
            "provider_family": "gemini",
            "explicit_cache_hint": False,
            "remote_session_reuse": False,
            "native_prompt_cache": True,
            "main_reply_current_code_path": "no cache_control; post-hook reverse-session sentinel can modify provider-visible system",
            "verdict": "potentially cacheable but trace must distinguish pre-hook and post-hook system hashes",
        },
        {
            "provider_family": "native_chat",
            "explicit_cache_hint": False,
            "remote_session_reuse": False,
            "native_prompt_cache": True,
            "main_reply_current_code_path": "OpenAI-compatible chat completions transport; `provider_family` here is a protocol family, not an upstream vendor identifier",
            "verdict": "treat this as an OpenAI-compatible endpoint classification; if the model points to kimi-* or another upstream label, that still does not prove a native upstream provider path was validated",
        },
        {
            "provider_family": "runner",
            "explicit_cache_hint": False,
            "remote_session_reuse": True,
            "native_prompt_cache": False,
            "main_reply_current_code_path": "session reuse may improve continuity, not equivalent to token cache hit",
            "verdict": "do not count provider_session_reuse as cache hit",
        },
        {
            "provider_family": "sample_from_trace",
            "explicit_cache_hint": bool(continuity.get("request_cache_control")),
            "remote_session_reuse": bool(continuity.get("request_session_id")),
            "native_prompt_cache": "unknown_without_provider_usage",
            "main_reply_current_code_path": continuity.get("request_provider_family", ""),
            "verdict": {
                "provider_family": continuity.get("request_provider_family", ""),
                "request_model_id": continuity.get("request_model_id", ""),
                "usage_input_cached": continuity.get("usage_input_cached", 0),
            },
        },
    ]


def _build_findings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    first = rows[0]
    continuity = dict(first.get("continuity", {}) or {})
    findings: list[dict[str, Any]] = []
    findings.append(
        {
            "title": "prefix_hash 只覆盖 frozen_prefix，不覆盖整个 provider-visible system",
            "severity": "high",
            "evidence": {
                "semantic_system_hash": continuity.get("semantic_system_hash", ""),
                "frozen_prefix_length": continuity.get("frozen_prefix_length", 0),
                "semi_stable_length": continuity.get("semi_stable_length", 0),
                "provider_visible_system_hash": continuity.get("provider_visible_system_hash", ""),
                "prefix_hash": continuity.get("prefix_hash", ""),
            },
            "implication": "prefix_stable 不等于最终 system prompt 稳定，不能直接当作 token cache 命中前提。",
        }
    )
    findings.append(
        {
            "title": "semantic_system_hash 稳定但 provider_visible_system_hash 波动时，应归因为 hook/provider 层",
            "severity": "medium",
            "evidence": {
                "semantic_system_hash": continuity.get("semantic_system_hash", ""),
                "provider_visible_system_hash": continuity.get("provider_visible_system_hash", ""),
                "post_hook_system_hash": continuity.get("post_hook_system_hash", ""),
            },
            "implication": "这说明主回复硬系统语义层稳定，但 provider 最终可见字符串仍可能被 hook 或 provider 特定处理改变。",
        }
    )
    findings.append(
        {
            "title": "主回复软背景已迁到 prompt，但仍需预算裁剪避免背景主导当前回复",
            "severity": "medium",
            "evidence": {
                "dynamic_prompt_length": continuity.get("dynamic_prompt_length", 0),
                "semi_stable_length": continuity.get("semi_stable_length", 0),
                "soft_background_length": len(str((first.get("prompt_envelope", {}) or {}).get("soft_background_block", "") or "")),
                "semi_stable_blocks": continuity.get("semi_stable_blocks", {}),
            },
            "implication": "system 抖动已经下降，但如果软背景不做预算和优先级控制，仍会干扰当前用户问题的主线回复。",
        }
    )
    findings.append(
        {
            "title": "Gemini reverse-session hook 是 post-gateway 的 system 注入，天然属于统计盲区风险",
            "severity": "medium",
            "evidence": {
                "post_hook_system_hash": continuity.get("post_hook_system_hash", ""),
                "provider_visible_system_hash": continuity.get("provider_visible_system_hash", ""),
            },
            "implication": "若 post_hook_system_hash 与 gateway 侧 system hash 不一致，则 context_economy 对最终可缓存前缀的判断会失真。",
        }
    )
    findings.append(
        {
            "title": "provider_session_usage_rate / prefix_stable / cache_affinity_ready_rate 都不是 cache hit 证据",
            "severity": "high",
            "evidence": {
                "request_session_id": continuity.get("request_session_id", ""),
                "cache_ready": continuity.get("cache_ready", False),
                "cache_ready_reasons": continuity.get("cache_ready_reasons", []),
                "usage_input_cached": continuity.get("usage_input_cached", 0),
            },
            "implication": "只有 provider 返回 cached input tokens 或等价 usage 字段，才能证明真实命中。",
        }
    )
    findings.append(
        {
            "title": "cold summary / recent transcript 裁剪 / memory budget 属于缩短输入，不等于缓存命中",
            "severity": "medium",
            "evidence": {
                "dynamic_prompt_blocks": continuity.get("dynamic_prompt_blocks", {}),
                "conversation_compression": (first.get("turn_trace", {}) or {}).get("conversation_compression", {}),
            },
            "implication": "报告里必须把“输入变短节约”与“缓存命中节约”分开统计。",
        }
    )
    return findings


def _build_markdown_report(rows: list[dict[str, Any]], findings: list[dict[str, Any]], output_path: Path) -> None:
    first = rows[0] if rows else {}
    continuity = dict(first.get("continuity", {}) or {})
    injection_matrix = _build_injection_matrix(first) if first else []
    provider_matrix = _build_provider_family_matrix(first) if first else []
    live_summary_root = REPO_ROOT / "artifacts" / "main_reply_cache_live"
    live_summary_json = live_summary_root / "summary.json"
    live_summary_path = live_summary_root / "summary.md"
    for provider_family in ("anthropic", "gemini", "native_chat"):
        provider_dir = live_summary_root / provider_family
        if (provider_dir / "summary.json").exists():
            live_summary_json = provider_dir / "summary.json"
            live_summary_path = provider_dir / "summary.md"
            break
    lines = [
        "# 主回复 LLM 构造 / 注入 / Token Cache 深度分析",
        "",
        "## 结论摘要",
        "",
        f"- 可缓存性：主回复当前具备部分 cache-friendly 结构，但 `prefix_hash` 只覆盖 `frozen_prefix`，不能代表最终 provider-visible system 全稳定。",
        f"- 真实命中证据：当前只有 provider 返回 `usage.input_cached` 时才能证明命中；`cache_ready` 只代表准备条件存在，不代表真实命中。",
        f"- token 节约效果：当前主回复更明确的是“输入缩短节约”，缓存命中节约仍需 provider usage 证据支持。",
        "",
        "## 主链调用图",
        "",
        "```text",
        "message_entry -> system2_runner -> planner.plan_and_execute",
        "  -> context_engine.build_prompt",
        "  -> planner_side_inputs/_apply_private_jump_context/_append_mode_instructions",
        "  -> prompt_refiner.refine_prompt",
        "  -> executor.execute",
        "  -> gateway.chat_in_lane_result / gateway.tool_chat_in_lane_result",
        "  -> context.llm_generate / context.tool_loop_agent",
        "  -> main.on_llm_request (Gemini reverse-session post-hook)",
        "```",
        "",
        "## 关键指标",
        "",
        f"- prefix_hash: `{continuity.get('prefix_hash', '')}`",
        f"- semantic_system_hash: `{continuity.get('semantic_system_hash', '')}`",
        f"- semantic_system_length: `{continuity.get('semantic_system_length', 0)}`",
        f"- provider_visible_system_hash: `{continuity.get('provider_visible_system_hash', '')}`",
        f"- post_hook_system_hash: `{continuity.get('post_hook_system_hash', '')}`",
        f"- provider_visible_prompt_hash: `{continuity.get('provider_visible_prompt_hash', '')}`",
        f"- frozen_prefix_length: `{continuity.get('frozen_prefix_length', 0)}`",
        f"- semi_stable_length: `{continuity.get('semi_stable_length', 0)}`",
        f"- dynamic_prompt_length: `{continuity.get('dynamic_prompt_length', 0)}`",
        f"- dynamic_prompt_blocks.soft_background: `{(continuity.get('dynamic_prompt_blocks', {}) or {}).get('soft_background', 0)}`",
        f"- request_provider_family: `{continuity.get('request_provider_family', '')}`",
        f"- request_model_id: `{continuity.get('request_model_id', '')}`",
        f"- request_session_id: `{continuity.get('request_session_id', '')}`",
        f"- request_cache_control: `{continuity.get('request_cache_control', '')}`",
        f"- cache_ready: `{continuity.get('cache_ready', False)}`",
        f"- cache_ready_reasons: `{continuity.get('cache_ready_reasons', [])}`",
        f"- cache_hit: `{continuity.get('cache_hit', False)}`",
        f"- cache_hit_evidence_supported: `{continuity.get('cache_hit_evidence_supported', False)}`",
        f"- usage_input_cached: `{continuity.get('usage_input_cached', 0)}`",
        "",
        "## Hash Responsibility",
        "",
        "- `prefix_hash`: continuity/native prefix 兼容口径，用于运行链路与历史稳定性观察；当前实现里它可能与 `semantic_system_hash` 数值相同，但职责不同。",
        "- `semantic_system_hash`: 语义层 hard system 是否真的变化的主判断依据。",
        "- `provider_visible_system_hash`: 最终 provider 可见 system 的稳定性，可能受 hook/provider 后处理影响。",
        "",
        "## 注入矩阵",
        "",
        "| 注入位置 | 注入目标 | 是否每轮变化 | 是否 provider 可见 | 是否被 prefix_hash 统计 | 是否影响 lane/session/cache |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in injection_matrix:
        lines.append(
            f"| {item['injection_site']} | {item['target']} | {item['changes_every_turn']} | {item['provider_visible']} | {item['counted_by_prefix_hash']} | {item['affects_lane_or_cache']} |"
        )
    lines.extend(
        [
            "",
            "## Provider Family 判定",
            "",
            "| Provider Family | 显式 cache hint | remote session | 原生 prompt cache | 主回复当前代码路径 | 判定 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in provider_matrix:
        lines.append(
            f"| {item['provider_family']} | {item['explicit_cache_hint']} | {item['remote_session_reuse']} | {item['native_prompt_cache']} | {item['main_reply_current_code_path']} | {item['verdict']} |"
        )
    lines.extend(
        [
            "",
            "## 主要发现",
            "",
        ]
    )
    for finding in findings:
        lines.append(f"- [{finding['severity']}] {finding['title']}: {finding['implication']}")
    lines.extend(
        [
            "",
            "## 节约 Token 口径",
            "",
            "- 缓存型节约：只有 provider usage 明确返回 `input_cached` 或等价字段才算。",
            "- 缩短输入型节约：`cold summary`、`recent transcript` 裁剪、`memory budget`、动态块迁到 prompt 都属于这类。",
            "- `semantic_system_hash` 表示语义层稳定；`provider_visible_system_hash` 表示最终 provider 可见 system 稳定，两者允许不一致。",
            "",
            "## 验证边界",
            "",
            "- 这份报告默认使用离线 replay，不证明真实 provider 已命中缓存。",
            "- 若需要证明真实命中，必须在真实 provider 上复跑，并观察 `usage.input_cached` 连续变化。",
        ]
    )
    if live_summary_path.exists():
        live_summary = {}
        if live_summary_json.exists():
            try:
                live_summary = json.loads(live_summary_json.read_text(encoding="utf-8"))
            except Exception:
                live_summary = {}
        lines.extend(
            [
                "",
                "## Live Replay",
                "",
                f"- 最近一轮 live 基线：`{live_summary_path}`",
                f"- cache_ready_rate: `{live_summary.get('cache_ready_rate', '')}`",
                f"- cache_hit_rate: `{live_summary.get('cache_hit_rate', '')}`",
                f"- cache_ready_but_hit_miss_count: `{live_summary.get('cache_ready_but_hit_miss_count', '')}`",
                f"- validation_verdict: `{live_summary.get('validation_verdict', '')}`",
                f"- provider_supports_cache_hint: `{live_summary.get('provider_supports_cache_hint', '')}`",
                f"- provider_supports_usage_reporting: `{live_summary.get('provider_supports_usage_reporting', '')}`",
                f"- cache_hint_observed_enabled: `{live_summary.get('cache_hint_observed_enabled', '')}`",
                f"- hash_stable_but_cache_miss_count: `{live_summary.get('hash_stable_but_cache_miss_count', '')}`",
                f"- hook_changed_system_case_ids: `{live_summary.get('hook_changed_system_case_ids', [])}`",
            ]
        )
    output_path.write_text("\n".join(lines), encoding="utf-8")


async def _run_audit(output_dir: Path) -> dict[str, Any]:
    planner = _build_planner()
    rows = [
        await _run_case(planner, "same-chat-turn-1", "Do you still remember what I said about exams?"),
        await _run_case(planner, "same-chat-turn-2", "Then answer only the key point.", chat_id="default:GroupMessage:group-1"),
        await _run_case(planner, "private-turn", "今天有点累，陪我聊两句。", chat_id="default:FriendMessage:user-1", is_private=True),
    ]
    findings = _build_findings(rows)
    result = {
        "rows": rows,
        "findings": findings,
    }
    (output_dir / "main_reply_cache_audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _build_markdown_report(rows, findings, output_dir / "main_reply_cache_audit.md")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline audit for main-reply cacheability and token economy.")
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "artifacts" / "main_reply_cache_audit"),
        help="Directory receiving the audit artifacts.",
    )
    args = parser.parse_args()
    output_dir = Path(args.output_dir).resolve()
    _ensure_artifact_dir(output_dir)
    asyncio.run(_run_audit(output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
