from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

from ...infrastructure.runtime.lane_manager import LaneKey
from .jargon_results import JargonEnrichmentResult


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

    async def enrich(self, group_id: str, candidates: list[dict[str, Any]]) -> JargonEnrichmentResult:
        if not candidates:
            return JargonEnrichmentResult(status="completed", reason="no_input")
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
                try:
                    result = json.loads(result)
                except (TypeError, ValueError) as exc:
                    return self._failed_result(
                        group_id,
                        candidates,
                        status="invalid_json",
                        reason="response_json_decode_failed",
                        error_type=type(exc).__name__,
                    )
        except Exception as exc:
            return self._failed_result(
                group_id,
                candidates,
                status="provider_failure",
                reason="gateway_call_failed",
                error_type=type(exc).__name__,
            )
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            return self._failed_result(
                group_id,
                candidates,
                status="invalid_schema",
                reason="items_array_missing",
                error_type=type(result).__name__,
            )
        rows = result["items"]
        by_index: dict[int, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                index = int(row.get("index"))
            except (TypeError, ValueError):
                continue
            if 1 <= index <= len(candidates) and index not in by_index:
                by_index[index] = row
        if not by_index:
            return self._failed_result(
                group_id,
                candidates,
                status="invalid_schema",
                reason="no_indexed_items",
                error_type="MissingCandidateIndex",
                returned_count=len(rows),
            )
        enriched: list[dict[str, Any]] = []
        rejected_count = 0
        missing_indexes: list[int] = []
        for index, item in enumerate(candidates, start=1):
            if index not in by_index:
                missing_indexes.append(index)
                continue
            payload = dict(item)
            extra = by_index[index]
            try:
                confidence = float(extra.get("confidence") or payload.get("activation_score") or 0.55)
            except (TypeError, ValueError):
                confidence = float(payload.get("activation_score") or 0.55)
            payload["meaning"] = str(extra.get("meaning") or payload.get("meaning") or "").strip()
            payload["scene"] = str(extra.get("scene") or "").strip()
            payload["confidence"] = max(0.0, min(confidence, 1.0))
            payload["is_jargon"] = bool(extra.get("is_jargon", False))
            payload["aliases"] = [str(alias).strip() for alias in extra.get("aliases", []) if str(alias).strip()][:5]
            model_examples = [str(example).strip() for example in extra.get("examples", []) if str(example).strip()]
            payload["examples"] = list(dict.fromkeys([*payload.get("examples", []), *model_examples]))[:5]
            payload["review_status"] = self._normalize_review_status(extra.get("review_status") or "")
            if payload["is_jargon"] and payload["meaning"]:
                enriched.append(payload)
            else:
                rejected_count += 1
        if missing_indexes:
            status = "partial"
            reason = "partial_response"
        elif enriched:
            status = "completed"
            reason = "enrichment_completed"
        else:
            status = "all_rejected"
            reason = "model_rejected_all_candidates"
        result_obj = JargonEnrichmentResult(
            status=status,
            items=enriched,
            input_count=len(candidates),
            returned_count=len(by_index),
            accepted_count=len(enriched),
            rejected_count=rejected_count,
            missing_indexes=missing_indexes,
            retryable=False,
            reason=reason,
        )
        logger.info(
            f"[JargonEnricher] group={group_id or 'global'} status={status} "
            f"input={len(candidates)} returned={len(by_index)} accepted={len(enriched)} "
            f"rejected={rejected_count} missing={len(missing_indexes)}"
        )
        return result_obj

    @staticmethod
    def _failed_result(
        group_id: str,
        candidates: list[dict[str, Any]],
        *,
        status: str,
        reason: str,
        error_type: str,
        returned_count: int = 0,
    ) -> JargonEnrichmentResult:
        result = JargonEnrichmentResult(
            status=status,
            input_count=len(candidates),
            returned_count=returned_count,
            retryable=True,
            reason=reason,
            error_type=error_type,
        )
        logger.warning(
            f"[JargonEnricher] group={group_id or 'global'} status={status} input={len(candidates)} "
            f"returned={returned_count} reason={reason} error_type={error_type}"
        )
        return result


__all__ = ["JargonEnricher"]
