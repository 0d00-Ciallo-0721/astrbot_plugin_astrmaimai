import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config import AstrMaiConfig
from astrmai.learning.evolution_manager import EvolutionManager
from astrmai.learning.profiling.nickname_generator import NicknameGenerator
from astrmai.learning.profiling.profile_generator import ProfileGenerator
from astrmai.learning.review.expression_auto_check_task import ExpressionAutoCheckTask
from astrmai.learning.review.expression_governance_runner import ExpressionGovernanceRunner
from astrmai.learning.review.jargon_auto_check_task import JargonAutoCheckTask
from astrmai.learning.review.reflect_tracker import ReflectTracker
from astrmai.learning.review.reflector import ExpressionReflector
from astrmai.memory.contracts.memory_query import MemoryCandidate
from astrmai.memory.services.expression_pattern_service import ExpressionPatternService
from astrmai.proactive.review_dispatcher import ReviewDispatcher
from astrmai.proactive.dream_scheduler import DreamScheduler
from astrmai.infrastructure.runtime.outbound_send_guard import OUTBOUND_SEND_GATE


def _config(**evolution_overrides):
    evolution = {
        "mining_trigger": 2,
        "mining_window_sec": 60,
        "mining_window_min_messages": 2,
        "mining_cooldown_sec": 5,
        "review_batch_size": 10,
        "review_min_count": 2,
        "review_runner_interval_sec": 60,
        "review_runner_min_interval_sec": 15,
        "jargon_min_count": 2,
    }
    evolution.update(evolution_overrides)
    return AstrMaiConfig(evolution=evolution)


class _Gateway:
    def __init__(self, result=None, config=None):
        self.result = result
        self.config = config or _config()
        self.calls = 0

    async def call_data_process_task(self, *args, **kwargs):
        self.calls += 1
        return self.result


class _Event:
    unified_msg_origin = "group-1"
    message_str = "hello"

    def get_extra(self, key, default=None):
        return default

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"


class _ReviewEvent:
    unified_msg_origin = "group-1"

    def __init__(self, text="通过"):
        self.message_str = text

    def get_sender_id(self):
        return "admin-1"


