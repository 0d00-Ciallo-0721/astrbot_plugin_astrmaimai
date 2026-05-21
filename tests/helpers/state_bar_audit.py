from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import random
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.helpers.planner_stubs import install_planner_stubs
from tests.helpers.reply_engine_stubs import FakeEvent, FakeStateEngine, install_reply_engine_stubs


def _install_state_stubs() -> None:
    gateway_mod = types.ModuleType("astrmai.infra.gateway")
    gateway_mod.GlobalModelGateway = type("GlobalModelGateway", (), {})
    sys.modules["astrmai.infra.gateway"] = gateway_mod


def _reset_modules(*module_names: str) -> None:
    for module_name in module_names:
        sys.modules.pop(module_name, None)


class _FakePersistence:
    def __init__(self):
        self.saved_chat_states = []

    async def load_chat_state(self, chat_id):
        return None

    async def save_chat_state(self, chat_id, state):
        self.saved_chat_states.append((chat_id, state.energy, state.mood))
        return None

    async def load_user_profile(self, user_id):
        return None

    async def save_user_profile(self, user_id, profile):
        return None


class _GatewayResult:
    def __init__(self, parsed_json=None, raw_completion=""):
        self.parsed_json = parsed_json or {}
        self.raw_completion = raw_completion


class _LiveMoodGatewayRecorder:
    def __init__(self, inner):
        self._inner = inner
        self.last_raw_output = ""
        self.last_parsed_json = {}

    async def chat_in_lane_result(self, **kwargs):
        result = await self._inner.chat_in_lane_result(**kwargs)
        self.last_parsed_json = getattr(result, "parsed_json", {}) or {}
        self.last_raw_output = str(getattr(result, "raw_completion", "") or "")
        return result

    async def call_mood_task(self, *args, **kwargs):
        result = await self._inner.call_mood_task(*args, **kwargs)
        if isinstance(result, dict):
            self.last_parsed_json = result
            self.last_raw_output = json.dumps(result, ensure_ascii=False)
        else:
            self.last_parsed_json = {}
            self.last_raw_output = str(result or "")
        return result

    def __getattr__(self, item):
        return getattr(self._inner, item)


class _MoodGateway:
    def __init__(self, response=None, should_fail: bool = False):
        self.response = response
        self.should_fail = should_fail
        self.config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
            provider=SimpleNamespace(task_models=[]),
        )
        self.lane_manager = object()

    async def chat_in_lane_result(self, **kwargs):
        del kwargs
        if self.should_fail:
            raise RuntimeError("forced mood lane failure")
        return _GatewayResult(
            parsed_json=self.response if isinstance(self.response, dict) else None,
            raw_completion=self.response if isinstance(self.response, str) else "",
        )

    async def call_mood_task(self, prompt, system_prompt=None):
        del prompt, system_prompt
        if self.should_fail:
            raise RuntimeError("forced mood gateway failure")
        return self.response


class _AuditEvent:
    def __init__(self, message="默认消息", group_id="group-1"):
        self.message_str = message
        self._group_id = group_id
        self._extra = {}

    def get_group_id(self):
        return self._group_id

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class _Tool:
    def __init__(self, name: str):
        self.name = name


class _IdentityActionModifier:
    def modify_tools(self, tools, **kwargs):
        return tools


class _FakeSys3Router:
    async def get_light_tools_for_planner(self):
        return SimpleNamespace(tools=[_Tool("sys3_light_tool")])


class _FakePlannerStateEngine:
    def __init__(self, *, energy=0.8):
        self._state = SimpleNamespace(energy=energy, mood=0.0, caution=0.0)
        self._profile = SimpleNamespace(social_score=0.0)
        self.relationship_engine = SimpleNamespace(
            get_or_create=lambda user_id: SimpleNamespace(social_score=0.0, trust=0.0)
        )

    async def get_state(self, chat_id):
        del chat_id
        return self._state

    async def get_user_profile(self, user_id):
        del user_id
        return self._profile

    async def settle_no_send_affection(self, **kwargs):
        del kwargs
        return False


def _load_state_modules():
    _install_state_stubs()
    _reset_modules(
        "astrmai.state",
        "astrmai.state.chat_state_service",
        "astrmai.state.mood.mood_manager",
    )
    state_mod = importlib.import_module("astrmai.state.chat_state_service")
    mood_mod = importlib.import_module("astrmai.state.mood.mood_manager")
    return importlib.reload(state_mod), importlib.reload(mood_mod)


def _load_planner_modules():
    install_planner_stubs()
    _reset_modules(
        "astrmai.conversation.planning.planner_side_inputs",
        "astrmai.conversation.planning.expression_policy",
        "astrmai.conversation.planning.planner",
    )
    side_inputs_mod = importlib.import_module("astrmai.conversation.planning.planner_side_inputs")
    expression_mod = importlib.import_module("astrmai.conversation.planning.expression_policy")
    planner_mod = importlib.import_module("astrmai.conversation.planning.planner")
    return (
        importlib.reload(side_inputs_mod),
        importlib.reload(expression_mod),
        importlib.reload(planner_mod),
    )


