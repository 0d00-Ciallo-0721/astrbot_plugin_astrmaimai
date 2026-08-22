import asyncio
import inspect
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import List, Dict, Any, Optional

import numpy as np
from astrbot.api import logger
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from ...infrastructure.runtime.turn_call_ledger import clamp_timeout_to_turn_budget
from ..utils import SearchResult, TextProcessor


class _QueryQueueTimeout(asyncio.TimeoutError):
    """Raised when a vector query cannot obtain a bounded execution slot."""


class _VectorStageTimeout(asyncio.TimeoutError):
    def __init__(self, stage: str):
        super().__init__(stage)
        self.stage = stage

class VectorRetriever:
    """
    向量密集检索器 (基于 AstrBot FaissVecDB 原生底座)
    完全重构：废弃脆弱的本地 bin 文件维护，全面接入平台提供的一致性存储。
    """
    def __init__(self, faiss_db: FaissVecDB, config=None, *, projection_count_provider=None):
        self.faiss_db = faiss_db
        self.processor = TextProcessor()
        self.config = config or {}
        # ID 映射缓存优化 (int_id -> uuid)
        self._id_cache: Dict[int, str] = {}
        self._cache_max_size = 1000
        self._failure_count = 0
        self._unavailable_until = 0.0
        self._query_limit = self._query_concurrency()
        self._query_semaphore = asyncio.Semaphore(self._query_limit)
        self._active_queries = 0
        self._index_executor = ThreadPoolExecutor(
            max_workers=self._query_limit,
            thread_name_prefix="astrmai-faiss",
        )
        self._index_lock = threading.RLock()
        self._active_index_jobs = 0
        self._background_index_tasks: set[asyncio.Task] = set()
        self._half_open_probe_active = False
        self._status_counts: Dict[str, int] = {}
        self._last_stage_timings: Dict[str, float] = {}
        self._timeout_origin_counts: Dict[str, int] = {}
        self._stage_latency_samples: Dict[str, list[float]] = {}
        self._index_lock_wait_samples: list[float] = []
        self._projection_count_provider = projection_count_provider
        self._document_count_cache: int | None = None
        self._projection_count_cache: int | None = None
        self._storage_metrics_refreshed_at = 0.0
        self._storage_metrics_refreshed_mono = 0.0
        self._storage_metrics_task: asyncio.Task | None = None

    def _timing_value(self, name: str, default: float) -> float:
        timing = getattr(self.config, "timing", None)
        if timing is None and isinstance(self.config, dict):
            timing = self.config.get("timing")
        value = getattr(timing, name, None) if timing is not None else None
        if value is None and isinstance(timing, dict):
            value = timing.get(name)
        try:
            return float(value if value is not None else default)
        except (TypeError, ValueError):
            return float(default)

    def _failure_threshold(self) -> int:
        return max(1, int(self._timing_value("faiss_failure_threshold", 3.0)))

    def _query_concurrency(self) -> int:
        return max(1, min(8, int(self._timing_value("faiss_query_concurrency", 2.0))))

    def _faiss_thread_count(self) -> int:
        return max(1, min(8, int(self._timing_value("faiss_thread_count", 1.0))))

    def refresh_config(self, config) -> None:
        self.config = config or {}
        configured = self._query_concurrency()
        if configured != self._query_limit and self._active_queries == 0:
            previous_executor = self._index_executor
            self._query_limit = configured
            self._query_semaphore = asyncio.Semaphore(configured)
            self._index_executor = ThreadPoolExecutor(
                max_workers=configured,
                thread_name_prefix="astrmai-faiss",
            )
            previous_executor.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        metrics_task = self._storage_metrics_task
        if metrics_task is not None and not metrics_task.done():
            metrics_task.cancel()
        self._index_executor.shutdown(wait=False, cancel_futures=True)

    def _supports_phased_retrieval(self) -> bool:
        return all(
            getattr(self.faiss_db, name, None) is not None
            for name in ("embedding_provider", "embedding_storage", "document_storage")
        ) and getattr(getattr(self.faiss_db, "embedding_storage", None), "index", None) is not None

    @staticmethod
    def _remaining_timeout(deadline: float, stage: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise _VectorStageTimeout(stage)
        return remaining

    async def _await_stage(self, awaitable, *, deadline: float, stage: str, timings: Dict[str, float]):
        stage_started = time.monotonic()
        try:
            timeout = self._remaining_timeout(deadline, stage)
            if stage == "embedding":
                timeout = min(
                    timeout,
                    max(0.1, self._timing_value("embedding_timeout_sec", 15.0)),
                )
            return await asyncio.wait_for(
                awaitable,
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise _VectorStageTimeout(stage) from exc
        finally:
            timings[f"{stage}_ms"] = round((time.monotonic() - stage_started) * 1000.0, 1)

    def _search_index_sync(self, vector: np.ndarray, k: int):
        import faiss

        lock_started = time.monotonic()
        self._index_lock.acquire()
        self._record_index_lock_wait((time.monotonic() - lock_started) * 1000.0)
        try:
            self._active_index_jobs += 1
            try:
                faiss.omp_set_num_threads(self._faiss_thread_count())
                faiss.normalize_L2(vector)
                return self.faiss_db.embedding_storage.index.search(vector, k)
            finally:
                self._active_index_jobs = max(0, self._active_index_jobs - 1)
        finally:
            self._index_lock.release()

    def _insert_index_sync(self, vector: np.ndarray, doc_id: int) -> None:
        import faiss

        storage = self.faiss_db.embedding_storage
        lock_started = time.monotonic()
        self._index_lock.acquire()
        self._record_index_lock_wait((time.monotonic() - lock_started) * 1000.0)
        try:
            self._active_index_jobs += 1
            try:
                storage.index.add_with_ids(
                    vector.reshape(1, -1),
                    np.array([doc_id], dtype=np.int64),
                )
                if getattr(storage, "path", None):
                    faiss.write_index(storage.index, storage.path)
            finally:
                self._active_index_jobs = max(0, self._active_index_jobs - 1)
        finally:
            self._index_lock.release()

    def _delete_index_sync(self, doc_id: int) -> None:
        import faiss

        storage = self.faiss_db.embedding_storage
        lock_started = time.monotonic()
        self._index_lock.acquire()
        self._record_index_lock_wait((time.monotonic() - lock_started) * 1000.0)
        try:
            self._active_index_jobs += 1
            try:
                storage.index.remove_ids(np.array([doc_id], dtype=np.int64))
                if getattr(storage, "path", None):
                    faiss.write_index(storage.index, storage.path)
            finally:
                self._active_index_jobs = max(0, self._active_index_jobs - 1)
        finally:
            self._index_lock.release()

    def _record_index_lock_wait(self, wait_ms: float) -> None:
        self._index_lock_wait_samples.append(round(max(0.0, float(wait_ms or 0.0)), 1))
        if len(self._index_lock_wait_samples) > 512:
            del self._index_lock_wait_samples[:-512]

    async def _run_index_job(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._index_executor, func, *args)

    async def _retrieve_phased(
        self,
        *,
        query: str,
        k: int,
        fetch_k: int,
        metadata_filters: Optional[Dict[str, Any]],
        deadline: float,
        timings: Dict[str, float],
    ):
        try:
            embedding = await self._await_stage(
                self.faiss_db.embedding_provider.get_embedding(query),
                deadline=deadline,
                stage="embedding",
                timings=timings,
            )
        except _VectorStageTimeout as exc:
            setattr(exc, "stage_timings", dict(timings))
            raise
        vector = np.array([embedding], dtype=np.float32)
        index_started = time.monotonic()
        index_task = asyncio.create_task(
            self._run_index_job(
                self._search_index_sync,
                vector,
                fetch_k if metadata_filters else k,
            )
        )
        try:
            scores, indices = await asyncio.wait_for(
                asyncio.shield(index_task),
                timeout=self._remaining_timeout(deadline, "faiss_index"),
            )
        except asyncio.TimeoutError as exc:
            setattr(exc, "astrmai_background_index_task", index_task)
            stage_timeout = _VectorStageTimeout("faiss_index")
            setattr(stage_timeout, "stage_timings", dict(timings))
            setattr(stage_timeout, "astrmai_background_index_task", index_task)
            raise stage_timeout from exc
        except asyncio.CancelledError as exc:
            setattr(exc, "astrmai_background_index_task", index_task)
            raise
        finally:
            timings["faiss_index_ms"] = round((time.monotonic() - index_started) * 1000.0, 1)

        scores = np.asarray(scores)
        indices = np.asarray(indices)
        if len(indices[0]) == 0 or indices[0][0] == -1:
            return []
        scores[0] = 1.0 - (scores[0] / 2.0)
        try:
            fetched_docs = await self._await_stage(
                self.faiss_db.document_storage.get_documents(
                    metadata_filters=metadata_filters or {},
                    ids=indices[0],
                ),
                deadline=deadline,
                stage="document_read",
                timings=timings,
            )
        except _VectorStageTimeout as exc:
            setattr(exc, "stage_timings", dict(timings))
            raise
        if not fetched_docs:
            return []
        indexed_docs = {doc["id"]: doc for doc in fetched_docs}
        results = []
        for position, index_id in enumerate(indices[0]):
            document = indexed_docs.get(index_id)
            if document is not None:
                results.append(
                    SimpleNamespace(
                        similarity=float(scores[0][position]),
                        data=document,
                    )
                )
        return results[:k]

    def _release_query_slot(self) -> None:
        self._active_queries = max(0, self._active_queries - 1)
        self._query_semaphore.release()

    def _finish_background_index_task(self, task: asyncio.Task) -> None:
        self._background_index_tasks.discard(task)
        try:
            task.result()
        except BaseException:
            # The foreground query already received the timeout. Consume any
            # late worker exception so it cannot become an unhandled task error.
            pass
        self._release_query_slot()

    async def _retrieve_with_budget(self, **kwargs):
        timeout = max(0.1, float(kwargs.pop("timeout", 0.1) or 0.1))
        wait_started = time.monotonic()
        if self._query_semaphore.locked():
            try:
                await asyncio.wait_for(self._query_semaphore.acquire(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise _QueryQueueTimeout from exc
        else:
            await self._query_semaphore.acquire()
        wait_ms = round((time.monotonic() - wait_started) * 1000.0, 1)
        self._active_queries += 1
        release_on_exit = True
        stage_timings: Dict[str, float] = {}
        try:
            remaining = max(0.1, timeout - wait_ms / 1000.0)
            deadline = time.monotonic() + remaining
            if self._supports_phased_retrieval():
                try:
                    results = await self._retrieve_phased(
                        query=kwargs["query"],
                        k=kwargs["k"],
                        fetch_k=kwargs["fetch_k"],
                        metadata_filters=kwargs.get("metadata_filters"),
                        deadline=deadline,
                        timings=stage_timings,
                    )
                except BaseException as exc:
                    setattr(exc, "stage_timings", dict(stage_timings))
                    background_task = getattr(exc.__cause__, "astrmai_background_index_task", None)
                    if background_task is None:
                        background_task = getattr(exc, "astrmai_background_index_task", None)
                    if background_task is not None and not background_task.done():
                        release_on_exit = False
                        self._background_index_tasks.add(background_task)
                        background_task.add_done_callback(self._finish_background_index_task)
                    raise
            else:
                stage_started = time.monotonic()
                try:
                    results = await asyncio.wait_for(
                        self.faiss_db.retrieve(**kwargs),
                        timeout=self._remaining_timeout(deadline, "faiss_db.retrieve"),
                    )
                except asyncio.TimeoutError as exc:
                    raise _VectorStageTimeout("faiss_db.retrieve") from exc
                finally:
                    stage_timings["faiss_db_retrieve_ms"] = round(
                        (time.monotonic() - stage_started) * 1000.0,
                        1,
                    )
            return results, wait_ms, stage_timings
        finally:
            if release_on_exit:
                self._release_query_slot()

    def _circuit_open(self) -> bool:
        return time.monotonic() < self._unavailable_until

    def _begin_circuit_probe(self) -> bool:
        if self._circuit_open():
            return False
        if self._failure_count < self._failure_threshold():
            return True
        if self._half_open_probe_active:
            return False
        self._half_open_probe_active = True
        return True

    def _mark_failure(self, reason: str) -> None:
        self._half_open_probe_active = False
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold():
            cooldown = max(5.0, self._timing_value("faiss_circuit_breaker_cooldown_sec", 180.0))
            self._unavailable_until = time.monotonic() + cooldown
            logger.warning(
                f"[VectorStore] Faiss circuit opened: failures={self._failure_count} "
                f"cooldown_sec={cooldown:.1f} reason={reason}"
            )

    def _mark_success(self) -> None:
        self._half_open_probe_active = False
        self._failure_count = 0
        self._unavailable_until = 0.0

    def describe_status(self) -> Dict[str, Any]:
        total = sum(self._status_counts.values())
        degraded = sum(
            count
            for status, count in self._status_counts.items()
            if status in {"timeout", "query_queue_timeout", "error", "circuit_open"}
        )
        index_count = self._index_count()
        projection_count = self._projection_count_cache
        document_count = self._document_count_cache
        return {
            "query_concurrency": int(self._query_limit),
            "faiss_thread_count": int(self._faiss_thread_count()),
            "active_queries": int(self._active_queries),
            "active_index_jobs": int(self._active_index_jobs),
            "background_index_jobs": int(len(self._background_index_tasks)),
            "failure_count": int(self._failure_count),
            "circuit_open": bool(self._circuit_open()),
            "half_open_probe_active": bool(self._half_open_probe_active),
            "total_queries": int(total),
            "degraded_queries": int(degraded),
            "degraded_ratio": round(degraded / total, 4) if total else 0.0,
            "status_counts": dict(self._status_counts),
            "timeout_origin_counts": dict(self._timeout_origin_counts),
            "stage_latency_ms": {
                stage: self._latency_summary(samples)
                for stage, samples in self._stage_latency_samples.items()
            },
            "index_lock_wait_ms": self._latency_summary(self._index_lock_wait_samples),
            "index_ntotal": index_count,
            "document_storage_count": document_count,
            "projection_count": projection_count,
            "index_delta_vs_projection": (
                index_count - projection_count
                if index_count is not None and projection_count is not None
                else None
            ),
            "document_delta_vs_projection": (
                document_count - projection_count
                if document_count is not None and projection_count is not None
                else None
            ),
            "storage_metrics_refreshed_at": self._storage_metrics_refreshed_at or None,
            "last_stage_timings": dict(self._last_stage_timings),
        }

    def _index_count(self) -> int | None:
        index = getattr(getattr(self.faiss_db, "embedding_storage", None), "index", None)
        try:
            value = getattr(index, "ntotal", None)
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    async def _resolve_count(source: Any, names: tuple[str, ...]) -> int | None:
        for name in names:
            value = getattr(source, name, None)
            if callable(value):
                try:
                    value = value()
                    if inspect.isawaitable(value):
                        value = await value
                except Exception:
                    continue
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None

    async def refresh_storage_metrics(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._storage_metrics_refreshed_mono < 30.0:
            return
        storage = getattr(self.faiss_db, "document_storage", None)
        self._document_count_cache = await self._resolve_count(
            storage,
            ("count_documents", "count", "document_count", "size"),
        )
        provider = self._projection_count_provider
        if callable(provider):
            try:
                value = provider()
                if inspect.isawaitable(value):
                    value = await value
                self._projection_count_cache = int(value) if value is not None else None
            except Exception:
                self._projection_count_cache = None
        self._storage_metrics_refreshed_at = time.time()
        self._storage_metrics_refreshed_mono = time.monotonic()

    def _schedule_storage_metrics_refresh(self) -> None:
        task = self._storage_metrics_task
        if task is not None and not task.done():
            return
        if time.monotonic() - self._storage_metrics_refreshed_mono < 30.0:
            return
        try:
            task = asyncio.create_task(
                self.refresh_storage_metrics(),
                name="astrmai-vector-storage-metrics",
            )
        except RuntimeError:
            return
        self._storage_metrics_task = task
        task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)

    @staticmethod
    def _latency_summary(samples: list[float]) -> Dict[str, float | int]:
        if not samples:
            return {"count": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        ordered = sorted(float(value) for value in samples)
        def percentile(ratio: float) -> float:
            index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * ratio)))
            return round(ordered[index], 1)
        return {
            "count": len(ordered),
            "avg": round(sum(ordered) / len(ordered), 1),
            "p50": percentile(0.50),
            "p95": percentile(0.95),
            "max": round(ordered[-1], 1),
        }

    @staticmethod
    def _normalize_metadata(raw_metadata: Any) -> Dict[str, Any]:
        if isinstance(raw_metadata, dict):
            return dict(raw_metadata)
        if isinstance(raw_metadata, str):
            try:
                parsed = json.loads(raw_metadata)
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.warning("[VectorStore] ignoring malformed JSON metadata from Faiss result")
                return {}
            if isinstance(parsed, dict):
                return parsed
            logger.warning("[VectorStore] ignoring non-object JSON metadata from Faiss result")
        return {}

    async def add_document(self, content: str, metadata: Dict[str, Any] = None) -> int:
        """存入文本，返回 document id (由 FaissVecDB 底层的 DocumentStorage 提供的主键)"""
        metadata = metadata or {}
        
        # 补充默认字段
        if "importance" not in metadata:
            metadata["importance"] = 0.5
        if "create_time" not in metadata:
            metadata["create_time"] = time.time()
        if "last_access_time" not in metadata:
            metadata["last_access_time"] = time.time()
            
        if not self._supports_phased_retrieval():
            return await self.faiss_db.insert(content=content, metadata=metadata)

        embedding = await asyncio.wait_for(
            self.faiss_db.embedding_provider.get_embedding(content),
            timeout=max(0.1, self._timing_value("embedding_timeout_sec", 15.0)),
        )
        vector = np.asarray(embedding, dtype=np.float32)
        expected_dimension = int(
            getattr(self.faiss_db.embedding_storage, "dimension", vector.shape[0])
            or vector.shape[0]
        )
        if vector.shape[0] != expected_dimension:
            raise ValueError(
                f"embedding dimension mismatch: expected={expected_dimension} actual={vector.shape[0]}"
            )
        doc_id = await self.faiss_db.document_storage.insert_document(
            str(uuid.uuid4()),
            content,
            metadata,
        )
        await self._run_index_job(self._insert_index_sync, vector, int(doc_id))
        return int(doc_id)

    async def delete_document(self, doc_key: str) -> bool:
        if not self._supports_phased_retrieval():
            await self.faiss_db.delete(doc_key)
            return True
        document = await self.faiss_db.document_storage.get_document_by_doc_id(doc_key)
        if not document:
            return False
        await self._run_index_job(self._delete_index_sync, int(document["id"]))
        await self.faiss_db.document_storage.delete_document_by_doc_id(doc_key)
        return True

    def _record_observation(
        self,
        observation: Optional[Dict[str, Any]],
        *,
        status: str,
        started_at: float,
        timeout_sec: float,
        configured_timeout_sec: float | None = None,
        result_count: int = 0,
        error_type: str = "",
        error_detail: str = "",
        requested_k: int = 0,
        fetch_k: int = 0,
        metadata_filter_count: int = 0,
        timeout_origin: str = "",
        stage_timings: Optional[Dict[str, float]] = None,
        query_queue_wait_ms: float = 0.0,
    ) -> None:
        normalized_status = str(status or "unknown")
        self._status_counts[normalized_status] = self._status_counts.get(normalized_status, 0) + 1
        self._last_stage_timings = dict(stage_timings or {})
        normalized_origin = str(timeout_origin or "").strip()
        if normalized_origin:
            self._timeout_origin_counts[normalized_origin] = (
                self._timeout_origin_counts.get(normalized_origin, 0) + 1
            )
        for stage, value in (stage_timings or {}).items():
            if not str(stage).endswith("_ms"):
                continue
            samples = self._stage_latency_samples.setdefault(str(stage), [])
            samples.append(round(max(0.0, float(value or 0.0)), 1))
            if len(samples) > 512:
                del samples[:-512]
        if query_queue_wait_ms:
            queue_samples = self._stage_latency_samples.setdefault("query_queue_wait_ms", [])
            queue_samples.append(round(max(0.0, float(query_queue_wait_ms)), 1))
            if len(queue_samples) > 512:
                del queue_samples[:-512]
        if observation is None:
            return
        cooldown_remaining = max(0.0, self._unavailable_until - time.monotonic())
        configured_timeout = float(
            configured_timeout_sec if configured_timeout_sec is not None else timeout_sec or 0.0
        )
        effective_timeout = float(timeout_sec or 0.0)
        observation.clear()
        observation.update(
            {
                "status": normalized_status,
                "retrieve_stage": "phased" if self._supports_phased_retrieval() else "faiss_db.retrieve",
                "timeout_origin": str(timeout_origin or ""),
                "elapsed_ms": round(max(0.0, time.monotonic() - started_at) * 1000.0, 1),
                "timeout_sec": round(max(0.0, effective_timeout), 3),
                "configured_timeout_sec": round(max(0.0, configured_timeout), 3),
                "effective_timeout_sec": round(max(0.0, effective_timeout), 3),
                "timeout_budget_clamped": bool(effective_timeout + 0.001 < configured_timeout),
                "failure_threshold": max(1, int(self._failure_threshold() or 1)),
                "cooldown_sec": round(
                    max(5.0, self._timing_value("faiss_circuit_breaker_cooldown_sec", 180.0)),
                    3,
                ),
                "requested_k": max(0, int(requested_k or 0)),
                "fetch_k": max(0, int(fetch_k or 0)),
                "metadata_filter_count": max(0, int(metadata_filter_count or 0)),
                "result_count": max(0, int(result_count or 0)),
                "failure_count": max(0, int(self._failure_count or 0)),
                "circuit_open": bool(self._circuit_open()),
                "cooldown_remaining_sec": round(cooldown_remaining, 3),
                "error_type": str(error_type or ""),
                "error_detail": str(error_detail or "")[:240],
                "query_concurrency": int(self._query_limit),
                "active_queries": int(self._active_queries),
                "faiss_thread_count": int(self._faiss_thread_count()),
                "query_queue_wait_ms": round(max(0.0, float(query_queue_wait_ms or 0.0)), 1),
                "stage_timings": dict(stage_timings or {}),
                "runtime_metrics": self.describe_status(),
            }
        )

    async def search(
        self,
        query: str,
        k: int = 10,
        session_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        observation: Optional[Dict[str, Any]] = None,
    ) -> List[SearchResult]:
        """执行向量相似度搜索"""
        started_at = time.monotonic()
        configured_timeout_sec = max(0.5, self._timing_value("faiss_timeout_sec", 20.0))
        timeout_sec = clamp_timeout_to_turn_budget(
            None,
            configured_timeout_sec,
            reserve_for_reply=True,
        )
        timeout_sec = max(0.1, float(timeout_sec or 0.0))
        if not query or not query.strip():
            self._record_observation(
                observation,
                status="empty_query",
                started_at=started_at,
                timeout_sec=timeout_sec,
                configured_timeout_sec=configured_timeout_sec,
                requested_k=k,
            )
            return []
        if not self._begin_circuit_probe():
            logger.warning("[VectorStore] Faiss circuit open; using lexical fallback")
            self._record_observation(
                observation,
                status="circuit_open",
                started_at=started_at,
                timeout_sec=timeout_sec,
                configured_timeout_sec=configured_timeout_sec,
                requested_k=k,
            )
            return []
            
        # 预处理查询
        tokens = self.processor.tokenize(query)
        processed_query = " ".join(tokens) if tokens else query

        # 构建元数据过滤器
        metadata_filters = {}
        if session_id is not None:
            metadata_filters["session_id"] = session_id
        if persona_id is not None:
            metadata_filters["persona_id"] = persona_id

        fetch_k = k * 2 if metadata_filters else k
        metadata_filter_count = len(metadata_filters)

        # 执行原生检索
        try:
            faiss_results, query_wait_ms, stage_timings = await self._retrieve_with_budget(
                query=processed_query,
                k=k,
                fetch_k=fetch_k,
                rerank=False,
                metadata_filters=metadata_filters if metadata_filters else None,
                timeout=timeout_sec,
            )
            self._mark_success()
        except _QueryQueueTimeout:
            self._mark_failure("query_queue_timeout")
            logger.warning("[VectorStore] query slot wait timed out; using lexical fallback")
            query_wait_ms = round(
                max(0.0, time.monotonic() - started_at) * 1000.0,
                1,
            )
            self._record_observation(
                observation,
                status="query_queue_timeout",
                started_at=started_at,
                timeout_sec=timeout_sec,
                configured_timeout_sec=configured_timeout_sec,
                error_type="TimeoutError",
                requested_k=k,
                fetch_k=fetch_k,
                metadata_filter_count=metadata_filter_count,
                query_queue_wait_ms=query_wait_ms,
            )
            return []
        except asyncio.CancelledError:
            self._half_open_probe_active = False
            raise
        except _VectorStageTimeout as exc:
            self._mark_failure("timeout")
            logger.warning(
                f"[VectorStore] Faiss search timed out at {exc.stage}; using lexical fallback"
            )
            self._record_observation(
                observation,
                status="timeout",
                started_at=started_at,
                timeout_sec=timeout_sec,
                configured_timeout_sec=configured_timeout_sec,
                error_type="TimeoutError",
                requested_k=k,
                fetch_k=fetch_k,
                metadata_filter_count=metadata_filter_count,
                timeout_origin=exc.stage,
                stage_timings=getattr(exc, "stage_timings", {}),
                query_queue_wait_ms=locals().get("query_wait_ms", 0.0),
            )
            return []
        except Exception as e:
            self._mark_failure(type(e).__name__)
            logger.error(f"[VectorStore] Faiss 原生检索异常: {e}")
            self._record_observation(
                observation,
                status="error",
                started_at=started_at,
                timeout_sec=timeout_sec,
                configured_timeout_sec=configured_timeout_sec,
                error_type=type(e).__name__,
                error_detail=str(e),
                requested_k=k,
                fetch_k=fetch_k,
                metadata_filter_count=metadata_filter_count,
                stage_timings=getattr(e, "stage_timings", {}),
            )
            return []

        out = []
        for result in faiss_results:
            doc_data = getattr(result, "data", {}) or {}
            doc_id = doc_data.get("id")
            content = doc_data.get("text")
            if doc_id is None or content is None:
                logger.warning(f"[VectorStore] skipping malformed Faiss result: {doc_data}")
                continue
            out.append(SearchResult(
                doc_id=doc_id,
                score=float(getattr(result, "similarity", 0.0) or 0.0),
                content=content,
                metadata=self._normalize_metadata(doc_data.get("metadata")),
                source="vector"
            ))

        self._record_observation(
            observation,
            status="success",
            started_at=started_at,
            timeout_sec=timeout_sec,
            configured_timeout_sec=configured_timeout_sec,
            result_count=len(out),
            requested_k=k,
            fetch_k=fetch_k,
            metadata_filter_count=metadata_filter_count,
            stage_timings=stage_timings,
            query_queue_wait_ms=query_wait_ms,
        )
        self._schedule_storage_metrics_refresh()
        return out
