import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_astrbot_stubs


class _FakeGateway:
    def __init__(self):
        self.config = SimpleNamespace()


class _FakeCandidate:
    def __init__(self, content, meaning, count, *, status="active", memory_id="mem-jargon-1"):
        self.id = memory_id
        self.content = content
        self.summary = meaning
        self.status = status
        self.metadata = {"meaning": meaning, "count": count}


class _FakeRetrievalService:
    def __init__(self):
        self.calls = []

    async def retrieve(self, query):
        self.calls.append(query)
        return [_FakeCandidate("团建黑话", "周五聚餐", 4)]


class _FakeMemoryEngine:
    def __init__(self):
        self.retrieval_service = _FakeRetrievalService()


class DreamMaintenanceMigratedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.memory.dream.dream_agent", None)
        sys.modules.pop("astrmai.memory.dream.dream_generator", None)
        self.agent_mod = importlib.import_module("astrmai.memory.dream.dream_agent")
        self.generator_mod = importlib.import_module("astrmai.memory.dream.dream_generator")
        self.agent_mod = importlib.reload(self.agent_mod)
        self.generator_mod = importlib.reload(self.generator_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_dream_agent_supports_read_only_jargon_tools(self):
        memory_engine = _FakeMemoryEngine()
        agent = self.agent_mod.DreamAgent(_FakeGateway(), SimpleNamespace(), memory_engine=memory_engine)

        async def _run():
            jargon_text = await agent._tool_search_jargon({"query": "团建", "limit": 3}, "group-1")
            suggestion = await agent._tool_suggest_jargon_review({"words": ["团建黑话"], "reason": "含义漂移"})
            return jargon_text, suggestion, memory_engine.retrieval_service.calls

        jargon_text, suggestion, calls = asyncio.run(_run())
        self.assertIn("团建黑话", jargon_text)
        self.assertIn("建议复核黑话", suggestion)
        self.assertEqual(calls[0].layers, ["jargon"])
        self.assertEqual(calls[0].intent, "jargon")
        self.assertTrue(calls[0].allow_stale)

    def test_build_maintenance_result_keeps_jargon_suggestions(self):
        result = self.generator_mod.DreamGenerator.build_maintenance_result(
            "[行动] search_memory({'query': '旅行'}) -> ok\n"
            "[行动] suggest_jargon_review({'words': ['团建黑话'], 'reason': '含义漂移'}) -> 建议复核黑话: 团建黑话 | 原因: 含义漂移",
            session_id="group-1",
        )
        self.assertIn("jargon_review", result["tags"])
        self.assertTrue(result["jargon_suggestions"])


__all__ = ["DreamMaintenanceMigratedTests"]
