"""Pure helpers adapted from astrbot_plugin_access_others_chat.

The upstream plugin is community-shared for secondary development.  These
helpers stay dependency-free so their truncation and privacy behaviour can be
tested without loading AstrBot.
"""

from __future__ import annotations

from typing import Any


def build_friend_umo(unified_msg_origin: str, sender_id: str) -> str:
    platform = str(unified_msg_origin or "default").split(":", 1)[0] or "default"
    return f"{platform}:FriendMessage:{str(sender_id or '').strip()}"


def extract_text_history(history: list[Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for message in history or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            continue
        text = " ".join(text.split())
        if text:
            result.append({"role": role, "content": text})
    return result


def render_context_block(messages: list[dict[str, str]], max_messages: int, max_chars: int) -> str:
    if not messages:
        return ""
    try:
        message_limit = max(1, min(int(max_messages), 50))
    except (TypeError, ValueError):
        message_limit = 10
    try:
        char_limit = max(100, min(int(max_chars), 12000))
    except (TypeError, ValueError):
        char_limit = 2000
    lines = [
        f"{'用户' if item.get('role') == 'user' else '你'}: {item.get('content', '')}"
        for item in messages[-message_limit:]
        if item.get("content")
    ]
    body = "\n".join(lines)
    if len(body) > char_limit:
        body = body[-char_limit:]
    return (
        "【跨会话历史参考】以下内容只用于回答用户明确提出的历史查询；"
        "不要主动复述、不要把它当成当前会话，也不要据此替用户建立新关系。\n"
        + body
    )
