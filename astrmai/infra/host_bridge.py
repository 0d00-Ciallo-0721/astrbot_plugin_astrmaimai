from __future__ import annotations

from typing import Iterable


class HostBridge:
    GHOST_SENTINEL = "[ASTRMAI_GHOST_LOCK]"

    ERROR_KEYWORDS = (
        "请求失败",
        "错误类型",
        "错误信息",
        "调用失败",
        "处理失败",
        "描述失败",
        "获取模型列表失败",
        "api error",
        "all chat models failed",
        "connection error",
        "notfounderror",
    )

    def suppress_default_llm(self, event) -> str:
        event.call_llm = True
        return self.GHOST_SENTINEL

    def is_ghost_sentinel(self, message: str) -> bool:
        return isinstance(message, str) and self.GHOST_SENTINEL in message

    def should_intercept_error(self, message: str, enabled: bool = True) -> bool:
        if not enabled or not isinstance(message, str):
            return False
        lowered = message.lower()
        return any(keyword in lowered for keyword in self.ERROR_KEYWORDS)

    def build_admin_alert(self, event, message: str) -> str:
        chat_id = event.get_group_id() or event.get_sender_id()
        chat_type = "群聊" if event.get_group_id() else "私聊"
        user_name = event.get_sender_name() or "未知用户"
        return f"🚨 [AstrMai 错误告警]\n位置: {chat_type}({chat_id})\n触发者: {user_name}\n详情: {message}"

    def admin_targets(self, admin_ids: Iterable[str]) -> list[str]:
        return [str(admin_id) for admin_id in admin_ids if str(admin_id).strip()]
