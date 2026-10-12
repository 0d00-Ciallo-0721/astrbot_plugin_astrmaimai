"""Regression contracts for identity, idle history and bounded restart restore."""
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
from pathlib import Path
import subprocess
import sys

import pytest

from astrmai.conversation.attention.gate import AttentionGate
from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.conversation.contracts.dialog_history_policy import DialogHistoryPolicy
from astrmai.conversation.contracts.focus_context import FocusThreadContext
from astrmai.conversation.ingress import sensors as sensors_module
from astrmai.conversation.planning import conversation_continuity as continuity_module
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin


class ComponentType(str, Enum):
    Plain = "Plain"
    At = "At"
    Reply = "Reply"


class Event:
    def __init__(self, text="Python library install", components=()):
        self.message_str = text
        self.message_obj = SimpleNamespace(message=list(components), message_id="new", self_id="bot")
        self.unified_msg_origin = "offline:GroupMessage:group"
        self.timestamp = 11860.0
        self.extras = {}

    def get_group_id(self): return "group"
    def get_sender_id(self): return "alice"
    def get_sender_name(self): return "Alice"
    def get_self_id(self): return "bot"
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value


def seeded_store(chat="offline:GroupMessage:group"):
    store = ConversationContinuityStore()
    store.record(chat_id=chat, focus_preview="Python library install", goal_summary="Python library install",
                 social_intent="answer", action_taken="reply", reply_preview="pip install example",
                 sender_id="alice", source_event_id="old", now=10000)
    return store


def test_enum_reply_and_at_preserve_identity():
    event = Event(components=[SimpleNamespace(type=ComponentType.Reply, id="old", sender_id="bob", sender_nickname="Bob"),
                              SimpleNamespace(type=ComponentType.At, qq="carol")])
    canonical = ConversationEvent.from_astr_event(event)
    assert (canonical.actor_id, canonical.reply_target_actor_id, canonical.reply_target_event_id) == ("alice", "bob", "old")
    assert canonical.at_actor_ids == ("carol",)
    assert PlannerPromptContextMixin._has_reply_reference(event)


def test_real_enum_plain_activity_is_valid_topic_evidence():
    event = Event(components=[SimpleNamespace(type=ComponentType.Plain, text="Python library install")])
    assert AttentionGate._is_pure_text_activity_event(event)


def test_config_defaults_match_schema():
    from config import AstrMaiConfig

    schema = json.loads((Path(__file__).resolve().parents[2] / "_conf_schema.json").read_text(encoding="utf-8"))
    config = AstrMaiConfig()
    for section, fields in {
        "system1": ("nickname_wakeup_threshold", "nickname_at_others_with_question_score", "nickname_prefix_score",
                    "nickname_with_question_score", "nickname_mention_only_score"),
        "conversation": ("group_history_readable_ttl_sec", "group_history_persist_ttl_sec", "restore_recent_message_limit"),
    }.items():
        for name in fields:
            assert schema[section]["items"][name]["default"] == getattr(getattr(config, section), name)


@pytest.mark.asyncio
async def test_nickname_ingress_does_not_crash(monkeypatch):
    class Plain:
        type = ComponentType.Plain
        def __init__(self, text): self.text = text
    monkeypatch.setattr(sensors_module.Comp, "Plain", Plain)
    config = SimpleNamespace(system1=SimpleNamespace(nicknames=["Mai"], nickname_wakeup_threshold=0.7))
    filters = sensors_module.PreFilters(config)
    filters._commands_loaded = True
    event = Event("Mai hello", [Plain("Mai hello")])
    assert await filters.should_process_message(event)
    assert event.get_extra("astrmai_bonus_score") == 0.8


def test_at_other_with_nickname_question_is_not_direct():
    filters = sensors_module.PreFilters(SimpleNamespace(system1=SimpleNamespace()))
    event = Event("@MaiFriend Mai在吗", [SimpleNamespace(type=ComponentType.At, qq="other")])
    assert filters._calculate_nickname_wakeup_score(event, event.message_str, ["Mai"], "bot") == 0


def test_plain_text_without_rendered_at_still_respects_chain_order():
    filters = sensors_module.PreFilters(SimpleNamespace(system1=SimpleNamespace()))
    at = SimpleNamespace(type=ComponentType.At, qq="other")
    plain = SimpleNamespace(type=ComponentType.Plain, text="Mai在吗")
    event = Event("Mai在吗", [at, plain])
    assert filters._calculate_nickname_wakeup_score(event, event.message_str, ["Mai"], "bot") == 0
    event = Event("Mai，你看他说得对吗", [SimpleNamespace(type=ComponentType.Plain, text="Mai，你看"), at,
                                           SimpleNamespace(type=ComponentType.Plain, text="他说得对吗")])
    assert filters._calculate_nickname_wakeup_score(event, event.message_str, ["Mai"], "bot") == 0.7


