from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _tupleize(values: Any) -> tuple[str, ...]:
    if not values:
        return ()
    if isinstance(values, (list, tuple, set)):
        return tuple(str(item).strip() for item in values if str(item).strip())
    return ()


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    max_concurrent_llm_calls: int = 3
    llm_retries: int = 2
    backoff_factor: float = 1.5
    api_timeout: float = 15.0
    rate_limit_model_cooldown_sec: int = 120
    quota_model_cooldown_sec: int = 1800
    debug_mode: bool = False
    task_models: tuple[str, ...] = ()
    agent_models: tuple[str, ...] = ()
    fallback_models: tuple[str, ...] = ()
    vision_models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LaneRuntimeSettings:
    nicknames: tuple[str, ...] = ()
    debug_mode: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeFeatureFlags:
    work_mode_enabled: bool = False
    private_chat_enabled: bool = False
    vision_enabled: bool = True
    proactive_enabled: bool = True
    dream_visible: bool = False
    meme_enabled: bool = False
    dialogue_store_enabled: bool = True
    context_compaction_enabled: bool = True
    prefix_caching_enabled: bool = True


@dataclass(frozen=True, slots=True)
class InfrastructureSettings:
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    lane: LaneRuntimeSettings = field(default_factory=LaneRuntimeSettings)
    features: RuntimeFeatureFlags = field(default_factory=RuntimeFeatureFlags)


def build_infrastructure_settings(config: Any) -> InfrastructureSettings:
    provider = getattr(config, "provider", None)
    infra = getattr(config, "infra", None)
    global_settings = getattr(config, "global_settings", None)
    system1 = getattr(config, "system1", None)
    sys3 = getattr(config, "sys3", None)
    vision = getattr(config, "vision", None)
    life = getattr(config, "life", None)
    reply = getattr(config, "reply", None)

    return InfrastructureSettings(
        gateway=GatewaySettings(
            max_concurrent_llm_calls=int(getattr(infra, "max_concurrent_llm_calls", 3) or 3),
            llm_retries=int(getattr(infra, "llm_retries", 2)),
            backoff_factor=float(getattr(infra, "backoff_factor", 1.5) or 1.5),
            api_timeout=float(getattr(infra, "api_timeout", 15.0) or 15.0),
            rate_limit_model_cooldown_sec=int(getattr(infra, "rate_limit_model_cooldown_sec", 120) or 120),
            quota_model_cooldown_sec=int(getattr(infra, "quota_model_cooldown_sec", 1800) or 1800),
            debug_mode=bool(getattr(global_settings, "debug_mode", False)),
            task_models=_tupleize(getattr(provider, "task_models", ())),
            agent_models=_tupleize(getattr(provider, "agent_models", ())),
            fallback_models=_tupleize(getattr(provider, "fallback_models", ())),
            vision_models=_tupleize(getattr(provider, "vision_models", ())),
        ),
        lane=LaneRuntimeSettings(
            nicknames=_tupleize(getattr(system1, "nicknames", ())),
            debug_mode=bool(getattr(global_settings, "debug_mode", False)),
        ),
        features=RuntimeFeatureFlags(
            work_mode_enabled=bool(getattr(sys3, "enable_work_mode", False)),
            private_chat_enabled=bool(getattr(global_settings, "enable_private_chat", False)),
            vision_enabled=bool(getattr(vision, "enable_vision", True)),
            proactive_enabled=bool(getattr(life, "enable_proactive", True)),
            dream_visible=bool(getattr(life, "dream_visible", False)),
            meme_enabled=bool(int(getattr(reply, "meme_probability", 0) or 0) > 0),
            dialogue_store_enabled=bool(getattr(getattr(config, "conversation", None), "enable_dialogue_store", True)),
            context_compaction_enabled=bool(getattr(getattr(config, "conversation", None), "enable_context_compaction", True)),
            prefix_caching_enabled=bool(getattr(getattr(config, "conversation", None), "enable_prefix_caching", True)),
        ),
    )


__all__ = [
    "GatewaySettings",
    "InfrastructureSettings",
    "LaneRuntimeSettings",
    "RuntimeFeatureFlags",
    "build_infrastructure_settings",
]