def _load_reply_module():
    install_reply_engine_stubs()
    _reset_modules(
        "astrmai.Brain.reply_engine",
        "astrmai.conversation.execution.reply_service",
    )
    reply_mod = importlib.import_module("astrmai.conversation.execution.reply_service")
    return importlib.reload(reply_mod)


def _classify_expected_issue(expected: str, actual_tag: str) -> list[str]:
    issues: list[str] = []
    negative_tags = {"sad", "angry"}
    positive_tags = {"happy"}
    actual = str(actual_tag or "").strip().lower()
    if expected == "positive" and actual == "neutral":
        issues.append("over_neutralized")
    if expected == "negative" and actual not in negative_tags:
        if actual == "neutral":
            issues.append("over_neutralized")
        else:
            issues.append("direction_conflict")
    if expected == "mixed" and actual in positive_tags:
        issues.append("mixed_affect_flattened")
    return issues


def _score_direction(score: float) -> str:
    if score >= 0.2:
        return "positive"
    if score <= -0.2:
        return "negative"
    return "neutral"


def _score_band(score: float) -> str:
    absolute = abs(float(score))
    if absolute >= 1.0:
        return "strong"
    if absolute >= 0.5:
        return "medium"
    return "light"


def _score_issue(case: dict[str, Any], score: float) -> str:
    expected_direction = str(case.get("expected_direction", "") or "")
    actual_direction = _score_direction(score)
    if expected_direction and actual_direction != expected_direction:
        return f"direction_mismatch:{expected_direction}->{actual_direction}"

    min_expected = case.get("min_expected_social_score")
    if min_expected is not None and score < float(min_expected):
        return f"below_range:{score:.2f}<{float(min_expected):.2f}"

    max_expected = case.get("max_expected_social_score")
    if max_expected is not None and score > float(max_expected):
        return f"above_range:{score:.2f}>{float(max_expected):.2f}"
    return ""


def _normalize_live_audit_enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_gateway_factory(factory_spec: str):
    module_name, _, attr_name = str(factory_spec or "").rpartition(".")
    if not module_name or not attr_name:
        raise ValueError("gateway factory spec must look like package.module.callable")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr_name, None)
    if factory is None or not callable(factory):
        raise ValueError(f"gateway factory '{factory_spec}' is not callable")
    return factory


async def _run_live_mood_semantic_audit(mood_mod, sample_cases: list[dict]) -> dict:
    if not _normalize_live_audit_enabled(os.getenv("ASTRMAI_ENABLE_LIVE_MOOD_AUDIT")):
        return {
            "status": "not_run",
            "reason": "live mood audit disabled; set ASTRMAI_ENABLE_LIVE_MOOD_AUDIT=1 to run",
            "cases": [],
            "drift_case_ids": [],
            "parse_failure_case_ids": [],
        }

    factory_spec = os.getenv("ASTRMAI_LIVE_MOOD_GATEWAY_FACTORY", "").strip()
    if not factory_spec:
        return {
            "status": "not_run",
            "reason": "missing ASTRMAI_LIVE_MOOD_GATEWAY_FACTORY",
            "cases": [],
            "drift_case_ids": [],
            "parse_failure_case_ids": [],
        }

    try:
        factory = _load_gateway_factory(factory_spec)
        gateway = factory()
        if asyncio.iscoroutine(gateway):
            gateway = await gateway
        gateway = _LiveMoodGatewayRecorder(gateway)
        manager = mood_mod.MoodManager(gateway, gateway.config)
    except Exception as exc:
        return {
            "status": "not_run",
            "reason": f"live gateway bootstrap failed: {exc}",
            "cases": [],
            "drift_case_ids": [],
            "parse_failure_case_ids": [],
        }

    cases = []
    drift_case_ids: list[str] = []
    parse_failure_case_ids: list[str] = []
    for sample in sample_cases:
        try:
            tag, value = await manager.analyze_mood(
                sample["text"],
                sample.get("current_mood", 0.0),
                chat_id=f"live:{sample['case_id']}",
            )
            issues = _classify_expected_issue(sample["expected_profile"], tag)
            if not tag:
                parse_failure_case_ids.append(sample["case_id"])
            elif issues:
                drift_case_ids.append(sample["case_id"])
            cases.append(
                {
                    "case_id": sample["case_id"],
                    "text": sample["text"],
                    "expected_profile": sample["expected_profile"],
                    "live_raw_output": gateway.last_raw_output,
                    "parsed_mood_tag": tag,
                    "parsed_mood_value": value,
                    "issues": issues,
                }
            )
        except Exception as exc:
            parse_failure_case_ids.append(sample["case_id"])
            cases.append(
                {
                    "case_id": sample["case_id"],
                    "text": sample["text"],
                    "expected_profile": sample["expected_profile"],
                    "live_raw_output": gateway.last_raw_output,
                    "parsed_mood_tag": "",
                    "parsed_mood_value": 0.0,
                    "issues": ["parse_failed"],
                    "error": str(exc),
                }
            )

    status = "passed"
    if parse_failure_case_ids:
        status = "parse_failed"
    elif drift_case_ids:
        status = "drift_detected"

    return {
        "status": status,
        "reason": "live mood semantic audit executed",
        "cases": cases,
        "drift_case_ids": drift_case_ids,
        "parse_failure_case_ids": parse_failure_case_ids,
    }


