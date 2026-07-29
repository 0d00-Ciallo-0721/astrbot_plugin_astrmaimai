from astrmai.conversation.planning.think_level_policy import ThinkLevelPolicy
from astrmai.conversation.contracts.turn_context import ensure_turn_context


class _Event:
    def __init__(self, text="", *, group_id="group-1", extras=None):
        self.message_str = text
        self.unified_msg_origin = f"default:GroupMessage:{group_id}" if group_id else "default:FriendMessage:user-1"
        self._group_id = group_id
        self._extra = dict(extras or {})

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_group_id(self):
        return self._group_id


def test_think_level_zero_for_lightweight_short_core_and_ambient_group():
    policy = ThinkLevelPolicy()

    assert policy.decide(event=_Event("poke", extras={"astrmai_lightweight_event": True})).level == 0
    assert policy.decide(event=_Event("哈哈")).level == 0
    assert policy.decide(event=_Event("hello", extras={"retrieve_keys": ["CORE_ONLY"]}), retrieve_keys=["CORE_ONLY"]).level == 0

    ambient = policy.decide(event=_Event("ordinary side chat"))
    assert ambient.level == 0
    assert ambient.reason == "group_non_direct"

    latest = policy.decide(event=_Event("ordinary side chat", extras={"astrmai_focus_reason": "latest_user_message"}))
    assert latest.level == 0
    assert latest.reason == "group_non_direct"


def test_think_level_one_for_direct_normal_turns():
    policy = ThinkLevelPolicy()

    private = policy.decide(event=_Event("tell me a joke", group_id=""))
    at_bot = policy.decide(event=_Event("hello bot", extras={"astrmai_focus_reason": "at_bot"}))

    assert private.level == 1
    assert at_bot.level == 1


def test_think_level_two_for_complex_emotional_and_memory_reference_turns():
    policy = ThinkLevelPolicy()

    assert policy.decide(event=_Event("为什么会这样？", group_id="")).level == 2
    assert policy.decide(event=_Event("please analyze this a little", group_id="")).level == 2
    assert policy.decide(event=_Event("我有点难受，想要安慰", group_id="")).level == 2
    assert policy.decide(event=_Event("刚才那件事继续说说", group_id="")).level == 2


def test_think_level_three_for_tool_sys3_and_deep_memory_intents():
    policy = ThinkLevelPolicy()

    assert policy.decide(event=_Event("帮我查一下这个人是谁", group_id="")).level == 3
    assert policy.decide(event=_Event("你还记得我上次说什么吗", group_id="")).level == 3
    assert policy.decide(event=_Event("please search this", group_id="")).level == 3
    assert policy.decide(event=_Event("hello", group_id=""), judge_action="TOOL_CALL", is_tool_call_mode=True).level == 3
    assert policy.decide(event=_Event("hello", group_id="", extras={"astrmai_cognitive_memory_policy": "deep"})).level == 3


def test_heartflow_posture_keeps_budget_above_fast_path():
    policy = ThinkLevelPolicy()
    event = _Event("嗯", group_id="group-1")
    turn_context = ensure_turn_context(event)
    turn_context.continuity.heartflow_context = "Heartflow state: prepare to join"
    turn_context.continuity.heartflow_pulse = "join"
    turn_context.continuity.heartflow_interest = 0.8

    decision = policy.decide(event=event)

    assert decision.level == 2
    assert decision.reason == "heartflow_posture"
    assert "heartflow_pulse_join" in decision.signals
    assert "heartflow_high_interest" in decision.signals


def test_heartflow_frequency_guard_keeps_non_direct_group_fast():
    policy = ThinkLevelPolicy()
    event = _Event("side chat", group_id="group-1")
    turn_context = ensure_turn_context(event)
    turn_context.continuity.heartflow_context = (
        "session=talk_frequency_adjust=0.42; insert_pressure=0.82; "
        "reply_pressure=0.12; visible_candidate_score=0.34"
    )
    turn_context.continuity.heartflow_pulse = "prepare_reply"
    turn_context.continuity.heartflow_interest = 0.86

    decision = policy.decide(event=event)

    assert decision.level == 0
    assert decision.reason == "heartflow_frequency_guard"
    assert "heartflow_high_insert_pressure" in decision.signals
    assert "heartflow_low_candidate_score" in decision.signals


