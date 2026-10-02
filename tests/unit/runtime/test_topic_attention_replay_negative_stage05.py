"""话题注意力连续性 · 阶段 05 集成验证：8 条确定性负向 / 攻击回放。

被测特性：跨话题“软桥接”（soft bridge）+ 话题注意力锚点（topic attention anchor）+ lane 历史。
本文件只证明门禁：该断开的必须断开（跨群、过期、旧话题、注入的内部 envelope、预算），
不该误伤的必须放行（有结构化回复目标的短句）。

全部打在真实生产入口上，不做任何 mock：
``ConversationContinuityStore.evaluate_topic_bridge`` / ``record`` / ``update_topic_anchor`` /
``snapshot`` / ``topic_anchor_prompt``、
``PromptRefiner._apply_flexible_context_budget`` / ``refine_prompt``、
``LaneHistoryMixin.build_history_turn`` / ``_sanitize_dialog_message`` / ``get_recent_transcript``、
``GatewayLaneMixin._build_history_user_text``、
``output_guard.sanitize_visible_reply_text`` / ``validate_visible_output_text``。
"""

import asyncio
from types import SimpleNamespace

from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.contracts.topic_attention_anchor import (
    ANCHOR_MAX_EVENT_IDS,
    ANCHOR_MAX_PARTICIPANTS,
)
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.prompt_refiner import PromptRefiner
from astrmai.infrastructure.gateway.gateway_lane import GatewayLaneMixin
from astrmai.infrastructure.gateway.output_guard import (
    looks_like_internal_event_envelope,
    sanitize_visible_reply_text,
    validate_visible_output_text,
)
from astrmai.infrastructure.runtime.lane_history import LaneHistoryMixin

# ── 脱敏夹具：一律使用假 id（u-alice / u-bob / g-A / evt-*），不含任何真实发言人 id ──
CHAT_A = "default:GroupMessage:g-A"
CHAT_B = "default:GroupMessage:g-B"
BOT_NAME = "妃爱"

# 上一话题（topic_epoch=1）的一次真实往返，作为桥接的“源证据”。
SOURCE_TURN_NO_LOOP = {
    "chat_id": CHAT_A,
    "focus_preview": "工作环境",
    "goal_summary": "想把显示器垫高一点",
    "social_intent": "answer",
    "action_tier": "talk",
    "action_taken": "reply",
    # 非疑问句回复：不会留下 open_loop
    "reply_preview": "先说结论：显示器垫高一点会舒服很多。",
    "reply_need": "reply",
    "sender_id": "u-alice",
    "source_event_id": "evt-old-1",
    "topic_epoch": 1,
}

# 同上，但机器人留了一个开放问题（open_loop），供用例 4 做差分对照。
SOURCE_TURN_OPEN_LOOP = {
    **SOURCE_TURN_NO_LOOP,
    "focus_preview": "项目排期",
    "goal_summary": "想把周五的评审挪到上午",
    "reply_preview": "你周五有空吗？",
}

T_BASE = 1000.0  # 源话题锚点的 updated_at 基准时刻


def _seed(store, *, turn=None, now=T_BASE, chat=CHAT_A):
    """用真实 record() 播下源话题状态（含 state.turns 与 topic_anchor）。"""
    data = dict(SOURCE_TURN_NO_LOOP if turn is None else turn)
    data["chat_id"] = chat
    store.record(now=now, **data)
    return store


def _event(event_id="evt-new-1", *, chat_id=CHAT_A, actor_id="u-alice", text="然后呢", reply_id="", epoch=2):
    """canonical 事件用普通 Mapping 表达（_anchor_event_value 接受任意 Mapping）。"""
    return {
        "event_id": event_id,
        "chat_id": chat_id,
        "actor_id": actor_id,
        "visible_text": text,
        "topic_epoch": epoch,
        "reply_target_event_id": reply_id,
        "quote_event_id": "",
        "causal_parent_event_id": "",
        "is_bot": False,
    }


def _bridge(store, event, *, epoch=2, now=T_BASE + 10.0, rotation_reason="new_topic", chat=CHAT_A):
    return store.evaluate_topic_bridge(
        chat,
        event=event,
        target_topic_epoch=epoch,
        rotation_reason=rotation_reason,
        now=now,
    )