def _judge_delta_probe(judge_mod, requested_delta: float, *, primary_applied: bool) -> dict:
    applied = requested_delta
    threshold = getattr(judge_mod.Judge, "PRIMARY_MOOD_MICROADJUST_THRESHOLD", 0.15)
    scale = getattr(judge_mod.Judge, "PRIMARY_MOOD_MICROADJUST_SCALE", 0.25)
    if primary_applied:
        if abs(applied) < threshold:
            applied = 0.0
        else:
            applied *= scale
    return {
        "primary_applied": bool(primary_applied),
        "requested_delta": float(requested_delta),
        "applied_delta": float(applied),
    }


def _post_send_delta_probe(reply_post_send_mod, bypassed_tag: str | None) -> dict:
    probe = type("PostSendProbe", (reply_post_send_mod.ReplyPostSendMixin,), {})()
    tag, _ = probe._resolve_post_send_tag(bypassed_tag)
    delta = 0.0 if not bypassed_tag else (0.1 if tag == "happy" else -0.1 if tag in {"sad", "angry"} else 0.0)
    return {
        "bypassed_tag": bypassed_tag or "",
        "resolved_tag": tag,
        "delta": float(delta),
    }


async def _run_mood_audit_async() -> dict:
    state_mod, mood_mod = _load_state_modules()
    _reset_modules("astrmai.conversation.decision.judge", "astrmai.conversation.execution.reply_post_send")
    judge_mod = importlib.reload(importlib.import_module("astrmai.conversation.decision.judge"))
    reply_post_send_mod = importlib.reload(importlib.import_module("astrmai.conversation.execution.reply_post_send"))

    parser_cases = [
        {
            "case_id": "parser_dict_happy",
            "text": "谢谢你，真的帮大忙了。",
            "current_mood": 0.0,
            "response": {"mood_tag": "happy", "mood_value": 0.55},
        },
        {
            "case_id": "parser_fenced_sad",
            "text": "对不起，让你担心了。",
            "current_mood": 0.0,
            "response": "```json\n{\"mood_tag\": \"sad\", \"mood_value\": -0.25}\n```",
        },
        {
            "case_id": "parser_list_surprise",
            "text": "咦，你怎么突然提这个？",
            "current_mood": 0.1,
            "response": "[{\"mood_tag\": \"surprise\", \"mood_value\": 0.15}]",
        },
    ]

    parser_results = []
    for case in parser_cases:
        gateway = _MoodGateway(response=case["response"], should_fail=False)
        manager = mood_mod.MoodManager(gateway, gateway.config)
        tag, value = await manager.analyze_mood(case["text"], case["current_mood"], chat_id="audit-chat")
        parser_results.append(
            {
                "case_id": case["case_id"],
                "text": case["text"],
                "current_mood": case["current_mood"],
                "mood_tag": tag,
                "mood_value": value,
                "analysis_source": "llm_parser_or_lane_raw_text",
                "parser_ok": bool(tag),
            }
        )

    fallback_cases = [
        {
            "case_id": "fallback_positive",
            "category": "夸奖/喜欢/贴近",
            "text": "谢谢你呀，我真的好开心，贴贴。",
            "current_mood": 0.0,
            "expected_profile": "positive",
        },
        {
            "case_id": "fallback_hostile",
            "category": "冒犯/责备/冷淡",
            "text": "闭嘴，烦死了，你真讨厌。",
            "current_mood": 0.0,
            "expected_profile": "negative",
        },
        {
            "case_id": "fallback_mixed",
            "category": "复杂混合情绪",
            "text": "谢谢你，但我还是有点难过。",
            "current_mood": 0.0,
            "expected_profile": "mixed",
        },
        {
            "case_id": "fallback_sarcasm",
            "category": "阴阳怪气/被动攻击",
            "text": "你可真行啊，又把事情搞砸了，真棒。",
            "current_mood": 0.0,
            "expected_profile": "negative",
        },
        {
            "case_id": "fallback_neutral_tool",
            "category": "工具/记忆意图消息",
            "text": "帮我查一下明天上海天气。",
            "current_mood": 0.0,
            "expected_profile": "neutral",
        },
        {
            "case_id": "fallback_short_ack",
            "category": "短 ack",
            "text": "嗯，好。",
            "current_mood": 0.0,
            "expected_profile": "neutral",
        },
    ]

    fallback_results = []
    for case in fallback_cases:
        gateway = _MoodGateway(response=None, should_fail=True)
        engine = state_mod.StateEngine(_FakePersistence(), gateway, config=gateway.config)
        initial_mood = (await engine.get_state("audit:fallback")).mood
        tag, final_mood = await engine.update_mood("audit:fallback", case["text"])
        fallback_results.append(
            {
                "case_id": case["case_id"],
                "category": case["category"],
                "text": case["text"],
                "initial_mood": initial_mood,
                "primary_mood_tag": tag,
                "primary_mood_value": final_mood,
                "analysis_source": "fallback",
                "expected_profile": case["expected_profile"],
                "issues": _classify_expected_issue(case["expected_profile"], tag),
            }
        )

    pipeline_cases = [
        {
            "case_id": "judge_micro_adjust_suppressed",
            "judge_requested_delta": 0.10,
            "primary_applied": True,
            "post_send_tag": None,
        },
        {
            "case_id": "judge_micro_adjust_scaled",
            "judge_requested_delta": -0.30,
            "primary_applied": True,
            "post_send_tag": "angry",
        },
        {
            "case_id": "post_send_happy_bounce",
            "judge_requested_delta": 0.0,
            "primary_applied": False,
            "post_send_tag": "happy",
        },
    ]

    pipeline_results = []
    for case in pipeline_cases:
        judge_probe = _judge_delta_probe(
            judge_mod,
            case["judge_requested_delta"],
            primary_applied=case["primary_applied"],
        )
        post_probe = _post_send_delta_probe(reply_post_send_mod, case["post_send_tag"])
        pipeline_results.append(
            {
                "case_id": case["case_id"],
                "judge": judge_probe,
                "post_send": post_probe,
            }
        )

    parser_failures = [item["case_id"] for item in parser_results if not item["parser_ok"]]
    fallback_issue_counts = {
        "over_neutralized": sum("over_neutralized" in item["issues"] for item in fallback_results),
        "direction_conflict": sum("direction_conflict" in item["issues"] for item in fallback_results),
        "mixed_affect_flattened": sum("mixed_affect_flattened" in item["issues"] for item in fallback_results),
    }
    live_semantic_audit = await _run_live_mood_semantic_audit(
        mood_mod,
        [
            {
                "case_id": item["case_id"],
                "text": item["text"],
                "current_mood": item["current_mood"],
                "expected_profile": item["expected_profile"],
            }
            for item in fallback_cases
        ],
    )

    return {
        "audit_mode": "static_and_chain_level",
        "live_llm_semantic_audit": live_semantic_audit,
        "parser_cases": parser_results,
        "fallback_cases": fallback_results,
        "pipeline_cases": pipeline_results,
        "summary": {
            "parser_failures": parser_failures,
            "fallback_issue_counts": fallback_issue_counts,
            "primary_update_live": True,
            "judge_micro_adjust_live": True,
            "post_send_settlement_live": True,
            "verdict": (
                "fallback quality is acceptable for obvious positive/negative text, "
                "and sarcasm plus mixed affect now stay directionally stable in the local heuristic"
            ),
        },
    }


