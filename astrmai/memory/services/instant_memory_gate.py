from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger

from ...infrastructure.context_economy import PromptTemplateId
from ...infrastructure.runtime.lane_manager import LaneKey
from ..contracts.memory_query import CommittedMemoryTurn, InstantGateResult, MemoryWriteRequest
from .memory_claim_service import MemoryClaimExtractor, MemoryConflictResolver


class InstantMemoryGate:
    _INSTANT_PATTERNS = [
        ("identity", re.compile(r"我(?:叫|是|名字(?:是|叫)?)\s*(\S{1,20})")),
        ("contact", re.compile(r"(?:手机|电话|微信|QQ|邮箱)[号码]*\s*[:：]?\s*(\S{5,30})")),
        ("preference", re.compile(r"我(?:喜欢|讨厌|最爱|不吃|不喜欢|偏好)\s*(.{2,40})")),
        ("relationship", re.compile(r"(?:男朋友|女朋友|老公|老婆|分手|结婚|离婚|恋爱)")),
        ("major_event", re.compile(r"(?:住院|去世|毕业|入职|辞职|搬家|怀孕|生了)")),
        ("explicit_cmd", re.compile(r"(?:记住|别忘了|记下来|帮我记|你要记得)")),
    ]

    def __init__(self, gateway: Any, engine: Any, config: Any = None):
        self.gateway = gateway
        self.engine = engine
        self.config = config if config is not None else getattr(gateway, "config", None)
        self.prompt_registry = getattr(getattr(gateway, "context_economy", None), "templates", None)
        self.claim_extractor = MemoryClaimExtractor(gateway)
        self.conflict_resolver = MemoryConflictResolver()

    async def process_committed_turn(self, turn: CommittedMemoryTurn) -> InstantGateResult:
        if turn.is_proactive:
            return InstantGateResult()
        if not str(turn.user_text or "").strip() or not str(turn.assistant_text or "").strip():
            return InstantGateResult()
        await self._observe(turn, "gate_entered", summary="turn entered instant gate")
        return await self._try_instant_memorize(turn)

    async def _try_instant_memorize(self, turn: CommittedMemoryTurn) -> InstantGateResult:
        text = str(turn.user_text or "").strip()
        if not hasattr(self.engine, "write_service") or len(text) < 4:
            return InstantGateResult()
        matched = self._rule_gate_match(text)
        if not matched:
            await self._observe(turn, "gate_miss", reason="rule_miss", summary="no instant rule matched")
            return InstantGateResult()

        category, extracted = matched
        content = f"[????|{category}] ????{text}"
        request, payload = await self._build_split_write_request(
            source="instant_gate",
            raw_text=text,
            extracted_fact=content,
            turn=turn,
            category=category,
        )
        memory_id = await self.engine.write_service.write(request)
        await self._observe(
            turn,
            "gate_hit",
            reason=category,
            summary=extracted[:120],
            memory_id=str(memory_id or ""),
            payload=payload,
        )
        return InstantGateResult(
            hit=bool(memory_id),
            memory_id=str(memory_id or ""),
            category=category,
            skip_backfill=True,
        )

    def _rule_gate_match(self, user_msg: str):
        text = str(user_msg or "").strip()
        if len(text) < 4:
            return None
        for category, pattern in self._INSTANT_PATTERNS:
            match = pattern.search(text)
            if match:
                extracted = match.group(1) if match.lastindex else match.group(0)
                return category, extracted.strip()
        return None

    def should_run_llm_backfill(self, turn: CommittedMemoryTurn, *, session_rounds: int, last_check: float, now: float) -> bool:
        gateway = getattr(self, "gateway", None)
        if not gateway or not hasattr(gateway, "call_data_process_task"):
            return False
        think_level = int(turn.think_level or 0)
        if think_level < 2 and session_rounds < 5:
            return False
        if now - float(last_check or 0.0) < 120:
            return False
        return True

    async def _build_split_write_request(
        self,
        *,
        source: str,
        raw_text: str,
        extracted_fact: str,
        turn: CommittedMemoryTurn,
        category: str,
    ) -> tuple[MemoryWriteRequest, dict[str, Any]]:
        claims = []
        decision = None
        fallback_used = False
        try:
            claims = await self.claim_extractor.extract(
                user_text=str(raw_text or ""),
                assistant_text=str(extracted_fact or ""),
                subject_id=str(turn.sender_id or turn.chat_id or ""),
                turn_id=str(turn.turn_id or ""),
                context_hint=source,
            )
            if claims:
                decision = self.conflict_resolver.resolve(claims)
        except Exception as exc:
            logger.error(f"[Instant Gate Error] Claim processing failed: {exc}. Falling back to legacy routing.")

        if claims and decision is not None:
            primary_claim = claims[0]
            if decision.action == "authority_override":
                request = MemoryWriteRequest(
                    source=source,
                    kind="fact",
                    session_id=str(turn.chat_id or ""),
                    sender_id=str(turn.sender_id or ""),
                    persona_id=str(turn.persona_id or ""),
                    content=str(extracted_fact or raw_text or ""),
                    summary=str(primary_claim.value or extracted_fact or "")[:240],
                    importance=0.85 if source == "instant_gate" else 0.8,
                    confidence=max(primary_claim.certainty, 0.8 if source == "instant_gate" else 0.72),
                    metadata={
                        "source": source,
                        "authority_eav": True,
                        "fact_scope": str(primary_claim.fact_scope or "medium_term"),
                        "correction_strength": float(decision.truth_score or decision.correction_strength or 0.0),
                        "promotion_entity": str(primary_claim.entity or ""),
                        "promotion_attribute": str(primary_claim.attribute or ""),
                        "promotion_value": str(primary_claim.value or ""),
                        "supports": [],
                        "contradicts": [],
                        "corrects": [],
                        "turn_id": str(turn.turn_id or ""),
                        "gate_category": category,
                        "instant_write": True,
                    },
                    dedup_key=f"{primary_claim.subject_id}:{primary_claim.entity}:{primary_claim.attribute}",
                    created_at=float(turn.committed_at or 0.0),
                )
                return request, {
                    "claim_count": len(claims),
                    "decision_action": "authority_override",
                    "fact_scope": str(primary_claim.fact_scope or "medium_term"),
                    "volatile_state": False,
                    "fallback_used": False,
                }
            if decision.action == "volatile_state_write" or str(primary_claim.fact_scope or "") == "short_term":
                request = MemoryWriteRequest(
                    source=source,
                    kind="topic",
                    session_id=str(turn.chat_id or ""),
                    sender_id=str(turn.sender_id or ""),
                    persona_id=str(turn.persona_id or ""),
                    content=str(raw_text or ""),
                    summary=str(primary_claim.value or extracted_fact or "")[:240],
                    importance=0.7,
                    confidence=max(primary_claim.certainty, 0.7),
                    metadata={
                        "source": f"{source}_degraded",
                        "volatile_state": True,
                        "fact_scope": "short_term",
                        "correction_strength": float(decision.truth_score or decision.correction_strength or 0.0),
                        "turn_id": str(turn.turn_id or ""),
                        "gate_category": category,
                        "instant_write": True,
                    },
                    dedup_key=f"volatile:{turn.chat_id}:{primary_claim.attribute}:{primary_claim.value[:40]}",
                    created_at=float(turn.committed_at or 0.0),
                )
                return request, {
                    "claim_count": len(claims),
                    "decision_action": "volatile_state_write",
                    "fact_scope": "short_term",
                    "volatile_state": True,
                    "fallback_used": False,
                }

        fallback_used = True
        fallback_prefix = "instant_gate_llm" if source == "instant_gate_llm" else "instant_gate"
        request = MemoryWriteRequest(
            source=source,
            kind="fact",
            session_id=str(turn.chat_id or ""),
            sender_id=str(turn.sender_id or ""),
            persona_id=str(turn.persona_id or ""),
            content=str(extracted_fact or raw_text or ""),
            summary=str(extracted_fact or raw_text or "")[:240],
            importance=0.85 if source == "instant_gate" else 0.8,
            confidence=0.9 if source == "instant_gate" else 0.72,
            metadata={
                "source": f"{source}_legacy_fallback",
                "fact_scope": "medium_term",
                "metadata_hydrated": False,
                "turn_id": str(turn.turn_id or ""),
                "gate_category": category,
                "instant_write": True,
                "authority_eav": True,
            },
            dedup_key=f"{fallback_prefix}:{turn.chat_id}:{category}:{str(extracted_fact or raw_text or '')[:60]}",
            created_at=float(turn.committed_at or 0.0),
        )
        return request, {
            "claim_count": len(claims or []),
            "decision_action": "legacy_fallback",
            "fact_scope": "medium_term",
            "volatile_state": False,
            "fallback_used": fallback_used,
        }

    @staticmethod
    def memory_lane_key(chat_id: str) -> LaneKey:
        lane_scope = str(chat_id or "").strip()
        if lane_scope and lane_scope != "global":
            return LaneKey(subsystem="bg", task_family="memory", scope_id=lane_scope, scope_kind="chat")
        logger.warning("[InstantMemoryGate] global scope fallback engaged; expected concrete chat/session id")
        return LaneKey(subsystem="bg", task_family="memory", scope_id="global", scope_kind="global")

    async def run_llm_backfill(self, turn: CommittedMemoryTurn) -> InstantGateResult:
        gateway = getattr(self, "gateway", None)
        if not gateway or not hasattr(gateway, "call_data_process_task") or not hasattr(self.engine, "write_service"):
            return InstantGateResult()
        await self._observe(turn, "backfill_started", summary="instant gate llm backfill started")
        if self.prompt_registry is None:
            return await self._run_llm_backfill_legacy(turn)

        envelope = self.prompt_registry.render_template(
            PromptTemplateId.MEMORY_INSTANT_BACKFILL,
            {
                "user_msg": str(turn.user_text or ""),
                "ai_msg": str(turn.assistant_text or "")[:200],
            },
        )
        try:
            response = await gateway.call_data_process_task(
                prompt=envelope.prompt,
                system_prompt=envelope.system_prompt,
                is_json=True,
                lane_key=self.memory_lane_key(turn.chat_id),
                base_origin="",
                template_envelope=envelope,
            )
        except TypeError:
            response = await gateway.call_data_process_task(
                envelope.prompt,
                system_prompt=envelope.system_prompt,
                lane_key=self.memory_lane_key(turn.chat_id),
                base_origin="",
            )
        except Exception as exc:
            logger.debug(f"[InstantMemoryGate] instant llm backfill degraded: {exc}")
            await self._observe(turn, "backfill_failed", level="error", reason="prompt_backfill_failed", summary=str(exc))
            return InstantGateResult()
        return await self._consume_llm_backfill_response(turn, response)

    async def _run_llm_backfill_legacy(self, turn: CommittedMemoryTurn) -> InstantGateResult:
        prompt = (
            "这轮对话是否有值得长期记住的一条关键信息？返回JSON "
            '{"worth": bool, "fact": "..."}。\n'
            f"用户: {turn.user_text}\n"
            f"助手: {turn.assistant_text}"
        )
        try:
            response = await self.gateway.call_data_process_task(
                prompt=prompt,
                is_json=True,
                lane_key=self.memory_lane_key(turn.chat_id),
                base_origin="",
            )
        except Exception as exc:
            logger.debug(f"[InstantMemoryGate] legacy llm backfill degraded: {exc}")
            await self._observe(turn, "backfill_failed", level="error", reason="legacy_backfill_failed", summary=str(exc))
            return InstantGateResult()
        return await self._consume_llm_backfill_response(turn, response)

    async def _consume_llm_backfill_response(self, turn: CommittedMemoryTurn, response: Any) -> InstantGateResult:
        try:
            if isinstance(response, str):
                response = json.loads(response)
        except Exception:
            return InstantGateResult()
        if not isinstance(response, dict) or not bool(response.get("worth")):
            await self._observe(turn, "backfill_skipped", reason="worth_false", summary="llm backfill judged not worth memorizing")
            return InstantGateResult()
        fact = str(response.get("fact") or "").strip()
        if len(fact) < 4:
            await self._observe(turn, "backfill_skipped", reason="fact_too_short", summary="llm backfill fact too short")
            return InstantGateResult()
        request, payload = await self._build_split_write_request(
            source="instant_gate_llm",
            raw_text=str(turn.user_text or ""),
            extracted_fact=fact,
            turn=turn,
            category="llm_backfill",
        )
        memory_id = await self.engine.write_service.write(request)
        await self._observe(
            turn,
            "backfill_success",
            summary=fact[:120],
            memory_id=str(memory_id or ""),
            payload=payload,
        )
        return InstantGateResult(
            hit=bool(memory_id),
            memory_id=str(memory_id or ""),
            category="llm_backfill",
            skip_backfill=True,
        )

    async def _observe(
        self,
        turn: CommittedMemoryTurn,
        stage: str,
        *,
        level: str = "info",
        reason: str = "",
        summary: str = "",
        memory_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        observer = getattr(self.engine, "memory_observer", None)
        if observer is None or not hasattr(observer, "record"):
            return
        try:
            await observer.record(
                chat_id=turn.chat_id,
                component="instant_gate",
                stage=stage,
                level=level,
                turn_id=turn.turn_id,
                memory_id=memory_id,
                reason=reason,
                summary=summary,
                payload=payload or {},
            )
        except Exception:
            return


__all__ = ["InstantMemoryGate"]
