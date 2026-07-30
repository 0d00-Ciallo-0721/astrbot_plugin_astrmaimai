import asyncio
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class Wave2MediumRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_expression_pattern_write_skip_returns_empty_id(self):
        from astrmai.memory.services.expression_pattern_service import ExpressionPatternService

        class _Store:
            async def get_by_dedup_key(self, *args, **kwargs):
                return None

        class _Writer:
            async def write(self, request):
                return ""

        service = ExpressionPatternService(_Store(), _Writer())
        result = asyncio.run(
            service.write_pattern(
                "group-1",
                {"expression": "Traceback", "situation": "debugging"},
            )
        )

        self.assertEqual(result, "")

    def test_reflector_retains_batch_after_transient_gateway_failure(self):
        sys.modules.pop("astrmai.learning.review.reflector", None)
        from astrmai.learning.review.reflector import ExpressionReflector

        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                raise asyncio.TimeoutError()

        reflector = ExpressionReflector(SimpleNamespace(memory_engine=None), _Gateway())
        reflector._pending_reflections = [
            {
                "pattern_id": str(index),
                "chat_id": "chat-1",
                "situation": "situation",
                "expression": f"expression-{index}",
                "reply": "reply",
                "reaction": "",
                "time": float(index),
            }
            for index in range(3)
        ]

        asyncio.run(reflector.reflect_batch("chat-1"))

        self.assertEqual(
            [item["pattern_id"] for item in reflector._pending_reflections],
            ["0", "1", "2"],
        )

    def test_reflector_retains_batch_when_weight_update_fails_after_llm_success(self):
        sys.modules.pop("astrmai.learning.review.reflector", None)
        from astrmai.learning.review.reflector import ExpressionReflector

        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                return [{"index": 1, "score": 10}]

        class _PatternService:
            async def adjust_weight(self, pattern_id, delta):
                raise TimeoutError("store locked")

        reflector = ExpressionReflector(
            SimpleNamespace(memory_engine=SimpleNamespace(expression_pattern_service=_PatternService())),
            _Gateway(),
        )
        reflector._pending_reflections = [
            {
                "pattern_id": f"pattern-{index}",
                "chat_id": "chat-1",
                "situation": "situation",
                "expression": f"expression-{index}",
                "reply": "reply",
                "reaction": "",
                "time": float(index),
            }
            for index in range(3)
        ]

        asyncio.run(reflector.reflect_batch("chat-1"))

        self.assertEqual(
            [item["pattern_id"] for item in reflector._pending_reflections],
            ["pattern-0", "pattern-1", "pattern-2"],
        )

    def test_detected_fact_memory_ids_are_marked_as_promoted(self):
        from astrmai.memory.dream.promotion_engine import MemoryPromotionEngine

        class _Store:
            def __init__(self):
                self.updated = []

            async def list_candidates(self, **kwargs):
                return []

            async def get_canonical(self, memory_id, include_inactive=False):
                return SimpleNamespace(metadata={"source": "detected"})

            async def update_memory(self, memory_id, **kwargs):
                self.updated.append((memory_id, kwargs))

        class _Writer:
            async def write(self, request):
                return "promoted-memory"

        store = _Store()
        engine = SimpleNamespace(v2_store=store, write_service=_Writer())
        promotion = MemoryPromotionEngine(engine)
        maintenance_result = {
            "detected_facts": [
                {
                    "subject_id": "user-1",
                    "entity": "profile",
                    "attribute": "favorite_color",
                    "value": "green",
                    "confidence_score": 0.95,
                    "evidence": {
                        "turn_id": f"turn-{index}",
                        "text": "favorite color is green",
                        "memory_id": f"source-{index}",
                    },
                }
                for index in range(3)
            ]
        }

        report = asyncio.run(promotion.run_audit("chat-1", maintenance_result, now=1000.0))

        self.assertEqual(report["promoted"][0]["memory_id"], "promoted-memory")
        self.assertEqual([item[0] for item in store.updated], ["source-0", "source-1", "source-2"])
        self.assertTrue(
            all(item[1]["metadata"]["promoted_to"] == "promoted-memory" for item in store.updated)
        )

    def test_nickname_parser_rejects_non_json_model_output(self):
        from astrmai.learning.profiling.nickname_generator import NicknameGenerator

        nickname, reason = NicknameGenerator().parse_result(
            "I could call this user Captain because they enjoy planning."
        )

        self.assertEqual((nickname, reason), ("", ""))

    def test_evolution_sync_log_fallback_runs_in_thread(self):
        from astrmai.learning.evolution_manager import EvolutionManager

        calls = []

        class _DB:
            def add_message_log(self, **kwargs):
                calls.append(("db", kwargs))

        manager = object.__new__(EvolutionManager)
        manager.db = _DB()
        to_thread_calls = []

        async def _to_thread(func, *args, **kwargs):
            to_thread_calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        with patch("astrmai.learning.evolution_manager.asyncio.to_thread", new=_to_thread):
            asyncio.run(
                manager._append_message_log(
                    group_id="group-1",
                    sender_id="user-1",
                    sender_name="Alice",
                    content="hello",
                )
            )

        self.assertEqual(len(to_thread_calls), 1)
        self.assertEqual(calls[0][1]["content"], "hello")

    def test_relationship_insult_matching_respects_token_boundaries(self):
        from astrmai.state.relationship.relationship_engine import RelationshipEngine, RelationshipEvent

        engine = RelationshipEngine()

        self.assertEqual(engine.classify_interaction_type("滚开"), RelationshipEvent.INSULT)
        self.assertEqual(engine.classify_interaction_type("你真是个 sb"), RelationshipEvent.INSULT)
        self.assertEqual(
            engine.classify_interaction_type("吃我的肉棒好不好"),
            RelationshipEvent.BOUNDARY_VIOLATION,
        )
        self.assertEqual(engine.classify_interaction_type("我喜欢摇滚音乐"), RelationshipEvent.NORMAL_CHAT)
        self.assertEqual(engine.classify_interaction_type("passbook migration"), RelationshipEvent.NORMAL_CHAT)

    def test_lane_lock_cleanup_skips_locked_oldest_and_evicts_other_idle_locks(self):
        from astrmai.infrastructure.runtime.lane_manager import LaneManager

        manager = LaneManager(SimpleNamespace())
        locked = asyncio.Lock()

        async def _run():
            await locked.acquire()
            manager._lane_locks["locked-oldest"] = locked
            for index in range(100):
                manager._lane_locks[f"idle-{index}"] = asyncio.Lock()
            await manager._get_lane_lock("new-lane")

        try:
            asyncio.run(_run())
        finally:
            if locked.locked():
                locked.release()

        self.assertIn("locked-oldest", manager._lane_locks)
        self.assertIn("new-lane", manager._lane_locks)
        self.assertLessEqual(len(manager._lane_locks), 100)

    def test_message_entry_reads_fallback_from_runtime_config_contract(self):
        from astrmai.presentation.events.message_entry import _runtime_fallback_text

        facade = SimpleNamespace(
            get_runtime_config=lambda: SimpleNamespace(
                reply=SimpleNamespace(fallback_text="configured fallback")
            )
        )

        self.assertEqual(_runtime_fallback_text(facade), "configured fallback")

    def test_hot_config_failure_rolls_runtime_and_components_back(self):
        from astrmai.app.plugin_facade import PluginFacade

        old_config = SimpleNamespace(name="old")
        new_config = SimpleNamespace(name="new")

        class _Component:
            def __init__(self, fail_on_new=False):
                self.config = old_config
                self.fail_on_new = fail_on_new
                self.calls = []

            def refresh_config(self, config):
                self.calls.append(config)
                self.config = config
                if self.fail_on_new and config is new_config:
                    raise RuntimeError("refresh failed")

        first = _Component()
        failing = _Component(fail_on_new=True)

        class _Runtime:
            raw_config = {"name": "old"}
            config = old_config
            gateway = first
            lane_manager = failing
            background_tasks = set()
            lifecycle = SimpleNamespace()

            def rebuild_infrastructure_settings(self):
                self.infrastructure_config = self.config

            def sync_host_compat_attrs(self):
                self.synced_config = self.config

        runtime = _Runtime()
        facade = object.__new__(PluginFacade)
        facade.runtime = runtime

        result = facade.apply_hot_config({"name": "new"}, new_config)

        self.assertFalse(result)
        self.assertIs(runtime.config, old_config)
        self.assertEqual(runtime.raw_config, {"name": "old"})
        self.assertIs(runtime.infrastructure_config, old_config)
        self.assertIs(runtime.synced_config, old_config)
        self.assertIs(first.config, old_config)
        self.assertIs(failing.config, old_config)


if __name__ == "__main__":
    unittest.main()
