import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from tests.test_persistence_regressions import _install_astrbot_stubs


def _install_memory_stubs():
    processor_mod = types.ModuleType("astrmai.memory.processor")

    class MemoryProcessor:
        def __init__(self, gateway):
            self.gateway = gateway

        async def process_conversation(self, text):
            return {
                "summary": "summary",
                "key_facts": ["fact"],
                "topics": ["topic"],
                "sentiment": "neutral",
                "reflection": "",
                "nodes": [],
                "importance": 0.4,
            }

    topic_mod = types.ModuleType("astrmai.memory.topic_summarizer")

    class TopicSummarizer:
        def __init__(self, gateway, config):
            self.gateway = gateway
            self.config = config
            self.calls = []

        async def process_history(self, messages, session_id=""):
            self.calls.append((messages, session_id))
            return []

    processor_mod.MemoryProcessor = MemoryProcessor
    topic_mod.TopicSummarizer = TopicSummarizer

    sys.modules["astrmai.memory.processor"] = processor_mod
    sys.modules["astrmai.memory.topic_summarizer"] = topic_mod


class _FakeExecResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def exec(self, statement):
        return _FakeExecResult(self._rows)


class _FakeSessionContext:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return _FakeSession(self._rows)

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeDBService:
    def __init__(self, rows):
        self._rows = rows

    def get_session(self):
        return _FakeSessionContext(self._rows)


class _FakeEngine:
    def __init__(self):
        self.topic_results = []
        self.memories = []

    async def store_topic_results(self, topic_results, session_id, persona_id=None):
        self.topic_results.append((topic_results, session_id, persona_id))

    async def add_memory(self, content, session_id, importance):
        self.memories.append((content, session_id, importance))


class MemoryContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        _install_memory_stubs()
        sys.modules.pop("astrmai.memory.summarizer", None)
        self.summarizer_mod = importlib.import_module("astrmai.memory.summarizer")
        self.summarizer_mod = importlib.reload(self.summarizer_mod)
        self.datamodels_mod = importlib.import_module("astrmai.infra.datamodels")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_extract_and_summarize_history_passes_structured_messages(self):
        logs = [
            self.datamodels_mod.MessageLog(
                group_id="chat-1",
                sender_id="user-1",
                sender_name="Alice",
                content="hello",
                timestamp=100.0,
            ),
            self.datamodels_mod.MessageLog(
                group_id="chat-1",
                sender_id="user-2",
                sender_name="Bob",
                content="world",
                timestamp=101.0,
            ),
        ]

        gateway = SimpleNamespace(
            config=SimpleNamespace(memory=SimpleNamespace(cleanup_interval=60, summary_threshold=2)),
            context=SimpleNamespace(),
        )
        context = SimpleNamespace(astrmai_plugin=SimpleNamespace(db_service=_FakeDBService(logs)))
        summarizer = self.summarizer_mod.ChatHistorySummarizer(context, gateway, _FakeEngine())

        captured = {}

        async def _capture(session_id, chat_history_text, persona_id=None, messages=None):
            captured["session_id"] = session_id
            captured["chat_history_text"] = chat_history_text
            captured["messages"] = messages

        summarizer.summarize_session = _capture

        asyncio.run(summarizer.extract_and_summarize_history("chat-1"))

        self.assertEqual(captured["session_id"], "chat-1")
        self.assertEqual(len(captured["messages"]), 2)
        self.assertEqual(captured["messages"][0]["sender"], "Alice")
        self.assertEqual(captured["messages"][1]["content"], "world")
        self.assertFalse(asyncio.iscoroutine(captured["messages"]))

    def test_summarize_session_uses_structured_messages_contract(self):
        gateway = SimpleNamespace(
            config=SimpleNamespace(memory=SimpleNamespace(cleanup_interval=60, summary_threshold=2)),
            context=SimpleNamespace(),
        )
        summarizer = self.summarizer_mod.ChatHistorySummarizer(SimpleNamespace(), gateway, _FakeEngine())

        recorded = {}

        class FakeTopicSummarizer:
            async def process_history(self, messages, session_id=""):
                recorded["messages"] = messages
                recorded["session_id"] = session_id
                return []

        summarizer.topic_summarizer = FakeTopicSummarizer()

        messages = [{"sender": "Alice", "content": "hello", "timestamp": 1.0}]
        asyncio.run(summarizer.summarize_session("chat-1", "ignored", messages=messages))

        self.assertEqual(recorded["session_id"], "chat-1")
        self.assertEqual(recorded["messages"], messages)


if __name__ == "__main__":
    unittest.main()
