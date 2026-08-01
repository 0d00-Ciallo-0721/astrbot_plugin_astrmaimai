from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ...infrastructure.context_economy.prompt_templates import PromptTemplateId
from ...infrastructure.runtime.lane_manager import LaneKey
from ..contracts.memory_query import MemoryClaim
from .claim_rules import (
    ANXIETY_KEYWORDS,
    ASCII_CORRECTION_HINTS,
    ASCII_SHORT_TERM_HINTS,
    SERVER_COUNT_PATTERN,
    SERVER_KEYWORDS,
)
from .claim_rules_zh import (
    ZH_ANXIETY_KEYWORDS,
    ZH_CORRECTION_HINTS,
    ZH_SERVER_COUNT_PATTERN,
    ZH_SERVER_KEYWORDS,
    ZH_SHORT_TERM_HINTS,
)


@dataclass(slots=True)
class ResolutionDecision:
    action: str
    claims: list[MemoryClaim]
    truth_score: float = 0.0
    correction_strength: float = 0.0
    metadata: dict[str, Any] | None = None


class MemoryClaimExtractor:
    _IDENTITY_PATTERN = re.compile(r"(?:我叫|我的名字(?:是|叫))\s*([^\s，。！？?]{1,20})")
    _PREFERENCE_PATTERN = re.compile(r"我(不喜欢|讨厌|喜欢|最爱|偏好|爱吃|不吃)\s*(.{1,40})")

    def __init__(self, gateway=None):
        self.gateway = gateway
        self.prompt_registry = getattr(getattr(gateway, "context_economy", None), "templates", None) if gateway else None

    @staticmethod
    def _lane_key(
        *,
        lane_scope_id: str,
        lane_scope_kind: str,
        subject_id: str,
        turn_id: str,
    ) -> LaneKey:
        explicit_scope = str(lane_scope_id or "").strip()
        if explicit_scope:
            return LaneKey(
                subsystem="bg",
                task_family="memory",
                scope_id=explicit_scope,
                scope_kind=str(lane_scope_kind or "chat").strip() or "chat",
            )
        subject_scope = str(subject_id or "").strip()
        if subject_scope:
            return LaneKey(
                subsystem="bg",
                task_family="memory",
                scope_id=f"subject:{subject_scope}",
                scope_kind="user",
            )
        turn_scope = str(turn_id or "").strip()
        if turn_scope:
            return LaneKey(
                subsystem="bg",
                task_family="memory",
                scope_id=f"turn:{turn_scope}",
                scope_kind="turn",
            )
        return LaneKey(
            subsystem="bg",
            task_family="memory",
            scope_id="global",
            scope_kind="global",
        )

    def _rule_extract(self, *, user_text: str, subject_id: str, turn_id: str) -> list[MemoryClaim]:
        text = str(user_text or "").strip()
        if not text:
            return []
        lowered = text.lower()
        claims: list[MemoryClaim] = []
        correction = any(hint in lowered for hint in ASCII_CORRECTION_HINTS) or any(hint in text for hint in ZH_CORRECTION_HINTS)
        short_term = any(hint in lowered for hint in ASCII_SHORT_TERM_HINTS) or any(hint in text for hint in ZH_SHORT_TERM_HINTS)

        identity_match = self._IDENTITY_PATTERN.search(text)
        if identity_match:
            claims.append(
                MemoryClaim(
                    subject_id=subject_id,
                    entity="identity",
                    attribute="display_name",
                    value=identity_match.group(1).strip(),
                    certainty=0.95,
                    fact_scope="medium_term",
                    source_text=text,
                    evidence_turn_id=turn_id,
                )
            )
        preference_match = self._PREFERENCE_PATTERN.search(text)
        if preference_match:
            verb = preference_match.group(1)
            value = preference_match.group(2).strip(" ，。！？?")
            if value:
                claims.append(
                    MemoryClaim(
                        subject_id=subject_id,
                        entity="preference",
                        attribute="dislike" if verb in {"不喜欢", "讨厌", "不吃"} else "like",
                        value=value,
                        polarity="deny" if verb in {"不喜欢", "讨厌", "不吃"} else "affirm",
                        certainty=0.9,
                        fact_scope="medium_term",
                        source_text=text,
                        evidence_turn_id=turn_id,
                    )
                )

        if any(keyword in lowered for keyword in SERVER_KEYWORDS) or any(keyword in text for keyword in ZH_SERVER_KEYWORDS):
            # OPT-15/ML-11: 演示场景遗留规则收紧——要求第一人称占有句式，
            # 否则聊 MC 服务器人数（"服务器 100 人在线"）会被记成用户资产
            possession_markers = ("我有", "我的", "my ", "i have", "i own")
            has_possession = any(marker in text or marker in lowered for marker in possession_markers)
            match = SERVER_COUNT_PATTERN.search(text) or ZH_SERVER_COUNT_PATTERN.search(text)
            # 纠错语境（"说错了/不是X是Y"）本身即指向用户自述事实，等同占有证据
            value = match.group(1) if (match and (has_possession or correction)) else ""
            if value:
                claims.append(
                    MemoryClaim(
                        subject_id=subject_id,
                        entity="asset",
                        attribute="server_count",
                        value=value,
                        polarity="affirm",
                        certainty=0.85 if correction else 0.7,
                        is_correction=correction,
                        fact_scope="medium_term",
                        source_text=text,
                        evidence_turn_id=turn_id,
                    )
                )
        if short_term and (any(keyword in lowered for keyword in ANXIETY_KEYWORDS) or any(keyword in text for keyword in ZH_ANXIETY_KEYWORDS)):
            claims.append(
                MemoryClaim(
                    subject_id=subject_id,
                    entity="emotion",
                    attribute="anxiety_state",
                    value="anxious",
                    polarity="affirm",
                    certainty=0.8,
                    is_correction=correction,
                    fact_scope="short_term",
                    source_text=text,
                    evidence_turn_id=turn_id,
                )
            )
        return claims

    async def extract(
        self,
        *,
        user_text: str,
        assistant_text: str = "",
        subject_id: str = "",
        turn_id: str = "",
        context_hint: str = "",
        allow_llm: bool = True,
        lane_scope_id: str = "",
        lane_scope_kind: str = "",
    ) -> list[MemoryClaim]:
        claims = self._rule_extract(user_text=user_text, subject_id=subject_id, turn_id=turn_id)
        # OPT-06/ML-06: allow_llm=False 时只做规则抽取——发送后内联路径不得同步等
        # 5-44s 的 LLM（拖长 turn 与同 chat 后续处理），LLM 精炼交给后台 backfill
        if claims or not allow_llm or not self.gateway or self.prompt_registry is None:
            return claims
        try:
            envelope = self.prompt_registry.render_template(
                PromptTemplateId.MEMORY_CONFLICT_CLAIM_EXTRACTION,
                {
                    "user_text": user_text,
                    "assistant_text": assistant_text,
                    "subject_id": subject_id,
                    "turn_id": turn_id,
                    "context_hint": context_hint,
                },
            )
            response = await self.gateway.call_data_process_task(
                prompt=envelope.prompt,
                system_prompt=envelope.system_prompt,
                is_json=True,
                lane_key=self._lane_key(
                    lane_scope_id=lane_scope_id,
                    lane_scope_kind=lane_scope_kind,
                    subject_id=subject_id,
                    turn_id=turn_id,
                ),
                base_origin="",
                template_envelope=envelope,
            )
            if isinstance(response, str):
                response = json.loads(response)
        except Exception:
            logger.debug("[MemoryClaimExtractor] LLM claim extraction failed", exc_info=True)
            return []
        result: list[MemoryClaim] = []
        for item in list((response or {}).get("claims", []) or []):
            if not isinstance(item, dict):
                continue
            result.append(
                MemoryClaim(
                    subject_id=str(item.get("subject_id") or subject_id or "").strip(),
                    entity=str(item.get("entity") or "").strip(),
                    attribute=str(item.get("attribute") or "").strip(),
                    value=str(item.get("value") or "").strip(),
                    polarity=str(item.get("polarity") or "affirm").strip(),
                    certainty=float(item.get("certainty") or 0.5),
                    is_correction=bool(item.get("is_correction")),
                    fact_scope=str(item.get("fact_scope") or "medium_term").strip(),
                    source_text=str(item.get("source_text") or user_text).strip(),
                    evidence_turn_id=str(item.get("evidence_turn_id") or turn_id).strip(),
                )
            )
        return result


