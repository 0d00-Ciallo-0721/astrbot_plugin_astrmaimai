from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

from ...infrastructure.gateway.json_utils import parse_json_contract
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.background_task_budget import BackgroundTaskBudget
from ..dedup import normalize_jargon_term
from .jargon_results import JargonEnrichmentResult


class JargonEnricher:
    def __init__(self, gateway, config=None, background_task_budget=None):
        self.gateway = gateway
        self.config = config if config else getattr(gateway, "config", None)
        self.background_task_budget = background_task_budget or BackgroundTaskBudget()

    @staticmethod
    def _reflect_lane(group_id: str) -> LaneKey:
        return LaneKey(subsystem="bg", task_family="jargon", scope_id=group_id or "global", scope_kind="global")

    @staticmethod
    def _normalize_review_status(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized == "active":
            return "review_pending"
        if normalized in {"review_pending", "pending_human"}:
            return "review_pending"
        if normalized == "rejected":
            return normalized
        return "review_pending"

    @staticmethod
    def _confidence(value: Any, fallback: float = 0.55) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = float(fallback)
        return max(0.0, min(parsed, 1.0))

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
                    "source_message_ids": list(item.get("source_message_ids") or [])[:12],
                    "context_windows": list(item.get("context_windows") or [])[:4],
                    "reply_relations": list(item.get("reply_relations") or [])[:8],
                    "source_spans": list(item.get("source_spans") or [])[:16],
                    "definition_hints": list(item.get("definition_hints") or [])[:4],
                    "canonical_form": str(item.get("canonical_form") or item.get("content") or ""),
                    "existing_identity": bool(item.get("existing_identity", False)),
                    "explicit_definition": bool(item.get("explicit_definition", False)),
                    "support_count": int(item.get("support_count") or 0),
                    "contributor_count": int(item.get("contributor_count") or 0),
                }
            )
        prompt = (
            "判断每个候选是否是真正的聊天黑话。黑话可以是词、短语或一句凝聚大量语境的句子；"
            "同一词形在不同语境可以有多个独立含义，必须按证据分别输出 senses。"
            "只允许增强已有证据，不得发明含义。口癖、语气词、句尾习惯、颜文字、回复节奏属于表达学习，必须拒绝；"
            "人名/昵称、作品名等专有名词，普通词汇，命令名，机器人或插件输出也必须拒绝。"
            "图片转述和相邻消息只用于理解语境，不能当作候选词由人说过的证据。"
            "每个义项必须用 supported_by 引用真实消息ID；证据不足时保留待审，不要硬编。"
            "返回 JSON：{\"items\":[{\"index\":1,\"canonical_term\":\"...\","
            "\"senses\":[{\"meaning\":\"...\",\"scene\":\"...\",\"confidence\":0.0,"
            "\"supported_by\":[\"真实消息ID\"],\"contradicted_by\":[\"真实消息ID\"]}],"
            "\"confidence\":0.0,\"is_jargon\":true,\"term_type\":\"jargon|expression_style|proper_name|common_word|plugin_output\","
            "\"semantic_novelty\":true,\"evidence_sufficient\":true,"
            "\"supported_by\":[\"真实消息ID\"],\"contradicted_by\":[\"真实消息ID\"],"
            "\"review_status\":\"review_pending|pending_human|rejected\","
            "\"aliases\":[\"...\"],\"examples\":[\"...\"]}]}\n"
            f"候选：{json.dumps(prompt_items, ensure_ascii=False)}"
        )
        try:
            async def _call():
                return await self.gateway.call_data_process_task(
                    prompt=prompt,
                    is_json=True,
                    lane_key=self._reflect_lane(group_id),
                    base_origin=group_id,
                )

            result = await self.background_task_budget.run(
                _call,
                task_name="learning.jargon_enrichment",
                scope_id=group_id,
                defer_release_on_timeout=True,
            )
            parsed = parse_json_contract(
                result,
                required_keys=("items",),
                field_types={"items": list},
                allow_extra_keys=False,
                allow_naked_members=True,
            )
            if not parsed.schema_valid:
                # Preserve the legacy public status for callers that classify
                # malformed model JSON as retryable invalid_json; the parser's
                # stricter schema_invalid terminal status remains in reason.
                compatibility_status = (
                    "invalid_json"
                    if parsed.terminal_status in {"schema_invalid", "parse_failed"}
                    else parsed.terminal_status
                )
                return self._failed_result(
                    group_id,
                    candidates,
                    status=compatibility_status,
                    reason="response_schema_invalid",
                    error_type="InvalidStructuredResponse",
                )
            result = parsed.value
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
            confidence = self._confidence(
                extra.get("confidence"),
                self._confidence(payload.get("activation_score"), 0.55),
            )
            raw_senses = extra.get("senses") if isinstance(extra.get("senses"), list) else []
            if not raw_senses and (extra.get("meaning") or payload.get("meaning")):
                raw_senses = [extra]
            payload["confidence"] = confidence
            payload["is_jargon"] = bool(extra.get("is_jargon", False))
            payload["term_type"] = str(extra.get("term_type") or ("jargon" if payload["is_jargon"] else "common_word")).strip()
            payload["semantic_novelty"] = bool(extra.get("semantic_novelty", payload["is_jargon"]))
            valid_surfaces = {
                normalize_jargon_term(value)
                for value in [
                    payload.get("content"),
                    payload.get("canonical_form"),
                    *(span.get("text") for span in (payload.get("source_spans") or []) if isinstance(span, dict)),
                ]
                if normalize_jargon_term(str(value or ""))
            }
            payload["aliases"] = [
                str(alias).strip()
                for alias in extra.get("aliases", [])
                if normalize_jargon_term(str(alias or "")) in valid_surfaces
            ][:5]
            model_examples = [str(example).strip() for example in extra.get("examples", []) if str(example).strip()]
            payload["model_examples"] = list(dict.fromkeys([*(payload.get("model_examples") or []), *model_examples]))[:5]
            payload["examples"] = list(payload.get("source_examples") or payload.get("examples") or [])[:12]
            valid_source_ids = {
                str(source_id)
                for source_id in (payload.get("source_message_ids") or [])
                if str(source_id).strip()
            }
            valid_context_ids = {
                str(message.get("message_id") or "")
                for window in (payload.get("context_windows") or [])
                if isinstance(window, dict)
                for message in (window.get("messages") or [])
                if isinstance(message, dict) and str(message.get("message_id") or "").strip()
            }
            citation_ids = valid_source_ids | valid_context_ids
            proposed_senses: list[dict[str, Any]] = []
            for raw_sense in raw_senses[:6]:
                if not isinstance(raw_sense, dict):
                    continue
                meaning = str(raw_sense.get("meaning") or "").strip()
                if not meaning:
                    continue
                supported_by = list(dict.fromkeys(
                    str(source_id) for source_id in (raw_sense.get("supported_by") or extra.get("supported_by") or [])
                    if str(source_id) in valid_source_ids
                ))
                contradicted_by = list(dict.fromkeys(
                    str(source_id) for source_id in (raw_sense.get("contradicted_by") or extra.get("contradicted_by") or [])
                    if str(source_id) in citation_ids
                ))
                proposed_senses.append({
                    "meaning": meaning,
                    "scene": str(raw_sense.get("scene") or extra.get("scene") or "").strip(),
                    "confidence": self._confidence(raw_sense.get("confidence"), confidence),
                    "supported_by": supported_by,
                    "contradicted_by": contradicted_by,
                    "support_count": len(supported_by),
                    "contradiction_count": len(contradicted_by),
                    "review_status": self._normalize_review_status(raw_sense.get("review_status") or extra.get("review_status") or ""),
                })
            payload["proposed_senses"] = proposed_senses
            primary = proposed_senses[0] if proposed_senses else {}
            payload["meaning"] = str(primary.get("meaning") or "").strip()
            payload["scene"] = str(primary.get("scene") or "").strip()
            payload["supported_by"] = list(primary.get("supported_by") or [])
            payload["contradicted_by"] = list(primary.get("contradicted_by") or [])
            payload["support_count"] = len(payload["supported_by"])
            payload["contradiction_count"] = len(payload["contradicted_by"])
            payload["evidence_sufficient"] = bool(
                extra.get("evidence_sufficient", False)
                and payload["support_count"] >= (1 if payload.get("explicit_definition") else 2)
            )
            payload["review_status"] = self._normalize_review_status(extra.get("review_status") or "")
            if payload["contradiction_count"] or not payload["evidence_sufficient"]:
                payload["review_status"] = "review_pending"
            if (
                payload["is_jargon"]
                and payload["meaning"]
                and payload["term_type"] == "jargon"
                and proposed_senses
            ):
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
