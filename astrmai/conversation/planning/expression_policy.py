from __future__ import annotations

import asyncio
import random
import re
from typing import Any, List, Optional

from astrbot.api import logger

from ...infrastructure.gateway import GlobalModelGateway
from ...infrastructure.persistence import DatabaseService, ExpressionPattern
from ...infrastructure.runtime.lane_manager import LaneKey


class ActionModifier:
    """Dynamically filter planner tools based on energy, mood, and relationship state."""

    INTIMATE_TOOLS = {
        'space_transition_action',
        'proactive_poke',
        'proactive_like_action',
    }
    SURVIVAL_TOOLS = {'wait_and_listen'}
    ALWAYS_AVAILABLE_TOOLS = {
        'wait_and_listen',
        'omni_perception_query',
        'self_lore_query',
    }
    HOSTILE_TOOLS = {
        'wait_and_listen',
        'topic_hijack_action',
        'omni_perception_query',
    }
    CHAT_LOW_ENERGY_TOOLS = {
        'message_reaction_action',
        'message_emoji_like_action',
        'proactive_like_action',
    }
    CHAT_HOSTILE_TOOLS = {
        'message_reaction_action',
        'message_emoji_like_action',
    }
    LOW_TRUST_TOOLS = {
        'proactive_poke',
        'construct_at_event',
        'space_transition_action',
        'topic_hijack_action',
        'proactive_like_action',
    }

    INTIMATE_THRESHOLD = 20
    HOSTILE_THRESHOLD = -20
    ENERGY_EXHAUSTION = 0.1

    def __init__(self, config=None):
        self.config = config
        if config and hasattr(config, 'life'):
            self.INTIMATE_THRESHOLD = getattr(config.life, 'intimate_tool_threshold', 20)
            self.HOSTILE_THRESHOLD = getattr(config.life, 'hostile_threshold', -20)
            self.ENERGY_EXHAUSTION = getattr(config.life, 'energy_exhaustion', 0.1)

    @staticmethod
    def _state_float(state, key: str, default: float) -> float:
        try:
            return float(getattr(state, key, default) if state is not None else default)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _tool_names(tools: List[Any]) -> List[str]:
        return [str(getattr(tool, 'name', '') or '').strip() for tool in tools or [] if str(getattr(tool, 'name', '') or '').strip()]

    def _trace_filter_step(self, trace, stage: str, before: List[Any], after: List[Any], reason: str, category: str = "") -> None:
        if trace is None:
            return
        before_names = self._tool_names(before)
        after_names = self._tool_names(after)
        if hasattr(trace, "record_step"):
            trace.record_step(stage, before_names, after_names, reason, category=category)
        elif isinstance(trace, dict):
            removed = [name for name in before_names if name not in set(after_names)]
            trace.setdefault("filter_steps", []).append(
                {
                    "stage": stage,
                    "reason": reason,
                    "category": category,
                    "before": before_names,
                    "after": after_names,
                    "removed": removed,
                }
            )
            trace.setdefault("filter_reasons", []).append(reason)
            bucket_name = {
                "energy": "removed_by_energy",
                "mood": "removed_by_mood",
                "hostility": "removed_by_hostility",
                "cooldown": "removed_by_cooldown",
                "caution": "removed_by_caution",
                "social_intent": "removed_by_social_intent",
                "stance": "removed_by_stance",
            }.get(str(category or ""))
            if bucket_name:
                bucket = trace.setdefault(bucket_name, [])
                for name in removed:
                    if name not in bucket:
                        bucket.append(name)

    def modify_tools(
        self,
        tools: List[Any],
        state=None,
        profile=None,
        relationship_vec=None,
        tool_tier: str = "full",
        social_intent: str = "",
        stance: str = "",
        cooldown_tags: List[str] | None = None,
        trace=None,
        return_trace: bool = False,
    ) -> List[Any]:
        if trace is None and return_trace:
            from ..contracts.turn_context import ToolDecisionTrace

            trace = ToolDecisionTrace()
        if not tools:
            return (tools, trace) if return_trace else tools

        filtered = list(tools)
        reasons: List[str] = []
        normalized_tier = str(tool_tier or "full").lower()
        normalized_intent = str(social_intent or "").lower()
        normalized_stance = str(stance or "").lower()
        cooldown_set = {str(tag or "").strip() for tag in (cooldown_tags or []) if str(tag or "").strip()}
        if trace is not None:
            if hasattr(trace, "social_intent"):
                trace.social_intent = str(social_intent or "")
                trace.final_tier = str(tool_tier or "")
            elif isinstance(trace, dict):
                trace["social_intent"] = str(social_intent or "")
                trace["final_tier"] = str(tool_tier or "")

        score = 0
        if relationship_vec:
            score = relationship_vec.social_score
        elif profile:
            score = getattr(profile, 'social_score', 0)

        patience = self._state_float(state, "patience", 0.6)
        curiosity = self._state_float(state, "curiosity", 0.4)
        caution = self._state_float(state, "caution", 0.4)

        if normalized_tier == "chat":
            if state and hasattr(state, 'energy') and state.energy < self.ENERGY_EXHAUSTION:
                before = list(filtered)
                filtered = [tool for tool in filtered if getattr(tool, 'name', '') in self.CHAT_LOW_ENERGY_TOOLS]
                reason = f'energy_exhausted({state.energy:.2f})'
                reasons.append(reason)
                self._trace_filter_step(trace, "action_modifier.energy", before, filtered, reason, "energy")
            elif profile or relationship_vec:
                if score < self.HOSTILE_THRESHOLD:
                    before = list(filtered)
                    filtered = [tool for tool in filtered if getattr(tool, 'name', '') in self.CHAT_HOSTILE_TOOLS]
                    reason = f'hostile({score:.0f})'
                    reasons.append(reason)
                    self._trace_filter_step(trace, "action_modifier.relationship", before, filtered, reason, "hostility")
                elif score < self.INTIMATE_THRESHOLD:
                    before = list(filtered)
                    filtered = [tool for tool in filtered if getattr(tool, 'name', '') not in self.INTIMATE_TOOLS]
                    reason = f'low_affection({score:.0f})'
                    reasons.append(reason)
                    self._trace_filter_step(trace, "action_modifier.relationship", before, filtered, reason, "hostility")

                if relationship_vec and getattr(relationship_vec, 'trust', 0.0) < -10:
                    before = list(filtered)
                    filtered = [tool for tool in filtered if getattr(tool, 'name', '') not in self.LOW_TRUST_TOOLS]
                    reason = f'low_trust({relationship_vec.trust:.0f})'
                    reasons.append(reason)
                    self._trace_filter_step(trace, "action_modifier.trust", before, filtered, reason, "hostility")
        else:
            if state and hasattr(state, 'energy') and state.energy < self.ENERGY_EXHAUSTION:
                before = list(filtered)
                filtered = [tool for tool in filtered if getattr(tool, 'name', '') in self.ALWAYS_AVAILABLE_TOOLS]
                reason = f'energy_exhausted({state.energy:.2f})'
                reasons.append(reason)
                self._trace_filter_step(trace, "action_modifier.energy", before, filtered, reason, "energy")
            elif profile or relationship_vec:
                if score < self.HOSTILE_THRESHOLD:
                    hostile_tools = self.HOSTILE_TOOLS | self.ALWAYS_AVAILABLE_TOOLS
                    before = list(filtered)
                    filtered = [tool for tool in filtered if getattr(tool, 'name', '') in hostile_tools]
                    reason = f'hostile({score:.0f})'
                    reasons.append(reason)
                    self._trace_filter_step(trace, "action_modifier.relationship", before, filtered, reason, "hostility")
                elif score < self.INTIMATE_THRESHOLD:
                    before = list(filtered)
                    filtered = [tool for tool in filtered if getattr(tool, 'name', '') not in self.INTIMATE_TOOLS]
                    reason = f'low_affection({score:.0f})'
                    reasons.append(reason)
                    self._trace_filter_step(trace, "action_modifier.relationship", before, filtered, reason, "hostility")

                if relationship_vec and getattr(relationship_vec, 'trust', 0.0) < -10:
                    before = list(filtered)
                    filtered = [tool for tool in filtered if getattr(tool, 'name', '') not in self.LOW_TRUST_TOOLS]
                    reason = f'low_trust({relationship_vec.trust:.0f})'
                    reasons.append(reason)
                    self._trace_filter_step(trace, "action_modifier.trust", before, filtered, reason, "hostility")

        if state and hasattr(state, 'mood') and state.mood < -0.7:
            entertainment_tools = {'meme_resonance_action', 'proactive_meme'}
            before = list(filtered)
            filtered = [tool for tool in filtered if getattr(tool, 'name', '') not in entertainment_tools]
            reason = f'low_mood({state.mood:.2f})'
            reasons.append(reason)
            self._trace_filter_step(trace, "action_modifier.mood", before, filtered, reason, "mood")

        if patience < 0.25 or normalized_intent in {"boundary", "observe", "ignore"}:
            calm_tools = {'wait_and_listen', 'message_reaction_action', 'message_emoji_like_action'}
            before = list(filtered)
            filtered = [tool for tool in filtered if getattr(tool, 'name', '') in calm_tools]
            reason = f'low_patience_or_boundary({patience:.2f})'
            reasons.append(reason)
            self._trace_filter_step(trace, "action_modifier.patience", before, filtered, reason, "social_intent")

        if caution > 0.7:
            intrusive_tools = {'proactive_poke', 'construct_at_event', 'space_transition_action'}
            before = list(filtered)
            filtered = [tool for tool in filtered if getattr(tool, 'name', '') not in intrusive_tools]
            reason = f'caution({caution:.2f})'
            reasons.append(reason)
            self._trace_filter_step(trace, "action_modifier.caution", before, filtered, reason, "caution")

        if normalized_intent in {"inquire", "recall"} and curiosity <= 0.7:
            before = list(filtered)
            filtered = [
                tool for tool in filtered
                if getattr(tool, 'name', '') not in {'topic_hijack_action', 'space_transition_action', 'regret_and_withdraw_action'}
            ]
            reason = f'query_intent_guard(curiosity={curiosity:.2f})'
            reasons.append(reason)
            self._trace_filter_step(trace, "action_modifier.query_guard", before, filtered, reason, "social_intent")

        if normalized_stance in {"guarded", "cool"}:
            stance_blocked_tools = {
                'proactive_meme',
                'meme_resonance_action',
                'proactive_poke',
                'construct_at_event',
                'proactive_like_action',
                'space_transition_action',
                'topic_hijack_action',
            }
            before = list(filtered)
            filtered = [tool for tool in filtered if getattr(tool, 'name', '') not in stance_blocked_tools]
            reason = f'stance_{normalized_stance}_guard'
            reasons.append(reason)
            self._trace_filter_step(trace, "action_modifier.stance", before, filtered, reason, "stance")

        cooldown_tools = {
            'meme': {'proactive_meme', 'meme_resonance_action'},
            'poke': {'proactive_poke'},
            'at': {'construct_at_event'},
            'like': {'proactive_like_action'},
            'sharp_reply': {'proactive_meme', 'proactive_poke', 'construct_at_event', 'proactive_like_action'},
            'long_reply': {'topic_hijack_action', 'space_transition_action', 'meme_resonance_action'},
        }
        blocked_by_cooldown = set()
        for tag in cooldown_set:
            blocked_by_cooldown.update(cooldown_tools.get(tag, set()))
        if blocked_by_cooldown:
            before = list(filtered)
            filtered = [tool for tool in filtered if getattr(tool, 'name', '') not in blocked_by_cooldown]
            reason = f'cooldown({",".join(sorted(cooldown_set))})'
            reasons.append(reason)
            self._trace_filter_step(trace, "action_modifier.cooldown", before, filtered, reason, "cooldown")

        if trace is not None:
            filtered_names = self._tool_names(filtered)
            if hasattr(trace, "filtered_tools"):
                trace.filtered_tools = filtered_names
                trace.filter_reasons = list(dict.fromkeys([*getattr(trace, "filter_reasons", []), *reasons]))
            elif isinstance(trace, dict):
                trace["filtered_tools"] = filtered_names
                trace["filter_reasons"] = list(dict.fromkeys([*(trace.get("filter_reasons", []) or []), *reasons]))

        if reasons:
            logger.info(
                f"[ActionModifier] toolset adjusted {len(tools)} -> {len(filtered)} "
                f"(reasons: {', '.join(reasons)})"
            )
        return (filtered, trace) if return_trace else filtered

    def get_filtered_tool_names(self, tools: List[Any], state=None, profile=None, relationship_vec=None, tool_tier: str = "full") -> List[str]:
        filtered = self.modify_tools(tools, state=state, profile=profile, relationship_vec=relationship_vec, tool_tier=tool_tier)
        return [getattr(tool, 'name', 'unknown') for tool in filtered]