def _decision_blob(decision) -> str:
    """把决策里所有可读文本拼起来，用来证明“被拒绝的决策不含任何旧话题内容”。"""
    parts = [
        decision.reason,
        decision.source_chat_key,
        decision.target_chat_key,
        decision.source_anchor_preview,
        decision.source_open_loop,
        decision.prompt_text(),
        *decision.evidence_event_ids,
        *decision.turns,
    ]
    return "\n".join(str(part) for part in parts)


def _continuity_rejects_envelope(text: str) -> bool:
    """委托给生产代码自己的 envelope 检测，避免在测试里复制一份正则。"""
    return bool(ConversationContinuityStore.INTERNAL_TOPIC_ENVELOPE_RE.search(text))


def _event_helper():
    class Event:
        message_str = "u-alice: 最新一句话"
        unified_msg_origin = CHAT_A

        def __init__(self):
            self.extras = {"retrieve_keys": []}

        def get_extra(self, key, default=None):
            return self.extras.get(key, default)

        def set_extra(self, key, value):
            self.extras[key] = value

    return Event()


def _refine(envelope):
    refiner = PromptRefiner(memory_engine=None)
    system, rendered = asyncio.run(
        refiner.refine_prompt(
            event=_event_helper(),
            system_prompt="stable system prompt",
            prompt="",
            context={"disable_rag_injection": True},
            prompt_envelope=envelope,
        )
    )
    return system, rendered, envelope


# ── 用例 7 用到的内部 envelope 形态文本（与 output_guard / conversation_continuity 的检测式一致）──
INTERNAL_ENVELOPE = (
    "[事件=evt-7 | 发言人=u-alice（ID:10001） | 角色=成员 | 类型=text | 来源=original]\n"
    "内容：今晚吃什么"
)
CANONICAL_USER_VISIBLE_TEXT = "今晚吃什么"


class _UserTurnEvent:
    """只提供 get_extra 的事件壳，真正被测的是 _build_history_user_text 本身。"""

    def __init__(self, canonical):
        self._extras = {"astrmai_conversation_event": canonical}

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


class _TranscriptLane(LaneHistoryMixin):
    """用阶段 04 已验证的 lane 夹具驱动真实 get_recent_transcript / 历史守卫。"""

    settings = SimpleNamespace(nicknames=[BOT_NAME], debug_mode=False)

    def __init__(self, history):
        self._history = history

    async def _ensure_lane_with_timeout(self, **kwargs):
        return "lane", "conversation", self._history, ""

    def _message_timestamp(self, message):
        return 1.0


# ─────────────────────────────────────────────────────────────────────────────
def test_01_跨chat_桥接必须拒绝且目标群拿不到源群锚点():
    """攻击回放：把 A 群的话题上下文桥接到 B 群（跨群泄漏 = release blocker 候选）。"""
    store = ConversationContinuityStore()
    _seed(store)
    # B 群先被 snapshot() 触碰到，产生一个空白 state，证明“有 state 但无锚点”也照样拒绝
    store.snapshot(CHAT_B, now=T_BASE + 10.0)

    # (a) 攻击者伪造 chat key = A，但事件其实来自 B —— 这是源码里的 chat_mismatch 分支
    forged = _bridge(
        store,
        _event("evt-b-1", chat_id=CHAT_B, text="那个方案继续说说", reply_id="evt-old-1"),
        chat=CHAT_A,
    )
    assert forged.allowed is False
    assert forged.reason == "chat_mismatch"
    assert forged.prompt_text() == ""
    assert "工作环境" not in _decision_blob(forged)

    # (b) 用完全合法的桥接参数，只把 chat key 换成 B 群 —— 源锚点根本不该跨 chat 可见。
    #     注意：conversation_continuity.py:884-891 只在 key 与 event.chat_id 不一致时报
    #     chat_mismatch；两者一致但 B 群无锚点时走 no_source_anchor 分支。
    cross_chat = _bridge(
        store,
        _event("evt-b-2", chat_id=CHAT_B, text="那个方案继续说说", reply_id="evt-old-1"),
        chat=CHAT_B,
    )
    assert cross_chat.allowed is False
    assert cross_chat.reason == "no_source_anchor"
    assert cross_chat.source_topic_epoch == 0
    assert cross_chat.prompt_text() == ""
    assert "工作环境" not in _decision_blob(cross_chat)

    # B 群的快照与锚点提示里都不能出现 A 群的 subject / open_loop
    assert store.snapshot(CHAT_B, now=T_BASE + 10.0)["topic_anchor"]["subject_preview"] == ""
    assert store.snapshot(CHAT_B, now=T_BASE + 10.0)["topic_anchor"]["recent_event_ids"] == []
    assert store.topic_anchor_prompt(CHAT_B, now=T_BASE + 10.0) == ""

    # 反向：桥接是只读的，失败的评估不能改写 A 群状态
    assert store.snapshot(CHAT_A, now=T_BASE + 10.0)["topic_epoch"] == 1
    assert store.snapshot(CHAT_A, now=T_BASE + 10.0)["topic_anchor"]["subject_preview"] == "工作环境"


