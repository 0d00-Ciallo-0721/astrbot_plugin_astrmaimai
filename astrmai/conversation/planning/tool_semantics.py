from __future__ import annotations

from dataclasses import dataclass

from .tool_contracts import TOOL_CAPABILITIES


@dataclass(frozen=True, slots=True)
class ToolPlannerSemantics:
    """Compact planner-facing meaning for one canonical tool.

    This is intentionally not an execution schema.  It tells the cognitive
    planner when a capability may be useful and how its result continues the
    turn, while all lifecycle/parameter/platform checks remain in execution.
    """

    name: str
    purpose: str
    use_when: str
    result_mode: str
    continuation_mode: str
    target_hint: str = ""
    context_hint: str = ""


_READ = "internal_observation"
_CARDS = {
    "wait_and_listen": ("等待并保持倾听", "应保持沉默或等待更多上下文", "control", "end_turn", "当前会话"),
    "omni_perception_query": ("查询相关记忆与上下文", "需要内部事实才能继续推理", _READ, "continue_text", "当前会话"),
    "self_lore_query": ("查询角色自身设定", "问题涉及人格设定事实", _READ, "continue_text", "角色设定"),
    "learned_language_lookup": ("查询已学习的群聊用语", "需要理解群内黑话或表达", _READ, "continue_text", "当前群聊"),
    "qq_friend_lookup": ("查询机器人好友事实", "需要确认好友身份或可联系对象", _READ, "continue_text", "机器人好友"),
    "qq_group_member_lookup": ("查询当前群成员", "需要确认群成员目标", _READ, "continue_text", "当前群"),
    "qq_user_identity_lookup": ("查询 QQ 用户身份", "需要确认数字账号对应的人", _READ, "continue_text", "QQ 用户"),
    "qq_forward_message_lookup": ("读取合并转发内容", "需要理解转发消息中的事实", _READ, "continue_text", "转发消息"),
    "qq_group_presence_lookup": ("查询群关系与成员状态", "需要确认群内关系事实", _READ, "continue_text", "当前群"),
    "qq_recent_contact_lookup": ("查询最近联系人", "需要从最近上下文解析联系人", _READ, "continue_text", "最近联系人"),
    "qq_message_artifact_lookup": ("读取消息附件与结构", "需要确认消息中的图片、引用或节点", _READ, "continue_text", "当前消息"),
    "vision_message_analyze_tool": ("理解当前图片内容", "回答依赖图片事实", _READ, "continue_text", "当前图片", "需要图片上下文"),
    "cross_session_reply_lookup": ("查询跨会话回复关联", "需要恢复跨会话上下文", _READ, "continue_text", "关联会话"),
    "quote_reply_action": ("发送引用消息并回复", "需要引用具体消息回应", "qq_side_effect", "direct_message_sent", "已绑定消息", "私聊或群聊"),
    "qq_message_recall_lookup": ("回溯历史消息", "需要查找此前消息事实", _READ, "continue_text", "历史消息"),
    "topic_thread_lookup": ("查询话题线索", "需要恢复当前话题脉络", _READ, "continue_text", "当前话题"),
    "bot_capability_lookup": ("自省当前可用能力", "需要确认工具是否已披露", _READ, "continue_text", "当前运行时"),
    "memory_write_correction_tool": ("修正长期记忆", "可靠事实明确纠正旧记忆", "memory_write", "continue_text", "长期记忆"),
    "unverified_report_record_tool": ("记录未核实说法", "需要保留不确定信息但不能当成事实", "memory_write", "continue_text", "长期记忆"),
    "persona_fact_check_tool": ("核查人格事实", "需要确认角色设定中的人物事实", _READ, "continue_text", "角色设定"),
    "group_activity_snapshot_tool": ("读取群聊活动快照", "需要判断当前群聊活跃度", _READ, "continue_text", "当前群"),
    "contact_route_suggest_tool": ("解析联系人发送路线", "需要判断是否可跨会话联系", _READ, "continue_text", "机器人好友"),
    "cross_chat_memory_query": ("查询跨聊天记忆", "需要跨会话内部事实", _READ, "continue_text", "关联会话"),
    "construct_at_event": ("在群聊中 @目标并回复", "应提醒明确的群成员", "message", "optional_text", "群成员", "群聊"),
    "proactive_poke": ("向目标发送戳一戳", "互动气氛适合轻量触碰", "qq_side_effect", "optional_text", "当前用户或可解析目标"),
    "proactive_meme": ("发送表情包", "表情能自然承接当前情绪", "message", "optional_text", "当前会话"),
    "meme_resonance_action": ("复读已有群消息", "已有消息适合形成复读共鸣", "control", "end_turn", "已有群消息", "群聊"),
    "topic_hijack_action": ("主动切换回答话题", "当前方向不合适且有自然替代话题", "control", "continue_text", "当前会话"),
    "space_transition_action": ("向机器人好友发送跨会话私聊", "需要自主联系或受托传话且目标可确认", "cross_session_message", "direct_message_sent_then_confirm", "机器人好友"),
    "regret_and_withdraw_action": ("撤回机器人上一条消息", "上一条机器人消息需要撤回或修正", "qq_side_effect", "optional_text", "上一条机器人消息"),
    "message_emoji_reaction_action": ("给具体 QQ 消息贴原生表情", "需要对消息做轻量非文本回应", "qq_side_effect", "optional_text", "已绑定消息"),
    "proactive_like_action": ("给好友发送 QQ 点赞", "关系和语境适合轻量互动", "qq_side_effect", "optional_text", "当前用户或好友"),
}


TOOL_PLANNER_SEMANTICS: dict[str, ToolPlannerSemantics] = {
    name: ToolPlannerSemantics(
        name=name,
        purpose=values[0],
        use_when=values[1],
        result_mode=values[2],
        continuation_mode=values[3],
        target_hint=values[4],
        context_hint=values[5] if len(values) > 5 else "",
    )
    for name, values in _CARDS.items()
}


def planner_semantics_for(tool_name: str) -> ToolPlannerSemantics | None:
    return TOOL_PLANNER_SEMANTICS.get(str(tool_name or "").strip())


def build_planner_capability_catalog(*, is_group: bool, has_image: bool) -> str:
    """Build a compact, context-aware catalog without exposing JSON schemas."""
    lines: list[str] = []
    for name, spec in TOOL_PLANNER_SEMANTICS.items():
        capability = TOOL_CAPABILITIES.get(name)
        if capability is None:
            continue
        if (is_group and "group" not in capability.contexts) or (not is_group and "private" not in capability.contexts):
            continue
        if spec.context_hint == "需要图片上下文" and not has_image:
            continue
        lines.append(
            f"{name} | {capability.family} | {spec.purpose} | {spec.result_mode} | {spec.continuation_mode}"
            f" | target={spec.target_hint or 'context'} | context={spec.context_hint or 'current'}"
        )
    return "\n".join(lines)


def validate_planner_semantics() -> tuple[set[str], set[str]]:
    canonical = set(TOOL_CAPABILITIES)
    described = set(TOOL_PLANNER_SEMANTICS)
    return canonical - described, described - canonical


__all__ = [
    "ToolPlannerSemantics",
    "TOOL_PLANNER_SEMANTICS",
    "planner_semantics_for",
    "build_planner_capability_catalog",
    "validate_planner_semantics",
]