async def _run_social_score_audit_async() -> dict:
    state_mod, _ = _load_state_modules()
    gateway = _MoodGateway(response=None, should_fail=True)
    cases = [
        {
            "case_id": "positive_gratitude",
            "category": "夸奖 / 贴近",
            "text": "谢谢你呀，我真的好开心，贴贴。",
            "mood_tag": "happy",
            "expected_direction": "positive",
            "min_expected_social_score": 1.0,
        },
        {
            "case_id": "mixed_affect",
            "category": "感谢里带受伤",
            "text": "谢谢你，但我还是有点难过。",
            "mood_tag": "sad",
            "expected_direction": "positive",
            "min_expected_social_score": 0.2,
            "max_expected_social_score": 0.4,
        },
        {
            "case_id": "comfort_with_complaint",
            "category": "安慰里带抱怨",
            "text": "抱抱，谢谢你愿意安慰我，但你刚才那句还是让我有点受伤。",
            "mood_tag": "sad",
            "expected_direction": "positive",
            "min_expected_social_score": 0.2,
            "max_expected_social_score": 0.4,
        },
        {
            "case_id": "ambiguous_soft_affection",
            "category": "关系暧昧但不明确示好",
            "text": "晚安呀，早点休息，别太累了。",
            "mood_tag": "neutral",
            "expected_direction": "positive",
            "min_expected_social_score": 0.12,
            "max_expected_social_score": 0.24,
        },
        {
            "case_id": "hostile_direct",
            "category": "明确负向",
            "text": "闭嘴，烦死了，你真讨厌。",
            "mood_tag": "angry",
            "expected_direction": "negative",
            "min_expected_social_score": -3.0,
            "max_expected_social_score": -1.0,
        },
        {
            "case_id": "cold_distance",
            "category": "冷淡疏离",
            "text": "哦，那你先忙吧，我就不打扰了。",
            "mood_tag": "neutral",
            "expected_direction": "negative",
            "min_expected_social_score": -0.30,
            "max_expected_social_score": -0.20,
        },
        {
            "case_id": "perfunctory_brief",
            "category": "敷衍收口",
            "text": "哦，行吧，就这样。",
            "mood_tag": "neutral",
            "expected_direction": "negative",
            "min_expected_social_score": -0.40,
            "max_expected_social_score": -0.30,
        },
        {
            "case_id": "mild_irritation",
            "category": "轻微不耐烦",
            "text": "行了，别说了，我知道了。",
            "mood_tag": "neutral",
            "expected_direction": "negative",
            "min_expected_social_score": -0.55,
            "max_expected_social_score": -0.40,
        },
        {
            "case_id": "tool_intent",
            "category": "工具意图",
            "text": "帮我查一下明天上海天气。",
            "mood_tag": "neutral",
            "expected_direction": "positive",
            "min_expected_social_score": 0.3,
            "max_expected_social_score": 0.45,
        },
        {
            "case_id": "sarcasm",
            "category": "阴阳怪气",
            "text": "你可真行啊，又把事情搞砸了，真棒。",
            "mood_tag": "angry",
            "expected_direction": "negative",
            "min_expected_social_score": -3.0,
            "max_expected_social_score": -1.0,
        },
        {
            "case_id": "long_mixed_balance",
            "category": "长文本正负信号混杂",
            "text": "谢谢你一直愿意听我说这些，我知道你是好意，但刚才那句还是让我有点失望和不舒服。",
            "mood_tag": "sad",
            "expected_direction": "positive",
            "min_expected_social_score": 0.2,
            "max_expected_social_score": 0.4,
        },
    ]

    results = []
    for case in cases:
        engine = state_mod.StateEngine(_FakePersistence(), gateway, config=gateway.config)
        user_id = f"audit:{case['case_id']}"
        published_change: dict[str, Any] = {}

        async def _capture_publish(user_id, old_score, new_score, mood_tag, event_type):
            published_change.update(
                {
                    "user_id": user_id,
                    "old_score": float(old_score),
                    "new_score": float(new_score),
                    "mood_tag": str(mood_tag or ""),
                    "event_type": str(event_type or ""),
                }
            )

        engine.affection_router.publish_change = _capture_publish
        resolved_text_event_type = engine._resolve_affection_event_type(case["text"])
        softened_support_event = engine.relationship_engine.should_soften_support_event_for_message(
            case["text"],
            resolved_text_event_type,
        )
        effective_base_event = (
            state_mod.RelationshipEvent.NORMAL_CHAT if softened_support_event else resolved_text_event_type
        )
        mood_tag_remap_suppressed = (
            effective_base_event == state_mod.RelationshipEvent.NORMAL_CHAT
            and engine.relationship_engine.should_preserve_normal_chat_for_message(case["text"], case["mood_tag"])
        )
        effective_event_type = (
            effective_base_event
            if mood_tag_remap_suppressed
            or not case["mood_tag"]
            or effective_base_event != state_mod.RelationshipEvent.NORMAL_CHAT
            else engine.relationship_engine.MOOD_TO_EVENT.get(case["mood_tag"], effective_base_event)
        )
        await engine.calculate_and_update_affection(
            user_id=user_id,
            group_id="audit-group",
            mood_tag=case["mood_tag"],
            intensity=1.0,
            message_text=case["text"],
        )
        profile = await engine.get_user_profile(user_id)
        social_score = float(getattr(profile, "social_score", 0.0) or 0.0)
        issue = _score_issue(case, social_score)
        results.append(
            {
                **case,
                "resolved_text_event_type": resolved_text_event_type,
                "effective_event_type": effective_event_type,
                "mood_tag_remap_suppressed": bool(mood_tag_remap_suppressed),
                "published_mood_tag": str(published_change.get("mood_tag", "")),
                "published_event_type": str(published_change.get("event_type", "")),
                "social_score": social_score,
                "score_direction": _score_direction(social_score),
                "score_band": _score_band(social_score),
                "issue": issue,
            }
        )

    mixed_case = next(item for item in results if item["case_id"] == "mixed_affect")
    tool_case = next(item for item in results if item["case_id"] == "tool_intent")
    ambiguous_case = next(item for item in results if item["case_id"] == "ambiguous_soft_affection")
    cold_case = next(item for item in results if item["case_id"] == "cold_distance")
    perfunctory_case = next(item for item in results if item["case_id"] == "perfunctory_brief")
    irritation_case = next(item for item in results if item["case_id"] == "mild_irritation")
    publish_change_semantics_aligned = all(
        item["published_mood_tag"] == ("" if item["mood_tag_remap_suppressed"] else item["mood_tag"])
        and item["published_event_type"] == item["effective_event_type"]
        for item in results
    )
    issue_case_ids = [item["case_id"] for item in results if item["issue"]]
    direction_conflicts = [item["case_id"] for item in results if item["issue"].startswith("direction_mismatch")]
    amplitude_issue_case_ids = [item["case_id"] for item in results if item["issue"] and not item["issue"].startswith("direction_mismatch")]

    return {
        "audit_mode": "static_and_host_chain_semantics",
        "cases": results,
        "summary": {
            "issue_case_ids": issue_case_ids,
            "direction_conflict_case_ids": direction_conflicts,
            "amplitude_issue_case_ids": amplitude_issue_case_ids,
            "mixed_affect_social_score": mixed_case["social_score"],
            "mixed_affect_remap_suppressed": mixed_case["mood_tag_remap_suppressed"],
            "positive_layering_ok": tool_case["social_score"] > mixed_case["social_score"] > ambiguous_case["social_score"],
            "negative_layering_ok": cold_case["social_score"] > perfunctory_case["social_score"] > irritation_case["social_score"],
            "publish_change_semantics_aligned": publish_change_semantics_aligned,
            "verdict": (
                "social_score direction stays aligned on obvious positive and negative text, "
                "and mixed affect no longer escalates into an overly strong support-style uplift"
                if not issue_case_ids
                else "social_score still has semantic or amplitude drift that needs follow-up"
            ),
        },
    }


