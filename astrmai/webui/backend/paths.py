from __future__ import annotations

import os
from pathlib import Path

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - only used outside AstrBot/stubbed tests
    def get_astrbot_data_path():
        return Path.cwd() / "data"


PLUGIN_ROOT = Path(__file__).resolve().parents[3]


def plugin_data_path(*parts: str) -> Path:
    return Path(get_astrbot_data_path()) / "plugin_data" / "astrmai" / Path(*parts)


def env_or_default_path(env_name: str, default_path: Path) -> str:
    return os.getenv(env_name, str(default_path))


def default_db_path() -> str:
    return env_or_default_path("ASTRMAI_DB_PATH", plugin_data_path("astrmai.db"))


def default_config_path() -> str:
    return env_or_default_path("ASTRMAI_CONFIG_PATH", plugin_data_path("config.json"))


def default_persona_cache_path() -> str:
    return env_or_default_path("ASTRMAI_PERSONA_CACHE_PATH", plugin_data_path("persona_cache.json"))


def default_schema_path() -> str:
    return env_or_default_path("ASTRMAI_CONF_SCHEMA_PATH", PLUGIN_ROOT / "_conf_schema.json")