def test_02_明显新话题不得带走旧锚点内容():
    """同群同发言人、无回复目标、语义无关的新话题：必须拒绝，且旧 subject/open_loop 不得夹带。"""
    store = ConversationContinuityStore()
    _seed(store, turn=SOURCE_TURN_OPEN_LOOP)
    # 夹具自检：open_loop 与 subject 确实存在（否则本用例变成“夹具写错”而不是产品行为）
    anchor = store.snapshot(CHAT_A, now=T_BASE + 10.0)["topic_anchor"]
    assert anchor["subject_preview"] == "项目排期"
    assert anchor["open_loop"] == "你周五有空吗？"

    decision = _bridge(store, _event("evt-new-2", text="今晚吃什么？", actor_id="u-alice"))

    # 源码分支（conversation_continuity.py:939-940）：无回复目标 + 同发言人 +
    # 文本既不是 open_loop 答案也不是 short followup 时，命中指代词报 insufficient_evidence，
    # 否则报 unrelated_topic。“今晚吃什么？”既不含他/她/它/这个/那个/上面/刚才/继续/然后，
    # 所以实际观察值是 unrelated_topic。
    assert decision.allowed is False
    assert decision.reason == "unrelated_topic"
    assert decision.prompt_text() == ""
    assert decision.source_anchor_preview == ""
    assert decision.source_open_loop == ""
    assert decision.turns == ()
    assert "项目排期" not in _decision_blob(decision)
    assert "你周五有空吗" not in _decision_blob(decision)


def test_03_bridge_超过_ttl_过期并在边界内仍然放行():
    """过期侧与边界侧都要证明，否则“拦住了”可能只是巧合。"""
    store = ConversationContinuityStore()
    _seed(store)
    ttl = store.BRIDGE_TTL_SECONDS  # 120
    assert ttl == 120
    event = _event("evt-new-3", text="那个方案后来怎么样了", reply_id="evt-old-1")

    fresh = _bridge(store, event, now=T_BASE + (ttl - 1))
    assert fresh.allowed is True
    assert fresh.reason == "explicit_reply"
    assert fresh.confidence >= 0.8

    exact = _bridge(_seed(ConversationContinuityStore()), event, now=T_BASE + ttl)
    assert exact.allowed is False
    assert exact.reason == "expired"  # 源码用 age >= min(...)，边界值即过期

    stale = _bridge(_seed(ConversationContinuityStore()), event, now=T_BASE + (ttl + 1))
    assert stale.allowed is False
    assert stale.reason == "expired"
    assert stale.source_age_seconds == 0.0  # deny 分支不携带任何源内容
    assert stale.prompt_text() == ""
    assert "工作环境" not in _decision_blob(stale)


