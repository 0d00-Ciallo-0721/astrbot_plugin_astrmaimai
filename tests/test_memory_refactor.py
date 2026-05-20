import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeDB:
    def __init__(self):
        self.saved = []

    async def save_retrieval_trace_async(self, trace):
        self.saved.append(trace)


class MemoryRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.memory.retrieval.react_retriever", None)
        self.mod = importlib.import_module("astrmai.memory.retrieval.react_retriever")
        self.mod = importlib.reload(self.mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_react_retriever_saves_trace_using_contract(self):
        retriever = self.mod.ReActRetriever(
            memory_engine=None,
            db_service=_FakeDB(),
            gateway=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
        )

        async def _run():
            await retriever._save_trace(
                chat_id="chat-1",
                sender_name="Bob",
                query="remember Alice",
                planner_question="Alice",
                collected_info=[{"tool": "query_person", "result": "evt_1 Alice profile"}],
                final_answer="Alice likes travel.",
            )

        asyncio.run(_run())

        self.assertEqual(len(retriever.db_service.saved), 1)
        trace = retriever.db_service.saved[0]
        self.assertEqual(trace.chat_id, "chat-1")
        self.assertEqual(json.loads(trace.source_layers), ["person"])

    def test_memory_processor_fallback_prompt_replaces_payload_without_format_errors(self):
        sys.modules.pop("astrmai.memory.services.memory_processor", None)
        memory_mod = importlib.import_module("astrmai.memory.services.memory_processor")
        memory_mod = importlib.reload(memory_mod)

        processor = memory_mod.MemoryProcessor(SimpleNamespace())
        rendered_history = processor.prompt_template.replace("{history}", "history-x")
        rendered_facts = processor.node_prompt_template.replace("{facts}", "facts-y")

        self.assertIn("history-x", rendered_history)
        self.assertIn("facts-y", rendered_facts)

    def test_memory_processor_uses_chat_scoped_lane_for_non_global_session(self):
        sys.modules.pop("astrmai.memory.services.memory_processor", None)
        memory_mod = importlib.import_module("astrmai.memory.services.memory_processor")
        memory_mod = importlib.reload(memory_mod)

        class _Gateway:
            def __init__(self):
                self.calls = []

            async def call_data_process_task(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return {
                        "summary": "ok",
                        "topics": ["a"],
                        "key_facts": ["f"],
                        "reflection": "r",
                        "sentiment": "neutral",
                        "importance": 0.6,
                    }
                return {"nodes": [], "deleted_nodes": []}

        gateway = _Gateway()
        processor = memory_mod.MemoryProcessor(gateway)

        async def _run():
            await processor.process_conversation("hello", session_id="chat-42")

        asyncio.run(_run())

        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(gateway.calls[0]["lane_key"].scope_id, "chat-42")
        self.assertEqual(gateway.calls[0]["lane_key"].scope_kind, "chat")

    def test_chat_history_summarizer_describes_memory_candidate_from_buffer(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        summarizer_mod = importlib.import_module("astrmai.memory.services.summarizer")
        summarizer_mod = importlib.reload(summarizer_mod)

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None))
        config = SimpleNamespace(memory=SimpleNamespace(summary_threshold=3, cleanup_interval=3600))
        summarizer = summarizer_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(),
            config=config,
        )
        summarizer._session_history_buffer["chat-1"] = {
            "buffer": ["u1", "a1", "u2", "a2", "u3", "a3"],
            "last_update": 1.0,
            "cooldown_until": 0.0,
            "failures": 0,
        }

        result = asyncio.run(summarizer.describe_session_eligibility("chat-1"))

        self.assertTrue(result["eligible"])
        self.assertTrue(result["candidate_present"])
        self.assertEqual(result["reason"], "eligible")
        self.assertEqual(result["pending_messages"], 6)

    def test_chat_history_summarizer_runs_once_for_session_when_threshold_reached(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        summarizer_mod = importlib.import_module("astrmai.memory.services.summarizer")
        summarizer_mod = importlib.reload(summarizer_mod)

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None))
        config = SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600))
        summarizer = summarizer_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(),
            config=config,
        )
        calls = []

        async def _fake_summarize_session(*, session_id, chat_history_text, persona_id=None, messages=None):
            calls.append((session_id, chat_history_text))

        summarizer.summarize_session = _fake_summarize_session
        summarizer._session_history_buffer["chat-2"] = {
            "buffer": ["u1", "a1", "u2", "a2"],
            "last_update": 1.0,
            "cooldown_until": 0.0,
            "failures": 0,
        }

        result = asyncio.run(summarizer.run_once_for_session("chat-2"))

        self.assertTrue(result["performed"])
        self.assertEqual(result["reason"], "summarized")
        self.assertEqual(calls[0][0], "chat-2")
        self.assertEqual(summarizer._session_history_buffer["chat-2"]["buffer"], [])

    def test_chat_history_summarizer_ingests_committed_turn_into_buffer(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        summarizer_mod = importlib.import_module("astrmai.memory.services.summarizer")
        summarizer_mod = importlib.reload(summarizer_mod)

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None))
        config = SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600))
        summarizer = summarizer_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(),
            config=config,
        )

        result = asyncio.run(
            summarizer.ingest_committed_turn(
                "chat-3",
                "Alice: hello",
                "bot: hi",
                source="test",
            )
        )

        self.assertTrue(result["performed"])
        self.assertEqual(result["reason"], "ingested")
        self.assertEqual(
            summarizer._session_history_buffer["chat-3"]["buffer"],
            ["用户/旁白：Alice: hello", "Bot：bot: hi"],
        )

    def test_chat_history_summarizer_ignores_proactive_turns(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        summarizer_mod = importlib.import_module("astrmai.memory.services.summarizer")
        summarizer_mod = importlib.reload(summarizer_mod)

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None))
        config = SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600))
        summarizer = summarizer_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(),
            config=config,
        )

        result = asyncio.run(
            summarizer.ingest_committed_turn(
                "chat-4",
                "Alice: hello",
                "bot: hi",
                source="test",
                is_proactive=True,
            )
        )

        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], "proactive_ignored")
        self.assertNotIn("chat-4", summarizer._session_history_buffer)


if __name__ == "__main__":
    unittest.main()