def test_canonical_keeps_component_names_and_quote_text():
    event = Event(components=[SimpleNamespace(type=ComponentType.At, qq="carol", name="Carol"),
                              SimpleNamespace(type=ComponentType.Reply, id="old", sender_id="bob", sender_nickname="Bob",
                                              message_str="quoted text", time=9876)])
    canonical = ConversationEvent.from_astr_event(event)
    assert canonical.at_actor_names == ("Carol",)
    assert canonical.reply_target_text == "quoted text"
    assert canonical.reply_target_timestamp == 9876


def test_new_identity_format_reaches_final_speaker_lock():
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    block = "实际发言人：Alice(QQ:111)\n该消息引用了：Bob(QQ:222)\n同时@了：QQ:333"
    result = PromptRefiner._render_final_speaker_lock(block)
    assert "当前唯一对话对象是 Alice（QQ 111）" in result
    assert "QQ 222" not in result and "QQ 333" not in result


def test_zero_threshold_does_not_promote_unmentioned_message():
    gate = AttentionGate.__new__(AttentionGate)
    gate.config = SimpleNamespace(system1=SimpleNamespace(nickname_wakeup_threshold=0))
    gate.sensors = None
    assert not gate._is_direct_wakeup_event(Event("ordinary text"), "bot")


def test_weak_window_read_order_does_not_erase_topic():
    store = seeded_store()
    chat = "offline:GroupMessage:group"
    before = store.evaluate_group_message(chat, "Python library install", has_reply_reference=True, now=11500)
    assert len(store.recent(chat, now=11500)) == 1
    after = store.evaluate_group_message(chat, "Python library install", has_reply_reference=True, now=11500)
    assert before == after


def test_private_topic_is_not_cleared_by_group_policy():
    chat = "offline:FriendMessage:alice"
    store = seeded_store(chat)
    before = store.evaluate_private_message(chat, "Python library install", now=11500)
    store.recent(chat, now=11500)
    assert store.evaluate_private_message(chat, "Python library install", now=11500) == before


def test_idle_history_survives_31_minutes_and_eventually_expires():
    store = seeded_store()
    chat = "offline:GroupMessage:group"
    assert len(store.recent(chat, now=11860)) == 1
    policy = store.evaluate_group_message(chat, "Python library install", sender_id="alice", has_reply_reference=True, now=11860)
    assert policy.history_mode == "recent_only"
    assert not policy.allow_provider_session
    assert store.recent(chat, now=10000 + 86401) == []


@pytest.mark.parametrize("age,mode", [(1199, "current_topic"), (1201, "current_topic"), (1799, "current_topic"),
                                      (1801, "recent_only"), (3600, "recent_only"), (7201, "background"), (86401, "none")])
def test_history_mode_boundaries(age, mode):
    store = seeded_store()
    chat = "offline:GroupMessage:group"
    policy = store.evaluate_group_message(chat, "Python library install", sender_id="alice", now=10000 + age)
    assert policy.history_mode == mode
    if mode in {"recent_only", "background", "none"}:
        assert not policy.allow_provider_session


def test_idle_history_does_not_reopen_for_unrelated_sender_or_topic_switch():
    store = seeded_store()
    chat = "offline:GroupMessage:group"
    assert store.evaluate_group_message(chat, "Python library install", sender_id="other", now=11860).history_mode == "none"
    assert store.evaluate_group_message(chat, "换个话题 Python library install", sender_id="alice", now=11860).history_mode == "none"
    assert store.recent("offline:GroupMessage:other", now=11860) == []


@pytest.mark.parametrize("mode", ["recent_only", "background"])
def test_history_modes_roundtrip_without_hidden_session(mode):
    policy = DialogHistoryPolicy.from_value({"history_mode": mode, "group_id": "group"})
    assert policy.history_mode == mode
    assert not policy.uses_lane_history
    assert not policy.allow_provider_session


