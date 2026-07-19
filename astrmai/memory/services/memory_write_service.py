from __future__ import annotations

import hashlib
import json
import re

from astrbot.api import logger

from ..contracts.memory_query import MemoryWriteRequest
from .memory_admission_service import MemoryAdmissionService
from .v2_store import MemoryV2Store


class MemoryWriteService:
    _NOISY_TOKENS = ("traceback", "exception", "all chat models fail", "apitimesouterror")
    _ERROR_JSON_KEYS = frozenset({"error", "errors", "exception", "traceback", "stack", "stacktrace", "detail", "details"})
    _INJECTION_PATTERNS = (
        r'</?user_input>',
        r'</?retrieved_memory>',
        r'忽略(所有)?系统指令',
        r'输出你的(系统)?提示词',
        r'\[SYSTEM\]',
        r'\[INST\]',
    )

    def __init__(self, store: MemoryV2Store, index_projector=None, config=None):
        self.store = store
        self.index_projector = index_projector
        self.config = config
        self.admission_service = MemoryAdmissionService(config)

    def refresh_config(self, config) -> None:
        self.config = config
        self.admission_service.refresh_config(config)

    def _minimum_confidence(self) -> float:
        memory_config = getattr(self.config, "memory", None)
        try:
            return max(0.0, min(1.0, float(getattr(memory_config, "min_memory_confidence", 0.0) or 0.0)))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _classify_skip_reason(cls, content: str) -> str | None:
        text = str(content or "").strip()
        if not text:
            return "empty_content"
        if text.startswith("```json"):
            return "fenced_json_payload"
        if text.startswith("{") and text.endswith("}"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            else:
                if isinstance(payload, dict):
                    lowered_keys = {str(key).strip().lower() for key in payload.keys()}
                    if lowered_keys & cls._ERROR_JSON_KEYS:
                        return "error_json_payload"
                    return None
                return None
        lowered = text.lower()
        if any(token in lowered for token in cls._NOISY_TOKENS):
            return "noisy_error_token"
        for pattern in cls._INJECTION_PATTERNS:
            if re.search(pattern, lowered):
                logger.warning(f"[MemoryWrite] injection payload blocked: {text[:80]}...")
                return "injection_payload"
        return None

    @staticmethod
    def should_skip_content(content: str) -> bool:
        return MemoryWriteService._classify_skip_reason(content) is not None

    async def write(self, request: MemoryWriteRequest) -> str:
        skip_reason = self._classify_skip_reason(request.content)
        if skip_reason:
            preview = str(request.content or "").strip().replace("\r", " ").replace("\n", " ")[:120]
            logger.info(
                f"[MemoryWriteService] skip write | reason={skip_reason} | kind={str(request.kind or '')} "
                f"| session_id={str(request.session_id or '')} | content_preview={preview}"
            )
            return ""
        admission = self.admission_service.evaluate(request)
        metadata = dict(request.metadata or {})
        metadata["admission"] = {
            "accepted": admission.accepted,
            "reason": admission.reason,
            "original_status": str(request.status or "active"),
        }
        try:
            confidence = float(request.confidence if request.confidence is not None else 0.8)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = admission.confidence
        minimum_confidence = self._minimum_confidence()
        if admission.status == "active" and minimum_confidence > 0.0 and confidence < minimum_confidence:
            logger.info(
                f"[MemoryWriteService] skip write | reason=confidence_below_threshold "
                f"| confidence={confidence:.3f} | minimum={minimum_confidence:.3f} "
                f"| kind={str(request.kind or '')} | session_id={str(request.session_id or '')}"
            )
            return ""
        dedup_key = str(request.dedup_key or "").strip()
        if not dedup_key:
            digest = hashlib.sha256(
                f"{request.kind}|{request.session_id}|{request.persona_id}|{request.content}".encode("utf-8")
            ).hexdigest()[:24]
            dedup_key = f"{request.kind}:{request.session_id}:{request.persona_id}:{digest}"
        normalized = MemoryWriteRequest(
            source=str(request.source or "unknown").strip() or "unknown",
            kind=str(request.kind or "memory").strip() or "memory",
            session_id=str(request.session_id or ""),
            sender_id=str(request.sender_id or ""),
            persona_id=str(request.persona_id or ""),
            content=str(request.content or ""),
            summary=str(request.summary or ""),
            tags=list(request.tags or []),
            importance=float(request.importance or 0.5),
            confidence=confidence,
            metadata=metadata,
            dedup_key=dedup_key,
            source_ref=str(request.source_ref or ""),
            visibility=admission.visibility,
            status=admission.status,
            created_at=float(request.created_at or 0.0),
        )
        upsert_result = await self.store.upsert(normalized)
        memory_id = getattr(upsert_result, "memory_id", "") or str(upsert_result.get("memory_id") or "")
        superseded_old_ids = getattr(upsert_result, "superseded_old_ids", []) or list(upsert_result.get("superseded_old_ids") or [])
        new_record_is_superseded = bool(
            getattr(upsert_result, "new_record_is_superseded", False) or upsert_result.get("new_record_is_superseded", False)
        )
        if self.index_projector and superseded_old_ids:
            await self.index_projector.cleanup_deleted(superseded_old_ids)
        if self.index_projector and memory_id and not new_record_is_superseded:
            try:
                projected = await self.index_projector.project(memory_id=memory_id, request=normalized)
                if projected is False:
                    logger.warning(f"[MemoryWrite] index projection pending repair for {memory_id}")
            except Exception as exc:
                logger.warning(f"[MemoryWrite] index projection failed for {memory_id}: {exc}")
        return memory_id


__all__ = ["MemoryWriteService"]
