import json
import re
import time
from typing import Optional

from astrbot.api import logger

from ...infrastructure.runtime.lane_manager import LaneKey


class JargonAutoCheckTask:
    """Canonical jargon AI auto-review task."""

    REVIEW_SYSTEM_PROMPT = (
        "你是群聊黑话审核员。"
        "你需要判断一个候选黑话是否真实成立、释义是否可信、是否应该进入长期可注入状态。"
        "严格返回 JSON："
        "{\"decision\":\"approved|rejected|revision_needed\","
        "\"reason\":\"简短原因\","
        "\"meaning\":\"可选修正释义\","
        "\"scene\":\"可选适用场景\","
        "\"examples\":[\"可选例句\"],"
        "\"review_suggestion\":\"可选人工复审建议\"}"
    )

    def __init__(self, db_service, gateway, config=None):
        self.db = db_service
        self.gateway = gateway
        self.config = config if config else gateway.config
        self._last_run_at: dict[str, float] = {}

    def _store(self):
        return getattr(getattr(self.db, "memory_engine", None), "v2_store", None)

    def _projector(self):
        return getattr(getattr(self.db, "memory_engine", None), "index_projector", None)

    @staticmethod
    def _scope_id(group_id: str | None) -> str:
        clean = str(group_id or "").strip()
        return clean or "GLOBAL"

    @staticmethod
    def _normalized_review_status(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized == "approved":
            return "approved"
        if normalized == "rejected":
            return "rejected"
        if normalized in {"pending_human", "revision_needed"}:
            return "pending_human"
        return "review_pending"

    async def list_governance_groups(self, *, limit: int = 500) -> list[str]:
        store = self._store()
        if not store or not hasattr(store, "list_candidates"):
            return []
        rows = await store.list_candidates(
            kinds=["jargon"],
            statuses=["review_pending", "rejected"],
            limit=max(int(limit or 500), 1),
            include_inactive=True,
        )
        groups: list[str] = []
        for candidate in rows:
            metadata = dict(candidate.metadata or {})
            review_status = self._normalized_review_status(metadata.get("review_status") or candidate.status or "review_pending")
            if review_status not in {"review_pending", "pending_human", "rejected", "approved"}:
                continue
            groups.append(self._scope_id(candidate.session_id))
        return list(dict.fromkeys(groups))

    async def run_once(self, group_id: Optional[str] = None) -> int:
        store = self._store()
        if not store or not hasattr(store, "list_candidates"):
            return 0
        now = time.time()
        scope = self._scope_id(group_id)
        min_interval = float(getattr(self.config.evolution, "review_runner_min_interval_sec", 45) or 45)
        if now - float(self._last_run_at.get(scope, 0.0) or 0.0) < min_interval:
            return 0
        self._last_run_at[scope] = now

        limit = max(int(getattr(self.config.evolution, "review_batch_size", 10) or 10), 1)
        jargon_min_count = int(
            getattr(
                self.config.evolution,
                "jargon_min_count",
                getattr(self.config.evolution, "review_min_count", 2),
            )
            or 2
        )
        rows = await store.list_candidates(
            session_id="" if scope == "GLOBAL" else scope,
            kinds=["jargon"],
            statuses=["review_pending"],
            limit=max(limit * 6, 60),
            include_inactive=True,
        )
        processed = 0
        for candidate in rows:
            if self._scope_id(candidate.session_id) != scope:
                continue
            metadata = dict(candidate.metadata or {})
            review_status = self._normalized_review_status(metadata.get("review_status") or candidate.status or "review_pending")
            if review_status not in {"review_pending", "pending_human"}:
                continue
            count = int(metadata.get("count") or 0)
            has_evidence = bool(str(metadata.get("meaning") or "").strip()) or bool(metadata.get("examples")) or float(candidate.confidence or 0.0) >= 0.2
            if count < jargon_min_count or not has_evidence:
                continue
            result = await self._review_candidate(candidate)
            if not result:
                continue
            await self._apply_review(candidate, result)
            processed += 1
            if processed >= limit:
                break
        return processed

    async def _review_candidate(self, candidate) -> Optional[dict]:
        metadata = dict(candidate.metadata or {})
        prompt = (
            f"群聊/会话：{self._scope_id(candidate.session_id)}\n"
            f"候选黑话：{candidate.content}\n"
            f"当前释义：{metadata.get('meaning') or candidate.summary}\n"
            f"场景：{metadata.get('scene') or ''}\n"
            f"样例：{json.dumps(list(metadata.get('examples') or [])[:5], ensure_ascii=False)}\n"
            f"原始上下文：{metadata.get('raw_content') or candidate.content}\n"
            f"出现次数：{int(metadata.get('count') or 1)}\n"
            f"置信度：{float(candidate.confidence or metadata.get('confidence') or 0.0):.2f}\n"
            "请判断它是否应作为长期群内黑话保留。"
        )
        try:
            result = await self.gateway.call_data_process_task(
                prompt=prompt,
                system_prompt=self.REVIEW_SYSTEM_PROMPT,
                is_json=True,
                lane_key=LaneKey(
                    subsystem="bg",
                    task_family="reflect",
                    scope_id=self._scope_id(candidate.session_id),
                    scope_kind="global",
                ),
                base_origin=str(candidate.session_id or ""),
            )
            if isinstance(result, dict):
                return result
            if isinstance(result, str):
                match = re.search(r"\{.*\}", result, re.DOTALL)
                if match:
                    return json.loads(match.group(0))
        except Exception as exc:
            logger.error(f"[JargonAutoCheck] 审核黑话失败 #{getattr(candidate, 'id', '?')}: {exc}")
        return None

    async def _apply_review(self, candidate, result: dict) -> None:
        store = self._store()
        if not store:
            return
        metadata = dict(candidate.metadata or {})
        decision = str(result.get("decision", "revision_needed")).strip().lower()
        reason = str(result.get("reason", "")).strip()
        meaning = str(result.get("meaning", "")).strip()
        scene = str(result.get("scene", "")).strip()
        suggestion = str(result.get("review_suggestion", "")).strip()
        examples = [str(item).strip() for item in (result.get("examples", []) or []) if str(item).strip()][:5]
        if meaning:
            metadata["meaning"] = meaning
        if scene:
            metadata["scene"] = scene
        if examples:
            metadata["examples"] = list(dict.fromkeys([*list(metadata.get("examples") or []), *examples]))[:5]
        metadata["review_reason"] = reason
        metadata["last_review_time"] = time.time()
        next_status = "review_pending"
        visibility = "maintenance_only"
        if decision == "approved":
            metadata["review_status"] = "approved"
            metadata["review_suggestion"] = ""
            next_status = "active"
            visibility = "auto_and_tool"
        elif decision == "rejected":
            metadata["review_status"] = "rejected"
            metadata["review_suggestion"] = ""
            next_status = "rejected"
        else:
            metadata["review_status"] = "pending_human"
            metadata["review_suggestion"] = suggestion or meaning or str(metadata.get("meaning") or "")
            next_status = "review_pending"
        changed = await store.update_memory(
            str(candidate.id),
            summary=str(metadata.get("meaning") or candidate.summary or candidate.content or "")[:240],
            status=next_status,
            metadata=metadata,
            visibility=visibility,
        )
        projector = self._projector()
        if changed and projector:
            if next_status == "active":
                await projector.project(str(candidate.id))
            else:
                await projector.cleanup_deleted([str(candidate.id)])
        logger.info(
            f"[JargonAutoCheck] 黑话审核完成 #{candidate.id}: decision={decision}, "
            f"group={self._scope_id(candidate.session_id)}, reason={reason or 'n/a'}"
        )


__all__ = ["JargonAutoCheckTask"]
