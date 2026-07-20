from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger

from ...infrastructure.runtime.lane_manager import LaneKey
from .expression_results import ExpressionEnrichmentResult


class ExpressionPatternEnricher:
    def __init__(self, gateway, config=None):
        self.gateway = gateway
        self.config = config if config else getattr(gateway, "config", None)
        self.last_result = ExpressionEnrichmentResult(status="completed")

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

    @staticmethod
    def _strict_fallback_candidate(candidate: dict[str, Any]) -> bool:
        expression = str(candidate.get("expression") or "").strip()
        evidence_ids = {str(item) for item in candidate.get("evidence_message_ids", []) if str(item).strip()}
        if candidate.get("candidate_type") != "exact" or int(candidate.get("count") or 0) < 3:
            return False
        if len(evidence_ids) < 2 or not (2 <= len(expression) <= 40):
            return False
        lowered = expression.lower()
        if any(token in lowered for token in ("http://", "https://", "[图片", "[pic", "cq:")):
            return False
        if expression.startswith(("/", "!", "@")) or re.fullmatch(r"[\W\d_]+", expression):
            return False
        return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", expression))

    @staticmethod
    def _coerce_rows(result: Any) -> list[dict[str, Any]]:
        if isinstance(result, str):
            result = json.loads(result)
        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            rows = result.get("items")
        else:
            raise TypeError("expression enrichment response must be an object or list")
        if not isinstance(rows, list):
            raise TypeError("expression enrichment response.items must be a list")
        return [item for item in rows if isinstance(item, dict)]

    async def _request_rows(
        self,
        group_id: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        prompt_items = []
        for index, item in enumerate(candidates, start=1):
            prompt_items.append(
                {
                    "index": index,
                    "candidate_id": str(item.get("candidate_id") or f"index:{index}"),
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
            "Return exactly one decision for every candidate. Return JSON only: "
            "{\"items\":[{\"candidate_id\":\"...\",\"index\":1,\"decision\":\"keep|reject\","
            "\"summary\":\"...\",\"situation\":\"...\","
            "\"style\":\"...\",\"confidence\":0.0,\"review_status\":\"pending|pending_human|rejected\","
            "\"review_reason\":\"...\",\"content_samples\":[\"...\"]}]}\n"
            f"Candidates: {json.dumps(prompt_items, ensure_ascii=False)}"
        )
        result = await self.gateway.call_data_process_task(
            prompt=prompt,
            is_json=True,
            lane_key=self._lane(group_id),
            base_origin=group_id,
        )
        return self._coerce_rows(result)

    @staticmethod
    def _resolve_row(
        row: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> tuple[str, dict[str, Any]] | None:
        candidate_id = str(row.get("candidate_id") or "").strip()
        by_id = {str(item.get("candidate_id") or ""): item for item in candidates}
        if candidate_id and candidate_id in by_id:
            return candidate_id, by_id[candidate_id]
        try:
            index = int(row.get("index")) - 1
        except (TypeError, ValueError):
            return None
        if not 0 <= index < len(candidates):
            return None
        candidate = candidates[index]
        return str(candidate.get("candidate_id") or f"index:{index + 1}"), candidate

    def _enrich_payload(self, item: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any] | None:
        decision = str(extra.get("decision") or "").strip().lower()
        review_status = self._normalize_review_status(extra.get("review_status"))
        if decision == "reject" or review_status == "rejected":
            return None
        payload = dict(item)
        try:
            confidence = float(extra.get("confidence") or payload.get("activation_score") or 0.6)
        except (TypeError, ValueError):
            confidence = float(payload.get("activation_score") or 0.6)
        payload["expression"] = str(extra.get("expression") or payload.get("expression") or "").strip()
        payload["summary"] = str(extra.get("summary") or payload.get("expression") or "").strip()
        payload["situation"] = str(extra.get("situation") or payload.get("situation") or "").strip()
        payload["style"] = str(extra.get("style") or payload.get("style") or "").strip()
        payload["confidence"] = max(0.0, min(confidence, 1.0))
        payload["review_status"] = review_status
        payload["review_reason"] = str(extra.get("review_reason") or "").strip()
        model_samples = [str(sample).strip() for sample in extra.get("content_samples", []) if str(sample).strip()]
        payload["content_samples"] = list(dict.fromkeys([*(payload.get("content_samples") or []), *model_samples]))[:6]
        return payload if payload["expression"] and payload["summary"] else None

    async def enrich(self, group_id: str, candidates: list[dict[str, Any]]) -> ExpressionEnrichmentResult:
        if not candidates:
            self.last_result = ExpressionEnrichmentResult(status="completed", reason="no_candidates")
            return self.last_result

        pending = [dict(item) for item in candidates]
        for index, item in enumerate(pending, start=1):
            item.setdefault("candidate_id", f"index:{index}")
        enriched: list[dict[str, Any]] = []
        rejected_ids: set[str] = set()
        resolved_ids: set[str] = set()
        attempts = 0
        last_error_status = ""
        last_error = ""

        for attempts in range(1, 3):
            if not pending:
                break
            try:
                rows = await self._request_rows(group_id, pending)
            except json.JSONDecodeError as exc:
                last_error_status, last_error = "parse_error", str(exc)
                logger.warning(f"[ExpressionPatternEnricher] JSON parse failed attempt={attempts}: {exc}")
                continue
            except (TypeError, ValueError) as exc:
                last_error_status, last_error = "invalid_response", str(exc)
                logger.warning(f"[ExpressionPatternEnricher] invalid response attempt={attempts}: {exc}")
                continue
            except Exception as exc:
                last_error_status, last_error = "provider_error", str(exc)
                logger.warning(f"[ExpressionPatternEnricher] provider failed attempt={attempts}: {exc}")
                continue

            current_ids = {str(item.get("candidate_id")) for item in pending}
            for row in rows:
                resolved = self._resolve_row(row, pending)
                if resolved is None:
                    continue
                candidate_id, candidate = resolved
                if candidate_id not in current_ids or candidate_id in resolved_ids:
                    continue
                payload = self._enrich_payload(candidate, row)
                resolved_ids.add(candidate_id)
                if payload is None:
                    rejected_ids.add(candidate_id)
                else:
                    enriched.append(payload)
            pending = [item for item in pending if str(item.get("candidate_id")) not in resolved_ids]

        fallback: list[dict[str, Any]] = []
        still_missing: list[dict[str, Any]] = []
        for candidate in pending:
            if self._strict_fallback_candidate(candidate):
                payload = dict(candidate)
                payload.update(
                    {
                        "summary": str(payload.get("expression") or "").strip(),
                        "confidence": min(float(payload.get("activation_score") or 0.6), 0.75),
                        "review_status": "pending_human",
                        "review_reason": "LLM 增强失败，严格重复表达规则回退，需人工复核",
                    }
                )
                fallback.append(payload)
            else:
                still_missing.append(candidate)
        enriched.extend(fallback)

        missing_ids = [str(item.get("candidate_id")) for item in still_missing]
        if missing_ids:
            status = "partial" if enriched or rejected_ids else (last_error_status or "invalid_response")
            retryable = True
            reason = last_error or "enrichment response omitted candidates"
        elif not enriched and len(rejected_ids) == len(candidates):
            status, retryable, reason = "all_rejected", False, "model_rejected_all_candidates"
        elif fallback:
            status, retryable, reason = "completed_fallback", False, last_error or "strict_fallback_completed"
        else:
            status, retryable, reason = "completed", False, "all_candidates_resolved"

        self.last_result = ExpressionEnrichmentResult(
            status=status,
            items=enriched,
            input_count=len(candidates),
            returned_count=len(enriched),
            rejected_count=len(rejected_ids),
            missing_candidate_ids=missing_ids,
            retryable=retryable,
            reason=reason,
            attempts=attempts,
            fallback_count=len(fallback),
        )
        return self.last_result


__all__ = ["ExpressionPatternEnricher", "ExpressionEnrichmentResult"]
