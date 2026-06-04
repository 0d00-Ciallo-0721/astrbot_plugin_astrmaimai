from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ....app.runtime_facade_protocol import RuntimeFacadeProtocol

_logger = logging.getLogger(__name__)

from ..paths import default_config_path, default_persona_cache_path, default_schema_path

ACTIVE_FACADE: Any = None
APPLY_STATUS: dict[str, Any] = {
    "applied_at": 0.0,
    "status": "idle",
    "runtime_bound": False,
    "reload_required": False,
    "error": "",
}


def set_active_facade(facade: Any) -> None:
    global ACTIVE_FACADE
    previous = ACTIVE_FACADE
    if previous is not None and previous is not facade:
        _logger.warning(
            "ACTIVE_FACADE is being overwritten 鈥?old facade (%r) may leak resources. "
            "Attempting graceful termination of the previous facade.",
            previous,
        )
        try:
            term = getattr(previous, "terminate", None)
            if callable(term):
                import asyncio as _asyncio

                try:
                    loop = _asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None and loop.is_running():
                    loop.create_task(term())
                else:
                    _asyncio.run(term())
        except Exception as exc:
            _logger.warning("Failed to terminate previous ACTIVE_FACADE: %s", exc)
    ACTIVE_FACADE = facade


def get_active_facade() -> Any:
    return ACTIVE_FACADE


