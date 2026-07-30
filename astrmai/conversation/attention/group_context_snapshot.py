from __future__ import annotations

import re
from dataclasses import dataclass, field

from .group_dialogue_store import GroupDialogueStore


_INCIDENT_LABELS = {
    "boundary_violation": "边界冒犯",
    "insult": "辱骂",
    "conflict": "冲突",
    "promise": "承诺",
    "apology": "道歉",
    "reconciliation": "和解",
}

_STANCE_LABELS = {
    "reject": "拒绝",
    "negative": "不接受",
    "warn": "警告",
    "accept": "接受",
    "comfort": "安慰",
    "neutral": "平静回应",
}


def classify_group_social_signal(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if not normalized:
        return ""
    if any(
        token in normalized
        for token in (
            "肉棒",
            "鸡巴",
            "操你",
            "强奸",
            "给你草",
            "给你肏",
            "脱光",
            "床上等我",
        )
    ):
        return "boundary_violation"
    if any(token in normalized for token in ("对不起", "抱歉", "我错了", "别生气")):
        return "apology"
    if any(token in normalized for token in ("没事了", "原谅你", "和好", "不生气了")):
        return "reconciliation"
    if any(token in normalized for token in ("傻逼", "废物", "滚", "恶心死了", "去死")):
        return "insult"
    if any(token in normalized for token in ("说好了", "答应你", "保证", "一定会")):
        return "promise"
    if any(token in normalized for token in ("你怎么了", "为什么骂我", "不理我", "生气")):
        return "conflict"
    return ""


def is_group_direct_correction(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if not normalized:
        return False
    return bool(
        normalized.startswith(("不是，", "不是,", "不对，", "不对,", "说错了"))
        or any(
            token in normalized
            for token in (
                "我是问",
                "我说的是",
                "我的意思是",
                "刚才那句",
                "前面那句",
                "重新问",
                "更正一下",
            )
        )
    )


@dataclass(slots=True)
class GroupContextSnapshot:
    text: str = ""
    watermark: int = 0
    candidate_count: int = 0
    selected_count: int = 0
    actor_tail_count: int = 0
    pending_direct_count: int = 0
    bot_turn_count: int = 0
    social_incident_count: int = 0
    echo_filtered_count: int = 0
    topic_bridge: bool = False
    exclusion_reasons: list[str] = field(default_factory=list)

    def trace_payload(self) -> dict:
        return {
            "watermark": self.watermark,
            "candidate_count": self.candidate_count,
            "selected_count": self.selected_count,
            "actor_tail_count": self.actor_tail_count,
            "pending_direct_count": self.pending_direct_count,
            "bot_turn_count": self.bot_turn_count,
            "social_incident_count": self.social_incident_count,
            "echo_filtered_count": self.echo_filtered_count,
            "topic_bridge": self.topic_bridge,
            "exclusion_reasons": list(self.exclusion_reasons),
            "text_chars": len(self.text),
        }


class GroupContextSnapshotBuilder:
    def __init__(
        self,
        store: GroupDialogueStore,
        *,
        actor_tail_ttl_sec: float = 1200.0,
        actor_tail_max_segments: int = 8,
        pending_direct_ttl_sec: float = 1200.0,
        social_incident_ttl_sec: float = 1800.0,
        max_chars: int = 5500,
    ) -> None:
        self.store = store
        self.actor_tail_ttl_sec = max(60.0, float(actor_tail_ttl_sec or 1200.0))
        self.actor_tail_max_segments = max(1, int(actor_tail_max_segments or 8))
        self.pending_direct_ttl_sec = max(60.0, float(pending_direct_ttl_sec or 1200.0))
        self.social_incident_ttl_sec = max(60.0, float(social_incident_ttl_sec or 1800.0))
        self.max_chars = max(800, int(max_chars or 5500))

    @staticmethod
    def _preview(text: str, limit: int = 220) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(1, limit - 3)] + "..."

    async def build(
        self,
        chat_id: str,
        *,
        current_sender_id: str,
        current_sender_name: str = "",
        topic_epoch: int = 0,
        now: float | None = None,
        watermark: int = 0,
    ) -> GroupContextSnapshot:
        actor_tail = await self.store.get_actor_tail(
            chat_id,
            current_sender_id=current_sender_id,
            ttl_seconds=self.actor_tail_ttl_sec,
            max_items=self.actor_tail_max_segments,
            now=now,
        )
        pending = await self.store.get_pending_direct_items(
            chat_id,
            current_sender_id=current_sender_id,
            ttl_seconds=self.pending_direct_ttl_sec,
            now=now,
        )
        bot_turns = await self.store.get_recent_bot_turns(
            chat_id,
            target_sender_id=current_sender_id,
            ttl_seconds=self.actor_tail_ttl_sec,
            max_items=4,
            now=now,
        )
        incidents = await self.store.get_social_incidents(
            chat_id,
            current_sender_id=current_sender_id,
            ttl_seconds=self.social_incident_ttl_sec,
            now=now,
        )
        echo_filtered = await self.store.count_recent_bot_echoes(
            chat_id,
            ttl_seconds=self.actor_tail_ttl_sec,
            now=now,
        )
        candidate_count = len(actor_tail) + len(pending) + len(bot_turns) + len(incidents)
        if candidate_count <= 0 and echo_filtered <= 0:
            return GroupContextSnapshot(
                watermark=max(0, int(watermark or 0)),
                echo_filtered_count=echo_filtered,
            )

        actor_label = str(current_sender_name or current_sender_id or "当前发言人").strip()
        lines = [
            "群聊因果上下文（严格按 QQ 归属，不把其他群友当成当前发言人）：",
            f"- 当前发言人：{actor_label}（QQ: {current_sender_id}）",
        ]
        selected_count = 0
        exclusion_reasons: list[str] = []

        if pending:
            lines.append("- 当前仍待回答的直接消息：")
            for item in pending[-3:]:
                lines.append(f"  - {actor_label}：{self._preview(item.content)}")
                selected_count += 1

        if bot_turns:
            lines.append("- Bot 对当前发言人的最近回应：")
            for turn in bot_turns[-3:]:
                stance = _STANCE_LABELS.get(turn.stance, turn.stance or "已回应")
                lines.append(
                    f"  - Bot 对 {actor_label} 的上一轮回应（{stance}）："
                    f"{self._preview(turn.reply_text)}"
                )
                selected_count += 1

        if incidents:
            lines.append("- 当前发言人的未解决社交事件：")
            for incident in incidents[-3:]:
                kind = _INCIDENT_LABELS.get(incident.kind, incident.kind)
                stance = _STANCE_LABELS.get(incident.stance, incident.stance)
                suffix = f"，Bot 立场：{stance}" if stance else ""
                lines.append(f"  - {actor_label} 触发了「{kind}」{suffix}；尚未解决。")
                selected_count += 1

        visible_actor_tail = [
            segment
            for segment in actor_tail
            if segment.provenance != "bot_echo"
        ]
        if visible_actor_tail:
            lines.append("- 当前发言人自己的近期消息（可跨短期话题代次承接）：")
            for segment in visible_actor_tail[-self.actor_tail_max_segments :]:
                lines.append(
                    f"  - [话题 {max(0, int(segment.topic_epoch or 0))}] "
                    f"{actor_label}：{self._preview(segment.content)}"
                )
                selected_count += 1
        if echo_filtered:
            exclusion_reasons.append("bot_echo_not_used_as_user_stance")
            lines.append(
                f"- 已过滤 {echo_filtered} 条群友复读 Bot 原话；"
                "它们不能证明复读者支持该立场，也不能改变事件归属。"
            )

        topic_bridge = any(
            int(segment.topic_epoch or 0) not in {0, int(topic_epoch or 0)}
            for segment in actor_tail
        ) or any(
            int(turn.topic_epoch or 0) not in {0, int(topic_epoch or 0)}
            for turn in bot_turns
        )
        lines.append(
            "- 使用规则：优先回答当前发言人的直接问题；"
            "共享群聊历史只提供背景，不能覆盖上述人物归属、Bot 立场与未解决事件。"
        )
        text = "\n".join(lines).strip()
        if len(text) > self.max_chars:
            text = text[: self.max_chars - 16].rstrip() + "\n[因果上下文已截断]"
            exclusion_reasons.append("snapshot_char_budget")

        return GroupContextSnapshot(
            text=text,
            watermark=max(0, int(watermark or 0)),
            candidate_count=candidate_count,
            selected_count=selected_count,
            actor_tail_count=len(visible_actor_tail),
            pending_direct_count=len(pending),
            bot_turn_count=len(bot_turns),
            social_incident_count=len(incidents),
            echo_filtered_count=echo_filtered,
            topic_bridge=topic_bridge,
            exclusion_reasons=exclusion_reasons,
        )
