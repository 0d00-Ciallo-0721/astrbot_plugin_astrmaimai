from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, Optional

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext
from sqlmodel import select

from ....infrastructure.persistence.orm_models import VisualAsset, VisualMessageBinding
from ....infrastructure.runtime.cross_session_handoff_store import CrossSessionHandoff
from ....presentation.dto.message_scope import MessageScope
from ...contracts.qq_action import PendingQQAction
from ..tool_contracts import TOOL_CAPABILITIES, record_tool_lifecycle
from ..tool_disclosure import TOOL_PACKAGES, normalize_requested_packages


QQ_MESSAGE_EMOJI_OPTIONS: dict[str, list[str]] = {
    "support": ["66", "124"],
    "approve": ["66", "76"],
    "laugh": ["63", "85"],
    "cute": ["124", "79"],
}


def _get_current_event(context: ContextWrapper[AstrAgentContext]):
    return context.context.event


def _get_pending_actions(event) -> list[dict[str, Any]]:
    pending_actions = event.get_extra("astrmai_pending_actions", [])
    if isinstance(pending_actions, list):
        return pending_actions
    return []


def _set_pending_actions(event, pending_actions: list[dict[str, Any]]) -> None:
    event.set_extra("astrmai_pending_actions", pending_actions)


def _record_tool_execution(event, tool_name: str, *, status: str = "success") -> None:
    trace = event.get_extra("astrmai_tool_execution_trace", [])
    trace = list(trace) if isinstance(trace, list) else []
    trace.append({"tool_name": str(tool_name or ""), "status": str(status or "success")})
    event.set_extra("astrmai_tool_execution_trace", trace[-32:])
    record_tool_lifecycle(
        event,
        tool_name,
        "tool_completed",
        source="model_tool_call",
        status=status,
    )


def _append_pending_action(event, action: dict[str, Any]) -> None:
    pending_actions = _get_pending_actions(event)
    pending_actions.append(action)
    _set_pending_actions(event, pending_actions)


def _append_qq_action(event, action: PendingQQAction) -> bool:
    action_data = action.to_dict()
    added = _append_once(
        event,
        matcher=lambda item: (
            str(item.get("action_type") or item.get("action") or "") == action.action_type
            and str(item.get("target_id") or "") == action.target_id
            and str(item.get("group_id") or "") == action.group_id
            and str(item.get("message_id") or "") == action.message_id
            and dict(item.get("payload") or {}) == action.payload
        ),
        action=action_data,
        record_execution=False,
    )
    if added:
        tool_name = {
            "poke": "proactive_poke",
            "message_emoji_like": "message_emoji_like_action",
            "group_sign": "group_sign_action",
            "withdraw": "regret_and_withdraw_action",
            "custom_face_send": "qq_custom_face_send_tool",
            "quote_reply": "quote_reply_action",
        }.get(action.action_type, action.action_type)
        record_tool_lifecycle(
            event,
            tool_name,
            "action_queued",
            status="pending",
        )
    return added


def _append_once(event, *, matcher, action: dict[str, Any], record_execution: bool = True) -> bool:
    pending_actions = _get_pending_actions(event)
    if any(matcher(item) for item in pending_actions):
        return False
    pending_actions.append(action)
    _set_pending_actions(event, pending_actions)
    tool_name = {
        "at": "construct_at_event",
        "meme": "proactive_meme",
        "terminal_reread": "meme_resonance_action",
        "withdraw": "regret_and_withdraw_action",
    }.get(str(action.get("action") or ""), "")
    if tool_name and record_execution:
        _record_tool_execution(event, tool_name)
    return True


def _current_message_id(event) -> str:
    message_obj = getattr(event, "message_obj", None)
    return str(getattr(message_obj, "message_id", "") or "").strip()


def _message_id_binding_error(event, requested_message_id: str) -> str:
    requested = str(requested_message_id or "").strip()
    if not requested:
        return ""
    current = _current_message_id(event)
    if current and requested == current:
        return ""

    identity_ids: set[str] = set()
    for getter_name in ("get_sender_id", "get_self_id", "get_group_id"):
        getter = getattr(event, getter_name, None)
        if not callable(getter):
            continue
        try:
            value = str(getter() or "").strip()
        except Exception:
            value = ""
        if value:
            identity_ids.add(value)
    if hasattr(event, "get_extra"):
        for key in (
            "astrmai_interaction_actor_id",
            "astrmai_interaction_target_id",
            "astrmai_at_action_target_id",
        ):
            value = str(event.get_extra(key, "") or "").strip()
            if value:
                identity_ids.add(value)
        bound_ids = event.get_extra("astrmai_bound_message_ids", []) or []
        if isinstance(bound_ids, (list, tuple, set)) and requested in {
            str(item or "").strip() for item in bound_ids
        }:
            return ""
        for key in (
            "astrmai_reply_message_id",
            "astrmai_quote_message_id",
            "astrmai_referenced_message_id",
        ):
            if requested == str(event.get_extra(key, "") or "").strip():
                return ""

    current_text = str(
        event.get_extra("astrmai_rich_text", getattr(event, "message_str", ""))
        if hasattr(event, "get_extra")
        else getattr(event, "message_str", "")
    ).strip()
    explicitly_requested = bool(
        requested
        and requested in current_text
        and re.search(r"(?:消息|message|msg|编号|ID|id)", current_text)
    )
    if explicitly_requested and requested not in identity_ids:
        return ""

    reason = "identity_id" if requested in identity_ids else "not_bound_to_current_turn"
    return (
        "参数错误[message_id_not_bound]："
        f"{requested} 未绑定到当前消息（reason={reason}）。"
        "不要把 QQ 号、群号或模型猜测的数字当作消息 ID；"
        "分析当前图片/表情包时请留空 message_id 后重新调用。"
    )


def _history_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            payload = data.get("messages", [])
        else:
            payload = data
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


async def _resolve_latest_bot_message_id(event) -> str:
    client = getattr(event, "bot", None)
    api = getattr(client, "api", None)
    if api is None:
        return ""
    group_id = str(event.get_group_id() or "").strip()
    sender_id = str(event.get_sender_id() or "").strip()
    self_id = str(event.get_self_id() or "").strip()
    try:
        if group_id:
            payload = await api.call_action(
                "get_group_msg_history",
                group_id=int(group_id) if group_id.isdigit() else group_id,
                message_seq=0,
                count=20,
            )
        elif sender_id:
            payload = await api.call_action(
                "get_friend_msg_history",
                user_id=int(sender_id) if sender_id.isdigit() else sender_id,
                message_seq=0,
                count=20,
            )
        else:
            return ""
    except Exception as exc:
        logger.warning(f"[RegretAndWithdrawTool] message history lookup failed: {exc}")
        return ""

    for message in reversed(_history_messages(payload)):
        message_sender = message.get("sender")
        author_id = message.get("user_id")
        if isinstance(message_sender, dict):
            author_id = message_sender.get("user_id", author_id)
        message_id = str(message.get("message_id") or "").strip()
        if message_id and str(author_id or "").strip() == self_id:
            return message_id
    return ""


async def _resolve_target(
    db_service: Any,
    *,
    target_name: str,
    current_event,
    astr_ctx,
) -> Optional[tuple[str, Optional[str]]]:
    if not db_service or not hasattr(db_service, "resolve_entity_spatio_temporal"):
        return None
    result = await db_service.resolve_entity_spatio_temporal(
        target_name=target_name,
        current_event=current_event,
        astr_ctx=astr_ctx,
    )
    if not result:
        return None
    target_id, group_id = result
    return str(target_id), str(group_id) if group_id is not None else None