def test_04_open_loop_被回答后不得再以答案身份桥接():
    """差分证明：唯一变量是 open_loop 是否还开着。"""
    store = ConversationContinuityStore()
    _seed(store, turn=SOURCE_TURN_OPEN_LOOP)

    # 对照组：loop 还开着时，同一条短句确实靠 open_loop_answer 过桥
    control = _bridge(store, _event("evt-ctrl-4", text="我周五有空"))
    assert control.allowed is True
    assert control.reason == "open_loop_answer"
    assert control.source_open_loop == "你周五有空吗？"

    # 实验组：用户先回答了这个开放问题（真实 update_topic_anchor 入口）
    store.update_topic_anchor(
        CHAT_A,
        event=_event("evt-answer-4", text="我周五有空", epoch=1),
        subject_preview="项目排期",
        now=T_BASE + 12.0,
    )
    snapshot = store.snapshot(CHAT_A, now=T_BASE + 15.0)
    assert snapshot["topic_anchor"]["open_loop"] == ""
    # 证明“真的只是 loop 被关掉”，subject / 参与者 / 证据事件都还在
    assert snapshot["topic_anchor"]["subject_preview"] == "项目排期"
    assert snapshot["topic_anchor"]["participants"] == ["u-alice"]
    assert "evt-old-1" in snapshot["topic_anchor"]["recent_event_ids"]

    after_answer = _bridge(
        store,
        _event("evt-after-4", text="我周五有空"),
        now=T_BASE + 15.0,
    )
    assert after_answer.allowed is False
    # 实际命中分支：open_loop 空 → 走不到 open_loop_answer；
    # “我周五有空”不是 short followup → conversation_continuity.py:939-940 的 else →
    # 无指代词 → unrelated_topic
    assert after_answer.reason == "unrelated_topic"
    assert after_answer.source_open_loop == ""
    assert after_answer.prompt_text() == ""


def test_05_无结构证据的短句全部拒绝并有正向对照():
    """短句门禁要窄：无目标+异人必须拒；无目标+同发言人但已过 90s 同发言人窗口也必须拒；
    而带合法 reply_target 的短句必须放行（证明不是全面封杀）。"""
    store = ConversationContinuityStore()
    _seed(store)

    # (a) 无回复/引用目标 + 另一个发言人 —— actor_mismatch（conversation_continuity.py:927-928）
    different_actor = {}
    for index, text in enumerate(("嗯", "那个", "继续")):
        decision = _bridge(
            store,
            _event(f"evt-x-5-{index}", text=text, actor_id="u-bob"),
            now=T_BASE + 10.0,
        )
        assert decision.allowed is False
        assert decision.reason == "actor_mismatch"
        different_actor[text] = decision.reason
    assert different_actor == {"嗯": "actor_mismatch", "那个": "actor_mismatch", "继续": "actor_mismatch"}

    # (b) 同一发言人，但既无结构化目标、也已超出 BRIDGE_SAME_ACTOR_SECONDS(90) 窗口：
    #     即“完全没有任何结构证据”的短句 —— 逐条断言实际原因（三条并不相同）
    same_actor = {}
    for index, text in enumerate(("嗯", "那个", "继续")):
        decision = _bridge(
            store,
            _event(f"evt-y-5-{index}", text=text, actor_id="u-alice"),
            now=T_BASE + store.BRIDGE_SAME_ACTOR_SECONDS + 5.0,
        )
        assert decision.allowed is False
        assert decision.prompt_text() == ""
        assert "工作环境" not in _decision_blob(decision)
        same_actor[text] = decision.reason
    # “嗯”不含任何指代词 → unrelated_topic；“那个”/“继续”命中指代词表 → insufficient_evidence
    assert same_actor == {
        "嗯": "unrelated_topic",
        "那个": "insufficient_evidence",
        "继续": "insufficient_evidence",
    }

    # (c) 正向对照：同样极短的“嗯”，但带一个真实存在于近窗证据里的 reply_target —— 必须放行
    control = _bridge(
        store,
        _event("evt-z-5", text="嗯", actor_id="u-alice", reply_id="evt-old-1"),
        now=T_BASE + 10.0,
    )
    assert control.allowed is True
    assert control.reason == "explicit_reply"
    assert control.evidence_event_ids == ("evt-old-1",)
    assert control.prompt_text()

    # (d) 目标 id 不存在于证据里 —— 不能凭伪造的 reply_target 过桥
    forged_target = _bridge(
        store,
        _event("evt-w-5", text="嗯", actor_id="u-alice", reply_id="evt-not-exist"),
        now=T_BASE + 10.0,
    )
    assert forged_target.allowed is False
    assert forged_target.reason == "unverified_reply_target"

    # (e) 设计边界（不是缺陷，但必须留档）：同一发言人在 BRIDGE_SAME_ACTOR_SECONDS(90) 内
    #     的短句会被 same_actor_followup 放行；这就是本特性残余的误判风险面。
    in_window = _bridge(
        store,
        _event("evt-v-5", text="嗯", actor_id="u-alice"),
        now=T_BASE + 10.0,
    )
    assert in_window.allowed is True
    assert in_window.reason == "same_actor_followup"
    assert in_window.confidence < control.confidence  # 弱证据必须比显式回复目标更不确定


