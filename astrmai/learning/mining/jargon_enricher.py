from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

from ...infrastructure.runtime.lane_manager import LaneKey


class JargonEnricher:
    def __init__(self, gateway, config=None):
        self.gateway = gateway
        self.config = config if config else getattr(gateway, "config", None)

    @staticmethod
    def _reflect_lane(group_id: str) -> LaneKey:
        return LaneKey(subsystem="bg", task_family="jargon", scope_id=group_id or "global", scope_kind="global")

    @staticmethod
    def _normalize_review_status(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized == "active":
            return "review_pending"
        if normalized in {"review_pending", "pending_human", "rejected"}:
            return normalized
        return "review_pending"

    async def enrich(self, group_id: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not candidates:
            return []
        prompt_items = []
        for index, item in enumerate(candidates, start=1):
            prompt_items.append(
                {
                    "index": index,
                    "term": str(item.get("content") or ""),
                    "raw_context": str(item.get("raw_content") or ""),
                    "examples": list(item.get("examples") or [])[:3],
                    "count": int(item.get("count") or 1),
                }
            )
        prompt = (
            "Analyze whether each candidate is likely a group-specific jargon term. "
            "LLM should only enhance meaning and confidence; do not invent unrelated slang. "
            "Return JSON only: {\"items\": [{\"index\":1,\"meaning\":\"...\",\"scene\":\"...\","
            "\"confidence\":0.0,\"is_jargon\":true,\"review_status\":\"review_pending|pending_human|rejected\","
            "\"aliases\":[\"...\"],\"examples\":[\"...\"]}]}\n"
            f"Candidates: {json.dumps(prompt_items, ensure_ascii=False)}"
        )
        try:
            result = await self.gateway.call_data_process_task(
                prompt=prompt,
                is_json=True,
                lane_key=self._reflect_lane(group_id),
                base_origin=group_id,
            )
            if isinstance(result, str):
                result = json.loads(result)
            rows = result.get("items", []) if isinstance(result, dict) else []
        except Exception as exc:
            logger.debug(f"[JargonEnricher] enrichment degraded: {exc}")
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
            confidence = float(extra.get("confidence") or payload.get("activation_score") or 0.55)
            payload["meaning"] = str(extra.get("meaning") or payload.get("meaning") or "").strip()
            payload["scene"] = str(extra.get("scene") or "").strip()
            payload["confidence"] = max(0.0, min(confidence, 1.0))
            payload["is_jargon"] = bool(extra.get("is_jargon", True))
            payload["aliases"] = [str(alias).strip() for alias in extra.get("aliases", []) if str(alias).strip()][:5]
            model_examples = [str(example).strip() for example in extra.get("examples", []) if str(example).strip()]
            payload["examples"] = list(dict.fromkeys([*payload.get("examples", []), *model_examples]))[:5]
            payload["review_status"] = self._normalize_review_status(extra.get("review_status") or "")
            enriched.append(payload)
        return enriched


__all__ = ["JargonEnricher"]