class MemoryConflictResolver:
    def resolve(self, claims: list[MemoryClaim]) -> ResolutionDecision:
        if not claims:
            return ResolutionDecision(action="plain_memory_write", claims=[], metadata={})
        best = max(claims, key=lambda item: (item.certainty, item.is_correction))
        truth_score = best.certainty
        if best.is_correction:
            truth_score += 0.15
        if best.fact_scope == "short_term":
            return ResolutionDecision(
                action="volatile_state_write",
                claims=claims,
                truth_score=min(truth_score, 1.0),
                correction_strength=0.7 if best.is_correction else 0.3,
                metadata={"fact_scope": "short_term", "volatile_state": True},
            )
        if best.fact_scope in {"permanent", "medium_term"} and truth_score >= 0.85:
            return ResolutionDecision(
                action="authority_override",
                claims=claims,
                truth_score=min(truth_score, 1.0),
                correction_strength=0.9 if best.is_correction else 0.6,
                metadata={"fact_scope": best.fact_scope, "volatile_state": False},
            )
        if best.is_correction and truth_score >= 0.8:
            return ResolutionDecision(
                action="authority_override",
                claims=claims,
                truth_score=min(truth_score, 1.0),
                correction_strength=0.9,
                metadata={"fact_scope": best.fact_scope, "volatile_state": False},
            )
        return ResolutionDecision(
            action="plain_memory_write",
            claims=claims,
            truth_score=min(truth_score, 1.0),
            correction_strength=0.4 if best.is_correction else 0.1,
            metadata={"fact_scope": best.fact_scope, "volatile_state": False},
        )


__all__ = ["MemoryClaimExtractor", "MemoryConflictResolver", "ResolutionDecision"]
