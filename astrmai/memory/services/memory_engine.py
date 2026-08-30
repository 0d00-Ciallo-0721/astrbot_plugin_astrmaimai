import aiosqlite
import asyncio
import hashlib
import inspect
import json
import os
import threading
import time
import uuid
from urllib.parse import urlparse
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import numpy as np

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from ...learning.dedup import (
    GLOBAL_JARGON_SESSION_ID,
    expression_fingerprint,
    jargon_fingerprint,
)

try:
    from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
    HAS_FAISS = True
except ImportError:
    FaissVecDB = None
    HAS_FAISS = False
    logger.warning("[AstrMai] faiss unavailable; vector memory features disabled.")

from ..retrieval.bm25 import BM25Retriever
from ..retrieval.hybrid_retriever import HybridRetriever
from ..retrieval.vector_store import VectorRetriever
from ..retrieval.embedding import invoke_embedding
from ..contracts.memory_query import MemoryQuery, MemoryWriteRequest
from .expression_pattern_service import ExpressionPatternService
from .cognitive_feedback import (
    FEEDBACK_SCHEMA_VERSION,
    normalize_payload,
    render_feedback,
    source_label,
)
from .instant_memory_gate import InstantMemoryGate
from .memory_index_projector import MemoryIndexProjector
from .memory_injection_service import MemoryInjectionService
from .memory_maintenance_service import MemoryMaintenanceService
from .memory_migration_service import MemoryMigrationService
from .memory_observer import MemoryObserver
from .memory_retrieval_service import MemoryRetrievalService
from .memory_turn_pipeline import MemoryTurnPipeline
from .memory_tool_service import MemoryToolService
from .memory_write_service import MemoryWriteService
from .session_memory_summarizer import SessionMemorySummarizer
from .v2_store import MemoryV2Store
from ...infrastructure.persistence.memory_turn_checkpoint import MemoryTurnCheckpointStore


@dataclass(slots=True)
class CognitiveFeedbackSignal:
    source: str
    chat_id: str
    summary: str
    guidance: str
    tags: list[str] = field(default_factory=list)
    timestamp: float = 0.0
    importance: float = 0.5
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VectorIndexDescriptor:
    """Published vector index metadata, including the physical dimension."""

    generation: int
    index_file: str
    embedding_models: tuple[str, ...]
    provider_source_id: str = ""
    api_base_fingerprint: str = ""
    dimension: int | None = None
    metric: str = "cosine"
    document_count: int | None = None
    vector_count: int | None = None
    created_at: float = 0.0
    status: str = "published"

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": int(self.generation),
            "file_name": self.index_file,
            "index_file": self.index_file,
            "embedding_models": list(self.embedding_models),
            "provider_source_id": self.provider_source_id,
            "api_base_fingerprint": self.api_base_fingerprint,
            "dimension": self.dimension,
            "metric": self.metric,
            "document_count": self.document_count,
            "vector_count": self.vector_count,
            "created_at": self.created_at,
            "published_at": self.created_at,
            "status": self.status,
        }


