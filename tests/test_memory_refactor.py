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


if __name__ == "__main__":
    unittest.main()