class _ReviewDB:
    def __init__(self, pattern, outcomes):
        self.pattern = pattern
        self.outcomes = list(outcomes)
        self.calls = 0

    async def update_pattern_review_async(self, pattern_id, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return self.pattern if outcome else None


class _JargonStore:
    def __init__(self, candidate):
        self.candidate = candidate
        self.updates = []

    async def list_candidates(self, **kwargs):
        statuses = kwargs.get("statuses") or []
        if statuses and self.candidate.status not in statuses:
            return []
        return [self.candidate]

    async def update_memory(self, memory_id, **kwargs):
        self.updates.append(dict(kwargs))
        for key in ("summary", "status", "visibility", "metadata"):
            if key in kwargs:
                setattr(self.candidate, key, kwargs[key])
        return 1


class _FlakyProjector:
    def __init__(self):
        self.results = [False, True]
        self.projected = []
        self.cleaned = []

    async def project(self, memory_id):
        self.projected.append(memory_id)
        return self.results.pop(0)

    async def cleanup_deleted(self, memory_ids):
        self.cleaned.extend(memory_ids)
        return len(memory_ids)


class Round9LearningReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_user_and_bot_logs_schedule_one_mining_task_at_threshold(self):
        config = _config()
        logged = []

        async def _add_message_log_async(**kwargs):
            logged.append(kwargs)

        db = SimpleNamespace(add_message_log_async=_add_message_log_async, memory_engine=None)
        manager = EvolutionManager(db, _Gateway(config=config), config=config)
        manager._try_trigger_mining = AsyncMock()

        await manager.record_user_message(_Event())
        await manager.process_bot_reply("group-1", "bot-1", "reply")
        await asyncio.gather(*list(manager._background_tasks))

        self.assertEqual(len(logged), 2)
        manager._try_trigger_mining.assert_awaited_once_with("group-1")

    async def test_reflector_acks_successful_items_and_retries_only_failed_delta(self):
        gateway = _Gateway(
            [
                {"index": 1, "score": 9},
                {"index": 2, "score": 9},
                {"index": 3, "score": 5},
            ]
        )
        reflector = ExpressionReflector(SimpleNamespace(memory_engine=None), gateway)
        for index in range(3):
            await reflector.record_usage(pattern_id=f"p{index}", pattern_expression=f"e{index}")

        attempts = {"p0": 0, "p1": 0}

        async def _adjust(group_id, situation, expression, delta, pattern_id="", operation_id=""):
            attempts[pattern_id] += 1
            return not (pattern_id == "p1" and attempts[pattern_id] == 1)

        reflector._adjust_canonical_pattern_weight = _adjust
        await reflector.reflect_batch("group-1")

        self.assertEqual([item["pattern_id"] for item in reflector._pending_reflections], ["p1"])
        self.assertEqual(attempts, {"p0": 1, "p1": 1})

        await reflector.reflect_batch("group-1")

        self.assertEqual(reflector._pending_reflections, [])
        self.assertEqual(attempts, {"p0": 1, "p1": 2})
        self.assertEqual(gateway.calls, 1)

    async def test_expression_weight_operation_is_idempotent_after_commit_ack_failure(self):
        candidate = MemoryCandidate(
            id="p1",
            kind="expression_pattern",
            source="learning_expression_pattern",
            summary="hello",
            content="hello",
            session_id="group-1",
            metadata={"weight": 1.0},
        )

        class _CommittedThenRaisedStore:
            def __init__(self):
                self.calls = 0

            async def get_canonical(self, pattern_id, include_inactive=False):
                return candidate

            async def update_memory(self, pattern_id, **kwargs):
                self.calls += 1
                candidate.metadata = dict(kwargs["metadata"])
                if self.calls == 1:
                    raise RuntimeError("ack lost after commit")
                return 1

        store = _CommittedThenRaisedStore()
        service = ExpressionPatternService(store, write_service=None)

        with self.assertRaisesRegex(RuntimeError, "ack lost"):
            await service.adjust_weight_once("p1", 0.15, operation_id="reflection-1")
        result = await service.adjust_weight_once("p1", 0.15, operation_id="reflection-1")

        self.assertEqual(store.calls, 1)
        self.assertAlmostEqual(result.weight, 1.15)

    async def test_review_dispatch_claims_once_and_requeues_failed_send(self):
        OUTBOUND_SEND_GATE.open()

        pattern = SimpleNamespace(id="p1", group_id="group-1", situation="chat", expression="hello")
        tracker = ReflectTracker(SimpleNamespace(), _Gateway())
        tracker.queue_review_request(pattern)

        class _Context:
            def __init__(self):
                self.calls = 0
                self.fail_first = False

            async def send_message(self, umo, chain):
                self.calls += 1
                await asyncio.sleep(0)
                if self.fail_first and self.calls == 1:
                    raise RuntimeError("send failed")

        concurrent_context = _Context()
        concurrent_dispatcher = ReviewDispatcher(concurrent_context, tracker)
        await asyncio.gather(
            concurrent_dispatcher.dispatch_pending(),
            concurrent_dispatcher.dispatch_pending(),
        )
        self.assertEqual(concurrent_context.calls, 1)

        retry_tracker = ReflectTracker(SimpleNamespace(), _Gateway())
        retry_tracker.queue_review_request(pattern)
        retry_context = _Context()
        retry_context.fail_first = True
        retry_dispatcher = ReviewDispatcher(retry_context, retry_tracker)
        await retry_dispatcher.dispatch_pending()
        self.assertEqual(len(await retry_tracker.get_unsent_requests()), 1)
        await retry_dispatcher.dispatch_pending()
        self.assertEqual(retry_context.calls, 2)
        self.assertEqual(await retry_tracker.get_unsent_requests(), [])

    async def test_reflector_serializes_overlapping_consumers(self):
        gateway = _Gateway([{"index": 1, "score": 5}, {"index": 2, "score": 5}, {"index": 3, "score": 5}])
        reflector = ExpressionReflector(SimpleNamespace(memory_engine=None), gateway)
        for index in range(3):
            await reflector.record_usage(pattern_expression=f"e{index}")

        await asyncio.gather(
            reflector.reflect_batch("group-1"),
            reflector.reflect_batch("group-1"),
        )

        self.assertEqual(gateway.calls, 1)
        self.assertEqual(reflector._pending_reflections, [])

    async def test_pending_human_pattern_is_not_auto_reviewed(self):
        pattern = SimpleNamespace(
            id="p1",
            group_id="group-1",
            situation="chat",
            expression="hello",
            style="",
            content_list="[]",
            count=3,
            review_status="pending_human",
        )

        class _Service:
            async def list_reviewable_patterns(self, **kwargs):
                return [pattern]

        gateway = _Gateway({"decision": "approved"})
        db = SimpleNamespace(memory_engine=SimpleNamespace(expression_pattern_service=_Service()))
        task = ExpressionAutoCheckTask(db, gateway, config=_config())

        self.assertEqual(await task.run_once("group-1"), 0)
        self.assertEqual(gateway.calls, 0)

    async def test_human_feedback_is_acked_only_after_successful_persistence(self):
        pattern = SimpleNamespace(id="p1", group_id="group-1", situation="chat", expression="hello")
        db = _ReviewDB(pattern, [RuntimeError("db locked"), True])
        config = SimpleNamespace(global_settings=SimpleNamespace(admin_ids=["admin-1"]))
        tracker = ReflectTracker(db, _Gateway(), config=config)
        tracker.queue_review_request(pattern)

        first = await tracker.try_consume_feedback(_ReviewEvent("通过"))
        self.assertIn("暂未处理", first)
        self.assertEqual(len(tracker._pending), 1)

        second = await tracker.try_consume_feedback(_ReviewEvent("通过"))
        self.assertIn("已处理", second)
        self.assertEqual(tracker._pending, {})

    async def test_sent_human_review_is_not_requeued_by_duplicate_auto_result(self):
        OUTBOUND_SEND_GATE.open()

        sent = []

        class _Context:
            async def send_message(self, umo, chain):
                sent.append(umo)

        pattern = SimpleNamespace(id="p1", group_id="group-1", situation="chat", expression="hello")
        tracker = ReflectTracker(SimpleNamespace(), _Gateway())
        tracker.queue_review_request(pattern)
        dispatcher = ReviewDispatcher(_Context(), tracker)

        await dispatcher.dispatch_pending()
        tracker.queue_review_request(pattern, reason="duplicate governance round")
        await dispatcher.dispatch_pending()

        self.assertEqual(sent, ["default:GroupMessage:group-1"])

    async def test_jargon_projection_failure_rolls_back_and_retries_without_llm(self):
        candidate = SimpleNamespace(
            id="j1",
            kind="jargon",
            session_id="group-1",
            source="learning_jargon",
            content="大鸟",
            summary="团本首领",
            tags=["jargon"],
            importance=0.6,
            confidence=0.8,
            status="review_pending",
            visibility="maintenance_only",
            metadata={"meaning": "团本首领", "count": 3, "review_status": "review_pending"},
        )
        store = _JargonStore(candidate)
        projector = _FlakyProjector()
        db = SimpleNamespace(memory_engine=SimpleNamespace(v2_store=store, index_projector=projector))
        gateway = _Gateway({"decision": "approved", "reason": "stable"})
        task = JargonAutoCheckTask(db, gateway, config=_config())

        await task.run_once("group-1")
        self.assertEqual(candidate.status, "review_pending")
        self.assertEqual(candidate.metadata["projection_status"], "pending")

        task._last_run_at.clear()
        await task.run_once("group-1")

        self.assertEqual(candidate.status, "active")
        self.assertEqual(candidate.metadata["projection_status"], "projected")
        self.assertEqual(gateway.calls, 1)

    async def test_active_jargon_with_pending_projection_is_recovered_without_llm(self):
        candidate = SimpleNamespace(
            id="j-active",
            kind="jargon",
            session_id="group-1",
            source="learning_jargon",
            content="大鸟",
            summary="团本首领",
            tags=["jargon"],
            importance=0.6,
            confidence=0.8,
            status="active",
            visibility="auto_and_tool",
            metadata={
                "meaning": "团本首领",
                "count": 3,
                "review_status": "approved",
                "projection_status": "pending",
            },
        )
        store = _JargonStore(candidate)
        projector = _FlakyProjector()
        projector.results = [True]
        gateway = _Gateway({"decision": "approved"})
        db = SimpleNamespace(memory_engine=SimpleNamespace(v2_store=store, index_projector=projector))
        task = JargonAutoCheckTask(db, gateway, config=_config())

        self.assertEqual(await task.list_governance_groups(), ["group-1"])
        processed = await task.run_once("group-1")

        self.assertEqual(processed, 1)
        self.assertEqual(candidate.metadata["projection_status"], "projected")
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(await task.list_governance_groups(), [])

    async def test_governance_hot_refresh_updates_interval_and_children(self):
        children = []

        class _Child:
            def refresh_config(self, config):
                children.append(config)

        runner = ExpressionGovernanceRunner(
            state_engine=SimpleNamespace(),
            reflector=_Child(),
            auto_check_task=_Child(),
            jargon_auto_check_task=_Child(),
            interval_seconds=60,
        )
        config = _config(review_runner_interval_sec=120, review_batch_size=4)

        runner.refresh_config(config)

        self.assertEqual(runner.interval_seconds, 120)
        self.assertEqual(children, [config, config, config])

    async def test_learning_and_dream_hot_refresh_updates_derived_values(self):
        old_config = _config(mining_window_sec=60, mining_window_min_messages=2)
        manager = EvolutionManager(
            SimpleNamespace(memory_engine=None),
            _Gateway(config=old_config),
            config=old_config,
        )
        manager.recorder.record("group-1")
        new_config = _config(mining_window_sec=180, mining_window_min_messages=7, mining_cooldown_sec=30)
        new_config.life.dream_interval_min = 45
        new_config.life.dream_visible = True

        manager.refresh_config(new_config)
        scheduler = DreamScheduler(
            context=SimpleNamespace(),
            memory_engine=None,
            config=old_config,
            semaphore=asyncio.Semaphore(1),
        )
        scheduler.refresh_config(new_config)

        self.assertEqual(manager.recorder.window_seconds, 180)
        self.assertEqual(manager.recorder.min_messages, 7)
        self.assertEqual(manager.recorder.cooldown_seconds, 30)
        self.assertIn("group-1", manager.recorder._windows)
        self.assertIs(manager.expression_miner.config, new_config)
        self.assertEqual(scheduler._dream_interval, 45 * 60)
        self.assertTrue(scheduler.dream_visible)

    def test_hot_config_rollback_restores_governance_runner(self):
        from astrmai.app.plugin_facade import PluginFacade

        old_config = _config(review_runner_interval_sec=60)
        new_config = _config(review_runner_interval_sec=120)
        runner = ExpressionGovernanceRunner(state_engine=SimpleNamespace(), interval_seconds=60, config=old_config)

        class _Failure:
            def refresh_config(self, config):
                if config is new_config:
                    raise RuntimeError("late refresh failure")

        runtime = SimpleNamespace(
            raw_config={"evolution": {"review_runner_interval_sec": 60}},
            config=old_config,
            sys3_router=None,
            cron_guard=None,
            expression_governance_runner=runner,
            proactive_task=_Failure(),
            rebuild_infrastructure_settings=lambda: None,
            sync_host_compat_attrs=lambda: None,
        )
        facade = PluginFacade.__new__(PluginFacade)
        facade.runtime = runtime
        facade._hot_config_lock = threading.RLock()

        applied = facade.apply_hot_config(
            {"evolution": {"review_runner_interval_sec": 120}},
            new_config,
        )

        self.assertFalse(applied)
        self.assertIs(runtime.config, old_config)
        self.assertIs(runner.config, old_config)
        self.assertEqual(runner.interval_seconds, 60)

    def test_empty_profile_template_payloads_are_readable(self):
        profile = SimpleNamespace(name="", persona_analysis="", tags=[], memory_points=[])

        profile_payload = ProfileGenerator().build_template_payload(profile)
        nickname_payload = NicknameGenerator().build_template_payload(profile)

        self.assertEqual(profile_payload["old_analysis"], "暂无旧画像")
        self.assertEqual(profile_payload["old_tags_text"], "暂无标签")
        self.assertEqual(profile_payload["old_memory_text"], "暂无记忆点")
        self.assertEqual(nickname_payload["analysis"], "暂无画像")
        self.assertEqual(nickname_payload["tags_text"], "暂无")


if __name__ == "__main__":
    unittest.main()