@pytest.mark.asyncio
async def test_idle_history_reaches_planner_envelope(monkeypatch):
    monkeypatch.setattr(continuity_module.time, "time", lambda: 11860)
    store = seeded_store()
    event = Event()
    planner = PlannerPromptContextMixin()
    planner.config = SimpleNamespace()
    planner.gateway = SimpleNamespace(config=SimpleNamespace())
    planner.conversation_continuity = store
    planner._get_warm_context_bundle = AsyncMock(return_value=SimpleNamespace(quote_text="Bot: pip install example"))
    planner._get_recent_dialogue_transcript = AsyncMock(return_value="")
    event.set_extra("astrmai_focus_thread_context", FocusThreadContext(focus_event=event, focus_message_text=event.message_str))
    result = await planner._build_planning_context(event, [event], event.unified_msg_origin)
    assert result["dialog_history_policy"].history_mode == "recent_only"
    assert "pip install example" in result["prompt_envelope"].warm_zone_transcript
    assert planner._get_warm_context_bundle.await_args.kwargs["max_age_seconds"] == 7200
    planner._get_recent_dialogue_transcript.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("count,limit", [(10, 5), (100, 20)])
async def test_restart_restores_recent_count_without_time_limit(tmp_path, count, limit):
    source = GroupDialogueStore(snapshot_dir=tmp_path, restore_recent_message_limit=limit)
    for index in range(count):
        await source.append_segment("group", event_id=str(index), speaker_id="alice", content=f"history-{index}", timestamp=1000 + index)
    assert await source.persist_snapshot()
    restored = GroupDialogueStore(snapshot_dir=tmp_path, restore_recent_message_limit=limit)
    assert await restored.restore_snapshot() == 1
    thread = restored._threads["group"]
    assert [item.event_id for item in thread.segments] == [str(index) for index in range(count - limit, count)]
    assert await restored.restore_snapshot() == 1
    assert len(thread.segments) == limit


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_real_store_history_reaches_final_prompt(tmp_path, monkeypatch, restart, record_property):
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    monkeypatch.setattr(continuity_module.time, "time", lambda: 11860)
    chat = "offline:GroupMessage:group"
    source = GroupDialogueStore(snapshot_dir=tmp_path, restore_recent_message_limit=20)
    timestamp = 1000 if restart else 10000
    await source.append_segment(chat, event_id="old-user", speaker_id="alice", speaker_name="Alice",
                                content="Python library install", timestamp=timestamp)
    await source.append_segment(chat, event_id="old-bot", speaker_id="bot", speaker_name="Bot", role="assistant",
                                is_bot=True, content="pip install example", timestamp=timestamp + 1)
    await source.append_segment("offline:GroupMessage:other", event_id="other-group", content="SECRET OTHER GROUP", timestamp=timestamp)
    if restart:
        assert await source.persist_snapshot()
        dialogue = GroupDialogueStore(snapshot_dir=tmp_path, restore_recent_message_limit=20)
        assert await dialogue.restore_snapshot() == 2
    else:
        dialogue = source
    if restart:
        await dialogue.append_segment(chat, event_id="new", speaker_id="alice", content="为什么不？", timestamp=11860)
    event = Event("为什么不？" if restart else "Python library install")
    planner = PlannerPromptContextMixin()
    planner.config = SimpleNamespace()
    planner.gateway = SimpleNamespace(config=SimpleNamespace())
    planner.dialogue_store = dialogue
    planner.conversation_continuity = ConversationContinuityStore() if restart else seeded_store()
    planner._get_recent_dialogue_transcript = AsyncMock(return_value="HIDDEN SESSION")
    result = await planner._build_planning_context(event, [event], chat)
    envelope = result["prompt_envelope"]
    system, prompt = await PromptRefiner(memory_engine=None).refine_prompt(
        event, "stable system", context={"disable_rag_injection": True}, prompt_envelope=envelope)
    assert system == "stable system"
    assert "Python library install" in prompt
    assert "pip install example" in prompt
    assert "SECRET OTHER GROUP" not in prompt
    assert "HIDDEN SESSION" not in prompt
    assert result["dialog_history_policy"].history_mode == "recent_only"
    record_property("replay_scenario", "group_restart" if restart else "group_idle_31_minutes")
    record_property("final_prompt", prompt)
    record_property("stable_system_prompt", system)
    if restart:
        assert result["dialog_history_policy"].topic_age_seconds == 10859
    planner._get_recent_dialogue_transcript.assert_not_awaited()


@pytest.mark.asyncio
async def test_count_restore_preserves_recall_and_live_messages(tmp_path):
    store = GroupDialogueStore(snapshot_dir=tmp_path, restore_recent_message_limit=5)
    for index in range(7):
        await store.append_segment("group", event_id=str(index), content=f"secret-{index}", timestamp=10 + index)
    assert await store.mark_recalled("group", "6")
    assert await store.persist_snapshot()
    restored = GroupDialogueStore(snapshot_dir=tmp_path, restore_recent_message_limit=5)
    await restored.append_segment("group", event_id="live", content="live message", timestamp=30)
    assert await restored.restore_snapshot() == 1
    assert await restored.restore_snapshot() == 1
    assert [item.event_id for item in restored._threads["group"].segments] == ["2", "3", "4", "5", "6", "live"]
    recalled = restored._threads["group"].segments[-2]
    assert recalled.is_recalled and "secret-6" not in recalled.content
    assert not restored._pending_direct
    bundle = await restored.get_warm_context_bundle("group", max_age_seconds=0, max_segments=5)
    assert "secret-6" not in bundle.quote_text


