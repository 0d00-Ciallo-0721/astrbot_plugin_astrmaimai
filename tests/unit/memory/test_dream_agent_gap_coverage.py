import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, tb):
        return False


class DreamAgentGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_parse_action_accepts_dict_json_and_embedded_json(self):
        from astrmai.memory.dream.dream_agent import DreamAgent

        direct = {"tool": "finish_dream", "params": {"summary": "done"}}
        self.assertIs(DreamAgent._parse_action(direct), direct)
        self.assertEqual(
            DreamAgent._parse_action('{"tool":"search_memory","params":{"query":"猫"}}')["tool"],
            "search_memory",
        )
        self.assertEqual(
            DreamAgent._parse_action('LLM says: {"tool":"delete_memory","params":{"event_id":"e1"}} ok')["params"]["event_id"],
            "e1",
        )
        self.assertIsNone(DreamAgent._parse_action("not-json"))

    def test_get_seed_events_serializes_sampled_events_and_degrades_on_db_error(self):
        async def _run():
            from astrmai.memory.dream.dream_agent import DreamAgent

            events = [
                SimpleNamespace(event_id=f"e{i}", narrative=f"narrative {i}", emotion="neutral", importance=0.5)
                for i in range(3)
            ]

            class _DB:
                def __init__(self, *, fail=False):
                    self.fail = fail

                def get_session(self):
                    if self.fail:
                        raise RuntimeError("db locked")
                    return _SessionContext(SimpleNamespace())

            agent = DreamAgent(SimpleNamespace(config=SimpleNamespace()), _DB())
            agent.SEED_SAMPLE_SIZE = 10
            agent._load_session_events = lambda _session, session_id: events

            result = await agent._get_seed_events("chat-1")

            self.assertCountEqual([item["event_id"] for item in result], ["e0", "e1", "e2"])
            by_id = {item["event_id"]: item for item in result}
            self.assertEqual(by_id["e0"]["narrative"], "narrative 0")
            self.assertEqual(by_id["e0"]["emotion"], "neutral")

            failing = DreamAgent(SimpleNamespace(config=SimpleNamespace()), _DB(fail=True))
            self.assertEqual(await failing._get_seed_events("chat-1"), [])

        asyncio.run(_run())

    def test_run_dream_cycle_executes_tools_until_finish(self):
        async def _run():
            from astrmai.memory.dream.dream_agent import DreamAgent

            class _Gateway:
                def __init__(self):
                    self.calls = []
                    self.responses = [
                        {
                            "thought": "先查相关记忆",
                            "tool": "search_memory",
                            "params": {"query": "火锅", "limit": 2},
                        },
                        {
                            "thought": "整理结束",
                            "tool": "finish_dream",
                            "params": {"summary": "已完成整理"},
                        },
                    ]
                    self.config = SimpleNamespace()

                async def call_data_process_task(self, **kwargs):
                    self.calls.append(kwargs)
                    return self.responses.pop(0)

            gateway = _Gateway()
            agent = DreamAgent(gateway, SimpleNamespace())
            agent._get_seed_events = lambda session_id: asyncio.sleep(
                0,
                result=[
                    {"event_id": "e1", "narrative": "火锅聊天", "emotion": "happy", "importance": 0.8},
                    {"event_id": "e2", "narrative": "重复火锅聊天", "emotion": "happy", "importance": 0.6},
                ],
            )
            executed = []

            async def _execute(tool_name, params, session_id):
                executed.append((tool_name, params, session_id))
                return "searched" if tool_name == "search_memory" else "done"

            agent._execute_tool = _execute

            result = await agent.run_dream_cycle("chat-1")

            self.assertIn("[思考] 先查相关记忆", result)
            self.assertIn("[行动] search_memory", result)
            self.assertIn("[结束] 已完成整理", result)
            self.assertEqual([item[0] for item in executed], ["search_memory", "finish_dream"])
            self.assertEqual(executed[0][2], "chat-1")
            self.assertEqual(len(gateway.calls), 2)
            self.assertEqual(gateway.calls[0]["lane_key"].scope_id, "chat-1")

        asyncio.run(_run())

    def test_tool_get_detail_reads_legacy_event_from_database_session(self):
        from astrmai.memory.dream.dream_agent import DreamAgent

        class _Result:
            @staticmethod
            def first():
                return SimpleNamespace(
                    event_id="event-1",
                    narrative="memory narrative",
                    emotion="happy",
                    importance=0.8,
                )

        class _Session:
            @staticmethod
            def exec(_statement):
                return _Result()

        class _DB:
            @staticmethod
            def get_session():
                return _SessionContext(_Session())

        agent = DreamAgent(SimpleNamespace(config=SimpleNamespace()), _DB())

        result = asyncio.run(agent._tool_get_detail({"event_id": "event-1"}))

        self.assertIn("memory narrative", result)
        self.assertIn("happy", result)
        self.assertIn("0.8", result)

    def test_tool_search_memory_uses_retrieval_service_and_renders_result(self):
        from astrmai.memory.dream.dream_agent import DreamAgent

        class _Retrieval:
            def __init__(self):
                self.query = None
                self.candidates = [SimpleNamespace(summary="first match")]

            async def retrieve(self, query):
                self.query = query
                return self.candidates

            def render_recall(self, query, candidates):
                self.rendered = (query, candidates)
                return "rendered memory result"

        retrieval = _Retrieval()
        engine = SimpleNamespace(retrieval_service=retrieval)
        agent = DreamAgent(
            SimpleNamespace(config=SimpleNamespace()),
            SimpleNamespace(),
            memory_engine=engine,
        )

        result = asyncio.run(
            agent._tool_search_memory({"query": "deployment", "limit": 3}, "chat-1")
        )

        self.assertEqual(result, "rendered memory result")
        self.assertEqual(retrieval.query.query, "deployment")
        self.assertEqual(retrieval.query.session_id, "chat-1")
        self.assertEqual(retrieval.query.top_k, 3)
        self.assertEqual(retrieval.rendered[1], retrieval.candidates)

    def test_tool_merge_writes_memory_and_marks_canonical_sources_merged(self):
        from astrmai.memory.dream.dream_agent import DreamAgent

        class _Store:
            async def find_ids_by_source_ref(self, source_ref):
                return {
                    "MemoryEvent:legacy-1": ["mem_1"],
                    "MemoryEvent:legacy-2": ["mem_2"],
                }.get(source_ref, [])

        class _Maintenance:
            def __init__(self):
                self.calls = []

            async def mark_merged(self, memory_ids, superseded_by):
                self.calls.append((memory_ids, superseded_by))

        class _Engine:
            def __init__(self):
                self.v2_store = _Store()
                self.maintenance_service = _Maintenance()
                self.write_calls = []

            async def add_memory(self, **kwargs):
                self.write_calls.append(kwargs)
                return "mem_new"

        engine = _Engine()
        agent = DreamAgent(
            SimpleNamespace(config=SimpleNamespace()),
            SimpleNamespace(),
            memory_engine=engine,
        )

        result = asyncio.run(
            agent._tool_merge(
                {
                    "event_ids": ["legacy-1", "legacy-2"],
                    "new_narrative": "merged narrative",
                },
                "chat-1",
            )
        )

        self.assertIn("merged narrative", result)
        self.assertEqual(
            engine.write_calls,
            [{"content": "merged narrative", "session_id": "chat-1", "importance": 0.7}],
        )
        self.assertEqual(
            engine.maintenance_service.calls,
            [(["mem_1", "mem_2"], "mem_new")],
        )

    def test_resolve_canonical_ids_handles_mixed_canonical_and_legacy_ids(self):
        from astrmai.memory.dream.dream_agent import DreamAgent

        class _Store:
            async def find_ids_by_source_ref(self, source_ref):
                return ["mem_legacy"] if source_ref == "MemoryEvent:legacy-1" else []

        agent = DreamAgent(
            SimpleNamespace(config=SimpleNamespace()),
            SimpleNamespace(),
            memory_engine=SimpleNamespace(v2_store=_Store()),
        )

        canonical, unresolved = asyncio.run(
            agent._resolve_canonical_ids(
                ["mem_direct", "legacy-1", "legacy-missing", "mem_direct", ""]
            )
        )

        self.assertEqual(canonical, ["mem_direct", "mem_legacy"])
        self.assertEqual(unresolved, ["legacy-missing"])

    def test_run_dream_cycle_returns_none_when_seed_events_are_empty(self):
        from astrmai.memory.dream.dream_agent import DreamAgent

        class _Gateway:
            config = SimpleNamespace()

            async def call_data_process_task(self, **_kwargs):
                raise AssertionError("gateway should not run without seed events")

        agent = DreamAgent(_Gateway(), SimpleNamespace())
        agent._get_seed_events = lambda _session_id: asyncio.sleep(0, result=[])

        self.assertIsNone(asyncio.run(agent.run_dream_cycle("chat-empty")))


if __name__ == "__main__":
    unittest.main()
