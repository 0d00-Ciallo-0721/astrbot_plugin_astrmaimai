from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..contracts.turn_context import ensure_turn_context
from ...infrastructure.runtime.lane_manager import LaneKey
from ..contracts.prompt_envelope import PromptEnvelope
from .tools.pfc_tools import (
    ConstructAtEventTool,
    MemeResonanceTool,
    MessageReactionTool,
    OmniPerceptionTool,
    ProactiveLikeTool,
    ProactiveMemeTool,
    ProactivePokeTool,
    RegretAndWithdrawTool,
    SelfLoreQueryTool,
    SpaceTransitionTool,
    TopicHijackTool,
    WaitTool,
)


class PlannerSideInputMixin:
    FOLLOW_UP_ALLOWED_INTENTS = {"comfort", "tease", "inquire", "answer"}
    FOLLOW_UP_COOLDOWN_SECONDS = {
        "comfort": 300.0,
        "tease": 600.0,
        "inquire": 600.0,
        "answer": 600.0,
    }
    TOOL_INTENT_KEYWORDS = {
        "查一下",
        "搜一下",
        "帮我看看",
        "帮我查",
        "你还记得",
        "你记得",
        "撤回",
        "转移话题",
        "换个话题",
        "look up",
        "search",
        "check",
        "withdraw",
        "do you remember",
        "still remember",
        "change topic",
        "switch topic",
    }
    POKE_INTENT_KEYWORDS = {
        "戳一下",
        "戳一戳",
        "戳戳",
        "poke",
    }
    AT_INTENT_KEYWORDS = {
        "@",
        "艾特",
    }
    CHAT_TOOL_NAMES = {
        "proactive_meme",
        "proactive_like_action",
        "message_reaction_action",
    }
    GUARDED_CHAT_TOOL_NAMES = {
        "proactive_poke",
        "construct_at_event",
    }
    FULL_ONLY_TOOL_NAMES = {
        "wait_and_listen",
        "omni_perception_query",
        "self_lore_query",
        "topic_hijack_action",
        "space_transition_action",
        "regret_and_withdraw_action",
        "meme_resonance_action",
    }
    TOOL_NAME_ALIASES = {
        "WaitTool": "wait_and_listen",
        "OmniPerceptionTool": "omni_perception_query",
        "SelfLoreQueryTool": "self_lore_query",
        "ConstructAtEventTool": "construct_at_event",
        "ProactivePokeTool": "proactive_poke",
        "ProactiveMemeTool": "proactive_meme",
        "MemeResonanceTool": "meme_resonance_action",
        "TopicHijackTool": "topic_hijack_action",
        "SpaceTransitionTool": "space_transition_action",
        "RegretAndWithdrawTool": "regret_and_withdraw_action",
        "MessageReactionTool": "message_reaction_action",
        "ProactiveLikeTool": "proactive_like_action",
    }
    TOOL_FAMILIES = {
        "wait_and_listen": {"wait"},
        "omni_perception_query": {"query"},
        "self_lore_query": {"query"},
        "construct_at_event": {"at"},
        "proactive_poke": {"poke"},
        "proactive_meme": {"meme"},
        "meme_resonance_action": {"resonance"},
        "topic_hijack_action": {"topic"},
        "space_transition_action": {"private"},
        "regret_and_withdraw_action": {"withdraw"},
        "message_reaction_action": {"reaction"},
        "proactive_like_action": {"reaction", "like"},
    }

    @staticmethod
    def _planner_side_input_text(
        prompt_envelope: PromptEnvelope,
        window_lines: List[str],
        *,
        recent_only: bool = False,
    ) -> str:
        if isinstance(prompt_envelope, PromptEnvelope):
            sections = [
                prompt_envelope.focus_message_text,
                prompt_envelope.direct_context_text,
                prompt_envelope.related_context_text,
                prompt_envelope.ambient_background_text,
            ]
            text = "\n".join(section for section in sections if str(section or "").strip()).strip()
            if text:
                return text
        lines = window_lines[-3:] if recent_only and window_lines else window_lines
        return "\n".join(line for line in lines if str(line or "").strip())

    async def _load_planning_side_inputs(self, chat_id: str, prompt_envelope: PromptEnvelope, window_lines: List[str], is_fast_mode: bool):
        if is_fast_mode:
            return {
                "slang_context": "",
                "goal_text": "",
                "expression_habits": "",
                "jargon_explanation": "",
                "planner_reasoning": "",
            }

        async def _load_slang():
            return await asyncio.to_thread(self.evolution_manager.get_active_patterns, chat_id)

        async def _load_goals():
            window_text = self._planner_side_input_text(prompt_envelope, window_lines)
            result = await self.goal_manager.analyze_and_update(chat_id, window_text)
            logger.debug(f"[{chat_id}] 当前主目标: {result}")
            return result

        async def _load_expressions():
            recent_text = self._planner_side_input_text(prompt_envelope, window_lines, recent_only=True)
            think_level = 1 if len(recent_text) >= 40 and len(window_lines) >= 2 else 0
            return await self.expression_selector.select(
                chat_id=chat_id,
                context_text=recent_text,
                think_level=think_level,
                shared_scope=chat_id,
            )

        async def _load_jargons():
            try:
                jargon_list = (
                    await self.context_engine.db.load_jargon_list(chat_id, limit=8)
                    if hasattr(self.context_engine.db, "load_jargon_list")
                    else []
                )
                if jargon_list:
                    if all(isinstance(item, str) for item in jargon_list):
                        lines = [item for item in jargon_list if item]
                    else:
                        lines = [
                            f"{j.get('text', '')} -> {j.get('meaning', '...')} (场景: {j.get('situation', '?')})"
                            for j in jargon_list
                            if isinstance(j, dict) and j.get("meaning") and j.get("text")
                        ]
                    return "\n".join(lines) if lines else ""
            except Exception as exc:
                logger.debug(f"[Planner] 黑话加载失败: {exc}")
            return ""

        slang_context, expression_habits, jargon_explanation = await asyncio.gather(
            _load_slang(),
            _load_expressions(),
            _load_jargons(),
        )
        goal_text = await _load_goals()
        return {
            "slang_context": slang_context,
            "goal_text": goal_text,
            "expression_habits": expression_habits,
            "jargon_explanation": jargon_explanation,
            "planner_reasoning": goal_text,
        }

    def _has_tool_intent(self, event: AstrMessageEvent) -> bool:
        msg = str(getattr(event, "message_str", "") or "").strip()
        if not msg:
            return False
        lowered = msg.lower()
        return any(keyword in msg or keyword in lowered for keyword in self.TOOL_INTENT_KEYWORDS)

    def _has_poke_intent(self, message: str) -> bool:
        if not message:
            return False
        lowered = message.lower()
        return any(keyword in message or keyword in lowered for keyword in self.POKE_INTENT_KEYWORDS)

    def _has_at_intent(self, message: str) -> bool:
        if not message:
            return False
        return any(keyword in message for keyword in self.AT_INTENT_KEYWORDS)

    def _has_guarded_chat_intent(self, event: AstrMessageEvent) -> bool:
        msg = str(getattr(event, "message_str", "") or "").strip()
        return self._has_poke_intent(msg) or self._has_at_intent(msg)

    @staticmethod
    def _set_tool_tier(event: AstrMessageEvent, tier: str) -> None:
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_tool_tier", tier)
            ensure_turn_context(event).tools.final_tier = str(tier or "")
        else:
            setattr(event, "astrmai_tool_tier", tier)

    def _emotion_mapping_for_meme_tool(self) -> list:
        value = getattr(getattr(self.reply_engine.config, "reply", None), "emotion_mapping", [])
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [f"{key}: {item}" for key, item in value.items()]
        return []

    def _build_full_pfc_tools(self, chat_id: str, user_id, sender_name: str):
        target_persona_id = getattr(self.gateway.config.persona, "persona_id", "") if hasattr(self.gateway.config, "persona") else ""
        memory_tool_service = getattr(self.memory_engine, "tool_service", None)
        return [
            WaitTool(),
            SelfLoreQueryTool(
                memory_engine=self.memory_engine,
                memory_tool_service=memory_tool_service,
                persona_id=target_persona_id,
            ),
            OmniPerceptionTool(
                memory_engine=self.memory_engine,
                memory_tool_service=memory_tool_service,
                db_service=self.context_engine.db,
                chat_id=chat_id,
                current_sender_id=str(user_id) if user_id is not None else "",
                current_sender_name=sender_name,
            ),
            ConstructAtEventTool(db_service=self.context_engine.db),
            ProactivePokeTool(db_service=self.context_engine.db),
            ProactiveMemeTool(emotion_mapping=self._emotion_mapping_for_meme_tool()),
            MemeResonanceTool(),
            TopicHijackTool(),
            SpaceTransitionTool(),
            RegretAndWithdrawTool(),
            MessageReactionTool(),
            ProactiveLikeTool(db_service=self.context_engine.db),
        ]

    def _build_chat_tools(self, event: AstrMessageEvent):
        tools = [
            ProactiveMemeTool(emotion_mapping=self._emotion_mapping_for_meme_tool()),
            MessageReactionTool(),
            ProactiveLikeTool(db_service=self.context_engine.db),
        ]
        if self._has_guarded_chat_intent(event):
            tools.extend(
                [
                    ProactivePokeTool(db_service=self.context_engine.db),
                    ConstructAtEventTool(db_service=self.context_engine.db),
                ]
            )
        return tools

    @classmethod
    def _canonical_tool_name(cls, tool: object) -> str:
        raw_name = getattr(tool, "name", "")
        if not raw_name and isinstance(tool, str):
            raw_name = tool
        name = str(raw_name or "").strip()
        return cls.TOOL_NAME_ALIASES.get(name, name)

    @staticmethod
    def _event_string_list(event: AstrMessageEvent, key: str) -> list[str]:
        value = event.get_extra(key, []) if hasattr(event, "get_extra") else []
        if isinstance(value, str):
            raw_items = re.split(r"[,，、\s]+", value)
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = []
        items: list[str] = []
        for item in raw_items:
            text = str(item or "").strip().lower()
            if text and text not in items:
                items.append(text)
        return items

    def _families_for_social_intent(self, event: AstrMessageEvent, social_intent: str) -> set[str] | None:
        if not social_intent:
            return None
        if social_intent == "comfort":
            return {"reaction", "like"}
        if social_intent == "tease":
            families = {"meme", "reaction", "like"}
            if self._has_poke_intent(str(getattr(event, "message_str", "") or "")):
                families.add("poke")
            if self._has_at_intent(str(getattr(event, "message_str", "") or "")):
                families.add("at")
            return families
        if social_intent in {"pushback", "boundary", "observe"}:
            return set()
        if social_intent in {"inquire", "recall"}:
            return {"query"}
        if social_intent == "redirect":
            return {"topic"}
        return None

    def _filter_tools_by_families(self, tools: list, allowed_families: set[str] | None) -> list:
        if allowed_families is None:
            return tools
        if not allowed_families:
            return []
        filtered = []
        for tool in tools:
            tool_name = self._canonical_tool_name(tool)
            families = self.TOOL_FAMILIES.get(tool_name, set())
            if families & allowed_families:
                filtered.append(tool)
        return filtered

    async def _build_execution_tools(
        self,
        chat_id: str,
        event: AstrMessageEvent,
        user_id,
        sender_name: str,
        ctx,
        *,
        is_all_mode: bool,
        is_fast_mode: bool,
        is_tool_call_mode: bool,
        tool_state=None,
    ):
        if is_tool_call_mode:
            sys3_light_tools = (await self.sys3_router.get_light_tools_for_planner()).tools
            target_persona_id = getattr(self.gateway.config.persona, "persona_id", "") if hasattr(self.gateway.config, "persona") else ""
            memory_tool_service = getattr(self.memory_engine, "tool_service", None)
            self._set_disable_rag_injection(ctx, True)
            self._set_tool_tier(event, "sys3")
            tools = [
                WaitTool(),
                OmniPerceptionTool(
                    memory_engine=self.memory_engine,
                    memory_tool_service=memory_tool_service,
                    db_service=self.context_engine.db,
                    chat_id=chat_id,
                    current_sender_id=str(user_id) if user_id is not None else "",
                    current_sender_name=sender_name,
                ),
                SelfLoreQueryTool(
                    memory_engine=self.memory_engine,
                    memory_tool_service=memory_tool_service,
                    persona_id=target_persona_id,
                ),
            ] + sys3_light_tools
            turn_tools = ensure_turn_context(event).tools
            tool_names = [
                self._canonical_tool_name(tool)
                for tool in tools or []
                if self._canonical_tool_name(tool)
            ]
            turn_tools.requested_tier = "sys3"
            turn_tools.final_tier = "sys3"
            turn_tools.explicit_tool_intent = True
            turn_tools.initial_tools = list(tool_names)
            turn_tools.available_tools = list(tool_names)
            turn_tools.family_filtered_tools = list(tool_names)
            turn_tools.filtered_tools = list(tool_names)
            logger.info(f"[{chat_id}] [TOOL_CALL 模式] 加载 Sys3 SubAgent 索引，工具总数: {len(tools)}")
            return tools

        if is_all_mode or is_fast_mode:
            self._set_disable_rag_injection(ctx, True)
        else:
            self._set_disable_rag_injection(ctx, False)

        explicit_tool_intent = self._has_tool_intent(event)
        requested_tier = str(event.get_extra("astrmai_action_tier", "") if hasattr(event, "get_extra") else "").strip().lower()
        social_intent = str(event.get_extra("astrmai_social_intent", "") if hasattr(event, "get_extra") else "").strip().lower()
        think_level = None
        if hasattr(event, "get_extra"):
            try:
                think_level = int(event.get_extra("astrmai_think_level", None))
            except (TypeError, ValueError):
                think_level = None
        turn_tools = ensure_turn_context(event).tools
        turn_tools.requested_tier = requested_tier
        turn_tools.explicit_tool_intent = bool(explicit_tool_intent)
        turn_tools.social_intent = social_intent
        turn_tools.filter_steps = []
        turn_tools.filter_reasons = []
        turn_tools.removed_by_energy = []
        turn_tools.removed_by_mood = []
        turn_tools.removed_by_hostility = []
        turn_tools.removed_by_cooldown = []
        turn_tools.removed_by_caution = []
        turn_tools.removed_by_social_intent = []
        if bool(event.get_extra("astrmai_is_proactive_event", False)):
            if requested_tier in {"full", "sys3"} or explicit_tool_intent:
                turn_tools.record_step(
                    "planner.proactive_tool_guard",
                    [requested_tier or "tool_intent"],
                    ["chat"],
                    "proactive_event_blocks_heavy_tools",
                    category="social_intent",
                )
            explicit_tool_intent = False
            turn_tools.explicit_tool_intent = False
            if requested_tier != "none":
                requested_tier = "chat"
                turn_tools.requested_tier = "chat"
        allowed_families = set(self._event_string_list(event, "astrmai_allowed_action_families"))
        intent_families = self._families_for_social_intent(event, social_intent)
        if intent_families is not None:
            allowed_families = (allowed_families & intent_families) if allowed_families else intent_families
        turn_tools.allowed_families = sorted(allowed_families if allowed_families else (intent_families or set()))

        state = None
        profile = None
        relationship_vec = None
        if tool_state is not None:
            if isinstance(tool_state, dict):
                state = tool_state.get("state")
                profile = tool_state.get("profile")
                relationship_vec = tool_state.get("relationship_vec")
            else:
                state = getattr(tool_state, "state", None)
                profile = getattr(tool_state, "profile", None)
                relationship_vec = getattr(tool_state, "relationship_vec", None)
        elif self.state_engine:
            try:
                state = await self.state_engine.get_state(chat_id)
            except Exception:
                pass
            if user_id:
                try:
                    profile = await self.state_engine.get_user_profile(str(user_id))
                except Exception:
                    pass
                if hasattr(self.state_engine, "relationship_engine"):
                    relationship_vec = self.state_engine.relationship_engine.get_or_create(str(user_id))

        if social_intent in {"pushback", "boundary", "observe"} and requested_tier not in {"full", "sys3"}:
            turn_tools.record_step(
                "planner.tier_guard",
                [],
                [],
                f"social_intent({social_intent})_forces_none",
                category="social_intent",
            )
            requested_tier = "none"
        if think_level is not None and think_level < 3 and requested_tier == "full" and not explicit_tool_intent:
            reason = f"think_level_{think_level}_prevents_full_tier"
            turn_tools.record_step(
                "planner.think_level_guard",
                ["full_tier"],
                ["chat_tier"],
                reason,
            )
            requested_tier = "chat"
        if requested_tier == "full" and not explicit_tool_intent:
            try:
                energy = float(getattr(state, "energy", 0.6) if state is not None else 0.6)
            except (TypeError, ValueError):
                energy = 0.6
            if energy < 0.25:
                reason = f"low_energy_tier_downgrade({energy:.2f})"
                turn_tools.record_step(
                    "planner.tier_state_guard",
                    ["full_tier"],
                    ["chat_tier"],
                    reason,
                    category="energy",
                )
                requested_tier = "chat"

        if requested_tier == "none":
            self._set_tool_tier(event, "none")
            tools = []
            turn_tools.record_step(
                "planner.tier_select",
                [],
                [],
                "requested_tier_none",
                category="social_intent" if social_intent in {"pushback", "boundary", "observe"} else "",
            )
        elif requested_tier == "full" or explicit_tool_intent:
            self._set_tool_tier(event, "full")
            tools = self._build_full_pfc_tools(chat_id, user_id, sender_name)
        else:
            self._set_tool_tier(event, "chat")
            tools = self._build_chat_tools(event)

        built_tool_names = [
            self._canonical_tool_name(tool)
            for tool in tools or []
            if self._canonical_tool_name(tool)
        ]
        turn_tools.initial_tools = list(built_tool_names)
        turn_tools.available_tools = list(built_tool_names)

        if tools:
            before_family_names = list(built_tool_names)
            tools = self._filter_tools_by_families(tools, allowed_families if allowed_families else intent_families)
            after_family_names = [
                self._canonical_tool_name(tool)
                for tool in tools or []
                if self._canonical_tool_name(tool)
            ]
            turn_tools.family_filtered_tools = list(after_family_names)
            if before_family_names != after_family_names:
                reason = "allowed_families(" + ",".join(turn_tools.allowed_families) + ")"
                turn_tools.record_step(
                    "planner.family_filter",
                    before_family_names,
                    after_family_names,
                    reason,
                    category="social_intent",
                )
        else:
            turn_tools.family_filtered_tools = []

        tools = self.action_modifier.modify_tools(
            tools,
            state=state,
            profile=profile,
            relationship_vec=relationship_vec,
            tool_tier=event.get_extra("astrmai_tool_tier", "full") if hasattr(event, "get_extra") else getattr(event, "astrmai_tool_tier", "full"),
            social_intent=social_intent,
            cooldown_tags=event.get_extra("astrmai_agency_cooldown_tags", []) if hasattr(event, "get_extra") else [],
            trace=turn_tools,
        )
        turn_tools.filtered_tools = [
            self._canonical_tool_name(tool)
            for tool in tools or []
            if self._canonical_tool_name(tool)
        ]
        return tools

    async def _apply_private_jump_context(self, system_prompt: str, ctx, event: AstrMessageEvent, user_id) -> str:
        if event.get_group_id() or not ctx:
            return system_prompt
        shared_dict = getattr(ctx, "shared_dict", {})
        jumps = shared_dict.get("astrmai_space_jumps", {})
        sender_id = str(user_id)
        if sender_id not in jumps:
            return system_prompt

        jump_info = jumps[sender_id]
        try:
            if time.time() - jump_info["timestamp"] < 600:
                source_group_id = jump_info.get("group_id")
                group_context_str = ""
                if source_group_id:
                    try:
                        conv_mgr = ctx.conversation_manager
                        uid = f"default:GroupMessage:{source_group_id}"
                        curr_cid = await conv_mgr.get_curr_conversation_id(uid)
                        conversation = await conv_mgr.get_conversation(uid, curr_cid)
                        history = json.loads(conversation.history) if conversation and conversation.history else []
                        recent_msgs = []
                        for msg in history[-5:]:
                            role = msg.get("role", "")
                            text_parts = [
                                item.get("text", "")
                                for item in (msg.get("content") or [])
                                if isinstance(item, dict) and item.get("type") == "text"
                            ]
                            content = " ".join(text_parts) if text_parts else ""
                            if content:
                                speaker = "群友" if role == "user" else "你"
                                recent_msgs.append(f"[{speaker}]: {content}")
                        if recent_msgs:
                            group_context_str = "\n".join(recent_msgs)
                    except Exception as exc:
                        logger.error(f"[Planner] 溯源群聊历史失败: {exc}")

                sys_inject = (
                    "\n\n刚才我还在群聊"
                    + (f" (群号:{source_group_id})" if source_group_id else "")
                    + "里和大家说话，随后又主动私下对 ta 说了一句：\n"
                    f"【我刚才的悄悄话】：{jump_info['private_message']}\n"
                )
                if group_context_str:
                    sys_inject += f"\n【我切出来前群里的话题回顾】：\n{group_context_str}\n"
                sys_inject += (
                    "\n对方现在这句，多半就是接着我刚才那次跨界私聊在回我。"
                    "我得把群里的前置话题和这句悄悄话一起接住，顺着私下交流的自然感继续聊下去。"
                )
                system_prompt += sys_inject
                logger.info(f"[Planner] 已触发跨界语境补偿，成功抓取群聊历史并注入到 {sender_id} 的私聊思考中。")
        finally:
            del jumps[sender_id]
        return system_prompt

    def _append_mode_instructions(self, system_prompt: str, event: AstrMessageEvent, *, is_tool_call_mode: bool, is_all_mode: bool, is_fast_mode: bool) -> str:
        if is_tool_call_mode:
            system_prompt += (
                "\n\n对方这次是在让我帮忙办事。"
                "我先看看手边有哪些对应的子智能体工具能真去执行，"
                "等拿到结果后，再用我自己的语气告诉 ta。"
            )
        if is_all_mode:
            user_message = event.message_str
            system_prompt += f'\n\n对方刚才说的是：“{user_message}”。我这轮就先接住这一条来回。'
        if is_fast_mode:
            system_prompt += "\n\n有人在喊我，我得马上用简短直接的话接住这次呼唤，不绕远路。"
        return system_prompt

    async def _should_follow_up_legacy(
        self,
        chat_id: str,
        last_reply: str,
        *,
        event: AstrMessageEvent | None = None,
        tools=None,
        decision=None,
    ) -> Optional[str]:
        if event is not None and hasattr(event, "get_extra"):
            try:
                if int(event.get_extra("astrmai_think_level", 1) or 0) < 1:
                    return None
            except (TypeError, ValueError):
                pass
            if event.get_extra("astrmai_lightweight_event", False):
                return None
            if event.get_extra("astrmai_agency_cooldown_tags", []):
                return None
            focus_reason = str(event.get_extra("astrmai_focus_reason", "") or "").lower()
            is_direct_focus = any(token in focus_reason for token in ("at", "reply", "direct", "wakeup", "private", "name"))
            group_getter = getattr(event, "get_group_id", None)
            if callable(group_getter) and group_getter() and not is_direct_focus:
                return None
        if tools:
            return None
        social_intent = str(getattr(decision, "social_intent", "") if decision else "").strip()
        if social_intent in {"boundary", "pushback", "observe"}:
            return None

        if self.state_engine:
            state = await self.state_engine.get_state(chat_id)
            if state and state.energy < 0.3:
                return None

        clean_reply = last_reply.strip()
        if len(clean_reply) < 15:
            return None
        if clean_reply.endswith("？") or clean_reply.endswith("?"):
            return None

        reply_cfg = getattr(self.gateway.config, "reply", None)
        follow_up_probability = getattr(reply_cfg, "follow_up_probability", 0.20)
        try:
            follow_up_probability = float(follow_up_probability)
        except (TypeError, ValueError):
            follow_up_probability = 0.20
        follow_up_probability = max(0.0, min(1.0, follow_up_probability))
        if follow_up_probability <= 0.0 or random.random() > follow_up_probability:
            return None

        prompt = (
            f'你刚回复:"{clean_reply[:100]}"\n'
            "需要紧接着追发第二句吗？(补充/追问/表情/吐槽)\n"
            'JSON: {"follow": true/false, "reason": "原因"}'
        )

        try:
            import json as _json
            import re

            result = await self.gateway.call_data_process_task(
                prompt,
                system_prompt=self.FOLLOW_UP_SYSTEM_PROMPT,
                is_json=True,
                lane_key=LaneKey(subsystem="sys2", task_family="followup", scope_id=chat_id),
                base_origin=chat_id,
            )
            data = result if isinstance(result, dict) else {}
            if not isinstance(data, dict):
                match = re.search(r"\{.*?\}", str(result), re.DOTALL)
                if match:
                    data = _json.loads(match.group(0))
            if data.get("follow") or data.get("should_follow"):
                return data.get("reason", "补充细节")
        except Exception as exc:
            logger.debug(f"[Planner] Follow-up 判定异常: {exc}")
        return None
    @staticmethod
    def _is_poke_event(event: AstrMessageEvent | None) -> bool:
        if event is None or not hasattr(event, "get_extra"):
            return False
        return bool(
            event.get_extra("is_virtual_poke", False)
            or str(event.get_extra("astrmai_interaction_kind", "") or "").lower() == "poke"
        )

    def _record_follow_up_decision(
        self,
        event: AstrMessageEvent | None,
        *,
        eligible: bool = False,
        skipped_reason: str = "",
        signals: list[str] | None = None,
        probability: float = 0.0,
        llm_checked: bool = False,
        followed: bool = False,
        reason: str = "",
        cooldown_until: float = 0.0,
    ) -> None:
        if event is None:
            return
        turn_context = ensure_turn_context(event)
        snapshot = turn_context.follow_up
        snapshot.eligible = bool(eligible)
        snapshot.skipped_reason = str(skipped_reason or "")
        snapshot.signals = [str(signal) for signal in (signals or []) if str(signal or "").strip()]
        snapshot.probability = float(probability or 0.0)
        snapshot.llm_checked = bool(llm_checked)
        snapshot.followed = bool(followed)
        snapshot.reason = str(reason or "")
        snapshot.cooldown_until = float(cooldown_until or 0.0)

    def _follow_up_cooldown_until(self, chat_id: str, now: float | None = None) -> float:
        cooldowns = getattr(self, "follow_up_cooldowns", None)
        if not isinstance(cooldowns, dict):
            return 0.0
        current_time = time.time() if now is None else float(now)
        until = float(cooldowns.get(str(chat_id or ""), 0.0) or 0.0)
        if until and until <= current_time:
            cooldowns.pop(str(chat_id or ""), None)
            return 0.0
        return until

    def _set_follow_up_cooldown(self, chat_id: str, social_intent: str) -> float:
        cooldowns = getattr(self, "follow_up_cooldowns", None)
        if not isinstance(cooldowns, dict):
            cooldowns = {}
            setattr(self, "follow_up_cooldowns", cooldowns)
        duration = self.FOLLOW_UP_COOLDOWN_SECONDS.get(str(social_intent or "answer"), 600.0)
        until = time.time() + float(duration)
        cooldowns[str(chat_id or "")] = until
        return until

    @classmethod
    def _follow_up_probability_for_intent(cls, base_probability: float, social_intent: str) -> float:
        base = max(0.0, min(1.0, float(base_probability or 0.0)))
        if social_intent == "comfort":
            return min(base * 1.5, 0.35)
        if social_intent in {"tease", "inquire"}:
            return min(base, 0.18)
        return min(base * 0.4, 0.08)

    @staticmethod
    def _looks_complete_for_follow_up(reply_text: str) -> bool:
        text = str(reply_text or "").strip()
        if len(text) < 20:
            return False
        return text.endswith(("。", ".", "！", "!", "～", "~"))

    async def _should_follow_up(
        self,
        chat_id: str,
        last_reply: str,
        *,
        event: AstrMessageEvent | None = None,
        tools=None,
        decision=None,
    ) -> Optional[str]:
        signals: list[str] = []

        def _skip(reason: str, *, probability: float = 0.0, cooldown_until: float = 0.0) -> None:
            self._record_follow_up_decision(
                event,
                eligible=False,
                skipped_reason=reason,
                signals=signals,
                probability=probability,
                llm_checked=False,
                followed=False,
                cooldown_until=cooldown_until,
            )

        if event is not None and hasattr(event, "get_extra"):
            try:
                if int(event.get_extra("astrmai_think_level", 1) or 0) < 1:
                    signals.append("think_level_below_1")
                    _skip("think_level_below_1")
                    return None
            except (TypeError, ValueError):
                pass
            if event.get_extra("astrmai_lightweight_event", False):
                signals.append("lightweight_event")
                _skip("lightweight_event")
                return None
            if self._is_poke_event(event):
                signals.append("poke_event")
                _skip("poke_event")
                return None
            cooldown_tags = {
                str(tag or "").strip()
                for tag in (event.get_extra("astrmai_agency_cooldown_tags", []) or [])
                if str(tag or "").strip()
            }
            blocking_cooldowns = cooldown_tags & {"meme", "poke", "like", "sharp_reply", "long_reply"}
            if blocking_cooldowns:
                signals.extend(sorted(blocking_cooldowns))
                _skip("agency_cooldown")
                return None
            focus_reason = str(event.get_extra("astrmai_focus_reason", "") or "").lower()
            is_direct_focus = any(token in focus_reason for token in ("at", "reply", "direct", "wakeup", "private", "name"))
            group_getter = getattr(event, "get_group_id", None)
            if callable(group_getter) and group_getter() and not is_direct_focus:
                signals.append("group_non_direct")
                _skip("group_non_direct")
                return None
            reply_need = str(event.get_extra("astrmai_reply_need", "") or "").strip()
            action_tier = str(event.get_extra("astrmai_action_tier", "") or "").strip()
            if reply_need in {"wait", "ignore"}:
                signals.append(f"reply_need_{reply_need}")
                _skip("reply_need_blocked")
                return None
            if action_tier == "none":
                signals.append("action_tier_none")
                _skip("action_tier_none")
                return None
        if tools:
            signals.append("tools_used")
            _skip("tools_used")
            return None
        social_intent = str(getattr(decision, "social_intent", "") if decision else "").strip()
        if not social_intent and event is not None and hasattr(event, "get_extra"):
            social_intent = str(event.get_extra("astrmai_social_intent", "") or "").strip()
        social_intent = social_intent or "answer"
        if social_intent in {"boundary", "pushback", "observe"}:
            signals.append(f"social_intent_{social_intent}")
            _skip("social_intent_blocked")
            return None
        if social_intent not in self.FOLLOW_UP_ALLOWED_INTENTS:
            signals.append(f"social_intent_{social_intent}")
            _skip("social_intent_not_allowed")
            return None

        if self.state_engine:
            state = await self.state_engine.get_state(chat_id)
            if state and state.energy < 0.3:
                signals.append("low_energy")
                _skip("low_energy")
                return None

        clean_reply = last_reply.strip()
        if not (8 <= len(clean_reply) <= 60):
            signals.append("reply_length_out_of_range")
            _skip("reply_length_out_of_range")
            return None
        if clean_reply.endswith(("？", "?")):
            signals.append("reply_already_invites_response")
            _skip("reply_already_invites_response")
            return None
        if social_intent != "comfort" and self._looks_complete_for_follow_up(clean_reply):
            signals.append("complete_reply")
            _skip("complete_reply")
            return None

        now = time.time()
        cooldown_until = self._follow_up_cooldown_until(chat_id, now)
        if cooldown_until > now:
            signals.append("follow_up_cooldown")
            _skip("follow_up_cooldown", cooldown_until=cooldown_until)
            return None

        reply_cfg = getattr(self.gateway.config, "reply", None)
        follow_up_probability = getattr(reply_cfg, "follow_up_probability", 0.20)
        try:
            follow_up_probability = float(follow_up_probability)
        except (TypeError, ValueError):
            follow_up_probability = 0.20
        follow_up_probability = max(0.0, min(1.0, follow_up_probability))
        effective_probability = self._follow_up_probability_for_intent(follow_up_probability, social_intent)
        if effective_probability <= 0.0:
            signals.append("follow_up_disabled")
            _skip("follow_up_disabled", probability=effective_probability)
            return None
        if random.random() > effective_probability:
            signals.append("probability_gate")
            _skip("probability_gate", probability=effective_probability)
            return None

        if social_intent == "comfort" and len(clean_reply) <= 24:
            cooldown_until = self._set_follow_up_cooldown(chat_id, social_intent)
            self._record_follow_up_decision(
                event,
                eligible=True,
                signals=[*signals, "comfort_short_reply"],
                probability=effective_probability,
                llm_checked=False,
                followed=True,
                reason="gentle_support",
                cooldown_until=cooldown_until,
            )
            return "gentle_support"

        prompt = (
            f'Last reply: "{clean_reply[:100]}"\n'
            "Should the bot send one very short natural follow-up now? "
            "Only say yes for a genuinely useful supplement, question, or gentle support.\n"
            'JSON: {"follow": true/false, "reason": "reason"}'
        )

        try:
            import json as _json
            import re

            result = await self.gateway.call_data_process_task(
                prompt,
                system_prompt=self.FOLLOW_UP_SYSTEM_PROMPT,
                is_json=True,
                lane_key=LaneKey(subsystem="sys2", task_family="followup", scope_id=chat_id),
                base_origin=chat_id,
            )
            data = result if isinstance(result, dict) else {}
            if not isinstance(data, dict):
                match = re.search(r"\{.*?\}", str(result), re.DOTALL)
                if match:
                    data = _json.loads(match.group(0))
            if data.get("follow") or data.get("should_follow"):
                reason = str(data.get("reason", "extra_detail") or "extra_detail")
                cooldown_until = self._set_follow_up_cooldown(chat_id, social_intent)
                self._record_follow_up_decision(
                    event,
                    eligible=True,
                    signals=signals,
                    probability=effective_probability,
                    llm_checked=True,
                    followed=True,
                    reason=reason,
                    cooldown_until=cooldown_until,
                )
                return reason
            self._record_follow_up_decision(
                event,
                eligible=True,
                skipped_reason="llm_rejected",
                signals=signals,
                probability=effective_probability,
                llm_checked=True,
                followed=False,
            )
        except Exception as exc:
            logger.debug(f"[Planner] Follow-up 判定异常: {exc}")
            self._record_follow_up_decision(
                event,
                eligible=True,
                skipped_reason="llm_error",
                signals=signals,
                probability=effective_probability,
                llm_checked=True,
                followed=False,
            )
        return None


__all__ = ["Planner"]