@pytest.mark.asyncio
async def test_restore_bootstrap_grace_does_not_reopen_forever(tmp_path, monkeypatch):
    store = GroupDialogueStore(snapshot_dir=tmp_path, restore_recent_message_limit=5)
    await store.append_segment("group", event_id="old", content="old answer", timestamp=1)
    assert await store.persist_snapshot()
    monkeypatch.setattr(continuity_module.time, "time", lambda: 10000)
    restored = GroupDialogueStore(snapshot_dir=tmp_path, restore_recent_message_limit=5)
    assert await restored.restore_snapshot() == 1
    monkeypatch.setattr(continuity_module.time, "time", lambda: 10301)
    bundle = await restored.get_warm_context_bundle("group", max_age_seconds=0, max_segments=5)
    assert not bundle.quote_text and not bundle.summary_text


def test_installed_astrbot_components_through_final_prompt(tmp_path, record_property):
    """A clean process loads installed components, not another suite's AstrBot stubs."""
    source = r'''
import asyncio
import json
from types import SimpleNamespace
from astrbot.api.message_components import At, Plain, Reply
from astrbot.core.message.components import ComponentType
from config import AstrMaiConfig
from astrmai.conversation.ingress.sensors import PreFilters
from astrmai.conversation.attention.gate import AttentionGate
from astrmai.conversation.attention.event_normalizer import build_normalized_events
from astrmai.conversation.attention.turn_target_resolver import resolve_turn_target
from astrmai.conversation.contracts.focus_context import FocusThreadContext
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin
from astrmai.conversation.planning.prompt_refiner import PromptRefiner
class Event:
    unified_msg_origin = "offline:GroupMessage:group"
    timestamp = 1000
    def __init__(self, text, chain):
        self.message_str = text
        self.message_obj = SimpleNamespace(message=chain, message_id="new", self_id="999")
        self.extras = {}
    def get_group_id(self): return "group"
    def get_sender_id(self): return "111"
    def get_sender_name(self): return "Alice"
    def get_self_id(self): return "999"
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
async def run():
    config = AstrMaiConfig()
    config.system1.nicknames = ["Mai"]
    sensors = PreFilters(config)
    sensors._commands_loaded = True
    event = Event("@Carol Mai在吗", [Reply(id="old", sender_id="222", sender_nickname="Bob", message_str="quoted text"),
                                     At(qq="333", name="Carol"), Plain("Mai在吗")])
    assert event.message_obj.message[0].type == ComponentType.Reply
    assert await sensors.should_process_message(event)
    gate = AttentionGate(state_engine=None, judge=None, config=config, sensors=sensors, system2_callback=None)
    candidates = build_normalized_events(gate, [event], "999")
    canonical = candidates[0].canonical_event
    assert canonical.actor_id == "111" and canonical.reply_target_actor_id == "222"
    assert canonical.reply_target_actor_name == "Bob" and canonical.at_actor_ids == ("333",)
    assert not candidates[0].is_direct_wakeup and not candidates[0].is_at_bot
    target, actors = resolve_turn_target(candidates[0], None, candidates, bot_id="999")
    context = FocusThreadContext(focus_event=event, focus_message_text=event.message_str, turn_target=target, actor_set=actors)
    event.set_extra("astrmai_focus_thread_context", context)
    planner = PlannerPromptContextMixin()
    planner.config = config
    planner.gateway = SimpleNamespace(config=config)
    planner.conversation_continuity = ConversationContinuityStore()
    result = await planner._build_planning_context(event, [event], event.unified_msg_origin)
    system, prompt = await PromptRefiner(memory_engine=None).refine_prompt(event, "stable system", context={"disable_rag_injection": True}, prompt_envelope=result["prompt_envelope"])
    assert system == "stable system"
    assert "实际发言人：Alice(QQ:111)" in prompt
    assert "该消息引用了：Bob(QQ:222)" in prompt
    assert "同时@了：QQ:333" in prompt
    assert "实际发言人：Bob" not in prompt
    assert "当前唯一对话对象是 Alice（QQ 111）" in prompt
    assert target.target_actor_id == "222"
    prompt_samples = {"quote_third_party_and_at_other": prompt}
    for text, chain, expected in [
        ("", [Reply(id="bot-old", sender_id="999", message_str="quoted answer")], False),
        (" \u200b\ufeff", [Reply(id="bot-old", sender_id="999"), Plain(" \u200b\ufeff")], False),
        ("@Carol(333)", [Reply(id="bot-old", sender_id="999"), At(qq="333", name="Carol")], False),
        ("？", [Reply(id="bot-old", sender_id="999"), Plain("？")], True),
        ("Mai在吗", [At(qq="333", name="Carol"), Plain("Mai在吗")], False),
        ("Mai hello", [Plain("Mai hello")], True),
        ("hello", [At(qq="999"), Plain("hello")], True),
        ("hello", [Reply(id="bot-old", sender_id="999"), Plain("hello")], True),
        ("Mai在哪里", [At(qq="all"), Plain("Mai在哪里")], True),
        ("ordinary text", [Plain("ordinary text")], False),
    ]:
        sample = Event(text, chain)
        assert await sensors.should_process_message(sample)
        candidate = build_normalized_events(gate, [sample], "999")[0]
        assert (candidate.is_direct_wakeup or candidate.is_reply_to_bot) == expected
        if text == "Mai在吗":
            sample_target, sample_actors = resolve_turn_target(candidate, None, [candidate], bot_id="999")
            sample.set_extra("astrmai_focus_thread_context", FocusThreadContext(
                focus_event=sample, focus_message_text=sample.message_str,
                turn_target=sample_target, actor_set=sample_actors))
            result = await planner._build_planning_context(sample, [sample], sample.unified_msg_origin)
            system, final = await PromptRefiner(memory_engine=None).refine_prompt(
                sample, "stable system", context={"disable_rag_injection": True}, prompt_envelope=result["prompt_envelope"])
            assert system == "stable system"
            assert "实际发言人：Alice" in final and "333" in final
            prompt_samples["at_other_and_bot_nickname"] = final
    print("prompt_samples=" + json.dumps(prompt_samples, ensure_ascii=False))
    print("installed_components_to_final_prompt_ok")
asyncio.run(run())
'''
    project = Path(__file__).resolve().parents[2]
    command = "import sys; sys.path.insert(0, " + repr(str(project)) + ");\n" + source
    result = subprocess.run([sys.executable, "-B", "-c", command], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "installed_components_to_final_prompt_ok" in result.stdout
    samples = next(line.removeprefix("prompt_samples=") for line in result.stdout.splitlines()
                   if line.startswith("prompt_samples="))
    record_property("installed_component_prompts", samples)


@pytest.mark.asyncio
async def test_private_followup_reaches_final_prompt_with_existing_ttl(monkeypatch, record_property):
    from config import AstrMaiConfig
    from astrmai.infrastructure.runtime.lane_manager import LaneManager
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    now = 10000
    monkeypatch.setattr(continuity_module.time, "time", lambda: now)
    event = Event("安装之后呢？")
    event.unified_msg_origin = "offline:FriendMessage:alice"
    event.get_group_id = lambda: ""
    config = AstrMaiConfig()
    lanes = LaneManager(conversation_manager=None, config=config)
    history = [
        {"role": "user", "content": "EXPIRED PRIVATE TEXT", "timestamp": now - 1801},
        {"role": "user", "content": "Python library install", "sender_name": "Alice", "timestamp": now - 600},
        {"role": "assistant", "content": "pip install example", "timestamp": now - 599},
    ]

    async def load_lane(*, lane_key, base_origin):
        assert base_origin == event.unified_msg_origin
        assert lane_key.task_family == "dialog"
        return "private-lane", "offline-conversation", history, False

    monkeypatch.setattr(lanes, "_ensure_lane_with_timeout", load_lane)
    planner = PlannerPromptContextMixin()
    planner.config = config
    planner.gateway = SimpleNamespace(config=config, lane_manager=lanes)
    planner.conversation_continuity = seeded_store(event.unified_msg_origin)
    result = await planner._build_planning_context(event, [event], event.unified_msg_origin)
    system, prompt = await PromptRefiner(memory_engine=None).refine_prompt(
        event, "stable system", context={"disable_rag_injection": True}, prompt_envelope=result["prompt_envelope"])
    assert system == "stable system"
    assert "Python library install" in prompt and "pip install example" in prompt
    assert "EXPIRED PRIVATE TEXT" not in prompt
    assert not result["dialog_history_policy"].group_id
    record_property("replay_scenario", "private_followup")
    record_property("final_prompt", prompt)
