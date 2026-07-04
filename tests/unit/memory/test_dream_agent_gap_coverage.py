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


if __name__ == "__main__":
    unittest.main()
