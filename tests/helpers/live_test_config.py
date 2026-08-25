from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST_CMD_CONFIG = Path(
    r"Z:\ai_robot\aibot\AstrBot-4.12.1\data\cmd_config.json"
)
DEFAULT_PLUGIN_CONFIG = Path(
    r"Z:\ai_robot\aibot\AstrBot-4.12.1\data\config\astrmai_config.json"
)
DEFAULT_SECRETS_FILE = Path(
    r"Z:\ai_robot\aibot\AstrBot-4.12.1\data\config\astrmai_live_secrets.json"
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _env_path(name: str, default: Path) -> Path:
    value = str(os.getenv(name, "") or "").strip()
    return Path(value) if value else default


def _load_secrets(path: Path) -> dict[str, Any]:
    """Load credentials separately from the model/provider configuration.

    The accepted shape is intentionally small and provider-oriented::

        {"providers": {"opencode": {"api_key": "..."}}}

    ``provider_keys`` is accepted as a compatibility shorthand. Missing files
    are treated as an empty secret store so existing environment/config based
    runs keep working during migration.
    """
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"live secrets file must contain a JSON object: {path}")
    return payload


def _secret_for_provider(secrets: dict[str, Any], provider_id: str) -> str:
    providers = secrets.get("providers", {})
    if not isinstance(providers, dict):
        providers = {}
    providers = {**providers, **(secrets.get("provider_keys", {}) if isinstance(secrets.get("provider_keys", {}), dict) else {})}
    value = providers.get(provider_id, "")
    if isinstance(value, dict):
        value = value.get("api_key", value.get("key", ""))
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and not (text.startswith("<") and text.endswith(">")):
            return text
    return ""


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


@dataclass(frozen=True)
class LiveLLMConfig:
    host_config_path: Path
    plugin_config_path: Path
    api_base: str
    configured_api_base: str
    api_key: str
    secrets_file: Path
    provider_id: str
    provider_family: str
    default_provider_id: str
    selected_model_id: str
    model_provider_source_id: str
    task_models: tuple[str, ...]
    agent_models: tuple[str, ...]
    fallback_models: tuple[str, ...]
    model_entries: dict[str, dict[str, Any]]
    infra_config: dict[str, Any]
    timing_config: dict[str, Any]
    configuration_status: str = "ok"
    configuration_errors: tuple[str, ...] = ()

    @property
    def all_models(self) -> list[str]:
        return _dedupe(list(self.task_models + self.agent_models + self.fallback_models))

    @property
    def default_model(self) -> str:
        configured = _first_non_empty(
            os.getenv("ASTRMAI_LIVE_MODEL"),
            os.getenv("LIVE_LLM_MODEL"),
        )
        return configured or (self.task_models[0] if self.task_models else self.all_models[0])

    def request_model(self, model_id: str) -> str:
        configured = self.model_entries.get(str(model_id or "").strip(), {})
        return _first_non_empty(configured.get("model"), str(model_id or "").split("/", 1)[-1])

    @property
    def endpoint_host(self) -> str:
        from urllib.parse import urlparse

        return str(urlparse(self.api_base).netloc or self.api_base)

    @property
    def configured_endpoint_host(self) -> str:
        from urllib.parse import urlparse

        return str(urlparse(self.configured_api_base).netloc or self.configured_api_base)

    @property
    def api_key_fingerprint(self) -> str:
        if not self.api_key:
            return ""
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:12]

    def public_summary(self) -> dict[str, Any]:
        infra = self.infra_config
        timing = self.timing_config
        gateway_timeout = infra.get("api_timeout")
        model_timeout = timing.get("model_request_timeout_sec", gateway_timeout)
        return {
            "host_config_path": str(self.host_config_path),
            "plugin_config_path": str(self.plugin_config_path),
            "api_base_host": self.endpoint_host,
            "configured_api_base_host": self.configured_endpoint_host,
            "api_base_override": self.api_base != self.configured_api_base,
            "provider_id": self.provider_id,
            "provider_family": self.provider_family,
            "default_provider_id": self.default_provider_id,
            "selected_model_id": self.selected_model_id,
            "model_provider_source_id": self.model_provider_source_id,
            "api_key_present": bool(self.api_key),
            "api_key_fingerprint": self.api_key_fingerprint,
            "secrets_file": str(self.secrets_file),
            "task_models": list(self.task_models),
            "agent_models": list(self.agent_models),
            "fallback_models": list(self.fallback_models),
            "default_model": self.default_model,
            "effective_gateway_timeout": gateway_timeout,
            "effective_model_request_timeout": model_timeout,
            "llm_retries": infra.get("llm_retries"),
            "backoff_factor": infra.get("backoff_factor"),
            "max_concurrent_llm_calls": infra.get("max_concurrent_llm_calls"),
            "semaphore_wait_timeout_sec": infra.get("semaphore_wait_timeout_sec"),
            "configuration_status": self.configuration_status,
            "configuration_errors": list(self.configuration_errors),
        }


