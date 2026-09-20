import json
import time
from time import monotonic
from typing import Optional

from astrbot.api import logger

from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.gateway.json_utils import parse_json_contract
from ...infrastructure.runtime.background_task_budget import BackgroundTaskBudget
from .orchestrator import ReviewWorkRequest


class JargonAutoCheckTask:
    """Canonical jargon AI auto-review task."""

    REVIEW_SYSTEM_PROMPT = (
        "你是群聊黑话审核员。"
        "你需要判断一个候选黑话是否真实成立、释义是否可信、是否应该进入长期可注入状态。"
        "严格返回 JSON："
        "{\"decision\":\"approved|rejected|revision_needed|quarantined\","
        "\"reason\":\"evidence_supported|evidence_conflict|insufficient_evidence|invalid_contract|policy_sensitive|reviewer_disagreement|pair_order_unstable\","
        "\"meaning\":\"可选修正释义\","
        "\"scene\":\"可选适用场景\","
        "\"examples\":[\"可选例句\"],"
        "\"review_suggestion\":\"可选人工复审建议\"}"
    )

    def __init__(self, db_service, gateway, config=None, background_task_budget=None, review_orchestrator=None, candidate_ledger=None, maintenance_only: bool = False):
        self.db = db_service
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.background_task_budget = background_task_budget or BackgroundTaskBudget()
        self.review_orchestrator = review_orchestrator
        self.candidate_ledger = candidate_ledger
        self.maintenance_only = bool(maintenance_only)
        self._last_run_at: dict[str, float] = {}

    def refresh_config(self, config) -> None:
        self.config = config

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
            statuses=["active", "review_pending", "rejected"],
            limit=max(int(limit or 500), 1),
            include_inactive=True,
        )
        groups: list[str] = []
        for candidate in rows:
            metadata = dict(candidate.metadata or {})
            review_status = self._normalized_review_status(metadata.get("review_status") or candidate.status or "review_pending")
            if review_status not in {"review_pending", "pending_human", "rejected", "approved"}:
                continue
            if review_status == "approved" and metadata.get("projection_status") != "pending":
                continue
            groups.append(self._scope_id(candidate.session_id))
        return list(dict.fromkeys(groups))

    async def run_once(self, group_id: Optional[str] = None, *, force: bool = False) -> int:
        if self.maintenance_only:
            return 0
        store = self._store()
        if not store or not hasattr(store, "list_candidates"):
            return 0
        now = monotonic()
        scope = self._scope_id(group_id)
        min_interval = float(getattr(self.config.evolution, "review_runner_min_interval_sec", 21600) or 21600)
        last_run_at = float(self._last_run_at.get(scope, 0.0) or 0.0)
        # ``force`` is intentionally not a cooldown override: governance is
        # limited to one run per scope every configured six-hour interval.
        if last_run_at > 0.0 and now - last_run_at < min_interval:
            return 0
        self._last_run_at[scope] = now
        # ponytail: prune unbounded _last_run_at, keep most recent 250 entries
        if len(self._last_run_at) > 500:
            sorted_keys = sorted(self._last_run_at, key=self._last_run_at.get, reverse=True)[:250]
            self._last_run_at = {k: self._last_run_at[k] for k in sorted_keys}

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
            statuses=["active", "review_pending"],
            limit=max(limit * 6, 60),
            include_inactive=True,
        )
        processed = 0
        for candidate in rows:
            if self._scope_id(candidate.session_id) != scope:
                continue
            metadata = dict(candidate.metadata or {})
            review_status = self._normalized_review_status(metadata.get("review_status") or candidate.status or "review_pending")
            if review_status == "approved" and metadata.get("projection_status") == "pending":
                logger.warning(
                    "[JargonAutoCheck] legacy approved projection blocked; "
                    "candidate=%s requires durable admission/publish proof",
                    getattr(candidate, "id", ""),
                )
                continue
            if review_status != "review_pending":
                continue
            count = int(metadata.get("count") or 0)
            has_evidence = bool(str(metadata.get("meaning") or "").strip()) or bool(metadata.get("examples")) or float(candidate.confidence or 0.0) >= 0.2
            if count < jargon_min_count or not has_evidence:
                continue
            if self.review_orchestrator is not None:
                outcome = await self._review_candidate_durable(candidate)
                if outcome is not None and outcome.status == "completed":
                    processed += 1
                if processed >= limit:
                    break
                continue
            result = await self._review_candidate(candidate)
            if not result:
                continue
            await self._apply_review(candidate, result)
            processed += 1
            if processed >= limit:
                break
        return processed

    @staticmethod
    def _durable_identity(evidence) -> str:
        event_id = str(getattr(evidence, "event_id", "") or "").strip()
        if event_id and not event_id.lower().startswith(("fallback_", "evt_")):
            return f"event_id:{event_id}"
        platform_id = str(getattr(evidence, "platform_message_id", "") or "").strip()
        if platform_id:
            return f"platform_message_id:{platform_id}"
        row_id = getattr(evidence, "source_row_id", None)
        if type(row_id) is int and row_id > 0:
            return f"row:{row_id}"
        return ""

    async def _review_candidate_durable(self, candidate):
        metadata = dict(candidate.metadata or {})
        candidate_id = str(metadata.get("candidate_id") or "").strip()
        revision = metadata.get("candidate_revision")
        if not candidate_id or type(revision) is not int or revision < 0 or self.candidate_ledger is None:
            return None
        ledger_candidate = await self.candidate_ledger.get_candidate(candidate_id)
        if ledger_candidate is None or ledger_candidate.revision != revision:
            return None
        evidence = tuple(
            item for item in await self.candidate_ledger.list_evidence(candidate_id)
            if item.eligible and not item.is_generated
        )
        evidence_ids = tuple(dict.fromkeys(self._durable_identity(item) for item in evidence))
        if not evidence_ids or any(not value for value in evidence_ids):
            return None
        prompt = (
            f"群聊/会话：{self._scope_id(candidate.session_id)}\n"
            f"候选黑话：{candidate.content}\n当前释义：{metadata.get('meaning') or candidate.summary}\n"
            f"场景：{metadata.get('scene') or ''}\n"
            f"样例：{json.dumps(list(metadata.get('examples') or [])[:5], ensure_ascii=False)}\n"
            "请按审核合同返回 decision/reason/confidence。"
        )
        reviewer_ids = tuple(getattr(self.review_orchestrator, "expected_reviewer_ids", ()))
        reviewer_id = reviewer_ids[0] if reviewer_ids else ""
        return await self.review_orchestrator.run_configured_quorum(ReviewWorkRequest(
            candidate_id=candidate_id, candidate_revision=revision,
            scope_id=self._scope_id(candidate.session_id), reviewer_id=reviewer_id,
            reviewer_kind="model", reviewer_attempt_id=f"{candidate_id}:{revision}:{reviewer_id}:ab",
            owner="jargon-auto-check", rubric_version="jargon-review-rubric-v1",
            prompt_version="jargon-review-prompt-v1", pair_order="ab",
            prompt=prompt, system_prompt=self.REVIEW_SYSTEM_PROMPT,
            source_evidence_ids=evidence_ids, source_example_ids=evidence_ids[:5],
            reviewer_profile_version="jargon-reviewer-profile-v1",
        ))

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
            async def _call():
                return await self.gateway.call_data_process_task(
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

            result = await self.background_task_budget.run(
                _call,
                task_name="governance.jargon_check",
                scope_id=self._scope_id(candidate.session_id),
                defer_release_on_timeout=True,
            )
            parsed = parse_json_contract(
                result,
                required_keys=("decision",),
                optional_keys=("reason", "meaning", "scene", "examples", "review_suggestion"),
                field_types={
                    "decision": str,
                    "reason": str,
                    "meaning": str,
                    "scene": str,
                    "examples": list,
                    "review_suggestion": str,
                },
                allow_extra_keys=False,
                allow_naked_members=True,
            )
            return dict(parsed.value) if parsed.schema_valid else None
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
            metadata["projection_status"] = "pending"
            await self._activate_approved_candidate(candidate, metadata)
            logger.info(
                f"[JargonAutoCheck] 黑话审核完成 #{candidate.id}: decision={decision}, "
                f"group={self._scope_id(candidate.session_id)}, reason={reason or 'n/a'}"
            )
            return
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

    async def _activate_approved_candidate(self, candidate, metadata: dict) -> bool:
        store = self._store()
        if not store:
            return False
        pending_metadata = dict(metadata or {})
        pending_metadata["review_status"] = "approved"
        pending_metadata["projection_status"] = "pending"
        changed = await store.update_memory(
            str(candidate.id),
            summary=str(pending_metadata.get("meaning") or candidate.summary or candidate.content or "")[:240],
            status="active",
            metadata=pending_metadata,
            visibility="auto_and_tool",
        )
        if not changed:
            return False

        projector = self._projector()
        projected = True
        if projector:
            try:
                projected = await projector.project(str(candidate.id))
            except Exception as exc:
                projected = False
                logger.warning(f"[JargonAutoCheck] 黑话投影失败 #{candidate.id}: {exc}")
        if projected is False:
            await store.update_memory(
                str(candidate.id),
                status="review_pending",
                metadata=pending_metadata,
                visibility="maintenance_only",
            )
            if projector and hasattr(projector, "cleanup_deleted"):
                await projector.cleanup_deleted([str(candidate.id)])
            return False

        projected_metadata = dict(pending_metadata)
        projected_metadata["projection_status"] = "projected" if projector else "not_required"
        await store.update_memory(
            str(candidate.id),
            status="active",
            metadata=projected_metadata,
            visibility="auto_and_tool",
        )
        return True


__all__ = ["JargonAutoCheckTask"]