def test_06_重复投递不刷新锚点且回放洪泛下有界():
    """同一 event 重复投递：不增长、不刷新 updated_at、桥接报 duplicate_event；
    30 个不同发言人/事件 id 灌进来后锚点仍必须被契约上限截断。"""
    store = ConversationContinuityStore()
    _seed(store)
    first = store.snapshot(CHAT_A, now=T_BASE + 1.0)["topic_anchor"]
    assert first["recent_event_ids"] == ["evt-old-1"]
    assert first["participants"] == ["u-alice"]
    assert first["updated_at"] == T_BASE

    # (a) 重复走 update_topic_anchor —— conversation_continuity.py:770-771 直接原样返回
    store.update_topic_anchor(
        CHAT_A,
        event=_event("evt-old-1", text="工作环境", epoch=1),
        subject_preview="工作环境",
        now=T_BASE + 5.0,
    )
    after_anchor_dup = store.snapshot(CHAT_A, now=T_BASE + 5.0)["topic_anchor"]
    assert after_anchor_dup["updated_at"] == T_BASE
    assert after_anchor_dup["recent_event_ids"] == ["evt-old-1"]
    assert after_anchor_dup["participants"] == ["u-alice"]

    # (b) 重复走 record() —— 同一 source_event_id 不得扩锚点
    store.record(
        now=T_BASE + 6.0,
        **{**SOURCE_TURN_NO_LOOP, "chat_id": CHAT_A},
    )
    after_record_dup = store.snapshot(CHAT_A, now=T_BASE + 6.0)["topic_anchor"]
    assert after_record_dup["updated_at"] == T_BASE
    assert after_record_dup["recent_event_ids"] == ["evt-old-1"]
    assert len(after_record_dup["participants"]) == 1

    # (c) 用重复的 current event id 请求桥接 —— duplicate_event
    dup = _bridge(
        store,
        _event("evt-old-1", text="那个后来怎么样了", reply_id="evt-old-1"),
        now=T_BASE + 10.0,
    )
    assert dup.allowed is False
    assert dup.reason == "duplicate_event"
    assert dup.prompt_text() == ""

    # (d) 回放洪泛：30 个不同发言人 / 事件 id，锚点上限依旧生效
    for index in range(30):
        store.update_topic_anchor(
            CHAT_A,
            event=_event(
                f"evt-flood-{index}",
                text="工作环境",
                actor_id=f"u-flood-{index}",
                epoch=1,
            ),
            subject_preview="工作环境",
            now=T_BASE + 20.0 + index,
        )
    flooded = store.snapshot(CHAT_A, now=T_BASE + 60.0)["topic_anchor"]
    assert len(flooded["participants"]) <= ANCHOR_MAX_PARTICIPANTS
    assert len(flooded["recent_event_ids"]) <= ANCHOR_MAX_EVENT_IDS
    # 非空洞：上限必须真的被触达，且保留的是最近的一批
    assert len(flooded["participants"]) == ANCHOR_MAX_PARTICIPANTS
    assert len(flooded["recent_event_ids"]) == ANCHOR_MAX_EVENT_IDS
    assert flooded["participants"][-1] == "u-flood-29"
    assert flooded["recent_event_ids"][-1] == "evt-flood-29"
    assert "evt-old-1" not in flooded["recent_event_ids"]
    assert flooded["updated_at"] == T_BASE + 49.0