class ExpressionSelector:
    FAST_SELECT_LIMIT = 5
    DEEP_CANDIDATE_LIMIT = 10
    RECENT_PATTERN_WINDOW = 6
    EXPRESSION_SYSTEM_PROMPT = '你是表达风格匹配器，需要从候选表达中挑选出当前语境最自然、最贴切的几条。'

    def __init__(self, db: DatabaseService, gateway: GlobalModelGateway, config=None, pattern_service=None):
        self.db = db
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.pattern_service = pattern_service
        self._recent_pattern_keys: dict[str, List[tuple[str, str]]] = {}

    async def select(
        self,
        chat_id: str,
        context_text: str = '',
        think_level: int = 0,
        shared_scope: Optional[str] = None,
    ) -> str:
        text, _selected = await self.select_with_trace(
            chat_id,
            context_text=context_text,
            think_level=think_level,
            shared_scope=shared_scope,
        )
        return text

    async def select_with_trace(
        self,
        chat_id: str,
        context_text: str = '',
        think_level: int = 0,
        shared_scope: Optional[str] = None,
    ) -> tuple[str, list[Any]]:
        try:
            if think_level <= 0:
                return await self._fast_select(chat_id, context_text=context_text, shared_scope=shared_scope, think_level=think_level)
            return await self._deep_select(
                chat_id,
                context_text,
                shared_scope=shared_scope,
                think_level=think_level,
            )
        except Exception as exc:
            logger.warning(f'[ExpressionSelector] selection failed: {exc}')
            return '', []

    def _scope_key(self, chat_id: str, shared_scope: Optional[str]) -> str:
        return str(shared_scope or chat_id or "global")

    @staticmethod
    def _pattern_key(pattern: ExpressionPattern) -> tuple[str, str]:
        return (str(pattern.situation or "").strip(), str(pattern.expression or "").strip())

    @staticmethod
    def _is_recent_short_repeat(pattern: ExpressionPattern, context_text: str) -> bool:
        expression = re.sub(r"\s+", "", str(pattern.expression or ""))
        if not expression or len(expression) > 12:
            return False
        compact_context = re.sub(r"\s+", "", str(context_text or ""))
        return expression in compact_context

    def _apply_pattern_cooldown(
        self,
        scope_key: str,
        patterns: List[ExpressionPattern],
        context_text: str,
        limit: int,
    ) -> List[ExpressionPattern]:
        if not patterns:
            return []
        recent_keys = self._recent_pattern_keys.get(scope_key, [])
        selected: List[ExpressionPattern] = []
        seen = set()
        for pattern in patterns:
            key = self._pattern_key(pattern)
            if key in seen:
                continue
            seen.add(key)
            if key in recent_keys:
                continue
            if self._is_recent_short_repeat(pattern, context_text):
                continue
            selected.append(pattern)
            if len(selected) >= limit:
                break
        if selected:
            return selected
        ranked = sorted(
            patterns,
            key=lambda pattern: recent_keys.index(self._pattern_key(pattern)) if self._pattern_key(pattern) in recent_keys else -1,
        )
        return ranked[:1]

    def _remember_patterns(self, scope_key: str, patterns: List[ExpressionPattern]) -> None:
        if not patterns:
            return
        recent = list(self._recent_pattern_keys.get(scope_key, []))
        for pattern in patterns:
            key = self._pattern_key(pattern)
            if key in recent:
                recent.remove(key)
            recent.append(key)
        self._recent_pattern_keys[scope_key] = recent[-self.RECENT_PATTERN_WINDOW:]

    def _finalize_habits(self, scope_key: str, patterns: List[Any]) -> tuple[str, list[Any]]:
        self._remember_patterns(scope_key, patterns)
        return self._format_habits(patterns), list(patterns or [])

    async def _load_patterns(
        self,
        chat_id: str,
        *,
        limit: int,
        shared_scope: Optional[str],
        think_level: int,
        review_status: str,
    ) -> list[Any]:
        if self.pattern_service and hasattr(self.pattern_service, "list_patterns"):
            return await self.pattern_service.list_patterns(
                chat_id,
                limit=limit,
                only_checked=(review_status == "approved"),
                include_rejected=False,
                shared_scope=shared_scope,
                think_level=think_level,
                review_status=review_status,
                statuses=["active"] if review_status == "approved" else ["review_pending", "active"],
            )
        return []

    async def _fast_select(self, chat_id: str, context_text: str = '', shared_scope: Optional[str] = None, think_level: int = 0) -> tuple[str, list[Any]]:
        patterns = await self._load_patterns(
            chat_id,
            limit=20,
            shared_scope=shared_scope,
            think_level=think_level,
            review_status='approved',
        )
        if not patterns:
            return '', []
        scope_key = self._scope_key(chat_id, shared_scope)
        selected = self._apply_pattern_cooldown(scope_key, patterns, context_text, self.FAST_SELECT_LIMIT)
        return self._finalize_habits(scope_key, selected)

    async def _deep_select(
        self,
        chat_id: str,
        context_text: str,
        shared_scope: Optional[str] = None,
        think_level: int = 1,
    ) -> tuple[str, list[Any]]:
        top_patterns = await self._load_patterns(
            chat_id,
            limit=5,
            shared_scope=shared_scope,
            think_level=think_level,
            review_status='approved',
        )
        all_patterns = await self._load_patterns(
            chat_id,
            limit=50,
            shared_scope=shared_scope,
            think_level=think_level,
            review_status='approved',
        )
        if not top_patterns and not all_patterns:
            return '', []

        seen = set()
        candidates: List[ExpressionPattern] = []
        for pattern in [*top_patterns, *all_patterns]:
            key = (pattern.situation, pattern.expression)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(pattern)
            if len(candidates) >= self.DEEP_CANDIDATE_LIMIT:
                break

        scope_key = self._scope_key(chat_id, shared_scope)
        candidates = self._apply_pattern_cooldown(scope_key, candidates, context_text, self.DEEP_CANDIDATE_LIMIT)

        if len(candidates) <= 3 or not context_text:
            return self._finalize_habits(scope_key, candidates[:3])

        selected = await self._llm_pick_best(chat_id, candidates, context_text)
        return self._finalize_habits(scope_key, selected)

    async def _llm_pick_best(
        self,
        chat_id: str,
        candidates: List[ExpressionPattern],
        context_text: str,
    ) -> List[ExpressionPattern]:
        candidates_desc = '\n'.join(
            f"{index + 1}. 当[{pattern.situation}]时：{pattern.expression}"
            for index, pattern in enumerate(candidates)
        )
        prompt = (
            f'当前对话语境："{context_text[-300:]}"\n\n'
            '以下是可供参考的语言习惯候选列表：\n'
            f'{candidates_desc}\n\n'
            '请从以上候选中，选出最适合当前语境、最自然、最有个性的 3 条，'
            '只返回序号并用逗号分隔，例如：1,3,5'
        )
        try:
            result = await self.gateway.call_data_process_task(
                prompt,
                system_prompt=self.EXPRESSION_SYSTEM_PROMPT,
                is_json=False,
                lane_key=LaneKey(subsystem='sys2', task_family='expression', scope_id=chat_id),
                base_origin=chat_id,
            )
            nums = re.findall(r'\d+', str(result).strip())
            selected_indices = [int(num) - 1 for num in nums if 0 < int(num) <= len(candidates)]
            if selected_indices:
                return [candidates[index] for index in selected_indices[:3]]
        except Exception as exc:
            logger.debug(f'[ExpressionSelector] llm selection fallback: {exc}')
        return random.sample(candidates, min(3, len(candidates)))

    @staticmethod
    def _format_habits(patterns: List[ExpressionPattern]) -> str:
        if not patterns:
            return ''
        lines = ['在回复时，你可以参考以下语气/节奏，不要原句复读，也不要把它当固定台词：']
        for pattern in patterns:
            lines.append(f'类似「{pattern.situation}」的场景，可借用这种感觉：{pattern.expression}')
        return '\n'.join(lines)


__all__ = [
    'ActionModifier',
    'ExpressionSelector',
]
