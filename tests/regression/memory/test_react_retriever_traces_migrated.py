import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_astrbot_stubs


class _FakeGateway:
    def __init__(self):
        self.calls = 0

    async def call_data_process_task(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {"tool": "query_person", "args": {"name": "Alice"}}
        return {"tool": "found_answer", "args": {"answer": "她喜欢旅行，也愿意继续聊。"}}


class _FakePersistence:
    def load_all_user_profiles(self):
        return {
            "u1": {
                "name": "Alice",
                "nickname": "旅行搭子",
                "persona_analysis": "热衷旅行攻略，聊天时经常分享路线。",
                "tags": ["旅行", "攻略"],
                "social_score": 72,
                "identity_points": ["经历:经常独自旅行"],
                "preference_points": ["爱好:喜欢城市漫步"],
                "relationship_points": ["关系:和你聊旅行话题很放松"],
                "speech_style_points": ["习惯:会先讲结论再补细节"],
            }
        }


class _FakeDB:
    def __init__(self):
        self.persistence = _FakePersistence()
        self.saved_traces = []

    async def save_retrieval_trace_async(self, trace):
        self.saved_traces.append(trace)


class ReactRetrieverTraceMigratedTests(unittest.TestCase):
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

    def test_retrieval_returns_meta_and_saves_trace(self):
        retriever = self.mod.ReActRetriever(
            memory_engine=None,
            db_service=_FakeDB(),
            gateway=_FakeGateway(),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
        )

        async def _run():
            return await retriever.retrieve(
                query="你还记得 Alice 吗",
                chat_id="group-1",
                chat_context="前面在聊旅行",
                sender_name="Bob",
                retrieve_keys=["relations"],
            )

        result = asyncio.run(_run())

        self.assertIn("[记忆元信息]", result)
        self.assertIn("人物记忆", result)
        self.assertEqual(len(retriever.db_service.saved_traces), 1)
        trace = retriever.db_service.saved_traces[0]
        self.assertEqual(trace.chat_id, "group-1")
        self.assertIn("person", json.loads(trace.source_layers))


__all__ = ["ReactRetrieverTraceMigratedTests"]