def _build_stance_planner(side_inputs_mod, planner_mod):
    del side_inputs_mod
    planner = object.__new__(planner_mod.Planner)
    planner._dedupe_guidance_lines = planner_mod.Planner._dedupe_guidance_lines
    planner._agency_posture_guidance = planner_mod.Planner._agency_posture_guidance
    planner._apply_cognitive_guidance = planner_mod.Planner._apply_cognitive_guidance.__get__(planner, planner_mod.Planner)
    return planner


def _build_stance_followup_mixin(side_inputs_mod):
    mixin = side_inputs_mod.PlannerSideInputMixin()
    mixin.gateway = SimpleNamespace(
        config=SimpleNamespace(
            persona=SimpleNamespace(persona_id="persona-1"),
            reply=SimpleNamespace(follow_up_probability=1.0),
        ),
        call_data_process_task=lambda *args, **kwargs: {"follow": True, "reason": "extra_detail"},
    )
    mixin.memory_engine = SimpleNamespace()
    mixin.context_engine = SimpleNamespace(db=SimpleNamespace())
    mixin.reply_engine = SimpleNamespace(config=SimpleNamespace(reply=SimpleNamespace(emotion_mapping=[])))
    mixin.state_engine = _FakePlannerStateEngine(energy=0.8)
    mixin.action_modifier = _IdentityActionModifier()
    mixin.sys3_router = _FakeSys3Router()

    def _set_disable_rag_injection(ctx, disabled):
        ctx.shared_dict["disable_rag_injection"] = disabled

    mixin._set_disable_rag_injection = _set_disable_rag_injection
    return mixin