class MemoryEngine:
    """Refactored memory engine with lazy vector bootstrap and stable facade methods."""

    DISABLE_TTL_SEC = 7 * 86400  # ponytail: 7-day TTL for disabled feedback keys
    STARTUP_CPU_SLICE_MS = 25.0
    STARTUP_BATCH_SIZE = 32
    STARTUP_YIELD_SEC = 0.001

    def __init__(self, context, gateway, embedding_models: list = None, config=None):
        self.context = context
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.db_service = None
        if hasattr(self.config, "provider") and getattr(self.config.provider, "embedding_models", None):
            self.embedding_models = self.config.provider.embedding_models
        else:
            self.embedding_models = embedding_models or []
        self._configured_vector_dimension = self._configured_embedding_dimension(self.config)
        self._vector_configuration_error = (
            "multiple_embedding_models_not_supported"
            if len(list(dict.fromkeys(self.embedding_models))) > 1
            else ""
        )
        self._embedding_config_fingerprint = self._embedding_runtime_fingerprint(self.config)

        self.data_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai" / "memory"
        os.makedirs(self.data_path, exist_ok=True)
        self.db_path = str(self.data_path / "docs.db")
        self.v2_db_path = str(self.data_path / "memory_v2.db")

        self.faiss_db = None
        self.vec_retriever = None
        self.bm25_retriever = None
        self.retriever = None
        self.instant_gate = None
        self.memory_pipeline = None
        self.session_summarizer = None
        self.memory_observer = None
        self.observability_hub = None
        self.index_projector = None

        self._faiss_lock = asyncio.Lock()
        self._projection_lock = asyncio.Lock()
        self._projection_rebuild_active = False
        self._is_ready = False
        self._vector_state = "uninitialized"
        self._vector_bootstrap_task: asyncio.Task | None = None
        self._vector_bootstrap_delay_task: asyncio.Task | None = None
        self._projection_ready_replay_task: asyncio.Task | None = None
        self._vector_retirement_tasks: set[asyncio.Task] = set()
        self._vector_sync_retirement_executor: ThreadPoolExecutor | None = None
        self._vector_sync_retirement_futures: set[Future] = set()
        self._vector_candidate_executor: ThreadPoolExecutor | None = None
        self._vector_candidate_futures: set[Future] = set()
        self._vector_candidate_build_tasks: set[asyncio.Task] = set()
        self._vector_candidate_paths: set[str] = set()
        self._retired_vector_stacks: dict[str, dict[str, Any]] = {}
        self._vector_retirement_capacity_rejected_total = 0
        self._vector_physical_timeout_exceeded_total = 0
        self._vector_registry_lock = threading.RLock()
        self._vector_close_tasks: dict[int, tuple[Any, Any]] = {}
        self._vector_close_retry_after: dict[int, tuple[Any, float]] = {}
        self._vector_last_error = ""
        self._projection_replay_status = "idle"
        self._projection_replay_error = ""
        self._projection_replay_completed_at = 0.0
        self._vector_bootstrap_started_at = 0.0
        self._vector_bootstrap_completed_at = 0.0
        self._vector_consistency_report: dict[str, Any] = {}
        self._vector_generation = 0
        self._vector_shutdown_generation = 0
        self._accepting_vector_work = True
        self._vector_index_path = ""
        self._vector_index_descriptor: dict[str, Any] = {}
        self._vector_dimension_probe_task: asyncio.Task | None = None
        self._accepting_dimension_probe = True
        self._vector_dimension_probe_cache: dict[str, Any] = {}
        self._vector_dimension_probe_cache_ttl_sec = 30.0
        self._startup_last_yield = time.monotonic()
        self._startup_yield_count = 0
        self._vector_dimension_check_status = "unknown"
        self._vector_dimension_source = "unknown"
        self._vector_query_dimension: int | None = None
        self._configured_vector_dimension = self._configured_embedding_dimension(self.config)
        self._vector_configuration_error = (
            "multiple_embedding_models_not_supported"
            if len(list(dict.fromkeys(self.embedding_models))) > 1
            else ""
        )
        self._vector_dimension_mismatch_total = 0
        self._vector_dimension_probe_failed_total = 0
        self._vector_dimension_probe_timeout_total = 0
        self._vector_dimension_probe_invalid_total = 0
        self._vector_dimension_probe_unavailable_total = 0
        self._vector_dimension_probe_provider_error_total = 0
        self._vector_rebuild_started_total = 0
        self._vector_rebuild_succeeded_total = 0
        self._vector_rebuild_failed_total = 0
        self._vector_old_retriever_blocked_total = 0
        self._init_failures = 0
        self._next_retry_time = 0.0
        self._index_consistency_repaired = False
        self._force_index_rebuild = False
        self._learning_event_history = []
        self._cognitive_feedback_cache: dict[str, list[CognitiveFeedbackSignal]] = {}
        self._disabled_cognitive_feedback_keys: dict[str, float] = {}
        self._last_feedback_cleanup_ts = 0.0
        self.v2_store = MemoryV2Store(self.v2_db_path, data_path=self.data_path, legacy_db_path=self.db_path)
        # Sub-components that depend on self are initialized in initialize()

    @staticmethod
    def _configured_embedding_models(config, fallback: list | None = None) -> list:
        configured = []
        if hasattr(config, "provider") and getattr(config.provider, "embedding_models", None):
            configured = getattr(config.provider, "embedding_models", None) or []
        elif fallback is not None:
            configured = fallback
        return [str(item).strip() for item in configured if str(item).strip()]

    @staticmethod
    def _configured_embedding_dimension(config: Any) -> int | None:
        provider = getattr(config, "provider", None)
        for source in (provider, config):
            for name in ("embedding_dimension", "embedding_dim", "dimension"):
                value = getattr(source, name, None) if source is not None else None
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    return parsed
        return None

    @classmethod
    def _embedding_configuration_fingerprint(cls, config: Any) -> str:
        provider = getattr(config, "provider", None)
        models = tuple(cls._configured_embedding_models(config))
        source = cls._provider_source_id(provider, "")
        base = cls._api_base_fingerprint(provider, config)
        return hashlib.sha256(repr((models, source, base)).encode("utf-8")).hexdigest()[:16]

    def _resolve_embedding_provider(self, model_id: str) -> Any:
        context = getattr(self, "context", None)
        if context is None:
            return None
        for name in ("get_provider_by_id", "get_provider"):
            resolver = getattr(context, name, None)
            if callable(resolver):
                try:
                    provider = resolver(model_id)
                except Exception:
                    provider = None
                if inspect.isawaitable(provider):
                    provider = None
                if provider is not None:
                    return provider
        return None

    def _embedding_runtime_fingerprint(self, config: Any) -> str:
        models = tuple(self._configured_embedding_models(config))
        provider = self._resolve_embedding_provider(models[0]) if models else None
        source = self._provider_source_id(provider, "") if provider is not None else ""
        base = self._api_base_fingerprint(provider, config)
        return hashlib.sha256(repr((models, source, base)).encode("utf-8")).hexdigest()[:16]

    def refresh_config(self, config):
        old_embedding_models = list(self.embedding_models or [])
        old_embedding_fingerprint = getattr(
            self,
            "_embedding_config_fingerprint",
            self._embedding_runtime_fingerprint(self.config),
        )
        self.config = config
        if getattr(self, "injection_service", None) is not None:
            self.injection_service.refresh_config(config)
        if getattr(self, "retrieval_service", None) is not None:
            self.retrieval_service.refresh_config(config)
        if getattr(self, "write_service", None) is not None:
            self.write_service.refresh_config(config)
        if getattr(self, "retriever", None) is not None and hasattr(self.retriever, "refresh_config"):
            self.retriever.refresh_config(config)
        pipeline = getattr(self, "memory_pipeline", None)
        pipeline_refresh = getattr(pipeline, "refresh_config", None)
        if callable(pipeline_refresh):
            pipeline_refresh(config)
        else:
            for attr in ("session_summarizer", "instant_gate"):
                component = getattr(self, attr, None)
                refresh = getattr(component, "refresh_config", None)
                if callable(refresh):
                    refresh(config)
        for attr in ("maintenance_service", "tool_service"):
            component = getattr(self, attr, None)
            refresh = getattr(component, "refresh_config", None)
            if callable(refresh):
                refresh(config)
        self.embedding_models = self._configured_embedding_models(config)
        self._configured_vector_dimension = self._configured_embedding_dimension(config)
        self._vector_configuration_error = (
            "multiple_embedding_models_not_supported"
            if len(list(dict.fromkeys(self.embedding_models))) > 1
            else ""
        )
        self._embedding_config_fingerprint = self._embedding_runtime_fingerprint(config)
        if (
            self.embedding_models != old_embedding_models
            or self._embedding_config_fingerprint != old_embedding_fingerprint
        ):
            self._vector_generation = int(getattr(self, "_vector_generation", 0) or 0) + 1
            bootstrap_task = getattr(self, "_vector_bootstrap_task", None)
            if bootstrap_task is not None and not bootstrap_task.done():
                bootstrap_task.cancel()
            previous_retriever = self.vec_retriever
            previous_faiss_db = self.faiss_db
            self._schedule_vector_stack_retirement(
                previous_retriever,
                previous_faiss_db,
                index_path=self._vector_index_path,
                generation=self._vector_generation - 1,
            )
            self.faiss_db = None
            self.vec_retriever = None
            self.retriever = None
            self._is_ready = False
            self._vector_state = "uninitialized"
            self._vector_last_error = ""
            self._vector_consistency_report = {}
            self._vector_index_path = ""
            self._init_failures = 0
            self._next_retry_time = 0.0
            self._index_consistency_repaired = False
            self._force_index_rebuild = True

    def _reset_vector_index_file(self) -> None:
        index_path = self.data_path / "vectors.index"
        try:
            if index_path.exists():
                index_path.unlink()
        except Exception as exc:
            logger.warning(f"[AstrMai] vector index reset degraded: {exc}")

    @property
    def _vector_manifest_path(self) -> Path:
        return self.data_path / "vector_index_manifest.json"

    @staticmethod
    def _index_dimension(index_or_db: Any) -> tuple[int | None, str]:
        """Read physical Faiss dimension using compatible storage shapes."""
        candidates = [index_or_db]
        storage = getattr(index_or_db, "embedding_storage", None)
        if storage is not None:
            candidates.extend((storage, getattr(storage, "index", None)))
        for candidate in candidates:
            if candidate is None:
                continue
            for name in ("d", "dimension", "dim"):
                try:
                    value = getattr(candidate, name, None)
                    if value is not None:
                        value = int(value)
                        if value > 0:
                            return value, f"{type(candidate).__name__}.{name}"
                except (TypeError, ValueError):
                    continue
        return None, "unknown"

    @classmethod
    def _index_file_dimension(cls, index_path: Path) -> tuple[int | None, str]:
        try:
            import faiss

            index = faiss.read_index(str(index_path))
            return cls._index_dimension(index)
        except Exception:
            return None, "unknown"

    @staticmethod
    def _provider_source_id(provider: Any, fallback: str = "") -> str:
        for candidate in (
            getattr(provider, "id", None),
            getattr(provider, "provider_id", None),
            getattr(getattr(provider, "meta", None), "name", None),
            fallback,
        ):
            value = str(candidate or "").strip()
            if value:
                return value
        return "unknown"

    @staticmethod
    def _normalize_api_base(value: Any) -> str:
        raw = str(value or "").strip().rstrip("/")
        if not raw:
            return ""
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return f"{parsed.scheme.lower()}://{parsed.netloc}{parsed.path.rstrip('/') or ''}"

    @staticmethod
    def _api_base_fingerprint(provider: Any, config: Any = None) -> str:
        raw = ""
        for source in (provider, config):
            for name in ("api_base", "base_url", "endpoint", "url"):
                value = getattr(source, name, None) if source is not None else None
                if value:
                    raw = MemoryEngine._normalize_api_base(value)
                    break
            if raw:
                break
        if not raw:
            return ""
        return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:12]

    async def _probe_embedding_dimension(self, provider: Any, model_id: str) -> dict[str, Any]:
        if not getattr(self, "_accepting_dimension_probe", True):
            return {
                "query_dimension": None,
                "dimension_probe_status": "shutdown_rejected",
                "dimension_probe_error": "vector dimension probe admission is closed",
            }
        source_id = self._provider_source_id(provider, model_id)
        fingerprint = f"{source_id}:{self._api_base_fingerprint(provider, self.config)}"
        cached = self._vector_dimension_probe_cache
        cached_at = float(cached.get("cached_at", 0.0) or 0.0)
        if (
            cached.get("fingerprint") == fingerprint
            and cached.get("dimension_probe_status") == "ok"
            and time.monotonic() - cached_at < self._vector_dimension_probe_cache_ttl_sec
        ):
            return dict(cached)
        task = self._vector_dimension_probe_task
        if task is None or task.done():
            async def _probe() -> dict[str, Any]:
                started = time.monotonic()
                timeout = max(0.1, self._timing_value("embedding_timeout_sec", 30.0))
                try:
                    vector = await invoke_embedding(
                        provider,
                        "dimension probe",
                        timeout_sec=timeout,
                    )
                    return {
                        "query_dimension": len(vector),
                        "dimension_probe_status": "ok",
                        "dimension_probe_error": "",
                        "provider_latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                    }
                except Exception as exc:
                    if isinstance(exc, asyncio.TimeoutError):
                        status = "timeout"
                    elif "method unavailable" in str(exc):
                        status = "unavailable"
                    elif "invalid_embedding" in repr(exc):
                        status = "invalid"
                    else:
                        status = "failed"
                    return {
                        "query_dimension": None,
                        "dimension_probe_status": status,
                        "dimension_probe_error": f"{type(exc).__name__}: {exc!r}"[:240],
                        "provider_latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                    }
            task = asyncio.create_task(_probe(), name="astrmai-embedding-dimension-probe")
            self._vector_dimension_probe_task = task
        result = dict(await asyncio.shield(task))
        result.update({"fingerprint": fingerprint, "provider_source_id": source_id})
        if not getattr(self, "_accepting_dimension_probe", True):
            return {
                **result,
                "query_dimension": None,
                "dimension_probe_status": "shutdown_rejected",
                "dimension_probe_error": "vector dimension probe completed after shutdown",
            }
        if result.get("dimension_probe_status") == "ok":
            result["cached_at"] = time.monotonic()
            self._vector_dimension_probe_cache = result
        else:
            self._vector_dimension_probe_cache = {}
        self._vector_query_dimension = result.get("query_dimension")
        self._vector_dimension_source = "provider_probe" if result.get("query_dimension") else "unknown"
        if result.get("dimension_probe_status") == "failed":
            self._vector_dimension_probe_failed_total += 1
            self._vector_dimension_probe_provider_error_total += 1
        elif result.get("dimension_probe_status") == "timeout":
            self._vector_dimension_probe_timeout_total += 1
        elif result.get("dimension_probe_status") == "invalid":
            self._vector_dimension_probe_invalid_total += 1
        elif result.get("dimension_probe_status") == "unavailable":
            self._vector_dimension_probe_unavailable_total += 1
        return result

    def _load_published_vector_index(
        self,
        embedding_models: list[str],
        expected_dimension: int | None = None,
    ) -> Path | None:
        try:
            payload = json.loads(self._vector_manifest_path.read_text(encoding="utf-8"))
            if list(payload.get("embedding_models") or []) != list(embedding_models):
                return None
            file_name = str(payload.get("file_name") or "").strip()
            if not file_name or Path(file_name).name != file_name:
                return None
            index_path = self.data_path / file_name
            if not index_path.is_file():
                return None
            manifest_dimension = payload.get("dimension")
            actual_dimension, source = self._index_file_dimension(index_path)
            if actual_dimension is None:
                if expected_dimension is not None:
                    self._vector_dimension_check_status = "unknown"
                    return None
                actual_dimension = manifest_dimension if isinstance(manifest_dimension, int) and manifest_dimension > 0 else None
                source = "manifest" if actual_dimension else "unknown"
            if actual_dimension is None:
                self._vector_dimension_check_status = "unknown"
                # Preserve the legacy helper's discovery-only behaviour when
                # no expected provider dimension was supplied.  Bootstrap
                # always supplies the probed dimension (or deliberately skips
                # this path), so an unknown physical dimension is never used
                # for runtime retrieval.
                return index_path if expected_dimension is None else None
            if expected_dimension is not None and int(actual_dimension) != int(expected_dimension):
                self._vector_dimension_mismatch_total += 1
                self._vector_dimension_check_status = "mismatch"
                self._vector_state = "dimension_mismatch_detected"
                return None
            self._vector_dimension_source = source
            self._vector_dimension_check_status = "matched"
            if (
                payload.get("dimension") != actual_dimension
                or not payload.get("provider_source_id")
                or not payload.get("api_base_fingerprint")
                or payload.get("generation") is None
                or not payload.get("status")
            ):
                try:
                    payload["dimension"] = int(actual_dimension)
                    provider = self._resolve_embedding_provider(
                        embedding_models[0] if embedding_models else ""
                    )
                    if not payload.get("provider_source_id"):
                        payload["provider_source_id"] = self._provider_source_id(
                            provider, embedding_models[0] if embedding_models else ""
                        )
                    if not payload.get("api_base_fingerprint"):
                        payload["api_base_fingerprint"] = self._api_base_fingerprint(
                            provider, self.config
                        )
                    payload.setdefault("generation", int(getattr(self, "_vector_generation", 0) or 0))
                    payload.setdefault("document_count", None)
                    payload.setdefault("vector_count", None)
                    payload.setdefault("status", "published")
                    payload.setdefault("created_at", time.time())
                    payload.setdefault("published_at", payload["created_at"])
                    temporary_path = self._vector_manifest_path.with_name(
                        f"{self._vector_manifest_path.name}.{uuid.uuid4().hex}.tmp"
                    )
                    temporary_path.write_text(
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    os.replace(temporary_path, self._vector_manifest_path)
                except Exception as exc:
                    logger.warning(
                        f"[AstrMai] descriptor_backfill_failed: {type(exc).__name__}: {exc!r}"
                    )
            self._vector_index_descriptor = dict(payload)
            return index_path
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._vector_dimension_check_status = "unknown"
            return None

    def _new_vector_index_path(self, generation: int) -> Path:
        return self.data_path / f"vectors.g{generation}.{uuid.uuid4().hex}.index"

    def _publish_vector_index_manifest(
        self,
        index_path: Path,
        embedding_models: list[str],
        *,
        dimension: int | None = None,
        provider_source_id: str = "",
        api_base_fingerprint: str = "",
        document_count: int | None = None,
        vector_count: int | None = None,
        generation: int | None = None,
        status: str = "published",
    ) -> None:
        created_at = time.time()
        payload = VectorIndexDescriptor(
            generation=int(self._vector_generation if generation is None else generation),
            index_file=index_path.name,
            embedding_models=tuple(embedding_models),
            provider_source_id=str(provider_source_id or ""),
            api_base_fingerprint=str(api_base_fingerprint or ""),
            dimension=int(dimension) if dimension is not None else None,
            document_count=document_count,
            vector_count=vector_count,
            created_at=created_at,
            status=status,
        ).to_dict()
        manifest_path = self._vector_manifest_path
        temporary_path = manifest_path.with_name(
            f"{manifest_path.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary_path, manifest_path)
        self._vector_index_descriptor = dict(payload)

    def _cleanup_stale_vector_indexes(
        self,
        current_index_path: Path,
        *,
        keep_history: int = 1,
    ) -> int:
        current_path = Path(current_index_path).resolve()
        with self._vector_registry_lock:
            protected_paths = set(self._vector_candidate_paths)
            retired_paths = [
                str(stack.get("index_path") or "").strip()
                for stack in self._retired_vector_stacks.values()
            ]
        for raw_path in retired_paths:
            if raw_path:
                try:
                    protected_paths.add(str(Path(raw_path).resolve()))
                except OSError:
                    protected_paths.add(raw_path)
        historical: list[tuple[int, Path]] = []
        for candidate in self.data_path.glob("vectors.g*.index"):
            try:
                resolved = candidate.resolve()
                if (
                    candidate.is_file()
                    and resolved != current_path
                    and str(resolved) not in protected_paths
                ):
                    historical.append((candidate.stat().st_mtime_ns, candidate))
            except OSError:
                continue
        historical.sort(key=lambda item: item[0], reverse=True)
        removed = 0
        for _, stale_path in historical[max(0, int(keep_history or 0)):]:
            try:
                stale_path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning(
                    f"[AstrMai] stale vector generation cleanup degraded "
                    f"path={stale_path.name}: {exc}"
                )
        return removed

    def _remember_learning_event(self, event_name: str, payload: dict | None) -> None:
        event_payload = dict(payload or {})
        event_payload["_event"] = event_name
        event_payload["_recorded_at"] = time.time()
        self._learning_event_history.append(event_payload)
        if len(self._learning_event_history) > 100:
            self._learning_event_history = self._learning_event_history[-100:]

    async def on_learning_bot_reply_recorded(self, payload: dict) -> None:
        self._remember_learning_event("learning.bot_reply_recorded", payload)

    async def on_learning_mining_completed(self, payload: dict) -> None:
        self._remember_learning_event("learning.mining_completed", payload)

    def _build_memory_metadata(
        self,
        *,
        session_id: str,
        persona_id: str = None,
        importance: float = 0.8,
        **extra,
    ) -> dict:
        metadata = {
            "session_id": session_id,
            "persona_id": persona_id,
            "importance": importance,
            "create_time": time.time(),
            "last_access_time": time.time(),
        }
        metadata.update(extra)
        return metadata

    async def _run_documents_query(self, query: str, params: tuple = (), *, db_path: str | None = None) -> list:
        target = str(db_path or "").strip()
        if not target:
            raise ValueError("db_path must be explicitly provided for _run_documents_query")
        async with aiosqlite.connect(target) as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchall()

    async def _execute_documents_write(self, query: str, params: tuple = (), *, db_path: str | None = None) -> int:
        target = str(db_path or self.db_path)
        async with aiosqlite.connect(target) as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.rowcount

    async def search_memories(
        self,
        query: str,
        *,
        top_k: int,
        session_id: str = None,
        persona_id: str = None,
        observation: dict | None = None,
    ):
        if self._projection_rebuild_active or self._vector_state == "rebuilding":
            if observation is not None:
                observation.clear()
                observation.update(
                    {
                        "vector": {"status": "rebuilding"},
                        "fallback_source": "canonical_fts",
                        "fused_result_count": 0,
                    }
                )
            return []
        if not await self._ensure_faiss_initialized():
            if observation is not None:
                observation.clear()
                observation.update(
                    {
                        "vector": {"status": str(self._vector_state or "uninitialized")},
                        "fallback_source": "none",
                        "fused_result_count": 0,
                    }
                )
            return []
        return await self.retriever.search(
            query,
            k=top_k,
            session_id=session_id,
            persona_id=persona_id,
            observation=observation,
        )

    # Backward-compatible alias — prefer search_memories() in new code.
    _search_memories = search_memories

    @staticmethod
    def _filter_search_results(results, *, min_score: float) -> list:
        return [item for item in results if getattr(item, "score", 1.0) >= min_score]

    @staticmethod
    def _is_cognitive_feedback_content(content: str) -> bool:
        return str(content or "").lstrip().startswith("[cognitive_feedback:")

    @staticmethod
    def _format_bullet_memories(results) -> str:
        return "\n".join(f"- {item.content}" for item in results)

    async def initialize(self):
        self._startup_last_yield = time.monotonic()
        self._startup_yield_count = 0
        await self.v2_store.initialize()
        await self._startup_checkpoint(force=True)
        self.index_projector = MemoryIndexProjector(self)
        self.v2_store.index_projector = self.index_projector
        self.write_service = MemoryWriteService(self.v2_store, self.index_projector, self.config)
        self.retrieval_service = MemoryRetrievalService(self.v2_store, engine=self)
        self.expression_pattern_service = ExpressionPatternService(self.v2_store, self.write_service)
        self.injection_service = MemoryInjectionService(self.retrieval_service, config=self.config)
        self.tool_service = MemoryToolService(self.retrieval_service, config=self.config)
        self.maintenance_service = MemoryMaintenanceService(self.v2_store, self.index_projector, config=self.config)
        self.migration_service = MemoryMigrationService(
            self.v2_store,
            index_projector=self.index_projector,
            engine=self,
        )
        await self.v2_store.import_legacy_documents()
        await self._startup_checkpoint(force=True)
        await self.v2_store.import_persona_cache()
        await self._startup_checkpoint(force=True)
        await self.import_legacy_memory_events()
        await self._startup_checkpoint(force=True)
        await self.import_legacy_jargons()
        await self._startup_checkpoint(force=True)
        await self.import_legacy_expression_patterns()
        await self._startup_checkpoint(force=True)
        await self._migrate_learning_scope_v3()
        await self._startup_checkpoint(force=True)
        await self.migrate_legacy_cognitive_feedback()
        await self._startup_checkpoint(force=True)
        await self._cleanup_cognitive_feedback_records(force=True)
        await self._startup_checkpoint(force=True)
        self.bm25_retriever = BM25Retriever(self.db_path)
        await self.bm25_retriever.initialize()
        logger.info("[AstrMai] memory skeleton initialized; vector store will be lazy-loaded.")

    async def _startup_checkpoint(self, *, force: bool = False) -> None:
        """Yield between startup batches so the shared event loop stays responsive."""
        now = time.monotonic()
        last_yield = float(getattr(self, "_startup_last_yield", now) or now)
        if force or (now - last_yield) * 1000.0 >= self.STARTUP_CPU_SLICE_MS:
            self._startup_last_yield = now
            self._startup_yield_count = int(getattr(self, "_startup_yield_count", 0) or 0) + 1
            await asyncio.sleep(self.STARTUP_YIELD_SEC)

    @staticmethod
    def _merge_learning_metadata(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
        merged = {**secondary, **primary}
        for key, limit in (("examples", 12), ("aliases", 12), ("content_samples", 8), ("source_groups", 64)):
            values = [
                str(item).strip()
                for item in [*(secondary.get(key) or []), *(primary.get(key) or [])]
                if str(item or "").strip()
            ]
            merged[key] = list(dict.fromkeys(values))[:limit]
        try:
            merged["count"] = int(primary.get("count") or 0) + int(secondary.get("count") or 0)
        except (TypeError, ValueError):
            merged["count"] = 1
        try:
            merged["weight"] = max(float(primary.get("weight") or 0.0), float(secondary.get("weight") or 0.0))
        except (TypeError, ValueError):
            merged["weight"] = 1.0
        for key in ("speaker_id", "speaker_name", "evidence_message_ids"):
            merged.pop(key, None)
        return merged

    @staticmethod
    def _merge_learning_status(*statuses: str) -> tuple[str, str, str]:
        normalized = {str(item or "").strip().lower() for item in statuses}
        if "active" in normalized:
            return "active", "approved", "auto_and_tool"
        if "rejected" in normalized:
            return "rejected", "rejected", "maintenance_only"
        if "review_pending" in normalized:
            return "review_pending", "review_pending", "maintenance_only"
        if "stale" in normalized:
            return "stale", "stale", "maintenance_only"
        return "rejected", "rejected", "maintenance_only"

    async def _migrate_learning_scope_v3(self) -> None:
        version = "3_learning_scope_unification"
        if await self.v2_store.migration_applied(version):
            return
        changed_ids: set[str] = set()
        removed_ids: list[str] = []
        processed = 0
        try:
            jargons = await self.v2_store.list_candidates(
                kinds=["jargon"],
                statuses=["active", "review_pending", "rejected", "stale"],
                limit=10000,
                include_inactive=True,
            )
            for item in jargons:
                processed += 1
                target_key = jargon_fingerprint(item.content)
                metadata = dict(item.metadata or {})
                metadata["source_groups"] = list(
                    dict.fromkeys(
                        [
                            *[str(value) for value in (metadata.get("source_groups") or []) if str(value)],
                            *([str(item.session_id)] if str(item.session_id or "") not in {"", GLOBAL_JARGON_SESSION_ID} else []),
                        ]
                    )
                )[-64:]
                target = await self.v2_store.get_by_dedup_key(target_key, include_inactive=True)
                if target and str(target.id) != str(item.id):
                    target_metadata = self._merge_learning_metadata(dict(target.metadata or {}), metadata)
                    status, review_status, visibility = self._merge_learning_status(target.status, item.status)
                    target_metadata["review_status"] = review_status
                    await self.v2_store.update_memory(
                        str(target.id),
                        metadata=target_metadata,
                        status=status,
                        visibility=visibility,
                        session_id=GLOBAL_JARGON_SESSION_ID,
                    )
                    await self.v2_store.hard_delete(str(item.id), kind="jargon")
                    changed_ids.add(str(target.id))
                    removed_ids.append(str(item.id))
                    continue
                await self.v2_store.update_memory(
                    str(item.id),
                    session_id=GLOBAL_JARGON_SESSION_ID,
                    dedup_key=target_key,
                    metadata=metadata,
                )
                changed_ids.add(str(item.id))
                if processed % self.STARTUP_BATCH_SIZE == 0:
                    await self._startup_checkpoint(force=True)

            patterns = await self.v2_store.list_candidates(
                kinds=["expression_pattern"],
                statuses=["active", "review_pending", "rejected", "stale"],
                limit=10000,
                include_inactive=True,
            )
            for item in patterns:
                processed += 1
                metadata = dict(item.metadata or {})
                group_id = str(item.session_id or metadata.get("group_id") or "")
                situation = str(metadata.get("situation") or "日常回应")
                habit_type = str(metadata.get("habit_type") or "sentence_pattern")
                target_key = expression_fingerprint(group_id, habit_type, item.content, situation)
                metadata["shared_scope"] = group_id
                metadata["scope_kind"] = "group"
                for key in ("speaker_id", "speaker_name", "evidence_message_ids"):
                    metadata.pop(key, None)
                target = await self.v2_store.get_by_dedup_key(target_key, include_inactive=True)
                if target and str(target.id) != str(item.id):
                    target_metadata = self._merge_learning_metadata(dict(target.metadata or {}), metadata)
                    status, review_status, visibility = self._merge_learning_status(target.status, item.status)
                    target_metadata["review_status"] = review_status
                    await self.v2_store.update_memory(
                        str(target.id),
                        metadata=target_metadata,
                        status=status,
                        visibility=visibility,
                        session_id=group_id,
                    )
                    await self.v2_store.hard_delete(str(item.id), kind="expression_pattern")
                    changed_ids.add(str(target.id))
                    removed_ids.append(str(item.id))
                    continue
                await self.v2_store.update_memory(
                    str(item.id),
                    session_id=group_id,
                    dedup_key=target_key,
                    metadata=metadata,
                )
                changed_ids.add(str(item.id))
                if processed % self.STARTUP_BATCH_SIZE == 0:
                    await self._startup_checkpoint(force=True)

            if self.index_projector:
                for memory_id in changed_ids:
                    candidate = await self.v2_store.get_canonical(memory_id, include_inactive=True)
                    if candidate and candidate.status == "active":
                        await self.index_projector.project(memory_id)
                if removed_ids:
                    await self.index_projector.cleanup_deleted(removed_ids)
            await self.v2_store.record_migration(
                version,
                status="applied",
                detail=f"changed={len(changed_ids)},removed={len(removed_ids)}",
            )
        except Exception as exc:
            await self.v2_store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] learning scope migration degraded: {exc}")

    def _timing_value(self, name: str, default: float) -> float:
        timing = getattr(self.config, "timing", None)
        try:
            return float(getattr(timing, name, default) or default)
        except (TypeError, ValueError):
            return float(default)

    def _schedule_vector_bootstrap(self) -> None:
        if not getattr(self, "_accepting_vector_work", True):
            return
        task = self._vector_bootstrap_task
        if task is not None and not task.done():
            return
        if time.time() < self._next_retry_time:
            return
        try:
            task = asyncio.create_task(
                self._run_vector_bootstrap_budgeted(),
                name="astrmai-vector-bootstrap",
            )
        except RuntimeError:
            return
        self._vector_bootstrap_task = task

        def _consume(completed: asyncio.Task) -> None:
            if self._vector_bootstrap_task is completed:
                self._vector_bootstrap_task = None
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(_consume)

    async def _run_vector_bootstrap_budgeted(self) -> None:
        budget = getattr(self, "background_task_budget", None)
        timeout_sec = max(1.0, self._timing_value("faiss_bootstrap_timeout_sec", 1800.0))
        try:
            if budget is not None:
                await budget.run(
                    self._bootstrap_vector_index,
                    task_name="memory.vector_bootstrap",
                    execution_timeout_sec=timeout_sec,
                    defer_release_on_timeout=True,
                )
            else:
                async with asyncio.timeout(timeout_sec):
                    await self._bootstrap_vector_index()
        except asyncio.CancelledError:
            if self._vector_state != "ready":
                self._vector_state = "uninitialized"
            raise
        except Exception as exc:
            self._mark_vector_bootstrap_failed(exc, include_trace=True)

    def _mark_vector_bootstrap_failed(
        self,
        exc: Exception,
        *,
        include_trace: bool = False,
    ) -> None:
        self._init_failures += 1
        backoff = min(3600, 30 * (2 ** (self._init_failures - 1)))
        self._next_retry_time = time.time() + backoff
        self._is_ready = False
        self._vector_state = "degraded"
        self._vector_last_error = f"{type(exc).__name__}: {exc}"[:500]
        self._vector_bootstrap_completed_at = time.time()
        logger.error(
            f"[AstrMai] vector bootstrap failed: {exc}; retry in {backoff}s.",
            exc_info=include_trace,
        )

    @staticmethod
    async def _close_vector_stack(retriever, faiss_db, *, timeout_sec: float | None = None) -> bool:
        def _invoke_close(close_fn):
            try:
                signature = inspect.signature(close_fn)
            except (TypeError, ValueError):
                return close_fn()
            try:
                signature.bind(timeout_sec=timeout_sec)
            except TypeError:
                return close_fn()
            return close_fn(timeout_sec=timeout_sec)

        async def _invoke_close_bounded(close_fn):
            is_async = inspect.iscoroutinefunction(close_fn)
            if is_async:
                result = _invoke_close(close_fn)
            else:
                call = asyncio.to_thread(_invoke_close, close_fn)
                result = await (asyncio.wait_for(call, timeout=timeout_sec) if timeout_sec is not None else call)
            if inspect.isawaitable(result):
                result = await (asyncio.wait_for(result, timeout=timeout_sec) if timeout_sec is not None else result)
            return result

        success = True
        if retriever is not None:
            try:
                close_retriever = getattr(retriever, "close", None)
                if callable(close_retriever):
                    result = await _invoke_close_bounded(close_retriever)
                    if result is False:
                        success = False
            except Exception as exc:
                success = False
                logger.warning(f"[AstrMai] vector retriever close degraded: {exc}")
            if not success:
                return False
        if faiss_db is not None:
            try:
                close_database = getattr(faiss_db, "close", None)
                if callable(close_database):
                    result = await _invoke_close_bounded(close_database)
                    if result is False:
                        success = False
            except Exception as exc:
                success = False
                logger.warning(f"[AstrMai] vector database close degraded: {exc}")
        return success

    @staticmethod
    def _close_vector_stack_physical(retriever, faiss_db, *, timeout_sec: float) -> bool:
        def _invoke(resource) -> bool:
            if resource is None:
                return True
            close_fn = getattr(resource, "close", None)
            if not callable(close_fn):
                return True
            try:
                kwargs: dict[str, Any] = {}
                try:
                    signature = inspect.signature(close_fn)
                except (TypeError, ValueError):
                    pass
                else:
                    try:
                        signature.bind(timeout_sec=timeout_sec)
                    except TypeError:
                        pass
                    else:
                        kwargs["timeout_sec"] = timeout_sec
                result = close_fn(**kwargs)
                if inspect.isawaitable(result):
                    result = asyncio.run(
                        asyncio.wait_for(result, timeout=max(0.05, timeout_sec))
                    )
                return result is not False
            except Exception as exc:
                logger.warning(f"[AstrMai] physical vector close degraded: {exc}")
                return False

        if not _invoke(retriever):
            return False
        return _invoke(faiss_db)

    def _vector_close_timeout_sec(self) -> float:
        return max(0.05, self._timing_value("shutdown_cancel_grace_sec", 1.0))

    def _consume_vector_close_task(self, resource, task: asyncio.Task) -> None:
        try:
            failed = task.exception() is not None or task.result() is False
        except (asyncio.CancelledError, Exception):
            failed = True
        if failed:
            with self._vector_registry_lock:
                self._vector_close_retry_after[id(resource)] = (
                    resource,
                    time.monotonic() + 0.25,
                )

    async def _await_vector_resource_close(self, resource, *, timeout_sec: float | None) -> bool:
        if resource is None:
            return True
        started_at = time.monotonic()
        key = id(resource)
        with self._vector_registry_lock:
            entry = self._vector_close_tasks.get(key)
            task = entry[1] if entry is not None and entry[0] is resource else None
        if task is not None and task.done():
            try:
                if bool(task.result()):
                    return True
            except (asyncio.CancelledError, Exception):
                pass
            with self._vector_registry_lock:
                current = self._vector_close_tasks.get(key)
                if current is not None and current[0] is resource and current[1] is task:
                    self._vector_close_tasks.pop(key, None)
            task = None
        joined_existing = task is not None
        if task is None:
            with self._vector_registry_lock:
                entry = self._vector_close_tasks.get(key)
                task = entry[1] if entry is not None and entry[0] is resource else None
                if task is None:
                    retry_entry = self._vector_close_retry_after.get(key)
                    if retry_entry is not None and retry_entry[0] is resource:
                        if time.monotonic() < retry_entry[1]:
                            return False
                        self._vector_close_retry_after.pop(key, None)
                    task = asyncio.create_task(
                        self._close_vector_stack(resource, None),
                        name="astrmai-vector-resource-close",
                    )
                    self._vector_close_tasks[key] = (resource, task)
                    task.add_done_callback(
                        lambda completed, owner=resource: self._consume_vector_close_task(owner, completed)
                    )
        async def _await_owner(owner):
            if isinstance(owner, asyncio.Future):
                return await asyncio.shield(owner)
            return await asyncio.shield(asyncio.wrap_future(owner))

        try:
            if timeout_sec is None:
                closed = await _await_owner(task)
            else:
                closed = await asyncio.wait_for(_await_owner(task), timeout=max(0.0, timeout_sec))
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            with self._vector_registry_lock:
                current = self._vector_close_tasks.get(key)
                if task.done() and current is not None and current[0] is resource and current[1] is task:
                    self._vector_close_tasks.pop(key, None)
            return False
        if not closed:
            with self._vector_registry_lock:
                current = self._vector_close_tasks.get(key)
                if current is not None and current[0] is resource and current[1] is task:
                    self._vector_close_tasks.pop(key, None)
        else:
            with self._vector_registry_lock:
                retry_entry = self._vector_close_retry_after.get(key)
                if retry_entry is not None and retry_entry[0] is resource:
                    self._vector_close_retry_after.pop(key, None)
        if not closed and joined_existing:
            remaining = None
            if timeout_sec is not None:
                remaining = max(0.0, timeout_sec - (time.monotonic() - started_at))
                if remaining <= 0.0:
                    return False
            return await self._await_vector_resource_close(resource, timeout_sec=remaining)
        return bool(closed)

    async def _await_vector_stack_close(
        self,
        retriever,
        faiss_db,
        *,
        timeout_sec: float | None,
    ) -> bool:
        deadline = None if timeout_sec is None else time.monotonic() + max(0.0, timeout_sec)

        def _remaining() -> float | None:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        if not await self._await_vector_resource_close(retriever, timeout_sec=_remaining()):
            return False
        return await self._await_vector_resource_close(faiss_db, timeout_sec=_remaining())

    def _forget_vector_stack_close(self, retriever, faiss_db) -> None:
        with self._vector_registry_lock:
            for resource in (retriever, faiss_db):
                if resource is None:
                    continue
                key = id(resource)
                entry = self._vector_close_tasks.get(key)
                if entry is not None and entry[0] is resource and entry[1].done():
                    self._vector_close_tasks.pop(key, None)
                retry_entry = self._vector_close_retry_after.get(key)
                if retry_entry is not None and retry_entry[0] is resource:
                    self._vector_close_retry_after.pop(key, None)

    async def _construct_vector_candidate(
        self,
        *,
        index_path: Path,
        embedding_provider,
    ):
        candidate_path = str(Path(index_path).resolve())
        with self._vector_registry_lock:
            self._vector_candidate_paths.add(candidate_path)
        cleanup_state = {"abandoned": False, "scheduled": False}
        with self._vector_registry_lock:
            executor = self._vector_candidate_executor
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="astrmai-vector-candidate",
                )
                self._vector_candidate_executor = executor
        physical_future = executor.submit(
            FaissVecDB,
            doc_store_path=str(self.data_path / "docs.db"),
            index_store_path=str(index_path),
            embedding_provider=embedding_provider,
        )
        with self._vector_registry_lock:
            self._vector_candidate_futures.add(physical_future)

        async def _wait_for_candidate():
            return await asyncio.wrap_future(physical_future)

        task = asyncio.create_task(_wait_for_candidate(), name="astrmai-vector-candidate-build")
        with self._vector_registry_lock:
            self._vector_candidate_build_tasks.add(task)

        def _finish_candidate_build(done: Future) -> None:
            with self._vector_registry_lock:
                self._vector_candidate_futures.discard(done)
            try:
                if done.cancelled():
                    with self._vector_registry_lock:
                        self._vector_candidate_paths.discard(candidate_path)
                    return
                try:
                    candidate_db = done.result()
                except Exception as exc:
                    logger.warning(f"[AstrMai] vector candidate construction failed: {exc}")
                    with self._vector_registry_lock:
                        self._vector_candidate_paths.discard(candidate_path)
                    return
                with self._vector_registry_lock:
                    if not cleanup_state["abandoned"] or cleanup_state["scheduled"]:
                        return
                    cleanup_state["scheduled"] = True
                try:
                    self._schedule_vector_stack_retirement(
                        None,
                        candidate_db,
                        index_path=index_path,
                        generation=self._vector_generation,
                        delete_index=True,
                    )
                finally:
                    with self._vector_registry_lock:
                        self._vector_candidate_paths.discard(candidate_path)
            finally:
                with self._vector_registry_lock:
                    active_executor = self._vector_candidate_executor
                    shutdown_executor = (
                        active_executor is executor and not self._vector_candidate_futures
                    )
                    if shutdown_executor:
                        self._vector_candidate_executor = None
                if shutdown_executor:
                    active_executor.shutdown(wait=False, cancel_futures=True)

        def _finish_candidate_waiter(done: asyncio.Task) -> None:
            with self._vector_registry_lock:
                self._vector_candidate_build_tasks.discard(done)
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        physical_future.add_done_callback(_finish_candidate_build)
        task.add_done_callback(_finish_candidate_waiter)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with self._vector_registry_lock:
                cleanup_state["abandoned"] = True
            if physical_future.done():
                _finish_candidate_build(physical_future)
            raise
        except Exception:
            with self._vector_registry_lock:
                self._vector_candidate_paths.discard(candidate_path)
            raise

    def _schedule_vector_stack_retirement(
        self,
        retriever,
        faiss_db,
        *,
        index_path=None,
        generation=None,
        delete_index: bool = False,
    ) -> bool:
        if retriever is None and faiss_db is None:
            return True
        registry_limit = max(
            1,
            int(self._timing_value("vector_retirement_registry_limit", 32.0)),
        )
        stack_id = uuid.uuid4().hex
        with self._vector_registry_lock:
            if len(self._retired_vector_stacks) >= registry_limit:
                self._vector_retirement_capacity_rejected_total += 1
                logger.error(
                    "[AstrMai] vector retirement registry hard limit reached; "
                    f"rejecting stack limit={registry_limit}"
                )
                reject = True
            else:
                reject = False
            if reject:
                pass
            else:
                self._retired_vector_stacks[stack_id] = {
                    "retriever": retriever,
                    "faiss_db": faiss_db,
                    "index_path": str(index_path) if index_path else "",
                    "generation": generation,
                    "delete_index": bool(delete_index),
                    "attempts": 0,
                    "close_attempt_total": 0,
                    "shutdown_retry_attempts": 0,
                    "physical_timeout_exceeded": False,
                    "max_attempts": max(
                        1,
                        int(self._timing_value("vector_retirement_retry_max_attempts", 5.0)),
                    ),
                    "next_retry_at": 0.0,
                    "status": "pending",
                    "last_error": "",
                    "task": None,
                    "sync_future": None,
                }
            registry_size = len(self._retired_vector_stacks)
        if reject:
            self._schedule_rejected_vector_cleanup(
                retriever,
                faiss_db,
                index_path=index_path,
                delete_index=delete_index,
            )
            return False
        self._schedule_vector_retirement_attempt(stack_id)
        return True

    def _schedule_rejected_vector_cleanup(
        self,
        retriever,
        faiss_db,
        *,
        index_path=None,
        delete_index: bool = False,
    ) -> None:
        """Close an overflow stack without retaining it in the retired registry."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and not loop.is_closed():
            async def _close() -> bool:
                closed = await self._await_vector_stack_close(
                    retriever,
                    faiss_db,
                    timeout_sec=self._vector_close_timeout_sec(),
                )
                if closed and delete_index and index_path:
                    try:
                        Path(index_path).unlink(missing_ok=True)
                    except OSError:
                        pass
                return closed

            task = loop.create_task(_close(), name="astrmai-vector-retirement-overflow")
            with self._vector_registry_lock:
                self._vector_retirement_tasks.add(task)

            def _consume(done: asyncio.Task) -> None:
                with self._vector_registry_lock:
                    self._vector_retirement_tasks.discard(done)
                try:
                    if not done.result():
                        logger.error("[AstrMai] rejected vector retirement cleanup failed")
                except Exception as exc:
                    logger.error(f"[AstrMai] rejected vector retirement cleanup degraded: {exc}")

            task.add_done_callback(_consume)
            return
        with self._vector_registry_lock:
            executor = self._vector_sync_retirement_executor
            if executor is None:
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="astrmai-vector-retire")
                self._vector_sync_retirement_executor = executor
            future = executor.submit(
                self._close_vector_stack_physical,
                retriever,
                faiss_db,
                timeout_sec=self._vector_close_timeout_sec(),
            )
            self._vector_sync_retirement_futures.add(future)

        def _consume_sync(done: Future) -> None:
            with self._vector_registry_lock:
                self._vector_sync_retirement_futures.discard(done)
            try:
                if done.result() and delete_index and index_path:
                    Path(index_path).unlink(missing_ok=True)
            except Exception as exc:
                logger.error(f"[AstrMai] rejected vector retirement cleanup degraded: {exc}")

        future.add_done_callback(_consume_sync)

    def _retirement_retry_delay(self, attempts: int) -> float:
        base = max(0.25, self._timing_value("vector_retirement_retry_base_sec", 0.25))
        maximum = max(base, self._timing_value("vector_retirement_retry_max_sec", 5.0))
        return min(maximum, base * (2 ** max(0, int(attempts) - 1)))

    def _vector_stack_close_retry_after(self, stack: dict[str, Any]) -> float:
        retry_after = 0.0
        with self._vector_registry_lock:
            for resource in (stack.get("retriever"), stack.get("faiss_db")):
                if resource is None:
                    continue
                entry = self._vector_close_retry_after.get(id(resource))
                if entry is not None and entry[0] is resource:
                    retry_after = max(retry_after, float(entry[1] or 0.0))
        return retry_after

    def _finalize_retired_vector_stack(self, stack_id: str) -> None:
        with self._vector_registry_lock:
            stack = self._retired_vector_stacks.pop(stack_id, None) or {}
        self._forget_vector_stack_close(stack.get("retriever"), stack.get("faiss_db"))
        retired_path = str(stack.get("index_path") or "").strip()
        if retired_path:
            try:
                with self._vector_registry_lock:
                    self._vector_candidate_paths.discard(str(Path(retired_path).resolve()))
            except OSError:
                with self._vector_registry_lock:
                    self._vector_candidate_paths.discard(retired_path)
        if stack.get("delete_index") and retired_path:
            try:
                Path(retired_path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"[AstrMai] retired vector index cleanup degraded: {exc}")
        self._schedule_deferred_vector_retirements()

    def _schedule_deferred_vector_retirements(self) -> None:
        registry_limit = max(
            1,
            int(self._timing_value("vector_retirement_registry_limit", 32.0)),
        )
        scheduled: list[str] = []
        with self._vector_registry_lock:
            active = sum(
                1
                for stack in self._retired_vector_stacks.values()
                if stack.get("task") is not None or stack.get("sync_future") is not None
            )
            for stack_id, stack in list(self._retired_vector_stacks.items()):
                if active >= registry_limit:
                    break
                if stack.get("status") != "capacity_deferred":
                    continue
                stack["status"] = "pending"
                stack["last_error"] = ""
                scheduled.append(stack_id)
                active += 1
        for stack_id in scheduled:
            self._schedule_vector_retirement_attempt(stack_id)

    def _schedule_vector_retirement_attempt(self, stack_id: str) -> None:
        with self._vector_registry_lock:
            stack = self._retired_vector_stacks.get(stack_id)
            if not stack or stack.get("task") is not None or stack.get("sync_future") is not None:
                return
            if int(stack.get("attempts", 0) or 0) >= int(stack.get("max_attempts", 1) or 1):
                stack["status"] = "retry_exhausted"
                return
            delay = max(
                0.0,
                float(stack.get("next_retry_at", 0.0) or 0.0) - time.monotonic(),
            )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._schedule_sync_vector_retirement(stack_id)
            return
        if loop.is_closed():
            self._schedule_sync_vector_retirement(stack_id)
            return

        async def _retire() -> bool:
            if delay:
                await asyncio.sleep(delay)
            with self._vector_registry_lock:
                current = self._retired_vector_stacks.get(stack_id)
                if current is not stack:
                    return False
                stack["attempts"] = int(stack.get("attempts", 0) or 0) + 1
                stack["close_attempt_total"] = int(stack.get("close_attempt_total", 0) or 0) + 1
                stack["status"] = "closing"
            return await self._await_vector_stack_close(
                stack.get("retriever"),
                stack.get("faiss_db"),
                timeout_sec=self._vector_close_timeout_sec(),
            )

        retirement_coro = _retire()
        try:
            task = loop.create_task(retirement_coro, name="astrmai-vector-stack-retirement")
        except RuntimeError:
            retirement_coro.close()
            self._schedule_sync_vector_retirement(stack_id)
            return
        with self._vector_registry_lock:
            current = self._retired_vector_stacks.get(stack_id)
            if current is not stack or stack.get("task") is not None or stack.get("sync_future") is not None:
                task.cancel()
                return
            stack["task"] = task
            self._vector_retirement_tasks.add(task)

        def _consume(completed: asyncio.Task) -> None:
            with self._vector_registry_lock:
                self._vector_retirement_tasks.discard(completed)
                current = self._retired_vector_stacks.get(stack_id)
                if current is not stack or stack.get("task") is not completed:
                    return
                stack["task"] = None
                if completed.cancelled():
                    stack["status"] = "cancelled"
                    return
            try:
                closed = bool(completed.result())
            except Exception as exc:
                closed = False
                with self._vector_registry_lock:
                    stack["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
            if closed:
                self._finalize_retired_vector_stack(stack_id)
                return
            with self._vector_registry_lock:
                stack["status"] = "retry_wait"
                stack["last_error"] = stack.get("last_error") or "close_failed"
                stack["next_retry_at"] = max(
                    time.monotonic() + self._retirement_retry_delay(
                        int(stack.get("attempts", 0) or 0)
                    ),
                    self._vector_stack_close_retry_after(stack) + 0.001,
                )
                should_retry = (
                    getattr(self, "_accepting_vector_work", True)
                    and int(stack.get("attempts", 0) or 0)
                    < int(stack.get("max_attempts", 1) or 1)
                )
                if not should_retry:
                    stack["status"] = "retry_exhausted"
            if should_retry:
                self._schedule_vector_retirement_attempt(stack_id)
            else:
                logger.warning(f"[AstrMai] vector stack retirement pending stack_id={stack_id}")

        task.add_done_callback(_consume)

    def _schedule_sync_vector_retirement(self, stack_id: str) -> None:
        with self._vector_registry_lock:
            stack = self._retired_vector_stacks.get(stack_id)
            if not stack or stack.get("sync_future") is not None:
                return
            resources = [
                resource
                for resource in (stack.get("retriever"), stack.get("faiss_db"))
                if resource is not None
            ]
            existing_owners: dict[int, Any] = {}
            owned_resources: list[Any] = []
            future: Future = Future()
            for resource in resources:
                entry = self._vector_close_tasks.get(id(resource))
                if entry is not None and entry[0] is resource and not entry[1].done():
                    existing_owners[id(resource)] = entry[1]
                else:
                    self._vector_close_tasks[id(resource)] = (resource, future)
                    owned_resources.append(resource)
            stack["sync_future"] = future
            self._vector_sync_retirement_futures.add(future)

        def _run() -> None:
            try:
                owner_deadline = time.monotonic() + self._vector_close_timeout_sec()
                for owner in existing_owners.values():
                    while not owner.done() and time.monotonic() < owner_deadline:
                        time.sleep(0.01)
                    if not owner.done():
                        with self._vector_registry_lock:
                            stack["physical_timeout_exceeded"] = True
                            self._vector_physical_timeout_exceeded_total += 1
                        future.set_result(False)
                        return
                    try:
                        if owner.result() is False:
                            future.set_result(False)
                            return
                    except (asyncio.CancelledError, Exception):
                        future.set_result(False)
                        return

                while True:
                    with self._vector_registry_lock:
                        if int(stack.get("attempts", 0) or 0) >= int(
                            stack.get("max_attempts", 1) or 1
                        ):
                            future.set_result(False)
                            return
                        stack["attempts"] = int(stack.get("attempts", 0) or 0) + 1
                        stack["close_attempt_total"] = int(
                            stack.get("close_attempt_total", 0) or 0
                        ) + 1
                        stack["status"] = "closing"
                    retriever = (
                        stack.get("retriever")
                        if any(resource is stack.get("retriever") for resource in owned_resources)
                        else None
                    )
                    faiss_db = (
                        stack.get("faiss_db")
                        if any(resource is stack.get("faiss_db") for resource in owned_resources)
                        else None
                    )
                    close_started_at = time.monotonic()
                    closed = self._close_vector_stack_physical(
                        retriever,
                        faiss_db,
                        timeout_sec=self._vector_close_timeout_sec(),
                    )
                    elapsed = time.monotonic() - close_started_at
                    with self._vector_registry_lock:
                        if elapsed > self._vector_close_timeout_sec():
                            stack["physical_timeout_exceeded"] = True
                            self._vector_physical_timeout_exceeded_total += 1
                    if closed:
                        future.set_result(True)
                        return
                    with self._vector_registry_lock:
                        stack["status"] = "retry_wait"
                        stack["last_error"] = "close_failed"
                        delay = self._retirement_retry_delay(
                            int(stack.get("attempts", 0) or 0)
                        )
                        stack["next_retry_at"] = time.monotonic() + delay
                    time.sleep(delay)
            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)

        thread = threading.Thread(
            target=_run,
            name="astrmai-vector-retire-physical",
            daemon=True,
        )
        thread.start()

        def _consume(completed: Future) -> None:
            try:
                with self._vector_registry_lock:
                    self._vector_sync_retirement_futures.discard(completed)
                    current = self._retired_vector_stacks.get(stack_id)
                    if current is not stack or stack.get("sync_future") is not completed:
                        return
                    stack["sync_future"] = None
                try:
                    closed = bool(completed.result())
                except Exception as exc:
                    closed = False
                    with self._vector_registry_lock:
                        stack["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
                if closed:
                    self._finalize_retired_vector_stack(stack_id)
                else:
                    with self._vector_registry_lock:
                        stack["status"] = "retry_exhausted"
                    logger.warning(f"[AstrMai] synchronous vector retirement exhausted stack_id={stack_id}")
            finally:
                pass

        future.add_done_callback(_consume)

    async def _close_retired_vector_stacks(self, *, timeout_sec: float | None = None) -> bool:
        all_closed = True
        with self._vector_registry_lock:
            stacks = list(self._retired_vector_stacks.items())
        for stack_id, stack in stacks:
            if stack.get("status") == "retry_exhausted":
                all_closed = False
                continue
            closed = await self._await_vector_stack_close(
                stack.get("retriever"),
                stack.get("faiss_db"),
                timeout_sec=timeout_sec,
            )
            if closed:
                self._finalize_retired_vector_stack(stack_id)
            else:
                all_closed = False
        return all_closed

    async def _bootstrap_vector_index(self) -> None:
        lifecycle = SimpleNamespace(
            retriever=None,
            faiss_db=None,
            index_path=None,
            discard_index=False,
            published=False,
            shutdown_generation=int(getattr(self, "_vector_shutdown_generation", 0) or 0),
        )
        try:
            await self._build_and_publish_vector_index(lifecycle)
        finally:
            self._projection_rebuild_active = False
            if not lifecycle.published and lifecycle.index_path is not None and lifecycle.discard_index:
                self._vector_rebuild_failed_total += 1
            if not lifecycle.published:
                closed = await self._await_vector_stack_close(
                    lifecycle.retriever,
                    lifecycle.faiss_db,
                    timeout_sec=self._vector_close_timeout_sec(),
                )
                if closed:
                    self._forget_vector_stack_close(lifecycle.retriever, lifecycle.faiss_db)
                if not closed:
                    self._schedule_vector_stack_retirement(
                        lifecycle.retriever,
                        lifecycle.faiss_db,
                        index_path=lifecycle.index_path,
                        generation=self._vector_generation,
                        delete_index=bool(lifecycle.discard_index),
                    )
                if closed and lifecycle.discard_index and lifecycle.index_path is not None:
                    try:
                        lifecycle.index_path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning(f"[AstrMai] stale vector candidate cleanup degraded: {exc}")
            if lifecycle.index_path is not None:
                try:
                    with self._vector_registry_lock:
                        self._vector_candidate_paths.discard(
                            str(Path(lifecycle.index_path).resolve())
                        )
                except OSError:
                    with self._vector_registry_lock:
                        self._vector_candidate_paths.discard(str(lifecycle.index_path))

    async def _build_and_publish_vector_index(self, lifecycle) -> None:
        generation = self._vector_generation
        self._vector_state = "initializing"
        self._vector_last_error = ""
        self._vector_bootstrap_started_at = time.time()
        if not HAS_FAISS:
            raise RuntimeError("faiss is unavailable in current environment")

        provider_instance = None
        clean_models = [m.strip() for m in self.embedding_models if m and m.strip()]
        unique_models = list(dict.fromkeys(clean_models))
        if len(unique_models) > 1:
            self._vector_configuration_error = "multiple_embedding_models_not_supported"
            self._vector_dimension_check_status = "configuration_error"
            self._vector_state = "degraded"
            raise RuntimeError(self._vector_configuration_error)
        for model_id in unique_models:
            provider_instance = self._resolve_embedding_provider(model_id)
            if provider_instance:
                break
        if provider_instance is None and not unique_models:
            providers = getattr(self.context, "get_all_embedding_providers", None)
            if callable(providers):
                try:
                    available = providers() or []
                    provider_instance = available[0] if available else None
                    if provider_instance is not None:
                        unique_models = [self._provider_source_id(provider_instance, "default")]
                except Exception:
                    provider_instance = None
        if not provider_instance:
            models_str = ", ".join(unique_models) if unique_models else "unconfigured"
            raise RuntimeError(f"no valid embedding model found [{models_str}]")

        probe = await self._probe_embedding_dimension(
            provider_instance,
            self._provider_source_id(provider_instance, unique_models[0] if unique_models else ""),
        )
        query_dimension = probe.get("query_dimension")
        if probe.get("dimension_probe_status") in {
            "failed", "unavailable", "invalid", "timeout", "shutdown_rejected"
        }:
            self._vector_dimension_check_status = probe.get("dimension_probe_status") or "probe_failed"
            self._vector_state = "degraded"
            raise RuntimeError(
                f"dimension probe failed: {probe.get('dimension_probe_error') or 'unknown'}"
            )
        elif query_dimension:
            self._vector_dimension_check_status = "checking"

        async with self._faiss_lock:
            if generation != self._vector_generation:
                return
            if self._is_ready and self._vector_state == "ready":
                return
            migration_applied = await self.v2_store.migration_applied("2_index_rebuild")
            published_index_path = (
                None
                if self._force_index_rebuild
                else (
                    await asyncio.to_thread(
                        self._load_published_vector_index,
                        unique_models,
                        query_dimension,
                    )
                    if query_dimension is not None
                    else None
                )
            )
            rebuild_required = self._force_index_rebuild or not migration_applied or published_index_path is None
            if published_index_path is None and not self._force_index_rebuild:
                self._vector_state = "dimension_mismatch_detected" if self._vector_dimension_check_status == "mismatch" else "degraded"
            if rebuild_required:
                self._vector_rebuild_started_total += 1
            projection_retry_snapshot = getattr(self.v2_store, "projection_retry_snapshot_with_revisions", None)
            candidate_outbox_candidates = set()
            candidate_outbox_watermarks = {}
            if callable(projection_retry_snapshot):
                try:
                    snapshot = await projection_retry_snapshot() or {}
                    candidate_outbox_candidates = set(snapshot.keys())
                    candidate_outbox_watermarks = {
                        str(key): int((value or {}).get("revision", 0))
                        for key, value in snapshot.items()
                    }
                except Exception as exc:
                    logger.warning(f"[AstrMai] projection outbox snapshot degraded during rebuild: {exc}")
            candidate_index_path = (
                self._new_vector_index_path(generation)
                if rebuild_required
                else published_index_path
            )
            async def _candidate_ready() -> bool:
                return True

            async def _make_candidate(index_path):
                candidate_db = None
                candidate_retriever = None
                try:
                    candidate_db = await self._construct_vector_candidate(
                        index_path=index_path,
                        embedding_provider=provider_instance,
                    )
                    await candidate_db.initialize()
                    candidate_retriever = VectorRetriever(candidate_db, self.config)
                    candidate_hybrid = HybridRetriever(
                        self.bm25_retriever,
                        candidate_retriever,
                        config=self.config,
                    )
                    candidate_engine = SimpleNamespace(
                        config=self.config,
                        v2_store=self.v2_store,
                        db_path=self.db_path,
                        faiss_db=candidate_db,
                        vec_retriever=candidate_retriever,
                        retriever=candidate_hybrid,
                        background_task_budget=getattr(self, "background_task_budget", None),
                        _projection_lock=self._projection_lock,
                        _ack_projection_outbox=False,
                        _candidate_outbox_candidates=candidate_outbox_candidates,
                        _candidate_outbox_watermarks=candidate_outbox_watermarks,
                        _ensure_faiss_initialized=_candidate_ready,
                        _run_documents_query=self._run_documents_query,
                        _execute_documents_write=self._execute_documents_write,
                        _build_memory_metadata=self._build_memory_metadata,
                    )
                    candidate_projector = MemoryIndexProjector(candidate_engine)
                    candidate_retriever._projection_count_provider = candidate_projector.projection_count
                    return candidate_db, candidate_retriever, candidate_hybrid, candidate_projector
                except (asyncio.CancelledError, Exception):
                    closed = await self._await_vector_stack_close(
                        candidate_retriever,
                        candidate_db,
                        timeout_sec=self._vector_close_timeout_sec(),
                    )
                    if closed:
                        self._forget_vector_stack_close(candidate_retriever, candidate_db)
                    if not closed:
                        self._schedule_vector_stack_retirement(
                            candidate_retriever,
                            candidate_db,
                            index_path=index_path,
                            generation=self._vector_generation,
                            delete_index=True,
                        )
                    try:
                        with self._vector_registry_lock:
                            self._vector_candidate_paths.discard(
                                str(Path(index_path).resolve())
                            )
                    except OSError:
                        with self._vector_registry_lock:
                            self._vector_candidate_paths.discard(str(index_path))
                    raise

            lifecycle.index_path = candidate_index_path
            lifecycle.discard_index = rebuild_required
            candidate_db, candidate_retriever, candidate_hybrid, candidate_projector = await _make_candidate(
                candidate_index_path
            )
            lifecycle.faiss_db = candidate_db
            lifecycle.retriever = candidate_retriever
            candidate_dimension, candidate_dimension_source = self._index_dimension(candidate_db)
            if query_dimension is not None and candidate_dimension is None:
                self._vector_dimension_check_status = "unknown"
                raise RuntimeError("candidate vector dimension is unavailable")
            if query_dimension is not None and candidate_dimension is not None and int(candidate_dimension) != int(query_dimension):
                self._vector_dimension_mismatch_total += 1
                self._vector_dimension_check_status = "mismatch"
                raise RuntimeError(
                    f"candidate vector dimension mismatch: index_dim={candidate_dimension} query_dim={query_dimension}"
                )
            if candidate_dimension is not None:
                self._vector_dimension_source = candidate_dimension_source
            self._projection_rebuild_active = True
            self._vector_state = "rebuilding"
            if rebuild_required:
                rebuilt = await candidate_projector.rebuild_all()
                await self.v2_store.record_migration(
                    "2_index_rebuild",
                    status="applied",
                    detail=f"rebuilt={rebuilt}",
                )
            report = await candidate_projector.check_consistency()
            if report.get("error"):
                raise RuntimeError(f"vector consistency scan failed: {report['error']}")
            needs_repair = any(
                report.get(key)
                for key in (
                    "missing_projection_ids",
                    "orphan_projection_ids",
                    "inactive_projection_ids",
                    "duplicate_projection_ids",
                    "faiss_ids_missing_from_documents",
                    "document_ids_missing_from_faiss",
                )
            )
            index_count_mismatch = bool(
                report.get("faiss_index_count_observed")
                and int(report.get("faiss_index_count_delta_vs_projection") or 0) != 0
            )
            exact_id_mismatch = bool(
                report.get("faiss_ids_missing_from_documents")
                or report.get("document_ids_missing_from_faiss")
            )
            if (index_count_mismatch or exact_id_mismatch) and not rebuild_required:
                if index_count_mismatch or exact_id_mismatch:
                    closed = await self._await_vector_stack_close(
                        candidate_retriever,
                        candidate_db,
                        timeout_sec=self._vector_close_timeout_sec(),
                    )
                    if closed:
                        self._forget_vector_stack_close(candidate_retriever, candidate_db)
                    if not closed:
                        self._schedule_vector_stack_retirement(
                            candidate_retriever,
                            candidate_db,
                            index_path=candidate_index_path,
                            generation=self._vector_generation,
                            delete_index=True,
                        )
                    try:
                        with self._vector_registry_lock:
                            self._vector_candidate_paths.discard(
                                str(Path(candidate_index_path).resolve())
                            )
                    except OSError:
                        with self._vector_registry_lock:
                            self._vector_candidate_paths.discard(str(candidate_index_path))
                    candidate_index_path = self._new_vector_index_path(generation)
                    lifecycle.index_path = candidate_index_path
                    lifecycle.discard_index = True
                    rebuild_required = True
                    candidate_db, candidate_retriever, candidate_hybrid, candidate_projector = await _make_candidate(
                        candidate_index_path
                    )
                    lifecycle.faiss_db = candidate_db
                    lifecycle.retriever = candidate_retriever
                rebuilt = await candidate_projector.rebuild_all()
                await self.v2_store.record_migration(
                    "2_index_rebuild",
                    status="applied",
                    detail=(
                        f"rebuilt={rebuilt},reason="
                        f"{'id_set_mismatch' if exact_id_mismatch else 'index_count_mismatch'}"
                    ),
                )
                report = await candidate_projector.check_consistency()
                if report.get("error"):
                    raise RuntimeError(f"vector consistency scan failed: {report['error']}")
                needs_repair = any(
                    report.get(key)
                    for key in (
                        "missing_projection_ids",
                        "orphan_projection_ids",
                        "inactive_projection_ids",
                        "duplicate_projection_ids",
                        "faiss_ids_missing_from_documents",
                        "document_ids_missing_from_faiss",
                    )
                )
            if needs_repair:
                report["repair"] = await candidate_projector.repair_consistency(report)
                report = await candidate_projector.check_consistency()
                if report.get("error"):
                    raise RuntimeError(f"vector consistency scan failed: {report['error']}")
            cutover_timeout = max(
                0.05,
                self._timing_value("projection_lock_timeout_sec", 1.0),
            )
            try:
                await asyncio.wait_for(self._projection_lock.acquire(), timeout=cutover_timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("vector index cutover lock timeout") from exc
            try:
                # Recheck while the cutover barrier is held. Online projectors see
                # _projection_rebuild_active and enqueue instead of mutating docs.db.
                report = await candidate_projector.check_consistency()
                if report.get("error"):
                    raise RuntimeError(f"vector consistency scan failed: {report['error']}")
                unresolved = {
                    key: list(report.get(key) or [])
                    for key in (
                        "missing_projection_ids",
                        "orphan_projection_ids",
                        "inactive_projection_ids",
                        "duplicate_projection_ids",
                        "faiss_ids_missing_from_documents",
                        "document_ids_missing_from_faiss",
                    )
                    if report.get(key)
                }
                if unresolved:
                    raise RuntimeError(f"vector index consistency repair incomplete: {unresolved}")
                if (
                    report.get("faiss_index_count_observed")
                    and int(report.get("faiss_index_count_delta_vs_projection") or 0) != 0
                ):
                    raise RuntimeError(
                        "vector index count does not match canonical projections: "
                        f"delta={report.get('faiss_index_count_delta_vs_projection')}"
                    )
                await candidate_retriever.refresh_storage_metrics(force=True)
                if generation != self._vector_generation:
                    return
                if (
                    not getattr(self, "_accepting_vector_work", True)
                    or int(getattr(self, "_vector_shutdown_generation", 0) or 0)
                    != int(lifecycle.shutdown_generation)
                ):
                    return

                previous_retriever = self.vec_retriever
                previous_faiss_db = self.faiss_db
                previous_index_path = self._vector_index_path
                if rebuild_required:
                    self._publish_vector_index_manifest(
                        candidate_index_path,
                        unique_models,
                        dimension=candidate_dimension or query_dimension,
                        provider_source_id=probe.get("provider_source_id", ""),
                        api_base_fingerprint=self._api_base_fingerprint(provider_instance, self.config),
                        vector_count=report.get("faiss_index_count_observed"),
                        generation=generation,
                    )
                self.faiss_db = candidate_db
                self.vec_retriever = candidate_retriever
                self.retriever = candidate_hybrid
                self._vector_index_path = str(candidate_index_path)
                self._vector_consistency_report = dict(report or {})
                self._force_index_rebuild = False
                self._index_consistency_repaired = True
                self._init_failures = 0
                self._next_retry_time = 0.0
                self._is_ready = True
                self._vector_state = "ready"
                self._vector_dimension_check_status = "matched" if query_dimension is not None else "unknown"
                if rebuild_required:
                    self._vector_rebuild_succeeded_total += 1
                self._vector_bootstrap_completed_at = time.time()
                lifecycle.published = True
                lifecycle.discard_index = False
                if previous_retriever is not candidate_retriever or previous_faiss_db is not candidate_db:
                    closed = await self._await_vector_stack_close(
                        previous_retriever,
                        previous_faiss_db,
                        timeout_sec=self._vector_close_timeout_sec(),
                    )
                    if closed:
                        self._forget_vector_stack_close(previous_retriever, previous_faiss_db)
                    if not closed:
                        self._schedule_vector_stack_retirement(
                            previous_retriever,
                            previous_faiss_db,
                            index_path=previous_index_path,
                            generation=generation - 1,
                        )
                active_projector = getattr(self, "index_projector", None)
                confirm = getattr(active_projector, "confirm_projection_outbox", None)
                if callable(confirm):
                    pending_ids = getattr(
                        candidate_projector,
                        "candidate_outbox_confirmations",
                        getattr(candidate_projector, "candidate_outbox_ids", lambda: set()),
                    )()
                    try:
                        await confirm(pending_ids)
                    except Exception as exc:
                        logger.warning(f"[AstrMai] projection outbox confirmation deferred: {exc}")
                replay = getattr(active_projector, "replay_pending_after_ready", None)
                if callable(replay) and getattr(self, "_accepting_vector_work", True):
                    previous_replay = self._projection_ready_replay_task
                    if previous_replay is None or previous_replay.done():
                        self._projection_replay_status = "running"
                        self._projection_replay_error = ""
                        self._projection_ready_replay_task = asyncio.create_task(
                            replay(limit=int(self._timing_value("projection_retry_batch_size", 20) or 20)),
                            name="astrmai-memory-projection-ready-replay",
                        )
                        replay_task = self._projection_ready_replay_task

                        replay_task.add_done_callback(self._handle_projection_replay_result)
                try:
                    with self._vector_registry_lock:
                        self._vector_candidate_paths.discard(
                            str(Path(candidate_index_path).resolve())
                        )
                except OSError:
                    with self._vector_registry_lock:
                        self._vector_candidate_paths.discard(str(candidate_index_path))
                self._cleanup_stale_vector_indexes(candidate_index_path, keep_history=1)
                logger.info("[AstrMai] hybrid memory engine ready (BM25 + FaissVecDB).")
            finally:
                self._projection_lock.release()

    async def _ensure_faiss_initialized(self):
        if self._projection_rebuild_active or self._vector_state == "rebuilding":
            self._vector_old_retriever_blocked_total += 1
            return False
        if self._is_ready:
            self._vector_state = "ready"
            return True
        if not HAS_FAISS:
            if time.time() >= self._next_retry_time:
                self._mark_vector_bootstrap_failed(
                    RuntimeError("faiss is unavailable in current environment")
                )
            return False
        configured_models = list(dict.fromkeys(
            str(item).strip() for item in self.embedding_models or [] if str(item).strip()
        ))
        if len(configured_models) > 1:
            self._vector_configuration_error = "multiple_embedding_models_not_supported"
            self._vector_state = "degraded"
            self._vector_dimension_check_status = "configuration_error"
            return False
        if not configured_models:
            providers_fn = getattr(self.context, "get_all_embedding_providers", None)
            try:
                available_providers = list(providers_fn() or []) if callable(providers_fn) else []
            except Exception:
                available_providers = []
            if not available_providers:
                if time.time() >= self._next_retry_time:
                    self._mark_vector_bootstrap_failed(
                        RuntimeError("no embedding model or default provider configured")
                    )
                return False
        self._schedule_vector_bootstrap()
        return False

    def describe_shutdown_owners(self) -> dict[str, Any]:
        def _running(owner: Any) -> bool:
            return owner is not None and not owner.done()

        def _owner_name(owner: Any, fallback: str) -> str:
            get_name = getattr(owner, "get_name", None)
            if callable(get_name):
                try:
                    return str(get_name())
                except Exception:
                    pass
            return fallback

        with self._vector_registry_lock:
            retirement_tasks = [
                task for task in self._vector_retirement_tasks if _running(task)
            ]
            candidate_build_tasks = [
                task for task in self._vector_candidate_build_tasks if _running(task)
            ]
            candidate_futures = [
                future for future in self._vector_candidate_futures if _running(future)
            ]
            sync_retirement_futures = [
                future
                for future in self._vector_sync_retirement_futures
                if _running(future)
            ]
            close_owners = [
                owner
                for _resource, owner in self._vector_close_tasks.values()
                if _running(owner)
            ]
            candidate_path_count = len(self._vector_candidate_paths)
            retired_stacks = [dict(stack) for stack in self._retired_vector_stacks.values()]
        dimension_probe_task = getattr(self, "_vector_dimension_probe_task", None)
        dimension_probe_count = int(_running(dimension_probe_task))

        owner_names = {
            *(
                _owner_name(task, "memory.vector_retirement")
                for task in retirement_tasks
            ),
            *(
                _owner_name(task, "memory.vector_candidate_build")
                for task in candidate_build_tasks
            ),
            *("astrmai-vector-candidate-physical" for _future in candidate_futures),
            *("astrmai-vector-sync-retirement" for _future in sync_retirement_futures),
            *(
                _owner_name(owner, "memory.vector_close_owner")
                for owner in close_owners
            ),
            *("memory.vector_dimension_probe" for _ in range(dimension_probe_count)),
        }
        retirement_statuses = {
            str(stack.get("status") or "pending") for stack in retired_stacks
        }
        return {
            "vector_retirement_count": len(retirement_tasks),
            "vector_candidate_build_count": len(candidate_build_tasks),
            "vector_candidate_physical_count": len(candidate_futures),
            "vector_candidate_path_count": candidate_path_count,
            "vector_sync_retirement_count": len(sync_retirement_futures),
            "vector_close_owner_count": len(close_owners),
            "vector_dimension_probe_count": dimension_probe_count,
            "retired_vector_stack_count": len(retired_stacks),
            "owner_task_names": sorted(owner_names),
            "vector_retirement_by_status": {
                status: sum(
                    1
                    for stack in retired_stacks
                    if str(stack.get("status") or "pending") == status
                )
                for status in sorted(retirement_statuses)
            },
            "vector_close_attempt_total": sum(
                int(stack.get("close_attempt_total", stack.get("attempts", 0)) or 0)
                for stack in retired_stacks
            ),
            "vector_shutdown_retry_attempts": sum(
                int(stack.get("shutdown_retry_attempts", 0) or 0)
                for stack in retired_stacks
            ),
            "vector_physical_timeout_exceeded_count": sum(
                int(bool(stack.get("physical_timeout_exceeded")))
                for stack in retired_stacks
            ),
        }

    def describe_vector_status(self) -> dict[str, Any]:
        retriever = self.vec_retriever
        shutdown_owners = self.describe_shutdown_owners()
        runtime = (
            retriever.describe_status()
            if retriever is not None and hasattr(retriever, "describe_status")
            else {"available": False}
        )
        runtime.update(
            {
                "state": self._vector_state,
                "available": bool(self._is_ready and self._vector_state == "ready"),
                "bootstrap_running": bool(
                    self._vector_bootstrap_task is not None
                    and not self._vector_bootstrap_task.done()
                ),
                "projection_ready_replay_running": bool(
                    self._projection_ready_replay_task is not None
                    and not self._projection_ready_replay_task.done()
                ),
                "bootstrap_started_at": self._vector_bootstrap_started_at or None,
                "bootstrap_completed_at": self._vector_bootstrap_completed_at or None,
                "last_error": self._vector_last_error,
                "next_retry_at": self._next_retry_time or None,
                "consistency": dict(self._vector_consistency_report),
                "index_path": self._vector_index_path,
                "vector_retirement_task_count": shutdown_owners["vector_retirement_count"],
                "vector_candidate_build_task_count": shutdown_owners["vector_candidate_build_count"],
                "vector_candidate_physical_future_count": shutdown_owners[
                    "vector_candidate_physical_count"
                ],
                "vector_candidate_path_count": shutdown_owners["vector_candidate_path_count"],
                "vector_sync_retirement_future_count": shutdown_owners[
                    "vector_sync_retirement_count"
                ],
                "vector_retirement_by_status": shutdown_owners["vector_retirement_by_status"],
                "vector_close_attempt_total": shutdown_owners["vector_close_attempt_total"],
                "vector_shutdown_retry_attempts": shutdown_owners[
                    "vector_shutdown_retry_attempts"
                ],
                "vector_physical_timeout_exceeded_count": shutdown_owners[
                    "vector_physical_timeout_exceeded_count"
                ],
                "vector_physical_timeout_exceeded_total": int(
                    getattr(self, "_vector_physical_timeout_exceeded_total", 0) or 0
                ),
                "index_dimension": self._index_dimension(self.faiss_db)[0],
                "physical_index_dimension": self._index_dimension(self.faiss_db)[0],
                "query_dimension": self._vector_query_dimension,
                "measured_query_dimension": self._vector_query_dimension,
                "configured_dimension": self._configured_vector_dimension,
                "dimension_source": self._vector_dimension_source,
                "dimension_check_status": self._vector_dimension_check_status,
                "configuration_error": self._vector_configuration_error,
                "migration_state": self._vector_state,
                "migration_generation": int(self._vector_generation),
                "dimension_mismatch_total": int(self._vector_dimension_mismatch_total),
                "dimension_probe_failed_total": int(self._vector_dimension_probe_failed_total),
                "dimension_probe_timeout_total": int(
                    getattr(self, "_vector_dimension_probe_timeout_total", 0) or 0
                ),
                "dimension_probe_invalid_vector_total": int(
                    getattr(self, "_vector_dimension_probe_invalid_total", 0) or 0
                ),
                "dimension_probe_unavailable_total": int(
                    getattr(self, "_vector_dimension_probe_unavailable_total", 0) or 0
                ),
                "dimension_probe_provider_error_total": int(
                    getattr(self, "_vector_dimension_probe_provider_error_total", 0) or 0
                ),
                "rebuild_started_total": int(self._vector_rebuild_started_total),
                "rebuild_succeeded_total": int(self._vector_rebuild_succeeded_total),
                "rebuild_failed_total": int(self._vector_rebuild_failed_total),
                "old_retriever_blocked_total": int(self._vector_old_retriever_blocked_total),
                "index_descriptor": dict(self._vector_index_descriptor),
                "vector_close_owner_task_count": shutdown_owners["vector_close_owner_count"],
                "vector_dimension_probe_count": shutdown_owners[
                    "vector_dimension_probe_count"
                ],
                "retired_vector_stack_count": shutdown_owners["retired_vector_stack_count"],
                "vector_close_state": str(getattr(self, "_vector_close_state", "idle")),
                "last_projector_shutdown": dict(
                    getattr(self, "_last_projector_shutdown", {}) or {}
                ),
                "projection_replay_status": self._projection_replay_status,
                "projection_replay_error": self._projection_replay_error,
                "projection_replay_completed_at": self._projection_replay_completed_at or None,
            }
        )
        projector = getattr(self, "index_projector", None)
        describe_projector = getattr(projector, "describe_status", None)
        if callable(describe_projector):
            try:
                runtime["projection"] = describe_projector()
            except Exception as exc:
                runtime["projection"] = {
                    "repair_required": True,
                    "diagnostics_error": type(exc).__name__,
                }
        return runtime

    def _handle_projection_replay_result(self, task: asyncio.Task) -> None:
        if self._projection_ready_replay_task is not task:
            return
        self._projection_ready_replay_task = None
        self._projection_replay_completed_at = time.time()
        if task.cancelled():
            self._projection_replay_status = "cancelled"
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            self._projection_replay_status = "cancelled"
            return
        if error is None:
            self._projection_replay_status = "completed"
            return
        self._projection_replay_status = "failed"
        self._projection_replay_error = f"{type(error).__name__}: {error}"[:500]
        logger.warning(f"[AstrMai] projection replay failed: {error}")

    async def add_memory(
        self,
        content: str,
        session_id: str,
        persona_id: str = None,
        importance: float = 0.8,
        sender_id: str = "",
        created_at: float = 0.0,
    ):
        request = MemoryWriteRequest(
            source="legacy_add_memory",
            kind="persona_lore" if session_id == "__self_lore__" else "memory",
            session_id=str(session_id or ""),
            sender_id=str(sender_id or ""),
            persona_id=str(persona_id or ""),
            content=str(content or ""),
            importance=float(importance or 0.8),
            confidence=0.8,
            source_ref="memory_engine.add_memory",
            created_at=float(created_at or 0.0),
        )
        return await self.write_service.write(request)

    @staticmethod
    def _feedback_prefix(source: str) -> str:
        clean_source = str(source or "unknown").strip().lower() or "unknown"
        return f"[cognitive_feedback:{clean_source}]"

    @staticmethod
    def _normalize_feedback_tags(tags: list[str] | None) -> list[str]:
        result: list[str] = []
        for tag in tags or []:
            clean = str(tag or "").strip().lower()
            if clean and clean not in result:
                result.append(clean)
        return result[:12]

    @classmethod
    def _format_cognitive_feedback_content(
        cls,
        *,
        source: str,
        summary: str,
        guidance: str = "",
        tags: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        lines = [
            cls._feedback_prefix(source),
            f"summary: {str(summary or '').strip()}",
        ]
        if str(guidance or "").strip():
            lines.append(f"guidance: {str(guidance or '').strip()}")
        clean_tags = cls._normalize_feedback_tags(tags)
        if clean_tags:
            lines.append("tags: " + ", ".join(clean_tags))
        clean_payload = normalize_payload(payload)
        if clean_payload:
            lines.append("payload: " + json.dumps(clean_payload, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines).strip()

    @classmethod
    def _parse_cognitive_feedback_content(
        cls,
        text: str,
        *,
        chat_id: str,
        timestamp: float = 0.0,
        importance: float = 0.5,
        payload: dict[str, Any] | None = None,
    ) -> CognitiveFeedbackSignal | None:
        content = str(text or "").strip()
        if not content.startswith("[cognitive_feedback:"):
            return None
        first_line, *rest = content.splitlines()
        source = first_line.removeprefix("[cognitive_feedback:").removesuffix("]").strip() or "unknown"
        summary = ""
        guidance = ""
        tags: list[str] = []
        parsed_payload = normalize_payload(payload)
        for line in rest:
            if line.startswith("summary:"):
                summary = line.split(":", 1)[1].strip()
            elif line.startswith("guidance:"):
                guidance = line.split(":", 1)[1].strip()
            elif line.startswith("tags:"):
                tags = cls._normalize_feedback_tags(line.split(":", 1)[1].split(","))
            elif line.startswith("payload:") and not parsed_payload:
                try:
                    parsed_payload = normalize_payload(json.loads(line.split(":", 1)[1].strip()))
                except (TypeError, ValueError, json.JSONDecodeError):
                    logger.debug("[MemoryEngine] cognitive feedback payload parse failed", exc_info=True)
        if not summary and not guidance:
            return None
        summary, guidance, _display_tags, parsed_payload = render_feedback(
            source=source,
            summary=summary,
            guidance=guidance,
            tags=tags,
            payload=parsed_payload,
        )
        return CognitiveFeedbackSignal(
            source=source,
            chat_id=chat_id,
            summary=summary,
            guidance=guidance,
            tags=tags,
            timestamp=float(timestamp or 0.0),
            importance=float(importance or 0.5),
            payload=parsed_payload,
        )

    def _remember_cognitive_feedback(self, signal: CognitiveFeedbackSignal) -> None:
        items = self._cognitive_feedback_cache.setdefault(signal.chat_id, [])
        items[:] = [item for item in items if item.source != signal.source]
        items.append(signal)
        if len(items) > 32:
            del items[:-32]
        if len(self._cognitive_feedback_cache) > 100:
            oldest = next(iter(self._cognitive_feedback_cache))
            del self._cognitive_feedback_cache[oldest]

    @staticmethod
    def _cognitive_feedback_key(signal: CognitiveFeedbackSignal) -> tuple[str, str, str, str]:
        return (
            str(signal.chat_id or ""),
            str(signal.source or ""),
            str(signal.summary or ""),
            str(signal.guidance or ""),
        )

    @staticmethod
    def _cognitive_feedback_key_str(signal: CognitiveFeedbackSignal) -> str:
        return f"{signal.chat_id}|{signal.source}|{signal.summary}|{signal.guidance}"

    def disable_cognitive_feedback(self, signal: CognitiveFeedbackSignal) -> None:
        now = time.time()
        key = self._cognitive_feedback_key_str(signal)
        self._disabled_cognitive_feedback_keys[key] = now
        # ponytail: lazy TTL cleanup on each disable
        stale = [k for k, ts in list(self._disabled_cognitive_feedback_keys.items()) if now - ts > 7 * 86400]
        for k in stale:
            del self._disabled_cognitive_feedback_keys[k]

    async def _cleanup_cognitive_feedback_records(self, *, force: bool = False) -> int:
        now = time.time()
        if not force and now - self._last_feedback_cleanup_ts < 3600:
            return 0
        self._last_feedback_cleanup_ts = now
        maintenance = getattr(self, "maintenance_service", None)
        if maintenance is None:
            return 0
        try:
            page = await self.v2_store.list_canonical(
                kind="feedback",
                status="active",
                limit=500,
                include_inactive=True,
            )
            candidates = list(page.get("items", []) or [])
            seen: set[tuple[str, str]] = set()
            delete_reasons: dict[str, str] = {}
            for candidate in candidates:
                memory_id = str(candidate.get("id") or "")
                metadata = dict(candidate.get("metadata") or {})
                valid_until = float(metadata.get("valid_until") or 0.0)
                key = (
                    str(candidate.get("session_id") or ""),
                    str(candidate.get("source") or "unknown").strip().lower(),
                )
                if valid_until > 0 and valid_until < now:
                    delete_reasons[memory_id] = "expired_cognitive_feedback"
                elif key in seen:
                    delete_reasons[memory_id] = "superseded_rolling_feedback"
                else:
                    seen.add(key)
            changed = 0
            for memory_id, reason in delete_reasons.items():
                if memory_id:
                    changed += int(await maintenance.soft_delete(memory_id, reason=reason) or 0)
            return changed
        except Exception as exc:
            logger.warning(f"[MemoryEngine] cognitive feedback cleanup degraded: {exc}")
            return 0

    async def migrate_legacy_cognitive_feedback(self) -> int:
        version = "feedback_schema_v2"
        required = ("migration_applied", "list_canonical", "update_memory", "record_migration")
        if not all(hasattr(self.v2_store, name) for name in required):
            return 0
        if await self.v2_store.migration_applied(version):
            return 0
        migrated = 0
        try:
            page = await self.v2_store.list_canonical(
                kind="feedback",
                status="active",
                limit=500,
                include_inactive=True,
            )
            for candidate in list(page.get("items", []) or []):
                metadata = dict(candidate.get("metadata") or {})
                if int(metadata.get("feedback_schema_version") or 1) >= FEEDBACK_SCHEMA_VERSION:
                    continue
                source = str(candidate.get("source") or "unknown").strip().lower() or "unknown"
                signal = self._parse_cognitive_feedback_content(
                    str(candidate.get("content") or ""),
                    chat_id=str(candidate.get("session_id") or ""),
                    timestamp=float(candidate.get("created_at") or 0.0),
                    importance=float(candidate.get("importance") or 0.5),
                )
                if signal is None:
                    continue
                metadata.update(
                    {
                        "guidance": signal.guidance,
                        "feedback_schema_version": FEEDBACK_SCHEMA_VERSION,
                        "feedback_payload": signal.payload,
                    }
                )
                changed = await self.v2_store.update_memory(
                    str(candidate.get("id") or ""),
                    content=self._format_cognitive_feedback_content(
                        source=source,
                        summary=signal.summary,
                        guidance=signal.guidance,
                        tags=signal.tags,
                        payload=signal.payload,
                    ),
                    summary=signal.summary,
                    tags=signal.tags,
                    metadata=metadata,
                )
                migrated += int(bool(changed))
            await self.v2_store.record_migration(version, status="applied", detail=f"migrated={migrated}")
        except Exception as exc:
            await self.v2_store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] cognitive feedback migration degraded: {exc}")
        return migrated

    async def record_cognitive_feedback(
        self,
        session_id: str,
        source: str,
        summary: str,
        guidance: str = "",
        tags: list[str] | None = None,
        importance: float = 0.5,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._cleanup_cognitive_feedback_records()
        chat_id = str(session_id or "").strip()
        clean_summary = str(summary or "").strip()
        clean_guidance = str(guidance or "").strip()
        if not chat_id or not (clean_summary or clean_guidance):
            return
        clean_source = str(source or "unknown").strip().lower() or "unknown"
        clean_tags = self._normalize_feedback_tags(tags)
        clean_summary, clean_guidance, _display_tags, clean_payload = render_feedback(
            source=clean_source,
            summary=clean_summary,
            guidance=clean_guidance,
            tags=clean_tags,
            payload=payload,
        )
        now = time.time()
        signal = CognitiveFeedbackSignal(
            source=clean_source,
            chat_id=chat_id,
            summary=clean_summary[:500],
            guidance=clean_guidance[:500],
            tags=clean_tags,
            timestamp=now,
            importance=float(importance or 0.5),
            payload=clean_payload,
        )
        self._remember_cognitive_feedback(signal)
        valid_until = now + (72 * 3600)
        await self.write_service.write(
            MemoryWriteRequest(
                source=signal.source,
                kind="feedback",
                session_id=chat_id,
                content=self._format_cognitive_feedback_content(
                    source=signal.source,
                    summary=signal.summary,
                    guidance=signal.guidance,
                    tags=signal.tags,
                    payload=signal.payload,
                ),
                summary=signal.summary,
                tags=signal.tags,
                importance=signal.importance,
                confidence=0.8,
                metadata={
                    "guidance": signal.guidance,
                    "cognitive_feedback": True,
                    "valid_until": valid_until,
                    "feedback_window": "rolling",
                    "feedback_schema_version": FEEDBACK_SCHEMA_VERSION,
                    "feedback_payload": signal.payload,
                },
                dedup_key=f"feedback:{chat_id}:{signal.source}:rolling",
                source_ref=f"cognitive_feedback:{signal.source}",
                visibility="tool_only",
            )
        )

    async def get_cognitive_feedback(
        self,
        session_id: str,
        *,
        limit: int = 3,
        max_age_seconds: float = 72 * 3600,
        sources: set[str] | None = None,
    ) -> list[CognitiveFeedbackSignal]:
        chat_id = str(session_id or "").strip()
        if not chat_id:
            return []
        now = time.time()
        source_filter = {str(item).strip().lower() for item in sources or set() if str(item).strip()}
        signals: list[CognitiveFeedbackSignal] = []

        for item in self._cognitive_feedback_cache.get(chat_id, []):
            if max_age_seconds is not None and now - float(item.timestamp or 0.0) > max_age_seconds:
                continue
            if source_filter and item.source not in source_filter:
                continue
            signals.append(item)

        try:
            where = ["kind = ?", "session_id = ?", "status = 'active'"]
            params: list[Any] = ["feedback", chat_id]
            if max_age_seconds is not None:
                where.append("create_time >= ?")
                params.append(now - max_age_seconds)
            rows = await self._run_documents_query(
                f"""
                SELECT content, metadata, create_time
                FROM canonical_memories
                WHERE {' AND '.join(where)}
                ORDER BY create_time DESC
                LIMIT ?
                """,
                (*params, max(limit * 4, limit)),
                db_path=self.v2_db_path,
            )
            for text, metadata_raw, create_time_raw in rows:
                timestamp = float(create_time_raw or 0.0)
                importance = 0.5
                metadata: dict[str, Any] = {}
                try:
                    metadata = json.loads(metadata_raw or "{}") if isinstance(metadata_raw, str) else {}
                    importance = float(metadata.get("importance") or 0.5)
                    valid_until = float(metadata.get("valid_until") or 0.0)
                    if valid_until > 0 and valid_until < now:
                        continue
                except Exception:
                    logger.debug("[MemoryEngine] cognitive feedback metadata parse failed", exc_info=True)
                    pass
                parse_kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "timestamp": timestamp,
                    "importance": importance,
                }
                metadata_payload = dict(metadata.get("feedback_payload") or {})
                if metadata_payload:
                    parse_kwargs["payload"] = metadata_payload
                parsed = self._parse_cognitive_feedback_content(str(text or ""), **parse_kwargs)
                if not parsed:
                    continue
                if source_filter and parsed.source not in source_filter:
                    continue
                signals.append(parsed)
        except Exception as exc:
            logger.warning(f"[MemoryEngine] cognitive feedback lookup from canonical_memories degraded: {exc}")

        unique: list[CognitiveFeedbackSignal] = []
        seen: set[tuple[str, str, str]] = set()
        for item in sorted(signals, key=lambda signal: signal.timestamp, reverse=True):
            if self._cognitive_feedback_key_str(item) in self._disabled_cognitive_feedback_keys:
                continue
            key = (item.source, item.summary, item.guidance)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= limit:
                break
        return unique

    async def list_cognitive_feedback_records(
        self,
        *,
        session_id: str = "",
        source: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        await self._cleanup_cognitive_feedback_records()
        page = await self.v2_store.list_canonical(
            session_id=str(session_id or ""),
            source=str(source or "").strip().lower(),
            kind="feedback",
            status="active",
            limit=max(1, min(int(limit or 50), 300)),
            offset=max(0, int(offset or 0)),
            include_inactive=False,
        )
        items: list[dict[str, Any]] = []
        now = time.time()
        for candidate in list(page.get("items", []) or []):
            candidate_source = str(candidate.get("source", "unknown") or "unknown").strip().lower()
            metadata = dict(candidate.get("metadata", {}) or {})
            signal = self._parse_cognitive_feedback_content(
                str(candidate.get("content", "") or ""),
                chat_id=str(candidate.get("session_id", "") or ""),
                timestamp=float(candidate.get("created_at", 0.0) or 0.0),
                importance=float(candidate.get("importance", 0.5) or 0.5),
                payload=dict(metadata.get("feedback_payload") or {}),
            )
            raw_summary = str(candidate.get("summary", "") or "")
            raw_guidance = str(metadata.get("guidance", "") or "")
            display_summary, display_guidance, display_tags, payload = render_feedback(
                source=candidate_source,
                summary=signal.summary if signal else raw_summary,
                guidance=signal.guidance if signal else raw_guidance,
                tags=list(candidate.get("tags", []) or []),
                payload=signal.payload if signal else dict(metadata.get("feedback_payload") or {}),
            )
            valid_until = float(metadata.get("valid_until") or 0.0)
            expiry_state = "长期有效"
            if valid_until > 0:
                remaining = valid_until - now
                expiry_state = "已过期" if remaining <= 0 else ("即将过期" if remaining <= 6 * 3600 else "有效")
            items.append(
                {
                    "id": str(candidate.get("id", "") or ""),
                    "chat_id": str(candidate.get("session_id", "") or ""),
                    "session_id": str(candidate.get("session_id", "") or ""),
                    "source": candidate_source,
                    "source_label": source_label(candidate_source),
                    "summary": display_summary,
                    "guidance": display_guidance,
                    "tags": list(candidate.get("tags", []) or []),
                    "display_tags": display_tags,
                    "payload": payload,
                    "feedback_schema_version": int(metadata.get("feedback_schema_version") or 1),
                    "valid_until": valid_until,
                    "expiry_state": expiry_state,
                    "timestamp": float(candidate.get("created_at", 0.0) or 0.0),
                    "importance": float(candidate.get("importance", 0.5) or 0.5),
                    "status": str(candidate.get("status", "active") or "active"),
                    "persisted": True,
                }
            )
        return {
            "items": items,
            "total": int(page.get("total", len(items)) or 0),
            "limit": int(page.get("limit", limit) or limit),
            "offset": int(page.get("offset", offset) or offset),
        }

    async def disable_cognitive_feedback_record(self, memory_id: str) -> bool:
        clean_id = str(memory_id or "").strip()
        if not clean_id:
            return False
        return bool(await self.maintenance_service.soft_delete(clean_id, reason="webui_feedback_disabled"))

    async def clear_persona_lore(self, persona_id: str = None) -> int:
        return await self.maintenance_service.soft_delete_by_filter(
            kind="persona_lore",
            session_id="__self_lore__",
            persona_id=str(persona_id or ""),
            reason="persona_lore_rebuild",
        )

    async def add_persona_lore(self, content: str, persona_id: str = None):
        return await self.write_service.write(
            MemoryWriteRequest(
                source="persona_lore",
                kind="persona_lore",
                session_id="__self_lore__",
                persona_id=str(persona_id or ""),
                content=str(content or ""),
                summary=str(content or "")[:240],
                importance=1.0,
                confidence=0.9,
                dedup_key=f"persona_lore:{persona_id or ''}:{hash(str(content or ''))}",
                source_ref=f"persona_lore:{persona_id or ''}",
            )
        )

    async def recall_persona_lore(self, query: str, persona_id: str = None, top_k: int = 3) -> str:
        memory_query = MemoryQuery(
            query=str(query or ""),
            session_id="__self_lore__",
            persona_id=str(persona_id or ""),
            layers=["persona_lore"],
            top_k=int(top_k or 3),
            include_persona_lore=True,
            allow_stale=True,
        )
        candidates = await self.retrieval_service.retrieve(memory_query)
        if not candidates:
            return "(no relevant persona lore found)"
        return "\n".join(f"[fact] {item.summary or item.content}" for item in candidates)

    async def recall(self, query: str, session_id: str = None, persona_id: str = None, top_k: int = None, layers: list[str] | None = None, exclude_kinds: list[str] | None = None) -> str:
        recall_top_k = top_k if top_k is not None else getattr(getattr(self.config, "memory", None), "recall_top_k", 5)
        memory_query = MemoryQuery(
            query=str(query or ""),
            session_id=str(session_id or ""),
            persona_id=str(persona_id or ""),
            layers=list(layers or []),
            top_k=int(recall_top_k or 5),
            exclude_kinds=list(exclude_kinds) if exclude_kinds is not None else ["feedback"],
        )
        candidates = await self.retrieval_service.retrieve(memory_query)
        # Safety net: exclude_kinds in MemoryQuery handles the v2_store path,
        # but hybrid search results may still contain feedback items.  The
        # content-based check catches those as a defense-in-depth measure.
        candidates = [
            item for item in candidates
            if not self._is_cognitive_feedback_content(getattr(item, "content", ""))
        ]
        if not candidates:
            return f"No relevant memory found for '{query}'."
        logger.info(f"[Memory] recall matched {len(candidates)} high-relevance fragments.")
        return self.retrieval_service.render_recall(memory_query, candidates)

    async def query(self, query: str, session_id: str = "", **kwargs) -> str:
        return await self.recall(query=query, session_id=session_id, top_k=kwargs.get("top_k"))

    async def search(self, query: str, session_id: str = "", **kwargs) -> str:
        return await self.recall(query=query, session_id=session_id, top_k=kwargs.get("top_k"))

    async def query_persona_lore(self, query: str, persona_id: str = "", **kwargs) -> str:
        return await self.recall_persona_lore(query=query, persona_id=persona_id, top_k=kwargs.get("top_k", 3))

    async def start_background_tasks(self):
        self._accepting_vector_work = True
        self._accepting_dimension_probe = True
        projector = getattr(self, "index_projector", None)
        if projector is not None:
            await projector.start()
        raw_trace_store = getattr(getattr(self, "db_service", None), "raw_trace_store", None)
        self.memory_observer = MemoryObserver(
            raw_trace_store,
            observability_hub=getattr(self, "observability_hub", None),
        )
        self.session_summarizer = SessionMemorySummarizer(self.context, self.gateway, self, config=self.config)
        self.instant_gate = InstantMemoryGate(self.gateway, self, config=self.config)
        checkpoint_store = None
        persistence = getattr(getattr(self, "db_service", None), "persistence", None)
        checkpoint_db_path = getattr(persistence, "db_path", None)
        if checkpoint_db_path:
            checkpoint_store = MemoryTurnCheckpointStore(checkpoint_db_path)
        self.memory_pipeline = MemoryTurnPipeline(
            context=self.context,
            gateway=self.gateway,
            engine=self,
            session_summarizer=self.session_summarizer,
            instant_gate=self.instant_gate,
            event_bus=getattr(getattr(self, "db_service", None), "event_bus", None) or getattr(self.gateway, "event_bus", None),
            config=self.config,
            observer=self.memory_observer,
            checkpoint_store=checkpoint_store,
            background_task_budget=getattr(self, "background_task_budget", None),
        )
        await self.memory_pipeline.start()

    def schedule_vector_bootstrap_after_startup(self, *, delay_sec: float = 0.25) -> None:
        """Start vector bootstrap only after the basic runtime is accepting events."""
        existing = self._vector_bootstrap_delay_task
        if existing is not None and not existing.done():
            return

        async def _delayed_start() -> None:
            await asyncio.sleep(max(0.0, float(delay_sec or 0.0)))
            if getattr(self, "_accepting_vector_work", True):
                self._schedule_vector_bootstrap()

        try:
            self._vector_bootstrap_delay_task = asyncio.create_task(
                _delayed_start(), name="astrmai-vector-bootstrap-delay"
            )
        except RuntimeError:
            self._vector_bootstrap_delay_task = None

    async def stop_background_producers(self):
        self.begin_shutdown()
        pipeline = getattr(self, "memory_pipeline", None)
        begin_pipeline_shutdown = getattr(pipeline, "begin_shutdown", None)
        if callable(begin_pipeline_shutdown):
            begin_pipeline_shutdown()
        bootstrap_task = self._vector_bootstrap_task
        delay_task = self._vector_bootstrap_delay_task
        await self._cancel_background_task_safely(delay_task, "vector bootstrap delay")
        if self._vector_bootstrap_delay_task is delay_task:
            self._vector_bootstrap_delay_task = None
        probe_task = getattr(self, "_vector_dimension_probe_task", None)
        await self._cancel_background_task_safely(probe_task, "vector dimension probe")
        await self._cancel_background_task_safely(bootstrap_task, "vector bootstrap")
        if self._vector_bootstrap_task is bootstrap_task and bootstrap_task is not None and bootstrap_task.done():
            self._vector_bootstrap_task = None
        replay_task = self._projection_ready_replay_task
        await self._cancel_background_task_safely(replay_task, "projection replay")
        if self._projection_ready_replay_task is replay_task and replay_task is not None and replay_task.done():
            self._projection_ready_replay_task = None
        with self._vector_registry_lock:
            retirement_tasks = list(self._vector_retirement_tasks)
        if retirement_tasks:
            _done, pending = await asyncio.wait(
                retirement_tasks,
                timeout=self._vector_close_timeout_sec(),
            )
            for task in pending:
                logger.warning("[AstrMai] vector retirement remains pending during shutdown")
        if pipeline is not None:
            await pipeline.stop()
        projector = getattr(self, "index_projector", None)
        if projector is not None:
            self._last_projector_shutdown = await projector.stop()

    async def _cancel_background_task_safely(self, task: asyncio.Task | None, label: str) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            if not task.done():
                logger.warning(f"[AstrMai] {label} did not stop within grace")
        except Exception as exc:
            logger.warning(f"[AstrMai] {label} stopped with error: {exc}")

    def begin_shutdown(self) -> None:
        if not getattr(self, "_accepting_vector_work", True):
            return
        self._accepting_vector_work = False
        self._accepting_dimension_probe = False
        probe_task = getattr(self, "_vector_dimension_probe_task", None)
        if probe_task is not None and not probe_task.done():
            probe_task.cancel()
        self._vector_shutdown_generation = int(
            getattr(self, "_vector_shutdown_generation", 0) or 0
        ) + 1

    async def close_background_resources(self) -> bool:
        retriever = getattr(self, "vec_retriever", None)
        faiss_db = getattr(self, "faiss_db", None)
        timeout_sec = self._vector_close_timeout_sec()
        closed = await self._await_vector_stack_close(
            retriever,
            faiss_db,
            timeout_sec=timeout_sec,
        )
        if closed:
            if self.vec_retriever is retriever and self.faiss_db is faiss_db:
                self.faiss_db = None
                self.vec_retriever = None
                self.retriever = None
            self._forget_vector_stack_close(retriever, faiss_db)
        retired_closed = await self._close_retired_vector_stacks(timeout_sec=timeout_sec)
        if not closed or not retired_closed:
            self._vector_state = "degraded"
            self._is_ready = False
            self._vector_close_state = "pending"
            return False
        self._is_ready = False
        self._vector_state = "uninitialized"
        self._vector_close_state = "closed"
        with self._vector_registry_lock:
            executor = self._vector_sync_retirement_executor
            sync_retirement_idle = not any(
                future is not None and not future.done()
                for future in self._vector_sync_retirement_futures
            )
        if executor is not None and sync_retirement_idle:
            executor.shutdown(wait=False, cancel_futures=True)
            with self._vector_registry_lock:
                if self._vector_sync_retirement_executor is executor:
                    self._vector_sync_retirement_executor = None
        with self._vector_registry_lock:
            candidate_executor = self._vector_candidate_executor
            candidate_retirement_idle = not any(
                future is not None and not future.done()
                for future in self._vector_candidate_futures
            )
        if candidate_executor is not None and candidate_retirement_idle:
            candidate_executor.shutdown(wait=False, cancel_futures=True)
            with self._vector_registry_lock:
                if self._vector_candidate_executor is candidate_executor:
                    self._vector_candidate_executor = None
        return True

    async def stop_background_tasks(self):
        await self.stop_background_producers()
        await self.close_background_resources()

    async def run_memory_maintenance(self, chat_id: str) -> dict:
        pipeline = getattr(self, "memory_pipeline", None)
        if pipeline is None or not hasattr(pipeline, "run_maintenance_for_session"):
            return {"performed": False, "reason": "memory_pipeline_unavailable"}
        return await pipeline.run_maintenance_for_session(chat_id)

    async def describe_memory_eligibility(self, chat_id: str) -> dict:
        pipeline = getattr(self, "memory_pipeline", None)
        if pipeline is None or not hasattr(pipeline, "describe_session_eligibility"):
            return {
                "eligible": False,
                "candidate_present": False,
                "reason": "memory_pipeline_unavailable",
                "pending_messages": 0,
                "history_size": 0,
                "threshold_messages": 0,
                "cooldown_until": 0.0,
                "last_memory_run_at": 0.0,
                "last_update": 0.0,
            }
        return await pipeline.describe_session_eligibility(chat_id)

    async def apply_daily_decay(self, decay_rate: float, days: int = 1) -> int:
        return await self.maintenance_service.apply_daily_decay(
            decay_rate=decay_rate,
            days=days,
            min_score=getattr(getattr(self.config, "memory", None), "prune_threshold", 0.2),
            stale_grace_seconds=7 * 86400,
        )

    async def import_legacy_memory_events(self, *, limit: int = 1000) -> int:
        version = "2_memory_event_import"
        if await self.v2_store.migration_applied(version):
            return 0
        db_service = getattr(self, "db_service", None)
        if not db_service or not hasattr(db_service, "get_session"):
            logger.warning("[MemoryV2] MemoryEvent import deferred: db service unavailable")
            return 0
        imported = 0
        page_size = max(int(limit or 1000), 1)
        try:
            from ...infrastructure.persistence import MemoryEvent
            from sqlmodel import desc, select

            def _load_events(offset: int):
                with db_service.get_session() as session:
                    statement = (
                        select(MemoryEvent)
                        .order_by(desc(MemoryEvent.created_at))
                        .offset(offset)
                        .limit(page_size)
                    )
                    return [MemoryEvent.model_validate(item.model_dump()) for item in session.exec(statement).all()]

            offset = 0
            while True:
                events = await asyncio.to_thread(_load_events, offset)
                for event in events:
                    content = str(getattr(event, "narrative", "") or "").strip()
                    if not content:
                        continue
                    dedup_key = f"memory_event:{getattr(event, 'event_id', '')}"
                    existing = await self.v2_store.get_by_dedup_key(dedup_key, include_inactive=True)
                    if existing is not None:
                        continue
                    tags = []
                    try:
                        parsed_tags = json.loads(getattr(event, "tags", "") or "[]")
                        if isinstance(parsed_tags, list):
                            tags = [str(item) for item in parsed_tags]
                    except Exception:
                        logger.debug("[MemoryEngine] legacy memory event tags parse failed", exc_info=True)
                        tags = []
                    metadata = {
                        "legacy_event_id": getattr(event, "event_id", ""),
                        "emotion": getattr(event, "emotion", ""),
                        "reflection": getattr(event, "reflection", ""),
                        "memory_kind": getattr(event, "memory_kind", ""),
                        "source_layer": getattr(event, "source_layer", ""),
                    }
                    memory_id = await self.write_service.write(
                        MemoryWriteRequest(
                            source="memory_event",
                            kind=str(getattr(event, "memory_kind", "") or "event"),
                            session_id=str(getattr(event, "session_id", "") or getattr(event, "date", "") or ""),
                            content=content,
                            summary=content[:240],
                            tags=tags,
                            importance=max(0.1, min(1.0, float(getattr(event, "importance", 5) or 5) / 10.0)),
                            confidence=0.75,
                            metadata=metadata,
                            dedup_key=dedup_key,
                            source_ref=f"MemoryEvent:{getattr(event, 'event_id', '')}",
                        )
                    )
                    imported += int(bool(memory_id))
                    await self._startup_checkpoint()
                offset += len(events)
                if len(events) < page_size:
                    break
            await self.v2_store.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.v2_store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] MemoryEvent import degraded: {exc}")
        return imported

    async def import_legacy_jargons(self, *, limit: int = 1000) -> int:
        version = "2_jargon_import"
        if await self.v2_store.migration_applied(version):
            return 0
        db_service = getattr(self, "db_service", None)
        if not db_service or not hasattr(db_service, "get_session"):
            logger.warning("[MemoryV2] Jargon import deferred: db service unavailable")
            return 0
        imported = 0
        page_size = max(int(limit or 1000), 1)
        try:
            from ...infrastructure.persistence import Jargon
            from sqlmodel import desc, select

            def _load_jargons(offset: int):
                with db_service.get_session() as session:
                    statement = select(Jargon).order_by(desc(Jargon.updated_at)).offset(offset).limit(page_size)
                    return [Jargon.model_validate(item.model_dump()) for item in session.exec(statement).all()]

            offset = 0
            while True:
                rows = await asyncio.to_thread(_load_jargons, offset)
                for item in rows:
                    content = str(getattr(item, "content", "") or "").strip()
                    if not content:
                        continue
                    meaning = str(getattr(item, "meaning", "") or "").strip()
                    group_id = str(getattr(item, "group_id", "") or "")
                    dedup_key = jargon_fingerprint(content)
                    existing = await self.v2_store.get_by_dedup_key(dedup_key, include_inactive=True)
                    if existing is not None:
                        continue
                    status = "active" if bool(getattr(item, "is_jargon", False)) and bool(getattr(item, "is_complete", False)) and meaning else "review_pending"
                    memory_id = await self.write_service.write(
                        MemoryWriteRequest(
                            source="legacy_jargon",
                            kind="jargon",
                            session_id=GLOBAL_JARGON_SESSION_ID,
                            content=content,
                            summary=meaning or content,
                            importance=0.65,
                            confidence=0.75 if status == "active" else 0.55,
                            metadata={
                                "legacy_jargon_id": getattr(item, "id", None),
                                "raw_content": str(getattr(item, "raw_content", "") or content),
                                "meaning": meaning,
                                "count": int(getattr(item, "count", 1) or 1),
                                "review_status": status,
                                "source_groups": [group_id] if group_id else [],
                            },
                            dedup_key=dedup_key,
                            source_ref=f"Jargon:{getattr(item, 'id', '')}",
                            visibility="auto_and_tool" if status == "active" else "maintenance_only",
                            status=status,
                        )
                    )
                    imported += int(bool(memory_id))
                    await self._startup_checkpoint()
                offset += len(rows)
                if len(rows) < page_size:
                    break
            await self.v2_store.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.v2_store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] Jargon import degraded: {exc}")
        return imported

    async def import_legacy_expression_patterns(self, *, limit: int = 1000) -> int:
        version = "2_expression_pattern_import"
        if await self.v2_store.migration_applied(version):
            return 0
        db_service = getattr(self, "db_service", None)
        service = getattr(self, "expression_pattern_service", None)
        if not db_service or not hasattr(db_service, "get_session") or service is None:
            logger.warning("[MemoryV2] ExpressionPattern import deferred: dependency unavailable")
            return 0
        imported = 0
        page_size = max(int(limit or 1000), 1)
        try:
            from ...infrastructure.persistence import ExpressionPattern
            from sqlmodel import desc, select

            def _load_patterns(offset: int):
                with db_service.get_session() as session:
                    statement = (
                        select(ExpressionPattern)
                        .order_by(desc(ExpressionPattern.last_active_time))
                        .offset(offset)
                        .limit(page_size)
                    )
                    return [ExpressionPattern.model_validate(item.model_dump()) for item in session.exec(statement).all()]

            offset = 0
            while True:
                rows = await asyncio.to_thread(_load_patterns, offset)
                for item in rows:
                    expression = str(getattr(item, "expression", "") or "").strip()
                    situation = str(getattr(item, "situation", "") or "").strip()
                    if not expression or not situation:
                        continue
                    group_id = str(getattr(item, "group_id", "") or "")
                    shared_scope = group_id
                    dedup_key = service.build_dedup_key(group_id, situation, expression, shared_scope)
                    existing = await self.v2_store.get_by_dedup_key(dedup_key, include_inactive=True)
                    if existing is not None:
                        continue
                    memory_id = await service.write_pattern(
                        group_id,
                        {
                            "expression": expression,
                            "situation": situation,
                            "style": str(getattr(item, "style", "") or ""),
                            "content_samples": json.loads(getattr(item, "content_list", "[]") or "[]"),
                            "count": int(getattr(item, "count", 1) or 1),
                            "think_level": int(getattr(item, "think_level", 0) or 0),
                            "review_status": str(getattr(item, "review_status", "pending") or "pending"),
                            "review_reason": str(getattr(item, "review_reason", "") or ""),
                            "review_suggestion": str(getattr(item, "review_suggestion", "") or ""),
                            "weight": float(getattr(item, "weight", 1.0) or 1.0),
                            "shared_scope": shared_scope,
                            "legacy_pattern_id": getattr(item, "id", None),
                            "source_ref": f"ExpressionPattern:{getattr(item, 'id', '')}",
                            "summary": expression,
                        },
                        source="legacy_expression_pattern",
                    )
                    imported += int(bool(memory_id))
                    await self._startup_checkpoint()
                offset += len(rows)
                if len(rows) < page_size:
                    break
            await self.v2_store.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.v2_store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] ExpressionPattern import degraded: {exc}")
        return imported

    async def get_recent_memories(self, session_id: str, hours: int = 24) -> list:
        if not await self._ensure_faiss_initialized():
            return []

        recent_memories: list[str] = []
        cutoff_time = time.time() - (hours * 3600)
        try:
            columns = [row[1] for row in await self._run_documents_query("PRAGMA table_info(documents)", db_path=self.db_path)]
            if not columns:
                logger.warning("[Memory] documents table has no columns yet; skip recent memory lookup.")
                return []

            text_col = "page_content" if "page_content" in columns else ("content" if "content" in columns else "text")
            rows = await self._run_documents_query(
                f"""
                SELECT {text_col}
                FROM documents
                WHERE json_extract(metadata, '$.session_id') = ?
                  AND json_extract(metadata, '$.create_time') >= ?
                """,
                (session_id, cutoff_time),
                db_path=self.db_path,
            )
            for row in rows:
                if row and row[0]:
                    if self._is_cognitive_feedback_content(str(row[0])):
                        continue
                    recent_memories.append(row[0])
        except Exception as exc:
            logger.error(f"[Memory] get recent memories failed: {exc}")

        return recent_memories

    async def prune_low_importance(self, threshold: float = 0.2) -> int:
        return await self.maintenance_service.prune_low_importance(threshold=threshold)

    @staticmethod
    def _compute_text_similarity(text_a: str, text_b: str) -> float:
        def ngrams(text, n=2):
            return set(text[i:i + n] for i in range(len(text) - n + 1))

        a = ngrams(text_a)
        b = ngrams(text_b)
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    async def store_topic_results(self, topic_results: list, session_id: str, persona_id: str = None):
        for topic_result in topic_results:
            summary = str(topic_result.get("summary", "") or "").strip()
            if not summary or summary == "topic too short":
                continue

            normalized_summary = " ".join(summary.split()).lower()
            summary_digest = hashlib.sha256(normalized_summary.encode("utf-8")).hexdigest()[:24]
            topic_dedup_key = f"topic:{session_id}:{summary_digest}"
            existing_exact = await self.v2_store.get_by_dedup_key(topic_dedup_key, include_inactive=True)
            if existing_exact is not None:
                continue

            existing_results = await self.retrieval_service.retrieve(
                MemoryQuery(
                    query=summary,
                    session_id=str(session_id or ""),
                    persona_id=str(persona_id or ""),
                    layers=["memory", "topic", "event"],
                    top_k=1,
                    metadata={"visibility_mode": "tool"},
                )
            )
            merged = False
            if existing_results:
                existing_doc = existing_results[0]
                existing_text = existing_doc.content
                if self._compute_text_similarity(summary, existing_text) > 0.85:
                    merged_summary = f"{existing_text}\nSupplement: {summary}"
                    if len(merged_summary) > len(existing_text) * 2:
                        merged_summary = merged_summary[: len(existing_text) * 2] + "..."
                    new_id = await self.write_service.write(
                        MemoryWriteRequest(
                            source="topic_summarizer",
                            kind=existing_doc.kind or "topic",
                            session_id=str(session_id or ""),
                            persona_id=str(persona_id or ""),
                            content=merged_summary,
                            summary=merged_summary[:240],
                            tags=list(existing_doc.tags or []),
                            importance=0.85,
                            confidence=max(existing_doc.confidence, 0.6),
                            metadata={
                                "merged_from": [existing_doc.id],
                                "topic_result": dict(topic_result or {}),
                                "valid_until": time.time() + (30 * 86400),
                            },
                            dedup_key=(
                                f"topic_merged:{session_id}:"
                                f"{hashlib.sha256(merged_summary.encode('utf-8')).hexdigest()[:24]}"
                            ),
                            source_ref="summarizer.topic_merge",
                        )
                    )
                    if new_id:
                        await self.maintenance_service.mark_merged([existing_doc.id], superseded_by=new_id)
                    logger.info(f"[MemoryEngine] merged similar topic memory: {summary[:20]}...")
                    merged = True

            if not merged:
                importance = float(topic_result.get("importance", 0.4) or 0.4)
                try:
                    confidence = float(topic_result.get("confidence", 0.6) or 0.6)
                except (TypeError, ValueError):
                    confidence = 0.6
                confidence = max(0.45, min(0.75, confidence))
                await self.write_service.write(
                    MemoryWriteRequest(
                        source="topic_summarizer",
                        kind="topic",
                        session_id=str(session_id or ""),
                        persona_id=str(persona_id or ""),
                        content=str(summary or ""),
                        summary=str(summary or "")[:240],
                        tags=[str(item) for item in topic_result.get("topic_keywords", []) or []],
                        importance=importance,
                        confidence=confidence,
                        metadata={
                            "topic_result": dict(topic_result or {}),
                            "valid_until": time.time() + (14 * 86400),
                            "topic_scope": "session",
                        },
                        dedup_key=topic_dedup_key,
                        source_ref="summarizer.topic",
                    )
                )
