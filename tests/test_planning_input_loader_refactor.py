import asyncio
import importlib
import sys
import time
from types import SimpleNamespace

from tests.original_ported.helpers import _install_astrbot_stubs


class _Event:
    def __init__(self):
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class _AgencyRuntime:
    def summary(self, chat_id):
        return f"agency summary for {chat_id}"

    def cooldown_tags(self, chat_id):
        return {"meme", "like"}


class _ContinuityStore:
    def summary(self, chat_id):
        return "Conversation continuity:\ncurrent_topic=homework"

    def snapshot(self, chat_id):
        return {
            "current_topic": "homework",
            "current_goal": "help sort homework",
            "goal_status": "continuing",
            "continuity_weight": "strong",
            "turn_count": 2,
        }


class _HeartflowManager:
    def get_hidden_context(self, chat_id):
        return "interest=0.8; guidance=join"

    def get_state(self, chat_id):
        return SimpleNamespace(interest=0.8, talk_willingness=0.5)

    def get_latest_pulse(self, chat_id):
        return SimpleNamespace(pulse_type="join")


class _ExpressionSelector:
    def __init__(self, calls):
        self.calls = calls

    async def select(self, **kwargs):
        self.calls.append(("expression", kwargs))
        return "expression habits"


class _TraceExpressionSelector:
    async def select_with_trace(self, **kwargs):
        return "trace expression habits", [SimpleNamespace(id="mem-expression-1", expression="soft ping")]


class _EvolutionManager:
    def __init__(self, calls):
        self.calls = calls

    def get_active_patterns(self, chat_id):
        self.calls.append(("slang", chat_id))
        return "slang context"


class _Db:
    def __init__(self, calls):
        self.calls = calls

    async def load_jargon_list(self, chat_id, limit=8):
        self.calls.append(("jargon", chat_id, limit))
        return [{"text": "x", "meaning": "y", "situation": "chat"}]


class _RetrievalService:
    def __init__(self, calls):
        self.calls = calls

    async def retrieve(self, query):
        self.calls.append(("canonical_jargon", query.query, list(query.layers or []), query.intent))
        return [
            SimpleNamespace(
                id="mem-jargon-1",
                content="x",
                summary="y",
                metadata={"meaning": "y", "scene": "chat"},
            )
        ]


class _EmptyRetrievalService:
    def __init__(self, calls):
        self.calls = calls

    async def retrieve(self, query):
        self.calls.append(("canonical_jargon_empty", query.query, list(query.layers or []), query.intent))
        return []


class _GoalManager:
    def __init__(self, calls):
        self.calls = calls

    async def analyze_and_update(self, chat_id, recent_messages):
        return "keep current topic"

    def get_goals_context(self, chat_id):
        self.calls.append(("goals_context", chat_id))
        return "goal context"


class _StateEngine:
    def __init__(self, calls):
        self.calls = calls
        self.relationship_engine = SimpleNamespace(
            get_or_create=lambda user_id: SimpleNamespace(social_score=3, trust=1.0)
        )

    async def get_state(self, chat_id):
        self.calls.append(("state", chat_id))
        return SimpleNamespace(energy=0.7)

    async def get_user_profile(self, user_id):
        self.calls.append(("profile", user_id))
        return SimpleNamespace(social_score=5)


class _Planner:
    def __init__(self):
        self.calls = []
        self.agency_runtime = _AgencyRuntime()
        self.conversation_continuity = _ContinuityStore()
        self.heartflow_manager = _HeartflowManager()
        self.expression_selector = _ExpressionSelector(self.calls)
        self.evolution_manager = _EvolutionManager(self.calls)
        self.context_engine = SimpleNamespace(db=_Db(self.calls))
        self.memory_engine = SimpleNamespace(retrieval_service=_RetrievalService(self.calls))
        self.goal_manager = _GoalManager(self.calls)
        self.state_engine = _StateEngine(self.calls)

    @staticmethod
    def _planner_side_input_text(prompt_envelope, window_lines, *, recent_only=False):
        return "\n".join(window_lines[-3:] if recent_only else window_lines)

    async def _load_memory_feedback_summary(self, chat_id):
        self.calls.append(("memory_feedback", chat_id))
        return "Long-term behavior and memory feedback:\n- agency: keep it varied"


def _load_loader_module(tmp_path):
    _install_astrbot_stubs(str(tmp_path))
    sys.modules.pop("astrmai.conversation.planning.planning_input_loader", None)
    module = importlib.import_module("astrmai.conversation.planning.planning_input_loader")
    return importlib.reload(module)