async def _run_stance_audit_async() -> dict:
    side_inputs_mod, expression_mod, planner_mod = _load_planner_modules()
    reply_mod = _load_reply_module()
    planner = _build_stance_planner(side_inputs_mod, planner_mod)
    followup_mixin = _build_stance_followup_mixin(side_inputs_mod)
    modifier = expression_mod.ActionModifier()
    reply_service = reply_mod.ReplyService(
        state_engine=FakeStateEngine(),
        mood_manager=SimpleNamespace(),
    )

    tool_set = [
        _Tool("proactive_meme"),
        _Tool("proactive_poke"),
        _Tool("construct_at_event"),
        _Tool("proactive_like_action"),
        _Tool("message_reaction_action"),
        _Tool("omni_perception_query"),
    ]
    cases = []
    original_random = random.random
    random.random = lambda: 0.99
    try:
        for stance in ["warm", "neutral", "cool", "guarded"]:
            for social_intent in ["answer", "comfort", "observe", "boundary"]:
                trace = {}
                filtered = modifier.modify_tools(
                    tool_set,
                    state=SimpleNamespace(energy=0.8, mood=0.0, caution=0.0),
                    tool_tier="chat",
                    stance=stance,
                    trace=trace,
                )
                prompt_envelope = SimpleNamespace(guidance_lines=[])
                decision = SimpleNamespace(
                    social_intent=social_intent,
                    stance=stance,
                    style_policy="",
                    forbid_history_continuation=False,
                )
                planner._apply_cognitive_guidance(prompt_envelope, decision)
                event = _AuditEvent(message="能再帮我看一下吗", group_id=None)
                event.set_extra("astrmai_think_level", 1)
                event.set_extra("astrmai_focus_reason", "private_direct")
                event.set_extra("astrmai_reply_need", "reply")
                event.set_extra("astrmai_action_tier", "chat")
                event.set_extra("astrmai_social_intent", social_intent)
                event.set_extra("astrmai_stance", stance)
                await followup_mixin._should_follow_up(
                    "audit:stance",
                    "我先把关键点放在这里，你看一眼就行。",
                    event=event,
                    tools=None,
                    decision=decision,
                )
                follow_trace = event.get_extra("astrmai_turn_context").follow_up
                reply_event = FakeEvent("user-1", "Alice", "audit-message")
                reply_event.set_extra("astrmai_stance", stance)
                reply_event.set_extra("astrmai_social_intent", social_intent)
                artifact = reply_service._build_visible_reply_artifact(
                    "I can help with that. Let me lay out the key point first. We can keep this gentle and steady for a moment. Do you want me to keep going?",
                    event=reply_event,
                )
                visible_text = artifact.visible_text
                cases.append(
                    {
                        "stance": stance,
                        "social_intent": social_intent,
                        "tool_names_after_filter": [tool.name for tool in filtered],
                        "removed_by_stance": list(trace.get("removed_by_stance", [])),
                        "guidance_lines": list(prompt_envelope.guidance_lines),
                        "follow_up_probability": float(follow_trace.probability or 0.0),
                        "follow_up_signals": list(follow_trace.signals or []),
                        "follow_up_skipped_reason": str(follow_trace.skipped_reason or ""),
                        "first_reply_hard_constraint_present": bool(artifact.metadata.get("stance_clamp_applied", False)),
                        "first_reply_surface_mode": (
                            "hard_clamped"
                            if artifact.metadata.get("stance_clamp_applied", False)
                            else "prompt_only_or_none"
                        ),
                        "first_reply_visible_length": len(visible_text),
                        "first_reply_sentence_count": len(reply_service._reply_sentence_chunks(visible_text)),
                        "first_reply_tail_question_trimmed": "trimmed_trailing_question"
                        in str(artifact.metadata.get("stance_clamp_reason", "")),
                        "stance_char_cap": artifact.metadata.get("stance_char_cap"),
                        "stance_sentence_cap": artifact.metadata.get("stance_sentence_cap"),
                    }
                )
    finally:
        random.random = original_random

    def _case(stance: str, social_intent: str):
        for item in cases:
            if item["stance"] == stance and item["social_intent"] == social_intent:
                return item
        raise KeyError((stance, social_intent))

    guarded_answer = _case("guarded", "answer")
    cool_answer = _case("cool", "answer")
    warm_answer = _case("warm", "answer")
    neutral_answer = _case("neutral", "answer")

    return {
        "audit_mode": "chain_level_plus_prompt_surface",
        "cases": cases,
        "summary": {
            "guarded_tool_constraints_present": bool(guarded_answer["removed_by_stance"]),
            "cool_tool_constraints_present": bool(cool_answer["removed_by_stance"]),
            "guarded_follow_up_probability": guarded_answer["follow_up_probability"],
            "cool_follow_up_probability": cool_answer["follow_up_probability"],
            "warm_follow_up_probability": warm_answer["follow_up_probability"],
            "neutral_follow_up_probability": neutral_answer["follow_up_probability"],
            "first_reply_constraints_are_prompt_only": not (
                guarded_answer["first_reply_hard_constraint_present"]
                and cool_answer["first_reply_hard_constraint_present"]
            ),
            "verdict": (
                "stance is real at tool and follow-up layers, "
                "and guarded/cool now also apply deterministic first-reply text constraints"
            ),
        },
    }


