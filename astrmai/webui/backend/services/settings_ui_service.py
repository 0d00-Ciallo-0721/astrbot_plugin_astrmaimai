from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter


class SettingsUiService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self.plugin_api = plugin_api

    async def get_config(self) -> dict:
        return await self.plugin_api.read_config()

    async def get_schema(self) -> dict:
        return await self.plugin_api.read_schema()

    @staticmethod
    def _section_items(section_schema: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(section_schema, dict):
            return {}
        return section_schema.get("items") or section_schema.get("keys") or {}

    @classmethod
    def _defaults_from_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        defaults: dict[str, Any] = {}
        for section, section_schema in (schema or {}).items():
            section_defaults: dict[str, Any] = {}
            for key, definition in cls._section_items(section_schema).items():
                if isinstance(definition, dict) and "default" in definition:
                    section_defaults[key] = deepcopy(definition["default"])
            defaults[section] = section_defaults
        return defaults

    @classmethod
    def _merge_effective_config(cls, schema: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        effective = cls._defaults_from_schema(schema)
        for section, values in (config or {}).items():
            if isinstance(values, dict):
                effective.setdefault(section, {})
                effective[section].update(values)
            else:
                effective[section] = values
        return effective

    @staticmethod
    def _type_matches(value: Any, expected_type: str) -> bool:
        if expected_type == "bool":
            return isinstance(value, bool)
        if expected_type == "int":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected_type == "float":
            return (isinstance(value, (int, float)) and not isinstance(value, bool))
        if expected_type in {"string", "text"}:
            return isinstance(value, str)
        if expected_type == "list":
            return isinstance(value, list)
        if expected_type in {"object", "dict", "template_list"}:
            return isinstance(value, (dict, list)) if expected_type == "template_list" else isinstance(value, dict)
        return True

    @classmethod
    def _validate_section(cls, schema: dict[str, Any], section: str, updates: dict[str, Any]) -> list[dict[str, str]]:
        if section not in schema:
            return [{"path": section, "message": "Unknown config section"}]
        if not isinstance(updates, dict):
            return [{"path": section, "message": "Section value must be an object"}]
        items = cls._section_items(schema.get(section, {}))
        errors: list[dict[str, str]] = []
        for key, value in updates.items():
            definition = items.get(key)
            if not isinstance(definition, dict):
                errors.append({"path": f"{section}.{key}", "message": "Unknown config field"})
                continue
            expected_type = str(definition.get("type", "") or "")
            if expected_type and not cls._type_matches(value, expected_type):
                errors.append({"path": f"{section}.{key}", "message": f"Expected {expected_type}"})
        return errors

    @classmethod
    def _changed_keys(cls, before: dict[str, Any], after: dict[str, Any]) -> set[str]:
        keys: set[str] = set()
        for section in set(before.keys()) | set(after.keys()):
            before_value = before.get(section, {})
            after_value = after.get(section, {})
            if isinstance(before_value, dict) and isinstance(after_value, dict):
                for key in set(before_value.keys()) | set(after_value.keys()):
                    if before_value.get(key) != after_value.get(key):
                        keys.add(f"{section}.{key}")
            elif before_value != after_value:
                keys.add(section)
        return keys

    async def get_effective_config(self) -> dict:
        return self._merge_effective_config(await self.get_schema(), await self.get_config())

    async def get_meta(self) -> dict:
        return await self.plugin_api.get_config_meta()

    async def update_section(self, section: str, updates: dict) -> dict:
        schema = await self.get_schema()
        errors = self._validate_section(schema, section, updates)
        if errors:
            return {"status": "error", "errors": errors}
        config = await self.get_config()
        before = deepcopy(config)
        config.setdefault(section, {})
        config[section].update(updates)
        await self.plugin_api.write_config(config)
        changed_keys = self._changed_keys(before, config)
        apply_result = await self.plugin_api.apply_config(config, changed_keys)
        return {
            "status": "ok",
            "changed": bool(changed_keys),
            "reload_required": bool(apply_result.get("reload_required", False)),
            "runtime_bound": bool(apply_result.get("runtime_bound", False)),
            "config": config.get(section, {}),
        }

    async def replace_config(self, data: dict[str, Any]) -> dict:
        schema = await self.get_schema()
        errors: list[dict[str, str]] = []
        if not isinstance(data, dict):
            errors.append({"path": "config", "message": "Config must be an object"})
        else:
            for section, updates in data.items():
                errors.extend(self._validate_section(schema, section, updates))
        if errors:
            return {"status": "error", "errors": errors}
        before = await self.get_config()
        await self.plugin_api.write_config(data)
        changed_keys = self._changed_keys(before, data)
        apply_result = await self.plugin_api.apply_config(data, changed_keys)
        return {
            "status": "ok",
            "changed": bool(changed_keys),
            "reload_required": bool(apply_result.get("reload_required", False)),
            "runtime_bound": bool(apply_result.get("runtime_bound", False)),
            "config": data,
        }

    async def reset_section(self, section: str) -> dict:
        schema = await self.get_schema()
        config = await self.get_config()
        section_schema = schema.get(section, {})
        defaults = {}
        for key, definition in self._section_items(section_schema).items():
            if "default" in definition:
                defaults[key] = deepcopy(definition["default"])
        before = deepcopy(config)
        config[section] = defaults
        await self.plugin_api.write_config(config)
        apply_result = await self.plugin_api.apply_config(config, self._changed_keys(before, config))
        return {
            "status": "ok",
            "data": defaults,
            "changed": before.get(section) != defaults,
            "reload_required": bool(apply_result.get("reload_required", False)),
            "runtime_bound": bool(apply_result.get("runtime_bound", False)),
        }

    async def reset_all(self) -> dict:
        schema = await self.get_schema()
        before = await self.get_config()
        defaults = self._defaults_from_schema(schema)
        await self.plugin_api.write_config(defaults)
        apply_result = await self.plugin_api.apply_config(defaults, self._changed_keys(before, defaults))
        return {
            "status": "ok",
            "data": defaults,
            "changed": before != defaults,
            "reload_required": bool(apply_result.get("reload_required", False)),
            "runtime_bound": bool(apply_result.get("runtime_bound", False)),
        }

    async def apply_config(self) -> dict:
        return await self.plugin_api.apply_config(await self.get_config())

    async def get_apply_status(self) -> dict:
        return await self.plugin_api.get_apply_status()

    async def get_persona_cache(self) -> dict:
        return await self.plugin_api.read_persona_cache()

    async def save_persona_cache(self, data: dict) -> dict:
        await self.plugin_api.write_persona_cache(data)
        return {"status": "ok"}


__all__ = ["SettingsUiService"]