def _friend_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("data", payload)
        if isinstance(payload, dict):
            direct = payload.get("friends", payload.get("list", None))
            if isinstance(direct, list):
                payload = direct
            else:
                categories = payload.get("categories", payload.get("category", []))
                if isinstance(categories, list):
                    flattened: list[dict[str, Any]] = []
                    for category in categories:
                        if not isinstance(category, dict):
                            continue
                        for item in category.get("friends", category.get("list", [])) or []:
                            if isinstance(item, dict):
                                flattened.append(item)
                    payload = flattened
                else:
                    payload = []
    if isinstance(payload, list) and any(isinstance(item, dict) and ("friends" in item or "list" in item) for item in payload):
        flattened = []
        for item in payload:
            if isinstance(item, dict) and ("friends" in item or "list" in item):
                flattened.extend(entry for entry in item.get("friends", item.get("list", [])) or [] if isinstance(entry, dict))
            elif isinstance(item, dict):
                flattened.append(item)
        payload = flattened
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _payload_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _list_entries(payload: Any, *keys: str) -> list[dict[str, Any]]:
    payload = _payload_data(payload)
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                payload = value
                break
        else:
            payload = []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _entry_id(entry: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = entry.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _friend_id(entry: dict[str, Any]) -> str:
    return _entry_id(entry, "user_id", "uin", "qq", "id")


def _friend_name(entry: dict[str, Any]) -> str:
    return (
        str(entry.get("remark") or "").strip()
        or str(entry.get("nickname") or "").strip()
        or str(entry.get("name") or "").strip()
        or _friend_id(entry)
    )


def _group_id(entry: dict[str, Any]) -> str:
    return _entry_id(entry, "group_id", "gid", "id")


def _group_name(entry: dict[str, Any]) -> str:
    return (
        str(entry.get("group_name") or "").strip()
        or str(entry.get("group_memo") or "").strip()
        or str(entry.get("name") or "").strip()
        or _group_id(entry)
    )


def _match_by_id_or_name(
    entries: list[dict[str, Any]],
    target: str,
    *,
    id_getter,
    name_getter,
) -> list[dict[str, Any]]:
    clean_target = str(target or "").strip().lstrip("@")
    if not clean_target:
        return []
    if clean_target.isdigit():
        exact = [entry for entry in entries if id_getter(entry) == clean_target]
        if exact:
            return exact
    lowered = clean_target.casefold()
    exact_name = [
        entry
        for entry in entries
        if lowered
        in {
            str(name_getter(entry) or "").strip().casefold(),
            str(entry.get("nickname") or "").strip().casefold(),
            str(entry.get("remark") or "").strip().casefold(),
            str(entry.get("group_name") or "").strip().casefold(),
            str(entry.get("name") or "").strip().casefold(),
        }
    ]
    if exact_name:
        return exact_name
    return [
        entry
        for entry in entries
        if lowered in str(name_getter(entry) or "").strip().casefold()
    ]


def _truncate_text(text: Any, limit: int = 160) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 3:
        return cleaned[:limit]
    return cleaned[: limit - 3].rstrip() + "..."


def _segment_type(segment: Any) -> str:
    if isinstance(segment, dict):
        return str(segment.get("type") or segment.get("message_type") or "").strip()
    name = segment.__class__.__name__ if segment is not None else ""
    return name or "unknown"


def _segment_data(segment: Any) -> dict[str, Any]:
    if isinstance(segment, dict):
        data = segment.get("data", segment)
        return dict(data) if isinstance(data, dict) else {}
    data: dict[str, Any] = {}
    for key in ("text", "file", "url", "path", "file_id", "file_unique", "name", "summary"):
        if hasattr(segment, key):
            value = getattr(segment, key)
            if value:
                data[key] = value
    return data


def _message_segments(payload: Any) -> list[Any]:
    payload = _payload_data(payload)
    if isinstance(payload, dict):
        payload = payload.get("message", payload.get("raw_message", payload.get("segments", payload)))
    if isinstance(payload, str):
        return [{"type": "text", "data": {"text": payload}}]
    if isinstance(payload, list):
        return payload
    return []


def _coerce_api_id(value: Any) -> Any:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else text


def _message_sender_id(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    value = message.get("user_id")
    if isinstance(sender, dict):
        value = sender.get("user_id", sender.get("uin", value))
    return str(value or "").strip()


def _message_sender_name(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    if isinstance(sender, dict):
        return str(sender.get("card") or sender.get("nickname") or sender.get("name") or "").strip()
    return ""


def _message_text_preview(message: dict[str, Any], limit: int = 120) -> str:
    texts: list[str] = []
    for segment in _message_segments(message):
        if _segment_type(segment).lower() in {"text", "plain"}:
            text = str(_segment_data(segment).get("text") or "").strip()
            if text:
                texts.append(text)
    if not texts:
        raw = message.get("raw_message")
        if isinstance(raw, str):
            texts.append(raw)
    return _truncate_text(" ".join(texts), limit)


def _format_message_summary(message: dict[str, Any], index: int = 0) -> str:
    message_id = str(message.get("message_id") or message.get("id") or "").strip()
    sender_id = _message_sender_id(message)
    sender_name = _message_sender_name(message)
    text = _message_text_preview(message, 96)
    prefix = f"{index}. " if index else ""
    who = sender_name or sender_id or "unknown"
    suffix = f" | {text}" if text else ""
    return f"{prefix}msg={message_id or 'unknown'} sender={who}({sender_id or 'unknown'}){suffix}"


def _group_member_id(entry: dict[str, Any]) -> str:
    return _entry_id(entry, "user_id", "uin", "qq", "id")


def _group_member_name(entry: dict[str, Any]) -> str:
    return (
        str(entry.get("card") or "").strip()
        or str(entry.get("nickname") or "").strip()
        or str(entry.get("name") or "").strip()
        or _group_member_id(entry)
    )


def _exact_group_member_matches(entries: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    clean_target = str(target or "").strip().lstrip("@").casefold()
    if not clean_target:
        return []
    return [
        entry
        for entry in entries
        if clean_target
        in {
            str(entry.get("card") or "").strip().casefold(),
            str(entry.get("nickname") or "").strip().casefold(),
            str(entry.get("name") or "").strip().casefold(),
            _group_member_id(entry).casefold(),
        }
    ]


def _visual_records_from_event(event) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for key in (
        "astrmai_vision_records",
        "astrmai_vision_context",
        "astrmai_visual_context",
        "astrmai_visual_descriptions",
        "astrmai_vision_descriptions",
        "astrmai_image_descriptions",
        "astrmai_image_context",
    ):
        value = event.get_extra(key, None) if hasattr(event, "get_extra") else None
        if isinstance(value, list):
            items = value
        elif isinstance(value, dict):
            items = [value]
        elif isinstance(value, str) and value.strip():
            items = [{"type": "image", "description": value.strip(), "emotion_tags": []}]
        else:
            items = []
        for item in items:
            if isinstance(item, dict):
                records.append(item)
            elif isinstance(item, str) and item.strip():
                records.append({"type": "image", "description": item.strip(), "emotion_tags": []})
    return records


def _extract_image_candidates(payload: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for segment in _message_segments(payload):
        seg_type = _segment_type(segment).lower()
        if seg_type not in {"image", "mface", "face", "emoji"}:
            continue
        data = _segment_data(segment)
        candidates.append(
            {
                "type": seg_type,
                "file": str(data.get("file") or data.get("file_id") or data.get("id") or "").strip(),
                "url": str(data.get("url") or "").strip(),
                "path": str(data.get("path") or data.get("file") or "").strip(),
                "raw": data,
            }
        )
    return candidates


def _append_report_extra(event, key: str, report: dict[str, Any], *, max_items: int = 32) -> None:
    reports = event.get_extra(key, []) if hasattr(event, "get_extra") else []
    reports = list(reports) if isinstance(reports, list) else []
    reports.append(report)
    if hasattr(event, "set_extra"):
        event.set_extra(key, reports[-max_items:])


async def _resolve_friend_target(
    event,
    astr_ctx,
    db_service: Any,
    target_name: str,
) -> tuple[Optional[tuple[str, str]], str]:
    client = getattr(event, "bot", None)
    api = getattr(client, "api", None)
    if api is None:
        return None, "NapCat 好友接口不可用"
    try:
        friends = _friend_entries(await api.call_action("get_friend_list"))
    except Exception as exc:
        logger.warning(f"[SpaceTransitionTool] friend list lookup failed: {exc}")
        return None, f"读取机器人好友列表失败：{exc}"
    if not friends:
        return None, "机器人好友列表为空"

    clean_target = str(target_name or "").strip().lstrip("@")
    candidate_id = clean_target if clean_target.isdigit() else ""
    matches: list[dict[str, Any]] = []
    if not candidate_id:
        lowered = clean_target.casefold()
        matches = [
            item
            for item in friends
            if lowered
            in {
                str(item.get("nickname") or "").strip().casefold(),
                str(item.get("remark") or "").strip().casefold(),
            }
        ]
    if not candidate_id and not matches:
        resolved = await _resolve_target(
            db_service,
            target_name=clean_target,
            current_event=event,
            astr_ctx=astr_ctx,
        )
        if resolved:
            candidate_id = str(resolved[0] or "").strip()

    if candidate_id:
        matches = [
            item
            for item in friends
            if str(item.get("user_id") or item.get("uin") or "").strip() == candidate_id
        ]
    if not matches:
        return None, f"机器人好友列表中没有找到“{clean_target}”"
    unique_ids = {
        str(item.get("user_id") or item.get("uin") or "").strip()
        for item in matches
        if str(item.get("user_id") or item.get("uin") or "").strip()
    }
    if len(unique_ids) != 1:
        return None, f"好友“{clean_target}”匹配到多个账号，请改用准确 QQ 号"
    target_id = next(iter(unique_ids))
    friend = next(
        item
        for item in matches
        if str(item.get("user_id") or item.get("uin") or "").strip() == target_id
    )
    display_name = str(friend.get("remark") or friend.get("nickname") or clean_target or target_id).strip()
    return (target_id, display_name), ""


async def prepare_explicit_tool_fallbacks(
    event,
    required_tools: list[str],
    *,
    emotion_mapping: Optional[list[str]] = None,
) -> list[str]:
    """Queue unambiguous explicit actions without relying on model tool selection."""
    required = set(required_tools or [])
    queued: list[str] = []
    message = str(getattr(event, "message_str", "") or "").strip().lower()
    group_id = str(event.get_group_id() or "").strip()

    if "proactive_poke" in required:
        self_targeted = any(
            marker in message
            for marker in ("戳一下我", "戳一戳我", "戳戳我", "戳我", "poke me")
        ) or message.rstrip("。！？!~～ ").endswith(("戳一下", "戳一戳", "戳戳", "poke"))
        if self_targeted:
            target_id = str(event.get_sender_id() or "").strip()
            if target_id and target_id != str(event.get_self_id() or "").strip():
                if _append_qq_action(
                    event,
                    PendingQQAction(
                        action_type="poke",
                        target_id=target_id,
                        target_name=str(event.get_sender_name() or "当前用户"),
                        group_id=group_id,
                    ),
                ):
                    queued.append("proactive_poke")

    if "message_emoji_like_action" in required:
        message_id = _current_message_id(event)
        pools = [item for values in QQ_MESSAGE_EMOJI_OPTIONS.values() for item in values]
        if message_id and pools:
            if _append_qq_action(
                event,
                PendingQQAction(
                    action_type="message_emoji_like",
                    message_id=message_id,
                    payload={"emoji_id": str(random.choice(pools)), "tone": "explicit"},
                ),
            ):
                queued.append("message_emoji_like_action")

    if "group_sign_action" in required and group_id:
        if _append_qq_action(
            event,
            PendingQQAction(action_type="group_sign", group_id=group_id),
        ):
            queued.append("group_sign_action")

    if "regret_and_withdraw_action" in required:
        message_id = await _resolve_latest_bot_message_id(event)
        if message_id:
            if _append_qq_action(
                event,
                PendingQQAction(action_type="withdraw", message_id=message_id),
            ):
                queued.append("regret_and_withdraw_action")

    if "proactive_meme" in required:
        tag_hints: list[tuple[str, list[str]]] = []
        for item in emotion_mapping or []:
            if not str(item or "").strip():
                continue
            tag, _, hints = str(item).partition(":")
            tag_hints.append(
                (
                    tag.strip().lower(),
                    [
                        part.strip().lower()
                        for part in hints.replace("，", ",").replace("、", ",").split(",")
                        if part.strip()
                    ],
                )
            )
        valid_tags = [tag for tag, _ in tag_hints if tag]
        mood_tag = str(event.get_extra("astrmai_mood_tag", "") or "").strip().lower()
        requested_tag = next(
            (tag for tag, hints in tag_hints if tag in message or any(hint in message for hint in hints)),
            "",
        )
        emotion_tag = requested_tag or (mood_tag if mood_tag in valid_tags else (
            "neutral" if "neutral" in valid_tags else (valid_tags[0] if valid_tags else "neutral")
        ))
        event.set_extra("astrmai_bypass_mood_analysis", emotion_tag)
        event.set_extra("astrmai_force_meme", True)
        if _append_once(
            event,
            matcher=lambda item: item.get("action") == "meme",
            action={"action": "meme", "tag": emotion_tag},
            record_execution=False,
        ):
            record_tool_lifecycle(
                event,
                "proactive_meme",
                "action_queued",
                status="pending",
            )
            queued.append("proactive_meme")

    for tool_name in queued:
        record_tool_lifecycle(
            event,
            tool_name,
            "explicit_fallback_prepared",
            source="explicit_router",
            status="queued",
        )
    return queued


@dataclass
class WaitTool(FunctionTool[AstrAgentContext]):
    name: str = "wait_and_listen"
    description: str = (
        "当你判断对方还没说完，或者当前更适合等待下一条消息时调用。"
        "调用后你最终必须只输出 [SYSTEM_WAIT_SIGNAL]。"
    )
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        _record_tool_execution(_get_current_event(context), self.name)
        return "[SYSTEM_WAIT_SIGNAL]"


@dataclass
class OmniPerceptionTool(FunctionTool[AstrAgentContext]):
    name: str = "omni_perception_query"
    description: str = (
        "统一查询记忆、用户画像、黑话词条、节点与每日反思。"
        "至少提供 query、target_name、recall_date 之一。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的事件、概念或关键词。", "maxLength": 240},
                "target_name": {"type": "string", "description": "要检索的人物名称或 ID。", "maxLength": 80},
                "recall_date": {
                    "type": "string",
                    "description": "要检索的日期，格式 YYYY-MM-DD。",
                    "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                },
            },
        }
    )
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    memory_tool_service: Optional[Any] = Field(default=None, exclude=True)
    db_service: Optional[Any] = Field(default=None, exclude=True)
    chat_id: str = Field(default="", exclude=True)
    current_sender_id: str = Field(default="", exclude=True)
    current_sender_name: str = Field(default="", exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query", "") or "").strip()
        target_name = str(kwargs.get("target_name", "") or "").strip()
        recall_date = str(kwargs.get("recall_date", "") or "").strip()
        if not (query or target_name or recall_date):
            return "执行失败：请至少提供 query、target_name、recall_date 之一。"

        current_event = _get_current_event(context)
        tool_service = self.memory_tool_service or getattr(self.memory_engine, "tool_service", None)
        if tool_service and hasattr(tool_service, "omni_query"):
            try:
                result = await tool_service.omni_query(
                    query=query,
                    target_name=target_name,
                    recall_date=recall_date,
                    chat_id=self.chat_id,
                    current_sender_id=self.current_sender_id,
                    current_sender_name=self.current_sender_name,
                    event=current_event,
                )
            except Exception:
                _record_tool_execution(current_event, self.name, status="failed")
                raise
            _record_tool_execution(current_event, self.name)
            return result

        async def _fetch_memory():
            return None

        async def _fetch_profile():
            if not self.db_service:
                return None
            entity = target_name or query
            if not entity:
                return None
            try:
                profile = None
                if entity == self.current_sender_name or entity in {"我", "自己", "当前用户"}:
                    getter = getattr(self.db_service, "get_user_profile", None)
                    if getter and self.current_sender_id:
                        profile = await getter(self.current_sender_id) if asyncio.iscoroutinefunction(getter) else getter(self.current_sender_id)
                else:
                    getter = getattr(self.db_service, "get_profile_by_name", None)
                    if getter:
                        profile = await getter(entity) if asyncio.iscoroutinefunction(getter) else getter(entity)
                if not profile:
                    return None
                social_score = float(getattr(profile, "social_score", 0.0) or 0.0)
                persona = getattr(profile, "persona_analysis", "") or "暂无稳定侧写。"
                name = getattr(profile, "name", entity)
                metadata = getattr(profile, "profile_metadata", None)
                identity = metadata.get("verified_identity", {}) if isinstance(metadata, dict) else {}
                identity_requested = bool(target_name) or any(
                    token in query.lower()
                    for token in ("qq", "是谁", "身份", "艾特", "@", "叫出来", "喊出来", "群成员")
                )
                identity_note = ""
                if identity_requested and isinstance(identity, dict) and bool(identity.get("verified")):
                    verified_qq = str(identity.get("user_id") or "").strip()
                    if verified_qq:
                        identity_note = (
                            f"\n可信 QQ 身份: {verified_qq}（来自真实 QQ 事件）。"
                            "\n当前群成员状态: 未验证；执行 @ 前必须通过 NapCat 校验当前群。"
                        )
                return f"对象: {name}\n好感度: {social_score:.1f}\n侧写: {persona}{identity_note}"
            except Exception as exc:
                logger.debug(f"[OmniPerceptionTool] profile lookup failed: {exc}")
                return None

        async def _fetch_nodes():
            if not self.db_service or not hasattr(self.db_service, "search_nodes_async"):
                return None
            search_term = target_name or query
            if not search_term:
                return None
            try:
                nodes = await self.db_service.search_nodes_async(search_term, limit=3, include_description=True)
                if not nodes:
                    return None
                lines = []
                for node in nodes:
                    lines.append(
                        f"- {getattr(node, 'name', '')} ({getattr(node, 'type', '')}): "
                        f"{getattr(node, 'description', '')}"
                    )
                return "\n".join(lines)
            except Exception as exc:
                logger.debug(f"[OmniPerceptionTool] node lookup failed: {exc}")
                return None

        async def _fetch_reflection():
            if not self.db_service or not recall_date or not hasattr(self.db_service, "get_reflection_async"):
                return None
            try:
                reflection = await self.db_service.get_reflection_async(recall_date)
                if not reflection:
                    return None
                return f"[{getattr(reflection, 'date', recall_date)}]\n{getattr(reflection, 'reflection', '')}"
            except Exception as exc:
                logger.debug(f"[OmniPerceptionTool] reflection lookup failed: {exc}")
                return None

        async def _fetch_jargon():
            return None

        memory_result, profile_result, node_result, reflection_result, jargon_result = await asyncio.gather(
            _fetch_memory(),
            _fetch_profile(),
            _fetch_nodes(),
            _fetch_reflection(),
            _fetch_jargon(),
            return_exceptions=True,
        )

        sections = []
        if memory_result:
            sections.append(f"[记忆]\n{memory_result}")
        if jargon_result:
            sections.append(f"[黑话]\n{jargon_result}")
        if profile_result:
            sections.append(f"[画像]\n{profile_result}")
        if node_result:
            sections.append(f"[节点]\n{node_result}")
        if reflection_result:
            sections.append(f"[反思]\n{reflection_result}")
        if not sections:
            _record_tool_execution(current_event, self.name)
            return "系统提示：当前没有检索到可用内部资料。"
        _record_tool_execution(current_event, self.name)
        return "\n\n".join(sections)


@dataclass
class ConstructAtEventTool(FunctionTool[AstrAgentContext]):
    name: str = "construct_at_event"
    description: str = (
        "在当前群中实时确认目标成员，并为最终发送阶段追加原生 @ 动作。"
        "只能使用当前会话群，历史群记录和记忆中的群号不能作为执行依据。"
    )
    db_service: Any = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_name": {"type": "string", "description": "要 @ 的目标用户名或 ID。", "minLength": 1, "maxLength": 80}
            },
            "required": ["target_name"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        target_name = str(kwargs.get("target_name", "") or "").strip()
        if not target_name:
            return "执行失败：target_name 不能为空。"
        current_event = _get_current_event(context)
        current_group_id = str(current_event.get_group_id() or "").strip()
        if not current_group_id:
            _record_tool_execution(current_event, self.name, status="failed")
            return "动作取消：当前不是群聊，无法执行群成员 @。"
        api = getattr(getattr(current_event, "bot", None), "api", None)
        if api is None:
            _record_tool_execution(current_event, self.name, status="failed")
            return "动作取消：NapCat 当前群成员接口不可用，无法安全确认 @ 目标。"

        astr_ctx = context.context.context
        clean_target = target_name.lstrip("@").strip()
        target_id = ""
        matched_name = ""
        try:
            if clean_target.isdigit():
                payload = await api.call_action(
                    "get_group_member_info",
                    group_id=_coerce_api_id(current_group_id),
                    user_id=_coerce_api_id(clean_target),
                    no_cache=False,
                )
                item = _payload_data(payload)
                if isinstance(item, dict) and _group_member_id(item) == clean_target:
                    target_id = clean_target
                    matched_name = _group_member_name(item)
            else:
                payload = await api.call_action(
                    "get_group_member_list",
                    group_id=_coerce_api_id(current_group_id),
                )
                matches = _exact_group_member_matches(
                    _list_entries(payload, "members", "list"),
                    clean_target,
                )
                if len(matches) == 1:
                    target_id = _group_member_id(matches[0])
                    matched_name = _group_member_name(matches[0])
                elif len(matches) > 1:
                    _record_tool_execution(current_event, self.name, status="failed")
                    return f"动作取消：当前群里有多个“{target_name}”候选，请提供 QQ 号。"
        except Exception as exc:
            _record_tool_execution(current_event, self.name, status="failed")
            return f"动作取消：读取当前群 {current_group_id} 成员时失败：{exc}"

        if not target_id and not clean_target.isdigit():
            resolved = await _resolve_target(
                self.db_service,
                target_name=target_name,
                current_event=current_event,
                astr_ctx=astr_ctx,
            )
            candidate_id = str(resolved[0] if resolved else "").strip()
            if candidate_id.isdigit():
                try:
                    payload = await api.call_action(
                        "get_group_member_info",
                        group_id=_coerce_api_id(current_group_id),
                        user_id=_coerce_api_id(candidate_id),
                        no_cache=False,
                    )
                    item = _payload_data(payload)
                    if isinstance(item, dict) and _group_member_id(item) == candidate_id:
                        target_id = candidate_id
                        matched_name = _group_member_name(item)
                except Exception as exc:
                    _record_tool_execution(current_event, self.name, status="failed")
                    return f"动作取消：身份候选 {candidate_id} 未能在当前群完成校验：{exc}"

        if not target_id:
            _record_tool_execution(current_event, self.name, status="failed")
            return f"动作取消：没有在当前群 {current_group_id} 中确认“{target_name}”。"
        if target_id == str(current_event.get_self_id()):
            _record_tool_execution(current_event, self.name, status="failed")
            return "动作取消：不能 @ 自己。"
        added = _append_once(
            current_event,
            matcher=lambda item: item.get("action") == "at" and str(item.get("target_id") or "") == target_id,
            action={
                "action": "at",
                "target_id": target_id,
                "target_name": matched_name or target_name,
                "requested_target_name": target_name,
                "group_id": current_group_id,
                "verified_current_group": True,
            },
        )
        current_event.set_extra("astrmai_at_action_verified", True)
        current_event.set_extra("astrmai_at_action_target_id", target_id)
        current_event.set_extra("astrmai_at_action_group_id", current_group_id)
        if not added:
            _record_tool_execution(current_event, self.name)
            return f"已存在对 {target_name} 的 @ 动作，无需重复添加。"
        return f"已在当前群确认 {matched_name or target_name}（QQ {target_id}），并加入原生 @ 动作。请继续生成最终回复文本。"


@dataclass
class ProactivePokeTool(FunctionTool[AstrAgentContext]):
    name: str = "proactive_poke"
    description: str = (
        "主动戳一戳目标用户。适合轻互动、调皮回戳、提醒对方看消息、或在群聊中自然拉某人回到话题。"
        "未指定目标时默认戳当前触发用户；不要在严肃、冲突或信息不明确时滥用。"
    )
    db_service: Any = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_name": {"type": "string", "description": "要戳的用户名或 ID；留空时使用当前发送者。", "maxLength": 80}
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        astr_ctx = context.context.context
        scope = MessageScope.from_event(current_event)
        target_name = str(kwargs.get("target_name", "") or "").strip()
        target_id: Optional[str] = None
        group_id = scope.group_id
        display_name = target_name or (current_event.get_sender_name() or "当前用户")
        is_peer_poke = (
            str(current_event.get_extra("astrmai_interaction_kind", "") or "").strip().lower() == "peer_poke"
        )

        if is_peer_poke:
            if not bool(current_event.get_extra("astrmai_peer_poke_join_allowed", False)):
                return "动作取消：这轮群友互戳不适合继续加入，请只用一句话旁观或忽略。"
            allowed_pairs = {
                str(current_event.get_extra("astrmai_interaction_actor_id", "") or "").strip(): str(
                    current_event.get_extra("astrmai_interaction_actor_name", "") or ""
                ).strip(),
                str(current_event.get_extra("astrmai_interaction_target_id", "") or "").strip(): str(
                    current_event.get_extra("astrmai_interaction_target_name", "") or ""
                ).strip(),
            }
            allowed_pairs = {key: value for key, value in allowed_pairs.items() if key}
            allowed_names = {name.strip().casefold(): uid for uid, name in allowed_pairs.items() if name}
            if not target_name:
                return "动作取消：加入群友互戳时必须明确选择戳发起者或被戳者。"
            clean_peer_target = target_name.lstrip("@").strip()
            if clean_peer_target in allowed_pairs:
                target_id = clean_peer_target
                display_name = allowed_pairs.get(target_id) or clean_peer_target
                target_name = display_name
            elif clean_peer_target.casefold() in allowed_names:
                target_id = allowed_names[clean_peer_target.casefold()]
                display_name = allowed_pairs.get(target_id) or clean_peer_target
                target_name = display_name

        if target_name:
            clean_target = target_name.lstrip("@").strip()
            current_sender_aliases = {
                str(scope.sender_id or "").strip().casefold(),
                str(current_event.get_sender_name() or "").strip().casefold(),
            }
            if target_id is not None:
                resolved_group_id = group_id
            elif scope.is_private_chat and clean_target.casefold() in current_sender_aliases:
                target_id = scope.sender_id
                resolved_group_id = None
            else:
                resolved = await _resolve_target(
                    self.db_service,
                    target_name=target_name,
                    current_event=current_event,
                    astr_ctx=astr_ctx,
                )
                if not resolved:
                    return f"动作取消：当前上下文里无法锁定 {target_name}。"
                target_id, resolved_group_id = resolved
                if is_peer_poke and target_id not in allowed_pairs:
                    allowed_display = "、".join(name or uid for uid, name in allowed_pairs.items())
                    return f"动作取消：群友互戳场景只能戳发起者或被戳者（{allowed_display}）。"
            current_group_id = str(scope.group_id or "").strip()
            resolved_group_id = str(resolved_group_id or "").strip()
            if scope.is_group_chat:
                if resolved_group_id and resolved_group_id != current_group_id:
                    return f"动作取消：{target_name} 不在当前群聊上下文里。"
            else:
                current_peer_id = str(scope.sender_id or "").strip()
                valid_private_scopes = {"", str(scope.umo or "").strip()}
                if (
                    resolved_group_id not in valid_private_scopes
                    or str(target_id or "").strip() != current_peer_id
                ):
                    return f"动作取消：{target_name} 不在当前私聊上下文里。"
        else:
            target_id = scope.sender_id

        if target_id == str(current_event.get_self_id()):
            return "动作取消：不能戳自己。"

        _append_qq_action(
            current_event,
            PendingQQAction(
                action_type="poke",
                target_id=str(target_id or ""),
                target_name=display_name,
                group_id=str(group_id or ""),
            ),
        )
        return f"已将戳一戳 {display_name} 加入待执行动作；不要声称已经成功，继续生成一两句自然、轻快的配套回应。"


@dataclass
class MessageEmojiLikeTool(FunctionTool[AstrAgentContext]):
    name: str = "message_emoji_like_action"
    description: str = "为当前焦点消息设置 QQ 原生表情回复。只会从内置的小型表情集合里挑选。"
    emoji_options: dict[str, list[str]] = Field(default_factory=lambda: dict(QQ_MESSAGE_EMOJI_OPTIONS), exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "tone": {
                    "type": "string",
                    "description": "表情语气，可选 support/approve/laugh/cute，留空则随机。",
                    "enum": ["support", "approve", "laugh", "cute", ""],
                }
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        message_id = _current_message_id(current_event)
        if not message_id:
            return "动作取消：当前消息没有可定位的 message_id。"

        tone = str(kwargs.get("tone", "") or "").strip().lower()
        selected_pool = list(self.emoji_options.get(tone) or [])
        selected_tone = tone if selected_pool else ""
        if not selected_pool:
            fallback_items = [(bucket, values) for bucket, values in self.emoji_options.items() if values]
            if not fallback_items:
                return "动作取消：当前没有可用的内置表情回复集合。"
            selected_tone, selected_pool = random.choice(fallback_items)
        emoji_id = random.choice(selected_pool)

        _append_qq_action(
            current_event,
            PendingQQAction(
                action_type="message_emoji_like",
                message_id=message_id,
                payload={
                    "emoji_id": str(emoji_id),
                    "tone": selected_tone or tone or "random",
                },
            ),
        )
        return "已将 QQ 原生消息表情回复加入待执行动作；不要声称已经成功，继续生成最终回复。"


@dataclass
class GroupSignTool(FunctionTool[AstrAgentContext]):
    name: str = "group_sign_action"
    description: str = "对当前群聊执行一次群签到。只允许在当前群上下文里使用。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        del kwargs
        current_event = _get_current_event(context)
        group_id = str(current_event.get_group_id() or "").strip()
        if not group_id:
            return "动作取消：当前不是群聊，无法执行群签到。"
        _append_qq_action(
            current_event,
            PendingQQAction(action_type="group_sign", group_id=group_id),
        )
        return "已将当前群签到加入待执行动作；不要声称已经成功，继续生成最终回复。"


@dataclass
class CustomFaceCatalogQueryTool(FunctionTool[AstrAgentContext]):
    name: str = "custom_face_catalog_query"
    description: str = "查询当前账号可用的 QQ 自定义表情目录，供后续聊天动作参考。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "最多返回多少个表情，默认 24。", "minimum": 1, "maximum": 96}
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        client = getattr(current_event, "bot", None)
        api = getattr(client, "api", None)
        if api is None:
            return "系统提示：当前无法访问 QQ 自定义表情目录。"
        try:
            count = int(kwargs.get("count", 24) or 24)
        except (TypeError, ValueError):
            count = 24
        count = max(1, min(count, 96))
        try:
            payload = await api.call_action("fetch_custom_face", count=count)
        except Exception as exc:
            logger.error(f"[CustomFaceCatalogQueryTool] execution failed: {exc}")
            return f"系统提示：查询自定义表情失败：{exc}"

        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            faces = payload["data"]
        else:
            faces = payload if isinstance(payload, list) else []
        normalized = [str(item).strip() for item in faces if str(item or "").strip()]
        if not normalized:
            return "系统提示：当前没有查询到可用的 QQ 自定义表情。"
        _record_tool_execution(current_event, self.name)
        preview = "\n".join(f"- {item}" for item in normalized[:count])
        return f"可用的 QQ 自定义表情如下：\n{preview}"


@dataclass
class ProactiveMemeTool(FunctionTool[AstrAgentContext]):
    name: str = "proactive_meme"
    description: str = "指定当前回复应附带的表情包情绪标签。"
    parameters: dict = Field(default_factory=dict)
    emotion_mapping: list = Field(default_factory=list, exclude=True)

    def __post_init__(self) -> None:
        valid_tags = [
            str(item).split(":", 1)[0].strip().lower()
            for item in self.emotion_mapping
            if str(item or "").strip()
        ]
        available = "\n".join(f"- {item}" for item in self.emotion_mapping) if self.emotion_mapping else "- neutral: 平静"
        self.description = (
            "选择一个表情包情绪标签，系统会在最终回复后自动按该标签补发表情包。\n"
            f"可用标签:\n{available}"
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "emotion_tag": {
                    "type": "string",
                    "description": "要使用的情绪标签。",
                    "enum": valid_tags or ["neutral"],
                }
            },
            "required": ["emotion_tag"],
        }

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        emotion_tag = str(kwargs.get("emotion_tag", "neutral") or "neutral").strip().lower()
        valid_tags = [str(item).split(":")[0].strip().lower() for item in self.emotion_mapping if str(item).strip()]
        if valid_tags and emotion_tag not in valid_tags:
            emotion_tag = "neutral"
        current_event.set_extra("astrmai_bypass_mood_analysis", emotion_tag)
        current_event.set_extra("astrmai_force_meme", True)
        _append_once(
            current_event,
            matcher=lambda item: item.get("action") == "meme",
            action={"action": "meme", "tag": emotion_tag},
        )
        return f"已锁定表情包标签 {emotion_tag}，请继续生成最终文本回复。"


@dataclass
class MemeResonanceTool(FunctionTool[AstrAgentContext]):
    name: str = "meme_resonance_action"
    description: str = "当需要 1:1 跟队复读时调用，系统会强制终止为固定输出。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_message": {"type": "string", "description": "要 1:1 复读的内容。", "minLength": 1, "maxLength": 500}
            },
            "required": ["target_message"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        target_message = str(kwargs.get("target_message", "") or "").strip()
        if not target_message:
            return "执行失败：target_message 不能为空。"
        current_event = _get_current_event(context)
        _append_once(
            current_event,
            matcher=lambda item: item.get("action") == "terminal_reread",
            action={"action": "terminal_reread", "content": target_message},
        )
        return f"[TERMINAL_YIELD]:{target_message}"


@dataclass
class TopicHijackTool(FunctionTool[AstrAgentContext]):
    name: str = "topic_hijack_action"
    description: str = "强制转移话题，用一个生活化状态切断当前硬核话题。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "fake_status": {"type": "string", "description": "用于自然转移话题的当前状态短句。", "minLength": 1, "maxLength": 80}
            },
            "required": ["fake_status"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        fake_status = str(kwargs.get("fake_status", "") or "").strip() or "正忙着"
        _record_tool_execution(_get_current_event(context), self.name)
        return (
            "[SYSTEM OVERRIDE] 立刻停止回答原话题。"
            f"请以“我现在{fake_status}”为现实前提，简短、口语化地把话题拐开。"
        )


@dataclass
class QQFriendLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "qq_friend_lookup"
    description: str = (
        "只读查询机器人自己的 QQ 好友事实。"
        "当用户问某 QQ/昵称是不是机器人好友，或跨会话传话前需要确认收件人时调用；"
        "工具查不到时只能回答未知，不能猜测对方身份。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要查询的 QQ 号、好友备注或昵称；为空时只返回好友数量摘要。",
                    "maxLength": 80,
                },
                "include_preview": {
                    "type": "boolean",
                    "description": "是否返回少量好友预览。默认 false；只有用户明确询问好友列表时才设为 true。",
                },
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        api = getattr(getattr(current_event, "bot", None), "api", None)
        if api is None:
            _record_tool_execution(current_event, self.name, status="failed")
            return "好友查询失败：NapCat 好友接口不可用。"
        target = str(kwargs.get("target", "") or "").strip()
        include_preview = bool(kwargs.get("include_preview", False))
        try:
            payload = await api.call_action("get_friend_list")
            friends = _friend_entries(payload)
            if not friends:
                payload = await api.call_action("get_friends_with_category")
                friends = _friend_entries(payload)
        except Exception as exc:
            _record_tool_execution(current_event, self.name, status="failed")
            return f"好友查询失败：读取机器人好友列表时出错：{exc}"

        if target:
            matches = _match_by_id_or_name(
                friends,
                target,
                id_getter=_friend_id,
                name_getter=_friend_name,
            )
            if not matches:
                _record_tool_execution(current_event, self.name)
                return f"好友查询结果：机器人好友列表中没有找到“{target}”。不能声称已经联系到这个人。"
            lines = []
            for item in matches[:5]:
                friend_id = _friend_id(item)
                display = _friend_name(item)
                nickname = str(item.get("nickname") or "").strip()
                remark = str(item.get("remark") or "").strip()
                extra = f"；昵称={nickname}" if nickname and nickname != display else ""
                extra += f"；备注={remark}" if remark and remark != display else ""
                lines.append(f"- QQ {friend_id}: {display}{extra}")
            if len(matches) > 5:
                lines.append(f"- 另有 {len(matches) - 5} 个候选，请让用户提供准确 QQ 号。")
            _record_tool_execution(current_event, self.name)
            return "好友查询结果：找到匹配好友。\n" + "\n".join(lines)

        _record_tool_execution(current_event, self.name)
        if include_preview:
            preview = [f"- QQ {_friend_id(item)}: {_friend_name(item)}" for item in friends[:12]]
            suffix = f"\n另有 {len(friends) - 12} 位未展示。" if len(friends) > 12 else ""
            return f"好友查询结果：机器人好友共 {len(friends)} 位。\n" + "\n".join(preview) + suffix
        return f"好友查询结果：机器人好友共 {len(friends)} 位。需要具体判断时请提供 QQ 号、备注或昵称。"


@dataclass
class QQGroupPresenceLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "qq_group_presence_lookup"
    description: str = (
        "只读查询机器人所在群、某群是否匹配、以及指定用户是否与机器人共享群。"
        "当用户说“我在别的群见过你”“那个群里的你是不是你”时必须先调用本工具查平台事实；"
        "查不到时只能说无法确认，不能断言冒充、授权或诈骗。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "group_query": {
                    "type": "string",
                    "description": "要查询的群号或群名关键词；可为空。",
                    "maxLength": 80,
                },
                "user_id": {
                    "type": "string",
                    "description": "要判断是否与机器人共享群的 QQ 号；为空时默认当前发言人。",
                    "maxLength": 32,
                },
                "include_preview": {
                    "type": "boolean",
                    "description": "是否返回少量群预览。默认 false；只有用户明确询问机器人在哪些群时才设为 true。",
                },
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        api = getattr(getattr(current_event, "bot", None), "api", None)
        if api is None:
            _record_tool_execution(current_event, self.name, status="failed")
            return "群事实查询失败：NapCat 群接口不可用。"
        include_preview = bool(kwargs.get("include_preview", False))
        group_query = str(kwargs.get("group_query", "") or "").strip()
        raw_user_id = str(kwargs.get("user_id", "") or "").strip()
        user_id = raw_user_id or (
            str(current_event.get_sender_id() or "").strip()
            if group_query or not include_preview
            else ""
        )
        try:
            groups = _list_entries(await api.call_action("get_group_list"), "groups", "list")
        except Exception as exc:
            _record_tool_execution(current_event, self.name, status="failed")
            return f"群事实查询失败：读取机器人群列表时出错：{exc}"

        matched_groups = (
            _match_by_id_or_name(groups, group_query, id_getter=_group_id, name_getter=_group_name)
            if group_query
            else []
        )
        lines: list[str] = [f"机器人当前可见群数量：{len(groups)}。"]
        if group_query:
            if matched_groups:
                lines.append("群匹配结果：")
                lines.extend(f"- 群 {_group_id(item)}: {_group_name(item)}" for item in matched_groups[:8])
                if len(matched_groups) > 8:
                    lines.append(f"- 另有 {len(matched_groups) - 8} 个候选。")
            else:
                lines.append(f"群匹配结果：没有找到与“{group_query}”匹配的机器人所在群。")
        elif include_preview:
            lines.append("群预览：")
            lines.extend(f"- 群 {_group_id(item)}: {_group_name(item)}" for item in groups[:10])
            if len(groups) > 10:
                lines.append(f"- 另有 {len(groups) - 10} 个群未展示。")

        if user_id:
            check_groups = matched_groups if matched_groups else groups[:50]
            shared: list[dict[str, Any]] = []
            current_group_id = str(current_event.get_group_id() or "").strip()
            if current_group_id and str(current_event.get_sender_id() or "").strip() == user_id:
                current_group = next((item for item in groups if _group_id(item) == current_group_id), None)
                if current_group is None:
                    current_group = {"group_id": current_group_id, "group_name": "当前群"}
                shared.append(current_group)
            for item in check_groups:
                gid = _group_id(item)
                if not gid or any(_group_id(existing) == gid for existing in shared):
                    continue
                try:
                    await api.call_action(
                        "get_group_member_info",
                        group_id=int(gid) if gid.isdigit() else gid,
                        user_id=int(user_id) if user_id.isdigit() else user_id,
                    )
                except Exception:
                    continue
                shared.append(item)
                if len(shared) >= 8:
                    break
            if shared:
                lines.append(f"共同群判断：QQ {user_id} 与机器人至少共享 {len(shared)} 个可见群：")
                lines.extend(f"- 群 {_group_id(item)}: {_group_name(item)}" for item in shared[:8])
            else:
                checked = len(check_groups)
                lines.append(f"共同群判断：在已检查的 {checked} 个机器人可见群中，没有确认 QQ {user_id} 与机器人共享群。")
        _record_tool_execution(current_event, self.name)
        return "\n".join(lines)


@dataclass
class QQRecentContactLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "qq_recent_contact_lookup"
    description: str = (
        "只读查询机器人最近联系人和本轮跨会话发送记录。"
        "用于回答“刚才谁找你”“你最近有没有联系某人”“跨会话消息发到哪里了”等事实问题。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "要筛选的 QQ、昵称、群名或备注。", "maxLength": 80},
                "count": {"type": "integer", "description": "最多返回多少条最近联系人，1-20。", "minimum": 1, "maximum": 20},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        api = getattr(getattr(current_event, "bot", None), "api", None)
        target = str(kwargs.get("target", "") or "").strip()
        try:
            count = max(1, min(int(kwargs.get("count", 8) or 8), 20))
        except (TypeError, ValueError):
            count = 8
        entries: list[dict[str, Any]] = []
        api_error = ""
        if api is not None:
            try:
                try:
                    payload = await api.call_action("get_recent_contact", count=count)
                except TypeError:
                    payload = await api.call_action("get_recent_contact")
                entries = _list_entries(payload, "contacts", "list", "items")
            except Exception as exc:
                api_error = str(exc)
        astr_ctx = context.context.context
        handoff_lines: list[str] = []
        sends = current_event.get_extra("astrmai_cross_session_sends", []) if hasattr(current_event, "get_extra") else []
        for item in list(sends or [])[-5:]:
            if not isinstance(item, dict):
                continue
            handoff_lines.append(
                f"- 本轮已向 {item.get('target_umo') or item.get('target_id')} 发送：{_truncate_text(item.get('message'), 80)}"
            )
        shared_dict = getattr(astr_ctx, "shared_dict", None)
        if isinstance(shared_dict, dict):
            jumps = shared_dict.get("astrmai_space_jumps", {})
            if isinstance(jumps, dict):
                for target_id, item in list(jumps.items())[-5:]:
                    if isinstance(item, dict):
                        handoff_lines.append(
                            f"- 待衔接跨会话 target={target_id}: {_truncate_text(item.get('context_summary') or item.get('private_message'), 80)}"
                        )

        if target:
            lowered = target.casefold()
            entries = [
                item
                for item in entries
                if lowered
                in " ".join(
                    str(item.get(key) or "")
                    for key in ("user_id", "group_id", "peer_id", "nickname", "remark", "name", "group_name")
                ).casefold()
            ]
        lines: list[str] = []
        if entries:
            lines.append(f"最近联系人查询结果：{len(entries)} 条候选。")
            for item in entries[:count]:
                contact_id = _entry_id(item, "peer_id", "user_id", "group_id", "uin", "id")
                name = (
                    str(item.get("remark") or item.get("nickname") or item.get("group_name") or item.get("name") or contact_id).strip()
                )
                contact_type = str(item.get("type") or item.get("chat_type") or item.get("msg_type") or "").strip()
                lines.append(f"- {contact_type or 'contact'} {contact_id}: {name}")
        elif api_error:
            lines.append(f"最近联系人接口暂时不可用：{api_error}")
        else:
            lines.append("最近联系人查询结果：没有拿到可用联系人。")
        if handoff_lines:
            lines.append("跨会话记录摘要：")
            lines.extend(handoff_lines)
        _record_tool_execution(current_event, self.name, status="success" if entries or handoff_lines or not api_error else "failed")
        return "\n".join(lines)


@dataclass
class QQMessageArtifactLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "qq_message_artifact_lookup"
    description: str = (
        "只读查询指定消息、当前消息、图片、表情、文件和转发片段的结构化信息。"
        "当用户说“刚才那张图/那条消息/那个文件/转发内容”时调用；"
        "本工具只描述可见消息素材，不直接做视觉识别。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": (
                        "要查询的真实消息 ID。查询当前消息、当前图片或当前表情包时必须留空，"
                        "由运行时自动绑定；禁止填写 QQ 号、群号或自行猜测的数字。"
                    ),
                    "maxLength": 80,
                },
                "include_text_preview": {
                    "type": "boolean",
                    "description": "是否返回短文本预览。默认 true。",
                },
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        api = getattr(getattr(current_event, "bot", None), "api", None)
        message_id = str(kwargs.get("message_id", "") or "").strip()
        include_text_preview = bool(kwargs.get("include_text_preview", True))
        binding_error = _message_id_binding_error(current_event, message_id)
        if binding_error:
            _record_tool_execution(current_event, self.name, status="failed")
            return binding_error
        payload: Any = None
        if message_id:
            if api is None:
                _record_tool_execution(current_event, self.name, status="failed")
                return "消息素材查询失败：NapCat 消息接口不可用。"
            try:
                payload = await api.call_action("get_msg", message_id=int(message_id) if message_id.isdigit() else message_id)
            except Exception as exc:
                _record_tool_execution(current_event, self.name, status="failed")
                return f"消息素材查询失败：读取消息 {message_id} 时出错：{exc}"
        else:
            message_obj = getattr(current_event, "message_obj", None)
            payload = {
                "message_id": _current_message_id(current_event),
                "sender": {"user_id": str(current_event.get_sender_id() or ""), "nickname": str(current_event.get_sender_name() or "")},
                "message": getattr(message_obj, "message", None),
                "raw_message": getattr(message_obj, "raw_message", None),
                "time": getattr(message_obj, "timestamp", None),
            }

        data = _payload_data(payload)
        if not isinstance(data, dict):
            data = {"message": data}
        segments = _message_segments(data)
        lines = [
            f"消息 ID：{str(data.get('message_id') or message_id or _current_message_id(current_event) or 'unknown')}",
        ]
        sender = data.get("sender")
        if isinstance(sender, dict):
            sender_id = str(sender.get("user_id") or sender.get("uin") or "").strip()
            sender_name = str(sender.get("nickname") or sender.get("card") or sender.get("name") or "").strip()
            if sender_id or sender_name:
                lines.append(f"发送者：{sender_name or 'unknown'} ({sender_id or 'unknown'})")
        if not segments:
            text = str(data.get("raw_message") or data.get("message") or "").strip()
            if text:
                segments = [{"type": "text", "data": {"text": text}}]
        if not segments:
            _record_tool_execution(current_event, self.name)
            return "\n".join(lines + ["消息片段：未发现可解析内容。"])

        lines.append(f"消息片段数量：{len(segments)}")
        for index, segment in enumerate(segments[:8], start=1):
            seg_type = _segment_type(segment) or "unknown"
            data_map = _segment_data(segment)
            parts = [f"{index}. type={seg_type}"]
            if include_text_preview and data_map.get("text"):
                parts.append(f"text={_truncate_text(data_map.get('text'), 100)}")
            for key in ("file", "name", "url", "path", "file_id", "file_unique", "summary"):
                value = str(data_map.get(key) or "").strip()
                if value:
                    parts.append(f"{key}={_truncate_text(value, 80)}")
            lines.append("；".join(parts))
        if len(segments) > 8:
            lines.append(f"另有 {len(segments) - 8} 个片段未展示。")
        _record_tool_execution(current_event, self.name)
        return "\n".join(lines)


@dataclass
class CrossChatMemoryQueryTool(FunctionTool[AstrAgentContext]):
    name: str = "cross_chat_memory_query"
    description: str = (
        "查询 AstrMai 自己跨群、跨私聊、跨会话的内部记忆和自述资料。"
        "用于确认“我是不是在别的群见过你”“你自己有没有相关记忆/设定/授权记录”；"
        "查不到时必须回答没有明确记录，不能把未知说成事实。"
    )
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    memory_tool_service: Optional[Any] = Field(default=None, exclude=True)
    persona_id: str = Field(default="", exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查询的跨会话事实、人物、群名或主题。", "minLength": 1, "maxLength": 240},
                "scope": {
                    "type": "string",
                    "description": "查询范围：memory 查普通跨会话记忆；self_lore 查自我设定；all 同时查两者。",
                    "enum": ["memory", "self_lore", "all"],
                },
                "top_k": {"type": "integer", "description": "最多返回条数，1-8。", "minimum": 1, "maximum": 8},
            },
            "required": ["query"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query", "") or "").strip()
        if not query:
            return "跨会话记忆查询失败：query 不能为空。"
        scope = str(kwargs.get("scope", "all") or "all").strip()
        if scope not in {"memory", "self_lore", "all"}:
            scope = "all"
        try:
            top_k = max(1, min(int(kwargs.get("top_k", 5) or 5), 8))
        except (TypeError, ValueError):
            top_k = 5
        current_event = _get_current_event(context)
        tool_service = self.memory_tool_service or getattr(self.memory_engine, "tool_service", None)
        if tool_service is None:
            _record_tool_execution(current_event, self.name, status="failed")
            return "系统提示：当前没有可用的跨会话记忆查询服务。"
        sections: list[str] = []
        try:
            if scope in {"memory", "all"} and hasattr(tool_service, "search_memory"):
                result = await tool_service.search_memory(
                    query=query,
                    session_id="",
                    top_k=top_k,
                    event=current_event,
                    allow_stale=True,
                )
                rendered = tool_service.render_result(result)
                if rendered and "no usable internal memory" not in rendered.lower():
                    sections.append("[AstrMai 跨会话记忆]\n" + rendered)
            if scope in {"self_lore", "all"} and hasattr(tool_service, "self_lore_query"):
                lore = await tool_service.self_lore_query(
                    query=query,
                    persona_id=self.persona_id,
                    event=current_event,
                    top_k=min(top_k, 3),
                    allow_stale=True,
                )
                rendered_lore = tool_service.render_result(lore)
                if rendered_lore and "no usable internal memory" not in rendered_lore.lower():
                    sections.append("[AstrMai 自我设定]\n" + rendered_lore)
        except Exception as exc:
            logger.debug(f"[CrossChatMemoryQueryTool] lookup failed: {exc}")
            _record_tool_execution(current_event, self.name, status="failed")
            return "系统提示：跨会话记忆查询暂时失败。"
        _record_tool_execution(current_event, self.name)
        if not sections:
            return "系统提示：AstrMai 没有检索到可确认的跨会话记忆或自我设定。请把这当作未知，不要断言。"
        return "\n\n".join(sections)


@dataclass
class QQGroupMemberLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "qq_group_member_lookup"
    description: str = (
        "只读查询机器人可见群中的成员身份、群名片、昵称、角色和活跃时间。"
        "当用户问某人在不在当前群/某群、群里怎么称呼、是否管理员时调用；查不到必须说未知。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "QQ 号、群名片或昵称；为空时默认当前发言人。", "maxLength": 80},
                "group_id": {"type": "string", "description": "群号；为空时使用当前群。", "maxLength": 32},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        api = getattr(getattr(event, "bot", None), "api", None)
        if api is None:
            _record_tool_execution(event, self.name, status="failed")
            return "群成员查询失败：NapCat 群接口不可用。"
        group_id = str(kwargs.get("group_id", "") or event.get_group_id() or "").strip()
        target = str(kwargs.get("target", "") or event.get_sender_id() or "").strip().lstrip("@")
        if not group_id:
            _record_tool_execution(event, self.name, status="failed")
            return "群成员查询失败：当前不是群聊，也没有提供 group_id。"
        if not target:
            _record_tool_execution(event, self.name, status="failed")
            return "群成员查询失败：target 不能为空。"
        try:
            if target.isdigit():
                payload = await api.call_action(
                    "get_group_member_info",
                    group_id=_coerce_api_id(group_id),
                    user_id=_coerce_api_id(target),
                    no_cache=False,
                )
                entries = [_payload_data(payload)] if isinstance(_payload_data(payload), dict) else []
            else:
                payload = await api.call_action("get_group_member_list", group_id=_coerce_api_id(group_id))
                entries = _match_by_id_or_name(
                    _list_entries(payload, "members", "list"),
                    target,
                    id_getter=_group_member_id,
                    name_getter=_group_member_name,
                )
        except Exception as exc:
            _record_tool_execution(event, self.name, status="failed")
            return f"群成员查询失败：读取群 {group_id} 成员信息时出错：{exc}"
        if not entries:
            _record_tool_execution(event, self.name)
            return f"群成员查询结果：没有在群 {group_id} 中确认“{target}”。"
        lines = [f"群成员查询结果：群 {group_id} 中找到 {len(entries)} 个候选。"]
        for item in entries[:6]:
            member_id = _group_member_id(item)
            name = _group_member_name(item)
            role = str(item.get("role") or "").strip()
            title = str(item.get("title") or item.get("special_title") or "").strip()
            last_sent = str(item.get("last_sent_time") or "").strip()
            parts = [f"- QQ {member_id or 'unknown'}: {name or 'unknown'}"]
            if role:
                parts.append(f"角色={role}")
            if title:
                parts.append(f"头衔={title}")
            if last_sent:
                parts.append(f"最近发言={last_sent}")
            lines.append("；".join(parts))
        _record_tool_execution(event, self.name)
        return "\n".join(lines)


@dataclass
class QQUserIdentityLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "qq_user_identity_lookup"
    description: str = (
        "只读综合查询一个 QQ/昵称在机器人视角下的身份：是否好友、当前群身份、备注、群名片和昵称。"
        "当模型需要区分不同人、避免把群友/好友搞混时调用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "QQ 号、好友备注、群名片或昵称；为空时默认当前发言人。", "maxLength": 80},
                "group_id": {"type": "string", "description": "群号；为空时使用当前群。", "maxLength": 32},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        api = getattr(getattr(event, "bot", None), "api", None)
        if api is None:
            _record_tool_execution(event, self.name, status="failed")
            return "用户身份查询失败：NapCat 接口不可用。"
        target = str(kwargs.get("target", "") or event.get_sender_id() or "").strip().lstrip("@")
        group_id = str(kwargs.get("group_id", "") or event.get_group_id() or "").strip()
        lines: list[str] = [f"用户身份查询：target={target or 'current'}。"]
        try:
            friends = _friend_entries(await api.call_action("get_friend_list"))
            friend_matches = _match_by_id_or_name(
                friends,
                target,
                id_getter=_friend_id,
                name_getter=_friend_name,
            ) if target else []
        except Exception as exc:
            friend_matches = []
            lines.append(f"好友身份：查询失败：{exc}")
        if friend_matches:
            for item in friend_matches[:4]:
                lines.append(f"好友身份：是机器人好友，QQ {_friend_id(item)}，显示名 {_friend_name(item)}。")
        elif target:
            lines.append("好友身份：未在机器人好友列表中确认。")

        if group_id and target:
            try:
                if target.isdigit():
                    payload = await api.call_action(
                        "get_group_member_info",
                        group_id=_coerce_api_id(group_id),
                        user_id=_coerce_api_id(target),
                        no_cache=False,
                    )
                    members = [_payload_data(payload)] if isinstance(_payload_data(payload), dict) else []
                else:
                    payload = await api.call_action("get_group_member_list", group_id=_coerce_api_id(group_id))
                    members = _match_by_id_or_name(
                        _list_entries(payload, "members", "list"),
                        target,
                        id_getter=_group_member_id,
                        name_getter=_group_member_name,
                    )
            except Exception as exc:
                members = []
                lines.append(f"群身份：查询群 {group_id} 失败：{exc}")
            if members:
                for item in members[:4]:
                    lines.append(
                        f"群身份：在群 {group_id} 中，QQ {_group_member_id(item)}，群名片/昵称 {_group_member_name(item)}，"
                        f"角色 {str(item.get('role') or 'member')}。"
                    )
            else:
                lines.append(f"群身份：未在群 {group_id} 中确认该用户。")
        _record_tool_execution(event, self.name)
        return "\n".join(lines)


@dataclass
class QQForwardMessageLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "qq_forward_message_lookup"
    description: str = (
        "只读解析当前或指定消息中的合并转发/转发节点。"
        "用于用户让你看转发消息、截图式聊天记录、或问转发里是谁说了什么。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "包含转发的消息 ID；为空时使用当前消息。", "maxLength": 80},
                "max_nodes": {"type": "integer", "description": "最多展示多少个转发节点，1-20。", "minimum": 1, "maximum": 20},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        api = getattr(getattr(event, "bot", None), "api", None)
        message_id = str(kwargs.get("message_id", "") or "").strip()
        try:
            max_nodes = max(1, min(int(kwargs.get("max_nodes", 8) or 8), 20))
        except (TypeError, ValueError):
            max_nodes = 8
        payload: Any
        if message_id:
            if api is None:
                _record_tool_execution(event, self.name, status="failed")
                return "转发消息查询失败：NapCat 消息接口不可用。"
            try:
                payload = await api.call_action("get_msg", message_id=_coerce_api_id(message_id))
            except Exception as exc:
                _record_tool_execution(event, self.name, status="failed")
                return f"转发消息查询失败：读取消息 {message_id} 时出错：{exc}"
        else:
            payload = {
                "message_id": _current_message_id(event),
                "message": getattr(getattr(event, "message_obj", None), "message", None),
                "raw_message": getattr(getattr(event, "message_obj", None), "raw_message", None),
            }
        data = _payload_data(payload)
        segments = _message_segments(data)
        forward_ids: list[str] = []
        inline_nodes: list[dict[str, Any]] = []
        for segment in segments:
            seg_type = _segment_type(segment).lower()
            data_map = _segment_data(segment)
            if seg_type in {"forward", "node", "json"}:
                fid = str(data_map.get("id") or data_map.get("forward_id") or data_map.get("resid") or "").strip()
                if fid:
                    forward_ids.append(fid)
                nodes = data_map.get("nodes") or data_map.get("content")
                if isinstance(nodes, list):
                    inline_nodes.extend(item for item in nodes if isinstance(item, dict))
        nodes: list[dict[str, Any]] = list(inline_nodes)
        if api is not None:
            for fid in forward_ids[:3]:
                try:
                    expanded = await api.call_action("get_forward_msg", message_id=fid)
                except Exception:
                    try:
                        expanded = await api.call_action("get_forward_msg", id=fid)
                    except Exception:
                        continue
                nodes.extend(_history_messages(expanded))
        if not nodes:
            _record_tool_execution(event, self.name)
            return "转发消息查询结果：当前消息中没有解析到可展开的合并转发节点。"
        lines = [f"转发消息查询结果：解析到 {len(nodes)} 个节点，展示前 {min(len(nodes), max_nodes)} 个。"]
        for idx, node in enumerate(nodes[:max_nodes], start=1):
            lines.append(_format_message_summary(node, idx))
        _record_tool_execution(event, self.name)
        return "\n".join(lines)


@dataclass
class VisionMessageAnalyzeTool(FunctionTool[AstrAgentContext]):
    db_service: Any = None
    name: str = "vision_message_analyze_tool"
    description: str = (
        "查询当前/指定图片消息的视觉转述结果；如果已由 VisualCortex 分析，会返回图片描述、类型和情绪标签。"
        "用于回答“这张图是什么”“这个表情包什么意思”。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": (
                        "真实图片消息 ID。分析当前图片/表情包时必须留空，由运行时自动绑定；"
                        "禁止填写 QQ 号、群号或自行猜测的数字。"
                    ),
                    "maxLength": 80,
                },
                "image_index": {"type": "integer", "description": "第几张图，从 1 开始。", "minimum": 1, "maximum": 8},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        message_id = str(kwargs.get("message_id", "") or "").strip()
        try:
            image_index = max(1, min(int(kwargs.get("image_index", 1) or 1), 8))
        except (TypeError, ValueError):
            image_index = 1
        records = _visual_records_from_event(event)
        if records:
            selected = records[min(image_index - 1, len(records) - 1)]
            desc = str(selected.get("description") or selected.get("summary") or selected.get("text") or "").strip()
            img_type = str(selected.get("type") or "image").strip()
            tags = selected.get("emotion_tags") or selected.get("tags") or []
            _record_tool_execution(event, self.name)
            return f"视觉转述结果：type={img_type}；tags={tags}；description={_truncate_text(desc, 600)}"
        if not message_id:
            message_obj = getattr(event, "message_obj", None)
            message_id = str(
                getattr(message_obj, "message_id", "")
                or getattr(message_obj, "id", "")
                or ""
            ).strip()
        if message_id and self.db_service is not None:
            try:
                chat_id = str(
                    getattr(event, "unified_msg_origin", "")
                    or getattr(event, "session_id", "")
                    or ""
                )
                with self.db_service.get_session() as session:
                    statement = select(VisualMessageBinding).where(
                        VisualMessageBinding.message_id == message_id,
                        VisualMessageBinding.image_index == image_index - 1,
                    )
                    if chat_id:
                        statement = statement.where(
                            VisualMessageBinding.chat_id == chat_id
                        )
                    binding = session.exec(statement).first()
                    asset = (
                        session.get(VisualAsset, binding.asset_id)
                        if binding is not None
                        else None
                    )
                if asset is not None and str(asset.description or "").strip():
                    tags = []
                    try:
                        import json

                        parsed_tags = json.loads(str(asset.emotion_tags or "[]"))
                        if isinstance(parsed_tags, list):
                            tags = parsed_tags
                    except (TypeError, ValueError):
                        tags = []
                    _record_tool_execution(event, self.name)
                    return (
                        "视觉转述结果："
                        f"type={str(asset.type or 'image')}；"
                        f"tags={tags}；"
                        f"description={_truncate_text(str(asset.description or ''), 600)}"
                    )
            except Exception as exc:
                logger.debug(
                    f"[VisionMessageAnalyzeTool] visual asset lookup degraded: {exc}"
                )
        binding_error = _message_id_binding_error(event, message_id)
        if binding_error:
            _record_tool_execution(event, self.name, status="failed")
            return binding_error
        payload: Any = None
        api = getattr(getattr(event, "bot", None), "api", None)
        if message_id and api is not None:
            try:
                payload = await api.call_action("get_msg", message_id=_coerce_api_id(message_id))
            except Exception as exc:
                _record_tool_execution(event, self.name, status="failed")
                return f"视觉转述查询失败：读取图片消息 {message_id} 时出错：{exc}"
        else:
            # OPT-12/TL-03: 执行事件被 sanitize 成 Plain 占位，优先读保留的原始组件
            preserved_segments = (
                event.get_extra("astrmai_original_message_segments", None)
                if hasattr(event, "get_extra")
                else None
            )
            payload = {
                "message": preserved_segments
                if preserved_segments
                else getattr(getattr(event, "message_obj", None), "message", None),
                "raw_message": getattr(getattr(event, "message_obj", None), "raw_message", None),
            }
        candidates = _extract_image_candidates(_payload_data(payload))
        if not candidates:
            _record_tool_execution(event, self.name)
            return "视觉转述结果：当前没有发现可分析的图片或表情包片段。"
        image = candidates[min(image_index - 1, len(candidates) - 1)]
        _record_tool_execution(event, self.name)
        return (
            "视觉转述结果：图片分析还没有完成；已看到图片片段。"
            f"type={image.get('type')} file={_truncate_text(image.get('file'), 120)} url={_truncate_text(image.get('url'), 120)}。"
            "请不要臆测图片内容，可提示用户稍后再问或根据已完成的视觉转述作答。"
        )


@dataclass
class CrossSessionReplyLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "cross_session_reply_lookup"
    description: str = (
        "只读查询跨会话传话后目标好友/会话的近期回复，用来判断对方有没有回、回了什么。"
        "不会主动发送消息。"
    )
    db_service: Any = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_name": {"type": "string", "description": "目标好友 QQ、备注或昵称；为空时读取本轮最近跨会话目标。", "maxLength": 80},
                "count": {"type": "integer", "description": "最多读取多少条近期私聊消息，1-20。", "minimum": 1, "maximum": 20},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        api = getattr(getattr(event, "bot", None), "api", None)
        if api is None:
            _record_tool_execution(event, self.name, status="failed")
            return "跨会话回复查询失败：NapCat 消息接口不可用。"
        target_name = str(kwargs.get("target_name", "") or "").strip()
        if not target_name:
            sends = event.get_extra("astrmai_cross_session_sends", []) if hasattr(event, "get_extra") else []
            if isinstance(sends, list) and sends:
                last = next((item for item in reversed(sends) if isinstance(item, dict)), {})
                target_name = str(last.get("target_id") or last.get("target_name") or "").strip()
        if not target_name:
            _record_tool_execution(event, self.name, status="failed")
            return "跨会话回复查询失败：没有目标好友。"
        resolved, reason = await _resolve_friend_target(event, context.context.context, self.db_service, target_name)
        if not resolved:
            _record_tool_execution(event, self.name, status="failed")
            return f"跨会话回复查询失败：{reason}"
        target_id, display = resolved
        try:
            count = max(1, min(int(kwargs.get("count", 8) or 8), 20))
        except (TypeError, ValueError):
            count = 8
        try:
            payload = await api.call_action(
                "get_friend_msg_history",
                user_id=_coerce_api_id(target_id),
                message_seq=0,
                count=count,
            )
        except Exception as exc:
            _record_tool_execution(event, self.name, status="failed")
            return f"跨会话回复查询失败：读取 {display}({target_id}) 的私聊历史出错：{exc}"
        messages = _history_messages(payload)
        if not messages:
            _record_tool_execution(event, self.name)
            return f"跨会话回复查询结果：没有读取到 {display}({target_id}) 的近期回复。"
        lines = [f"跨会话回复查询结果：{display}({target_id}) 近期消息 {len(messages)} 条。"]
        for idx, message in enumerate(messages[-count:], start=1):
            lines.append(_format_message_summary(message, idx))
        _record_tool_execution(event, self.name)
        return "\n".join(lines)


@dataclass
class QQCustomFaceSendTool(FunctionTool[AstrAgentContext]):
    name: str = "qq_custom_face_send_tool"
    description: str = (
        "从机器人自定义表情列表中挑选并发送一个自定义表情。"
        "适合普通聊天中表达调侃、开心、震惊等情绪；如果找不到表情，必须如实说明。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "想匹配的表情关键词、名字或情绪。", "maxLength": 80},
                "face_id": {"type": "string", "description": "已知的自定义表情 id，可为空。", "maxLength": 120},
                "reason": {"type": "string", "description": "为什么想发这个表情，给审计日志使用。", "maxLength": 120},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        api = getattr(getattr(event, "bot", None), "api", None)
        keyword = str(kwargs.get("keyword", "") or "").strip()
        face_id = str(kwargs.get("face_id", "") or "").strip()
        reason = str(kwargs.get("reason", "") or "").strip()
        face_payload: dict[str, Any] = {"id": face_id} if face_id else {}
        if api is not None and not face_payload:
            try:
                payload = await api.call_action("fetch_custom_face", count=80)
                entries = _list_entries(payload, "faces", "list", "items")
                if not entries and isinstance(_payload_data(payload), list):
                    entries = _list_entries(payload)
                lowered = keyword.casefold()
                match = next(
                    (
                        item
                        for item in entries
                        if not lowered
                        or lowered
                        in " ".join(str(item.get(k) or "") for k in ("id", "name", "summary", "key", "emoji_id")).casefold()
                    ),
                    None,
                )
                if isinstance(match, dict):
                    face_payload = dict(match)
            except Exception as exc:
                logger.debug(f"[QQCustomFaceSendTool] custom face lookup degraded: {exc}")
        if not face_payload:
            _record_tool_execution(event, self.name, status="failed")
            return "自定义表情发送失败：没有匹配到可发送的自定义表情。"
        group_id = str(event.get_group_id() or "").strip()
        target_id = str(event.get_sender_id() or "").strip() if not group_id else ""
        queued = _append_qq_action(
            event,
            PendingQQAction(
                action_type="custom_face_send",
                target_id=target_id,
                group_id=group_id,
                payload={"face": face_payload, "keyword": keyword, "reason": reason},
            ),
        )
        if queued:
            return "已准备发送自定义表情；如果平台发送失败，会在动作结果里记录失败原因。"
        return "自定义表情发送已经在本轮排队，无需重复调用。"


@dataclass
class QuoteReplyActionTool(FunctionTool[AstrAgentContext]):
    name: str = "quote_reply_action"
    description: str = (
        "引用某条 QQ 消息并发送一条短回复。"
        "当用户明确要求“引用那条回复”“回这条消息”时使用；message_id 为空时引用当前消息。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "要引用的消息 ID；为空则引用当前消息。", "maxLength": 80},
                "text": {"type": "string", "description": "引用回复正文，尽量简短。", "maxLength": 240},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        message_id = str(kwargs.get("message_id", "") or _current_message_id(event) or "").strip()
        text = str(kwargs.get("text", "") or "").strip()
        if not message_id:
            _record_tool_execution(event, self.name, status="failed")
            return "引用回复失败：没有可引用的 message_id。"
        if not text:
            _record_tool_execution(event, self.name, status="failed")
            return "引用回复失败：text 不能为空。"
        queued = _append_qq_action(
            event,
            PendingQQAction(
                action_type="quote_reply",
                target_id=str(event.get_sender_id() or "").strip(),
                group_id=str(event.get_group_id() or "").strip(),
                message_id=message_id,
                payload={"text": text},
            ),
        )
        if queued:
            return f"已准备引用消息 {message_id} 回复。"
        return f"引用消息 {message_id} 的回复已经在本轮排队。"


@dataclass
class QQMessageRecallLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "qq_message_recall_lookup"
    description: str = (
        "只读查询近期可撤回/可引用的消息候选，尤其是机器人刚发过的消息。"
        "用于撤回、引用、解释上一条回复前先确认 message_id。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "whose": {"type": "string", "description": "bot/current/sender/all，默认 bot。", "enum": ["bot", "current", "sender", "all"]},
                "count": {"type": "integer", "description": "最多返回多少条，1-20。", "minimum": 1, "maximum": 20},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        api = getattr(getattr(event, "bot", None), "api", None)
        if api is None:
            _record_tool_execution(event, self.name, status="failed")
            return "消息候选查询失败：NapCat 消息接口不可用。"
        whose = str(kwargs.get("whose", "bot") or "bot").strip()
        if whose not in {"bot", "current", "sender", "all"}:
            whose = "bot"
        try:
            count = max(1, min(int(kwargs.get("count", 8) or 8), 20))
        except (TypeError, ValueError):
            count = 8
        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        self_id = str(event.get_self_id() or "").strip()
        try:
            if group_id:
                payload = await api.call_action("get_group_msg_history", group_id=_coerce_api_id(group_id), message_seq=0, count=count * 3)
            else:
                payload = await api.call_action("get_friend_msg_history", user_id=_coerce_api_id(sender_id), message_seq=0, count=count * 3)
        except Exception as exc:
            _record_tool_execution(event, self.name, status="failed")
            return f"消息候选查询失败：读取历史消息出错：{exc}"
        messages = _history_messages(payload)
        if whose == "bot":
            messages = [item for item in messages if _message_sender_id(item) == self_id]
        elif whose in {"current", "sender"}:
            messages = [item for item in messages if _message_sender_id(item) == sender_id]
        if not messages:
            _record_tool_execution(event, self.name)
            return "消息候选查询结果：没有找到符合条件的近期消息。"
        lines = [f"消息候选查询结果：{len(messages)} 条候选，展示最近 {min(len(messages), count)} 条。"]
        for idx, message in enumerate(messages[-count:], start=1):
            lines.append(_format_message_summary(message, idx))
        _record_tool_execution(event, self.name)
        return "\n".join(lines)


@dataclass
class TopicThreadLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "topic_thread_lookup"
    description: str = (
        "只读查看当前会话的近期话题线索、focus 上下文和消息窗口。"
        "用于分辨“那个”“刚才说的”“这件事”指的是什么，避免串话题。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "要聚焦的话题关键词，可为空。", "maxLength": 80},
                "recent_count": {"type": "integer", "description": "返回最近多少条线索，1-12。", "minimum": 1, "maximum": 12},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        topic = str(kwargs.get("topic", "") or "").strip()
        try:
            recent_count = max(1, min(int(kwargs.get("recent_count", 6) or 6), 12))
        except (TypeError, ValueError):
            recent_count = 6
        lines: list[str] = []
        envelope = event.get_extra("astrmai_prompt_envelope", None) if hasattr(event, "get_extra") else None
        for label, attr in (
            ("当前焦点", "focus_message_text"),
            ("直接上下文", "direct_context_text"),
            ("相关上下文", "related_context_text"),
            ("环境背景", "ambient_background_text"),
        ):
            text = str(getattr(envelope, attr, "") or "").strip()
            if text:
                lines.append(f"{label}: {_truncate_text(text, 240)}")
        for key in ("astrmai_attention_window", "astrmai_recent_messages", "astrmai_window_lines"):
            value = event.get_extra(key, None) if hasattr(event, "get_extra") else None
            if isinstance(value, list):
                for item in value[-recent_count:]:
                    text = item if isinstance(item, str) else _message_text_preview(item, 160) if isinstance(item, dict) else str(item)
                    if text and (not topic or topic.casefold() in text.casefold()):
                        lines.append(f"近期线索: {_truncate_text(text, 180)}")
        if not lines:
            lines.append("话题线索查询结果：当前事件没有暴露可用的 focus/thread 上下文。")
        _record_tool_execution(event, self.name)
        return "\n".join(lines[: recent_count + 4])


@dataclass
class BotCapabilityLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "bot_capability_lookup"
    description: str = (
        "只读查询本轮模型可用的工具能力、工具族和已过滤/已要求的工具。"
        "用于模型不知道自己能不能查好友、发私聊、引用回复、发自定义表情时自检。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "needed_package": {
                    "type": "string",
                    "description": (
                        "如果当前工具不够用，可以填写希望系统二次开放的工具包名称；"
                        "可选：identity、relationship、artifact、memory_governance。"
                    ),
                }
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        turn_context = event.get_extra("astrmai_turn_context", None) if hasattr(event, "get_extra") else None
        tools_state = getattr(turn_context, "tools", None)
        available = list(getattr(tools_state, "available_tools", []) or [])
        filtered = list(getattr(tools_state, "filtered_tools", []) or [])
        required = list(getattr(tools_state, "required_tools", []) or [])
        package = normalize_requested_packages([kwargs.get("needed_package", "")])
        if package and hasattr(event, "set_extra"):
            existing = list(event.get_extra("astrmai_requested_tool_packages", []) or [])
            merged = []
            for item in [*existing, *package]:
                text = str(item or "").strip()
                if text and text not in merged:
                    merged.append(text)
            event.set_extra("astrmai_requested_tool_packages", merged)
        if not available:
            available = sorted(TOOL_CAPABILITIES)
        lines = [
            f"本轮可用工具数量：{len(available)}。",
            "可用工具：" + ", ".join(sorted(str(item) for item in available)[:40]),
            "可请求二次开放的只读工具包：" + ", ".join(
                name for name in ("identity", "relationship", "artifact", "memory_governance") if name in TOOL_PACKAGES
            ),
        ]
        if package:
            lines.append("已记录工具包二次开放请求：" + ", ".join(package))
        if filtered:
            lines.append("过滤后工具：" + ", ".join(sorted(str(item) for item in filtered)[:40]))
        if required:
            lines.append("显式要求工具：" + ", ".join(str(item) for item in required))
        _record_tool_execution(event, self.name)
        return "\n".join(lines)


@dataclass
class MemoryWriteCorrectionTool(FunctionTool[AstrAgentContext]):
    name: str = "memory_write_correction_tool"
    description: str = (
        "记录一条用户纠错：哪条记忆/说法不对，正确事实是什么。"
        "用于用户明确说“你记错了/不是这样/改成……”时。"
    )
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "wrong_fact": {"type": "string", "description": "需要纠正的错误事实。", "maxLength": 300},
                "correct_fact": {"type": "string", "description": "用户给出的正确事实。", "maxLength": 300},
                "reason": {"type": "string", "description": "纠错原因，可为空。", "maxLength": 160},
            },
            "required": ["wrong_fact", "correct_fact"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        wrong_fact = str(kwargs.get("wrong_fact", "") or "").strip()
        correct_fact = str(kwargs.get("correct_fact", "") or "").strip()
        reason = str(kwargs.get("reason", "") or "").strip()
        if not wrong_fact or not correct_fact:
            _record_tool_execution(event, self.name, status="failed")
            return "记忆纠错记录失败：wrong_fact 和 correct_fact 都不能为空。"
        report = {
            "wrong_fact": wrong_fact[:300],
            "correct_fact": correct_fact[:300],
            "reason": reason[:160],
            "sender_id": str(event.get_sender_id() or ""),
            "session_id": str(getattr(event, "unified_msg_origin", "") or ""),
            "created_at": time.time(),
        }
        _append_report_extra(event, "astrmai_memory_correction_reports", report)
        status = "success"
        if self.memory_engine is not None and hasattr(self.memory_engine, "record_cognitive_feedback"):
            try:
                await self.memory_engine.record_cognitive_feedback(
                    session_id=report["session_id"],
                    source="memory_correction_tool",
                    summary=f"用户纠正：{wrong_fact} -> {correct_fact}",
                    guidance=f"以后优先采用正确事实：{correct_fact}。{reason}",
                    tags=["memory_correction"],
                    importance=0.72,
                )
            except Exception as exc:
                status = "degraded"
                logger.debug(f"[MemoryWriteCorrectionTool] feedback write degraded: {exc}")
        _record_tool_execution(event, self.name, status=status)
        return "已记录记忆纠错；后续回答应以用户给出的正确事实为准。"


@dataclass
class UnverifiedReportRecordTool(FunctionTool[AstrAgentContext]):
    name: str = "unverified_report_record_tool"
    description: str = (
        "记录一条用户转述但尚未核实的报告。"
        "例如“别的群有人卖妃爱娃娃”，应标记为未核实，不能当成事实。"
    )
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "description": "用户转述的未核实说法。", "maxLength": 400},
                "source_hint": {"type": "string", "description": "来源线索，如群名/某人说/截图。", "maxLength": 160},
            },
            "required": ["claim"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        claim = str(kwargs.get("claim", "") or "").strip()
        source_hint = str(kwargs.get("source_hint", "") or "").strip()
        if not claim:
            _record_tool_execution(event, self.name, status="failed")
            return "未核实报告记录失败：claim 不能为空。"
        report = {
            "claim": claim[:400],
            "source_hint": source_hint[:160],
            "sender_id": str(event.get_sender_id() or ""),
            "session_id": str(getattr(event, "unified_msg_origin", "") or ""),
            "created_at": time.time(),
        }
        _append_report_extra(event, "astrmai_unverified_reports", report)
        status = "success"
        if self.memory_engine is not None and hasattr(self.memory_engine, "record_cognitive_feedback"):
            try:
                await self.memory_engine.record_cognitive_feedback(
                    session_id=report["session_id"],
                    source="unverified_report_tool",
                    summary=f"未核实报告：{claim}",
                    guidance="这是用户转述的未核实信息，回答时必须使用“不确定/需要确认”等措辞，不要断言为事实。",
                    tags=["unverified_report"],
                    importance=0.55,
                )
            except Exception as exc:
                status = "degraded"
                logger.debug(f"[UnverifiedReportRecordTool] feedback write degraded: {exc}")
        _record_tool_execution(event, self.name, status=status)
        return "已记录为未核实报告；之后回答时必须保持不确定措辞，不能当成已确认事实。"


@dataclass
class PersonaFactCheckTool(FunctionTool[AstrAgentContext]):
    name: str = "persona_fact_check_tool"
    description: str = (
        "查询 AstrMai 自己的人格/自述/授权类事实，判断用户说法是否有内部依据。"
        "适合确认“你有没有授权”“你是不是在某群”“你的人设里有没有这件事”。"
    )
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    memory_tool_service: Optional[Any] = Field(default=None, exclude=True)
    persona_id: str = Field(default="", exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "description": "要核对的说法。", "minLength": 1, "maxLength": 260},
            },
            "required": ["claim"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        claim = str(kwargs.get("claim", "") or "").strip()
        event = _get_current_event(context)
        if not claim:
            return "人格事实核对失败：claim 不能为空。"
        tool_service = self.memory_tool_service or getattr(self.memory_engine, "tool_service", None)
        if tool_service is None or not hasattr(tool_service, "self_lore_query"):
            _record_tool_execution(event, self.name, status="failed")
            return "人格事实核对失败：自我设定查询服务不可用。"
        try:
            lore = await tool_service.self_lore_query(query=claim, persona_id=self.persona_id, event=event, top_k=4, allow_stale=True)
            rendered = tool_service.render_result(lore)
        except Exception as exc:
            _record_tool_execution(event, self.name, status="failed")
            return f"人格事实核对失败：{exc}"
        _record_tool_execution(event, self.name)
        if not rendered:
            return "人格事实核对结果：内部人格/自述资料没有找到可确认依据。回答时应说未知或需要核实。"
        return "人格事实核对结果：找到相关内部资料，但仍需按资料内容谨慎表达。\n" + str(rendered)


@dataclass
class GroupActivitySnapshotTool(FunctionTool[AstrAgentContext]):
    name: str = "group_activity_snapshot_tool"
    description: str = (
        "只读获取当前群近期活跃快照：谁在说话、最近话题、消息数量。"
        "用于群聊里判断是否该插话、谁刚刚参与了话题。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "群号；为空时使用当前群。", "maxLength": 32},
                "count": {"type": "integer", "description": "读取最近多少条，1-50。", "minimum": 1, "maximum": 50},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        api = getattr(getattr(event, "bot", None), "api", None)
        group_id = str(kwargs.get("group_id", "") or event.get_group_id() or "").strip()
        if not group_id:
            _record_tool_execution(event, self.name, status="failed")
            return "群活跃快照失败：当前不是群聊，也没有提供 group_id。"
        if api is None:
            _record_tool_execution(event, self.name, status="failed")
            return "群活跃快照失败：NapCat 群消息接口不可用。"
        try:
            count = max(1, min(int(kwargs.get("count", 20) or 20), 50))
        except (TypeError, ValueError):
            count = 20
        try:
            payload = await api.call_action("get_group_msg_history", group_id=_coerce_api_id(group_id), message_seq=0, count=count)
        except Exception as exc:
            _record_tool_execution(event, self.name, status="failed")
            return f"群活跃快照失败：读取群 {group_id} 历史出错：{exc}"
        messages = _history_messages(payload)
        if not messages:
            _record_tool_execution(event, self.name)
            return f"群活跃快照：群 {group_id} 没有读取到近期消息。"
        senders: dict[str, int] = {}
        for msg in messages:
            key = _message_sender_name(msg) or _message_sender_id(msg) or "unknown"
            senders[key] = senders.get(key, 0) + 1
        ranked = sorted(senders.items(), key=lambda item: item[1], reverse=True)[:6]
        lines = [f"群活跃快照：群 {group_id} 最近 {len(messages)} 条消息。"]
        lines.append("活跃成员：" + "，".join(f"{name}({num})" for name, num in ranked))
        lines.append("最近消息：")
        for idx, msg in enumerate(messages[-6:], start=1):
            lines.append(_format_message_summary(msg, idx))
        _record_tool_execution(event, self.name)
        return "\n".join(lines)


@dataclass
class ContactRouteSuggestTool(FunctionTool[AstrAgentContext]):
    name: str = "contact_route_suggest_tool"
    description: str = (
        "只读判断当前请求更适合在当前会话回复、私聊好友、群里 @ 人，还是不能执行。"
        "跨会话发消息前可先调用，避免把人、群、发送位置搞错。"
    )
    db_service: Any = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "目标 QQ、好友备注、昵称或群友名称。", "maxLength": 80},
                "intent": {"type": "string", "description": "想做的事，如传话/询问/提醒/回复当前群。", "maxLength": 120},
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = _get_current_event(context)
        target = str(kwargs.get("target", "") or "").strip()
        intent = str(kwargs.get("intent", "") or "").strip()
        lines = [f"路由建议：intent={intent or '未说明'}。"]
        if not target:
            lines.append("没有目标人：建议直接在当前会话回复，不要跨会话发送。")
            _record_tool_execution(event, self.name)
            return "\n".join(lines)
        resolved, reason = await _resolve_friend_target(event, context.context.context, self.db_service, target)
        if resolved:
            target_id, display = resolved
            lines.append(f"目标是机器人好友：{display}({target_id})。")
            lines.append("建议：如果用户明确让你传话或联系对方，可调用 space_transition_action；否则先在当前会话确认。")
        else:
            group_id = str(event.get_group_id() or "").strip()
            if group_id:
                lines.append(f"未确认为机器人好友：{reason}")
                lines.append("建议：如果目标是当前群友，可用 construct_at_event 或当前群回复；不要声称能私聊对方。")
            else:
                lines.append(f"未确认为机器人好友：{reason}")
                lines.append("建议：无法跨会话发送；请让用户提供准确 QQ 或确认对方是机器人好友。")
        _record_tool_execution(event, self.name)
        return "\n".join(lines)


@dataclass
class SpaceTransitionTool(FunctionTool[AstrAgentContext]):
    name: str = "space_transition_action"
    description: str = (
        "跨越当前会话，向机器人好友列表中的指定 QQ 好友真实发送一条私聊消息。"
        "可用于你主动判断需要联系某人，也可作为传话人执行用户明确的转告请求。"
        "relay 模式会自动标明当前发起人的昵称，message 只填写需要转达的正文，不要重复添加来源前缀。"
        "目标必须是机器人好友；发送失败时必须把工具错误如实告诉当前发起会话，不得声称发送成功。"
    )
    db_service: Any = Field(default=None, exclude=True)
    handoff_store: Any = Field(default=None, exclude=True)
    runtime_coordinator: Any = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_name": {
                    "type": "string",
                    "description": "接收消息的机器人好友昵称、备注或准确 QQ 号。",
                    "minLength": 1,
                    "maxLength": 80,
                },
                "message": {
                    "type": "string",
                    "description": "要发送到目标好友私聊窗口的正文；relay 模式会自动添加“发起人让我转告你”的来源说明。",
                    "minLength": 1,
                    "maxLength": 800,
                },
                "context_summary": {
                    "type": "string",
                    "description": "供目标会话后续衔接使用的简短摘要，说明消息来源、人物关系和传话目的，不要混淆发起人与接收人。",
                    "minLength": 1,
                    "maxLength": 400,
                },
                "delivery_mode": {
                    "type": "string",
                    "description": "relay 表示受当前用户委托传话；proactive 表示机器人自主联系。",
                    "enum": ["relay", "proactive"],
                },
                "origin_reply": {
                    "type": "string",
                    "description": "发送成功后准备在当前发起会话中回复的简短确认语，可留空。",
                    "maxLength": 200,
                },
            },
            "required": ["target_name", "message", "context_summary", "delivery_mode"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        target_name = str(kwargs.get("target_name", "") or "").strip()
        private_message = str(kwargs.get("message", kwargs.get("private_message", "")) or "").strip()
        context_summary = str(kwargs.get("context_summary", "") or "").strip()
        delivery_mode = str(kwargs.get("delivery_mode", "relay") or "relay").strip().lower()
        origin_reply = str(kwargs.get("origin_reply", kwargs.get("cover_message", "")) or "").strip()
        current_event = _get_current_event(context)
        if not target_name or not private_message or not context_summary:
            _record_tool_execution(current_event, self.name, status="failed")
            return "发送失败：target_name、message 和 context_summary 都不能为空，消息未发送。"
        if delivery_mode not in {"relay", "proactive"}:
            _record_tool_execution(current_event, self.name, status="failed")
            return "发送失败：delivery_mode 只能是 relay 或 proactive，消息未发送。"

        astr_ctx = context.context.context
        resolved, error = await _resolve_friend_target(
            current_event,
            astr_ctx,
            self.db_service,
            target_name,
        )
        if not resolved:
            _record_tool_execution(current_event, self.name, status="failed")
            return f"发送失败：{error}，消息未发送。"
        target_id, display_name = resolved
        if target_id == str(current_event.get_self_id() or "").strip():
            _record_tool_execution(current_event, self.name, status="failed")
            return "发送失败：不能向机器人自己发送跨会话消息。"

        source_umo = str(getattr(current_event, "unified_msg_origin", "") or "")
        platform_id = source_umo.split(":", 1)[0] if ":" in source_umo else "default"
        target_umo = f"{platform_id}:FriendMessage:{target_id}"
        source_sender_name = str(current_event.get_sender_name() or "当前用户").strip() or "当前用户"
        outbound_message = private_message
        if delivery_mode == "relay":
            outbound_message = f"{source_sender_name}让我转告你：{private_message}"

        sends = current_event.get_extra("astrmai_cross_session_sends", [])
        sends = list(sends) if isinstance(sends, list) else []
        if any(
            str(item.get("target_id") or "") == target_id
            and str(item.get("message") or "") == outbound_message
            for item in sends
            if isinstance(item, dict)
        ):
            return f"消息已在本轮发送给 {display_name}，不再重复发送。"
        if not hasattr(astr_ctx, "send_message"):
            _record_tool_execution(current_event, self.name, status="failed")
            return "发送失败：AstrBot 跨会话发送接口不可用，消息未发送。"

        if self.runtime_coordinator is not None:
            turn = current_event.get_extra("astrmai_turn_identity", None)
            try:
                is_current = await self.runtime_coordinator.is_current_turn(turn)
            except Exception as exc:
                logger.warning(
                    "[SpaceTransitionTool] turn freshness check failed: %s",
                    exc,
                )
                is_current = False
            if not is_current:
                _record_tool_execution(current_event, self.name, status="failed")
                return "发送取消：当前请求已经过期或插件正在重载，消息未发送。"

        try:
            send_result = await astr_ctx.send_message(
                target_umo,
                MessageChain().message(outbound_message),
            )
        except Exception as exc:
            logger.warning(f"[SpaceTransitionTool] cross-session send failed: {exc}")
            _record_tool_execution(current_event, self.name, status="failed")
            return f"发送失败：向 {display_name} 的私聊发送时出错：{exc}。"
        if send_result is False:
            logger.warning(
                "[SpaceTransitionTool] cross-session send returned false target=%s",
                target_id,
            )
            _record_tool_execution(current_event, self.name, status="failed")
            return f"发送失败：AstrBot 未能把消息送入 {display_name} 的私聊，消息未发送。"

        sends.append({"target_id": target_id, "target_umo": target_umo, "message": outbound_message})
        current_event.set_extra("astrmai_cross_session_sends", sends[-8:])
        if self.handoff_store is not None:
            try:
                handoff = CrossSessionHandoff(
                    platform_id=platform_id,
                    source_umo=source_umo,
                    source_sender_id=str(current_event.get_sender_id() or ""),
                    source_sender_name=source_sender_name,
                    target_umo=target_umo,
                    target_id=target_id,
                    target_name=display_name,
                    outbound_message=outbound_message,
                    context_summary=context_summary,
                    delivery_mode=delivery_mode,
                )
                await self.handoff_store.put(handoff)
                if delivery_mode == "relay":
                    await self.handoff_store.complete_for_recipient(
                        platform_id,
                        str(current_event.get_sender_id() or ""),
                    )
                logger.info(
                    "[SpaceTransitionTool] handoff stored id=%s target=%s",
                    handoff.handoff_id,
                    target_id,
                )
            except Exception as exc:
                logger.warning(
                    "[SpaceTransitionTool] message delivered but handoff storage failed: %s",
                    exc,
                )
        else:
            shared_dict = getattr(astr_ctx, "shared_dict", None)
            if isinstance(shared_dict, dict):
                jumps = dict(shared_dict.get("astrmai_space_jumps", {}) or {})
                jumps[target_id] = {
                    "timestamp": time.time(),
                    "source_umo": source_umo,
                    "group_id": str(current_event.get_group_id() or ""),
                    "source_sender_id": str(current_event.get_sender_id() or ""),
                    "source_sender_name": source_sender_name,
                    "target_id": target_id,
                    "target_name": display_name,
                    "private_message": outbound_message,
                    "context_summary": context_summary,
                    "delivery_mode": delivery_mode,
                }
                shared_dict["astrmai_space_jumps"] = jumps
            else:
                logger.warning(
                    "[SpaceTransitionTool] message delivered without cross-session handoff store"
                )
        _record_tool_execution(current_event, self.name)
        confirmation = origin_reply or f"我已经把消息发给 {display_name} 了。"
        return f"发送成功：消息已进入 {display_name} 的私聊会话。请在当前发起会话中自然确认：{confirmation}"


@dataclass
class RegretAndWithdrawTool(FunctionTool[AstrAgentContext]):
    name: str = "regret_and_withdraw_action"
    description: str = "当你决定撤回或强烈后悔上一条发言时调用。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        message_id = await _resolve_latest_bot_message_id(current_event)
        if not message_id:
            return "动作取消：没有找到可撤回的上一条机器人消息。"
        pending_actions = _get_pending_actions(current_event)
        if not any(item.get("action") == "withdraw" for item in pending_actions):
            _append_qq_action(
                current_event,
                PendingQQAction(action_type="withdraw", message_id=message_id),
            )
        return "已将撤回上一条 AstrMai 回复加入待执行动作；不要声称已经成功，请继续生成简短自然的补救文本。"


@dataclass
class MessageReactionTool(FunctionTool[AstrAgentContext]):
    name: str = "message_reaction_action"
    description: str = "只调整最终文字回复的互动语气，不执行 QQ 原生消息表情或点赞。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "reaction": {"type": "string", "description": "要在最终文字里体现的互动反应。", "minLength": 1, "maxLength": 80}
            },
            "required": ["reaction"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        reaction = str(kwargs.get("reaction", "") or "").strip()
        if not reaction:
            return "执行失败：reaction 不能为空。"
        _record_tool_execution(_get_current_event(context), self.name)
        return f"请在最终文字回复中自然体现“{reaction}”的互动语气，不要声称执行了 QQ 动作。"


@dataclass
class ProactiveLikeTool(FunctionTool[AstrAgentContext]):
    name: str = "proactive_like_action"
    description: str = "只调整最终文字回复中的夸奖或好感表达，不执行 QQ 资料点赞。"
    db_service: Any = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_name": {"type": "string", "description": "要在文字中表达好感的目标，可为空。", "maxLength": 80}
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        target_name = str(kwargs.get("target_name", "") or "").strip()
        target_id = str(current_event.get_sender_id())
        if target_name:
            resolved = await _resolve_target(
                self.db_service,
                target_name=target_name,
                current_event=current_event,
                astr_ctx=context.context.context,
            )
            if resolved:
                target_id, _ = resolved
        display_name = target_name or (current_event.get_sender_name() or "对方")
        _record_tool_execution(current_event, self.name)
        return f"请在最终文字回复中自然表达对 {display_name} 的夸奖或好感，不要声称执行了 QQ 点赞。"


@dataclass
class SelfLoreQueryTool(FunctionTool[AstrAgentContext]):
    name: str = "self_lore_query"
    description: str = "查询当前 persona 的自我设定、世界观或既有自述。"
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    memory_tool_service: Optional[Any] = Field(default=None, exclude=True)
    persona_id: str = Field(default="", exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查询的自我设定关键词。", "minLength": 1, "maxLength": 240}
            },
            "required": ["query"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query", "") or "").strip()
        if not query:
            return "执行失败：query 不能为空。"
        if not self.memory_engine and not self.memory_tool_service:
            return "系统提示：当前没有可用的自我设定记忆引擎。"
        current_event = _get_current_event(context)
        try:
            tool_service = self.memory_tool_service or getattr(self.memory_engine, "tool_service", None)
            if tool_service and hasattr(tool_service, "self_lore_query"):
                tool_result = await tool_service.self_lore_query(
                    query=query,
                    persona_id=self.persona_id,
                    event=current_event,
                )
                result = tool_service.render_result(tool_result)
            else:
                result = None
        except Exception as exc:
            logger.debug(f"[SelfLoreQueryTool] recall failed: {exc}")
            _record_tool_execution(current_event, self.name, status="failed")
            return "系统提示：自我设定查询暂时失败。"
        _record_tool_execution(current_event, self.name)
        if not result:
            return "系统提示：当前没有检索到相关自我设定。"
        return str(result)


__all__ = [
    "BotCapabilityLookupTool",
    "ContactRouteSuggestTool",
    "CrossChatMemoryQueryTool",
    "CrossSessionReplyLookupTool",
    "CustomFaceCatalogQueryTool",
    "ConstructAtEventTool",
    "GroupSignTool",
    "MemeResonanceTool",
    "MemoryWriteCorrectionTool",
    "MessageEmojiLikeTool",
    "MessageReactionTool",
    "OmniPerceptionTool",
    "PersonaFactCheckTool",
    "ProactiveLikeTool",
    "ProactiveMemeTool",
    "ProactivePokeTool",
    "QQCustomFaceSendTool",
    "QQFriendLookupTool",
    "QQForwardMessageLookupTool",
    "QQGroupMemberLookupTool",
    "QQGroupPresenceLookupTool",
    "QQMessageArtifactLookupTool",
    "QQMessageRecallLookupTool",
    "QQRecentContactLookupTool",
    "QQUserIdentityLookupTool",
    "prepare_explicit_tool_fallbacks",
    "QuoteReplyActionTool",
    "RegretAndWithdrawTool",
    "SelfLoreQueryTool",
    "SpaceTransitionTool",
    "TopicHijackTool",
    "TopicThreadLookupTool",
    "UnverifiedReportRecordTool",
    "VisionMessageAnalyzeTool",
    "WaitTool",
]