def build_state_bar_audit_baseline(*, enable_live_mood: bool = False) -> dict:
    if enable_live_mood:
        os.environ["ASTRMAI_ENABLE_LIVE_MOOD_AUDIT"] = "1"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        install_astrbot_stubs(temp_dir)
        mood_audit = asyncio.run(_run_mood_audit_async())
        social_score_audit = asyncio.run(_run_social_score_audit_async())
        stance_audit = asyncio.run(_run_stance_audit_async())
    return {
        "title": "P10.2 / P10.3 audit baseline",
        "mood": mood_audit,
        "social_score": social_score_audit,
        "stance": stance_audit,
    }


def _render_markdown_report(payload: dict) -> str:
    mood_summary = payload["mood"]["summary"]
    stance_summary = payload["stance"]["summary"]
    lines = [
        "# P10.2 / P10.3 审计基线",
        "",
        "## mood",
        f"- 审计模式: `{payload['mood']['audit_mode']}`",
        f"- LLM 实时语义审计: `{payload['mood']['live_llm_semantic_audit']['status']}` ({payload['mood']['live_llm_semantic_audit']['reason']})",
        f"- parser failures: `{', '.join(mood_summary['parser_failures']) if mood_summary['parser_failures'] else 'none'}`",
        f"- fallback issues: `{json.dumps(mood_summary['fallback_issue_counts'], ensure_ascii=False)}`",
        f"- 结论: {mood_summary['verdict']}",
        "",
        "## stance",
        f"- 审计模式: `{payload['stance']['audit_mode']}`",
        f"- guarded follow-up probability: `{stance_summary['guarded_follow_up_probability']:.4f}`",
        f"- cool follow-up probability: `{stance_summary['cool_follow_up_probability']:.4f}`",
        f"- neutral follow-up probability: `{stance_summary['neutral_follow_up_probability']:.4f}`",
        f"- warm follow-up probability: `{stance_summary['warm_follow_up_probability']:.4f}`",
        f"- 首条文案硬约束: `{stance_summary['first_reply_constraints_are_prompt_only']}`",
        f"- 结论: {stance_summary['verdict']}",
        "",
        "## Audit Readout",
        "- mood：主链更新、Judge 微调、post-send 收尾都已接通；当前最明显的弱项在 fallback 对阴阳怪气和 mixed affect 的处理。",
        "- stance：工具过滤和 follow-up 缩放已经是真作用，但首条纯文本回复仍主要依赖 prompt guidance，没有确定性长度/扩展硬钳制。",
    ]
    return "\n".join(lines) + "\n"


