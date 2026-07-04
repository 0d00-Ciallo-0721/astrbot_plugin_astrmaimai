from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.integration.host.test_host_mock_validation import _install_extended_astrbot_stubs


class _MessageObj:
    def __init__(self, self_id: str, message=None):
        self.self_id = self_id
        self.message = list(message or [])
        self.message_id = "msg-1"


class _Event:
    def __init__(self, *, text: str, components=None):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_obj = _MessageObj("bot-1", components)
        self.timestamp = 123.0
        self._extra = {"wakeup": True}
        self._stopped = False

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"

    def get_group_id(self):
        return "group-1"

    def get_self_id(self):
        return "bot-1"

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def stop_event(self):
        self._stopped = True

    def plain_result(self, text):
        return {"type": "plain", "text": text}


class _ConversationManager:
    def __init__(self):
        self.pairs = []

    async def get_curr_conversation_id(self, chat_id):
        return f"conv:{chat_id}"

    async def add_message_pair(self, cid, user_message, assistant_message):
        self.pairs.append((cid, user_message, assistant_message))


class _Context:
    def __init__(self):
        self.sent = []
        self.conversation_manager = _ConversationManager()
        self.command_manager = SimpleNamespace(commands={})
        self.shared_dict = {}

    async def send_message(self, umo, chain):
        self.sent.append((umo, list(getattr(chain, "chain", []))))


class _Persistence:
    def __init__(self):
        self.chat_states = {}
        self.user_profiles = {}

    async def load_chat_state(self, chat_id):
        return self.chat_states.get(chat_id)

    async def save_chat_state(self, chat_id, state):
        self.chat_states[chat_id] = state

    async def load_user_profile(self, user_id):
        return self.user_profiles.get(user_id)

    async def save_user_profile(self, *args):
        if len(args) == 1:
            profile = args[0]
            self.user_profiles[getattr(profile, "user_id", "")] = profile
        else:
            self.user_profiles[args[0]] = args[1]

    def load_persona_cache(self):
        return {}

    def save_persona_cache(self, cache):
        self.persona_cache = dict(cache or {})


class _Gateway:
    def __init__(self, context, config):
        self.context = context
        self.config = config
        self.calls = []
        self.lane_manager = SimpleNamespace(
            get_recent_transcript=lambda *args, **kwargs: "",
            get_lane_history=lambda *args, **kwargs: [],
        )

    def get_agent_models(self):
        return ["mock/agent"]

    async def chat_in_lane_result(self, **kwargs):
        self.calls.append(("chat", dict(kwargs)))
        if kwargs.get("is_json"):
            return SimpleNamespace(parsed_json={"mood_tag": "happy", "mood_value": 0.35})
        return SimpleNamespace(text="收到，我会按这个方向继续。")

    async def tool_chat_in_lane_result(self, **kwargs):
        self.calls.append(("tool", dict(kwargs)))
        return SimpleNamespace(text="收到，我会按这个方向继续。")

    async def call_judge_task(self, **kwargs):
        return {"action": "REPLY", "thought": "direct wakeup", "relevance": 0.8}

    async def call_mood_task(self, **kwargs):
        return {"mood_tag": "happy", "mood_value": 0.35}

    async def call_persona_task(self, **kwargs):
        return {"summary": "friendly assistant", "style": "short", "shards": {}, "is_full_ready": True}


class _PersonaSummarizer:
    async def get_summary(self, **kwargs):
        return {"summary": "friendly assistant", "style": "short", "shards": {}, "is_full_ready": True}


class _RuntimeCoordinator:
    class _Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def locked(self):
            return False

        def release(self):
            return None

    async def get_sys2_lock(self, chat_id):
        return self._Lock()

    async def try_acquire_executor(self, chat_id, max_pending=2):
        return self._Lock()

    async def release_executor(self, chat_id):
        return None

    async def evaluate_reply_freshness(self, *args, **kwargs):
        return SimpleNamespace(state="fresh", reason="")

    async def update_wait_targets(self, *args, **kwargs):
        return None

    async def get_latest_activity(self, chat_id):
        return 123.0


class _LaneManager:
    async def ensure_lane(self, *args, **kwargs):
        return None

    async def get_recent_transcript(self, *args, **kwargs):
        return ""

    async def get_lane_history(self, *args, **kwargs):
        return []


class MessageToReplyPipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_extended_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_direct_message_flows_through_attention_planner_executor_reply(self):
        from config import AstrMaiConfig
        from astrmai.app.runtime_context import CognitionServices, CoreServices, InteractionServices
        from astrmai.app.plugin_facade import PluginFacade
        from astrmai.app.runtime_context import PluginRuntimeContext
        from astrmai.conversation.attention.gate import AttentionGate
        from astrmai.conversation.decision.judge import Judge
        from astrmai.conversation.execution.reply_service import ReplyService
        from astrmai.conversation.execution.system2_runner import System2Runner
        from astrmai.conversation.planning.context_engine import ContextEngine
        from astrmai.conversation.planning.planner import CognitiveDecision, Planner
        from astrmai.conversation.planning.prompt_refiner import PromptRefiner
        from astrmai.memory.services.memory_turn_pipeline import MemoryTurnPipeline
        from astrmai.state.chat_state_service import StateEngine

        config = AstrMaiConfig(
            provider={"agent_models": ["mock/agent"]},
            reply={"follow_up_probability": 0.0, "meme_probability": 0, "typing_speed_factor": 0.0},
            attention={"debounce_window": 0.0},
            memory={"auto_recall_probability": 0.0},
        )
        context = _Context()
        persistence = _Persistence()
        gateway = _Gateway(context, config)
        state_engine = StateEngine(persistence, gateway, config=config)
        memory_pipeline = MemoryTurnPipeline(
            context=context,
            gateway=gateway,
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(
                process_committed_turn=lambda turn: SimpleNamespace(hit=False, memory_id=""),
                should_run_llm_backfill=lambda *args, **kwargs: False,
            ),
            config=config,
        )
        memory_engine = SimpleNamespace(memory_pipeline=memory_pipeline, instant_gate=memory_pipeline.instant_gate)
        reply_service = ReplyService(state_engine, state_engine.mood_manager, config=config, memory_engine=memory_engine)
        context_engine = ContextEngine(
            db=SimpleNamespace(get_chat_state=lambda chat_id: None),
            persona_summarizer=_PersonaSummarizer(),
            config=config,
            context=context,
        )
        prompt_refiner = PromptRefiner(memory_engine=memory_engine, config=config)
        async def _process_bot_reply(*args, **kwargs):
            return None

        async def _decide(**kwargs):
            return CognitiveDecision(action="reply", intent="answer", memory_policy="light", action_tier="none")

        async def _no_follow(*args, **kwargs):
            return None

        planner = Planner(
            context=context,
            gateway=gateway,
            context_engine=context_engine,
            reply_engine=reply_service,
            memory_engine=memory_engine,
            evolution_manager=SimpleNamespace(get_active_patterns=lambda chat_id: "", process_bot_reply=_process_bot_reply),
            state_engine=state_engine,
            prompt_refiner=prompt_refiner,
            runtime_coordinator=None,
            cognitive_loop=SimpleNamespace(
                gate_decision=lambda *a, **k: SimpleNamespace(should_run=True, skip_reason="", signals=[]),
                mark_gate_decision=lambda *a, **k: None,
                decide=_decide,
            ),
        )
        planner._should_follow_up = _no_follow

        runner_runtime = SimpleNamespace(
            runtime_coordinator=_RuntimeCoordinator(),
            state_engine=SimpleNamespace(consume_energy=lambda chat_id: asyncio.sleep(0)),
            lane_manager=_LaneManager(),
            system2_planner=planner,
            private_chat_manager=None,
            group_reply_wait_manager=None,
        )
        runner = System2Runner(runner_runtime)
        gate = AttentionGate(
            state_engine=state_engine,
            judge=Judge(gateway, state_engine, config=config),
            sensors=SimpleNamespace(is_wakeup_signal=lambda event, self_id: event.get_extra("wakeup", False)),
            system2_callback=runner.run,
            config=config,
        )
        async def _record_user_message(event):
            return None

        runtime = PluginRuntimeContext(
            host_context=context,
            config=config,
            raw_config={},
            runtime_coordinator=_RuntimeCoordinator(),
            host_bridge=SimpleNamespace(suppress_default_llm=lambda event: "(suppressed)"),
            core=CoreServices(
                persistence=persistence,
                gateway=gateway,
                state_engine=state_engine,
                sensors=SimpleNamespace(),
                judge=gate.judge,
                memory_engine=memory_engine,
                lane_manager=_LaneManager(),
            ),
            cognition=CognitionServices(
                reply_engine=reply_service,
                evolution=SimpleNamespace(record_user_message=_record_user_message),
                system2_planner=planner,
            ),
            interaction=InteractionServices(attention_gate=gate),
        )
        facade = PluginFacade(runtime)
        event = _Event(text="Mai")

        async def run():
            yielded = [item async for item in facade.on_global_message(event)]
            tasks = list(gate._background_tasks)
            if tasks:
                await asyncio.gather(*tasks)
            return yielded

        yielded = asyncio.run(run())
        sent_text = "".join(str(part.text if hasattr(part, "text") else part) for _umo, chain in context.sent for part in chain)

        self.assertEqual(yielded, [{"type": "plain", "text": "(suppressed)"}])
        self.assertTrue(sent_text)
        self.assertNotIn("[TERMINAL_YIELD]", sent_text)
        self.assertIsNotNone(event.get_extra("astrmai_turn_context"))
        self.assertIn("收到", sent_text, {"trace": event.get_extra("astrmai_trace_log"), "gateway": gateway.calls})
        self.assertNotEqual(persistence.chat_states["group-1"].mood, 0.0)
        self.assertIn(event.unified_msg_origin, memory_pipeline._session_history_buffer)


if __name__ == "__main__":
    unittest.main()
