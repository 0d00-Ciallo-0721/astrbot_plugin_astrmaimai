import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_astrbot_stubs


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


class MemoryContractMigratedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.memory.services.session_memory_summarizer", None)
        self.summarizer_mod = importlib.import_module("astrmai.memory.services.session_memory_summarizer")
        self.summarizer_mod = importlib.reload(self.summarizer_mod)
        self.datamodels_mod = importlib.import_module("astrmai.infrastructure.persistence.orm_models")

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
        summarizer = self.summarizer_mod.SessionMemorySummarizer(context, gateway, _FakeEngine())

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
        self.assertEqual(captured["messages"][0]["sender_id"], "user-1")
        self.assertEqual(captured["messages"][1]["content"], "world")
        self.assertFalse(asyncio.iscoroutine(captured["messages"]))

    def test_summarize_session_uses_structured_messages_contract(self):
        gateway = SimpleNamespace(
            config=SimpleNamespace(memory=SimpleNamespace(cleanup_interval=60, summary_threshold=2)),
            context=SimpleNamespace(),
        )
        summarizer = self.summarizer_mod.SessionMemorySummarizer(SimpleNamespace(), gateway, _FakeEngine())

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

    def test_group_summary_uses_stable_identity_without_claiming_session_as_sender(self):
        requests = []
        claim_requests = []

        async def _write(request):
            requests.append(request)
            return "memory-1"

        gateway = SimpleNamespace(
            config=SimpleNamespace(memory=SimpleNamespace(cleanup_interval=60, summary_threshold=2)),
            context=SimpleNamespace(),
        )
        engine = SimpleNamespace(write_service=SimpleNamespace(write=_write))
        summarizer = self.summarizer_mod.SessionMemorySummarizer(SimpleNamespace(), gateway, engine)
        summarizer.topic_summarizer = SimpleNamespace(
            process_history=lambda **_kwargs: asyncio.sleep(0, result=[])
        )
        summarizer.processor = SimpleNamespace(
            process_conversation=lambda *_args, **_kwargs: asyncio.sleep(
                0,
                result={
                    "summary": "Alice discussed a deployment plan",
                    "key_facts": ["Alice discussed a deployment plan"],
                    "topics": ["deployment"],
                    "sentiment": "neutral",
                    "importance": 0.7,
                },
            )
        )
        async def _extract(**kwargs):
            claim_requests.append(kwargs)
            return []

        summarizer.claim_extractor = SimpleNamespace(extract=_extract)
        messages = [
            {
                "message_id": 11,
                "sender_id": "user-1",
                "sender": "Alice",
                "content": "deployment plan",
                "timestamp": 100.0,
            }
        ]

        asyncio.run(
            summarizer.summarize_session(
                "ff:GroupMessage:123",
                "[00:00] Alice: deployment plan",
                messages=messages,
            )
        )
        asyncio.run(
            summarizer.summarize_session(
                "ff:GroupMessage:123",
                "[00:00] Alice: deployment plan",
                messages=messages,
            )
        )

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].dedup_key, requests[1].dedup_key)
        self.assertEqual(requests[0].sender_id, "")
        self.assertEqual(requests[0].kind, "memory")
        self.assertEqual(requests[0].metadata["evidence_message_ids"], [11])
        self.assertEqual(len(claim_requests), 2)
        self.assertEqual(claim_requests[0]["lane_scope_id"], "ff:GroupMessage:123")
        self.assertEqual(claim_requests[0]["lane_scope_kind"], "chat")
        self.assertEqual(claim_requests[0]["subject_id"], "")

    def test_compat_summarizer_module_still_reexports_chat_history_summarizer(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        compat_mod = importlib.import_module("astrmai.memory.services.summarizer")
        compat_mod = importlib.reload(compat_mod)
        gateway = SimpleNamespace(
            config=SimpleNamespace(memory=SimpleNamespace(cleanup_interval=60, summary_threshold=2)),
            context=SimpleNamespace(),
        )
        instance = compat_mod.ChatHistorySummarizer(SimpleNamespace(), gateway, _FakeEngine())
        self.assertIsNotNone(instance)


__all__ = ["MemoryContractMigratedTests"]