def test_heartflow_action_observe_keeps_non_direct_group_fast():
    policy = ThinkLevelPolicy()
    event = _Event("side chat?", group_id="group-1")
    turn_context = ensure_turn_context(event)
    turn_context.continuity.heartflow_context = "Heartflow state: observing"
    turn_context.continuity.heartflow_action = "no_reply"
    turn_context.continuity.heartflow_interest = 0.9
    turn_context.continuity.heartflow_pulse = "prepare_reply"

    decision = policy.decide(event=event)

    assert decision.level == 0
    assert decision.reason == "group_non_direct"


def test_direct_question_with_high_interest_gets_deep_budget():
    policy = ThinkLevelPolicy()
    event = _Event("what should we do?", group_id="", extras={"astrmai_focus_reason": "private"})
    turn_context = ensure_turn_context(event)
    turn_context.continuity.heartflow_context = "Heartflow state: high interest"
    turn_context.continuity.heartflow_interest = 0.82
    turn_context.continuity.heartflow_pulse = "prepare_reply"

    decision = policy.decide(event=event)

    assert decision.level == 2
    assert decision.reason == "heartflow_direct_question"


def test_proactive_event_uses_bounded_budget_and_blocks_tool_escalation():
    policy = ThinkLevelPolicy()
    event = _Event(
        "please search and look up something later",
        extras={
            "astrmai_is_proactive_event": True,
            "astrmai_proactive_source": "wakeup",
            "astrmai_proactive_urgency": 0.58,
        },
    )
    urgent = _Event(
        "please search and look up something later",
        extras={
            "astrmai_is_proactive_event": True,
            "astrmai_proactive_source": "heartflow",
            "astrmai_proactive_urgency": 0.82,
        },
    )
    urgent_context = ensure_turn_context(urgent)
    urgent_context.continuity.goal_status = "continuing"
    urgent_context.continuity.turn_count = 2

    decision = policy.decide(event=event, judge_action="TOOL_CALL", is_tool_call_mode=True)
    urgent_decision = policy.decide(event=urgent)

    assert decision.level == 1
    assert decision.reason == "proactive_opening"
    assert "proactive_event" in decision.signals
    assert urgent_decision.level == 2
    assert urgent_decision.reason == "proactive_high_urgency_with_continuity"


def test_sharp_and_long_cooldowns_skip_simple_turns_only():
    policy = ThinkLevelPolicy()

    simple = policy.decide(event=_Event("收到", group_id=""), cooldown_tags=["sharp_reply"])
    tool = policy.decide(event=_Event("帮我查一下这个", group_id=""), cooldown_tags=["sharp_reply"])
    meme_cooldown = policy.decide(event=_Event("tell me more", group_id=""), cooldown_tags=["meme"])

    assert simple.level == 0
    assert simple.reason in {"short_ack", "cooldown_simple_turn"}
    assert tool.level == 3
    assert meme_cooldown.level == 1


def test_think_level_decision_exposes_stable_pipeline_route():
    policy = ThinkLevelPolicy()

    fast = policy.decide(event=_Event("好", group_id=""))
    normal = policy.decide(event=_Event("今天过得怎么样", group_id=""))
    cognitive = policy.decide(event=_Event("为什么我总是感到焦虑", group_id=""))
    deep_memory = policy.decide(event=_Event("你还记得我上次说了什么吗", group_id=""))
    tool = policy.decide(
        event=_Event("查一下天气", group_id=""),
        judge_action="TOOL_CALL",
        is_tool_call_mode=True,
    )

    assert fast.route == "fast_chat"
    assert normal.route == "normal_chat"
    assert cognitive.route == "cognitive_chat"
    assert deep_memory.route == "deep_memory"
    assert tool.route == "tool"
