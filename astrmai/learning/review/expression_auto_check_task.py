import time
from time import monotonic
from typing import Optional

from astrbot.api import logger

from ...infrastructure.persistence import DatabaseService
from ...infrastructure.persistence import ExpressionPattern
from ...infrastructure.gateway import GlobalModelGateway
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.gateway.json_utils import parse_json_contract
from ...infrastructure.runtime.background_task_budget import BackgroundTaskBudget


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

    def __init__(self, db_service: DatabaseService, gateway: GlobalModelGateway, tracker=None, config=None, background_task_budget=None):
        self.db = db_service
        self.gateway = gateway
        self.tracker = tracker
        self.config = config if config else gateway.config
        self.background_task_budget = background_task_budget or BackgroundTaskBudget()
        self._last_run_at: dict[str, float] = {}

    def refresh_config(self, config) -> None:
        self.config = config

    async def run_once(self, group_id: Optional[str] = None, *, force: bool = False) -> int:
        now = monotonic()
        scope = str(group_id or "__global__")
        min_interval = float(getattr(self.config.evolution, "review_runner_min_interval_sec", 21600) or 21600)
        last_run_at = float(self._last_run_at.get(scope, 0.0) or 0.0)
        # The six-hour governance interval is a hard business cooldown.  The
        # ``force`` flag may bypass candidate-count gates, but never starts a
        # second audit for the same scope during the cooldown window.
        if last_run_at > 0.0 and now - last_run_at < min_interval:
            return 0
        self._last_run_at[scope] = now
        # ponytail: prune unbounded _last_run_at, keep most recent 250 entries
        if len(self._last_run_at) > 500:
            sorted_keys = sorted(self._last_run_at, key=self._last_run_at.get, reverse=True)[:250]
            self._last_run_at = {k: self._last_run_at[k] for k in sorted_keys}
        limit = getattr(self.config.evolution, "review_batch_size", 10)
        min_count = getattr(self.config.evolution, "review_min_count", 2)
        service = getattr(getattr(self.db, "memory_engine", None), "expression_pattern_service", None)
        if service and hasattr(service, "list_reviewable_patterns"):
            patterns = await service.list_reviewable_patterns(group_id=group_id, limit=limit)
        else:
            patterns = await self.db.list_reviewable_patterns_async(group_id=group_id, limit=limit)
        processed = 0
        for pattern in patterns:
            if str(getattr(pattern, "review_status", "") or "").strip().lower() == "pending_human":
                continue
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
            async def _call():
                return await self.gateway.call_data_process_task(
                    prompt=prompt,
                    system_prompt=self.REVIEW_SYSTEM_PROMPT,
                    is_json=True,
                    lane_key=LaneKey(subsystem="bg", task_family="reflect", scope_id=pattern.group_id or "global", scope_kind="global"),
                    base_origin="",
                )

            result = await self.background_task_budget.run(
                _call,
                task_name="governance.expression_check",
                scope_id=str(pattern.group_id or "GLOBAL"),
                defer_release_on_timeout=True,
            )
            parsed = parse_json_contract(
                result,
                required_keys=("decision",),
                optional_keys=("reason", "replacement_expression", "style", "weight_delta"),
                field_types={
                    "decision": str,
                    "reason": str,
                    "replacement_expression": str,
                    "style": str,
                    "weight_delta": (int, float),
                },
                allow_extra_keys=False,
                allow_naked_members=True,
            )
            return dict(parsed.value) if parsed.schema_valid else None
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

        service = getattr(getattr(self.db, "memory_engine", None), "expression_pattern_service", None)
        if service and hasattr(service, "update_review"):
            updated = await service.update_review(str(pattern.id), **kwargs)
        else:
            updated = await self.db.update_pattern_review_async(pattern.id, **kwargs)
        if decision == "revision_needed" and updated and self.tracker:
            self.tracker.queue_review_request(updated, reason=reason, replacement=replacement)
        logger.info(
            f"[ExpressionAutoCheck] 表达审核完成 #{pattern.id}: decision={decision}, "
            f"group={pattern.group_id}, reason={reason or 'n/a'}"
        )