def test_07_assistant_内部_envelope_被拒但同一事件的规范用户轮必须存活():
    """阶段 00 的不对称性：内部 envelope 绝不能作为 assistant 可见输出/历史/提示词出现；
    但同一事件作为 canonical 用户轮时必须以“用户真正可见的文字”活下来。"""
    assert looks_like_internal_event_envelope(INTERNAL_ENVELOPE) is True
    assert _continuity_rejects_envelope(INTERNAL_ENVELOPE) is True

    # (a) assistant 候选输出：被对外输出守卫整体拒绝
    assert sanitize_visible_reply_text(INTERNAL_ENVELOPE, fallback_text="", speaker_names=[BOT_NAME]) == ""
    assert validate_visible_output_text(INTERNAL_ENVELOPE) == ("", "internal_event_envelope")

    lane = _TranscriptLane([])
    assert lane.build_history_turn("assistant", INTERNAL_ENVELOPE) is None
    assert lane._sanitize_dialog_message({"role": "assistant", "content": INTERNAL_ENVELOPE}) is None

    # (b) 同一段原文直接当 user 内容：一样被拦（说明这不是“用户侧白名单”，而是 envelope 必拦）
    assert lane.build_history_turn("user", INTERNAL_ENVELOPE) is None
    assert lane._sanitize_dialog_message({"role": "user", "content": INTERNAL_ENVELOPE}) is None

    # (c) 同一事件以 canonical 用户轮到达：阶段 00 的宽松历史路径用 visible_text 重建历史文本
    canonical = ConversationEvent(
        event_id="evt-7",
        chat_id=CHAT_A,
        chat_kind="group",
        timestamp=T_BASE,
        actor_id="u-alice",
        actor_name="u-alice",
        visible_text=CANONICAL_USER_VISIBLE_TEXT,
        rich_text=CANONICAL_USER_VISIBLE_TEXT,
        message_kind="text",
        role="user",
    )
    history_user_text = GatewayLaneMixin._build_history_user_text(
        _UserTurnEvent(canonical),
        raw_user_text=INTERNAL_ENVELOPE,
        prompt="",
    )
    assert CANONICAL_USER_VISIBLE_TEXT in history_user_text
    assert "事件=" not in history_user_text
    assert "10001" not in history_user_text
    user_turn = lane.build_history_turn("user", history_user_text)
    assert user_turn is not None and user_turn["role"] == "user"
    assert CANONICAL_USER_VISIBLE_TEXT in user_turn["content"]
    assert "发言人=" not in user_turn["content"]

    # (d) lane 转录层：assistant 的 envelope 消失、user 的可见文字留下
    transcript_lane = _TranscriptLane(
        [
            {"role": "user", "content": CANONICAL_USER_VISIBLE_TEXT, "sender_name": "u-alice", "event_id": "evt-7"},
            {"role": "assistant", "content": INTERNAL_ENVELOPE, "message_id": "reply-7"},
        ]
    )
    transcript = asyncio.run(transcript_lane.get_recent_transcript("lane", CHAT_A))
    assert CANONICAL_USER_VISIBLE_TEXT in transcript
    assert "事件=" not in transcript and "发言人=" not in transcript and "10001" not in transcript

    # (e) 组装后的提示词里同样不得出现 envelope 文本
    envelope = PromptEnvelope(
        raw_user_text="u-alice: 那今晚加个辣子鸡",
        focus_message_text="u-alice: 那今晚加个辣子鸡",
        recent_transcript=transcript,
        recent_transcript_source="lane",
    )
    system, rendered, _ = _refine(envelope)
    assert system == "stable system prompt"
    assert CANONICAL_USER_VISIBLE_TEXT in rendered
    for marker in ("事件=evt-7", "发言人=u-alice", "来源=original", "10001"):
        assert marker not in rendered


