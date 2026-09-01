from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ....app.runtime_facade_protocol import RuntimeFacadeProtocol

_logger = logging.getLogger(__name__)

from ..paths import default_config_path, default_persona_cache_path, default_schema_path
from ....app.runtime_instance_coordinator import RUNTIME_INSTANCE_COORDINATOR

ACTIVE_FACADE: Any = None
_FACADE_UNSET = object()
APPLY_STATUS: dict[str, Any] = {
    "applied_at": 0.0,
    "status": "idle",
    "runtime_bound": False,
    "reload_required": False,
    "apply_mode": "IDLE",
    "error": "",
}
# ponytail: global lock for apply_config serialization — acceptable
_apply_lock = threading.Lock()


def set_active_facade(facade: Any) -> Any:
    global ACTIVE_FACADE
    registration = RUNTIME_INSTANCE_COORDINATOR.register_facade(facade)
    if ACTIVE_FACADE is not None and ACTIVE_FACADE is not facade:
        _logger.warning(
            "ACTIVE_FACADE replaced; previous facade termination is fenced before shared-resource startup."
        )
    ACTIVE_FACADE = facade
    return registration


def get_active_facade() -> Any:
    return ACTIVE_FACADE


@dataclass(slots=True)
class PluginApiAdapter:
    facade: Any = field(default=_FACADE_UNSET)
    config_path: str = field(default_factory=default_config_path)
    schema_path: str = field(default_factory=default_schema_path)
    persona_cache_path: str = field(default_factory=default_persona_cache_path)

    def _current_facade(self) -> Optional[RuntimeFacadeProtocol]:
        if self.facade is _FACADE_UNSET:
            return get_active_facade()
        return self.facade

    def has_bound_facade(self) -> bool:
        return self._current_facade() is not None

    # 閳光偓閳光偓 internal helpers 閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓

    @staticmethod
    def _contains_symlink(path: str) -> bool:
        """Check whether *path* or any of its parent components is a symbolic link.

        Returns ``True`` if a symlink is detected.  Silently stops at the
        first non-existent component (a missing file cannot be a symlink).
        Re-raises non-``FileNotFoundError`` OSErrors to avoid silently
        skipping paths hidden behind permission errors.
        """
        current = os.path.abspath(path)
        while True:
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                return False  # 文件/目录不存在，不可能为符号链接
            if stat.S_ISLNK(st.st_mode):
                return True
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return False

    def _validate_path(self, path: str) -> str:
        """Resolve and validate that *path* is within allowed directories.

        Returns the resolved real path.  Raises ``ValueError`` if the path
        escapes the plugin data or plugin root directories, or contains a
        symbolic link anywhere in its ancestry.
        """
        try:
            if self._contains_symlink(path):
                raise ValueError(f"Symbolic link detected in path: {path!r}")
        except OSError as exc:
            raise ValueError(f"Cannot verify path safety (lstat failed): {path!r} ({exc})") from exc
        resolved = os.path.realpath(path)
        data_root = os.path.realpath(os.path.dirname(self.config_path) or ".")
        plugin_root = os.path.realpath(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        allowed_roots = {data_root, plugin_root}
        resolved_norm = os.path.normpath(resolved)
        for root in allowed_roots:
            root_norm = os.path.normpath(root)
            if resolved_norm == root_norm or resolved_norm.startswith(root_norm + os.sep):
                return resolved
        raise ValueError(
            f"Path traversal detected: {path!r} resolves to {resolved!r}, "
            f"which is outside allowed directories"
        )

    def _read_json(self, path: str) -> dict[str, Any]:
        safe_path = self._validate_path(path)
        if not os.path.exists(safe_path):
            return {}
        with open(safe_path, "r", encoding="utf-8") as file:
            return json.load(file)

    def _read_schema_json(self, path: str) -> dict[str, Any]:
        safe_path = os.path.realpath(path)
        default_path = os.path.realpath(default_schema_path())
        if safe_path == default_path:
            if not os.path.exists(safe_path):
                return {}
            with open(safe_path, "r", encoding="utf-8") as file:
                return json.load(file)
        return self._read_json(path)

    def _write_json(self, path: str, data: dict[str, Any]) -> None:
        safe_path = self._validate_path(path)
        os.makedirs(os.path.dirname(safe_path) or ".", exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)

    def _backup_json_file(self, path: str, *, keep: int = 5) -> str:
        safe_path = self._validate_path(path)
        if not os.path.exists(safe_path):
            return ""
        timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
        backup_path = f"{safe_path}.{timestamp}.bak"
        with open(safe_path, "rb") as src, open(backup_path, "wb") as dst:
            dst.write(src.read())
        directory = os.path.dirname(safe_path) or "."
        prefix = os.path.basename(safe_path) + "."
        backups = sorted(
            [
                os.path.join(directory, name)
                for name in os.listdir(directory)
                if name.startswith(prefix) and name.endswith(".bak")
            ],
            key=lambda item: os.path.getmtime(item),
            reverse=True,
        )
        for old_path in backups[keep:]:
            try:
                os.remove(old_path)
            except OSError:
                pass
        return backup_path

    def _write_json_atomic(self, path: str, data: dict[str, Any]) -> str:
        safe_path = self._validate_path(path)
        os.makedirs(os.path.dirname(safe_path) or ".", exist_ok=True)
        backup_path = self._backup_json_file(safe_path)
        temp_path = f"{safe_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        os.replace(temp_path, safe_path)
        return backup_path

    def _call_facade(self, method_name: str, *args: Any) -> Any:
        facade = self._current_facade()
        method = getattr(facade, method_name, None) if facade else None
        if not callable(method):
            return None
        # ponytail: positional-only — keyword args silently dropped. Add **kwargs support if needed.
        return method(*args)

    # 閳光偓閳光偓 narrow facade accessors (preferred over runtime passthrough) 閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓

    def get_planner(self) -> Any:
        return self._call_facade("get_planner")

    def get_gateway(self) -> Any:
        return self._call_facade("get_gateway")

    def get_proactive_task(self) -> Any:
        return self._call_facade("get_proactive_task")

    def get_observability_hub(self) -> Any:
        return self._call_facade("get_observability_hub")

    def get_memory_engine(self) -> Any:
        return self._call_facade("get_memory_engine")

    def get_runtime_coordinator(self) -> Any:
        return self._call_facade("get_runtime_coordinator")

    def get_reflector(self) -> Any:
        return self._call_facade("get_reflector")

    def get_evolution(self) -> Any:
        evolution = self._call_facade("get_evolution")
        if evolution is not None:
            return evolution
        facade = self._current_facade()
        runtime = getattr(facade, "runtime", None) if facade is not None else None
        return getattr(runtime, "evolution", None) if runtime is not None else None

    def get_runtime_config(self) -> Any:
        return self._call_facade("get_runtime_config")

    def get_persona_summarizer(self) -> Any:
        return self._call_facade("get_persona_summarizer")

    def get_state_engine(self) -> Any:
        return self._call_facade("get_state_engine")

    def get_persistence(self) -> Any:
        persistence = self._call_facade("get_persistence")
        if persistence is not None:
            return persistence
        state_engine = self.get_state_engine()
        if state_engine is not None:
            persistence = getattr(state_engine, "persistence", None)
            if persistence is not None:
                return persistence
        memory_engine = self.get_memory_engine()
        return getattr(memory_engine, "persistence", None) if memory_engine is not None else None

    def get_auto_check_task(self) -> Any:
        return self._call_facade("get_auto_check_task")

    def get_expression_governance_runner(self) -> Any:
        return self._call_facade("get_expression_governance_runner")

    def get_reflect_tracker(self) -> Any:
        return self._call_facade("get_reflect_tracker")

    def get_chat_loop_kernel(self) -> Any:
        kernel = self._call_facade("get_chat_loop_kernel")
        if kernel is not None:
            return kernel
        task = self.get_proactive_task()
        return getattr(task, "chat_loop_kernel", None) if task else None

    def get_heartflow_manager(self) -> Any:
        manager = self._call_facade("get_heartflow_manager")
        if manager is not None:
            return manager
        task = self.get_proactive_task()
        return getattr(task, "heartflow_manager", None) if task else None

    def get_heartflow_topic_digest_service(self) -> Any:
        service = self._call_facade("get_heartflow_topic_digest_service")
        if service is not None:
            return service
        task = self.get_proactive_task()
        return getattr(task, "heartflow_topic_digest_service", None) if task else None

    # 閳光偓閳光偓 memory sub-component accessors 閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓

    def _memory_sub(self, attr: str) -> Any:
        engine = self.get_memory_engine()
        return getattr(engine, attr, None) if engine else None

    def get_v2_store(self) -> Any:
        store = self._call_facade("get_v2_store")
        return store if store is not None else self._memory_sub("v2_store")

    def get_memory_observer(self) -> Any:
        observer = self._call_facade("get_memory_observer")
        return observer if observer is not None else self._memory_sub("memory_observer")

    def get_memory_pipeline(self) -> Any:
        pipeline = self._call_facade("get_memory_pipeline")
        return pipeline if pipeline is not None else self._memory_sub("memory_pipeline")

    def get_maintenance_service(self) -> Any:
        maintenance = self._call_facade("get_maintenance_service")
        return maintenance if maintenance is not None else self._memory_sub("maintenance_service")

    def get_migration_service(self) -> Any:
        migration = self._call_facade("get_migration_service")
        return migration if migration is not None else self._memory_sub("migration_service")

    def get_index_projector(self) -> Any:
        projector = self._call_facade("get_index_projector")
        return projector if projector is not None else self._memory_sub("index_projector")

    def get_write_service(self) -> Any:
        writer = self._call_facade("get_write_service")
        return writer if writer is not None else self._memory_sub("write_service")

    def get_session_summarizer(self) -> Any:
        summarizer = self._call_facade("get_session_summarizer")
        return summarizer if summarizer is not None else self._memory_sub("session_summarizer")

    def get_instant_gate(self) -> Any:
        gate = self._call_facade("get_instant_gate")
        return gate if gate is not None else self._memory_sub("instant_gate")

    # 閳光偓閳光偓 safe wrappers for private/internal access 閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓

    def candidate_to_dict(self, candidate: Any) -> dict[str, Any]:
        """鐎瑰鍙忛崠鍛邦棅 store._candidate_to_dict閿涘矂浼╅崗宥囶潌閺堝鏌熷▔鏇犫敍闁?"""
        candidate_dict = self._call_facade("candidate_to_dict", candidate)
        if isinstance(candidate_dict, dict):
            return candidate_dict
        store = self.get_v2_store()
        if store and hasattr(store, "_candidate_to_dict"):
            return store._candidate_to_dict(candidate)
        return dict(candidate) if hasattr(candidate, "__dict__") else {}

    def format_timeline_item(self, item: Any) -> Any:
        """鐎瑰鍙忛崠鍛邦棅 observer.format_timeline_item"""
        facade = self._current_facade()
        formatter = getattr(facade, "format_timeline_item", None) if facade else None
        if callable(formatter):
            return formatter(item)
        observer = self.get_memory_observer()
        if observer:
            observer_formatter = getattr(observer, "format_timeline_item", None)
            if callable(observer_formatter):
                return observer_formatter(item)
        return item

    def get_expression_pattern_service(self) -> Any:
        """鐎瑰鍙忛懢宄板絿 expression_pattern_service"""
        service = self._call_facade("get_expression_pattern_service")
        if service is not None:
            return service
        engine = self.get_memory_engine()
        return getattr(engine, "expression_pattern_service", None) if engine else None

    # 閳光偓閳光偓 public API 閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓閳光偓

    async def get_runtime_diagnostics(self) -> dict[str, Any]:
        facade = self._current_facade()
        if facade and hasattr(facade, "get_runtime_diagnostics"):
            return facade.get_runtime_diagnostics() or {}
        return {}

    async def get_capability_overview(self) -> dict[str, Any]:
        facade = self._current_facade()
        if facade and hasattr(facade, "get_capability_overview"):
            return await facade.get_capability_overview()
        return {}

    async def list_pending_reviews(self, group_id: str = "", limit: int = 50):
        facade = self._current_facade()
        if facade and hasattr(facade, "list_pending_expression_reviews"):
            return await facade.list_pending_expression_reviews(group_id=group_id, limit=limit)
        return []

    async def list_recent_reviews(self, group_id: str = "", limit: int = 50):
        facade = self._current_facade()
        if facade and hasattr(facade, "list_recent_expression_reviews"):
            return await facade.list_recent_expression_reviews(group_id=group_id, limit=limit)
        return []

    async def get_review_detail(self, pattern_id: str):
        facade = self._current_facade()
        if facade and hasattr(facade, "get_expression_review_detail"):
            return await facade.get_expression_review_detail(pattern_id)
        return None

    async def submit_review(self, **kwargs):
        facade = self._current_facade()
        if facade and hasattr(facade, "submit_expression_review"):
            return await facade.submit_expression_review(**kwargs)
        return {"status": "deferred"}

    async def read_config(self) -> dict[str, Any]:
        return self._read_json(self.config_path)

    async def write_config(self, data: dict[str, Any]) -> None:
        self._write_json_atomic(self.config_path, data)

    async def read_schema(self) -> dict[str, Any]:
        return self._read_schema_json(self.schema_path)

    async def get_config_meta(self) -> dict[str, Any]:
        config_exists = os.path.exists(self.config_path)
        schema_exists = os.path.exists(self.schema_path)
        config_mtime = os.path.getmtime(self.config_path) if config_exists else 0.0
        schema_mtime = os.path.getmtime(self.schema_path) if schema_exists else 0.0
        with _apply_lock:
            apply_snapshot = dict(APPLY_STATUS)
        return {
            "config_path": self.config_path,
            "schema_path": self.schema_path,
            "config_exists": config_exists,
            "schema_exists": schema_exists,
            "config_mtime": config_mtime,
            "schema_mtime": schema_mtime,
            "pending_apply": config_mtime > float(apply_snapshot.get("applied_at", 0.0) or 0.0),
            "apply_status": apply_snapshot,
        }

    @staticmethod
    def _requires_reload(changed_keys: set[str]) -> bool:
        reload_prefixes = (
            "persona.",
            "performance.summary_threshold",
        )
        return any(any(key.startswith(prefix) for prefix in reload_prefixes) for key in changed_keys)

    def _apply_hot_config(self, config_dict: dict[str, Any], parsed_config: Any) -> bool:
        """Apply config to bound runtime via facade.  Returns True if applied.

        **Hot-config contract for new services**:
        The preferred path is ``facade.apply_hot_config(config_dict, parsed_config)``.
        When adding a new runtime service that needs live config updates, implement
        ``apply_hot_config`` on the service class itself (or on PluginFacade as a
        dispatcher) rather than adding if-blocks to a central ``_refresh_live_config_refs``.
        The old centralized method has been removed; each service owns its config refresh.
        """
        facade = self._current_facade()
        if not facade:
            return False
        if hasattr(facade, "apply_hot_config"):
            return bool(facade.apply_hot_config(config_dict, parsed_config))
        _logger.error(
            "_apply_hot_config: facade.apply_hot_config unavailable 閳?cannot hot-apply config without facade support."
        )
        raise RuntimeError(
            "Facade does not support apply_hot_config. "
            "A full restart is required to apply configuration changes."
        )

    async def apply_config(self, data: dict[str, Any] | None = None, changed_keys: set[str] | None = None) -> dict[str, Any]:
        global APPLY_STATUS
        config_data = data if data is not None else await self.read_config()
        try:
            from config import AstrMaiConfig

            parsed_config = AstrMaiConfig(**config_data)
        except Exception as exc:
            with _apply_lock:
                APPLY_STATUS = {
                    "applied_at": time.time(),
                    "status": "error",
                    "runtime_bound": self._current_facade() is not None,
                    "reload_required": False,
                    "apply_mode": "REJECTED",
                    "error": str(exc),
                }
            return {"status": "error", "errors": [{"path": "config", "message": str(exc)}], **APPLY_STATUS}

        facade_bound = self._current_facade() is not None
        try:
            runtime_bound = self._apply_hot_config(config_data, parsed_config)
        except Exception as exc:
            with _apply_lock:
                APPLY_STATUS = {
                    "applied_at": time.time(),
                    "status": "error",
                    "runtime_bound": facade_bound,
                    "reload_required": False,
                    "apply_mode": "REJECTED",
                    "error": str(exc),
                }
            return {"status": "error", "errors": [{"path": "config", "message": str(exc)}], **APPLY_STATUS}

        reload_required = not runtime_bound
        apply_mode = "HOT_APPLY" if runtime_bound else "FULL_RELOAD"

        with _apply_lock:
            APPLY_STATUS = {
                "applied_at": time.time(),
                "status": "ok",
                "runtime_bound": runtime_bound,
                "reload_required": reload_required,
                "apply_mode": apply_mode,
                "error": "",
            }
        return {
            "status": "ok",
            "runtime_bound": runtime_bound,
            "reload_required": reload_required,
            "apply_mode": apply_mode,
        }

    async def get_apply_status(self) -> dict[str, Any]:
        with _apply_lock:
            return dict(APPLY_STATUS)

    async def read_persona_cache(self) -> dict[str, Any]:
        persistence = self.get_persistence()
        if persistence is not None:
            loader = getattr(persistence, "load_persona_cache_async", None)
            if callable(loader):
                return dict(await loader() or {})
            loader = getattr(persistence, "load_persona_cache", None)
            if callable(loader):
                return dict(await asyncio.to_thread(loader) or {})
        return self._read_json(self.persona_cache_path)

    async def write_persona_cache(self, data: dict[str, Any]) -> None:
        persistence = self.get_persistence()
        summarizer = self.get_persona_summarizer()

        async def _persist_and_sync() -> None:
            if persistence is not None:
                saver = getattr(persistence, "save_persona_cache_async", None)
                if callable(saver):
                    await saver(data)
                else:
                    saver = getattr(persistence, "save_persona_cache", None)
                    if callable(saver):
                        await asyncio.to_thread(saver, data)
                    else:
                        self._write_json(self.persona_cache_path, data)
            else:
                self._write_json(self.persona_cache_path, data)
            if summarizer is not None and hasattr(summarizer, "cache"):
                summarizer.cache = dict(data)

        lock = getattr(summarizer, "_lock", None) if summarizer is not None else None
        if lock is not None and hasattr(lock, "__aenter__"):
            async with lock:
                await _persist_and_sync()
        else:
            await _persist_and_sync()

__all__ = [
    "PluginApiAdapter",
    "get_active_facade",
    "set_active_facade",
]
