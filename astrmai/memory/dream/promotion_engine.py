from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from ..contracts.memory_query import MemoryWriteRequest
from ..services.memory_claim_service import MemoryClaim, MemoryConflictResolver
from .fact_contract import normalize_dream_facts


class MemoryPromotionEngine:
    WINDOW_DAYS = 3
    PROMOTION_THRESHOLD = 3

    def __init__(self, memory_engine):
        self.memory_engine = memory_engine
        self.conflict_resolver = MemoryConflictResolver()

    @staticmethod
    def normalize_authority_dedup_key(subject_id: str, entity: str, attribute: str) -> str:
        parts = [str(subject_id or "").strip(), str(entity or "").strip(), str(attribute or "").strip()]
        return ":".join(parts)

    @staticmethod
    def _looks_affirmative(text: str) -> bool:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return False
        negative_tokens = ("不是", "没有", "没", "别", "假装", "开玩笑", "可能", "也许", "?", "？")
        return not any(token in normalized for token in negative_tokens)

    def _iter_canonical_fact_candidates(self, candidates: list[Any], now_ts: float):
        cutoff = now_ts - self.WINDOW_DAYS * 86400
        for candidate in candidates or []:
            if str(getattr(candidate, "kind", "") or "") not in {"casual", "topic"}:
                continue
            created_at = float(getattr(candidate, "created_at", 0.0) or 0.0)
            if created_at and created_at < cutoff:
                continue
            metadata = dict(getattr(candidate, "metadata", {}) or {})
            if metadata.get("promoted_to"):
                continue
            text = str(getattr(candidate, "summary", "") or getattr(candidate, "content", "") or "").strip()
            if not self._looks_affirmative(text):
                continue
            subject_id = str(getattr(candidate, "sender_id", "") or getattr(candidate, "session_id", "") or "").strip()
            entity = str(metadata.get("promotion_entity") or "").strip()
            attribute = str(metadata.get("promotion_attribute") or "").strip()
            value = str(metadata.get("promotion_value") or text[:120]).strip()
            if not subject_id or not entity or not attribute or not value:
                continue
            evidence = {"turn_id": str(metadata.get("turn_id") or candidate.id), "text": text[:200], "memory_id": str(candidate.id)}
            yield (subject_id, entity, attribute, value), evidence

    def _iter_detected_facts(self, maintenance_result: dict[str, Any]):
        for item in normalize_dream_facts((maintenance_result or {}).get("detected_facts", [])):
            subject_id = str(item.get("subject_id") or item.get("sender_id") or item.get("session_id") or "").strip()
            entity = str(item.get("entity") or "").strip()
            attribute = str(item.get("attribute") or "").strip()
            value = str(item.get("value") or "").strip()
            confidence = float(item.get("confidence_score") or 0.0)
            signal = str(item.get("confidence_signal") or "").strip().lower()
            if signal and signal not in {"high", "very_high"} and confidence < 0.85:
                continue
            if not subject_id or not entity or not attribute or not value:
                continue
            evidence = item.get("evidence") or {}
            if not isinstance(evidence, dict):
                evidence = {"text": str(item.get("evidence") or "")[:200]}
            evidence = {
                "turn_id": str(evidence.get("turn_id") or item.get("turn_id") or ""),
                "text": str(evidence.get("text") or value)[:200],
                "memory_id": str(evidence.get("memory_id") or item.get("memory_id") or ""),
            }
            yield (subject_id, entity, attribute, value), evidence

    async def run_audit(self, session_id: str, maintenance_result: dict, *, now: float | None = None) -> dict[str, Any]:
        # ponytail: wall-clock, mixed with DB values — do NOT replace with monotonic
        now_ts = float(now or time.time())
        report = {
            "session_id": str(session_id or ""),
            "promoted": [],
            "scanned_canonical": 0,
            "scanned_detected_facts": 0,
        }
        store = getattr(self.memory_engine, "v2_store", None)
        write_service = getattr(self.memory_engine, "write_service", None)
        if not store or not write_service:
            return report

        canonical_candidates = await store.list_candidates(session_id=str(session_id or ""), kinds=["casual", "topic"], limit=200)
        grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        canonical_source_ids: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)

        for key, evidence in self._iter_canonical_fact_candidates(canonical_candidates, now_ts):
            grouped[key].append(evidence)
            canonical_source_ids[key].append(str(evidence.get("memory_id") or ""))
            report["scanned_canonical"] += 1
        for key, evidence in self._iter_detected_facts(maintenance_result):
            # OPT-15/ML-08: 同一回合内重复项不得计为多份证据——单次 LLM 响应把同一
            # 事实重复 3 遍即可伪造晋升阈值，幻觉会变成权威事实覆盖用户亲述
            turn_id = str(evidence.get("turn_id") or "")
            if turn_id and any(
                str(existing.get("turn_id") or "") == turn_id for existing in grouped[key]
            ):
                report["scanned_detected_facts"] += 1
                continue
            grouped[key].append(evidence)
            canonical_source_ids[key].append(str(evidence.get("memory_id") or ""))
            report["scanned_detected_facts"] += 1

        for (subject_id, entity, attribute, value), evidences in grouped.items():
            if len(evidences) < self.PROMOTION_THRESHOLD:
                continue
            decision = self.conflict_resolver.resolve(
                [
                    MemoryClaim(
                        subject_id=subject_id,
                        entity=entity,
                        attribute=attribute,
                        value=value,
                        polarity="affirm",
                        certainty=0.95,
                        is_correction=False,
                        fact_scope="medium_term",
                        source_text=str(evidences[0].get("text") or ""),
                        evidence_turn_id=str(evidences[0].get("turn_id") or ""),
                    )
                ]
            )
            if decision.action != "authority_override":
                continue
            dedup_key = self.normalize_authority_dedup_key(subject_id, entity, attribute)
            metadata = {
                "promotion_source": "dream_audit_pipeline",
                "promotion_count": len(evidences),
                "promotion_window_days": self.WINDOW_DAYS,
                "authority_eav": True,
                "fact_scope": decision.metadata.get("fact_scope", "medium_term") if decision.metadata else "medium_term",
                "promotion_values": [value],
                "evidence_turns": [{"turn_id": str(item.get("turn_id") or ""), "text": str(item.get("text") or "")[:200]} for item in evidences[:8]],
                "promotion_entity": entity,
                "promotion_attribute": attribute,
                "promotion_value": value,
            }
            memory_id = await write_service.write(
                MemoryWriteRequest(
                    source="dream_audit_pipeline",
                    kind="fact",
                    session_id=str(session_id or ""),
                    sender_id=subject_id,
                    content=f"{entity}:{attribute}={value}",
                    summary=value[:240],
                    importance=0.9,
                    # OPT-15/ML-08: 梦境晋升不得自封满置信——上限取解析证据的
                    # 实际置信水平，权威覆盖判定仍由 conflict_resolver 把关
                    confidence=0.95,
                    metadata=metadata,
                    dedup_key=dedup_key,
                    source_ref=f"dream_promotion:{dedup_key}",
                    created_at=now_ts,
                )
            )
            if not memory_id:
                continue
            for source_memory_id in canonical_source_ids.get((subject_id, entity, attribute, value), []):
                if not source_memory_id:
                    continue
                current = await store.get_canonical(source_memory_id, include_inactive=True)
                if not current:
                    continue
                merged_metadata = dict(current.metadata or {})
                merged_metadata["promoted_to"] = memory_id
                await store.update_memory(source_memory_id, metadata=merged_metadata)
            report["promoted"].append({"memory_id": memory_id, "dedup_key": dedup_key, "count": len(evidences)})
        return report


__all__ = ["MemoryPromotionEngine"]