def test_08_超预算裁剪顺序与最新用户输入永不被挤掉():
    """ auxiliary 总量必须 ≤ FLEX_CONTEXT_BUDGET_CHARS，裁剪顺序固定为
    soft_background → warm(先 quotes 后 summary) → bridge → anchor，
    recent 保留最新完整 user/assistant 对，当前用户输入永远在 rendered 里。"""
    refiner = PromptRefiner(memory_engine=None)
    budget = refiner.FLEX_CONTEXT_BUDGET_CHARS
    assert budget == 1600

    recent_lines = []
    for index in range(8):
        # 角色词表由 _truncate_recent_transcript 决定：assistant 行必须以 Bot/assistant/astrmai 开头
        recent_lines.append(f"u-alice: 问题{index} " + "U" * 120)
        recent_lines.append(f"Bot: 回答{index} " + "A" * 120)
    recent_text = "\n".join(recent_lines)
    warm_summary = "S" * 600
    warm_quotes = "Q" * 800

    result = refiner._apply_flexible_context_budget(
        focus_text="u-alice: 那今晚加个辣子鸡",
        direct_text="u-alice: 上一条被直接回复的消息",
        warm_text=f"{warm_summary}\n{warm_quotes}",
        warm_meta={"warm_summary": warm_summary, "warm_quotes": warm_quotes},
        recent_text=recent_text,
        memory_text="M" * 400,
        memory_meta={},
        soft_background_text="F" * 400,
        anchor_text="T" * 900,
        bridge_text="B" * 900,
    )

    trimmed = list(result["trimmed_sections"])
    assert result["budget_chars"] == budget
    # 顺序：soft_background 最先出局，然后 warm（先 quotes 再 summary），然后 bridge，最后 anchor
    assert trimmed[0] == "soft_background"
    assert trimmed.index("warm_quotes") < trimmed.index("warm_summary:truncated")
    assert trimmed.index("warm_summary:truncated") < trimmed.index("bridge:truncated")
    assert trimmed.index("bridge:truncated") < trimmed.index("anchor:truncated")
    assert result["soft_background_text"] == ""
    assert result["warm_text"] == ""
    assert result["bridge_text"] == ""
    assert result["anchor_text"] == ""

    kept_recent = str(result["recent_text"])
    auxiliary_total = sum(
        len(str(result[key] or ""))
        for key in (
            "warm_text",
            "recent_text",
            "anchor_text",
            "bridge_text",
            "memory_text",
            "soft_background_text",
        )
    )
    assert auxiliary_total <= budget
    # 最新完整对：最新的 user/assistant 都在，最旧的一对被丢掉
    assert "问题7" in kept_recent and "Bot: 回答7" in kept_recent
    assert "问题0" not in kept_recent
    assert kept_recent.endswith("A" * 120)

    protected = list(result["protected_sections"])
    assert "focus_message" in protected and "direct_context" in protected

    # 提示词层：同样的预算压力下，当前用户这一句话必须还在 rendered 里
    envelope = PromptEnvelope(
        raw_user_text="u-alice: 那今晚加个辣子鸡",
        focus_message_text="u-alice: 那今晚加个辣子鸡",
        recent_transcript=recent_text,
        recent_transcript_source="lane",
        warm_zone_summary=warm_summary,
        warm_zone_quotes=warm_quotes,
        topic_attention_anchor_block="T" * 900,
        cross_topic_bridge_block="B" * 900,
        soft_background_sections={"cold_summary": "F" * 400},
    )
    _, rendered, envelope = _refine(envelope)
    assert "u-alice: 那今晚加个辣子鸡" in rendered
    assert envelope.flex_context_budget_chars == budget
    # flex 预算的第一刀永远砍向 soft_background（不是 flex 之前的 soft_background 自身分段裁剪）
    assert envelope.flex_context_trimmed_sections[0] == "soft_background"
    assert envelope.soft_background_rendered_chars == 0
    assert "---背景理解" not in rendered
    assert envelope.warm_context_rendered_chars + envelope.recent_context_rendered_chars <= budget

    # 缺陷候选留档（把观察到的实际行为固化，而不是断言理想行为）：
    # _truncate_recent_transcript 只把 bot/assistant/astrmai 认作 assistant 角色
    # （prompt_refiner.py:234-236），而生产 lane 转录把 assistant 渲染成机器人昵称
    # （lane_transcript.py:82 用 settings.nicknames[0]）。
    # 结果：真实昵称转录被判定为“没有完整的 user/assistant 相邻对”，
    # “优先保留最新一对”退化成只保留最后一行，最新那条用户输入反而被挤掉。
    nickname_lines = []
    for index in range(7):
        nickname_lines.append(f"u-alice: 昵称问题{index} " + "U" * 120)
        nickname_lines.append(f"{BOT_NAME}: 昵称回答{index} " + "A" * 120)
    nickname_only = refiner._apply_flexible_context_budget(
        focus_text="u-alice: 那今晚加个辣子鸡",
        direct_text="",
        warm_text="",
        warm_meta={},
        recent_text="\n".join(nickname_lines),
        memory_text="",
        memory_meta={},
        soft_background_text="",
    )
    degraded = str(nickname_only["recent_text"])
    assert degraded == f"{BOT_NAME}: 昵称回答6 " + "A" * 120
    assert "昵称问题6" not in degraded
