"""阶段05 集成验收：把 5 段真实「上一句明明相关，下一轮像失忆」的对话脱敏后做确定性正向回放。

每条用例都驱动真实生产代码：
  * 车道历史（写入 / 读取 / prefix_hash 轮换）——LaneManager.append_exchange、
    LaneManager.ensure_lane、LaneHistoryMixin._build_rotation_seed、
    LaneTranscriptMixin.get_recent_transcript；
  * 话题连续性（轮次记录、话题锚点、跨话题桥接）——ConversationContinuityStore.record /
    update_topic_anchor / topic_anchor_prompt / evaluate_topic_bridge、BridgeDecision.prompt_text；
  * 最终提示词组装——PromptRefiner.refine_prompt + PromptEnvelope.sanitize_derived_context、
    PlannerPromptContextMixin._should_include_recent_transcript。

所有时间戳都用显式 now=（NOW / NOW+n），不依赖挂钟；文本为中文且已脱敏（假 id：u-alice、bot-1、g-1）。
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.contracts.topic_attention_anchor import (
    ANCHOR_MAX_EVENT_IDS,
    ANCHOR_MAX_OPEN_LOOP_CHARS,
    ANCHOR_MAX_PARTICIPANTS,
    ANCHOR_MAX_PROMPT_CHARS,
    ANCHOR_MAX_SERIALIZED_CHARS,
    ANCHOR_MAX_SUBJECT_CHARS,
    ANCHOR_SOURCE_VALUES,
)
from astrmai.conversation.contracts.topic_bridge import (
    BRIDGE_MAX_EVENT_IDS,
    BRIDGE_MAX_PROMPT_CHARS,
    BRIDGE_MAX_TURNS,
    BridgeDecision,
)
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin
from astrmai.conversation.planning.prompt_refiner import PromptRefiner
from astrmai.infrastructure.runtime.lane_history import LaneHistoryMixin
from astrmai.infrastructure.runtime.lane_manager import LaneKey, LaneManager
from tests.original_ported.helpers import _FakeConversationManager

# 会话键：与 event.unified_msg_origin 同形（群号已脱敏成 g-1）。
CHAT = "default:GroupMessage:g-1"
GROUP_SCOPE_ID = "g-1"
NOW = 1_700_000_000.0

DERIVED_BLOCK_RE = re.compile(
    r'<untrusted_context type="derived" source="(?P<source>[^"]*)" trusted="false">\n'
    r"(?P<body>.*?)\n</untrusted_context>",
    re.DOTALL,
)

# ── 脱敏回放数据（真实对话脚本，不含任何真实发送者 id）────────────────────────
REPLAY_FIXTURES = {
    "plain_continuous": {
        "turns": [
            {"event_id": "e-c1-u1", "actor_id": "u-alice", "role": "user", "text": "人还好，就是什么都不记得了"},
            {
                "event_id": "e-c1-a1",
                "actor_id": "bot-1",
                "role": "assistant",
                "text": "先确认一下，你是不记得最近发生的事情，还是更早的经历？",
            },
            {"event_id": "e-c1-u2", "actor_id": "u-alice", "role": "user", "text": "就是刚才那段"},
        ],
        "topic_epoch": 1,
    },
    "prefix_rotation": {
        # 轮换前的旧车道：3 组占位对话 + 1 组必须被带进新车道的最新完整问答。
        "filler_pairs": [
            ("最近工作有点累", "累的是工作本身，还是环境？"),
            ("也可能是节奏问题", "那可以先把手头上的事排个顺序。"),
            ("嗯，先看看机会", "有合适的机会再动也不迟。"),
        ],
        "latest_pair": ("我在考虑换工作", "你最在意薪资、环境还是发展？"),
        "next_user_text": "主要是环境",
        "prefix_before": "prefix-a",
        "prefix_after": "prefix-b",
    },
    "topic_anchor": {
        "event_id": "e-c3-cat",
        "actor_id": "u-alice",
        "user_text": "小锦家的那只猫又跑出去了",
        "assistant_event_id": "e-c3-cat-reply",
        "assistant_actor_id": "bot-1",
        "assistant_text": "它上次也是从窗户出去的吗？",
        "flood_count": 20,
        "follower_text": "它每次都挑这个点跑",
        "next_user_text": "在窗台那边蹲着",
    },
    "cross_topic_bridge": {
        "source_epoch": 1,
        "target_epoch": 2,
        "seeded_event_ids": ["e-c4-src-1"],
        "source_pair": {
            "actor_id": "u-alice",
            "user_text": "外星肉包章鱼人是什么？",
            "event_id": "e-c4-src-1",
            "assistant_text": "这个我还没弄清，是你们的梗吗？",
        },
        "current_event_id": "e-c4-cur",
        "current_actor_id": "u-alice",
        "current_text": "回复上一条：就是小锦家里那个",
        "reply_target_event_id": "e-c4-src-1",
        "rotation_reason": "new_topic",
    },
    "warm_plus_recent": {
        "user_text": "那他后来答应了吗？",
        "pair_count": 9,
        "question_text": "问：他到底有没有当场答应帮我们把这批东西搬过去，我这边一直没等到明确回复。",
        "answer_text": "答：这个我也没听到准话，得再找他确认一下当时的情况和后面的具体安排。",
        "padding": "他一直没给准话我这边也不好意思催别人太多次",
        "warm_summary": (
            "更早的脉络：用户一直在追问某个人会不会答应帮忙搬东西，"
            "对方当时没有给出明确答复，只说再看看。"
        ),
        "legacy_warm_line": "用户: 更早的一句独立旧话",
        "legacy_warm_event_id": "e-c5-legacy",
        "shared_question_event_id": "e-c5-shared-q",
        "shared_answer_event_id": "e-c5-shared-a",
    },
}

RECENT_MARKER = "---对话记录"
ANCHOR_MARKER = "---话题注意力锚点"
BRIDGE_MARKER = "---跨话题桥接"
WARM_MARKER = "---近期对话脉络"
ANCHOR_SOURCE = "topic_attention_anchor"
BRIDGE_SOURCE = "cross_topic_bridge"
ROTATION_PREFIX = LaneHistoryMixin.ROTATION_SUMMARY_PREFIX


class _PlannerContext(PlannerPromptContextMixin):
    """只提供 _should_include_recent_transcript 所需的 mixin 上下文。"""


def _event(text: str):
    """PromptRefiner 需要的最小 event 面（与 stage04 配方一致）。"""

    class Event:
        message_str = text
        unified_msg_origin = CHAT

        def __init__(self):
            self.extras = {"retrieve_keys": []}

        def get_extra(self, key, default=None):
            return self.extras.get(key, default)

        def set_extra(self, key, value):
            self.extras[key] = value

    return Event()


def _refine(focus_text: str, envelope: PromptEnvelope) -> tuple[str, str, PromptEnvelope]:
    refiner = PromptRefiner(memory_engine=None)
    system, rendered = asyncio.run(
        refiner.refine_prompt(
            event=_event(focus_text),
            system_prompt="stable system prompt",
            prompt="",
            context={"disable_rag_injection": True},
            prompt_envelope=envelope,
        )
    )
    return system, rendered, envelope


def _canonical_event(
    event_id: str,
    text: str,
    *,
    actor_id: str = "u-alice",
    topic_epoch: int = 1,
    is_bot: bool = False,
    reply_target_event_id: str = "",
    quote_event_id: str = "",
) -> dict:
    """锚点/桥接接受 Mapping 形态的规范化事件（ConversationContinuityStore._anchor_event_value）。"""
    return {
        "event_id": event_id,
        "chat_id": CHAT,
        "actor_id": actor_id,
        "visible_text": text,
        "topic_epoch": topic_epoch,
        "is_bot": is_bot,
        "reply_target_event_id": reply_target_event_id,
        "quote_event_id": quote_event_id,
    }


def _lane_manager() -> LaneManager:
    return LaneManager(_FakeConversationManager(), config=SimpleNamespace())


def _dialog_lane_key() -> LaneKey:
    return LaneKey(subsystem="sys2", task_family="dialog", scope_id=GROUP_SCOPE_ID)


def _derived_blocks(rendered: str) -> list[tuple[str, str]]:
    return [(m.group("source"), m.group("body")) for m in DERIVED_BLOCK_RE.finditer(rendered)]


def _strip_derived_blocks(rendered: str) -> str:
    return DERIVED_BLOCK_RE.sub("", rendered)


def _auxiliary_rendered_chars(envelope: PromptEnvelope) -> int:
    return (
        envelope.recent_context_rendered_chars
        + envelope.warm_context_rendered_chars
        + envelope.memory_context_rendered_chars
        + len(envelope.topic_attention_anchor_block)
        + len(envelope.cross_topic_bridge_block)
        + len(envelope.soft_background_block)
    )


def _continuity_signature(store: ConversationContinuityStore, now: float) -> str:
    """桥接评估前后的完整连续性状态指纹（快照 + 轮次 + 锚点投影）。"""
    return json.dumps(
        {
            "snapshot": store.snapshot(CHAT, now=now),
            "turns": [
                [turn.focus_preview, turn.reply_preview, turn.source_event_id, turn.topic_epoch, turn.timestamp]
                for turn in store.recent(CHAT, now=now)
            ],
            "anchor_prompt": store.topic_anchor_prompt(CHAT, now=now),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# ── 用例 1：普通连续对话不能丢上下两句 ───────────────────────────────────────
def test_case1_plain_continuous_dialogue_keeps_real_tail_and_pending_question():
    fixture = REPLAY_FIXTURES["plain_continuous"]
    first, second, third = fixture["turns"]
    u1, a1, u2 = first["text"], second["text"], third["text"]
    manager = _lane_manager()
    lane_key = _dialog_lane_key()
    store = ConversationContinuityStore()

    history_after_first_pair = asyncio.run(manager.append_exchange(lane_key, CHAT, u1, a1))

    # 真实写入面：u1 以 role=user 落到车道历史，正文一字不差；角色按时间交替。
    assert [turn["role"] for turn in history_after_first_pair] == ["user", "assistant"]
    assert history_after_first_pair[0]["content"] == u1
    assert history_after_first_pair[1]["content"] == a1
    assert history_after_first_pair[0]["timestamp"] <= history_after_first_pair[1]["timestamp"]

    # Planner 落一轮真实问答：助手的反问进 anchor.open_loop，来源含 explicit_question。
    store.record(
        chat_id=CHAT,
        focus_preview=u1,
        reply_preview=a1,
        sender_id=first["actor_id"],
        source_event_id=first["event_id"],
        anchor_event=_canonical_event(
            first["event_id"], u1, actor_id=first["actor_id"], topic_epoch=fixture["topic_epoch"]
        ),
        topic_epoch=fixture["topic_epoch"],
        now=NOW,
    )
    anchor_after_reply = store.snapshot(CHAT, now=NOW)["topic_anchor"]
    assert anchor_after_reply["open_loop"] == a1
    assert "explicit_question" in anchor_after_reply["source"]
    assert anchor_after_reply["subject_preview"] == u1
    anchor_prompt = store.topic_anchor_prompt(CHAT, now=NOW + 1)

    # 下一轮进来时的真实读取面：尾段必须仍然带着 a1 与 u1。
    tail_before_u2 = asyncio.run(manager.get_recent_transcript(lane_key, CHAT, max_turns=4))
    assert u1 in tail_before_u2 and a1 in tail_before_u2

    history_after_u2 = asyncio.run(manager.append_exchange(lane_key, CHAT, u2, ""))
    assert [turn["role"] for turn in history_after_u2] == ["user", "assistant", "user"]
    assert history_after_u2 == sorted(history_after_u2, key=lambda turn: turn["timestamp"])

    tail_after_u2, tail_event_ids = asyncio.run(
        manager.get_recent_transcript(lane_key, CHAT, max_turns=4, include_event_ids=True)
    )
    assert u1 in tail_after_u2 and a1 in tail_after_u2 and u2 in tail_after_u2
    # 生产车道写入不带 event_id，因此这里不伪造 id。
    assert [event_id for event_id in tail_event_ids if event_id] == []

    store.record(
        chat_id=CHAT,
        focus_preview=u2,
        sender_id=third["actor_id"],
        source_event_id=third["event_id"],
        anchor_event=_canonical_event(
            third["event_id"], u2, actor_id=third["actor_id"], topic_epoch=fixture["topic_epoch"]
        ),
        topic_epoch=fixture["topic_epoch"],
        now=NOW + 2,
    )
    turns = store.recent(CHAT, now=NOW + 3)
    assert [turn.source_event_id for turn in turns] == [first["event_id"], third["event_id"]]
    assert {turn.topic_epoch for turn in turns} == {fixture["topic_epoch"]}

    envelope = PromptEnvelope(
        raw_user_text=u2,
        focus_message_text=u2,
        recent_transcript=tail_after_u2,
        recent_transcript_source="lane",
        topic_attention_anchor_block=anchor_prompt,
        topic_attention_anchor_event_ids=list(anchor_after_reply["recent_event_ids"]),
    )
    system, rendered, _ = _refine(u2, envelope)

    assert system == "stable system prompt"
    assert RECENT_MARKER in rendered
    assert ANCHOR_MARKER in rendered
    assert u1 in rendered and a1 in rendered
    assert f"- open_loop={a1}" in rendered
    anchor_blocks = [body for source, body in _derived_blocks(rendered) if source == ANCHOR_SOURCE]
    assert anchor_blocks == [anchor_prompt]


# ── 用例 2：prefix_hash 轮换后连续性仍在，且不伪造任何轮次 ──────────────────
def test_case2_prefix_hash_rotation_keeps_latest_pair_without_fabricating_turns():
    fixture = REPLAY_FIXTURES["prefix_rotation"]
    latest_user, latest_assistant = fixture["latest_pair"]
    filler_pairs = fixture["filler_pairs"]
    manager = _lane_manager()
    lane_key = _dialog_lane_key()

    def seeded_history():
        history = []
        index = 0
        for question, answer in filler_pairs + [fixture["latest_pair"]]:
            index += 1
            history.append(
                {"role": "user", "content": question, "timestamp": float(NOW + index * 2), "event_id": f"e-c2-q{index}"}
            )
            history.append(
                {
                    "role": "assistant",
                    "content": answer,
                    "timestamp": float(NOW + index * 2 + 1),
                    "message_id": f"e-c2-a{index}",
                }
            )
        return history

    async def _scenario():
        old_umo, old_id, _, _ = await manager.ensure_lane(
            lane_key=lane_key, base_origin=CHAT, prefix_hash=fixture["prefix_before"]
        )
        await manager.conversation_manager.update_conversation(
            unified_msg_origin=old_umo, conversation_id=old_id, history=seeded_history()
        )
        old_umo, old_id, normalized_old, _ = await manager.ensure_lane(
            lane_key=lane_key, base_origin=CHAT, prefix_hash=fixture["prefix_before"]
        )
        rotated_umo, rotated_id, rotated_history, _ = await manager.ensure_lane(
            lane_key=lane_key, base_origin=CHAT, prefix_hash=fixture["prefix_after"]
        )
        tail, _ids = await manager.get_recent_transcript(
            lane_key, CHAT, max_turns=4, include_event_ids=True
        )
        return old_id, rotated_umo == old_umo, normalized_old, rotated_id, rotated_history, tail

    old_id, same_lane_key, normalized_old, rotated_id, rotated_history, rotated_tail = asyncio.run(_scenario())

    # 轮换只换 conversation，不换 lane 键：不会串到别的群。
    assert same_lane_key is True
    assert rotated_id != old_id

    bound = 1 + 2 * LaneHistoryMixin.ROTATION_PAIR_LIMIT
    assert len(normalized_old) == len(filler_pairs) * 2 + 2
    assert len(rotated_history) <= bound
    assert len(rotated_history) < len(normalized_old)  # 没有整条旧车道搬过来

    # 最新的一组完整问答按原角色顺序保留。
    assert [turn["role"] for turn in rotated_history[-2:]] == ["user", "assistant"]
    assert rotated_history[-2]["content"] == latest_user
    assert rotated_history[-1]["content"] == latest_assistant
    assert [turn["role"] for turn in rotated_history] == [
        "assistant", "user", "assistant", "user", "assistant", "user", "assistant"
    ]

    old_contents = {turn["content"] for turn in normalized_old}
    synthetic = [turn for turn in rotated_history if turn["content"] not in old_contents]
    # 唯一的非原文轮次必须是带固定前缀的轮换摘要，且只能是 assistant 角色，绝不冒充真实回复。
    assert synthetic
    assert all(turn["role"] == "assistant" and turn["content"].startswith(ROTATION_PREFIX) for turn in synthetic)
    for turn in synthetic:
        body_lines = turn["content"][len(ROTATION_PREFIX) :].strip().splitlines()
        assert body_lines
        assert all(line.startswith(("user: ", "assistant: ")) for line in body_lines)

    # 落在 3 组窗口之外的最早一组不整条搬过来，只以摘要文本出现一次。
    oldest_pair = filler_pairs[0]
    verbatim_contents = [turn["content"] for turn in rotated_history]
    assert oldest_pair[0] not in verbatim_contents
    assert oldest_pair[1] not in verbatim_contents
    assert oldest_pair[0] in rotated_history[0]["content"]
    assert not any(turn["content"] == oldest_pair[0] for turn in rotated_history[1:])

    # 轮换后的真实读取面：可见尾段不含摘要/合成轮次，最新问答仍在。
    assert latest_user in rotated_tail and latest_assistant in rotated_tail
    assert ROTATION_PREFIX not in rotated_tail
    assert "较早对话摘要" not in rotated_tail

    post_rotation_history = asyncio.run(
        manager.append_exchange(lane_key, CHAT, fixture["next_user_text"], "")
    )
    assert post_rotation_history[-1]["content"] == fixture["next_user_text"]

    envelope = PromptEnvelope(
        raw_user_text=fixture["next_user_text"],
        focus_message_text=fixture["next_user_text"],
        recent_transcript=rotated_tail,
        recent_transcript_source="lane",
    )
    system, rendered, _ = _refine(fixture["next_user_text"], envelope)

    assert system == "stable system prompt"
    assert RECENT_MARKER in rendered
    assert latest_user in rendered and latest_assistant in rendered
    assert fixture["next_user_text"] in rendered
    assert ROTATION_PREFIX not in rendered

    # 没有伪造发言人或引语：可见尾段每行都只有生产渲染出的角色前缀，正文全部来自旧车道原文。
    lane_blocks = [body for source, body in _derived_blocks(rendered) if source == "lane"]
    assert len(lane_blocks) == 1
    visible_lines = lane_blocks[0].splitlines()
    assert len(visible_lines) >= 2
    assert all(line.startswith(("用户: ", "Bot: ")) for line in visible_lines)
    assert all(line.split(": ", 1)[1] in old_contents for line in visible_lines)


# ── 用例 3：话题锚点更新有界，且只以不可信派生块进入提示词 ──────────────────
def test_case3_topic_anchor_update_is_bounded_and_only_renders_as_untrusted_context():
    fixture = REPLAY_FIXTURES["topic_anchor"]
    cat_text = fixture["user_text"]
    question_text = fixture["assistant_text"]
    store = ConversationContinuityStore()

    store.update_topic_anchor(
        CHAT,
        event=_canonical_event(fixture["event_id"], cat_text, actor_id=fixture["actor_id"]),
        subject_preview=cat_text,
        actor_id=fixture["actor_id"],
        topic_epoch=1,
        now=NOW,
    )
    for index in range(fixture["flood_count"]):
        store.update_topic_anchor(
            CHAT,
            event=_canonical_event(f"e-c3-f{index}", fixture["follower_text"], actor_id=f"u-p{index}"),
            subject_preview=cat_text,
            topic_epoch=1,
            now=NOW + index + 1,
        )
    anchor_view = store.update_topic_anchor(
        CHAT,
        event=_canonical_event(
            fixture["assistant_event_id"],
            question_text,
            actor_id=fixture["assistant_actor_id"],
            is_bot=True,
        ),
        subject_preview=cat_text,
        reply_text=question_text,
        actor_id=fixture["assistant_actor_id"],
        topic_epoch=1,
        now=NOW + fixture["flood_count"] + 1,
    )
    stamp = NOW + fixture["flood_count"] + 2

    assert anchor_view["subject_preview"]
    assert len(anchor_view["subject_preview"]) <= ANCHOR_MAX_SUBJECT_CHARS
    assert "猫" in anchor_view["subject_preview"]
    assert len(anchor_view["participants"]) <= ANCHOR_MAX_PARTICIPANTS
    assert anchor_view["open_loop"] == question_text
    assert len(anchor_view["open_loop"]) <= ANCHOR_MAX_OPEN_LOOP_CHARS
    event_ids = anchor_view["recent_event_ids"]
    assert len(event_ids) <= ANCHOR_MAX_EVENT_IDS
    assert len(event_ids) == len(set(event_ids))
    assert fixture["assistant_event_id"] in event_ids
    assert set(anchor_view["source"]) <= set(ANCHOR_SOURCE_VALUES)
    assert "explicit_question" in anchor_view["source"]
    assert len(json.dumps(anchor_view, ensure_ascii=False)) <= ANCHOR_MAX_SERIALIZED_CHARS
    # 快照投影与写入返回值一致，只额外带一个由 now 推出的 age_seconds。
    snapshot_anchor = store.snapshot(CHAT, now=stamp)["topic_anchor"]
    assert snapshot_anchor["age_seconds"] == 1.0
    assert {key: value for key, value in snapshot_anchor.items() if key != "age_seconds"} == anchor_view

    anchor_prompt = store.topic_anchor_prompt(CHAT, now=stamp)
    assert len(anchor_prompt) <= ANCHOR_MAX_PROMPT_CHARS

    envelope = PromptEnvelope(
        raw_user_text=fixture["next_user_text"],
        focus_message_text=fixture["next_user_text"],
        recent_transcript=f"用户: {cat_text}\nBot: {question_text}",
        recent_transcript_source="lane",
        topic_attention_anchor_block=anchor_prompt,
        topic_attention_anchor_event_ids=list(event_ids),
    )
    system, rendered, envelope_after = _refine(fixture["next_user_text"], envelope)

    # 稳定系统提示词逐字节不变；锚点只活在派生/不可信块里。
    assert system == "stable system prompt"
    assert system.encode("utf-8") == b"stable system prompt"
    assert "stable system prompt" not in rendered
    assert ANCHOR_MARKER in rendered
    wrapped = PromptEnvelope.sanitize_derived_context(anchor_prompt, source=ANCHOR_SOURCE)
    assert rendered.count(wrapped) == 1
    assert rendered.count(anchor_prompt) == 1
    assert [source for source, _ in _derived_blocks(rendered)].count(ANCHOR_SOURCE) == 1
    assert anchor_prompt.splitlines()[0] not in _strip_derived_blocks(rendered)
    assert envelope_after.topic_attention_anchor_block == anchor_prompt


# ── 用例 4：显式 quote/reply 跨 topic 桥接：有界、只读、只以派生块出现 ──────
def test_case4_explicit_reply_bridge_is_bounded_read_only_and_untrusted_only():
    fixture = REPLAY_FIXTURES["cross_topic_bridge"]
    source_pair = fixture["source_pair"]
    store = ConversationContinuityStore()
    store.record(
        chat_id=CHAT,
        focus_preview=source_pair["user_text"],
        reply_preview=source_pair["assistant_text"],
        sender_id=source_pair["actor_id"],
        source_event_id=source_pair["event_id"],
        anchor_event=_canonical_event(
            source_pair["event_id"],
            source_pair["user_text"],
            actor_id=source_pair["actor_id"],
            topic_epoch=fixture["source_epoch"],
        ),
        topic_epoch=fixture["source_epoch"],
        now=NOW,
    )
    current_event = _canonical_event(
        fixture["current_event_id"],
        fixture["current_text"],
        actor_id=fixture["current_actor_id"],
        topic_epoch=fixture["target_epoch"],
        reply_target_event_id=fixture["reply_target_event_id"],
        quote_event_id=fixture["reply_target_event_id"],
    )
    target_epoch = fixture["target_epoch"]

    def _evaluate():
        return store.evaluate_topic_bridge(
            CHAT,
            event=current_event,
            target_topic_epoch=target_epoch,
            rotation_reason=fixture["rotation_reason"],
            now=NOW + 20,
        )

    before = _continuity_signature(store, NOW + 20)
    decision = _evaluate()

    assert decision.allowed is True
    assert decision.reason == "explicit_reply"
    assert decision.confidence == 0.95
    assert set(decision.evidence_event_ids) <= set(fixture["seeded_event_ids"])
    assert len(decision.evidence_event_ids) <= BRIDGE_MAX_EVENT_IDS
    assert decision.evidence_event_ids == (fixture["reply_target_event_id"],)
    assert (decision.source_topic_epoch, decision.target_topic_epoch) == (
        fixture["source_epoch"],
        target_epoch,
    )
    assert decision.source_chat_key == decision.target_chat_key == CHAT
    assert len(decision.turns) <= BRIDGE_MAX_TURNS
    assert source_pair["user_text"] in decision.turns[0]
    assert source_pair["assistant_text"] in decision.turns[0]
    assert decision.is_active(NOW + 20)
    bridge_text = decision.prompt_text()
    assert bridge_text
    assert len(bridge_text) <= BRIDGE_MAX_PROMPT_CHARS

    # 评估器只读：重复评估得到同一决策，且不推进 topic_epoch / 锚点 / 轮次。
    second = _evaluate()
    assert second == decision
    assert isinstance(second, BridgeDecision)
    after = _continuity_signature(store, NOW + 20)
    assert after == before
    assert store.snapshot(CHAT, now=NOW + 20)["topic_epoch"] == fixture["source_epoch"]

    envelope = PromptEnvelope(
        raw_user_text=fixture["current_text"],
        focus_message_text=fixture["current_text"],
        cross_topic_bridge_block=bridge_text,
        cross_topic_bridge_event_ids=list(decision.evidence_event_ids),
    )
    system, rendered, envelope_after = _refine(fixture["current_text"], envelope)

    assert system == "stable system prompt"
    assert BRIDGE_MARKER in rendered
    wrapped = PromptEnvelope.sanitize_derived_context(bridge_text, source=BRIDGE_SOURCE)
    assert rendered.count(wrapped) == 1
    assert rendered.count(bridge_text) == 1
    assert [body for source, body in _derived_blocks(rendered) if source == BRIDGE_SOURCE] == [bridge_text]
    assert bridge_text.splitlines()[0] not in _strip_derived_blocks(rendered)
    assert envelope_after.cross_topic_bridge_block == bridge_text
    # 锚点与桥接互斥：桥接生效时不得再把旧话题锚点当稳定事实注入。
    assert ANCHOR_MARKER not in rendered


# ── 用例 5：长历史 + 非空 warm 摘要时，真实尾段优先，warm 先被裁 ───────────
def test_case5_warm_summary_and_long_recent_tail_trims_warm_before_recent():
    fixture = REPLAY_FIXTURES["warm_plus_recent"]
    manager = _lane_manager()
    lane_key = _dialog_lane_key()
    current_text = fixture["user_text"]

    async def _seed_and_read():
        for index in range(1, fixture["pair_count"] + 1):
            await manager.append_exchange(
                lane_key,
                CHAT,
                f"{index}{fixture['question_text']}{fixture['padding'] * 4}",
                f"{index}{fixture['answer_text']}{fixture['padding'] * 4}",
            )
        return await manager.get_recent_transcript(
            lane_key, CHAT, max_turns=fixture["pair_count"] + 2, include_event_ids=True
        )

    tail, lane_event_ids = asyncio.run(_seed_and_read())
    tail_lines = tail.splitlines()
    latest_user_line, latest_assistant_line = tail_lines[-2], tail_lines[-1]
    assert len(tail_lines) >= 4
    assert latest_user_line.startswith("用户: ") and latest_assistant_line.startswith("Bot: ")
    assert [event_id for event_id in lane_event_ids if event_id] == []
    assert len(tail) > PromptRefiner.FLEX_CONTEXT_BUDGET_CHARS

    warm_bundle = SimpleNamespace(
        summary_text=fixture["warm_summary"] * 2,
        quote_text=f"{latest_user_line}\n{latest_assistant_line}",
        has_latest_assistant=True,
    )
    include, reason = _PlannerContext()._should_include_recent_transcript(current_text, warm_bundle, tail)
    assert (include, reason) == (True, "warm_with_recent_minimum")

    envelope = PromptEnvelope(
        raw_user_text=current_text,
        focus_message_text=current_text,
        recent_transcript=tail,
        recent_transcript_event_ids=[""] * (len(tail_lines) - 2)
        + [fixture["shared_question_event_id"], fixture["shared_answer_event_id"]],
        recent_transcript_source="lane",
        warm_zone_summary=fixture["warm_summary"] * 2,
        warm_zone_quotes=f"{latest_user_line}\n{latest_assistant_line}\n{fixture['legacy_warm_line']}",
        warm_zone_quote_entries=[
            (fixture["shared_question_event_id"], latest_user_line),
            (fixture["shared_answer_event_id"], latest_assistant_line),
            (fixture["legacy_warm_event_id"], fixture["legacy_warm_line"]),
        ],
        warm_zone_quote_event_ids=[
            fixture["shared_question_event_id"],
            fixture["shared_answer_event_id"],
            fixture["legacy_warm_event_id"],
        ],
        warm_zone_transcript_source="store",
    )
    system, rendered, envelope_after = _refine(current_text, envelope)

    assert system == "stable system prompt"
    assert current_text in rendered
    assert RECENT_MARKER in rendered
    assert latest_user_line in rendered and latest_assistant_line in rendered

    # 事件身份去重：event id 本身从不进提示词，同一事件的真实行只出现一次。
    assert fixture["shared_question_event_id"] not in rendered
    assert fixture["shared_answer_event_id"] not in rendered
    assert rendered.count(latest_user_line) == 1
    assert rendered.count(latest_assistant_line) == 1
    assert latest_user_line not in envelope_after.warm_zone_quotes
    assert latest_assistant_line not in envelope_after.warm_zone_quotes

    trimmed = envelope_after.flex_context_trimmed_sections
    assert "recent:tail_truncated" in trimmed
    warm_trims = [
        index
        for index, name in enumerate(trimmed)
        if name in {"warm_quotes", "warm_summary:truncated"}
    ]
    assert warm_trims
    assert min(warm_trims) < trimmed.index("recent:tail_truncated")

    kept_lines = envelope_after.recent_transcript.splitlines()
    assert kept_lines == tail_lines[-len(kept_lines) :]
    assert kept_lines[-2:] == [latest_user_line, latest_assistant_line]
    assert envelope_after.flex_context_budget_chars == PromptRefiner.FLEX_CONTEXT_BUDGET_CHARS
    assert _auxiliary_rendered_chars(envelope_after) <= PromptRefiner.FLEX_CONTEXT_BUDGET_CHARS
