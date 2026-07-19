from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..contracts.memory_query import MemoryWriteRequest


@dataclass(frozen=True, slots=True)
class MemoryAdmissionDecision:
    status: str
    visibility: str
    confidence: float
    reason: str
    accepted: bool


class MemoryAdmissionService:
    """Apply the minimum semantic contract before canonical activation."""

    _GOVERNED_FACT_SOURCES = frozenset({"instant_gate", "instant_gate_llm", "memory_summary"})
    _QUESTION_PATTERN = re.compile(
        r"(?:[?？]\s*$|^(?:谁|什么|怎么|为什么|为何|哪里|哪儿|多少|几|是不是|能不能|可不可以)|"
        r"(?:吗|嘛|么|呢|不|没有|没)\s*[?？]?\s*$)"
    )
    _COMMAND_PATTERN = re.compile(
        r"^(?:请|麻烦|帮我|再帮我|给我|替我|帮忙|记得帮我|去|快去)"
        r"(?:叫|喊|问|查|找|看|发|转|提醒|告诉|联系|戳|艾特|@)"
    )
    _UNCERTAIN_PATTERN = re.compile(r"(?:可能|也许|大概|好像|听说|假如|如果|假设|开玩笑|逗你|骗你的)")

    def __init__(self, config: Any = None):
        self.config = config

    def refresh_config(self, config: Any) -> None:
        self.config = config

    def enabled(self) -> bool:
        memory_config = getattr(self.config, "memory", None)
        return bool(getattr(memory_config, "memory_quality_admission_enabled", True))

    @staticmethod
    def _clamp_confidence(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _looks_interrogative(cls, text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split())
        if not normalized:
            return False
        return bool(cls._QUESTION_PATTERN.search(normalized))

    @classmethod
    def _looks_command_like(cls, text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split())
        return bool(normalized and cls._COMMAND_PATTERN.search(normalized))

    @staticmethod
    def _has_evidence(request: MemoryWriteRequest) -> bool:
        metadata = dict(request.metadata or {})
        return bool(
            str(request.source_ref or "").strip()
            or str(metadata.get("turn_id") or "").strip()
            or str(metadata.get("evidence_turn_id") or "").strip()
            or metadata.get("evidence_message_ids")
        )

    def evaluate(self, request: MemoryWriteRequest) -> MemoryAdmissionDecision:
        confidence = self._clamp_confidence(request.confidence)
        status = str(request.status or "active").strip() or "active"
        visibility = (
            request.visibility
            if request.visibility in {"auto_and_tool", "tool_only", "maintenance_only"}
            else "auto_and_tool"
        )
        if not self.enabled() or status != "active" or bool((request.metadata or {}).get("admission_bypass")):
            return MemoryAdmissionDecision(status, visibility, confidence, "unchanged", status == "active")

        kind = str(request.kind or "memory").strip().lower()
        source = str(request.source or "unknown").strip().lower()
        text = str(request.summary or request.content or "").strip()
        if kind != "fact" or source not in self._GOVERNED_FACT_SOURCES:
            return MemoryAdmissionDecision(status, visibility, confidence, "not_governed", True)

        reason = ""
        if self._looks_interrogative(text) or self._looks_interrogative(request.content):
            reason = "interrogative_fact"
        elif self._looks_command_like(text) or self._looks_command_like(request.content):
            reason = "command_like_fact"
        elif self._UNCERTAIN_PATTERN.search(text):
            reason = "uncertain_fact"
        elif not str(request.sender_id or "").strip():
            reason = "missing_subject"
        elif not self._has_evidence(request):
            reason = "missing_evidence"
        elif bool((request.metadata or {}).get("metadata_hydrated") is False):
            reason = "unverified_fallback"

        if not reason:
            return MemoryAdmissionDecision(status, visibility, confidence, "accepted", True)
        return MemoryAdmissionDecision(
            status="review_pending",
            visibility="maintenance_only",
            confidence=min(confidence, 0.49),
            reason=reason,
            accepted=False,
        )


__all__ = ["MemoryAdmissionDecision", "MemoryAdmissionService"]
