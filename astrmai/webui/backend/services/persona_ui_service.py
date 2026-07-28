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

CORE_EDIT_FIELDS = ("summary", "first_person_rewrite", "style")
MAX_PERSONA_FIELD_CHARS = 12000

class PersonaUiService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self.plugin_api = plugin_api

    async def get_persona_slices(self) -> dict[str, Any]:
        cache = await self.plugin_api.read_persona_cache()
        persona_id = self._resolve_persona_id(self.plugin_api)
        cache_key, payload = self._select_cache_payload(cache, persona_id)
        pending_tasks = self._pending_tasks(self.plugin_api)
        pending_task = cache_key in pending_tasks
        summarizer = self.plugin_api.get_persona_summarizer()
        regeneration = (
            summarizer.get_regeneration_status(cache_key)
            if summarizer is not None and hasattr(summarizer, "get_regeneration_status")
            else {"state": "idle", "cache_key": cache_key}
        )
        raw_text = str(payload.get("raw", "") or "")
        shards = payload.get("shards", {})
        if not isinstance(shards, dict):
            shards = {}
        return {
            "status": "ok",
            "data": {
                "persona_id": persona_id,
                "cache_key": cache_key,
                "cache_keys": [
                    str(key)
                    for key in cache
                    if not str(key).startswith("__persona_regeneration__:")
                ] if isinstance(cache, dict) else [],
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
                "manual_overrides": dict(payload.get("manual_overrides", {}) or {}),
                "manual_revision": int(payload.get("manual_revision", 0) or 0),
                "manual_updated_at": float(payload.get("manual_updated_at", 0.0) or 0.0),
                "generated_baseline_available": isinstance(payload.get("generated_baseline"), dict),
                "derivation_version": int(payload.get("derivation_version", 0) or 0),
                "regeneration": regeneration,
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

    @staticmethod
    def _expected_timestamp(data: dict[str, Any]) -> float | None:
        value = data.get("expected_timestamp")
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("缓存时间戳格式无效，请重新读取页面") from exc

    @staticmethod
    def _clean_edit_value(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{label}必须是文本")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{label}不能为空")
        if len(cleaned) > MAX_PERSONA_FIELD_CHARS:
            raise ValueError(f"{label}不能超过 {MAX_PERSONA_FIELD_CHARS} 个字符")
        return cleaned

    def _normalize_manual_changes(self, data: dict[str, Any]) -> dict[str, Any]:
        allowed = {"cache_key", "expected_timestamp", *CORE_EDIT_FIELDS, "shards"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ValueError("包含不允许修改的字段：" + ", ".join(unknown))
        changes: dict[str, Any] = {}
        labels = {
            "summary": "核心摘要",
            "first_person_rewrite": "第一人称自觉",
            "style": "说话方式",
        }
        for field in CORE_EDIT_FIELDS:
            if field in data:
                changes[field] = self._clean_edit_value(data[field], labels[field])
        if "shards" in data:
            shards = data.get("shards")
            if not isinstance(shards, dict):
                raise ValueError("八维切片必须是对象")
            unknown_shards = sorted(set(shards) - set(SHARD_LABELS))
            if unknown_shards:
                raise ValueError("包含未知的切片：" + ", ".join(unknown_shards))
            changes["shards"] = {
                name: self._clean_edit_value(value, SHARD_LABELS[name])
                for name, value in shards.items()
            }
        if not changes or (set(changes) == {"shards"} and not changes["shards"]):
            raise ValueError("没有可保存的人格修改")
        return changes

    async def update_persona_slices(self, data: dict[str, Any]) -> dict[str, Any]:
        summarizer = self.plugin_api.get_persona_summarizer()
        if summarizer is None or not hasattr(summarizer, "apply_manual_overrides"):
            return {"status": "error", "code": "runtime_unavailable", "message": "人格运行时尚未就绪"}
        cache = await self.plugin_api.read_persona_cache()
        persona_id = self._resolve_persona_id(self.plugin_api)
        cache_key, _ = self._select_cache_payload(cache, persona_id)
        requested_key = str(data.get("cache_key", "") or cache_key).strip()
        if requested_key != cache_key:
            return {"status": "error", "code": "stale_cache_key", "message": "当前人格已经变化，请重新读取页面"}
        try:
            await summarizer.apply_manual_overrides(
                cache_key,
                self._normalize_manual_changes(dict(data or {})),
                expected_timestamp=self._expected_timestamp(data),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return {"status": "error", "code": "persona_update_failed", "message": str(exc)}
        return await self.get_persona_slices()

    async def restore_persona_slices(self, data: dict[str, Any]) -> dict[str, Any]:
        summarizer = self.plugin_api.get_persona_summarizer()
        if summarizer is None or not hasattr(summarizer, "restore_manual_overrides"):
            return {"status": "error", "code": "runtime_unavailable", "message": "人格运行时尚未就绪"}
        cache = await self.plugin_api.read_persona_cache()
        persona_id = self._resolve_persona_id(self.plugin_api)
        cache_key, _ = self._select_cache_payload(cache, persona_id)
        requested_key = str(data.get("cache_key", "") or cache_key).strip()
        if requested_key != cache_key:
            return {"status": "error", "code": "stale_cache_key", "message": "当前人格已经变化，请重新读取页面"}
        raw_fields = data.get("fields", [])
        if raw_fields is not None and not isinstance(raw_fields, list):
            return {"status": "error", "code": "invalid_fields", "message": "恢复字段必须是列表"}
        valid_fields = set(CORE_EDIT_FIELDS) | {f"shards.{name}" for name in SHARD_LABELS}
        fields = [str(item or "").strip() for item in (raw_fields or []) if str(item or "").strip()]
        unknown = sorted(set(fields) - valid_fields)
        if unknown:
            return {"status": "error", "code": "invalid_fields", "message": "包含未知的恢复字段：" + ", ".join(unknown)}
        try:
            await summarizer.restore_manual_overrides(
                cache_key,
                fields or None,
                expected_timestamp=self._expected_timestamp(data),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return {"status": "error", "code": "persona_restore_failed", "message": str(exc)}
        return await self.get_persona_slices()

    async def regenerate_persona_slices(self, data: dict[str, Any]) -> dict[str, Any]:
        summarizer = self.plugin_api.get_persona_summarizer()
        if summarizer is None or not hasattr(summarizer, "start_regeneration"):
            return {"status": "error", "code": "runtime_unavailable", "message": "人格运行时尚未就绪"}
        allowed = {
            "cache_key",
            "expected_timestamp",
            "clear_manual_overrides",
            "idempotency_key",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            return {
                "status": "error",
                "code": "invalid_fields",
                "message": "包含不允许的重建参数：" + ", ".join(unknown),
            }
        cache = await self.plugin_api.read_persona_cache()
        persona_id = self._resolve_persona_id(self.plugin_api)
        cache_key, _ = self._select_cache_payload(cache, persona_id)
        requested_key = str(data.get("cache_key", "") or cache_key).strip()
        if requested_key != cache_key:
            return {"status": "error", "code": "stale_cache_key", "message": "当前人格已经变化，请重新读取页面"}
        clear_manual_overrides = data.get("clear_manual_overrides", True)
        if not isinstance(clear_manual_overrides, bool):
            return {
                "status": "error",
                "code": "invalid_fields",
                "message": "清除人工微调标记必须是布尔值",
            }
        try:
            result = await summarizer.start_regeneration(
                cache_key,
                expected_timestamp=self._expected_timestamp(data),
                clear_manual_overrides=clear_manual_overrides,
                idempotency_key=str(data.get("idempotency_key", "") or ""),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return {"status": "error", "code": "persona_regeneration_failed", "message": str(exc)}
        return {"status": "ok", "data": result}

    async def get_persona_regeneration_status(self) -> dict[str, Any]:
        cache = await self.plugin_api.read_persona_cache()
        persona_id = self._resolve_persona_id(self.plugin_api)
        cache_key, _ = self._select_cache_payload(cache, persona_id)
        summarizer = self.plugin_api.get_persona_summarizer()
        if summarizer is None or not hasattr(summarizer, "get_regeneration_status"):
            return {"status": "ok", "data": {"state": "idle", "cache_key": cache_key}}
        return {"status": "ok", "data": summarizer.get_regeneration_status(cache_key)}

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
            if isinstance(value, dict) and not str(key).startswith("__persona_regeneration__:"):
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
