from __future__ import annotations

import asyncio
import inspect
import random
import time
from typing import List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..contracts.turn_context import build_turn_trace_summary, ensure_turn_context
from ...infrastructure.runtime.trace_runtime import debug_trace, preview_text
from .agency_feedback_bridge import AgencyReflectionBridge
from .agency_runtime import AgencyRuntimeStore
from ...shared.helpers.plugin_helpers import safe_create_task
from .behavior_tuning import BehaviorTuningPolicy
from .cognitive_loop import CognitiveDecision, CognitiveLoop
from .conversation_continuity import ConversationContinuityStore
from ..execution.executor import ConcurrentExecutor
from .expression_policy import ActionModifier, ExpressionSelector
from .goal_service import GoalManager
from .planning_input_loader import PlanningInputLoader
from .planner_prompt_context import PlannerPromptContextMixin
from .planner_side_inputs import PlannerSideInputMixin
from .think_level_policy import ThinkLevelPolicy


class Planner(PlannerPromptContextMixin, PlannerSideInputMixin):
    """System 2 planner facade for prompt construction and execution orchestration."""

    FOLLOW_UP_SYSTEM_PROMPT = (
        "You are a follow-up decision judge. "
        "Only decide whether the bot should send one short follow-up message. "
        'Return strict JSON: {"follow": true/false, "reason": "reason"}.'
    )
    TOOL_HINT_LABELS = {
        "omni_perception_query": "查询记忆/画像",
        "self_lore_query": "查询自我设定",
        "wait_and_listen": "等待",
        "construct_at_event": "@某人",
        "proactive_poke": "戳一戳",
        "proactive_meme": "发表情包",
        "meme_resonance_action": "复读",
        "topic_hijack_action": "转移话题",
        "space_transition_action": "跨会话私聊消息",
        "regret_and_withdraw_action": "撤回",
        "message_reaction_action": "互动反应",
        "message_emoji_like_action": "消息表情回复",
        "proactive_like_action": "表达好感",
        "custom_face_catalog_query": "查自定义表情",
        "group_sign_action": "群签到",
    }

    def __init__(
        self,
        context,
        gateway,
        context_engine,
        reply_engine,
        memory_engine,
        evolution_manager,
        state_engine=None,
        prompt_refiner=None,
        sys3_router=None,
        runtime_coordinator=None,
        cross_session_handoff_store=None,
        cognitive_loop=None,
    ):
        self.gateway = gateway
        self.context_engine = context_engine
        self.memory_engine = memory_engine
        self.evolution_manager = evolution_manager
        self.state_engine = state_engine
        self.reply_engine = reply_engine
        self.prompt_refiner = prompt_refiner
        self.sys3_router = sys3_router
        self.context = context
        self.runtime_coordinator = runtime_coordinator
        self.cross_session_handoff_store = cross_session_handoff_store
        self.agency_runtime = AgencyRuntimeStore()
        self.agency_reflection_bridge = AgencyReflectionBridge(memory_engine)
        self.conversation_continuity = ConversationContinuityStore()
        self.heartflow_manager = None
        self.behavior_tuning = BehaviorTuningPolicy()
        self.think_level_policy = ThinkLevelPolicy()
        self.input_loader = PlanningInputLoader(self)
        self.reflector = None
        # ponytail: per-instance history but not per-chat — fidelity degrades under load.
        # Replace with dict[chat_id, list[dict]] when multi-chat contention is observed.
        self.cognitive_decision_history: list[dict] = []
        self.tool_trace_history: list[dict] = []
        self.turn_trace_history: list[dict] = []
        self.follow_up_cooldowns: dict[str, float] = {}
        self.dialogue_store = getattr(getattr(self.context_engine, "db", None), "dialogue_store", None)
        if self.dialogue_store is None:
            self.dialogue_store = getattr(getattr(self.context_engine, "db_service", None), "dialogue_store", None)
        self.context_compaction = getattr(getattr(self.context_engine, "db", None), "context_compaction", None)
        if self.context_compaction is None:
            self.context_compaction = getattr(getattr(self.context_engine, "db_service", None), "context_compaction", None)
        self.turn_trace_store = getattr(getattr(self.context_engine, "db", None), "turn_trace_store", None)
        if self.turn_trace_store is None:
            self.turn_trace_store = getattr(getattr(self.context_engine, "db_service", None), "turn_trace_store", None)
        self.raw_trace_store = getattr(getattr(self.context_engine, "db", None), "raw_trace_store", None)
        if self.raw_trace_store is None:
            self.raw_trace_store = getattr(getattr(self.context_engine, "db_service", None), "raw_trace_store", None)
        self.prefix_caching_enabled = bool(getattr(getattr(gateway.config, "conversation", None), "enable_prefix_caching", True))
        self._prefix_hash_history: dict[str, str] = {}
        self._prefix_cold_summary_history: dict[str, str] = {}
        self._provider_visible_system_hash_history: dict[str, str] = {}
        self.cognitive_loop = cognitive_loop or CognitiveLoop(
            gateway,
            memory_engine=memory_engine,
            state_engine=state_engine,
            config=gateway.config,
        )

        self.goal_manager = GoalManager(gateway, config=gateway.config)
        self.action_modifier = ActionModifier(config=gateway.config)
        self.expression_selector = ExpressionSelector(
            db=context_engine.db,
            gateway=gateway,
            config=gateway.config,
            pattern_service=getattr(memory_engine, "expression_pattern_service", None),
        )
        self.executor = ConcurrentExecutor(
            context,
            gateway,
            reply_engine,
            evolution_manager,
            config=gateway.config,
            runtime_coordinator=runtime_coordinator,
        )

    def refresh_config(self, config) -> None:
        self.prefix_caching_enabled = bool(
            getattr(getattr(config, "conversation", None), "enable_prefix_caching", True)
        )
        owned_components = (
            self.context_engine,
            self.prompt_refiner,
            self.cognitive_loop,
            self.goal_manager,
            self.action_modifier,
            self.expression_selector,
            self.executor,
        )
        for component in owned_components:
            if component is None:
                continue
            refresh = getattr(component, "refresh_config", None)
            if callable(refresh):
                refresh(config)
            else:
                component.config = config

    async def _update_turn_trace_runtime(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        *,
        prompt_envelope=None,
        reply_text: str = "",
    ) -> None:
        turn_context = ensure_turn_context(event)
        if prompt_envelope is not None:
            turn_context.prompt_envelope = prompt_envelope
        warm_summary = str(getattr(prompt_envelope, "warm_zone_summary", "") if prompt_envelope else "")
        warm_quotes = str(getattr(prompt_envelope, "warm_zone_quotes", "") if prompt_envelope else "")
        warm_transcript = str(getattr(prompt_envelope, "warm_zone_transcript", "") if prompt_envelope else "")
        if not warm_transcript:
            warm_transcript = "\n".join(part for part in (warm_summary, warm_quotes) if part).strip()
        turn_context.attention.warm_transcript_preview = warm_transcript[:180]
        turn_context.attention.warm_transcript_source = str(
            getattr(prompt_envelope, "warm_zone_transcript_source", "") if prompt_envelope else ""
        )
        turn_context.attention.warm_summary_preview = warm_summary[:180]
        turn_context.attention.warm_quotes_preview = warm_quotes[:180]
        turn_context.attention.warm_topics_preview = str(
            getattr(prompt_envelope, "warm_topics_preview", "") if prompt_envelope else ""
        )[:180]
        turn_context.attention.recent_transcript_preview = str(
            getattr(prompt_envelope, "recent_transcript", "") if prompt_envelope else ""
        )[:180]
        turn_context.attention.recent_transcript_reason = str(
            getattr(prompt_envelope, "recent_transcript_reason", "") if prompt_envelope else ""
        )
        turn_context.attention.recent_transcript_used = bool(
            str(getattr(prompt_envelope, "recent_transcript", "") if prompt_envelope else "").strip()
        )
        turn_context.attention.reply_prompt_focus_anchor = str(
            getattr(prompt_envelope, "focus_message_text", "")
            or getattr(prompt_envelope, "raw_user_text", "")
            or ""
            if prompt_envelope
            else ""
        )[:180]
        turn_context.attention.cold_summary_preview = ""
        turn_context.continuity.dialogue_store_version = "v1"
        turn_context.continuity.compaction_status = "unknown"
        turn_context.continuity.compaction_eligibility_reason = ""
        turn_context.continuity.recompact_armed = False
        turn_context.continuity.focus_tail_overlap = False
        turn_context.continuity.delta_old_segments = 0
        turn_context.continuity.delta_old_message_load = 0.0
        turn_context.continuity.delta_old_long_message_count = 0
        turn_context.continuity.cold_summary_section_counts = {}
        turn_context.continuity.message_count_since_last_compaction = 0
        turn_context.continuity.next_eval_at_count = 80
        turn_context.continuity.final_score = 0.0
        turn_context.continuity.count_score = 0.0
        turn_context.continuity.closure_score = 0.0
        turn_context.continuity.tail_activity_score = 0.0
        turn_context.continuity.topic_density_score = 0.0
        turn_context.continuity.stability_score = 0.0
        turn_context.continuity.benefit_score = 0.0
        turn_context.continuity.is_forced = False
        turn_context.continuity.is_safe_to_compact = False
        turn_context.continuity.closure_signals = []
        turn_context.continuity.tail_activity_signals = []
        turn_context.continuity.topic_density_signals = []
        turn_context.continuity.stability_signals = []
        turn_context.continuity.benefit_signals = []
        turn_context.continuity.forced_pending_message_delta = 0
        turn_context.continuity.last_safe_window_seen_at_count = 0
        turn_context.continuity.post_compaction_recovery_rounds = 0
        turn_context.continuity.evaluation_count = 0
        turn_context.continuity.current_message_count = 0
        turn_context.continuity.queued_eval_node = 0
        turn_context.continuity.pending_eval_nodes_count = 0
        turn_context.continuity.pending_eval_nodes = []
        turn_context.continuity.force_execute_on_next_safe_hook = False
        turn_context.continuity.safe_hook_block_reason = ""
        turn_context.continuity.last_hook_source = ""
        turn_context.continuity.last_safe_hook_checked_at = 0
        prefix_status = (
            self.context_engine.get_last_prefix_status(chat_id)
            if hasattr(self.context_engine, "get_last_prefix_status")
            else {
                "prefix_hash": "",
                "prefix_stable": False,
                "prefix_changed_reason": "unavailable_in_trace",
                "frozen_prefix_length": 0,
                "semi_stable_length": 0,
                "frozen_prefix_blocks": {},
                "semi_stable_blocks": {},
                "system_rules_items": [],
                "system_rules_candidate_items": [],
            }
        )
        turn_context.continuity.prefix_hash = str(prefix_status.get("prefix_hash", "") or "")
        turn_context.continuity.semantic_system_hash = str(prefix_status.get("semantic_system_hash", "") or "")
        turn_context.continuity.semantic_system_length = int(prefix_status.get("semantic_system_length", 0) or 0)
        turn_context.continuity.prefix_stable = bool(prefix_status.get("prefix_stable", False))
        turn_context.continuity.prefix_changed_reason = str(prefix_status.get("prefix_changed_reason", "") or "unavailable_in_trace")
        turn_context.continuity.frozen_prefix_length = int(prefix_status.get("frozen_prefix_length", 0) or 0)
        turn_context.continuity.semi_stable_length = int(prefix_status.get("semi_stable_length", 0) or 0)
        turn_context.continuity.frozen_prefix_blocks = dict(prefix_status.get("frozen_prefix_blocks", {}) or {})
        turn_context.continuity.semi_stable_blocks = dict(prefix_status.get("semi_stable_blocks", {}) or {})
        turn_context.continuity.system_rules_items = list(prefix_status.get("system_rules_items", []) or [])
        turn_context.continuity.system_rules_candidate_items = list(prefix_status.get("system_rules_candidate_items", []) or [])
        if self.dialogue_store is not None:
            try:
                if hasattr(self.dialogue_store, "snapshot_counts"):
                    counts = await self.dialogue_store.snapshot_counts(chat_id)
                    turn_context.continuity.dialogue_store_version = f"segments:{counts.get('segments', 0)}"
                    turn_context.continuity.compaction_status = "summary_ready" if counts.get("has_summary") else "summary_empty"
                if hasattr(self.dialogue_store, "get_cold_summary"):
                    cold_text = str(await self.dialogue_store.get_cold_summary(chat_id) or "")
                    turn_context.attention.cold_summary_preview = cold_text[:180]
                if hasattr(self.dialogue_store, "get_cold_summary_structure"):
                    cold_structure = await self.dialogue_store.get_cold_summary_structure(chat_id)
                    if cold_structure is not None and hasattr(cold_structure, "section_counts"):
                        turn_context.continuity.cold_summary_section_counts = dict(cold_structure.section_counts())
            except Exception:
                pass
        if self.context_compaction is not None and hasattr(self.context_compaction, "get_trace_status"):
            try:
                compaction_status = await self.context_compaction.get_trace_status(
                    chat_id,
                    focus_context=getattr(turn_context.attention, "focus_thread", None),
                )
                turn_context.continuity.compaction_status = str(compaction_status.get("state", "") or turn_context.continuity.compaction_status)
                turn_context.continuity.compaction_eligibility_reason = str(compaction_status.get("eligibility_reason", "") or "")
                turn_context.continuity.recompact_armed = bool(compaction_status.get("recompact_armed", False))
                turn_context.continuity.focus_tail_overlap = bool(compaction_status.get("focus_tail_overlap", False))
                turn_context.continuity.delta_old_segments = int(compaction_status.get("delta_old_segments", 0) or 0)
                turn_context.continuity.delta_old_message_load = float(compaction_status.get("delta_old_message_load", 0.0) or 0.0)
                turn_context.continuity.delta_old_long_message_count = int(compaction_status.get("delta_old_long_message_count", 0) or 0)
                turn_context.continuity.message_count_since_last_compaction = int(compaction_status.get("message_count_since_last_compaction", 0) or 0)
                turn_context.continuity.next_eval_at_count = int(compaction_status.get("next_eval_at_count", 80) or 80)
                turn_context.continuity.final_score = float(compaction_status.get("final_score", 0.0) or 0.0)
                turn_context.continuity.count_score = float(compaction_status.get("count_score", 0.0) or 0.0)
                turn_context.continuity.closure_score = float(compaction_status.get("closure_score", 0.0) or 0.0)
                turn_context.continuity.tail_activity_score = float(compaction_status.get("tail_activity_score", 0.0) or 0.0)
                turn_context.continuity.topic_density_score = float(compaction_status.get("topic_density_score", 0.0) or 0.0)
                turn_context.continuity.stability_score = float(compaction_status.get("stability_score", 0.0) or 0.0)
                turn_context.continuity.benefit_score = float(compaction_status.get("benefit_score", 0.0) or 0.0)
                turn_context.continuity.is_forced = bool(compaction_status.get("is_forced", False))
                turn_context.continuity.is_safe_to_compact = bool(compaction_status.get("is_safe_to_compact", False))
                turn_context.continuity.closure_signals = list(compaction_status.get("closure_signals", []) or [])
                turn_context.continuity.tail_activity_signals = list(compaction_status.get("tail_activity_signals", []) or [])
                turn_context.continuity.topic_density_signals = list(compaction_status.get("topic_density_signals", []) or [])
                turn_context.continuity.stability_signals = list(compaction_status.get("stability_signals", []) or [])
                turn_context.continuity.benefit_signals = list(compaction_status.get("benefit_signals", []) or [])
                turn_context.continuity.forced_pending_message_delta = int(compaction_status.get("forced_pending_message_delta", 0) or 0)
                turn_context.continuity.last_safe_window_seen_at_count = int(compaction_status.get("last_safe_window_seen_at_count", 0) or 0)
                turn_context.continuity.post_compaction_recovery_rounds = int(compaction_status.get("post_compaction_recovery_rounds", 0) or 0)
                turn_context.continuity.evaluation_count = int(compaction_status.get("evaluation_count", 0) or 0)
                turn_context.continuity.current_message_count = int(compaction_status.get("current_message_count", 0) or 0)
                turn_context.continuity.queued_eval_node = int(compaction_status.get("queued_eval_node", 0) or 0)
                turn_context.continuity.pending_eval_nodes_count = int(compaction_status.get("pending_eval_nodes_count", 0) or 0)
                turn_context.continuity.pending_eval_nodes = list(compaction_status.get("pending_eval_nodes", []) or [])
                turn_context.continuity.force_execute_on_next_safe_hook = bool(compaction_status.get("force_execute_on_next_safe_hook", False))
                turn_context.continuity.safe_hook_block_reason = str(compaction_status.get("safe_hook_block_reason", "") or "")
                turn_context.continuity.last_hook_source = str(compaction_status.get("last_hook_source", "") or "")
                turn_context.continuity.last_safe_hook_checked_at = int(compaction_status.get("last_safe_hook_checked_at", 0) or 0)
            except Exception:
                pass
        if reply_text:
            turn_context.continuity.conversation_summary = str(reply_text)[:180]
        request_trace = event.get_extra("astrmai_request_trace", {}) if hasattr(event, "get_extra") else {}
        if isinstance(request_trace, dict):
            turn_context.continuity.gateway_system_hash = str(request_trace.get("gateway_system_hash", "") or "")
            turn_context.continuity.gateway_prompt_hash = str(request_trace.get("gateway_prompt_hash", "") or "")
            turn_context.continuity.provider_visible_system_hash = str(request_trace.get("provider_visible_system_hash", "") or "")
            turn_context.continuity.provider_visible_prompt_hash = str(request_trace.get("provider_visible_prompt_hash", "") or "")
            turn_context.continuity.post_hook_system_hash = str(
                event.get_extra("astrmai_post_hook_system_hash", "") if hasattr(event, "get_extra") else ""
            ) or str(request_trace.get("post_hook_system_hash", "") or "")
            turn_context.continuity.request_session_id = str(request_trace.get("request_session_id", "") or "")
            turn_context.continuity.request_cache_control = str(request_trace.get("request_cache_control", "") or "")
            turn_context.continuity.request_provider_family = str(request_trace.get("request_provider_family", "") or "")
            turn_context.continuity.request_model_id = str(request_trace.get("request_model_id", "") or "")
            turn_context.continuity.usage_input_tokens = int(request_trace.get("usage_input_tokens", 0) or 0)
            turn_context.continuity.usage_input_cached = int(request_trace.get("usage_input_cached", 0) or 0)
            turn_context.continuity.usage_output_tokens = int(request_trace.get("usage_output_tokens", 0) or 0)
        cache_ready_reasons: list[str] = []
        if str(turn_context.continuity.request_cache_control or "").strip():
            cache_ready_reasons.append("explicit_cache_hint")
        if str(turn_context.continuity.request_session_id or "").strip():
            cache_ready_reasons.append("session_reuse")
        if bool(turn_context.continuity.prefix_stable):
            cache_ready_reasons.append("semantic_system_hash_stable")
        provider_visible_history = getattr(self, "_provider_visible_system_hash_history", None)
        if not isinstance(provider_visible_history, dict):
            provider_visible_history = {}
            self._provider_visible_system_hash_history = provider_visible_history
        current_provider_visible_hash = str(turn_context.continuity.provider_visible_system_hash or "").strip()
        previous_provider_visible_hash = str(provider_visible_history.get(chat_id, "") or "").strip()
        if current_provider_visible_hash and previous_provider_visible_hash and current_provider_visible_hash == previous_provider_visible_hash:
            cache_ready_reasons.append("provider_visible_hash_stable")
        if current_provider_visible_hash:
            provider_visible_history[chat_id] = current_provider_visible_hash
        if bool(event.get_extra("astrmai_cache_affinity_enabled", False)):
            cache_ready_reasons.append("cache_affinity_enabled")
        cache_ready_reasons = list(dict.fromkeys(cache_ready_reasons))
        turn_context.continuity.cache_ready_reasons = cache_ready_reasons
        turn_context.continuity.cache_ready = bool(cache_ready_reasons)
        turn_context.continuity.cache_hit = bool(int(turn_context.continuity.usage_input_cached or 0) > 0)
        turn_context.continuity.cache_hit_evidence_supported = bool(
            turn_context.continuity.cache_hit or event.get_extra("astrmai_cached_usage_supported", False)
        )

    @staticmethod
    def _merge_inner_monologue(existing: str, addition: str) -> str:
        existing_text = str(existing or "").strip()
        addition_text = str(addition or "").strip()
        if not addition_text:
            return existing_text
        if not existing_text:
            return addition_text
        return f"{existing_text}\n{addition_text}"

    @staticmethod
    def _dedupe_guidance_lines(guidance_lines: list[str]) -> list[str]:
        unique_lines: list[str] = []
        for line in guidance_lines:
            cleaned = str(line or "").strip()
            if cleaned and cleaned not in unique_lines:
                unique_lines.append(cleaned)
        return unique_lines

    @staticmethod
    def _resolve_cognitive_retrieve_keys(current_keys: list[str], decision: CognitiveDecision) -> list[str]:
        normalized_current = [key for key in current_keys if isinstance(key, str) and key]
        if decision.memory_policy == "none":
            return []
        if decision.retrieve_keys:
            return list(decision.retrieve_keys)
        return normalized_current

    def _apply_cognitive_guidance(self, prompt_envelope, decision: CognitiveDecision) -> None:
        if not prompt_envelope:
            return
        guidance_lines = list(getattr(prompt_envelope, "guidance_lines", []) or [])
        posture = self._agency_posture_guidance(decision)
        if posture:
            guidance_lines.append(posture)
        stance = str(getattr(decision, "stance", "") or "").strip().lower()
        if stance in {"guarded", "cool"}:
            guidance_lines.append("Keep this turn brief and avoid proactive expansion.")
        elif stance == "warm":
            guidance_lines.append("If it fits naturally, receive the emotion first and answer steadily.")
        if decision.style_policy:
            guidance_lines.append(f"表达倾向：{decision.style_policy}")
        if decision.forbid_history_continuation:
            guidance_lines.append("不要把历史当剧本续写，只接当前线索。")
        prompt_envelope.guidance_lines = self._dedupe_guidance_lines(guidance_lines)

    @staticmethod
    def _adjust_expression_habits_for_behavior(
        expression_habits: str,
        decision: CognitiveDecision | None,
        cooldown_tags,
    ) -> tuple[str, str]:
        text = str(expression_habits or "").strip()
        if not text:
            return "", ""
        social_intent = str(getattr(decision, "social_intent", "") if decision else "").strip()
        cooldown_set = {str(tag or "").strip() for tag in (cooldown_tags or []) if str(tag or "").strip()}
        if social_intent in {"boundary", "pushback", "observe"}:
            return "", ""
        if "sharp_reply" in cooldown_set:
            return text, "本轮避免尖锐或攻击性表达"
        if "long_reply" in cooldown_set:
            return text, "Keep this turn short; avoid another long reply."
        return text, ""

    @staticmethod
    def _agency_posture_guidance(decision: CognitiveDecision) -> str:
        labels = {
            "answer": "自然回应当前线索",
            "comfort": "温和安慰，先接住情绪",
            "tease": "轻微调侃，但不要过火",
            "pushback": "克制反驳，最多一句，不辱骂、不升级冲突",
            "boundary": "保持边界，可以明确不接过分内容",
            "observe": "先观察，不急着插话",
            "join": "自然加入当前聊天节奏",
            "inquire": "简短追问或确认信息",
            "recall": "结合记忆理解，但不要复述原文",
            "redirect": "轻轻转回更合适的话题",
        }
        social_intent = str(getattr(decision, "social_intent", "") or "").strip()
        label = labels.get(social_intent)
        if not label:
            return ""
        stance = str(getattr(decision, "stance", "") or "").strip()
        state_bias = str(getattr(decision, "state_bias", "") or "").strip()
        detail = f"本轮姿态：{label}"
        if stance:
            detail += f"；态度基调：{stance}"
        if state_bias:
            detail += f"；内在状态倾向：{state_bias}"
        return detail

    def _append_tool_guidance(self, prompt_envelope, tools, event: AstrMessageEvent | None = None) -> None:
        if not prompt_envelope:
            return
        if event is not None and hasattr(event, "get_extra") and event.get_extra("astrmai_tool_clarification_needed", False):
            prompt = str(event.get_extra("astrmai_tool_clarification_prompt", "") or "").strip()
            missing_slots = [
                str(item or "").strip()
                for item in event.get_extra("astrmai_tool_clarification_missing_slots", []) or []
                if str(item or "").strip()
            ]
            guidance_lines = list(getattr(prompt_envelope, "guidance_lines", []) or [])
            detail = "用户像是在请求我使用工具或执行动作，但信息还不完整。"
            if prompt:
                detail += f"这轮不要声称已经执行，先自然追问：{prompt}"
            else:
                detail += "这轮不要声称已经执行，先自然追问缺少的信息。"
            if missing_slots:
                detail += " 缺少信息：" + "、".join(missing_slots[:6]) + "。"
            guidance_lines.append(detail)
            prompt_envelope.guidance_lines = self._dedupe_guidance_lines(guidance_lines)
        if not tools:
            return
        tool_tier = "full"
        if event is not None and hasattr(event, "get_extra"):
            tool_tier = str(event.get_extra("astrmai_tool_tier", "full") or "full")
        tool_names = [str(getattr(tool, "name", "") or "").strip() for tool in tools]
        guidance_lines = list(getattr(prompt_envelope, "guidance_lines", []) or [])
        if event is not None and hasattr(event, "get_extra"):
            required_tools = [
                str(name or "").strip()
                for name in event.get_extra("astrmai_required_tools", []) or []
                if str(name or "").strip() in tool_names
            ]
            prepared_tools = {
                str(name or "").strip()
                for name in event.get_extra("astrmai_prepared_required_tools", []) or []
            }
            pending_required = [name for name in required_tools if name not in prepared_tools]
            if pending_required:
                labels = [self.TOOL_HINT_LABELS.get(name, name) for name in pending_required]
                guidance_lines.append(
                    "用户本轮明确要求执行以下能力："
                    + "、".join(labels)
                    + "。这些不是可选建议；必须各调用一次对应工具，并根据工具结果生成最终回复。"
                )
            prepared_required = [name for name in required_tools if name in prepared_tools]
            if prepared_required:
                labels = [self.TOOL_HINT_LABELS.get(name, name) for name in prepared_required]
                guidance_lines.append(
                    "系统已根据用户明确请求准备好以下动作："
                    + "、".join(labels)
                    + "。不要重复调用同一动作，也不要声称动作已经成功；请生成自然的配套回复。"
                )
        if tool_tier == "chat":
            guidance = "如果气氛合适，可以顺手发表情包、轻轻互动或点个赞；这不是必须动作，普通闲聊直接自然回复即可。"
            if any(name in {"proactive_poke", "construct_at_event"} for name in tool_names):
                guidance += "戳人或@别人只在非常自然、明确相关时使用。"
            guidance_lines.append(guidance)
            prompt_envelope.guidance_lines = self._dedupe_guidance_lines(guidance_lines)
            return

        labels: list[str] = []
        for tool_name in tool_names:
            label = self.TOOL_HINT_LABELS.get(tool_name)
            if label and label not in labels:
                labels.append(label)
        if not labels:
            return
        guidance = (
            f"本轮可用动作：{'、'.join(labels)}。只有确实合适时才使用，普通闲聊直接回复。"
            "等待只在对方明显没说完、或当前确实不该回复时使用；撤回只在用户明确要求或上一条回复确实需要撤回时使用。"
        )
        social_intent = ""
        if event is not None and hasattr(event, "get_extra"):
            social_intent = str(event.get_extra("astrmai_social_intent", "") or "")
        if social_intent in {"pushback", "boundary"}:
            guidance += "可以明确立场，但不要辱骂、不要扩大冲突。"
        guidance_lines.append(guidance)
        prompt_envelope.guidance_lines = self._dedupe_guidance_lines(guidance_lines)

    @staticmethod
    def _tool_names(tools) -> list[str]:
        names: list[str] = []
        for tool in tools or []:
            name = str(getattr(tool, "name", "") or "").strip()
            if name:
                names.append(name)
        return names

    @staticmethod
    def _executed_tool_names(event: AstrMessageEvent | None) -> list[str]:
        if event is None or not hasattr(event, "get_extra"):
            return []
        trace = event.get_extra("astrmai_tool_execution_trace", [])
        if not isinstance(trace, list):
            return []
        names: list[str] = []
        for item in trace:
            if not isinstance(item, dict) or str(item.get("status", "success")) != "success":
                continue
            name = str(item.get("tool_name", "") or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    @classmethod
    def _cooldown_tags_from_execution(cls, event, decision: CognitiveDecision | None, reply_text: str | None) -> list[str]:
        tool_names = set(cls._executed_tool_names(event))
        tags: set[str] = set()
        if {"proactive_meme", "meme_resonance_action"} & tool_names:
            tags.add("meme")
        if "proactive_poke" in tool_names:
            tags.add("poke")
        if "construct_at_event" in tool_names:
            tags.add("at")
        if "proactive_like_action" in tool_names:
            tags.add("like")
        if decision and getattr(decision, "social_intent", "") == "pushback":
            tags.add("sharp_reply")
        if reply_text and len(str(reply_text)) >= 80:
            tags.add("long_reply")
        return sorted(tags)

    def _record_agency_reflection(self, chat_id: str, reply_text: str | None, event, decision: CognitiveDecision | None) -> None:
        reply_need = getattr(decision, "reply_need", "reply") if decision else "reply"
        social_intent = getattr(decision, "social_intent", "answer") if decision else "answer"
        action_tier = getattr(decision, "action_tier", "") if decision else ""
        executed_tools = self._executed_tool_names(event)
        action_tier = action_tier or ""
        if not action_tier:
            action_tier = "tool" if executed_tools else "none"
        action_taken = "reply" if reply_text else ("tool" if executed_tools else "none")
        tags = self._cooldown_tags_from_execution(event, decision, reply_text)
        note_parts = []
        if decision and getattr(decision, "state_bias", ""):
            note_parts.append(str(decision.state_bias))
        if tags:
            note_parts.append("本轮触发冷却：" + "、".join(tags))
        self.agency_runtime.record(
            chat_id=chat_id,
            reply_need=reply_need,
            social_intent=social_intent,
            action_tier=action_tier,
            action_taken=action_taken,
            reply_preview=str(reply_text or "")[:120],
            note="；".join(note_parts),
            cooldown_tags=tags,
        )
        self._schedule_agency_feedback_flush(chat_id)

    def _schedule_agency_feedback_flush(self, chat_id: str) -> None:
        if not getattr(self, "agency_reflection_bridge", None):
            return
        try:
            safe_create_task(self.agency_reflection_bridge.maybe_flush(self.agency_runtime, chat_id))
        except RuntimeError:
            pass

    def _remember_cognitive_decision(self, chat_id: str, decision: CognitiveDecision | None, fallback_reason: str = "") -> None:
        if decision is None:
            return
        item = {
            "created_at": time.time(),
            "chat_id": chat_id,
            "action": getattr(decision, "action", ""),
            "reply_need": getattr(decision, "reply_need", ""),
            "social_intent": getattr(decision, "social_intent", ""),
            "action_tier": getattr(decision, "action_tier", ""),
            "memory_policy": getattr(decision, "memory_policy", ""),
            "retrieve_keys": list(getattr(decision, "retrieve_keys", []) or []),
            "stance": getattr(decision, "stance", ""),
            "risk_flags": list(getattr(decision, "risk_flags", []) or []),
            "attack_confidence": float(getattr(decision, "attack_confidence", 0.0) or 0.0),
            "fallback_reason": fallback_reason,
        }
        self.cognitive_decision_history = [*self.cognitive_decision_history, item][-300:]

    def _remember_tool_trace(self, chat_id: str, tools, event: AstrMessageEvent) -> None:
        tool_names = self._tool_names(tools)
        turn_context = ensure_turn_context(event)
        turn_tools = turn_context.tools
        if not turn_tools.filtered_tools:
            turn_tools.filtered_tools = list(tool_names)
        if not turn_tools.family_filtered_tools:
            turn_tools.family_filtered_tools = list(turn_tools.filtered_tools)
        if not turn_tools.available_tools:
            turn_tools.available_tools = list(tool_names)
        if not turn_tools.initial_tools:
            turn_tools.initial_tools = list(turn_tools.available_tools)
        turn_tools.final_tier = str(
            event.get_extra("astrmai_tool_tier", "none") if hasattr(event, "get_extra") else "none"
        )
        item = {
            "created_at": time.time(),
            "chat_id": chat_id,
            "tool_tier": turn_tools.final_tier,
            "social_intent": event.get_extra("astrmai_social_intent", "") if hasattr(event, "get_extra") else "",
            "tool_names": tool_names,
            "tool_count": len(tools or []),
            "requested_tier": turn_tools.requested_tier,
            "final_tier": turn_tools.final_tier,
            "explicit_tool_intent": bool(turn_tools.explicit_tool_intent),
            "allowed_families": list(turn_tools.allowed_families or []),
            "initial_tools": list(turn_tools.initial_tools or []),
            "family_filtered_tools": list(turn_tools.family_filtered_tools or []),
            "filtered_tools": list(turn_tools.filtered_tools or []),
            "filter_reasons": list(turn_tools.filter_reasons or []),
            "filter_steps": list(turn_tools.filter_steps or []),
        }
        self.tool_trace_history = [*self.tool_trace_history, item][-300:]

    async def _remember_turn_trace(
        self,
        chat_id: str,
        event: AstrMessageEvent,
        *,
        status: str,
        reply_text: str | None = None,
    ) -> None:
        turn_context = ensure_turn_context(event)
        if not turn_context.perception.chat_id:
            turn_context.perception.chat_id = str(chat_id or "")
        if not turn_context.perception.sender_id and hasattr(event, "get_sender_id"):
            turn_context.perception.sender_id = str(event.get_sender_id() or "")
        if not turn_context.perception.sender_name and hasattr(event, "get_sender_name"):
            turn_context.perception.sender_name = str(event.get_sender_name() or "")
        if not turn_context.perception.text:
            turn_context.perception.text = str(getattr(event, "message_str", "") or "")
        item = build_turn_trace_summary(
            turn_context,
            created_at=time.time(),
            status=status,
            reply_sent=(bool(event.get_extra("astrmai_reply_sent", False)) if hasattr(event, "get_extra") else False) or bool(reply_text),
            reply_preview=str(reply_text or ""),
        )
        self.turn_trace_history = [*self.turn_trace_history, item][-300:]
        if self.turn_trace_store is not None and hasattr(self.turn_trace_store, "append"):
            try:
                await self.turn_trace_store.append(item)
            except Exception:
                pass
        if self.raw_trace_store is not None and hasattr(self.raw_trace_store, "append_many"):
            try:
                await self.raw_trace_store.append_many(chat_id, self._build_raw_trace_events(chat_id, event))
            except Exception:
                pass

    def _build_raw_trace_events(self, chat_id: str, event: AstrMessageEvent) -> list[dict]:
        if not hasattr(event, "get_extra"):
            return []
        trace_id = str(event.get_extra("astrmai_trace_id", "") or "")
        trace_log = list(event.get_extra("astrmai_trace_log", []) or [])
        created_at = time.time()
        events: list[dict] = []
        for item in trace_log:
            payload = dict(item or {})
            stage = str(payload.get("stage", "") or "")
            if not stage:
                continue
            payload["chat_id"] = str(chat_id or "")
            payload["trace_id"] = trace_id or str(payload.get("trace_id", "") or "")
            payload["created_at"] = float(payload.get("created_at", created_at) or created_at)
            events.append(payload)
        return events

    async def _record_expression_pattern_usage(self, event: AstrMessageEvent, chat_id: str, reply_text: str | None) -> None:
        reflector = getattr(self, "reflector", None)
        trace = event.get_extra("astrmai_expression_pattern_trace", None) if hasattr(event, "get_extra") else None
        selected = list(getattr(trace, "items", []) or [])
        if not reflector or not selected or not str(reply_text or "").strip():
            return
        for item in selected:
            expression = str(getattr(item, "expression", "") or "").strip()
            if not expression:
                continue
            normalized_expression = "".join(expression.lower().split())
            normalized_reply = "".join(str(reply_text or "").lower().split())
            await reflector.record_usage(
                pattern_id=str(getattr(item, "id", "") or ""),
                pattern_situation=str(getattr(item, "situation", "") or ""),
                pattern_expression=expression,
                actual_reply=str(reply_text or ""),
                user_reaction="matched" if normalized_expression and normalized_expression in normalized_reply else "",
                chat_id=str(chat_id or ""),
            )

    async def _load_memory_feedback_summary(self, chat_id: str) -> str:
        if not self.memory_engine or not hasattr(self.memory_engine, "get_cognitive_feedback"):
            return ""
        try:
            signals = await self.memory_engine.get_cognitive_feedback(chat_id, limit=3)
        except Exception as exc:
            logger.debug(f"[{chat_id}] cognitive feedback lookup degraded: {exc}")
            return ""
        lines: list[str] = []
        for signal in signals or []:
            source = str(getattr(signal, "source", "") or "memory")
            summary = str(getattr(signal, "summary", "") or "").strip()
            guidance = str(getattr(signal, "guidance", "") or "").strip()
            tags = list(getattr(signal, "tags", []) or [])
            if not (summary or guidance):
                continue
            line = f"- {source}: {summary[:180]}"
            if guidance:
                line += f" Guidance: {guidance[:180]}"
            if tags:
                line += " Tags: " + ", ".join(str(tag) for tag in tags[:6])
            lines.append(line)
        if not lines:
            return ""
        return "Long-term behavior and memory feedback:\n" + "\n".join(lines)

    def _apply_heartflow_context(self, event: AstrMessageEvent, chat_id: str) -> str:
        manager = getattr(self, "heartflow_manager", None)
        if not manager or not hasattr(manager, "get_hidden_context"):
            return ""
        try:
            context_text = str(manager.get_hidden_context(chat_id) or "").strip()
        except Exception as exc:
            logger.debug(f"[{chat_id}] heartflow context lookup degraded: {exc}")
            return ""
        if not context_text:
            return ""
        event.set_extra("astrmai_heartflow_context", context_text)
        turn_context = ensure_turn_context(event)
        turn_context.continuity.heartflow_context = context_text
        state = manager.get_state(chat_id) if hasattr(manager, "get_state") else None
        if state:
            interest = float(getattr(state, "interest", 0.0) or 0.0)
            talk_willingness = float(getattr(state, "talk_willingness", 0.0) or 0.0)
            event.set_extra("astrmai_heartflow_interest", interest)
            event.set_extra("astrmai_heartflow_talk_willingness", talk_willingness)
            turn_context.continuity.heartflow_interest = interest
            turn_context.continuity.heartflow_talk_willingness = talk_willingness
        pulse = manager.get_latest_pulse(chat_id) if hasattr(manager, "get_latest_pulse") else None
        if pulse:
            pulse_type = str(getattr(pulse, "pulse_type", "") or "")
            event.set_extra("astrmai_heartflow_pulse", pulse_type)
            turn_context.continuity.heartflow_pulse = pulse_type
            turn_context.continuity.heartflow_urgency = float(getattr(pulse, "urgency", 0.0) or 0.0)
        action = manager.get_latest_action_decision(chat_id) if hasattr(manager, "get_latest_action_decision") else None
        if action:
            action_type = str(getattr(action, "action_type", "") or "")
            event.set_extra("astrmai_heartflow_action", action_type)
            turn_context.continuity.heartflow_action = action_type
        session = manager.get_session(chat_id) if hasattr(manager, "get_session") else None
        if session:
            turn_context.continuity.heartflow_talk_frequency_adjust = float(getattr(session, "talk_frequency_adjust", 0.0) or 0.0)
            turn_context.continuity.heartflow_insert_pressure = float(getattr(session, "insert_pressure", 0.0) or 0.0)
            turn_context.continuity.heartflow_reply_pressure = float(getattr(session, "reply_pressure", 0.0) or 0.0)
            turn_context.continuity.heartflow_candidate_score = float(getattr(session, "visible_candidate_score", 0.0) or 0.0)
        return context_text

    def _apply_turn_continuity_context(self, event: AstrMessageEvent, chat_id: str) -> str:
        summary = self.conversation_continuity.summary(chat_id)
        snapshot = self.conversation_continuity.snapshot(chat_id)
        turn_context = ensure_turn_context(event)
        turn_context.continuity.current_topic = str(snapshot.get("current_topic", "") or "")
        turn_context.continuity.current_goal = str(snapshot.get("current_goal", "") or "")
        turn_context.continuity.goal_status = str(snapshot.get("goal_status", "") or "")
        turn_context.continuity.continuity_weight = str(snapshot.get("continuity_weight", "") or "")
        turn_context.continuity.turn_count = int(snapshot.get("turn_count", 0) or 0)
        if summary:
            event.set_extra("astrmai_conversation_continuity_summary", summary)
            turn_context.continuity.conversation_summary = summary
        return summary

    def _record_conversation_continuity(
        self,
        chat_id: str,
        prompt_envelope,
        reply_text: str | None,
        tools,
        decision: CognitiveDecision | None,
        goal_summary: str = "",
        is_lightweight_event: bool = False,
    ) -> None:
        focus_preview = ""
        if prompt_envelope is not None:
            focus_preview = str(
                getattr(prompt_envelope, "focus_message_text", "")
                or getattr(prompt_envelope, "raw_user_text", "")
                or ""
            )
        social_intent = getattr(decision, "social_intent", "") if decision else ""
        action_tier = getattr(decision, "action_tier", "") if decision else ""
        reply_need = getattr(decision, "reply_need", "") if decision else "reply"
        if not reply_need and decision:
            reply_need = getattr(decision, "action", "") or "reply"
        action_taken = "reply" if reply_text else ("tool" if tools else "none")
        self.conversation_continuity.record(
            chat_id=chat_id,
            focus_preview=focus_preview,
            goal_summary=goal_summary,
            social_intent=social_intent,
            action_tier=action_tier,
            action_taken=action_taken,
            reply_preview=str(reply_text or "")[:120],
            reply_need=reply_need,
            lightweight_event=is_lightweight_event,
        )

    @staticmethod
    def _payload_value(payload, key: str, default=None):
        if isinstance(payload, dict):
            return payload.get(key, default)
        return getattr(payload, key, default)

    def _apply_proactive_context(self, event: AstrMessageEvent) -> None:
        if not bool(event.get_extra("astrmai_is_proactive_event", False)):
            return
        turn_context = ensure_turn_context(event)
        proactive = turn_context.proactive
        decision = event.get_extra("astrmai_proactive_dispatch_decision", None)
        proactive.is_proactive = True
        proactive.source = str(event.get_extra("astrmai_proactive_source", "") or "")
        proactive.intent_id = str(event.get_extra("astrmai_proactive_intent_id", "") or "")
        proactive.reason = str(event.get_extra("astrmai_proactive_reason", "") or "")
        proactive.guidance_preview = str(event.get_extra("astrmai_proactive_guidance", "") or "")[:240]
        proactive.dispatch_status = str(self._payload_value(decision, "status", "") or "")
        proactive.blocked_reason = str(self._payload_value(decision, "blocked_reason", "") or "")
        proactive.synthetic_event_queued = bool(self._payload_value(decision, "synthetic_event_queued", False))
        proactive.reply_sent = bool(self._payload_value(decision, "reply_sent", False))
        try:
            proactive.energy_cost = float(event.get_extra("astrmai_proactive_cost", 0.0) or 0.0)
        except (TypeError, ValueError):
            proactive.energy_cost = 0.0
        try:
            proactive.cooldown_seconds = float(event.get_extra("astrmai_proactive_cooldown", 0.0) or 0.0)
        except (TypeError, ValueError):
            proactive.cooldown_seconds = 0.0

    async def _append_dialogue_segment(self, event: AstrMessageEvent, reply_text: str) -> None:
        store = getattr(self, "dialogue_store", None)
        if store is None:
            return
        chat_id = str(event.unified_msg_origin or "")
        if not chat_id:
            return
        content = str(reply_text or "").strip()
        if not content:
            return
        try:
            await store.append_segment(
                chat_id,
                event_id=f"reply:{chat_id}:{int(time.time() * 1000)}",
                speaker_id=str(getattr(self.context, "bot_id", "") or getattr(self.gateway, "bot_id", "") or ""),
                speaker_name=str(getattr(getattr(self.gateway.config, "system1", None), "nicknames", ["Bot"])[0] if getattr(getattr(self.gateway.config, "system1", None), "nicknames", None) else "Bot"),
                content=content,
                role="assistant",
                message_kind="text",
                is_bot=True,
                timestamp=time.time(),
            )
        except Exception as exc:
            logger.debug(f"[Planner] dialogue segment append degraded: {exc}")

    async def _record_planner_dialogue_segment(self, event: AstrMessageEvent, chat_id: str, reply_text: str, *, focus_context=None) -> None:
        store = getattr(self, "dialogue_store", None)
        if store is None:
            return
        content = str(reply_text or "").strip()
        if not content:
            return
        try:
            await store.append_segment(
                chat_id,
                event_id=f"reply:{chat_id}:{int(time.time() * 1000)}",
                speaker_id=str(getattr(self.context, "bot_id", "") or getattr(self.gateway, "bot_id", "") or ""),
                speaker_name=str(getattr(getattr(self.gateway.config, "system1", None), "nicknames", ["Bot"])[0] if getattr(getattr(self.gateway.config, "system1", None), "nicknames", None) else "Bot"),
                content=content,
                role="assistant",
                message_kind="text",
                is_bot=True,
                timestamp=time.time(),
            )
            if self.context_compaction is not None:
                try:
                    safe_create_task(
                        self.context_compaction.schedule_compaction_evaluation(
                            chat_id,
                            focus_context=focus_context,
                            message_source="assistant",
                        )
                    )
                except Exception as exc:
                    logger.debug(f"[Planner] dialogue compaction degraded: {exc}")
        except Exception as exc:
            logger.debug(f"[Planner] dialogue segment append degraded: {exc}")

    async def _finalize_proactive_event(self, event: AstrMessageEvent, reply_text: str | None = None) -> None:
        if not bool(event.get_extra("astrmai_is_proactive_event", False)):
            return
        if bool(event.get_extra("astrmai_proactive_completed", False)):
            return
        event.set_extra("astrmai_proactive_completed", True)
        reply_sent = bool(event.get_extra("astrmai_reply_sent", False)) or bool(reply_text)
        reply_preview = str(reply_text or "")[:160]
        turn_context = ensure_turn_context(event)
        turn_context.proactive.reply_sent = reply_sent
        turn_context.proactive.dispatch_status = "sent" if reply_sent else "skipped"
        decision = event.get_extra("astrmai_proactive_dispatch_decision", None)
        if decision is not None:
            if isinstance(decision, dict):
                decision["reply_sent"] = reply_sent
                decision["reply_preview"] = reply_preview
                decision["status"] = "sent" if reply_sent else "skipped"
            else:
                setattr(decision, "reply_sent", reply_sent)
                setattr(decision, "reply_preview", reply_preview)
                setattr(decision, "status", "sent" if reply_sent else "skipped")
        callback = event.get_extra("astrmai_proactive_completion_callback", None)
        if callable(callback):
            try:
                result = callback(reply_sent, reply_preview)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug(f"[Proactive] completion callback degraded: {exc}")

    async def _prepare_plan_context(
        self,
        event: AstrMessageEvent,
        event_messages: List[AstrMessageEvent],
    ) -> dict[str, Any]:
        debug_trace(
            event,
            "planning.enter",
            queue_size=len(event_messages or []),
            preview=preview_text(str(event.message_str or ""), 120),
        )
        chat_id = event.unified_msg_origin
        user_id = event.get_sender_id()
        sender_name = event.get_sender_name() or "群友/用户"
        turn_context = ensure_turn_context(event)
        self._apply_proactive_context(event)

        retrieve_keys = event.get_extra("retrieve_keys", [])
        if not isinstance(retrieve_keys, list):
            retrieve_keys = []
        if event.get_extra("is_fast_mode", False) and "CORE_ONLY" not in retrieve_keys:
            retrieve_keys.append("CORE_ONLY")

        is_all_mode = "ALL" in retrieve_keys
        is_fast_mode = "CORE_ONLY" in retrieve_keys
        judge_action = event.get_extra("judge_action", "REPLY")
        turn_context.attention.judge_action = str(judge_action or "")
        is_tool_call_mode = (judge_action == "TOOL_CALL") and (self.sys3_router is not None)

        if is_all_mode and len(event_messages) > 3:
            event_messages = event_messages[-3:]

        planning_context = await self._build_planning_context(event, event_messages, chat_id)
        prompt_envelope = planning_context["prompt_envelope"]
        turn_context.prompt_envelope = prompt_envelope
        turn_context.attention.focus_thread = planning_context["focus_context"]
        turn_context.attention.is_lightweight_event = bool(planning_context.get("is_lightweight_event", False))
        if planning_context.get("is_lightweight_event", False):
            retrieve_keys = ["CORE_ONLY"]
            event.set_extra("retrieve_keys", retrieve_keys)
            event.set_extra("is_fast_mode", True)
            is_all_mode = False
            is_fast_mode = True
        turn_context.attention.retrieve_keys = list(retrieve_keys)
        turn_context.attention.is_fast_mode = bool(is_fast_mode)

        pre_budget_inputs = await self.input_loader.load_pre_budget(event, chat_id)
        reflection_summary = pre_budget_inputs.reflection_summary
        cooldown_tags = pre_budget_inputs.cooldown_tags
        think_decision = self.think_level_policy.decide(
            event=event,
            prompt_envelope=prompt_envelope,
            retrieve_keys=retrieve_keys,
            planning_context=planning_context,
            cooldown_tags=cooldown_tags,
            judge_action=str(judge_action or ""),
            is_tool_call_mode=is_tool_call_mode,
        )
        event.set_extra("astrmai_think_level", think_decision.level)
        event.set_extra("astrmai_think_reason", think_decision.reason)
        event.set_extra("astrmai_think_signals", list(think_decision.signals))
        turn_context.cognitive.think_level = int(think_decision.level)
        turn_context.cognitive.think_reason = think_decision.reason
        turn_context.cognitive.think_signals = list(think_decision.signals)
        memory_feedback_summary = await self.input_loader.load_memory_feedback(
            event,
            chat_id,
            think_decision.level,
        )
        cognitive_gate = None
        if self.cognitive_loop and hasattr(self.cognitive_loop, "gate_decision"):
            cognitive_gate = self.cognitive_loop.gate_decision(event, prompt_envelope)
            if hasattr(self.cognitive_loop, "mark_gate_decision"):
                self.cognitive_loop.mark_gate_decision(event, cognitive_gate, ran=False)
        if think_decision.level <= 0:
            if "CORE_ONLY" not in retrieve_keys:
                retrieve_keys = [*retrieve_keys, "CORE_ONLY"]
            event.set_extra("retrieve_keys", retrieve_keys)
            event.set_extra("is_fast_mode", True)
            is_all_mode = False
            is_fast_mode = True
            turn_context.attention.retrieve_keys = list(retrieve_keys)
            turn_context.attention.is_fast_mode = True
        return {
            "chat_id": chat_id,
            "user_id": user_id,
            "sender_name": sender_name,
            "turn_context": turn_context,
            "retrieve_keys": retrieve_keys,
            "is_all_mode": is_all_mode,
            "is_fast_mode": is_fast_mode,
            "judge_action": judge_action,
            "is_tool_call_mode": is_tool_call_mode,
            "planning_context": planning_context,
            "focus_context": planning_context["focus_context"],
            "prompt_envelope": prompt_envelope,
            "near_context_priority": planning_context["near_context_priority"],
            "reflection_summary": reflection_summary,
            "cooldown_tags": cooldown_tags,
            "think_decision": think_decision,
            "memory_feedback_summary": memory_feedback_summary,
            "cognitive_gate": cognitive_gate,
        }

    async def _invoke_planning_llm(
        self,
        *,
        event: AstrMessageEvent,
        chat_id: str,
        user_id: str,
        sender_name: str,
        focus_context,
        planning_context: dict[str, Any],
        prompt_envelope,
        retrieve_keys: list[str],
        think_level: int,
        reflection_summary: str,
        cooldown_tags: list[str],
        cognitive_decision: CognitiveDecision | None,
        is_all_mode: bool,
        is_fast_mode: bool,
        is_tool_call_mode: bool,
        near_context_priority: bool,
    ) -> dict[str, Any]:
        sys1_thought = event.get_extra("sys1_thought", "")
        ctx = getattr(self.context_engine, "context", None)
        side_inputs = await self.input_loader.load_prompt_inputs(
            event,
            chat_id,
            prompt_envelope,
            planning_context["window_lines"],
            think_level,
            user_id=user_id,
        )
        stable_expression_habits, situational_style_append = self._adjust_expression_habits_for_behavior(
            side_inputs.get("stable_expression_habits", ""),
            cognitive_decision,
            cooldown_tags,
        )
        side_inputs["stable_expression_habits"] = stable_expression_habits
        if situational_style_append:
            current_style_cues = str(side_inputs.get("situational_style_cues", "") or "").strip()
            side_inputs["situational_style_cues"] = "\n".join(
                part for part in [current_style_cues, situational_style_append] if part
            ).strip()
        tools = await self._build_execution_tools(
            chat_id,
            event,
            user_id,
            sender_name,
            ctx,
            is_all_mode=is_all_mode,
            is_fast_mode=is_fast_mode,
            is_tool_call_mode=is_tool_call_mode,
            tool_state=side_inputs.get("tool_state"),
        )
        self._remember_tool_trace(chat_id, tools, event)
        self._append_tool_guidance(prompt_envelope, tools, event)
        if cognitive_decision and not (is_all_mode or is_fast_mode or is_tool_call_mode):
            self._set_disable_rag_injection(ctx, cognitive_decision.memory_policy == "none")

        goals_context = side_inputs.get("goals_context", "")
        agency_context = "\n".join(
            part
            for part in [
                reflection_summary,
                self._agency_posture_guidance(cognitive_decision) if cognitive_decision else "",
            ]
            if str(part or "").strip()
        )
        planner_reasoning = side_inputs.get("planner_reasoning", "")
        system_prompt, style_variant, proactive_recall = await self.context_engine.build_prompt(
            chat_id=chat_id,
            event_messages=planning_context["context_events"],
            prompt_envelope=prompt_envelope,
            retrieve_keys=retrieve_keys,
            situational_style_cues=side_inputs.get("situational_style_cues", ""),
            sys1_thought=sys1_thought,
            goals_context=goals_context,
            stable_expression_habits=side_inputs.get("stable_expression_habits", ""),
            planner_reasoning=planner_reasoning,
            stable_jargon_explanation=side_inputs.get("stable_jargon_explanation", ""),
            near_context_priority=near_context_priority,
            agency_context=agency_context,
        )
        if think_level <= 0:
            proactive_recall = ""
        event.set_extra("astrmai_prefix_hash", self.context_engine.get_last_prefix_hash(chat_id))

        if prompt_envelope is not None:
            prompt_envelope.cognitive_drive_block = agency_context or planner_reasoning or sys1_thought or goals_context
            event.set_extra("astrmai_prompt_envelope", prompt_envelope)
        await self._apply_private_jump_context(
            ctx,
            event,
            user_id,
            prompt_envelope=prompt_envelope,
        )
        self._append_mode_instructions(
            event,
            prompt_envelope=prompt_envelope,
            is_tool_call_mode=is_tool_call_mode,
            is_all_mode=is_all_mode,
            is_fast_mode=is_fast_mode,
        )

        prefix_status = self.context_engine.get_last_prefix_status(chat_id) if hasattr(self.context_engine, "get_last_prefix_status") else {}
        final_system_prompt, final_prompt = await self.prompt_refiner.refine_prompt(
            event=event,
            system_prompt=system_prompt,
            context=ctx,
            prompt_envelope=prompt_envelope,
            style_variant=style_variant,
            proactive_recall=proactive_recall,
        )
        turn_context = ensure_turn_context(event)
        turn_context.continuity.system_prompt_length = len(final_system_prompt or "")
        turn_context.continuity.prompt_length = len(final_prompt or "")
        turn_context.continuity.frozen_prefix_length = int(prefix_status.get("frozen_prefix_length", 0) or 0)
        turn_context.continuity.semi_stable_length = int(prefix_status.get("semi_stable_length", 0) or 0)
        turn_context.continuity.dynamic_prompt_blocks = {
            "cognitive_drive": len(getattr(prompt_envelope, "cognitive_drive_block", "") or ""),
            "soft_background": int(getattr(prompt_envelope, "soft_background_rendered_chars", 0) or 0),
            "situational_context": len(getattr(prompt_envelope, "situational_context_block", "") or ""),
            "planner_runtime_instruction": len(getattr(prompt_envelope, "planner_runtime_instruction_block", "") or ""),
        }
        turn_context.continuity.dynamic_prompt_length = (
            int(turn_context.continuity.dynamic_prompt_blocks.get("cognitive_drive", 0) or 0)
            + int(turn_context.continuity.dynamic_prompt_blocks.get("soft_background", 0) or 0)
            + int(turn_context.continuity.dynamic_prompt_blocks.get("situational_context", 0) or 0)
            + int(turn_context.continuity.dynamic_prompt_blocks.get("planner_runtime_instruction", 0) or 0)
        )
        await self._update_turn_trace_runtime(
            event,
            chat_id,
            prompt_envelope=prompt_envelope,
        )

        direct_vision_urls = list(
            dict.fromkeys(
                list(focus_context.vision_bundle.direct_image_urls or [])
                or list(focus_context.vision_bundle.image_urls or [])
            )
        )
        if direct_vision_urls:
            final_prompt += "\n(Director note: the user shared photos with you; please respond with the image content in mind.)"
            logger.info(f"[{chat_id}] Scheduled direct-vision payload with {len(direct_vision_urls)} image(s) into executor.")

        reply_text = await self.executor.execute(
            event=event,
            system_prompt=final_system_prompt,
            prompt=final_prompt,
            tools=tools,
            direct_vision_urls=direct_vision_urls,
        )
        return {
            "reply_text": reply_text,
            "focus_context": focus_context,
            "tools": tools,
            "side_inputs": side_inputs,
            "planning_context": planning_context,
            "is_fast_mode": is_fast_mode,
            "is_all_mode": is_all_mode,
            "is_tool_call_mode": is_tool_call_mode,
            "final_system_prompt": final_system_prompt,
        }

    async def _parse_plan_result(
        self,
        *,
        event: AstrMessageEvent,
        chat_id: str,
        prompt_envelope,
        cognitive_decision: CognitiveDecision | None,
        llm_result: dict[str, Any],
    ) -> str:
        return await self._finalize_plan_result(
            event=event,
            chat_id=chat_id,
            reply_text=llm_result["reply_text"],
            focus_context=llm_result["focus_context"],
            prompt_envelope=prompt_envelope,
            tools=llm_result["tools"],
            cognitive_decision=cognitive_decision,
            side_inputs=llm_result["side_inputs"],
            planning_context=llm_result["planning_context"],
            is_fast_mode=llm_result["is_fast_mode"],
            is_all_mode=llm_result["is_all_mode"],
            is_tool_call_mode=llm_result["is_tool_call_mode"],
            final_system_prompt=llm_result["final_system_prompt"],
        )

    async def plan_and_execute(self, event: AstrMessageEvent, event_messages: List[AstrMessageEvent]):
        prepared = await self._prepare_plan_context(event, event_messages)
        return await self._continue_plan_execution(event, prepared)

    async def _continue_plan_execution(
        self,
        event: AstrMessageEvent,
        prepared: dict[str, Any],
    ) -> str:
        chat_id = prepared["chat_id"]
        user_id = prepared["user_id"]
        sender_name = prepared["sender_name"]
        turn_context = prepared["turn_context"]
        retrieve_keys = prepared["retrieve_keys"]
        is_all_mode = prepared["is_all_mode"]
        is_fast_mode = prepared["is_fast_mode"]
        judge_action = prepared["judge_action"]
        is_tool_call_mode = prepared["is_tool_call_mode"]
        planning_context = prepared["planning_context"]
        focus_context = prepared["focus_context"]
        prompt_envelope = prepared["prompt_envelope"]
        near_context_priority = prepared["near_context_priority"]
        reflection_summary = prepared["reflection_summary"]
        cooldown_tags = prepared["cooldown_tags"]
        think_decision = prepared["think_decision"]
        memory_feedback_summary = prepared["memory_feedback_summary"]
        cognitive_gate = prepared["cognitive_gate"]
        if think_decision.level <= 0 and "group_non_direct" in think_decision.signals:
            event.set_extra("astrmai_cognitive_action", "wait")
            event.set_extra("astrmai_reply_need", "wait")
            event.set_extra("astrmai_social_intent", "observe")
            event.set_extra("astrmai_action_tier", "none")
            event.set_extra("astrmai_risk_flags", ["group_non_direct_budget_wait", "group_ambient_short_wait"])
            turn_context.cognitive.action = "wait"
            turn_context.cognitive.reply_need = "wait"
            turn_context.cognitive.social_intent = "observe"
            turn_context.cognitive.action_tier = "none"
            turn_context.cognitive.risk_flags = ["group_non_direct_budget_wait", "group_ambient_short_wait"]
            await self._settle_no_send_relationship_event(
                event,
                chat_id,
                skipped_reason="wait",
            )
            await self._finalize_proactive_event(event, None)
            await self._remember_turn_trace(chat_id, event, status="skipped_wait")
            return ""

        cognitive_decision = None
        should_run_cognitive_loop = (
            bool(cognitive_gate and cognitive_gate.should_run)
            if cognitive_gate is not None
            else bool(
                self.cognitive_loop
                and think_decision.level > 0
                and not planning_context.get("is_lightweight_event", False)
            )
        )
        if self.cognitive_loop and should_run_cognitive_loop:
            cognitive_decision = await self.cognitive_loop.decide(
                event=event,
                prompt_envelope=prompt_envelope,
                gate=cognitive_gate,
            )
            if cognitive_decision:
                cognitive_decision = self.behavior_tuning.apply(
                    event=event,
                    decision=cognitive_decision,
                    prompt_envelope=prompt_envelope,
                    cooldown_tags=cooldown_tags,
                )
                self._remember_cognitive_decision(chat_id, cognitive_decision)
                event.set_extra("astrmai_cognitive_action", cognitive_decision.action)
                event.set_extra("astrmai_cognitive_intent", cognitive_decision.intent)
                event.set_extra("astrmai_cognitive_memory_policy", cognitive_decision.memory_policy)
                event.set_extra("astrmai_reply_need", cognitive_decision.reply_need)
                event.set_extra("astrmai_social_intent", cognitive_decision.social_intent)
                event.set_extra("astrmai_action_tier", cognitive_decision.action_tier)
                event.set_extra("astrmai_allowed_action_families", list(cognitive_decision.allowed_action_families))
                event.set_extra("astrmai_stance", cognitive_decision.stance)
                event.set_extra("astrmai_state_bias", cognitive_decision.state_bias)
                event.set_extra("astrmai_risk_flags", list(cognitive_decision.risk_flags))
                event.set_extra("astrmai_attack_confidence", cognitive_decision.attack_confidence)
                turn_context.cognitive.action = cognitive_decision.action
                turn_context.cognitive.intent = cognitive_decision.intent
                turn_context.cognitive.memory_policy = cognitive_decision.memory_policy
                turn_context.cognitive.reply_need = cognitive_decision.reply_need
                turn_context.cognitive.social_intent = cognitive_decision.social_intent
                turn_context.cognitive.action_tier = cognitive_decision.action_tier
                turn_context.cognitive.allowed_action_families = list(cognitive_decision.allowed_action_families)
                turn_context.cognitive.stance = cognitive_decision.stance
                turn_context.cognitive.state_bias = cognitive_decision.state_bias
                turn_context.cognitive.risk_flags = list(cognitive_decision.risk_flags)
                turn_context.cognitive.attack_confidence = cognitive_decision.attack_confidence
                turn_context.cognitive.inner_monologue = cognitive_decision.inner_monologue
                event.set_extra(
                    "sys1_thought",
                    self._merge_inner_monologue(event.get_extra("sys1_thought", ""), cognitive_decision.inner_monologue),
                )
                self._apply_cognitive_guidance(prompt_envelope, cognitive_decision)
                if cognitive_decision.reply_need in {"wait", "ignore"} or cognitive_decision.action in {"wait", "ignore"}:
                    logger.info(
                        f"[{chat_id}] CognitiveLoop decision={cognitive_decision.reply_need}; planner execution skipped."
                    )
                    self._record_agency_reflection(chat_id, None, event, cognitive_decision)
                    self._record_conversation_continuity(
                        chat_id,
                        prompt_envelope,
                        None,
                        None,
                        cognitive_decision,
                        is_lightweight_event=bool(planning_context.get("is_lightweight_event", False)),
                    )
                    skip_reason = (
                        cognitive_decision.reply_need
                        if cognitive_decision.reply_need in {"wait", "ignore"}
                        else cognitive_decision.action
                    )
                    settle_exc = None
                    try:
                        await self._settle_no_send_relationship_event(
                            event,
                            chat_id,
                            skipped_reason=skip_reason,
                        )
                        await self._finalize_proactive_event(event, None)
                    except Exception as exc:
                        settle_exc = exc
                        logger.debug(f"[Planner] wait/ignore settle degraded: {exc}")
                    try:
                        await self._record_expression_pattern_usage(event, chat_id, None)
                    except Exception as exc:
                        logger.debug(f"[Planner] wait/ignore expression pattern degraded: {exc}")
                    trace_status = f"skipped_{skip_reason}"
                    if settle_exc is not None:
                        trace_status += "_partial"
                    await self._remember_turn_trace(
                        chat_id,
                        event,
                        status=trace_status,
                    )
                    return ""
                if (cognitive_decision.action == "tool_call" or cognitive_decision.action_tier == "sys3") and self.sys3_router is not None:
                    judge_action = "TOOL_CALL"
                    event.set_extra("judge_action", judge_action)
                    turn_context.attention.judge_action = judge_action
                    is_tool_call_mode = True
                else:
                    judge_action = "REPLY"
                    event.set_extra("judge_action", judge_action)
                    turn_context.attention.judge_action = judge_action
                    is_tool_call_mode = False
                retrieve_keys = self._resolve_cognitive_retrieve_keys(retrieve_keys, cognitive_decision)
                event.set_extra("retrieve_keys", retrieve_keys)
                turn_context.attention.retrieve_keys = list(retrieve_keys)
                is_all_mode = False
                is_fast_mode = False

        llm_result = await self._invoke_planning_llm(
            event=event,
            chat_id=chat_id,
            user_id=user_id,
            sender_name=sender_name,
            focus_context=focus_context,
            planning_context=planning_context,
            prompt_envelope=prompt_envelope,
            retrieve_keys=retrieve_keys,
            think_level=think_decision.level,
            reflection_summary=reflection_summary,
            cooldown_tags=cooldown_tags,
            cognitive_decision=cognitive_decision,
            is_all_mode=is_all_mode,
            is_fast_mode=is_fast_mode,
            is_tool_call_mode=is_tool_call_mode,
            near_context_priority=near_context_priority,
        )
        return await self._parse_plan_result(
            event=event,
            chat_id=chat_id,
            prompt_envelope=prompt_envelope,
            cognitive_decision=cognitive_decision,
            llm_result=llm_result,
        )

    async def _finalize_plan_result(
        self,
        *,
        event: AstrMessageEvent,
        chat_id: str,
        reply_text: str | None,
        focus_context: Any,
        prompt_envelope: Any,
        tools: Any,
        cognitive_decision: Any,
        side_inputs: dict,
        planning_context: dict,
        is_fast_mode: bool,
        is_all_mode: bool,
        is_tool_call_mode: bool,
        final_system_prompt: str,
    ):
        if reply_text is None:
            execution_status = str(event.get_extra("astrmai_execution_status", "") or "")
            execution_signal = str(event.get_extra("astrmai_execution_signal", "") or "")
            if execution_signal == "wait" or execution_status == "skipped_wait":
                trace_status = "skipped_wait"
                skipped_reason = "wait"
            elif execution_status == "stale_drop":
                trace_status = "stale_drop"
                skipped_reason = "stale"
            elif execution_status:
                trace_status = execution_status
                skipped_reason = execution_status
            else:
                trace_status = "no_visible_reply"
                skipped_reason = "no_visible_reply"
            self._record_agency_reflection(chat_id, None, event, cognitive_decision)
            self._record_conversation_continuity(
                chat_id,
                prompt_envelope,
                None,
                None,
                cognitive_decision,
                goal_summary=side_inputs.get("planner_reasoning", ""),
                is_lightweight_event=bool(planning_context.get("is_lightweight_event", False)),
            )
            self._apply_turn_continuity_context(event, chat_id)
            try:
                await self._settle_no_send_relationship_event(
                    event,
                    chat_id,
                    skipped_reason=skipped_reason,
                )
            except Exception as exc:
                logger.debug(f"[Planner] executor no-send settlement degraded: {exc}")
            await self._finalize_proactive_event(event, None)
            await self._remember_turn_trace(chat_id, event, status=trace_status)
            return None

        await self._record_planner_dialogue_segment(event, chat_id, reply_text, focus_context=focus_context)
        await self._update_turn_trace_runtime(event, chat_id, prompt_envelope=prompt_envelope, reply_text=reply_text)
        await self._record_expression_pattern_usage(event, chat_id, reply_text)
        self._record_agency_reflection(chat_id, reply_text, event, cognitive_decision)
        self._record_conversation_continuity(
            chat_id,
            prompt_envelope,
            reply_text,
            tools,
            cognitive_decision,
            goal_summary=side_inputs.get("planner_reasoning", ""),
            is_lightweight_event=bool(planning_context.get("is_lightweight_event", False)),
        )
        self._apply_turn_continuity_context(event, chat_id)
        await self._finalize_proactive_event(event, reply_text)

        if reply_text and not is_fast_mode and not is_all_mode and not is_tool_call_mode:
            follow_reason = await self._should_follow_up(
                chat_id,
                reply_text,
                event=event,
                tools=tools,
                decision=cognitive_decision,
            )
            if follow_reason:
                logger.info(f"[{chat_id}] Triggered follow-up: {follow_reason}")
                follow_prompt = (
                    f"(Director note: you just said \"{reply_text[:100]}\"."
                    f" Now you want to add one more short line because: {follow_reason}."
                    " Generate one extremely short follow-up message that feels like a natural second message from a real person."
                    " Never repeat what you already said just now!)"
                )
                await asyncio.sleep(random.uniform(1.0, 3.5))
                previous_response_kind = event.get_extra("astrmai_response_kind", None)
                event.set_extra("astrmai_response_kind", "follow_up")
                try:
                    await self.executor.execute(
                        event=event,
                        system_prompt=final_system_prompt,
                        prompt=follow_prompt,
                        tools=None,
                    )
                finally:
                    event.set_extra("astrmai_response_kind", previous_response_kind)
        elif reply_text:
            blocked_modes = []
            if is_fast_mode:
                blocked_modes.append("fast_or_core")
            if is_all_mode:
                blocked_modes.append("all_mode")
            if is_tool_call_mode:
                blocked_modes.append("tool_or_sys3_mode")
            self._record_follow_up_decision(
                event,
                eligible=False,
                skipped_reason="mode_blocks_follow_up",
                signals=blocked_modes,
            )

        await self._remember_turn_trace(chat_id, event, status="executed", reply_text=reply_text)

        logger.debug(
            f"[{event.unified_msg_origin}] [planning] exit | reply_sent={bool(event.get_extra('astrmai_reply_sent', False))}"
        )
        debug_trace(
            event,
            "planning.exit",
            reply_sent=bool(event.get_extra("astrmai_reply_sent", False)),
        )
        return reply_text

__all__ = ["Planner"]
