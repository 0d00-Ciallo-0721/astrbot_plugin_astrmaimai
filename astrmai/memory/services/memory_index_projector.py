from __future__ import annotations

import asyncio
import json

from astrbot.api import logger

from ..contracts.memory_query import MemoryWriteRequest
from ...infrastructure.runtime.background_task_budget import BackgroundTaskQueueFull


class MemoryIndexProjector:
    """Projects canonical SQL memory into the existing hybrid index."""

    def __init__(self, engine):
        self.engine = engine
        self._pending_projection_ids: set[str] = set()
        self._pending_projection_reasons: dict[str, str] = {}
        self._pending_projection_scheduled: dict[str, bool] = {}
        self._retry_task: asyncio.Task | None = None
        self._retry_stop = asyncio.Event()
        self._projection_lock = getattr(engine, "_projection_lock", None) or asyncio.Lock()
        self._ack_projection_outbox = bool(getattr(engine, "_ack_projection_outbox", True))
        self._candidate_outbox_candidates: set[str] = {
            str(item)
            for item in (getattr(engine, "_candidate_outbox_candidates", set()) or set())
            if item
        }
        self._candidate_outbox_watermarks: dict[str, int] = {
            str(key): int(value or 0)
            for key, value in (getattr(engine, "_candidate_outbox_watermarks", {}) or {}).items()
        }
        self._candidate_outbox_ids: set[str] = set()
        self._candidate_outbox_confirmations: dict[str, int] = {}
        self._outbox_diagnostics: dict[str, object] = {
            "pending_count": 0,
            "pending_count_by_reason": {},
            "dead_letter_count": 0,
            "dead_letter_count_by_reason": {},
            "oldest_pending_age_sec": 0.0,
            "max_attempts": 0,
            "next_retry_at": None,
        }
        self._retry_success_count = 0
        self._retry_failure_count = 0
        self._retry_rejected_by_shutdown = 0
        self._last_retry_items: list[dict[str, object]] = []
        self._projection_failure_by_reason: dict[str, int] = {}
        self._projection_deferred_by_reason: dict[str, int] = {}
        self._projection_inflight_ids: set[str] = set()

    def _get_projection_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_projection_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._projection_lock = lock
        return lock

    def _projection_rebuild_active(self) -> bool:
        return bool(getattr(self.engine, "_projection_rebuild_active", False))

    def _retriever_ready_for_projection(self) -> bool:
        retriever = getattr(self.engine, "retriever", None)
        if retriever is None:
            return False
        if hasattr(self.engine, "_is_ready") and not bool(getattr(self.engine, "_is_ready", False)):
            return False
        if hasattr(self.engine, "_vector_state"):
            state = str(getattr(self.engine, "_vector_state", "") or "")
            if state and state != "ready":
                return False
        return True

    async def _defer_projection_ids(self, memory_ids: list[str], reason: str) -> None:
        for memory_id in memory_ids:
            if memory_id:
                await self._mark_pending_persisted(str(memory_id), reason)

    async def _acquire_projection_lock(self, *, timeout_sec: float | None = None) -> bool:
        timeout = max(
            0.05,
            float(
                timeout_sec
                if timeout_sec is not None
                else self._config_value("projection_lock_timeout_sec", 1.0)
                or 1.0
            ),
        )
        try:
            await asyncio.wait_for(self._get_projection_lock().acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _config_value(self, name: str, default):
        config = getattr(self.engine, "config", None)
        timing_config = getattr(config, "timing", None)
        if timing_config is not None and hasattr(timing_config, name):
            return getattr(timing_config, name)
        memory_config = getattr(config, "memory", None)
        return getattr(memory_config, name, default)

    @staticmethod
    def _classify_projection_exception(exc: BaseException) -> str:
        name = type(exc).__name__
        stage = str(getattr(exc, "stage", "") or "").lower()
        if "queue" in name.lower() and "timeout" in name.lower():
            return "embedding_queue_timeout"
        if stage == "embedding" or "embedding" in name.lower():
            return "embedding_timeout"
        if "cancel" in name.lower():
            return "cancelled"
        if "lock" in name.lower() and "timeout" in name.lower():
            return "projection_lock_timeout"
        if "vector" in name.lower() or "faiss" in name.lower():
            return "vector_add_failed"
        return f"projection_error:{name}"

    async def _mark_pending_persisted(self, memory_id: str, reason: str) -> bool:
        self._mark_pending(memory_id, reason)
        store = getattr(self.engine, "v2_store", None)
        schedule = getattr(store, "schedule_projection_retry", None)
        if not callable(schedule):
            return False
        scheduled = bool(
            await schedule(
                memory_id,
                reason,
                base_delay_sec=float(self._config_value("projection_retry_base_delay_sec", 30.0) or 30.0),
                max_delay_sec=float(self._config_value("projection_retry_max_delay_sec", 900.0) or 900.0),
                max_attempts=int(self._config_value("projection_retry_max_attempts", 8) or 8),
            )
        )
        self._pending_projection_scheduled[memory_id] = scheduled
        await self._refresh_outbox_diagnostics()
        if not scheduled:
            status_reader = getattr(store, "projection_retry_status", None)
            if callable(status_reader):
                try:
                    if await status_reader(memory_id) == "dead_letter":
                        self._clear_pending(memory_id)
                except Exception as exc:
                    logger.debug(f"[MemoryIndexProjector] retry status unavailable: {exc}")
        return scheduled

    async def _clear_pending_persisted(self, memory_id: str, expected_revision: int = 0) -> None:
        self._clear_pending(memory_id)
        self._pending_projection_scheduled.pop(memory_id, None)
        if not self._ack_projection_outbox:
            normalized_id = str(memory_id or "")
            if normalized_id in self._candidate_outbox_candidates:
                self._candidate_outbox_ids.add(normalized_id)
                self._candidate_outbox_confirmations[normalized_id] = self._candidate_outbox_watermarks.get(
                    normalized_id, 0
                )
            return
        store = getattr(self.engine, "v2_store", None)
        conditional = getattr(store, "complete_projection_retry_if_unchanged", None)
        if callable(conditional):
            await conditional(memory_id, int(expected_revision or 0))
            await self._refresh_outbox_diagnostics()
            return
        complete = getattr(store, "complete_projection_retry", None)
        if callable(complete):
            await complete(memory_id)
            await self._refresh_outbox_diagnostics()

    def candidate_outbox_ids(self) -> set[str]:
        return {item for item in self._candidate_outbox_ids if item}

    def candidate_outbox_confirmations(self) -> dict[str, int]:
        return dict(self._candidate_outbox_confirmations)

    async def confirm_projection_outbox(self, memory_ids) -> None:
        store = getattr(self.engine, "v2_store", None)
        complete = getattr(store, "complete_projection_retry", None)
        conditional = getattr(store, "complete_projection_retry_if_unchanged", None)
        if isinstance(memory_ids, dict) and callable(conditional):
            for memory_id, watermark in memory_ids.items():
                await conditional(str(memory_id), int(watermark))
            return
        if not callable(complete):
            return
        for memory_id in set(memory_ids or []):
            await complete(str(memory_id))

    def _mark_pending(self, memory_id: str, reason: str) -> None:
        if not memory_id:
            return
        normalized_reason = str(reason or "unknown")
        self._pending_projection_ids.add(memory_id)
        self._pending_projection_reasons[memory_id] = normalized_reason
        deferred_reasons = {"retriever_not_ready", "projection_rebuild_in_progress", "shutdown_rejected"}
        if normalized_reason in deferred_reasons:
            deferred_counts = getattr(self, "_projection_deferred_by_reason", None)
            if deferred_counts is None:
                deferred_counts = {}
                self._projection_deferred_by_reason = deferred_counts
            deferred_counts[normalized_reason] = deferred_counts.get(normalized_reason, 0) + 1
            return
        failure_counts = getattr(self, "_projection_failure_by_reason", None)
        if failure_counts is None:
            failure_counts = {}
            self._projection_failure_by_reason = failure_counts
        failure_counts[normalized_reason] = failure_counts.get(normalized_reason, 0) + 1

    def _clear_pending(self, memory_id: str) -> None:
        self._pending_projection_ids.discard(memory_id)
        self._pending_projection_reasons.pop(memory_id, None)

    def pending_reason(self, memory_id: str) -> str:
        return str(self._pending_projection_reasons.get(str(memory_id or ""), "") or "")

    def retry_scheduled(self, memory_id: str) -> bool:
        return bool(self._pending_projection_scheduled.get(str(memory_id or ""), False))

    def _documents_db_path(self) -> str | None:
        return getattr(self.engine, "db_path", None)

    async def project(self, memory_id: str, request: MemoryWriteRequest | None = None) -> bool:
        normalized_id = str(memory_id or "").strip()
        inflight = getattr(self, "_projection_inflight_ids", None)
        if inflight is None:
            inflight = set()
            self._projection_inflight_ids = inflight
        if not normalized_id or normalized_id in inflight:
            return False
        inflight.add(normalized_id)
        try:
            return await self._project_once(normalized_id, request)
        finally:
            inflight.discard(normalized_id)

    async def _project_once(self, memory_id: str, request: MemoryWriteRequest | None = None) -> bool:
        if self._projection_rebuild_active():
            await self._mark_pending_persisted(memory_id, "projection_rebuild_in_progress")
            return False
        if not await self._acquire_projection_lock():
            await self._mark_pending_persisted(memory_id, "projection_lock_timeout")
            return False
        if self._projection_rebuild_active():
            self._get_projection_lock().release()
            await self._mark_pending_persisted(memory_id, "projection_rebuild_in_progress")
            return False
        try:
            return await self._project_locked(memory_id, request)
        finally:
            self._get_projection_lock().release()

    async def _project_locked(self, memory_id: str, request: MemoryWriteRequest | None = None) -> bool:
        if not memory_id:
            return False
        expected_revision = 0
        revision_reader = getattr(getattr(self.engine, "v2_store", None), "projection_retry_revision", None)
        if callable(revision_reader):
            try:
                expected_revision = int(await revision_reader(memory_id) or 0)
            except Exception:
                expected_revision = 0
        if not self._retriever_ready_for_projection():
            scheduled = await self._mark_pending_persisted(memory_id, "retriever_not_ready")
            logger.info(
                f"[MemoryIndexProjector] projection deferred memory_id={memory_id} "
                f"reason=retriever_not_ready repair_scheduled={str(scheduled).lower()}"
            )
            return False
        try:
            if request is None:
                candidate = await self.engine.v2_store.get_by_id(memory_id, allow_stale=False)
                if not candidate:
                    cleanup_result = await self._cleanup_deleted_locked(
                        [memory_id],
                        return_result=True,
                    )
                    if cleanup_result.get("failed"):
                        return False
                    return True
                request = MemoryWriteRequest(
                    source=candidate.source,
                    kind=candidate.kind,
                    session_id=candidate.session_id,
                    persona_id=candidate.persona_id,
                    content=candidate.content,
                    summary=candidate.summary,
                    tags=candidate.tags,
                    importance=candidate.importance,
                    confidence=candidate.confidence,
                    metadata=candidate.metadata,
                    dedup_key=str(candidate.metadata.get("dedup_key") or ""),
                    visibility=candidate.visibility,
                )
            cleanup_result = await self._cleanup_deleted_locked(
                [memory_id],
                settle_outbox=False,
                return_result=True,
            )
            if cleanup_result.get("failed"):
                return False
            metadata = self.engine._build_memory_metadata(
                session_id=request.session_id,
                persona_id=request.persona_id or None,
                importance=request.importance,
                canonical_id=memory_id,
                source=request.source,
                kind=request.kind,
                status="active",
                visibility=request.visibility,
                source_ref=request.source_ref,
            )
            metadata.update(dict(request.metadata or {}))
            await self.engine.retriever.add_memory(request.content, metadata)
            await self._clear_pending_persisted(memory_id, expected_revision)
            return True
        except Exception as exc:
            reason = self._classify_projection_exception(exc)
            scheduled = await self._mark_pending_persisted(memory_id, reason)
            logger.warning(
                f"[MemoryIndexProjector] projection degraded memory_id={memory_id} "
                f"reason={reason} repair_scheduled={str(scheduled).lower()}"
            )
            return False

    async def start(self) -> None:
        if self._retry_task is not None and not self._retry_task.done():
            return
        self._retry_stop = asyncio.Event()
        self._retry_task = asyncio.create_task(
            self._retry_loop(),
            name="astrmai-memory-projection-retry",
        )
        await self._refresh_outbox_diagnostics()

    async def stop(self) -> None:
        self._retry_stop.set()
        task = self._retry_task
        self._retry_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _retry_loop(self) -> None:
        interval = max(
            5.0,
            float(self._config_value("projection_retry_interval_sec", 60.0) or 60.0),
        )
        while not self._retry_stop.is_set():
            try:
                limit = int(self._config_value("projection_retry_batch_size", 20) or 20)
                background_budget = getattr(self.engine, "background_task_budget", None)
                if background_budget is not None:
                    await background_budget.run(
                        lambda: self.retry_due(limit=limit),
                        task_name="memory_projection",
                        scope_id="GLOBAL",
                        defer_release_on_timeout=True,
                    )
                else:
                    await self.retry_due(limit=limit)
                await asyncio.wait_for(self._retry_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except BackgroundTaskQueueFull as exc:
                if self._budget_is_draining():
                    self._retry_rejected_by_shutdown += 1
                    logger.info(
                        "[MemoryIndexProjector] retry worker stopped during shutdown: "
                        f"{exc}"
                    )
                    return
                self._retry_failure_count += 1
                logger.warning(f"[MemoryIndexProjector] retry worker degraded: {exc}")
                await asyncio.sleep(min(interval, 30.0))
            except Exception as exc:
                self._retry_failure_count += 1
                logger.warning(f"[MemoryIndexProjector] retry worker degraded: {exc}")
                await asyncio.sleep(min(interval, 30.0))

    async def retry_due(self, *, limit: int = 20) -> dict:
        store = getattr(self.engine, "v2_store", None)
        list_due = getattr(store, "list_due_projection_retries", None)
        if not callable(list_due):
            return {"attempted": 0, "projected": 0, "failed": 0}
        rows = await list_due(limit=max(1, int(limit or 20)))
        result = {"attempted": 0, "projected": 0, "failed": 0}
        items: list[dict[str, object]] = []
        for row in rows:
            memory_id = str((row or {}).get("memory_id") or "")
            if not memory_id:
                continue
            result["attempted"] += 1
            projected = await self.project(memory_id)
            item = {
                "memory_id": memory_id,
                "attempts": int((row or {}).get("attempts", 0) or 0),
                "previous_reason": str((row or {}).get("last_error", "") or ""),
                "status": "projected" if projected else "failed_retryable",
                "reason": "projected" if projected else self.pending_reason(memory_id) or "projection_failed",
            }
            items.append(item)
            if projected:
                result["projected"] += 1
                self._retry_success_count += 1
            else:
                result["failed"] += 1
                self._retry_failure_count += 1
        self._last_retry_items = items[-100:]
        if result["attempted"]:
            logger.info(
                "[MemoryIndexProjector] retry batch "
                f"attempted={result['attempted']} projected={result['projected']} failed={result['failed']}"
            )
        return result

    async def replay_pending_after_ready(self, *, limit: int = 20) -> dict:
        """Replay deferred rows immediately when the vector retriever becomes ready."""
        store = getattr(self.engine, "v2_store", None)
        snapshot = getattr(store, "projection_retry_snapshot_with_revisions", None)
        if not callable(snapshot):
            return {"attempted": 0, "projected": 0, "failed": 0, "reason": "outbox_unavailable"}
        rows = await snapshot(limit=max(1, int(limit or 20)))
        eligible = [
            memory_id
            for memory_id, payload in (rows or {}).items()
            if str((payload or {}).get("reason", "") or "") in {"retriever_not_ready", "embedding_timeout", "embedding_queue_timeout"}
        ]
        result = {"attempted": 0, "projected": 0, "failed": 0}
        items: list[dict[str, object]] = []
        for memory_id in eligible[: max(1, int(limit or 20))]:
            result["attempted"] += 1
            projected = await self.project(str(memory_id))
            if projected:
                result["projected"] += 1
                self._retry_success_count += 1
                status = "projected"
                reason = "projected"
            else:
                result["failed"] += 1
                self._retry_failure_count += 1
                status = "failed_retryable"
                reason = self.pending_reason(str(memory_id)) or "projection_failed"
            items.append({"memory_id": str(memory_id), "status": status, "reason": reason})
        self._last_retry_items = items[-100:]
        return result

    def _budget_is_draining(self) -> bool:
        budget = getattr(self.engine, "background_task_budget", None)
        status = getattr(budget, "status", None)
        if not callable(status):
            return False
        try:
            return bool((status() or {}).get("draining") is True)
        except Exception:
            return False

    async def _refresh_outbox_diagnostics(self) -> dict[str, object]:
        self._outbox_diagnostics = dict(getattr(self, "_outbox_diagnostics", {}) or {})
        store = getattr(self.engine, "v2_store", None)
        diagnostics = getattr(store, "projection_retry_diagnostics", None)
        if callable(diagnostics):
            try:
                self._outbox_diagnostics = dict(await diagnostics() or {})
            except Exception as exc:
                logger.debug(f"[MemoryIndexProjector] outbox diagnostics unavailable: {exc}")
        return dict(self._outbox_diagnostics)

    def _vector_capability_status(self) -> dict[str, object]:
        vector_retriever = getattr(self.engine, "vec_retriever", None)
        faiss_db = getattr(self.engine, "faiss_db", None)
        return {
            "retriever_present": bool(getattr(self.engine, "retriever", None)),
            "retriever_ready": self._retriever_ready_for_projection(),
            "faiss_db_present": faiss_db is not None,
            "coordinated_delete_supported": callable(getattr(vector_retriever, "delete_document", None)),
            "faiss_delete_supported": callable(getattr(faiss_db, "delete", None)),
            "vector_delete_supported": callable(getattr(vector_retriever, "delete_document", None))
            or callable(getattr(faiss_db, "delete", None)),
            "fts_delete_supported": callable(getattr(self.engine, "_execute_documents_write", None)),
            "vector_state": str(getattr(self.engine, "_vector_state", "unknown") or "unknown"),
            "rebuild_generation": int(getattr(self.engine, "_vector_generation", 0) or 0),
        }

    def describe_status(self) -> dict[str, object]:
        diagnostics = dict(getattr(self, "_outbox_diagnostics", {}) or {})
        pending_count = int(diagnostics.get("pending_count", 0) or 0)
        dead_letter_count = int(diagnostics.get("dead_letter_count", 0) or 0)
        diagnostics.update(
            {
                "pending_count": max(pending_count, len(self._pending_projection_ids)),
                "pending_count_by_reason": {
                    **dict(diagnostics.get("pending_count_by_reason", {}) or {}),
                    **({"in_memory": len(self._pending_projection_ids)} if self._pending_projection_ids else {}),
                },
                "repair_required": bool(pending_count or dead_letter_count or self._pending_projection_ids),
                "pending_projection_count": max(pending_count, len(self._pending_projection_ids)),
                "pending_by_reason": {
                    **dict(diagnostics.get("pending_count_by_reason", {}) or {}),
                    **({"in_memory": len(self._pending_projection_ids)} if self._pending_projection_ids else {}),
                },
                "retry_worker_alive": bool(self._retry_task is not None and not self._retry_task.done()),
                "retry_success_count": int(getattr(self, "_retry_success_count", 0) or 0),
                "retry_failure_count": int(getattr(self, "_retry_failure_count", 0) or 0),
                "retry_rejected_by_shutdown": int(getattr(self, "_retry_rejected_by_shutdown", 0) or 0),
                "last_retry_items": list(getattr(self, "_last_retry_items", []) or []),
                "projection_failure_by_reason": dict(getattr(self, "_projection_failure_by_reason", {}) or {}),
                "projection_deferred_by_reason": dict(getattr(self, "_projection_deferred_by_reason", {}) or {}),
                "storage_mode": "sqlite_outbox"
                if callable(getattr(getattr(self.engine, "v2_store", None), "schedule_projection_retry", None))
                else "memory_process_local",
            }
        )
        diagnostics.update(self._vector_capability_status())
        return diagnostics

    async def delete_projection(self, memory_id: str) -> int:
        return await self.cleanup_deleted([memory_id])

    async def cleanup_deleted(self, memory_ids: list[str]) -> int:
        normalized_ids = [str(memory_id or "") for memory_id in memory_ids or []]
        if self._projection_rebuild_active():
            await self._defer_projection_ids(normalized_ids, "projection_rebuild_in_progress")
            return 0
        if not await self._acquire_projection_lock():
            await self._defer_projection_ids(normalized_ids, "projection_lock_timeout")
            return 0
        if self._projection_rebuild_active():
            self._get_projection_lock().release()
            await self._defer_projection_ids(normalized_ids, "projection_rebuild_in_progress")
            return 0
        try:
            return await self._cleanup_deleted_locked(memory_ids)
        finally:
            self._get_projection_lock().release()

    async def _cleanup_deleted_locked(
        self,
        memory_ids: list[str],
        *,
        settle_outbox: bool = True,
        return_result: bool = False,
    ) -> int | dict[str, object]:
        if not memory_ids or not hasattr(self.engine, "_execute_documents_write"):
            for memory_id in memory_ids or []:
                await self._mark_pending_persisted(str(memory_id), "documents_write_unavailable")
            return {"deleted": 0, "failed": bool(memory_ids)} if return_result else 0
        deleted = 0
        failed_ids: list[str] = []
        faiss_db = getattr(self.engine, "faiss_db", None)
        faiss_delete = getattr(faiss_db, "delete", None) if faiss_db is not None else None
        vector_retriever = getattr(self.engine, "vec_retriever", None)
        coordinated_delete = getattr(vector_retriever, "delete_document", None)
        for memory_id in memory_ids:
            try:
                expected_revision = 0
                revision_reader = getattr(getattr(self.engine, "v2_store", None), "projection_retry_revision", None)
                if callable(revision_reader):
                    try:
                        expected_revision = int(await revision_reader(memory_id) or 0)
                    except Exception:
                        expected_revision = 0
                try:
                    rows = await self.engine._run_documents_query(
                        "SELECT id, doc_id FROM documents WHERE json_extract(metadata, '$.canonical_id') = ?",
                        (memory_id,),
                        db_path=self._documents_db_path(),
                    )
                except Exception as exc:
                    await self._mark_pending_persisted(memory_id, "documents_query_failed")
                    logger.warning(
                        f"[MemoryIndexProjector] documents lookup failed for {memory_id}: {exc}"
                    )
                    failed_ids.append(memory_id)
                    continue
                if not rows:
                    orphan_fts_result = await self._cleanup_orphan_fts_rows()
                    if orphan_fts_result is None or orphan_fts_result:
                        await self._mark_pending_persisted(memory_id, "fts_delete_failed")
                        failed_ids.append(memory_id)
                        continue
                    if settle_outbox:
                        await self._clear_pending_persisted(memory_id, expected_revision)
                    continue
                int_ids = [row[0] for row in rows if row]
                doc_keys = [str(row[1]) for row in rows if row and len(row) > 1 and row[1]]
                # OPT-05/ML-04: 必须走 FaissVecDB.delete 才会同步删除 embedding——
                # 旧实现只删 documents 行与 FTS，嵌入向量成为幽灵、top-k 名额被
                # 已删条目挤占且索引文件只增不减。顺序关键：faiss.delete 内部按
                # doc_id 反查 int id，必须先于任何 documents 行删除执行。
                vector_delete_supported = callable(coordinated_delete) or callable(faiss_delete)
                if not vector_delete_supported and int_ids:
                    await self._mark_pending_persisted(memory_id, "vector_delete_unavailable")
                    logger.warning(
                        f"[MemoryIndexProjector] retained document rows because vector delete is unavailable "
                        f"memory_id={memory_id}"
                    )
                    failed_ids.append(memory_id)
                    continue
                vector_delete_failed = False
                successful_rows: list[tuple[int, str]] = []
                if callable(coordinated_delete):
                    for doc_key in doc_keys:
                        try:
                            result = await coordinated_delete(doc_key)
                            if result is False:
                                raise RuntimeError(
                                    f"vector document not found during cleanup: {doc_key}"
                                )
                            row = next((item for item in rows if str(item[1]) == doc_key), None)
                            if row:
                                successful_rows.append((int(row[0]), doc_key))
                        except Exception:
                            vector_delete_failed = True
                            logger.debug(
                                f"[MemoryIndexProjector] coordinated faiss delete degraded doc_id={doc_key}",
                                exc_info=True,
                            )
                elif callable(faiss_delete):
                    for doc_key in doc_keys:
                        try:
                            await faiss_delete(doc_key)
                            row = next((item for item in rows if str(item[1]) == doc_key), None)
                            if row:
                                successful_rows.append((int(row[0]), doc_key))
                        except Exception:
                            vector_delete_failed = True
                            logger.debug(
                                f"[MemoryIndexProjector] faiss delete degraded doc_id={doc_key}",
                                exc_info=True,
                            )
                if vector_delete_supported and vector_delete_failed and not successful_rows:
                    await self._mark_pending_persisted(memory_id, "vector_delete_failed")
                    logger.warning(
                        f"[MemoryIndexProjector] retained document rows after vector delete failure "
                        f"memory_id={memory_id} repair_scheduled={str(self.retry_scheduled(memory_id)).lower()}"
                    )
                    failed_ids.append(memory_id)
                    continue
                if len(successful_rows) == len(rows):
                    documents_delete_failed = False
                    try:
                        removed_rows = await self.engine._execute_documents_write(
                            "DELETE FROM documents WHERE json_extract(metadata, '$.canonical_id') = ?",
                            (memory_id,),
                            db_path=self._documents_db_path(),
                        )
                    except Exception as exc:
                        documents_delete_failed = True
                        await self._mark_pending_persisted(memory_id, "documents_delete_failed")
                        logger.warning(
                            f"[MemoryIndexProjector] documents delete failed for {memory_id}: {exc}"
                        )
                        failed_ids.append(memory_id)
                        continue
                    deleted += max(int(removed_rows or 0), len(successful_rows))
                    fts_failed_ids = await self._delete_fts_rows(
                        [doc_id for doc_id, _ in successful_rows]
                    ) or []
                else:
                    fts_failed_ids = []
                    documents_delete_failed = False
                    for doc_id, doc_key in successful_rows:
                        try:
                            removed_rows = await self.engine._execute_documents_write(
                                "DELETE FROM documents WHERE id = ?",
                                (doc_id,),
                                db_path=self._documents_db_path(),
                            )
                        except Exception as exc:
                            documents_delete_failed = True
                            await self._mark_pending_persisted(memory_id, "documents_delete_failed")
                            logger.warning(
                                f"[MemoryIndexProjector] documents delete failed for {memory_id}: {exc}"
                            )
                            failed_ids.append(memory_id)
                            break
                        deleted += max(int(removed_rows or 0), 1)
                        fts_failed_ids.extend(await self._delete_fts_rows([doc_id]) or [])
                if documents_delete_failed or vector_delete_failed or fts_failed_ids:
                    reason = (
                        "documents_delete_failed"
                        if documents_delete_failed
                        else "vector_delete_failed"
                        if vector_delete_failed
                        else "fts_delete_failed"
                    )
                    await self._mark_pending_persisted(memory_id, reason)
                    failed_ids.append(memory_id)
                elif settle_outbox:
                    await self._clear_pending_persisted(memory_id, expected_revision)
            except Exception as exc:
                reason = f"cleanup_error:{type(exc).__name__}"
                try:
                    scheduled = await self._mark_pending_persisted(memory_id, reason)
                except Exception:
                    scheduled = False
                logger.warning(
                    f"[MemoryIndexProjector] cleanup degraded for {memory_id}: {exc} "
                    f"repair_scheduled={str(scheduled).lower()}"
                )
                failed_ids.append(memory_id)
        if return_result:
            return {"deleted": deleted, "failed": bool(failed_ids), "failed_ids": failed_ids}
        return deleted

    async def _cleanup_orphan_fts_rows(self) -> list[int] | None:
        """Remove FTS rows whose integer document IDs no longer exist."""
        if not hasattr(self.engine, "_run_documents_query"):
            return []
        try:
            rows = await self.engine._run_documents_query(
                """
                SELECT f.doc_id
                FROM memories_fts AS f
                LEFT JOIN documents AS d ON d.id = f.doc_id
                WHERE d.id IS NULL
                """,
                db_path=self._documents_db_path(),
            )
            orphan_ids = [int(row[0]) for row in rows if row and row[0] is not None]
            return await self._delete_fts_rows(orphan_ids) or []
        except Exception as exc:
            logger.warning(f"[MemoryIndexProjector] orphan FTS cleanup degraded: {exc}")
            return None

    async def rebuild_all(self) -> int:
        if not await self._acquire_projection_lock(
            timeout_sec=self._config_value("projection_rebuild_lock_timeout_sec", 60.0)
        ):
            raise asyncio.TimeoutError("projection rebuild lock timeout")
        try:
            if not await self.engine._ensure_faiss_initialized():
                return 0
            await self._clear_projected_documents()
            count = 0
            for candidate in await self.engine.v2_store.list_projectable():
                await self._project_locked(candidate.id)
                count += 1
            return count
        finally:
            self._get_projection_lock().release()

    async def rebuild_session(self, session_id: str) -> int:
        if not await self._acquire_projection_lock(
            timeout_sec=self._config_value("projection_rebuild_lock_timeout_sec", 60.0)
        ):
            raise asyncio.TimeoutError("projection rebuild lock timeout")
        try:
            if not await self.engine._ensure_faiss_initialized():
                return 0
            await self._clear_projected_documents(session_id=session_id)
            count = 0
            for candidate in await self.engine.v2_store.list_projectable(session_id=session_id):
                await self._project_locked(candidate.id)
                count += 1
            return count
        finally:
            self._get_projection_lock().release()

    async def check_consistency(self) -> dict:
        outbox_diagnostics = await self._refresh_outbox_diagnostics()
        persisted_pending: dict[str, str] = {}
        snapshot = getattr(self.engine.v2_store, "projection_retry_snapshot_with_revisions", None)
        if callable(snapshot):
            try:
                structured_pending = await snapshot()
                persisted_pending = {
                    str(memory_id): str((value or {}).get("reason", "unknown"))
                    for memory_id, value in (structured_pending or {}).items()
                }
                persisted_revisions = {
                    str(memory_id): int((value or {}).get("revision", 0))
                    for memory_id, value in (structured_pending or {}).items()
                }
            except Exception as exc:
                logger.warning(f"[MemoryIndexProjector] projection outbox snapshot degraded: {exc}")
                persisted_revisions = {}
        else:
            snapshot = getattr(self.engine.v2_store, "projection_retry_snapshot", None)
            persisted_revisions = {}
            if callable(snapshot):
                try:
                    persisted_pending = await snapshot()
                except Exception as exc:
                    logger.warning(f"[MemoryIndexProjector] projection outbox snapshot degraded: {exc}")
        pending_ids = set(self._pending_projection_ids) | set(persisted_pending)
        deferred_projection_ids: set[str] = set()
        ignored_pending: set[str] = set()
        if self._candidate_outbox_confirmations:
            ignored_pending = {
                memory_id
                for memory_id, watermark in self._candidate_outbox_confirmations.items()
                if memory_id in persisted_pending
                and memory_id in persisted_revisions
                and int(persisted_revisions[memory_id]) == int(watermark)
            }
        pending_ids -= ignored_pending
        if not self._ack_projection_outbox:
            deferred_projection_ids = set(pending_ids)
        pending_reasons = dict(persisted_pending)
        pending_reasons.update(self._pending_projection_reasons)
        pending_reasons = {
            memory_id: reason
            for memory_id, reason in pending_reasons.items()
            if memory_id in pending_ids
        }
        pending_by_reason: dict[str, int] = {}
        for reason in pending_reasons.values():
            pending_by_reason[str(reason or "unknown")] = pending_by_reason.get(str(reason or "unknown"), 0) + 1
        report = {
            "missing_projection_ids": [],
            "orphan_projection_ids": [],
            "inactive_projection_ids": [],
            "duplicate_projection_ids": [],
            "projection_count": 0,
            "canonical_projectable_count": 0,
            "pending_projection_count": len(pending_ids),
            "pending_projection_reasons": pending_reasons,
            "pending_projection_count_by_reason": dict(
                outbox_diagnostics.get("pending_count_by_reason", {}) or {}
            ),
            "pending_projection_count": len(pending_ids),
            "pending_by_reason": pending_by_reason,
            "dead_letter_count": int(outbox_diagnostics.get("dead_letter_count", 0) or 0),
            "dead_letter_count_by_reason": dict(
                outbox_diagnostics.get("dead_letter_count_by_reason", {}) or {}
            ),
            "oldest_pending_age_sec": float(
                outbox_diagnostics.get("oldest_pending_age_sec", 0.0) or 0.0
            ),
            "max_attempts": int(outbox_diagnostics.get("max_attempts", 0) or 0),
            "next_retry_at": outbox_diagnostics.get("next_retry_at"),
            "repair_required": bool(
                pending_ids or int(outbox_diagnostics.get("dead_letter_count", 0) or 0)
            ),
            "retry_worker_alive": bool(self._retry_task is not None and not self._retry_task.done()),
            "retry_success_count": int(self._retry_success_count),
            "retry_failure_count": int(self._retry_failure_count),
            "retry_rejected_by_shutdown": int(self._retry_rejected_by_shutdown),
            **self._vector_capability_status(),
            "deferred_projection_ids": sorted(deferred_projection_ids),
            "faiss_index_count": None,
            "faiss_index_count_observed": False,
            "faiss_index_count_delta_vs_projection": None,
            "faiss_id_set_observed": False,
            "faiss_ids_missing_from_documents": [],
            "document_ids_missing_from_faiss": [],
        }
        try:
            projectable = await self.engine.v2_store.list_projectable()
            projectable_ids = {item.id for item in projectable}
            report["canonical_projectable_count"] = len(projectable_ids)
            projection_rows = await self._projection_rows()
            report["projection_count"] = len(projection_rows)
            document_ids = {int(doc_id) for doc_id, _ in projection_rows}
            faiss_ids = self._faiss_id_set()
            if faiss_ids is not None:
                report["faiss_id_set_observed"] = True
                report["faiss_ids_missing_from_documents"] = sorted(faiss_ids - document_ids)
                report["document_ids_missing_from_faiss"] = sorted(document_ids - faiss_ids)
            faiss_db = getattr(self.engine, "faiss_db", None)
            if faiss_db is None:
                vector_retriever = getattr(self.engine, "vec_retriever", None)
                faiss_db = getattr(vector_retriever, "faiss_db", None)
            index = getattr(getattr(faiss_db, "embedding_storage", None), "index", None)
            ntotal = getattr(index, "ntotal", None)
            if ntotal is not None:
                try:
                    report["faiss_index_count"] = int(ntotal)
                    report["faiss_index_count_observed"] = True
                    report["faiss_index_count_delta_vs_projection"] = int(ntotal) - len(projection_rows)
                except (TypeError, ValueError):
                    pass
            by_canonical: dict[str, list[int]] = {}
            for doc_id, canonical_id in projection_rows:
                by_canonical.setdefault(canonical_id, []).append(doc_id)
            projected_ids = set(by_canonical)
            report["missing_projection_ids"] = sorted(
                (projectable_ids - projected_ids) - deferred_projection_ids
                if not self._ack_projection_outbox
                else projectable_ids - projected_ids
            )
            if pending_ids and self._ack_projection_outbox:
                report["missing_projection_ids"] = sorted(
                    set(report["missing_projection_ids"]) | (pending_ids & projectable_ids)
                )
            for canonical_id, doc_ids in by_canonical.items():
                if len(doc_ids) > 1:
                    report["duplicate_projection_ids"].append(canonical_id)
                candidate = await self.engine.v2_store.get_canonical(canonical_id, include_inactive=True)
                if not candidate:
                    report["orphan_projection_ids"].append(canonical_id)
                    continue
                if candidate.status != "active" or candidate.visibility not in {"auto_and_tool", "tool_only"}:
                    report["inactive_projection_ids"].append(canonical_id)
        except Exception as exc:
            report["error"] = str(exc)
        return report

    async def repair_consistency(self, report: dict | None = None) -> dict:
        report = report or await self.check_consistency()
        repaired = {
            "rebuilt_missing": 0,
            "rebuilt_index": 0,
            "deleted_orphan": 0,
            "deleted_inactive": 0,
            "deduplicated": 0,
            "remaining_pending": len(self._pending_projection_ids),
            "remaining_pending_reasons": dict(self._pending_projection_reasons),
        }
        if report.get("faiss_ids_missing_from_documents") or report.get("document_ids_missing_from_faiss"):
            repaired["rebuilt_index"] = await self.rebuild_all()
            report = await self.check_consistency()
        for memory_id in report.get("missing_projection_ids", []) or []:
            if await self.project(str(memory_id)):
                repaired["rebuilt_missing"] += 1
        removable = set(report.get("orphan_projection_ids", []) or []) | set(report.get("inactive_projection_ids", []) or [])
        if removable:
            repaired["deleted_orphan"] = await self.cleanup_deleted(sorted(removable & set(report.get("orphan_projection_ids", []) or [])))
            repaired["deleted_inactive"] = await self.cleanup_deleted(sorted(removable & set(report.get("inactive_projection_ids", []) or [])))
        for memory_id in report.get("duplicate_projection_ids", []) or []:
            if await self.project(str(memory_id)):
                repaired["deduplicated"] += 1
        repaired["remaining_pending"] = len(self._pending_projection_ids)
        repaired["remaining_pending_reasons"] = dict(self._pending_projection_reasons)
        return repaired

    async def _clear_projected_documents(self, *, session_id: str = "") -> int:
        if not hasattr(self.engine, "_execute_documents_write"):
            return 0
        if session_id:
            rows = await self.engine._run_documents_query(
                """
                SELECT id, doc_id FROM documents
                WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL
                  AND json_extract(metadata, '$.session_id') = ?
                """,
                (session_id,),
                db_path=self._documents_db_path(),
            )
        else:
            rows = await self.engine._run_documents_query(
                "SELECT id, doc_id FROM documents WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL",
                db_path=self._documents_db_path(),
            )

        int_ids = [int(row[0]) for row in rows if row and row[0] is not None]
        doc_keys = [str(row[1]) for row in rows if row and len(row) > 1 and row[1]]
        vector_retriever = getattr(self.engine, "vec_retriever", None)
        coordinated_delete = getattr(vector_retriever, "delete_document", None)
        faiss_db = getattr(self.engine, "faiss_db", None)
        faiss_delete = getattr(faiss_db, "delete", None)
        if doc_keys and not callable(coordinated_delete) and not callable(faiss_delete):
            raise RuntimeError("vector delete capability unavailable during projection rebuild")
        for doc_key in doc_keys:
            if callable(coordinated_delete):
                deleted = await coordinated_delete(doc_key)
                if deleted is False:
                    raise RuntimeError(f"vector document not found during projection rebuild: {doc_key}")
            else:
                await faiss_delete(doc_key)

        if session_id:
            await self.engine._execute_documents_write(
                """
                DELETE FROM documents
                WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL
                  AND json_extract(metadata, '$.session_id') = ?
                """,
                (session_id,),
                db_path=self._documents_db_path(),
            )
        else:
            await self.engine._execute_documents_write(
                "DELETE FROM documents WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL",
                db_path=self._documents_db_path(),
            )
        fts_failed_ids = await self._delete_fts_rows(int_ids)
        if fts_failed_ids:
            raise RuntimeError(
                f"FTS cleanup failed for document IDs: {', '.join(str(item) for item in fts_failed_ids)}"
            )
        return len(rows)

    async def _projection_rows(self) -> list[tuple[int, str]]:
        if not hasattr(self.engine, "_run_documents_query"):
            return []
        try:
            rows = await self.engine._run_documents_query(
                "SELECT id, metadata FROM documents WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL",
                db_path=self._documents_db_path(),
            )
        except Exception as exc:
            logger.warning(f"[MemoryIndexProjector] consistency scan degraded: {exc}")
            raise
        result: list[tuple[int, str]] = []
        for row in rows:
            try:
                doc_id = int(row[0])
                metadata = row[1] or "{}"
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                canonical_id = str((metadata or {}).get("canonical_id") or "")
                if canonical_id:
                    result.append((doc_id, canonical_id))
            except Exception:
                continue
        return result

    def _faiss_id_set(self) -> set[int] | None:
        faiss_db = getattr(self.engine, "faiss_db", None)
        if faiss_db is None:
            faiss_db = getattr(getattr(self.engine, "vec_retriever", None), "faiss_db", None)
        index = getattr(getattr(faiss_db, "embedding_storage", None), "index", None)
        id_map = getattr(index, "id_map", None)
        if id_map is None:
            return None
        try:
            return {int(value) for value in id_map.tolist()}
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            return {int(value) for value in id_map}
        except (TypeError, ValueError):
            pass
        try:
            import faiss

            values = faiss.vector_to_array(id_map)
            return {int(value) for value in values.tolist()}
        except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
            return None

    async def projection_count(self) -> int:
        return len(await self._projection_rows())

    async def _delete_fts_rows(self, doc_ids: list[int]) -> list[int]:
        ids = [item for item in doc_ids if item is not None]
        if not ids or not hasattr(self.engine, "_execute_documents_write"):
            return []
        failed_ids: list[int] = []
        for doc_id in ids:
            try:
                await self.engine._execute_documents_write(
                    "DELETE FROM memories_fts WHERE doc_id = ?",
                    (doc_id,),
                    db_path=self._documents_db_path(),
                )
            except Exception as exc:
                failed_ids.append(int(doc_id))
                logger.warning(f"[MemoryIndexProjector] fts cleanup degraded for {doc_id}: {exc}")
        return failed_ids


__all__ = ["MemoryIndexProjector"]
