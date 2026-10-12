"""Local prompt-context budgets, including final prompt rendering (no network)."""
import math
import statistics
import time
from types import SimpleNamespace

import pytest

from config import AstrMaiConfig
from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.conversation.ingress.sensors import PreFilters
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin
from astrmai.conversation.planning.prompt_refiner import PromptRefiner
from tests.unit.test_prompt_context_closure import ComponentType, Event, seeded_store


SAMPLES = 40


def report_budget(name, durations, limit_ms, record_property):
    values = sorted(durations)
    p95 = values[math.ceil(len(values) * 0.95) - 1]
    result = {"samples": len(values), "median_ms": statistics.median(values),
              "p95_ms": p95, "max_ms": max(values), "limit_ms": limit_ms}
    record_property(name, result)
    print(f"{name}: {result}")
    assert max(values) < limit_ms, result


def test_nickname_score_budget(record_property):
    filters = PreFilters(AstrMaiConfig())
    event = Event("Mai，你看他说得对吗", [
        SimpleNamespace(type=ComponentType.Plain, text="Mai，你看他说得对吗"),
        SimpleNamespace(type=ComponentType.At, qq="other"),
    ])
    durations = []
    for _ in range(SAMPLES):
        started = time.perf_counter_ns()
        score = filters._calculate_nickname_wakeup_score(event, event.message_str, ["Mai"], "bot")
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        assert score == 0.7
    report_budget("nickname_score", durations, 10, record_property)


@pytest.fixture
async def history_planner():
    now = time.time()
    event = Event("Python library install")
    chat = event.unified_msg_origin
    dialogue = GroupDialogueStore()
    for index in range(100):
        await dialogue.append_segment(chat, event_id=str(index), speaker_id="alice" if index % 2 == 0 else "bot",
                                      speaker_name="Alice" if index % 2 == 0 else "Bot",
                                      role="user" if index % 2 == 0 else "assistant", is_bot=index % 2 == 1,
                                      content=f"Python library install {index}", timestamp=now - 1860 + index)
    assert len(dialogue._threads[chat].segments) == 100
    continuity = seeded_store(chat)
    # Establish a 31-minute history gap without freezing the measurement clock.
    state = continuity._state(chat)
    state.last_goal_update_ts = now - 1860
    state.turns[0].timestamp = now - 1860
    planner = PlannerPromptContextMixin()
    planner.config = AstrMaiConfig()
    planner.gateway = SimpleNamespace(config=planner.config)
    planner.dialogue_store = dialogue
    planner.conversation_continuity = continuity
    return planner, event


async def test_history_loading_budget(history_planner, record_property):
    planner, event = history_planner
    durations = []
    for _ in range(SAMPLES):
        started = time.perf_counter_ns()
        bundle = await planner.dialogue_store.get_warm_context_bundle(
            event.unified_msg_origin, max_segments=20, max_age_seconds=7200)
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        assert "Python library install 99" in bundle.quote_text
        assert len(bundle.quote_event_ids) <= 20
    report_budget("history_loading", durations, 100, record_property)


async def test_final_prompt_building_budget(history_planner, record_property):
    planner, event = history_planner
    refiner = PromptRefiner(memory_engine=None)
    durations = []
    for _ in range(SAMPLES):
        started = time.perf_counter_ns()
        result = await planner._build_planning_context(event, [event], event.unified_msg_origin)
        system, prompt = await refiner.refine_prompt(
            event, "stable system", context={"disable_rag_injection": True},
            prompt_envelope=result["prompt_envelope"])
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        assert system == "stable system"
        assert "Python library install 99" in prompt
        assert "实际发言人：Alice" in prompt
        assert result["dialog_history_policy"].history_mode == "recent_only"
    report_budget("final_prompt_building", durations, 50, record_property)


def test_layered_ttl_budget(record_property):
    durations = []
    for index in range(SAMPLES):
        store = seeded_store()
        age = (1199, 1801, 7201, 86401)[index % 4]
        expected = ("current_topic", "recent_only", "background", "none")[index % 4]
        started = time.perf_counter_ns()
        policy = store.evaluate_group_message("offline:GroupMessage:group", "Python library install",
                                              sender_id="alice", now=10000 + age)
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        assert policy.history_mode == expected
    report_budget("layered_ttl", durations, 5, record_property)
