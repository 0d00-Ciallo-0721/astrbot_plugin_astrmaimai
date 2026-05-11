from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

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
    ACTIVE_FACADE = facade


def get_active_facade() -> Any:
    return ACTIVE_FACADE


@dataclass(slots=True)
class PluginApiAdapter:
    facade: Any = None
    config_path: str = default_config_path()
    schema_path: str = default_schema_path()
    persona_cache_path: str = default_persona_cache_path()

    def __post_init__(self) -> None:
        if self.facade is None:
            self.facade = get_active_facade()

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

    async def get_review_detail(self, pattern_id: int):
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
            "global_settings.webui_password",
            "memory.embedding_models",
        )
        return any(any(key.startswith(prefix) for prefix in reload_prefixes) for key in changed_keys)

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
        runtime_bound = False
        if self.facade and getattr(self.facade, "runtime", None):
            runtime = self.facade.runtime
            runtime.raw_config = dict(config_data)
            runtime.config = parsed_config
            if hasattr(runtime, "rebuild_infrastructure_settings"):
                runtime.rebuild_infrastructure_settings()
            runtime_bound = True

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

    def get_runtime(self) -> Any:
        return getattr(self.facade, "runtime", None) if self.facade else None

    async def read_persona_cache(self) -> dict[str, Any]:
        return self._read_json(self.persona_cache_path)

    async def write_persona_cache(self, data: dict[str, Any]) -> None:
        self._write_json(self.persona_cache_path, data)

    def get_webui_password(self) -> str:
        config = self._read_json(self.config_path)
        return config.get("global_settings", {}).get("webui_password", "astrmai_admin")


__all__ = ["PluginApiAdapter", "get_active_facade", "set_active_facade"]
