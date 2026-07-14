import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeResponse:
    def __init__(self, text):
        self.completion_text = text
        self.usage = SimpleNamespace(input=3, input_cached=0, output=2)


class _FlakyGatewayContext:
    def __init__(self):
        self.calls = []

    async def llm_generate(self, chat_provider_id, **kwargs):
        self.calls.append((chat_provider_id, kwargs))
        if chat_provider_id == "model-timeout":
            raise asyncio.TimeoutError()
        return _FakeResponse("visible reply")


class _Conversation:
    def __init__(self, history=None):
        self.history = history or []


class _ConcurrentConversationManager:
    def __init__(self):
        self.curr = {}
        self.conversations = {}
        self.counter = 0
        self.new_calls = 0

    async def get_curr_conversation_id(self, unified_msg_origin):
        await asyncio.sleep(0)
        return self.curr.get(unified_msg_origin)

    async def new_conversation(self, unified_msg_origin, platform_id=None, content=None, title=None, persona_id=None):
        self.new_calls += 1
        await asyncio.sleep(0.01)
        self.counter += 1
        cid = f"conv-{self.counter}"
        self.curr[unified_msg_origin] = cid
        self.conversations[cid] = _Conversation(history=content or [])
        return cid

    async def get_conversation(self, unified_msg_origin, conversation_id, create_if_not_exists=False):
        if conversation_id not in self.conversations and create_if_not_exists:
            self.conversations[conversation_id] = _Conversation(history=[])
        return self.conversations.get(conversation_id)

    async def update_conversation(self, unified_msg_origin, conversation_id=None, history=None, title=None, persona_id=None, token_usage=None):
        conversation_id = conversation_id or self.curr.get(unified_msg_origin)
        self.conversations[conversation_id] = _Conversation(history=history or [])


class InfrastructureGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_elastic_call_retries_next_model_after_timeout(self):
        from astrmai.infrastructure.gateway.model_gateway import GlobalModelGateway

        context = _FlakyGatewayContext()
        gateway = GlobalModelGateway(
            context,
            SimpleNamespace(
                infra=SimpleNamespace(
                    max_concurrent_llm_calls=2,
                    llm_retries=0,
                    backoff_factor=1.0,
                    api_timeout=10,
                ),
                provider=SimpleNamespace(fallback_models=[]),
                global_settings=SimpleNamespace(debug_mode=False),
                system1=SimpleNamespace(nicknames=[]),
            ),
        )

        result = asyncio.run(
            gateway._elastic_call(
                pool_name="task",
                prompt="hello",
                system_prompt="system",
                models=["model-timeout", "model-ok"],
                use_fallback=False,
            )
        )

        self.assertEqual(result, "visible reply")
        self.assertEqual([model for model, _ in context.calls], ["model-timeout", "model-ok"])
        stats = gateway.router.get_stats()["task"]["models"]
        self.assertEqual(stats["model-timeout"]["failures"], 1)
        self.assertEqual(stats["model-ok"]["calls"], 1)

    def test_model_router_moves_fatal_cooldown_model_to_tail(self):
        from astrmai.infrastructure.gateway.model_router import ModelRouter

        router = ModelRouter()
        self.assertEqual(router.get_ranked_models("task", ["a", "b"]), ["a", "b"])
        router.report_failure("task", "a", is_fatal=True)

        ranked = router.get_ranked_models("task", ["a", "b"])

        self.assertEqual(ranked, ["b", "a"])
        self.assertGreater(router._pools["task"].models["a"].cooldown_until, 0.0)

    def test_lane_manager_ensure_lane_concurrent_creation_is_single_flight(self):
        from astrmai.infrastructure.runtime.lane_manager import LaneKey, LaneManager

        conversation_manager = _ConcurrentConversationManager()
        manager = LaneManager(conversation_manager)
        lane_key = LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        async def _run():
            results = await asyncio.gather(
                *[
                    manager.ensure_lane(lane_key, "default:GroupMessage:group-1")
                    for _ in range(8)
                ]
            )
            return results

        results = asyncio.run(_run())

        conversation_ids = {item[1] for item in results}
        self.assertEqual(len(conversation_ids), 1)
        self.assertEqual(conversation_manager.new_calls, 1)

    def test_lane_manager_concurrent_append_preserves_both_exchanges(self):
        from astrmai.infrastructure.runtime.lane_manager import LaneKey, LaneManager

        conversation_manager = _ConcurrentConversationManager()
        manager = LaneManager(conversation_manager)
        lane_key = LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        async def _run():
            await asyncio.gather(
                manager.append_exchange(
                    lane_key,
                    "default:GroupMessage:group-1",
                    "user-a",
                    "assistant-a",
                ),
                manager.append_exchange(
                    lane_key,
                    "default:GroupMessage:group-1",
                    "user-b",
                    "assistant-b",
                ),
            )
            return await manager.get_lane_history(
                lane_key,
                "default:GroupMessage:group-1",
            )

        history = asyncio.run(_run())
        contents = [item.get("content") for item in history]

        self.assertIn("user-a", contents)
        self.assertIn("assistant-a", contents)
        self.assertIn("user-b", contents)
        self.assertIn("assistant-b", contents)

    def test_event_bus_stop_allows_workers_to_restart_on_next_publish(self):
        from astrmai.infrastructure.runtime.event_bus import EventBus

        event_bus = EventBus()
        event_bus._init_bus()
        received = []

        async def _capture(payload):
            received.append(payload["value"])

        event_bus.subscribe("infra.test", _capture)

        async def _run():
            await event_bus.publish("infra.test", {"value": 1})
            await asyncio.sleep(0.05)
            await event_bus.stop()
            self.assertFalse(event_bus._workers_started)
            await event_bus.publish("infra.test", {"value": 2})
            await asyncio.sleep(0.05)
            await event_bus.stop()

        asyncio.run(_run())

        self.assertEqual(received, [1, 2])

    def test_chat_runtime_clear_runtime_state_removes_activity_and_wait_targets(self):
        from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator

        coordinator = ChatRuntimeCoordinator()

        async def _run():
            await coordinator.update_wait_targets("chat-1", ["user-1"], "Alice")
            await coordinator.mark_activity("chat-1", 123.0, sender_id="user-1", preview="hello")
            removed = await coordinator.clear_runtime_state("chat-1")
            wait_targets = await coordinator.get_wait_targets("chat-1")
            latest = await coordinator.get_latest_activity("chat-1")
            return removed, wait_targets, latest

        removed, wait_targets, latest = asyncio.run(_run())

        self.assertTrue(removed)
        self.assertEqual(wait_targets, [])
        self.assertEqual(latest[0], 0.0)

    def test_token_bucket_concurrent_consumes_never_exceed_capacity(self):
        from astrmai.infrastructure.security.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=1.0, capacity=3)

        async def _run():
            return await asyncio.gather(*[bucket.consume() for _ in range(10)])

        results = asyncio.run(_run())

        self.assertEqual(sum(1 for item in results if item), 3)
        self.assertEqual(sum(1 for item in results if not item), 7)

    def test_database_service_get_chat_state_falls_back_on_dirty_json_fields(self):
        import sqlite3

        from astrmai.infrastructure.persistence.database_service import DatabaseService
        from astrmai.infrastructure.persistence.persistence_schema import PersistenceSchemaMixin

        class _Persistence(PersistenceSchemaMixin):
            def __init__(self, db_path):
                self.db_path = db_path
                self._init_db_sync()

        db_path = Path(self.temp_dir.name) / "infra.db"
        persistence = _Persistence(db_path)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO chat_states
                (chat_id, energy, mood, group_config, last_msg_info)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("chat-dirty", 0.4, -0.2, "{bad-json", "{also-bad"),
            )
            conn.commit()

        service = DatabaseService(SimpleNamespace(db_path=str(db_path)))
        loaded = service.get_chat_state("chat-dirty")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.group_config, {})
        self.assertEqual(loaded.last_msg_info.sender_id, "")
        self.assertFalse(loaded.last_msg_info.has_image)


if __name__ == "__main__":
    unittest.main()
