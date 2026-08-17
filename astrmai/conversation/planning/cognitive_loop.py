from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ...infrastructure.runtime.lane_manager import LaneKey
from ..contracts.prompt_envelope import PromptEnvelope
from ..contracts.turn_context import ensure_turn_context, get_turn_context
from .member_action_intent import detect_member_action_candidate, normalize_member_action_purpose


@dataclass(slots=True)
class CognitiveDecision:
    action: str = "reply"  # reply | wait | ignore | tool_call
    intent: str = ""
    memory_policy: str = "light"  # none | light | deep
    retrieve_keys: list[str] = field(default_factory=list)
    style_policy: str = ""
    forbid_history_continuation: bool = True
    inner_monologue: str = ""
    reply_need: str = "reply"  # reply | wait | ignore
    social_intent: str = "answer"
    action_tier: str = "chat"  # none | chat | full | sys3
    allowed_action_families: list[str] = field(default_factory=list)
    stance: str = "neutral"  # warm | neutral | cool | guarded
    state_bias: str = ""
    risk_flags: list[str] = field(default_factory=list)
    attack_confidence: float = 0.0
    member_action_purpose: str = ""
    member_action_target: str = ""
    member_action_confidence: float = 0.0


@dataclass(slots=True)
class CognitiveLoopGateDecision:
    should_run: bool = False
    skip_reason: str = ""
    signals: list[str] = field(default_factory=list)
    readonly_tools_allowed: bool = False