def test_pre_budget_inputs_run_concurrently_and_write_context(tmp_path):
    module = _load_loader_module(tmp_path)
    planner = _Planner()
    loader = module.PlanningInputLoader(planner)
    event = _Event()

    async def _slow_agency(chat_id):
        await asyncio.sleep(0.05)
        return {"reflection_summary": "agency", "cooldown_tags": ["meme"]}

    async def _slow_continuity(chat_id):
        await asyncio.sleep(0.05)
        return {"summary": "Conversation continuity:\ncurrent_topic=x", "snapshot": {"current_topic": "x"}}

    async def _slow_heartflow(chat_id):
        await asyncio.sleep(0.05)
        return {"context_text": "interest=0.8", "interest": 0.8, "talk_willingness": 0.4, "pulse_type": "join"}

    loader._agency_snapshot = _slow_agency
    loader._continuity_snapshot = _slow_continuity
    loader._heartflow_snapshot = _slow_heartflow

    started = time.perf_counter()
    result = asyncio.run(loader.load_pre_budget(event, "chat-1"))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.12
    assert result.reflection_summary == "agency"
    assert event.get_extra("astrmai_agency_reflection_summary") == "agency"
    assert event.get_extra("astrmai_heartflow_pulse") == "join"
    turn_context = event.get_extra("astrmai_turn_context")
    assert turn_context.continuity.current_topic == "x"
    assert len(event.get_extra("astrmai_side_input_timings")) == 3


def test_budgeted_prompt_inputs_respect_think_level(tmp_path):
    module = _load_loader_module(tmp_path)
    planner = _Planner()
    loader = module.PlanningInputLoader(planner)

    level_zero_event = _Event()
    level_zero = asyncio.run(
        loader.load_prompt_inputs(level_zero_event, "chat-1", None, ["hello"], 0, user_id="user-1")
    )
    assert level_zero["stable_expression_habits"] == ""
    assert level_zero["situational_style_cues"] == ""
    assert level_zero["stable_jargon_explanation"] == ""
    assert level_zero["expression_habits"] == ""  # compatibility mirror
    assert planner.calls == []
    assert level_zero_event.get_extra("astrmai_side_input_timings")[0]["skipped_reason"] == "think_level_0"

    level_one_event = _Event()
    level_one = asyncio.run(
        loader.load_prompt_inputs(level_one_event, "chat-1", None, ["hello there"], 1, user_id="user-1")
    )
    assert level_one["stable_expression_habits"] == "expression habits"
    assert level_one["situational_style_cues"] == ""
    assert level_one["expression_habits"] == "expression habits"  # compatibility mirror
    assert level_one["tool_state"].state.energy == 0.7

    level_two_event = _Event()
    level_two = asyncio.run(
        loader.load_prompt_inputs(level_two_event, "chat-1", None, ["please analyze this"], 2, user_id="user-1")
    )
    assert level_two["stable_expression_habits"] == "expression habits"
    assert level_two["situational_style_cues"] == ""
    assert level_two["stable_jargon_explanation"] == ""
    assert level_two["slang_context"] == ""  # compatibility mirror
    assert level_two["jargon_explanation"] == ""  # compatibility mirror
    assert ("jargon", "chat-1", 8) not in planner.calls
    assert level_two["planner_reasoning"] == "keep current topic"
    assert level_two["goals_context"] == "goal context"


def test_jargon_loader_returns_empty_instead_of_legacy_fallback(tmp_path):
    module = _load_loader_module(tmp_path)
    planner = _Planner()
    loader = module.PlanningInputLoader(planner)

    query_text = "please check whether bigbird slang is still active"
    result = asyncio.run(
        loader._load_jargon_explanation(_Event(), "chat-1", None, [query_text])
    )

    assert result == ""
    assert ("jargon", "chat-1", 8) not in planner.calls


def test_expression_habit_loader_writes_canonical_trace(tmp_path):
    module = _load_loader_module(tmp_path)
    planner = _Planner()
    planner.expression_selector = _TraceExpressionSelector()
    loader = module.PlanningInputLoader(planner)
    event = _Event()

    result = asyncio.run(
        loader._load_expression_habits(event, "chat-1", None, ["this context is long enough for expression"], 1)
    )

    assert result == "trace expression habits"
    trace = event.get_extra("astrmai_expression_pattern_trace")
    assert trace.selected_ids == ["mem-expression-1"]
    turn_context = event.get_extra("astrmai_turn_context")
    assert turn_context.expression_patterns.selected_ids == ["mem-expression-1"]
    assert turn_context.expression_patterns.injected is True


def test_memory_feedback_and_failures_degrade_without_blocking(tmp_path):
    module = _load_loader_module(tmp_path)
    planner = _Planner()
    loader = module.PlanningInputLoader(planner)

    skipped_event = _Event()
    skipped = asyncio.run(loader.load_memory_feedback(skipped_event, "chat-1", 1))
    assert skipped == ""
    assert skipped_event.get_extra("astrmai_side_input_timings")[0]["skipped_reason"] == "think_level_1"

    loaded_event = _Event()
    loaded = asyncio.run(loader.load_memory_feedback(loaded_event, "chat-1", 2))
    assert "Long-term behavior" in loaded
    assert loaded_event.get_extra("astrmai_memory_feedback_summary") == loaded

    async def _broken_expression(*args, **kwargs):
        raise RuntimeError("boom")

    loader._load_expression_habits = _broken_expression
    failed_event = _Event()
    result = asyncio.run(
        loader.load_prompt_inputs(failed_event, "chat-1", None, ["please analyze this"], 1, user_id="user-1")
    )
    assert result["stable_expression_habits"] == ""
    assert result["expression_habits"] == ""  # compatibility mirror
    failed_timing = next(item for item in failed_event.get_extra("astrmai_side_input_timings") if item["name"] == "expression_habits")
    assert failed_timing["ok"] is False
    assert "RuntimeError" in failed_timing["error"]
