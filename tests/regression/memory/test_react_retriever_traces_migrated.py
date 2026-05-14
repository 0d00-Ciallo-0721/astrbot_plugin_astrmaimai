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

    def test_query_memory_prefers_v2_retrieval_service(self):
        class _RetrievalService:
            def __init__(self):
                self.calls = []

            async def retrieve_deep(self, query):
                self.calls.append(query)
                return [SimpleNamespace(id="mem-1", summary="Alice v2 memory", content="Alice v2 memory", status="active")]

            def render_recall(self, query, candidates):
                return f"v2:{candidates[0].summary}"

        class _MemoryEngine:
            def __init__(self):
                self.retrieval_service = _RetrievalService()
                self.recall_calls = []

            async def recall(self, *args, **kwargs):
                self.recall_calls.append((args, kwargs))
                return "legacy"

        async def _run():
            engine = _MemoryEngine()
            retriever = self.mod.ReActRetriever(memory_engine=engine)
            result = await retriever._tool_query_memory(chat_id="chat-1", query="Alice")
            return engine, result

        engine, result = asyncio.run(_run())
        self.assertEqual(result, "v2:Alice v2 memory")
        self.assertEqual(engine.recall_calls, [])
        self.assertEqual(engine.retrieval_service.calls[0].session_id, "chat-1")
        self.assertEqual(engine.retrieval_service.calls[0].policy, "deep")

    def test_query_jargon_prefers_v2_retrieval_service(self):
        class _RetrievalService:
            def __init__(self):
                self.calls = []

            async def retrieve(self, query):
                self.calls.append(query)
                return [
                    SimpleNamespace(
                        id="jargon-1",
                        content="开黑",
                        summary="一起组队玩游戏",
                        metadata={"meaning": "一起组队玩游戏"},
                        status="active",
                    )
                ]

        class _MemoryEngine:
            def __init__(self):
                self.retrieval_service = _RetrievalService()

        class _LegacyDB:
            def __init__(self):
                self.get_jargon_calls = []

            def get_jargon(self, *args, **kwargs):
                self.get_jargon_calls.append((args, kwargs))
                return "legacy"

        async def _run():
            db = _LegacyDB()
            engine = _MemoryEngine()
            retriever = self.mod.ReActRetriever(memory_engine=engine, db_service=db)
            result = await retriever._tool_query_jargon(chat_id="group-1", word="开黑")
            return engine, db, result

        engine, db, result = asyncio.run(_run())
        self.assertIn("'开黑': 一起组队玩游戏", result)
        self.assertEqual(db.get_jargon_calls, [])
        self.assertEqual(engine.retrieval_service.calls[0].session_id, "group-1")
        self.assertEqual(engine.retrieval_service.calls[0].layers, ["jargon"])
        self.assertEqual(engine.retrieval_service.calls[0].intent, "jargon")


__all__ = ["ReactRetrieverTraceMigratedTests"]