def load_live_llm_config(*, require_key: bool = True, model_id: str | None = None) -> LiveLLMConfig:
    host_path = _env_path("ASTRMAI_HOST_CMD_CONFIG", DEFAULT_HOST_CMD_CONFIG)
    plugin_path = _env_path("ASTRMAI_PLUGIN_CONFIG", DEFAULT_PLUGIN_CONFIG)
    secrets_path = _env_path("ASTRMAI_LIVE_SECRETS_FILE", DEFAULT_SECRETS_FILE)
    secrets = _load_secrets(secrets_path)
    host_config = _load_json(host_path)
    plugin_config = _load_json(plugin_path)

    provider_config = dict(plugin_config.get("provider", {}) or {})
    infra_config = dict(plugin_config.get("infra", {}) or {})
    timing_config = dict(plugin_config.get("timing", {}) or {})
    task_models = tuple(_dedupe(list(provider_config.get("task_models", []) or [])))
    agent_models = tuple(_dedupe(list(provider_config.get("agent_models", []) or [])))
    fallback_models = tuple(_dedupe(list(provider_config.get("fallback_models", []) or [])))
    if not task_models and not agent_models and not fallback_models:
        raise RuntimeError("AstrMai provider config has no LLM models")

    sources = [item for item in host_config.get("provider_sources", []) or [] if item.get("enable", True)]
    source_by_id = {str(item.get("id", "") or "").strip(): item for item in sources}
    all_models = _dedupe(list(task_models + agent_models + fallback_models))
    requested_model = _first_non_empty(model_id, os.getenv("ASTRMAI_LIVE_MODEL"), os.getenv("LIVE_LLM_MODEL"), all_models[0])
    provider_entries = {
        str(item.get("id", "")).strip(): dict(item)
        for item in host_config.get("provider", []) or []
        if item.get("enable", True)
    }
    model_id = next(
        (
            entry_id
            for entry_id, item in provider_entries.items()
            if entry_id == requested_model or str(item.get("model", "")).strip() == requested_model
        ),
        requested_model,
    )
    model_entry = provider_entries.get(model_id, {})
    model_source_id = str(model_entry.get("provider_source_id", "") or "").strip()
    provider_settings = dict(host_config.get("provider_settings", {}) or {})
    default_provider_id = str(provider_settings.get("default_provider_id", "") or "").strip()
    default_source_id = default_provider_id.split("/", 1)[0] if default_provider_id else ""
    selected_source_id = model_source_id or default_source_id
    if not selected_source_id and sources:
        selected_source_id = str(sources[0].get("id", "") or "").strip()
    source = source_by_id.get(selected_source_id)
    errors: list[str] = []
    if not source:
        errors.append(f"missing_provider_source:{selected_source_id or 'unknown'}")
        source = sources[0] if sources else {}
    if default_source_id and selected_source_id != default_source_id:
        errors.append(f"default_provider_source_mismatch:{default_source_id}!={selected_source_id}")
    if model_source_id and selected_source_id != model_source_id:
        errors.append(f"model_provider_source_mismatch:{model_source_id}!={selected_source_id}")
    if provider_entries and not model_entry:
        errors.append(f"model_not_configured:{requested_model}")
    source_id = str(source.get("id", "") or "").strip()
    configured_api_base = str(source.get("api_base", "") or "").strip().rstrip("/")
    api_base_override = str(os.getenv("ASTRMAI_LIVE_BASE_URL", "") or "").strip().rstrip("/")
    api_base = _first_non_empty(api_base_override, configured_api_base)
    if api_base_override and configured_api_base and api_base_override != configured_api_base:
        errors.append("api_base_override_mismatch")
    keys = source.get("key", []) or []
    api_key = _first_non_empty(
        os.getenv("ASTRMAI_LIVE_API_KEY"),
        os.getenv("MAIN_REPLY_LIVE_API_KEY"),
        os.getenv("GROUP_TRACE_AUDIT_API_KEY"),
        _secret_for_provider(secrets, source_id if source else selected_source_id),
        keys[0] if keys else "",
    )
    if not api_base:
        raise RuntimeError("provider source is missing api_base")
    if require_key and not api_key:
        raise RuntimeError("provider source is missing api key")

    model_entries = {
        entry_id: item
        for entry_id, item in provider_entries.items()
        if str(item.get("provider_source_id", "")).strip() == source_id
    }
    return LiveLLMConfig(
        host_config_path=host_path,
        plugin_config_path=plugin_path,
        api_base=api_base.rstrip("/"),
        configured_api_base=configured_api_base,
        api_key=api_key,
        secrets_file=secrets_path,
        provider_id=source_id,
        provider_family=str(source.get("provider", "") or "").strip().lower(),
        default_provider_id=default_provider_id,
        selected_model_id=model_id,
        model_provider_source_id=model_source_id,
        task_models=task_models,
        agent_models=agent_models,
        fallback_models=fallback_models,
        model_entries=model_entries,
        infra_config=infra_config,
        timing_config=timing_config,
        configuration_status="configuration_mismatch" if errors else "ok",
        configuration_errors=tuple(errors),
    )


__all__ = [
    "DEFAULT_HOST_CMD_CONFIG",
    "DEFAULT_PLUGIN_CONFIG",
    "DEFAULT_SECRETS_FILE",
    "LiveLLMConfig",
    "load_live_llm_config",
]
