from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "ChatStateService": (".chat_state_service", "ChatStateService"),
    "StateEngine": (".chat_state_service", "StateEngine"),
    "UserProfileSummary": (".contracts", "UserProfileSummary"),
    "WaitStateSnapshot": (".contracts", "WaitStateSnapshot"),
    "FrequencyController": (".energy.frequency_controller", "FrequencyController"),
    "GroupReplyWaitManager": (".group_wait.group_reply_wait_manager", "GroupReplyWaitManager"),
    "GroupReplyWaitState": (".group_wait.group_reply_wait_manager", "GroupReplyWaitState"),
    "PrivateChatManager": (".private_chat.private_chat_manager", "PrivateChatManager"),
    "PrivateSession": (".private_chat.private_chat_manager", "PrivateSession"),
    "RelationshipEngine": (".relationship.relationship_engine", "RelationshipEngine"),
    "RelationshipEvent": (".relationship.relationship_engine", "RelationshipEvent"),
    "RelationshipVector": (".relationship.relationship_engine", "RelationshipVector"),
    "UserProfileService": (".user_profile_service", "UserProfileService"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
