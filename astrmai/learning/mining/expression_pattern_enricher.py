from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

from ...infrastructure.runtime.lane_manager import LaneKey


class ExpressionPatternEnricher:
    def __init__(self, gateway, config=None):
        self.gateway = gateway
        self.config = config if config else getattr(gateway, "config", None)

    @staticmethod
    def _lane(group_id: str) -> LaneKey:
        return LaneKey(subsystem="bg", task_family="expression_pattern", scope_id=group_id or "global", scope_kind="global")

    @staticmethod
    def _normalize_review_status(value: Any) -> str:
        normalized = str(value or "pending").strip().lower()
        if normalized == "approved":
            return "pending"
        if normalized in {"pending", "pending_human", "rejected"}:
            return normalized
        return "pending"

    async def enrich(self, group_id: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not candidates:
            return []
        prompt_items = []
        for index, item in enumerate(candidates, start=1):
            prompt_items.append(
                {
                    "index": index,
                    "expression": str(item.get("expression") or ""),
                    "situation": str(item.get("situation") or ""),
                    "style": str(item.get("style") or ""),
                    "count": int(item.get("count") or 1),
                    "samples": list(item.get("content_samples") or [])[:4],
                }
            )
        prompt = (
            "Enhance these conversation expression-pattern candidates. "
            "Keep the same expression unless a very small cleanup is needed. "
            "Return JSON only: {\"items\":[{\"index\":1,\"summary\":\"...\",\"situation\":\"...\","
            "\"style\":\"...\",\"confidence\":0.0,\"review_status\":\"pending|pending_human|rejected\","
            "\"review_reason\":\"...\",\"content_samples\":[\"...\"]}]}\n"
            f"Candidates: {json.dumps(prompt_items, ensure_ascii=False)}"
        )
        try:
            result = await self.gateway.call_data_process_task(
                prompt=prompt,
                is_json=True,
                lane_key=self._lane(group_id),
                base_origin=group_id,
            )
            if isinstance(result, str):
                result = json.loads(result)
            rows = result.get("items", []) if isinstance(result, dict) else []
        except Exception as exc:
            logger.debug(f"[ExpressionPatternEnricher] enrichment degraded: {exc}")
            rows = []
        by_index = {
            int(item.get("index")): item
            for item in rows
            if isinstance(item, dict) and str(item.get("index", "")).strip()
        }
        enriched: list[dict[str, Any]] = []
        for index, item in enumerate(candidates, start=1):
            payload = dict(item)
            extra = by_index.get(index, {})
            confidence = float(extra.get("confidence") or payload.get("activation_score") or 0.6)
            payload["expression"] = str(extra.get("expression") or payload.get("expression") or "").strip()
            payload["summary"] = str(extra.get("summary") or payload.get("expression") or "").strip()
            payload["situation"] = str(extra.get("situation") or payload.get("situation") or "").strip()
            payload["style"] = str(extra.get("style") or payload.get("style") or "").strip()
            payload["confidence"] = max(0.0, min(confidence, 1.0))
            payload["review_status"] = self._normalize_review_status(extra.get("review_status"))
            payload["review_reason"] = str(extra.get("review_reason") or "").strip()
            model_samples = [str(sample).strip() for sample in extra.get("content_samples", []) if str(sample).strip()]
            payload["content_samples"] = list(dict.fromkeys([*(payload.get("content_samples") or []), *model_samples]))[:6]
            enriched.append(payload)
        return enriched


__all__ = ["ExpressionPatternEnricher"]
