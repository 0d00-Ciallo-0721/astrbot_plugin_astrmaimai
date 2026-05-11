from __future__ import annotations

from ...presentation.dto.message_scope import IngressDecision, MessageScope


def check_message_scope_access(runtime, scope: MessageScope) -> IngressDecision:
    whitelist_ids = getattr(runtime.config.global_settings, "whitelist_ids", [])
    admin_ids = getattr(runtime.config.global_settings, "admin_ids", [])
    enable_private_chat = getattr(runtime.config.global_settings, "enable_private_chat", False)

    is_admin = scope.entity_id in admin_ids or scope.sender_id in admin_ids
    is_whitelisted = (scope.umo in whitelist_ids) or (scope.entity_id in whitelist_ids) or is_admin

    if not is_whitelisted:
        if scope.platform_type == "GroupMessage":
            if whitelist_ids:
                return IngressDecision.stop("group_not_whitelisted")
        elif scope.platform_type == "FriendMessage":
            if not enable_private_chat and not is_admin:
                return IngressDecision.stop("private_chat_disabled")

    return IngressDecision.allow()
