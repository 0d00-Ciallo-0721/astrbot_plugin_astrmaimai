from __future__ import annotations

from typing import Any

from ..adapters.plugin_api import PluginApiAdapter


SHARD_LABELS: dict[str, str] = {
    "logic_style": "性格逻辑",
    "speech_style": "语言风格",
    "world_view": "世界观",
    "timeline": "生平经历",
    "relations": "人际关系",
    "skills": "技能能力",
    "values": "价值观",
    "secrets": "深层秘密",
}

class PersonaUiService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self.plugin_api = plugin_api

    async def get_persona_slices(self) -> dict[str, Any]:
        cache = await self.plugin_api.read_persona_cache()
        persona_id = self._resolve_persona_id(self.plugin_api)
        cache_key, payload = self._select_cache_payload(cache, persona_id)
        pending_tasks = self._pending_tasks(self.plugin_api)
        pending_task = cache_key in pending_tasks
        raw_text = str(payload.get("raw", "") or "")
        shards = payload.get("shards", {})
        if not isinstance(shards, dict):
            shards = {}
        return {
            "status": "ok",
            "data": {
                "persona_id": persona_id,
                "cache_key": cache_key,
                "cache_keys": list(cache.keys()) if isinstance(cache, dict) else [],
                "summary": str(payload.get("summary", "") or ""),
                "first_person_rewrite": str(payload.get("first_person_rewrite", "") or ""),
                "style": str(payload.get("style", "") or ""),
                "shards": {key: str(shards.get(key, "") or "") for key in SHARD_LABELS},
                "shard_labels": dict(SHARD_LABELS),
                "shard_order": list(SHARD_LABELS.keys()),
                "is_full_ready": bool(payload.get("is_full_ready", False)),
                "timestamp": payload.get("timestamp", 0.0),
                "pending_task": pending_task,
                "pending_task_keys": pending_tasks,
                "raw_length": len(raw_text),
                "self_lore": {
                    "available": self.plugin_api.get_memory_engine() is not None,
                    "persona_id": persona_id,
                    "source": "memory_engine.__self_lore__",
                },
            },
        }

    async def get_persona(self) -> dict[str, Any]:
        cache = await self.plugin_api.read_persona_cache()
        persona_id = self._resolve_persona_id(self.plugin_api)
        _, payload = self._select_cache_payload(cache, persona_id)
        return dict(payload)

    async def update_persona(self, data: dict) -> dict[str, Any]:
        cache = await self.plugin_api.read_persona_cache()
        persona_id = self._resolve_persona_id(self.plugin_api)
        cache_key, payload = self._select_cache_payload(cache, persona_id)
        updated_payload = dict(payload)
        updated_payload.update(dict(data or {}))

        updated_cache = dict(cache) if isinstance(cache, dict) else {}
        target_key = cache_key or persona_id or "global"
        updated_cache[target_key] = updated_payload
        await self.plugin_api.write_persona_cache(updated_cache)
        return updated_payload

    def _resolve_persona_id(self, plugin_api: Any) -> str:
        try:
            config = plugin_api.get_runtime_config()
            persona_id = getattr(getattr(config, "persona", None), "persona_id", "") if config else ""
            if persona_id:
                return str(persona_id)
        except Exception:
            pass
        try:
            config = self.plugin_api._read_json(self.plugin_api.config_path)
            persona_id = config.get("persona", {}).get("persona_id", "")
            if persona_id:
                return str(persona_id)
        except Exception:
            pass
        return "global"

    def _select_cache_payload(self, cache: dict[str, Any], persona_id: str) -> tuple[str, dict[str, Any]]:
        if not isinstance(cache, dict):
            return persona_id or "global", {}
        for key in (persona_id, "global"):
            if key and isinstance(cache.get(key), dict):
                return str(key), dict(cache[key])
        if any(key in cache for key in ("summary", "first_person_rewrite", "shards")):
            return persona_id or "global", dict(cache)
        for key, value in cache.items():
            if isinstance(value, dict):
                return str(key), dict(value)
        return persona_id or "global", {}

    @staticmethod
    def _pending_tasks(plugin_api: Any) -> list[str]:
        summarizer = plugin_api.get_persona_summarizer()
        pending = getattr(summarizer, "pending_tasks", {}) or {}
        keys: list[str] = []
        for key, task in pending.items():
            done = False
            try:
                done = bool(task.done())
            except Exception:
                done = False
            if not done:
                keys.append(str(key))
        return keys


__all__ = ["PersonaUiService"]
