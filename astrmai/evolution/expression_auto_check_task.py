import json
import re
from typing import Optional

from astrbot.api import logger

from ..infra.database import DatabaseService
from ..infra.datamodels import ExpressionPattern
from ..infra.gateway import GlobalModelGateway
from ..infra.lane_manager import LaneKey


class ExpressionAutoCheckTask:
    """表达库自动审核任务。"""

    REVIEW_SYSTEM_PROMPT = (
        "你是表达库治理审核员。"
        "你需要判断某条表达模式是否适合作为长期表达习惯保留。"
        "严格返回 JSON："
        "{\"decision\":\"approved|rejected|revision_needed\","
        "\"reason\":\"简短原因\","
        "\"replacement_expression\":\"可选替代表达\","
        "\"style\":\"可选风格标签\","
        "\"weight_delta\":-0.3}"
    )

    def __init__(self, db_service: DatabaseService, gateway: GlobalModelGateway, tracker=None, config=None):
        self.db = db_service
        self.gateway = gateway
        self.tracker = tracker
        self.config = config if config else gateway.config

    async def run_once(self, group_id: Optional[str] = None) -> int:
        limit = getattr(self.config.evolution, "review_batch_size", 10)
        min_count = getattr(self.config.evolution, "review_min_count", 2)
        patterns = await self.db.list_reviewable_patterns_async(group_id=group_id, limit=limit)
        processed = 0
        for pattern in patterns:
            if int(getattr(pattern, "count", 1) or 1) < min_count:
                continue
            result = await self._review_pattern(pattern)
            if not result:
                continue
            processed += 1
            await self._apply_review(pattern, result)
        return processed

    async def _review_pattern(self, pattern: ExpressionPattern) -> Optional[dict]:
        prompt = (
            f"群聊/会话：{pattern.group_id}\n"
            f"场景：{pattern.situation}\n"
            f"表达：{pattern.expression}\n"
            f"风格：{pattern.style}\n"
            f"样例：{pattern.content_list}\n"
            f"出现次数：{pattern.count}\n"
            "请判断这条表达是否适合作为长期表达习惯。"
        )
        try:
            result = await self.gateway.call_data_process_task(
                prompt=prompt,
                system_prompt=self.REVIEW_SYSTEM_PROMPT,
                is_json=True,
                lane_key=LaneKey(subsystem="bg", task_family="reflect", scope_id=pattern.group_id or "global", scope_kind="global"),
                base_origin="",
            )
            if isinstance(result, dict):
                return result
            if isinstance(result, str):
                match = re.search(r"\{.*\}", result, re.DOTALL)
                if match:
                    return json.loads(match.group(0))
        except Exception as exc:
            logger.error(f"[ExpressionAutoCheck] 审核表达失败 #{getattr(pattern, 'id', '?')}: {exc}")
        return None

    async def _apply_review(self, pattern: ExpressionPattern, result: dict):
        decision = str(result.get("decision", "revision_needed")).strip().lower()
        reason = str(result.get("reason", "")).strip()
        replacement = str(result.get("replacement_expression", "")).strip()
        style = str(result.get("style", "")).strip() or None
        try:
            weight_delta = float(result.get("weight_delta", 0.0) or 0.0)
        except (TypeError, ValueError):
            weight_delta = 0.0

        kwargs = {
            "modified_by": "ai",
            "style": style,
            "weight_delta": weight_delta,
            "review_reason": reason or None,
        }
        if decision == "approved":
            kwargs.update({"checked": True, "rejected": False, "review_status": "approved", "review_suggestion": ""})
        elif decision == "rejected":
            kwargs.update({"checked": False, "rejected": True, "review_status": "rejected", "review_suggestion": ""})
        else:
            kwargs.update(
                {
                    "checked": False,
                    "rejected": False,
                    "review_status": "pending_human",
                    "review_suggestion": replacement or None,
                }
            )

        updated = await self.db.update_pattern_review_async(pattern.id, **kwargs)
        if decision == "revision_needed" and updated and self.tracker:
            self.tracker.queue_review_request(updated, reason=reason, replacement=replacement)
        logger.info(
            f"[ExpressionAutoCheck] 表达审核完成 #{pattern.id}: decision={decision}, "
            f"group={pattern.group_id}, reason={reason or 'n/a'}"
        )