class CognitiveLoop:
    """Planner-side cognitive decision layer with one optional readonly tool step."""

    SOFT_TIMEOUT_SECONDS = 2.5
    FULL_RETRIEVE_KEYS = [
        "logic_style",
        "speech_style",
        "world_view",
        "timeline",
        "relations",
        "skills",
        "values",
        "secrets",
    ]
    ALLOWED_READONLY_TOOLS = {
        "self_lore",
        "user_profile",
        "relationship",
        "current_state",
        "light_memory",
    }
    COMPLEXITY_HINTS = (
        "为什么",
        "为何",
        "怎么",
        "如何",
        "帮我",
        "请你",
        "记得",
        "刚才",
        "之前",
        "分析",
        "搜索",
        "查一下",
        "查下",
        "工具",
    )
    SYSTEM_PROMPT = (
        "You are the hidden cognitive loop for a chat bot planner. "
        "Your output is never shown to the user. "
        "Decide only from the current clue, do not script-continue old dialogue. "
        "You may request at most one readonly tool observation before the final decision. "
        "Never request side-effect tools. "
        'Return strict JSON with keys: action, intent, memory_policy, retrieve_keys, '
        "style_policy, forbid_history_continuation, inner_monologue, reply_need, "
        "social_intent, action_tier, allowed_action_families, stance, state_bias, "
        "risk_flags, attack_confidence, member_action_purpose, member_action_target, "
        "member_action_confidence, "
        "need_tool, tool_name, tool_query. "
        "Valid action: reply|wait|ignore|tool_call. "
        "Valid memory_policy: none|light|deep. "
        "Valid social_intent: answer|comfort|tease|pushback|boundary|observe|join|inquire|recall|redirect. "
        "Valid action_tier: none|chat|full|sys3. "
        "Valid allowed_action_families values: wait|query|at|poke|meme|resonance|topic|private|withdraw|reaction|qq_reaction|like. "
        "When a Member action candidate is supplied, classify its actual purpose as exactly one of "
        "mention_member|lookup_member|discuss_member|unclear. Confirm intent only: copy the supplied target text, "
        "never invent a QQ number or group id. Use mention_member only when the user truly asks the bot to call/@ that person. "
        "Pushback is allowed only for very obvious direct attacks toward the bot with attack_confidence >= 0.85; "
        "when and only when it is a direct attack toward the bot, include risk_flags item direct_attack_to_bot. "
        "Ordinary disagreement, correction, joking, third-party conflict, distress, or help-seeking must not be pushback. "
        "otherwise choose boundary, observe, ignore, or a neutral reply. "
        "Long-term feedback and Heartflow state may guide posture, memory policy, timing, and action tier, "
        "but never repeat them to the user."
    )
    FINAL_SYSTEM_PROMPT = (
        "You are the hidden cognitive loop for a chat bot planner. "
        "You already received one readonly observation. "
        "Now return the final strict JSON only. "
        "Do not ask for more tools. "
        'Required keys: action, intent, memory_policy, retrieve_keys, '
        "style_policy, forbid_history_continuation, inner_monologue, reply_need, "
        "social_intent, action_tier, allowed_action_families, stance, state_bias, "
        "risk_flags, attack_confidence, member_action_purpose, member_action_target, member_action_confidence."
    )

    def __init__(self, gateway, memory_engine=None, state_engine=None, config=None):
        self.gateway = gateway
        self.memory_engine = memory_engine
        self.state_engine = state_engine
        self.config = config if config is not None else getattr(gateway, "config", None)

    def refresh_config(self, config) -> None:
        self.config = config

    def _soft_timeout_seconds(self) -> float:
        timing = getattr(self.config, "timing", None)
        try:
            configured = float(
                getattr(timing, "cognitive_loop_timeout_sec", self.SOFT_TIMEOUT_SECONDS)
                or self.SOFT_TIMEOUT_SECONDS
            )
        except (TypeError, ValueError):
            configured = self.SOFT_TIMEOUT_SECONDS
        return max(0.01, configured)

    def _lane_key(self, chat_id: str) -> LaneKey:
        return LaneKey(subsystem="sys2", task_family="cognitive_loop", scope_id=chat_id)

    def should_run(self, event: AstrMessageEvent, prompt_envelope: PromptEnvelope | None = None) -> bool:
        gate = self.gate_decision(event, prompt_envelope)
        self._write_gate_state(event, gate, ran=False)
        return gate.should_run

    def mark_gate_decision(
        self,
        event: AstrMessageEvent,
        gate: CognitiveLoopGateDecision,
        *,
        ran: bool = False,
    ) -> None:
        self._write_gate_state(event, gate, ran=ran)

    def _min_loop_think_level(self) -> int:
        attention_cfg = getattr(self.config, "attention", None)
        try:
            return max(1, min(3, int(getattr(attention_cfg, "cognitive_loop_min_think_level", 2) or 2)))
        except (TypeError, ValueError):
            return 2

    def gate_decision(
        self,
        event: AstrMessageEvent,
        prompt_envelope: PromptEnvelope | None = None,
    ) -> CognitiveLoopGateDecision:
        think_level = self._think_level(event)
        signals: list[str] = []
        if think_level is not None and think_level <= 0:
            return CognitiveLoopGateDecision(False, "think_level_0", [f"think_level_{think_level}"], False)
        if event.get_extra("astrmai_lightweight_event", False):
            return CognitiveLoopGateDecision(False, "lightweight_event", ["lightweight_event"], False)
        if event.get_extra("is_fast_mode", False):
            return CognitiveLoopGateDecision(False, "fast_mode", ["fast_mode"], False)
        current_keys = event.get_extra("retrieve_keys", [])
        if isinstance(current_keys, list) and any(key == "CORE_ONLY" for key in current_keys):
            return CognitiveLoopGateDecision(False, "core_only", ["core_only"], False)
        judge_action = str(event.get_extra("judge_action", "REPLY") or "REPLY").upper()
        if judge_action in {"WAIT", "IGNORE"}:
            return CognitiveLoopGateDecision(False, f"judge_{judge_action.lower()}", [f"judge_{judge_action.lower()}"], False)
        if judge_action == "TOOL_CALL":
            return CognitiveLoopGateDecision(False, "tool_call_mode", ["tool_call_mode"], False)
        current_text = self._current_text(event, prompt_envelope)
        if not current_text:
            return CognitiveLoopGateDecision(False, "empty_message", ["empty_message"], False)
        if self._cooldown_blocks_loop(event, current_text, think_level):
            return CognitiveLoopGateDecision(
                False,
                "cooldown_simple_turn",
                ["cooldown_simple_turn", *self._cooldown_tags(event)],
                False,
            )
        readonly_allowed = think_level is not None and think_level >= 3
        # OPT-08/RT-06: think1 覆盖 82% 消息、每次 8-35s 意图分类多为平凡结论；
        # 低于门槛(默认2)的等级不再无条件放行，落到下方长句/复杂度信号判定
        if think_level is not None and think_level >= self._min_loop_think_level():
            return CognitiveLoopGateDecision(True, "", [f"think_level_{think_level}"], readonly_allowed)
        if len(current_text) >= 12:
            return CognitiveLoopGateDecision(True, "", ["legacy_length"], False)
        lowered = current_text.lower()
        if any(token in current_text or token in lowered for token in self.COMPLEXITY_HINTS):
            return CognitiveLoopGateDecision(True, "", ["legacy_complexity"], False)
        # Single-char `?` / `？` are too noisy for COMPLEXITY_HINTS (match URLs,
        # emoji, punctuation).  Only treat as complexity when the non-whitespace,
        # non-question-mark text is non-trivial (≥ 3 chars).
        compact = "".join(current_text.split())
        meaningful = compact.replace("?", "").replace("？", "")
        if ("?" in compact or "？" in compact) and len(meaningful) >= 3:
            return CognitiveLoopGateDecision(True, "", ["legacy_complexity"], False)
        return CognitiveLoopGateDecision(False, "trivial_turn", ["trivial_turn"], False)

    async def decide(
        self,
        *,
        event: AstrMessageEvent,
        prompt_envelope: PromptEnvelope | None = None,
        gate: CognitiveLoopGateDecision | None = None,
    ) -> Optional[CognitiveDecision]:
        if gate is None:
            gate = self.gate_decision(event, prompt_envelope)
        if not gate.should_run:
            return None
        try:
            self._write_gate_state(event, gate, ran=True)
            return await asyncio.wait_for(
                self._decide_inner(event=event, prompt_envelope=prompt_envelope),
                timeout=self._soft_timeout_seconds(),
            )
        except asyncio.TimeoutError:
            logger.info(f"[{event.unified_msg_origin}] CognitiveLoop timeout; fallback to planner default flow.")
        except Exception as exc:
            logger.debug(f"[{event.unified_msg_origin}] CognitiveLoop degraded: {exc}")
        return None

    async def _decide_inner(
        self,
        *,
        event: AstrMessageEvent,
        prompt_envelope: PromptEnvelope | None,
    ) -> Optional[CognitiveDecision]:
        chat_id = event.unified_msg_origin
        prompt = self._build_initial_prompt(event, prompt_envelope)
        raw = await self.gateway.call_data_process_task(
            prompt,
            system_prompt=self.SYSTEM_PROMPT,
            is_json=True,
            lane_key=self._lane_key(chat_id),
            base_origin=chat_id,
        )
        data = self._safe_parse_json(raw)
        if not data:
            return None

        think_level = self._think_level(event)
        if data.get("need_tool"):
            readonly_allowed, readonly_skip_reason = self._readonly_tool_gate(event, data)
            self._write_readonly_state(event, readonly_allowed, readonly_skip_reason)
        else:
            readonly_allowed = bool(think_level is not None and think_level >= 3)
            self._write_readonly_state(event, readonly_allowed, "")
        if data.get("need_tool") and readonly_allowed:
            tool_name = str(data.get("tool_name", "") or "").strip()
            tool_query = str(data.get("tool_query", "") or "").strip()
            observation = await self._run_readonly_tool(
                event=event,
                tool_name=tool_name,
                tool_query=tool_query,
            )
            final_prompt = self._build_finalize_prompt(prompt, data, observation)
            final_raw = await self.gateway.call_data_process_task(
                final_prompt,
                system_prompt=self.FINAL_SYSTEM_PROMPT,
                is_json=True,
                lane_key=self._lane_key(chat_id),
                base_origin=chat_id,
            )
            data = self._safe_parse_json(final_raw)
            if not data:
                return None

        return self._build_decision(data)

    @staticmethod
    def _write_gate_state(event: AstrMessageEvent, gate: CognitiveLoopGateDecision, *, ran: bool) -> None:
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_cognitive_loop_ran", bool(ran))
            event.set_extra("astrmai_cognitive_loop_skipped_reason", "" if gate.should_run else gate.skip_reason)
            event.set_extra("astrmai_cognitive_loop_skip_signals", list(gate.signals or []))
            event.set_extra("astrmai_cognitive_loop_readonly_tools_allowed", bool(gate.readonly_tools_allowed))
        turn_context = ensure_turn_context(event)
        turn_context.cognitive.cognitive_loop_ran = bool(ran)
        turn_context.cognitive.cognitive_loop_skipped_reason = "" if gate.should_run else gate.skip_reason
        turn_context.cognitive.cognitive_loop_skip_signals = list(gate.signals or [])
        turn_context.cognitive.readonly_tools_allowed = bool(gate.readonly_tools_allowed)

    @staticmethod
    def _write_readonly_state(event: AstrMessageEvent, allowed: bool, skip_reason: str) -> None:
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_cognitive_loop_readonly_tools_allowed", bool(allowed))
            event.set_extra("astrmai_cognitive_loop_readonly_skip_reason", str(skip_reason or ""))
        turn_context = ensure_turn_context(event)
        turn_context.cognitive.readonly_tools_allowed = bool(allowed)
        turn_context.cognitive.readonly_tools_skip_reason = str(skip_reason or "")

    def _readonly_tool_gate(self, event: AstrMessageEvent, data: dict[str, Any]) -> tuple[bool, str]:
        think_level = self._think_level(event)
        if think_level is not None and think_level < 3:
            return False, f"think_level_{think_level}_blocks_readonly_tool"
        if think_level is None:
            return True, ""
        tool_name = str(data.get("tool_name", "") or "").strip()
        memory_policy = self._normalize_memory_policy(data.get("memory_policy"))
        if memory_policy == "deep" or tool_name in self.ALLOWED_READONLY_TOOLS:
            return True, ""
        return False, "readonly_tool_not_explicit"

    @staticmethod
    def _cooldown_tags(event: AstrMessageEvent) -> list[str]:
        if not hasattr(event, "get_extra"):
            return []
        raw_tags = event.get_extra("astrmai_agency_cooldown_tags", [])
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        if not isinstance(raw_tags, list):
            return []
        return [str(tag or "").strip() for tag in raw_tags if str(tag or "").strip()]

    def _cooldown_blocks_loop(self, event: AstrMessageEvent, current_text: str, think_level: int | None) -> bool:
        if think_level is not None and think_level >= 3:
            return False
        cooldown_tags = set(self._cooldown_tags(event))
        if not (cooldown_tags & {"sharp_reply", "long_reply"}):
            return False
        compact_text = "".join(str(current_text or "").split())
        if len(compact_text) > 24:
            return False
        lowered = current_text.lower()
        return not any(token in current_text or token in lowered for token in self.COMPLEXITY_HINTS)

    @staticmethod
    def _think_level(event: AstrMessageEvent) -> int | None:
        if not hasattr(event, "get_extra"):
            return None
        value = event.get_extra("astrmai_think_level", None)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        cleaned = str(text or "").strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[:limit] + "..."

    def _current_text(self, event: AstrMessageEvent, prompt_envelope: PromptEnvelope | None) -> str:
        if isinstance(prompt_envelope, PromptEnvelope):
            text = (
                prompt_envelope.raw_user_text
                or prompt_envelope.focus_message_text
                or prompt_envelope.direct_context_text
                or getattr(event, "message_str", "")
                or ""
            )
        else:
            text = str(getattr(event, "message_str", "") or "")
        return str(text or "").strip()

    def _build_initial_prompt(self, event: AstrMessageEvent, prompt_envelope: PromptEnvelope | None) -> str:
        current_text = self._current_text(event, prompt_envelope)
        member_candidate = detect_member_action_candidate(current_text)
        member_candidate_data = member_candidate.to_dict() if member_candidate is not None else {}
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_member_action_candidate", member_candidate_data)
        focus_message = getattr(prompt_envelope, "focus_message_text", "") if isinstance(prompt_envelope, PromptEnvelope) else ""
        direct_context = getattr(prompt_envelope, "direct_context_text", "") if isinstance(prompt_envelope, PromptEnvelope) else ""
        related_context = getattr(prompt_envelope, "related_context_text", "") if isinstance(prompt_envelope, PromptEnvelope) else ""
        background = getattr(prompt_envelope, "ambient_background_text", "") if isinstance(prompt_envelope, PromptEnvelope) else ""
        recent_transcript = getattr(prompt_envelope, "recent_transcript", "") if isinstance(prompt_envelope, PromptEnvelope) else ""
        last_reply = getattr(prompt_envelope, "last_assistant_reply", "") if isinstance(prompt_envelope, PromptEnvelope) else ""
        focus_reason = getattr(prompt_envelope, "focus_reason", "") if isinstance(prompt_envelope, PromptEnvelope) else ""
        agency_reflection = ""
        memory_feedback = ""
        heartflow_context = ""
        conversation_continuity = ""
        if hasattr(event, "get_extra"):
            agency_reflection = str(event.get_extra("astrmai_agency_reflection_summary", "") or "")
            memory_feedback = str(event.get_extra("astrmai_memory_feedback_summary", "") or "")
            heartflow_context = str(event.get_extra("astrmai_heartflow_context", "") or "")
            conversation_continuity = str(event.get_extra("astrmai_conversation_continuity_summary", "") or "")
            think_level = event.get_extra("astrmai_think_level", "")
            think_reason = str(event.get_extra("astrmai_think_reason", "") or "")
            think_signals = event.get_extra("astrmai_think_signals", [])
        else:
            think_level = ""
            think_reason = ""
            think_signals = []
        turn_context = get_turn_context(event)
        if turn_context is not None:
            agency_reflection = agency_reflection or turn_context.continuity.agency_reflection_summary
            memory_feedback = memory_feedback or turn_context.continuity.memory_feedback_summary
            heartflow_context = heartflow_context or turn_context.continuity.heartflow_context
            conversation_continuity = conversation_continuity or turn_context.continuity.conversation_summary
        safe_current_text = PromptEnvelope.sanitize_inline_text(current_text)
        safe_focus_message = PromptEnvelope.sanitize_inline_text(focus_message)
        safe_direct_context = PromptEnvelope.sanitize_inline_text(direct_context)
        safe_related_context = PromptEnvelope.sanitize_inline_text(related_context)
        safe_background = PromptEnvelope.sanitize_inline_text(background)
        safe_recent_transcript = PromptEnvelope.sanitize_inline_text(recent_transcript)
        return (
            "Current message:\n"
            f"{self._truncate(safe_current_text, 600)}\n\n"
            "Member action candidate (rule proposal; semantically confirm purpose only):\n"
            f"{json.dumps(member_candidate_data, ensure_ascii=False)}\n\n"
            "Focused clue:\n"
            f"{self._truncate(safe_focus_message, 500)}\n\n"
            "Direct context:\n"
            f"{self._truncate(safe_direct_context, 600)}\n\n"
            "Related context:\n"
            f"{self._truncate(safe_related_context, 600)}\n\n"
            "Background (reference only):\n"
            f"{self._truncate(safe_background, 600)}\n\n"
            "Recent transcript:\n"
            f"{self._truncate(safe_recent_transcript, 800)}\n\n"
            "Last bot reply:\n"
            f"{self._truncate(last_reply, 240)}\n\n"
            "Short-term self-continuity notes:\n"
            f"{self._truncate(agency_reflection, 800)}\n\n"
            "Long-term behavior/memory feedback:\n"
            f"{self._truncate(memory_feedback, 1000)}\n\n"
            "Heartflow state:\n"
            f"{self._truncate(heartflow_context, 1000)}\n"
            "This Heartflow state is hidden. Use it only to adjust posture, timing, memory policy and action tier. "
            "Never repeat it to the user.\n\n"
            "Conversation continuity:\n"
            f"{self._truncate(conversation_continuity, 800)}\n\n"
            "Think budget:\n"
            f"level={think_level}; reason={think_reason}; signals={self._truncate(','.join(str(item) for item in (think_signals or [])), 240)}\n\n"
            f"Focus reason: {focus_reason or 'latest_message'}\n"
            f"Chat id: {PromptEnvelope.sanitize_inline_text(str(event.unified_msg_origin or ''))}\n"
            f"Sender id: {PromptEnvelope.sanitize_inline_text(str(event.get_sender_id() or ''))}\n"
            f"Sender name: {PromptEnvelope.sanitize_inline_text(str(event.get_sender_name() or ''))}\n"
            "If a readonly observation would materially help, request one allowed tool."
        )

    def _build_finalize_prompt(
        self,
        initial_prompt: str,
        prior_data: dict[str, Any],
        observation: str,
    ) -> str:
        return (
            f"{initial_prompt}\n\n"
            "Previous tentative decision:\n"
            f"{json.dumps(prior_data, ensure_ascii=False)}\n\n"
            "Readonly observation:\n"
            f"{self._truncate(observation, 1200)}\n\n"
            "Now produce the final decision JSON."
        )

    def _safe_parse_json(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            chunk = self._extract_braced_json(text)
            if chunk is None:
                return {}
            try:
                return json.loads(chunk)
            except Exception:
                return {}

    @staticmethod
    def _extract_braced_json(text: str) -> str | None:
        r"""Extract the first complete JSON object from text by counting braces.

        This handles nested objects/arrays correctly, unlike greedy
        ``re.search(r"\{.*\}", text, re.DOTALL)`` which can match from the
        first ``{`` to the last ``}`` and produce invalid JSON.

        Braces inside JSON string literals are ignored so that values like
        ``{"key": "text with { in it}"}`` are not miscounted.
        """
        start = text.find('{')
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    def _build_decision(self, data: dict[str, Any]) -> CognitiveDecision:
        action = self._normalize_action(data.get("action"))
        reply_need = self._normalize_reply_need(data.get("reply_need", action))
        if reply_need in {"wait", "ignore"}:
            action = reply_need
        memory_policy = self._normalize_memory_policy(data.get("memory_policy"))
        retrieve_keys = self._normalize_retrieve_keys(data.get("retrieve_keys"), memory_policy)
        social_intent = self._normalize_social_intent(data.get("social_intent", data.get("intent", "")))
        risk_flags = self._normalize_string_list(data.get("risk_flags"))
        attack_confidence = self._normalize_confidence(data.get("attack_confidence"))
        if social_intent == "pushback" and not self._allow_pushback(attack_confidence, risk_flags):
            social_intent = "boundary"
            if not any(flag == "pushback_downgraded" for flag in risk_flags):
                risk_flags.append("pushback_downgraded")
        action_tier = self._normalize_action_tier(data.get("action_tier"), action, social_intent)
        if reply_need in {"wait", "ignore"} and action_tier != "none":
            action_tier = "none"
        member_action_purpose = normalize_member_action_purpose(data.get("member_action_purpose"))
        member_action_target = str(data.get("member_action_target", "") or "").strip()
        member_action_confidence = self._normalize_confidence(data.get("member_action_confidence"))
        return CognitiveDecision(
            action=action,
            intent=str(data.get("intent", "") or "").strip(),
            memory_policy=memory_policy,
            retrieve_keys=retrieve_keys,
            style_policy=str(data.get("style_policy", "") or "").strip(),
            forbid_history_continuation=bool(data.get("forbid_history_continuation", True)),
            inner_monologue=str(data.get("inner_monologue", "") or "").strip(),
            reply_need=reply_need,
            social_intent=social_intent,
            action_tier=action_tier,
            allowed_action_families=self._normalize_string_list(data.get("allowed_action_families")),
            stance=self._normalize_stance(data.get("stance"), social_intent),
            state_bias=str(data.get("state_bias", "") or "").strip(),
            risk_flags=risk_flags,
            attack_confidence=attack_confidence,
            member_action_purpose=member_action_purpose,
            member_action_target=member_action_target,
            member_action_confidence=member_action_confidence,
        )

    @staticmethod
    def _normalize_action(value: Any) -> str:
        normalized = str(value or "reply").strip().lower()
        if normalized in {"tool", "toolcall"}:
            normalized = "tool_call"
        if normalized not in {"reply", "wait", "ignore", "tool_call"}:
            return "reply"
        return normalized

    @staticmethod
    def _normalize_reply_need(value: Any) -> str:
        normalized = str(value or "reply").strip().lower()
        if normalized == "no_reply":
            normalized = "ignore"
        if normalized not in {"reply", "wait", "ignore"}:
            return "reply"
        return normalized

    @staticmethod
    def _normalize_social_intent(value: Any) -> str:
        normalized = str(value or "answer").strip().lower()
        aliases = {
            "respond": "answer",
            "reply": "answer",
            "listen": "observe",
            "support": "comfort",
            "remember": "recall",
            "lookup": "inquire",
        }
        normalized = aliases.get(normalized, normalized)
        allowed = {"answer", "comfort", "tease", "pushback", "boundary", "observe", "join", "inquire", "recall", "redirect"}
        return normalized if normalized in allowed else "answer"

    @staticmethod
    def _normalize_action_tier(value: Any, action: str, social_intent: str) -> str:
        normalized = str(value or "").strip().lower()
        if action == "tool_call":
            return "sys3"
        if social_intent in {"boundary", "observe"} and not normalized:
            return "none"
        if social_intent in {"inquire", "recall", "redirect"} and not normalized:
            return "full"
        if normalized not in {"none", "chat", "full", "sys3"}:
            return "chat"
        return normalized

    @staticmethod
    def _normalize_stance(value: Any, social_intent: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"warm", "neutral", "cool", "guarded"}:
            if social_intent in {"comfort"}:
                return "warm"
            if social_intent in {"boundary", "pushback"}:
                return "guarded"
            if social_intent in {"observe"}:
                return "cool"
            return "neutral"
        return normalized

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
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

    @staticmethod
    def _normalize_confidence(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value or 0.0)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _allow_pushback(attack_confidence: float, risk_flags: list[str]) -> bool:
        blocked_flags = {
            "vulnerable_user",
            "help_seeking",
            "self_harm",
            "medical",
            "legal",
            "financial",
            "medical/legal/financial_sensitive",
            "unclear_target",
            "ambiguous",
            "ambiguous_context",
            "sensitive_topic",
            "third_party_conflict",
            "joke",
            "low_intensity",
        }
        direct_flags = {"direct_attack_to_bot", "bot_direct_attack", "target_bot", "directed_at_bot"}
        flags = set(risk_flags)
        return attack_confidence >= 0.85 and bool(flags & direct_flags) and not (flags & blocked_flags)

    @staticmethod
    def _normalize_memory_policy(value: Any) -> str:
        normalized = str(value or "light").strip().lower()
        if normalized not in {"none", "light", "deep"}:
            return "light"
        return normalized

    def _normalize_retrieve_keys(self, value: Any, memory_policy: str) -> list[str]:
        if isinstance(value, str):
            raw_keys = [value]
        elif isinstance(value, list):
            raw_keys = value
        else:
            raw_keys = []
        keys: list[str] = []
        for item in raw_keys:
            key = str(item or "").strip()
            if key and key not in {"CORE_ONLY", "ALL"} and key in self.FULL_RETRIEVE_KEYS and key not in keys:
                keys.append(key)
        if memory_policy == "none":
            return []
        if memory_policy == "deep" and not keys:
            return list(self.FULL_RETRIEVE_KEYS)
        return keys

    async def _run_readonly_tool(self, *, event: AstrMessageEvent, tool_name: str, tool_query: str) -> str:
        if tool_name not in self.ALLOWED_READONLY_TOOLS:
            return f"Readonly restriction: tool `{tool_name}` is not allowed in CognitiveLoop."

        chat_id = event.unified_msg_origin
        sender_id = str(event.get_sender_id() or "")
        if tool_name == "self_lore":
            if not self.memory_engine:
                return "Self lore is offline."
            persona_id = str(getattr(getattr(self.config, "persona", None), "persona_id", "") or "")
            query = tool_query or self._current_text(event, None)
            tool_service = getattr(self.memory_engine, "tool_service", None)
            if tool_service and hasattr(tool_service, "self_lore_query"):
                result = await tool_service.self_lore_query(query=query, persona_id=persona_id, event=event)
                return tool_service.render_result(result)
            return "Self lore is offline."

        if tool_name == "user_profile":
            if not self.state_engine or not hasattr(self.state_engine, "get_user_profile_summary"):
                return "User profile service is offline."
            summary = await self.state_engine.get_user_profile_summary(sender_id)
            memory_points = ", ".join(str(item) for item in (summary.memory_points or [])[:4] if str(item).strip())
            return (
                f"name={summary.name or sender_id}; nickname={summary.nickname or ''}; "
                f"social_score={summary.social_score:.1f}; analysis={summary.persona_analysis or ''}; "
                f"memory_points={memory_points}"
            )

        if tool_name == "relationship":
            if not self.state_engine or not hasattr(self.state_engine, "relationship_engine"):
                return "Relationship engine is offline."
            vector = self.state_engine.relationship_engine.get_or_create(sender_id)
            return vector.get_context_description() if hasattr(vector, "get_context_description") else str(vector)

        if tool_name == "current_state":
            if not self.state_engine or not hasattr(self.state_engine, "get_state"):
                return "Current state service is offline."
            state = await self.state_engine.get_state(chat_id)
            last_reply_time = float(getattr(state, "last_reply_time", 0.0) or 0.0)
            idle_seconds = max(0, int(time.time() - last_reply_time)) if last_reply_time > 0 else -1  # ponytail: NTP guard
            return (
                f"energy={float(getattr(state, 'energy', 0.0) or 0.0):.2f}; "
                f"mood={float(getattr(state, 'mood', 0.0) or 0.0):.2f}; "
                f"total_replies={int(getattr(state, 'total_replies', 0) or 0)}; "
                f"idle_seconds={idle_seconds}"
            )

        if tool_name == "light_memory":
            if not self.memory_engine:
                return "Light memory service is offline."
            query = tool_query or self._current_text(event, None)
            tool_service = getattr(self.memory_engine, "tool_service", None)
            if tool_service and hasattr(tool_service, "search_memory"):
                result = await tool_service.search_memory(query=query, session_id=chat_id, top_k=2, event=event)
                return tool_service.render_result(result)
            return "Light memory service is offline."

        return f"Readonly restriction: tool `{tool_name}` is not available."


__all__ = ["CognitiveDecision", "CognitiveLoop", "CognitiveLoopGateDecision"]
