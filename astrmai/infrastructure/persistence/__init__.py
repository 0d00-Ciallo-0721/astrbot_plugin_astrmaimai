from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "ChatRepository": (".repositories.chat_repository", "ChatRepository"),
    "CronSnapshot": (".orm_models", "CronSnapshot"),
    "DailyReflection": (".orm_models", "DailyReflection"),
    "DatabaseService": (".database_service", "DatabaseService"),
    "ExpressionPattern": (".orm_models", "ExpressionPattern"),
    "Jargon": (".orm_models", "Jargon"),
    "LastMessageMetadataDB": (".orm_models", "LastMessageMetadataDB"),
    "MemoryEvent": (".orm_models", "MemoryEvent"),
    "MemoryNode": (".orm_models", "MemoryNode"),
    "MemoryRepository": (".repositories.memory_repository", "MemoryRepository"),
    "MemoryRetrievalTrace": (".orm_models", "MemoryRetrievalTrace"),
    "MessageLog": (".orm_models", "MessageLog"),
    "PersistenceManager": (".persistence_manager", "PersistenceManager"),
    "ProfileRepository": (".repositories.profile_repository", "ProfileRepository"),
    "ReviewRepository": (".repositories.review_repository", "ReviewRepository"),
    "SocialRelation": (".orm_models", "SocialRelation"),
    "VisualAsset": (".orm_models", "VisualAsset"),
    "VisualMessageBinding": (".orm_models", "VisualMessageBinding"),
    "VisualMemory": (".orm_models", "VisualMemory"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:  # pragma: no cover
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
