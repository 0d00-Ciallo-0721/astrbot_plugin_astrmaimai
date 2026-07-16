import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _PatternService:
    def __init__(self, patterns=None):
        self.patterns = list(patterns or [])
        self.list_calls = []
        self.rejected = []

    async def list_patterns(self, group_id, **kwargs):
        self.list_calls.append((group_id, kwargs))
        return list(self.patterns)

    async def update_review(self, pattern_id, **kwargs):
        self.rejected.append((pattern_id, kwargs))
        return True


class _EvolutionDB:
    def __init__(self, current_logs, groups=None):
        self.current_logs = list(current_logs)
        self.groups = list(groups or [])
        self.marked = []

    async def get_unprocessed_logs_async(self, group_id, limit=999):
        return list(self.current_logs)[:limit]

    async def list_unprocessed_log_groups_async(self, *, min_count=1, limit=20):
        return [
            item for item in self.groups
            if int(item.get("count", 0) or 0) >= int(min_count or 1)
        ][:limit]

    async def mark_logs_processed_async(self, log_ids):
        self.marked.append(list(log_ids))


class LearningGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for module_name in (
            "astrmai.learning.review.reflector",
            "astrmai.learning.profiling.profile_generator",
            "astrmai.learning.evolution_manager",
        ):
            sys.modules.pop(module_name, None)
        self.reflector_mod = importlib.import_module("astrmai.learning.review.reflector")
        self.profile_mod = importlib.import_module("astrmai.learning.profiling.profile_generator")
        self.evolution_mod = importlib.import_module("astrmai.learning.evolution_manager")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_auto_audit_does_not_consume_cooldown_before_service_is_ready(self):
        db = SimpleNamespace(memory_engine=None)
        reflector = self.reflector_mod.ExpressionReflector(db, SimpleNamespace())

        asyncio.run(reflector.auto_audit("chat-1"))

        service = _PatternService(
            [SimpleNamespace(id=str(index), weight=1.0, expression=chr(65 + index)) for index in range(10)]
        )
        db.memory_engine = SimpleNamespace(expression_pattern_service=service)
        asyncio.run(reflector.auto_audit("chat-1"))

        self.assertEqual(len(service.list_calls), 1)

    def test_auto_audit_rejects_low_weight_and_weaker_duplicate(self):
        patterns = [
            SimpleNamespace(id="low", weight=0.05, expression="z"),
            SimpleNamespace(id="strong", weight=1.0, expression="duplicate"),
            SimpleNamespace(id="weak", weight=0.5, expression="duplicate"),
        ]
        patterns.extend(
            SimpleNamespace(id=f"filler-{index}", weight=1.0, expression=chr(65 + index))
            for index in range(7)
        )
        service = _PatternService(patterns)
        db = SimpleNamespace(memory_engine=SimpleNamespace(expression_pattern_service=service))
        reflector = self.reflector_mod.ExpressionReflector(db, SimpleNamespace())

        asyncio.run(reflector.auto_audit("chat-2"))

        rejected_ids = [pattern_id for pattern_id, _ in service.rejected]
        self.assertCountEqual(rejected_ids, ["low", "weak"])
        self.assertNotIn("strong", rejected_ids)

    def test_auto_audit_retries_after_transient_list_failure(self):
        class _FlakyPatternService(_PatternService):
            def __init__(self):
                super().__init__(
                    [SimpleNamespace(id=str(index), weight=1.0, expression=chr(65 + index)) for index in range(10)]
                )
                self.attempts = 0

            async def list_patterns(self, group_id, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("database unavailable")
                return await super().list_patterns(group_id, **kwargs)

        service = _FlakyPatternService()
        db = SimpleNamespace(memory_engine=SimpleNamespace(expression_pattern_service=service))
        reflector = self.reflector_mod.ExpressionReflector(db, SimpleNamespace())

        asyncio.run(reflector.auto_audit("chat-retry"))
        asyncio.run(reflector.auto_audit("chat-retry"))

        self.assertEqual(service.attempts, 2)
        self.assertEqual(len(service.list_calls), 1)

    def test_profile_generator_accepts_structured_gateway_result(self):
        generator = self.profile_mod.ProfileGenerator()

        parsed = generator.parse_result(
            {
                "tags": ["curious", "patient"],
                "summary": "Prefers careful technical discussions.",
                "memory_points": [
                    {"category": "preference", "content": "likes typed APIs", "weight": 0.8},
                    {"category": "ignored", "content": "", "weight": 0.2},
                ],
            }
        )

        self.assertEqual(parsed["tags"], ["curious", "patient"])
        self.assertEqual(parsed["analysis"], "Prefers careful technical discussions.")
        self.assertEqual(parsed["memory_points"], ["preference:likes typed APIs:0.8"])

    def test_profile_generator_ignores_null_and_blank_tags(self):
        generator = self.profile_mod.ProfileGenerator()

        parsed = generator.parse_result(
            {
                "tags": [None, "", "  patient  "],
                "summary": "Stable summary.",
                "memory_points": [],
            }
        )

        self.assertEqual(parsed["tags"], ["patient"])

    def test_profile_generator_skips_prompt_without_new_messages(self):
        generator = self.profile_mod.ProfileGenerator()
        profile = SimpleNamespace(name="Alice", message_count_for_profiling=0)

        self.assertIsNone(generator.build_prompt(profile))

    def test_process_logs_and_mine_filters_partially_stale_batch(self):
        current = SimpleNamespace(id=1)
        stale = SimpleNamespace(id=2)
        db = _EvolutionDB([current])
        config = SimpleNamespace(
            evolution=SimpleNamespace(
                mining_window_sec=60,
                mining_window_min_messages=2,
                mining_cooldown_sec=60,
                mining_trigger=20,
            ),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        manager = self.evolution_mod.EvolutionManager(
            db,
            SimpleNamespace(config=config),
            config=config,
        )
        mined_ids = []

        async def _mine(group_id, logs):
            mined_ids.extend(log.id for log in logs)
            return []

        manager.expression_miner.mine = _mine

        asyncio.run(manager.process_logs_and_mine("chat-3", [current, stale]))

        self.assertEqual(mined_ids, [1])
        self.assertEqual(db.marked, [[1]])

    def test_process_logs_and_mine_skips_fully_stale_batch(self):
        db = _EvolutionDB([])
        config = SimpleNamespace(
            evolution=SimpleNamespace(
                mining_window_sec=60,
                mining_window_min_messages=2,
                mining_cooldown_sec=60,
                mining_trigger=20,
            ),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        manager = self.evolution_mod.EvolutionManager(
            db,
            SimpleNamespace(config=config),
            config=config,
        )
        mine_calls = []

        async def _mine(group_id, logs):
            mine_calls.append((group_id, logs))
            return []

        manager.expression_miner.mine = _mine

        asyncio.run(
            manager.process_logs_and_mine(
                "chat-4",
                [SimpleNamespace(id=9)],
            )
        )

        self.assertEqual(mine_calls, [])
        self.assertEqual(db.marked, [])

    def test_process_logs_and_mine_uses_current_database_log(self):
        requested = SimpleNamespace(id=1, content="stale content")
        current = SimpleNamespace(id=1, content="current content")
        db = _EvolutionDB([current])
        config = SimpleNamespace(
            evolution=SimpleNamespace(
                mining_window_sec=60,
                mining_window_min_messages=2,
                mining_cooldown_sec=60,
                mining_trigger=20,
            ),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        manager = self.evolution_mod.EvolutionManager(
            db,
            SimpleNamespace(config=config),
            config=config,
        )
        mined_contents = []

        async def _mine(group_id, logs):
            mined_contents.extend(log.content for log in logs)
            return []

        manager.expression_miner.mine = _mine

        asyncio.run(manager.process_logs_and_mine("chat-current", [requested]))

        self.assertEqual(mined_contents, ["current content"])
        self.assertEqual(db.marked, [[1]])

    def test_backlog_mining_processes_eligible_unprocessed_group(self):
        logs = [SimpleNamespace(id=index, content=f"message {index}") for index in range(1, 6)]
        db = _EvolutionDB(
            logs,
            groups=[
                {
                    "group_id": "chat-backlog",
                    "count": 5,
                    "oldest_timestamp": 1.0,
                    "latest_timestamp": 5.0,
                }
            ],
        )
        config = SimpleNamespace(
            evolution=SimpleNamespace(
                enable_expression_mining=True,
                enable_backlog_mining=True,
                backlog_min_unprocessed_logs=3,
                backlog_batch_size=5,
                backlog_group_limit=1,
                backlog_scan_interval_sec=60,
                backlog_failure_cooldown_sec=60,
                min_mining_context=3,
                mining_window_sec=60,
                mining_window_min_messages=10,
                mining_cooldown_sec=60,
                mining_trigger=20,
            ),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        manager = self.evolution_mod.EvolutionManager(
            db,
            SimpleNamespace(config=config),
            config=config,
        )
        mined = []

        async def _mine(group_id, batch):
            mined.append((group_id, [item.id for item in batch]))
            return []

        manager.expression_miner.mine = _mine

        report = asyncio.run(manager.run_backlog_mining_once())

        self.assertEqual(mined, [("chat-backlog", [1, 2, 3, 4, 5])])
        self.assertEqual(db.marked, [[1, 2, 3, 4, 5]])
        self.assertEqual(report["processed_groups"][0]["group_id"], "chat-backlog")

    def test_backlog_overview_reports_threshold_and_top_groups(self):
        db = _EvolutionDB(
            [],
            groups=[
                {
                    "group_id": "chat-small",
                    "count": 2,
                    "oldest_timestamp": 1.0,
                    "latest_timestamp": 2.0,
                },
                {
                    "group_id": "chat-ready",
                    "count": 6,
                    "oldest_timestamp": 1.0,
                    "latest_timestamp": 6.0,
                },
            ],
        )
        config = SimpleNamespace(
            evolution=SimpleNamespace(
                enable_expression_mining=True,
                enable_backlog_mining=True,
                backlog_min_unprocessed_logs=4,
                backlog_batch_size=10,
                backlog_group_limit=2,
                backlog_scan_interval_sec=60,
                backlog_failure_cooldown_sec=60,
                min_mining_context=3,
                mining_window_sec=60,
                mining_window_min_messages=10,
                mining_cooldown_sec=60,
                mining_trigger=20,
            ),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        manager = self.evolution_mod.EvolutionManager(
            db,
            SimpleNamespace(config=config),
            config=config,
        )

        overview = asyncio.run(manager.backlog_overview())

        self.assertEqual(overview["threshold"], 4)
        self.assertEqual([item["group_id"] for item in overview["top_unprocessed_groups"]], ["chat-small", "chat-ready"])
        self.assertEqual([item["group_id"] for item in overview["eligible_groups"]], ["chat-ready"])


if __name__ == "__main__":
    unittest.main()