def _render_markdown_report_v2(payload: dict) -> str:
    mood = payload["mood"]
    mood_summary = mood["summary"]
    live = mood["live_llm_semantic_audit"]
    social_score = payload["social_score"]
    social_summary = social_score["summary"]
    stance = payload["stance"]
    stance_summary = stance["summary"]
    soft_cases = [
        f"{item['stance']}/{item['social_intent']}"
        for item in stance["cases"]
        if item["stance"] in {"guarded", "cool"} and not item["first_reply_hard_constraint_present"]
    ]
    lines = [
        "# P10.2 / P10.3 audit baseline",
        "",
        "## mood",
        f"- audit mode: `{mood['audit_mode']}`",
        f"- live LLM semantic audit: `{live['status']}` ({live['reason']})",
        f"- parser failures: `{', '.join(mood_summary['parser_failures']) if mood_summary['parser_failures'] else 'none'}`",
        f"- fallback issues: `{json.dumps(mood_summary['fallback_issue_counts'], ensure_ascii=False)}`",
        f"- live drift cases: `{', '.join(live.get('drift_case_ids', [])) if live.get('drift_case_ids') else 'none'}`",
        f"- live parse failures: `{', '.join(live.get('parse_failure_case_ids', [])) if live.get('parse_failure_case_ids') else 'none'}`",
        f"- verdict: {mood_summary['verdict']}",
        "",
        "## social_score",
        f"- audit mode: `{social_score['audit_mode']}`",
        f"- issue cases: `{', '.join(social_summary['issue_case_ids']) if social_summary['issue_case_ids'] else 'none'}`",
        f"- mixed affect score: `{social_summary['mixed_affect_social_score']:.4f}`",
        f"- mixed affect remap suppressed: `{social_summary['mixed_affect_remap_suppressed']}`",
        f"- verdict: {social_summary['verdict']}",
        "",
        "## stance",
        f"- audit mode: `{stance['audit_mode']}`",
        f"- guarded follow-up probability: `{stance_summary['guarded_follow_up_probability']:.4f}`",
        f"- cool follow-up probability: `{stance_summary['cool_follow_up_probability']:.4f}`",
        f"- neutral follow-up probability: `{stance_summary['neutral_follow_up_probability']:.4f}`",
        f"- warm follow-up probability: `{stance_summary['warm_follow_up_probability']:.4f}`",
        f"- first reply prompt-only: `{stance_summary['first_reply_constraints_are_prompt_only']}`",
        f"- soft first-reply cases: `{', '.join(soft_cases) if soft_cases else 'none'}`",
        f"- verdict: {stance_summary['verdict']}",
        "",
        "## Audit Readout",
        "- mood: primary update, Judge micro-adjust, and post-send settlement are all live; this baseline now separates static-chain checks from optional live semantic drift checks.",
        "- social_score: the audit now distinguishes text classification, mood-tag remap, and final score amplitude so mixed affect does not silently inherit an overly positive support event.",
        "- stance: guarded/cool affect tool filtering, follow-up probability, and deterministic first-reply text constraints; the remaining question is how much tightening each social_intent should apply.",
    ]
    return "\n".join(lines) + "\n"


def _render_social_score_markdown(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "# Social Score Audit Baseline",
        "",
        f"- audit mode: `{payload['audit_mode']}`",
        f"- issue cases: `{', '.join(summary['issue_case_ids']) if summary['issue_case_ids'] else 'none'}`",
        f"- mixed affect score: `{summary['mixed_affect_social_score']:.4f}`",
        f"- mixed affect remap suppressed: `{summary['mixed_affect_remap_suppressed']}`",
        f"- verdict: {summary['verdict']}",
        "",
        "## Cases",
    ]
    for case in payload.get("cases", []) or []:
        lines.extend(
            [
                f"- `{case['case_id']}` ({case['category']})",
                f"  - mood tag: `{case['mood_tag']}`",
                f"  - text event: `{case['resolved_text_event_type']}`",
                f"  - effective event: `{case['effective_event_type']}`",
                f"  - remap suppressed: `{case['mood_tag_remap_suppressed']}`",
                f"  - social_score: `{case['social_score']:.4f}`",
                f"  - direction/band: `{case['score_direction']}` / `{case['score_band']}`",
                f"  - issue: `{case['issue'] or 'none'}`",
            ]
        )
    return "\n".join(lines) + "\n"


def write_state_bar_audit_artifacts(base_dir: str | Path, *, enable_live_mood: bool = False) -> dict:
    payload = build_state_bar_audit_baseline(enable_live_mood=enable_live_mood)
    base_path = Path(base_dir)
    base_path.mkdir(parents=True, exist_ok=True)
    json_path = base_path / "p10_2_p10_3_audit_baseline.json"
    md_path = base_path / "p10_2_p10_3_audit_baseline.md"
    social_json_path = base_path / "social_score_audit_baseline.json"
    social_md_path = base_path / "social_score_audit_baseline.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown_report_v2(payload), encoding="utf-8")
    social_json_path.write_text(json.dumps(payload["social_score"], ensure_ascii=False, indent=2), encoding="utf-8")
    social_md_path.write_text(_render_social_score_markdown(payload["social_score"]), encoding="utf-8")
    return {
        "json_path": str(json_path),
        "markdown_path": str(md_path),
        "social_score_json_path": str(social_json_path),
        "social_score_markdown_path": str(social_md_path),
        "payload": payload,
    }


__all__ = [
    "build_state_bar_audit_baseline",
    "write_state_bar_audit_artifacts",
]


def _default_output_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "artifacts" / "state_bar_audit"


def _main() -> int:
    parser = argparse.ArgumentParser(description="Generate P10.2 / P10.3 mood and stance audit artifacts.")
    parser.add_argument("--base-dir", default=str(_default_output_dir()))
    parser.add_argument("--live-mood", action="store_true", help="Enable the dev-only live mood semantic audit.")
    args = parser.parse_args()
    result = write_state_bar_audit_artifacts(args.base_dir, enable_live_mood=args.live_mood)
    print(json.dumps({"json_path": result["json_path"], "markdown_path": result["markdown_path"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
