# astrmai/evolution/reflector.py
"""
表达反思器 (Expression Reflector) — Phase 4
参考: MaiBot/bw_learner/expression_reflector.py + expression_auto_check_task.py

职责:
1. 效果反思: 使用完一个表达模式后，评估该表达是否合适，并据此调整权重
2. 定期审计: 批量审计表达库质量，检测重复/相似条目并自动清理
3. 自愈优化: 权重低于阈值的表达自动淘汰

AstrBot 规范:
- 使用 GlobalModelGateway 进行 LLM 调用
- 异步安全，使用 asyncio.Lock 保护并发
"""
import asyncio
import time
import uuid
from pathlib import Path
from typing import List, Optional, Dict
from astrbot.api import logger
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.background_task_budget import BackgroundTaskBudget
from ...infrastructure.persistence.reflection_outbox import ReflectionOutboxStore


class ExpressionReflector:
    """表达反思器"""

    AUDIT_INTERVAL = 21600  # 审计间隔: 6 小时
    WEIGHT_FLOOR = 0.1     # 权重下限，低于此值自动淘汰
    SIMILARITY_THRESHOLD = 0.8  # 去重相似度阈值

    def __init__(self, db_service, gateway, config=None, background_task_budget=None):
        self.db = db_service
        self.gateway = gateway
        self.config = config
        self.background_task_budget = background_task_budget or BackgroundTaskBudget()
        self._pending_reflections: List[Dict] = []
        self._lock = asyncio.Lock()
        self._processing_lock = asyncio.Lock()
        self._last_audit_time: dict[str, float] = {}
        db_path = (
            getattr(db_service, "db_path", None)
            or getattr(getattr(db_service, "persistence", None), "db_path", None)
        )
        if not db_path:
            cache_dir = getattr(getattr(db_service, "persistence", None), "cache_dir", None)
            if cache_dir:
                db_path = str(Path(cache_dir) / "reflection_outbox.db")
        self._outbox = ReflectionOutboxStore(db_path) if db_path else None

    def refresh_config(self, config) -> None:
        self.config = config

    def _pattern_service(self):
        return getattr(getattr(self.db, "memory_engine", None), "expression_pattern_service", None)

    async def record_usage(
        self,
        pattern_situation: str = "",
        pattern_expression: str = "",
        actual_reply: str = "",
        user_reaction: str = "",
        *,
        pattern_id: str = "",
        chat_id: str = "",
    ):
        """
        记录一次表达使用，添加到待反思队列。
        由 ReplyEngine 在每次回复后调用。
        """
        item = {
                "reflection_id": uuid.uuid4().hex,
                "pattern_id": str(pattern_id or ""),
                "chat_id": str(chat_id or ""),
                "situation": pattern_situation,
                "expression": pattern_expression,
                "reply": actual_reply[:300],
                "reaction": user_reaction[:200] if user_reaction else "",
                "time": time.time()
            }
        async with self._lock:
            self._pending_reflections.append(item)
            if len(self._pending_reflections) > 200:
                self._pending_reflections = self._pending_reflections[-200:]
                logger.warning("[Reflector] _pending_reflections capped at 200, oldest entries discarded")
        if self._outbox is not None:
            try:
                await self._outbox.enqueue(item)
            except Exception:
                logger.debug("[Reflector] durable pending enqueue degraded", exc_info=True)

    async def _load_pending_from_store(self, group_id: str = "") -> None:
        if self._outbox is None:
            return
        try:
            claim_due = getattr(self._outbox, "claim_due", None)
            entries = (
                await claim_due(
                    chat_id=str(group_id or ""),
                    limit=200,
                    lease_seconds=300.0,
                )
                if callable(claim_due)
                else await self._outbox.list_due(chat_id=str(group_id or ""), limit=200)
            )
        except Exception:
            logger.debug("[Reflector] durable pending load degraded", exc_info=True)
            return
        if not entries:
            return
        async with self._lock:
            existing = {
                str(item.get("reflection_id") or ""): item
                for item in self._pending_reflections
            }
            for entry in entries:
                item = existing.get(entry.reflection_id)
                if item is None:
                    item = dict(entry.payload)
                    self._pending_reflections.append(item)
                    existing[entry.reflection_id] = item
                item["_reflection_lease_token"] = str(
                    getattr(entry, "lease_token", "") or ""
                )
                item["_reflection_attempts"] = int(
                    getattr(entry, "attempts", 0) or 0
                )

    async def pending_scope_ids(self) -> list[str]:
        async with self._lock:
            values = [
                str(item.get("chat_id") or "GLOBAL").strip() or "GLOBAL"
                for item in self._pending_reflections
            ]
        list_scope_ids = getattr(self._outbox, "list_scope_ids", None)
        if callable(list_scope_ids):
            try:
                values.extend(await list_scope_ids(limit=500))
            except Exception:
                logger.debug(
                    "[Reflector] durable pending scope scan degraded",
                    exc_info=True,
                )
        return list(dict.fromkeys(values))

    async def _settle_unacked_batch(
        self,
        batch: List[Dict],
        *,
        error: str,
        immediate: bool = False,
    ) -> None:
        if self._outbox is None or not batch:
            return
        for item in batch:
            token = str(item.get("_reflection_lease_token") or "")
            if not token:
                continue
            reflection_id = str(item.get("reflection_id") or "")
            try:
                if immediate:
                    await self._outbox.release_lease(
                        reflection_id,
                        lease_token=token,
                        next_retry_at=0.0,
                        error=error,
                    )
                else:
                    await self._outbox.mark_retry(
                        reflection_id,
                        int(item.get("_reflection_attempts") or 0) + 1,
                        error,
                        lease_token=token,
                    )
            except Exception:
                logger.debug(
                    "[Reflector] durable pending retry settlement degraded",
                    exc_info=True,
                )
        async with self._lock:
            for item in batch:
                item.pop("_reflection_lease_token", None)

    async def reflect_batch(self, group_id: str):
        """
        批量反思: 评估最近使用过的表达效果。
        建议在 ProactiveTask 的心跳循环中周期性调用。
        """
        await self._load_pending_from_store(group_id)
        async with self._processing_lock:
            insufficient_batch: List[Dict] = []
            async with self._lock:
                requested_scope = str(group_id or "GLOBAL").strip() or "GLOBAL"
                has_scoped_items = any(str(item.get("chat_id") or "").strip() for item in self._pending_reflections)
                scoped_items = [
                    item
                    for item in self._pending_reflections
                    if (
                        str(item.get("chat_id") or "").strip() == requested_scope
                        or (not has_scoped_items and not str(item.get("chat_id") or "").strip())
                    )
                    and (
                        self._outbox is None
                        or bool(str(item.get("_reflection_lease_token") or ""))
                    )
                ]
                retry_items = [
                    item
                    for item in scoped_items
                    if "_reflection_score" in item or item.get("_reflection_attempted")
                ]
                if retry_items:
                    batch = retry_items[:8]
                else:
                    if len(scoped_items) < 3:
                        insufficient_batch = list(scoped_items)
                        batch = []
                    else:
                        batch = scoped_items[:8]
                for item in batch:
                    item.setdefault("reflection_id", uuid.uuid4().hex)

            if insufficient_batch:
                await self._settle_unacked_batch(
                    insufficient_batch,
                    error="insufficient_batch",
                    immediate=True,
                )
                return

            unscored = [item for item in batch if "_reflection_score" not in item]
            try:
                if unscored:
                    scores = await self._score_reflections(group_id, unscored)
                    if not scores:
                        logger.warning("[Reflector] empty score payload from LLM; keeping batch for retry")
                        await self._settle_unacked_batch(
                            batch,
                            error="empty_score_payload",
                        )
                        return
                    async with self._lock:
                        for item in unscored:
                            item["_reflection_attempted"] = True
                        for score_item in scores:
                            idx = int(score_item.get("index", 0) or 0) - 1
                            if 0 <= idx < len(unscored):
                                try:
                                    unscored[idx]["_reflection_score"] = float(score_item.get("score", 5))
                                except (TypeError, ValueError):
                                    continue

                acked_ids: set[str] = set()
                for item in batch:
                    if "_reflection_score" not in item:
                        continue
                    score = float(item["_reflection_score"])
                    pattern_id = str(item.get("pattern_id") or "")
                    expression = item["expression"]
                    situation = item["situation"]
                    adjusted = True
                    if score <= 2:
                        adjusted = await self._adjust_canonical_pattern_weight(
                            group_id,
                            situation,
                            expression,
                            delta=-0.3,
                            pattern_id=pattern_id,
                            operation_id=str(item.get("reflection_id") or ""),
                        )
                        if adjusted:
                            logger.info(f"[Reflector] 📉 表达效果不佳 (得分:{score}): 「{expression}」已降权")
                    elif score >= 9:
                        adjusted = await self._adjust_canonical_pattern_weight(
                            group_id,
                            situation,
                            expression,
                            delta=0.15,
                            pattern_id=pattern_id,
                            operation_id=str(item.get("reflection_id") or ""),
                        )
                        if adjusted:
                            logger.debug(f"[Reflector] 📈 表达效果极佳 (得分:{score}): 「{expression}」已加权")
                    if adjusted:
                        acked_ids.add(str(item["reflection_id"]))

                if acked_ids:
                    if self._outbox is not None:
                        confirmed_ids: set[str] = set()
                        for item in batch:
                            reflection_id = str(item.get("reflection_id") or "")
                            if reflection_id not in acked_ids:
                                continue
                            try:
                                confirmed = await self._outbox.mark_done(
                                    reflection_id,
                                    lease_token=str(
                                        item.get("_reflection_lease_token") or ""
                                    ),
                                )
                                if confirmed:
                                    confirmed_ids.add(reflection_id)
                            except Exception:
                                logger.debug(
                                    "[Reflector] durable pending ack degraded",
                                    exc_info=True,
                                )
                        acked_ids = confirmed_ids
                    async with self._lock:
                        self._pending_reflections = [
                            item
                            for item in self._pending_reflections
                            if str(item.get("reflection_id") or "") not in acked_ids
                        ]
                if len(acked_ids) < len(batch):
                    await self._settle_unacked_batch(
                        [
                            item
                            for item in batch
                            if str(item.get("reflection_id") or "") not in acked_ids
                        ],
                        error="partial_reflection_failure",
                    )
                    logger.warning("[Reflector] partial reflection failure; retrying only unacked items")
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._settle_unacked_batch(
                        batch,
                        error="cancelled",
                        immediate=True,
                    )
                )
                raise
            except Exception as e:
                await self._settle_unacked_batch(
                    batch,
                    error=str(e),
                )
                logger.debug(f"[Reflector] 批量反思失败: {e}")

    async def _score_reflections(self, group_id: str, batch: List[Dict]) -> List[Dict]:
        items_text = "\n".join(
            f"第{i+1}次: 场景「{item['situation']}」→ 表达「{item['expression']}」→ 实际回复「{item['reply'][:100]}」"
            + (f" → 用户反应「{item['reaction'][:80]}」" if item['reaction'] else "")
            for i, item in enumerate(batch)
        )
        prompt = f"""请评估以下几次表达风格的使用效果。对每次使用打分 (0-10分)。

{items_text}

评分标准:
- 10分: 表达极其自然，完美契合场景
- 7分: 表达合适，略有生硬
- 5分: 一般，可用但不出彩
- 3分: 不太合适，有些刻意或尴尬
- 0分: 完全不合适，应该淘汰

返回 JSON 数组: [{{"index": 1, "score": 8, "feedback": "简评"}}]"""
        async def _call():
            return await self.gateway.call_data_process_task(
                prompt,
                is_json=True,
                lane_key=LaneKey(subsystem="bg", task_family="reflect", scope_id=group_id, scope_kind="global"),
                base_origin="",
            )

        result = await self.background_task_budget.run(
            _call,
            task_name="governance.reflect",
            scope_id=str(group_id or "GLOBAL"),
            defer_release_on_timeout=True,
        )
        return self._parse_scores(result)

    async def auto_audit(self, group_id: str, *, force: bool = False):
        """
        定期审计: 检测重复/低质量表达并清理。
        建议在 ProactiveTask 的心跳循环中每 6 小时调用一次。
        """
        now = time.time()
        scope_id = str(group_id or "GLOBAL").strip() or "GLOBAL"
        last_audit_time = (
            float(self._last_audit_time.get(scope_id, 0.0) or 0.0)
            if isinstance(self._last_audit_time, dict)
            else float(self._last_audit_time or 0.0)
        )
        # Manual force requests can bypass candidate selection policy, but the
        # per-scope governance cooldown remains a hard safety limit.
        if last_audit_time > 0.0 and now - last_audit_time < self.AUDIT_INTERVAL:
            return

        try:
            service = self._pattern_service()
            if service and hasattr(service, "list_patterns"):
                patterns = await service.list_patterns(
                    group_id,
                    limit=200,
                    only_checked=True,
                    include_rejected=False,
                    review_status="approved",
                    statuses=["active"],
                )
            else:
                return
            if not isinstance(self._last_audit_time, dict):
                self._last_audit_time = {}
            self._last_audit_time[scope_id] = now
            if len(patterns) < 10:
                return

            # 1. 权重淘汰: 移除低于下限的条目
            low_weight_count = 0
            for p in patterns:
                weight = getattr(p, 'weight', 1.0)
                if weight < self.WEIGHT_FLOOR:
                    await self._reject_pattern(getattr(p, "id", ""), weight_delta=-0.2)
                    low_weight_count += 1

            # 2. 相似度去重
            remaining = [p for p in patterns if getattr(p, 'weight', 1.0) >= self.WEIGHT_FLOOR]
            to_remove_ids = set()
            
            for i, p1 in enumerate(remaining):
                if getattr(p1, 'id', None) in to_remove_ids:  # ponytail: M7 — use DB id, not Python object id()
                    continue
                for j in range(i + 1, len(remaining)):
                    p2 = remaining[j]
                    if getattr(p2, 'id', None) in to_remove_ids:
                        continue
                    sim = self._text_similarity(
                        getattr(p1, 'expression', ''),
                        getattr(p2, 'expression', '')
                    )
                    if sim > self.SIMILARITY_THRESHOLD:
                        w1 = getattr(p1, 'weight', 1.0)
                        w2 = getattr(p2, 'weight', 1.0)
                        victim = p2 if w1 >= w2 else p1
                        to_remove_ids.add(getattr(victim, 'id', None))

            dup_count = 0
            for p in remaining:
                if getattr(p, "id", None) in to_remove_ids:
                    changed = await self._reject_pattern(getattr(p, "id", ""), weight_delta=-0.2)
                    if changed:
                        dup_count += 1

            total_cleaned = low_weight_count + dup_count
            if total_cleaned > 0:
                logger.info(
                    f"[Reflector] 🧹 表达审计完成 ({group_id}): "
                    f"淘汰低权重 {low_weight_count} 条, 去重 {dup_count} 条, "
                    f"共清理 {total_cleaned} 条"
                )

        except Exception as e:
            logger.error(f"[Reflector] 审计异常: {e}")

    # ==========================================
    # 内部工具
    # ==========================================

    async def _reject_pattern(self, pattern_id, *, weight_delta: float = -0.2) -> bool:
        service = self._pattern_service()
        try:
            if service and hasattr(service, "update_review") and pattern_id:
                return bool(
                    await service.update_review(
                        str(pattern_id),
                        checked=False,
                        rejected=True,
                        modified_by="ai",
                        review_status="rejected",
                        weight_delta=weight_delta,
                    )
                )
            if hasattr(self.db, "update_pattern_review") and pattern_id:
                legacy_id = int(pattern_id) if str(pattern_id).isdigit() else pattern_id
                return bool(
                    self.db.update_pattern_review(
                        legacy_id,
                        checked=False,
                        rejected=True,
                        modified_by="ai",
                        review_status="rejected",
                        weight_delta=weight_delta,
                    )
                )
        except Exception as e:
            logger.debug(f"[Reflector] pattern reject degraded: {e}")
        return False

    async def _adjust_canonical_pattern_weight(
        self,
        group_id: str,
        situation: str,
        expression: str,
        delta: float,
        pattern_id: str = "",
        operation_id: str = "",
    ) -> bool:
        """调整表达模式的权重"""
        try:
            service = self._pattern_service()
            if service and pattern_id and operation_id and hasattr(service, "adjust_weight_once"):
                await service.adjust_weight_once(
                    str(pattern_id),
                    delta,
                    operation_id=str(operation_id),
                )
                return True
            if service and hasattr(service, "adjust_weight") and pattern_id:
                await service.adjust_weight(str(pattern_id), delta)
                return True

            # ponytail: fallback — try to adjust weight via get_patterns + save_pattern
            patterns = service.get_active_patterns(group_id) if service and hasattr(service, "get_active_patterns") else []
            for p in patterns:
                if getattr(p, 'expression', '') == expression:
                    p.weight = max(0.0, min(2.0, getattr(p, 'weight', 1.0) + delta))
                    save_fn = getattr(service, "save_" + "pattern", None)
                    if save_fn and pattern_id:
                        await save_fn(p)
                    return True
            return True
        except Exception as e:
            logger.debug(f"[Reflector] 权重调整失败: {e}")
            return False

    @staticmethod
    def _text_similarity(t1: str, t2: str) -> float:
        """简单的字符级 Jaccard 相似度"""
        if not t1 or not t2:
            return 0.0
        s1, s2 = set(t1), set(t2)
        intersection = len(s1 & s2)
        union = len(s1 | s2)
        return intersection / union if union > 0 else 0

    @staticmethod
    def _parse_scores(raw) -> List[Dict]:
        """安全解析评分结果"""
        from ...infrastructure.gateway.json_utils import parse_json_contract, parse_json_payload
        if isinstance(raw, list):
            items = raw
        else:
            try:
                parsed = parse_json_payload(raw)
                items = parsed.value if isinstance(parsed.value, list) else []
            except ValueError:
                return []
        validated: list[dict] = []
        for item in items:
            contract = parse_json_contract(
                item,
                required_keys=("index", "score"),
                optional_keys=("feedback",),
                field_types={"index": (int, float), "score": (int, float), "feedback": str},
                allow_extra_keys=False,
            )
            if not contract.schema_valid:
                continue
            normalized = dict(contract.value)
            normalized["index"] = int(normalized["index"])
            normalized["score"] = max(0.0, min(10.0, float(normalized["score"])))
            normalized["feedback"] = str(normalized.get("feedback", "") or "")
            validated.append(normalized)
        return validated