@dataclass(slots=True)
class PluginApiAdapter:
    facade: Optional[RuntimeFacadeProtocol] = None
    config_path: str = field(default_factory=default_config_path)
    schema_path: str = field(default_factory=default_schema_path)
    persona_cache_path: str = field(default_factory=default_persona_cache_path)

    def __post_init__(self) -> None:
        if self.facade is None:
            self.facade = get_active_facade()

    # 鈹€鈹€ internal helpers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def _read_json(self, path: str) -> dict[str, Any]:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    def _write_json(self, path: str, data: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)

    def _backup_json_file(self, path: str, *, keep: int = 5) -> str:
        if not os.path.exists(path):
            return ""
        timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
        backup_path = f"{path}.{timestamp}.bak"
        with open(path, "rb") as src, open(backup_path, "wb") as dst:
            dst.write(src.read())
        directory = os.path.dirname(path) or "."
        prefix = os.path.basename(path) + "."
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
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        backup_path = self._backup_json_file(path)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        os.replace(temp_path, path)
        return backup_path

    def _call_facade(self, method_name: str, *args: Any) -> Any:
        method = getattr(self.facade, method_name, None) if self.facade else None
        return method(*args) if callable(method) else None

    # 鈹€鈹€ narrow facade accessors (preferred over runtime passthrough) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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

    def get_runtime_config(self) -> Any:
        return self._call_facade("get_runtime_config")

    def get_persona_summarizer(self) -> Any:
        return self._call_facade("get_persona_summarizer")

    def get_state_engine(self) -> Any:
        return self._call_facade("get_state_engine")

    def get_auto_check_task(self) -> Any:
        return self._call_facade("get_auto_check_task")

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

    # 鈹€鈹€ memory sub-component accessors 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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

    # 鈹€鈹€ safe wrappers for private/internal access 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def candidate_to_dict(self, candidate: Any) -> dict[str, Any]:
        """瀹夊叏鍖呰 store._candidate_to_dict锛岄伩鍏嶇鏈夋柟娉曠┛閫?"""
        candidate_dict = self._call_facade("candidate_to_dict", candidate)
        if isinstance(candidate_dict, dict):
            return candidate_dict
        store = self.get_v2_store()
        if store and hasattr(store, "_candidate_to_dict"):
            return store._candidate_to_dict(candidate)
        return dict(candidate) if hasattr(candidate, "__dict__") else {}

    def format_timeline_item(self, item: Any) -> Any:
        """瀹夊叏鍖呰 observer.format_timeline_item"""
        formatter = getattr(self.facade, "format_timeline_item", None) if self.facade else None
        if callable(formatter):
            return formatter(item)
        observer = self.get_memory_observer()
        if observer:
            observer_formatter = getattr(observer, "format_timeline_item", None)
            if callable(observer_formatter):
                return observer_formatter(item)
        return item

    def get_expression_pattern_service(self) -> Any:
        """瀹夊叏鑾峰彇 expression_pattern_service"""
        service = self._call_facade("get_expression_pattern_service")
        if service is not None:
            return service
        engine = self.get_memory_engine()
        return getattr(engine, "expression_pattern_service", None) if engine else None

    # 鈹€鈹€ public API 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    async def get_runtime_diagnostics(self) -> dict[str, Any]:
        if self.facade:
            return self.facade.get_runtime_diagnostics()
        return {}

    async def get_capability_overview(self) -> dict[str, Any]:
        if self.facade and hasattr(self.facade, "get_capability_overview"):
            return await self.facade.get_capability_overview()
        return {}

    async def list_pending_reviews(self, group_id: str = "", limit: int = 50):
        if self.facade:
            return await self.facade.list_pending_expression_reviews(group_id=group_id, limit=limit)
        return []

    async def list_recent_reviews(self, group_id: str = "", limit: int = 50):
        if self.facade and hasattr(self.facade, "list_recent_expression_reviews"):
            return await self.facade.list_recent_expression_reviews(group_id=group_id, limit=limit)
        return []

    async def get_review_detail(self, pattern_id: str):
        if self.facade:
            return await self.facade.get_expression_review_detail(pattern_id)
        return None

    async def submit_review(self, **kwargs):
        if self.facade:
            return await self.facade.submit_expression_review(**kwargs)
        return {"status": "deferred"}

    async def read_config(self) -> dict[str, Any]:
        return self._read_json(self.config_path)

    async def write_config(self, data: dict[str, Any]) -> None:
        self._write_json_atomic(self.config_path, data)

    async def read_schema(self) -> dict[str, Any]:
        return self._read_json(self.schema_path)

    async def get_config_meta(self) -> dict[str, Any]:
        config_exists = os.path.exists(self.config_path)
        schema_exists = os.path.exists(self.schema_path)
        config_mtime = os.path.getmtime(self.config_path) if config_exists else 0.0
        schema_mtime = os.path.getmtime(self.schema_path) if schema_exists else 0.0
        return {
            "config_path": self.config_path,
            "schema_path": self.schema_path,
            "config_exists": config_exists,
            "schema_exists": schema_exists,
            "config_mtime": config_mtime,
            "schema_mtime": schema_mtime,
            "pending_apply": config_mtime > float(APPLY_STATUS.get("applied_at", 0.0) or 0.0),
            "apply_status": dict(APPLY_STATUS),
        }

    @staticmethod
    def _requires_reload(changed_keys: set[str]) -> bool:
        reload_prefixes = (
            "provider.",
            "vision.",
            "sys3.",
            "life.",
            "global_settings.webui_password",
            "memory.embedding_models",
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
        if not self.facade:
            return False
        if hasattr(self.facade, "apply_hot_config"):
            return bool(self.facade.apply_hot_config(config_dict, parsed_config))
        _logger.error(
            "_apply_hot_config: facade.apply_hot_config unavailable 鈥?cannot hot-apply config without facade support."
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
            APPLY_STATUS = {
                "applied_at": time.time(),
                "status": "error",
                "runtime_bound": self.facade is not None,
                "reload_required": False,
                "error": str(exc),
            }
            return {"status": "error", "errors": [{"path": "config", "message": str(exc)}], **APPLY_STATUS}

        reload_required = self._requires_reload(changed_keys or set())
        runtime_bound = self._apply_hot_config(config_data, parsed_config)

        APPLY_STATUS = {
            "applied_at": time.time(),
            "status": "ok",
            "runtime_bound": runtime_bound,
            "reload_required": reload_required,
            "error": "",
        }
        return {"status": "ok", "runtime_bound": runtime_bound, "reload_required": reload_required}

    async def get_apply_status(self) -> dict[str, Any]:
        return dict(APPLY_STATUS)

    async def read_persona_cache(self) -> dict[str, Any]:
        return self._read_json(self.persona_cache_path)

    async def write_persona_cache(self, data: dict[str, Any]) -> None:
        self._write_json(self.persona_cache_path, data)

    def get_webui_password(self) -> str:
        config = self._read_json(self.config_path)
        return config.get("global_settings", {}).get("webui_password", "") or ""


__all__ = [
    "PluginApiAdapter",
    "get_active_facade",
    "set_active_facade",
]
