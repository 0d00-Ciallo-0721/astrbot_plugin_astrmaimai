import asyncio
import importlib
import json
import sys
import tempfile
import time
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

    def test_memory_processor_fallback_prompt_replaces_payload_without_format_errors(self):
        sys.modules.pop("astrmai.memory.services.memory_processor", None)
        memory_mod = importlib.import_module("astrmai.memory.services.memory_processor")
        memory_mod = importlib.reload(memory_mod)

        processor = memory_mod.MemoryProcessor(SimpleNamespace())
        rendered_history = processor.prompt_template.replace("{history}", "history-x")
        rendered_facts = processor.node_prompt_template.replace("{facts}", "facts-y")

        self.assertIn("history-x", rendered_history)
        self.assertIn("facts-y", rendered_facts)

    def test_memory_processor_uses_chat_scoped_lane_for_non_global_session(self):
        sys.modules.pop("astrmai.memory.services.memory_processor", None)
        memory_mod = importlib.import_module("astrmai.memory.services.memory_processor")
        memory_mod = importlib.reload(memory_mod)

        class _Gateway:
            def __init__(self):
                self.calls = []

            async def call_data_process_task(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return {
                        "summary": "ok",
                        "topics": ["a"],
                        "key_facts": ["f"],
                        "reflection": "r",
                        "sentiment": "neutral",
                        "importance": 0.6,
                    }
                return {"nodes": [], "deleted_nodes": []}

        gateway = _Gateway()
        processor = memory_mod.MemoryProcessor(gateway)

        async def _run():
            await processor.process_conversation("hello", session_id="chat-42")

        asyncio.run(_run())

        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(gateway.calls[0]["lane_key"].scope_id, "chat-42")
        self.assertEqual(gateway.calls[0]["lane_key"].scope_kind, "chat")
        self.assertEqual(gateway.calls[0]["base_origin"], "chat-42")
        self.assertEqual(gateway.calls[1]["base_origin"], "chat-42")

    def test_memory_turn_pipeline_describes_eligibility_from_buffer(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=3, cleanup_interval=3600))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=3, cleanup_interval=3600)),
        )
        pipeline._session_history_buffer["chat-1"] = {
            "buffer": ["u1", "a1", "u2", "a2", "u3", "a3"],
            "last_update": 1.0,
            "cooldown_until": 0.0,
            "failures": 0,
            "last_run_at": 0.0,
        }

        result = asyncio.run(pipeline.describe_session_eligibility("chat-1"))

        self.assertTrue(result["eligible"])
        self.assertTrue(result["candidate_present"])
        self.assertEqual(result["reason"], "eligible")

    def test_memory_turn_pipeline_rejects_maintenance_after_shutdown_fence(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2)),
        )
        pipeline._session_history_buffer["shutdown-chat"] = {
            "buffer": ["u", "a", "u2", "a2"], "last_update": time.time(),
            "cooldown_until": 0.0, "failures": 0, "last_run_at": 0.0,
        }
        pipeline.begin_shutdown()
        result = asyncio.run(pipeline.run_maintenance_for_session("shutdown-chat", force=True))
        self.assertEqual(result["reason"], "shutdown_rejected")
        self.assertEqual(pipeline.describe_runtime_status()["started_after_shutdown"], 0)
        self.assertEqual(len(pipeline._session_history_buffer["shutdown-chat"]["buffer"]), 4)

    def test_memory_turn_pipeline_counts_worker_start_attempt_after_shutdown(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2)),
        )
        pipeline.begin_shutdown()
        turn = pipeline.build_turn(
            chat_id="late-chat",
            user_text="hello",
            assistant_text="world",
            source="test",
        )

        asyncio.run(pipeline.on_turn_committed({"turn": turn}))

        self.assertEqual(pipeline.describe_runtime_status()["started_after_shutdown"], 1)
        self.assertEqual(pipeline._worker_tasks, {})

    def test_memory_turn_pipeline_replaces_externally_cancelled_chat_worker(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2)),
        )

        async def _run():
            first_started = asyncio.Event()
            second_consumed = asyncio.Event()
            consumed = []

            async def _consume(turn):
                consumed.append(turn.user_text)
                if turn.user_text == "first":
                    first_started.set()
                    await asyncio.Event().wait()
                second_consumed.set()

            pipeline._maybe_run_llm_backfill = _consume
            pipeline._running = True
            first = pipeline.build_turn(
                chat_id="worker-recovery",
                user_text="first",
                assistant_text="one",
                source="test",
            )
            await pipeline.on_turn_committed({"turn": first})
            await asyncio.wait_for(first_started.wait(), timeout=0.2)
            cancelled_worker = pipeline._worker_tasks[first.chat_id]
            cancelled_worker.cancel()
            await asyncio.wait_for(cancelled_worker, timeout=0.2)
            await asyncio.sleep(0)

            second = pipeline.build_turn(
                chat_id="worker-recovery",
                user_text="second",
                assistant_text="two",
                source="test",
            )
            await pipeline.on_turn_committed({"turn": second})
            replacement = pipeline._worker_tasks[second.chat_id]
            await asyncio.wait_for(second_consumed.wait(), timeout=0.2)
            pipeline.begin_shutdown()
            replacement.cancel()
            await asyncio.gather(replacement, return_exceptions=True)
            return cancelled_worker, replacement, consumed

        cancelled_worker, replacement, consumed = asyncio.run(_run())

        self.assertIsNot(cancelled_worker, replacement)
        self.assertEqual(consumed, ["first", "second"])

    def test_memory_engine_replay_cancellation_does_not_skip_component_stops(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        config = SimpleNamespace(
            provider=SimpleNamespace(embedding_models=[]),
            memory=SimpleNamespace(recall_top_k=5),
        )
        engine = memory_engine_mod.MemoryEngine(
            SimpleNamespace(),
            SimpleNamespace(config=config),
            config=config,
        )
        calls = []

        class _Pipeline:
            def begin_shutdown(self):
                calls.append("pipeline.begin_shutdown")

            async def stop(self):
                calls.append("pipeline.stop")

        class _Projector:
            async def stop(self):
                calls.append("projector.stop")
                return {"remaining": 0}

        async def _run():
            engine.memory_pipeline = _Pipeline()
            engine.index_projector = _Projector()
            engine._projection_ready_replay_task = asyncio.create_task(asyncio.Event().wait())
            await asyncio.sleep(0)
            await engine.stop_background_producers()

        asyncio.run(_run())

        self.assertIn("pipeline.stop", calls)
        self.assertIn("projector.stop", calls)

    def test_memory_engine_replay_failure_is_consumed_and_observed(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        config = SimpleNamespace(
            provider=SimpleNamespace(embedding_models=[]),
            memory=SimpleNamespace(recall_top_k=5),
        )
        engine = memory_engine_mod.MemoryEngine(
            SimpleNamespace(),
            SimpleNamespace(config=config),
            config=config,
        )

        async def _run():
            async def _fail():
                raise RuntimeError("replay failed")

            task = asyncio.create_task(_fail())
            engine._projection_ready_replay_task = task
            engine._projection_replay_status = "running"
            task.add_done_callback(engine._handle_projection_replay_result)
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        asyncio.run(_run())

        self.assertIsNone(engine._projection_ready_replay_task)
        self.assertEqual(engine._projection_replay_status, "failed")
        self.assertIn("RuntimeError: replay failed", engine._projection_replay_error)
        self.assertGreater(engine._projection_replay_completed_at, 0.0)

    def test_memory_turn_pipeline_maintenance_delegates_to_session_summarizer(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)

        calls = []

        class _SessionSummarizer:
            async def summarize_session(self, session_id, chat_history_text, persona_id=None, messages=None):
                calls.append((session_id, chat_history_text))

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600))),
            engine=SimpleNamespace(),
            session_summarizer=_SessionSummarizer(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)),
        )
        pipeline._session_history_buffer["chat-2"] = {
            "buffer": ["u1", "a1", "u2", "a2"],
            "last_update": 1.0,
            "cooldown_until": 0.0,
            "failures": 0,
            "last_run_at": 0.0,
        }

        result = asyncio.run(pipeline.run_maintenance_for_session("chat-2"))

        self.assertTrue(result["performed"])
        self.assertEqual(result["reason"], "summarized")
        self.assertEqual(calls[0][0], "chat-2")

    def test_memory_turn_pipeline_record_turn_keeps_buffer_after_instant_hit(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)

        class _Gate:
            async def process_committed_turn(self, turn):
                return SimpleNamespace(hit=True, memory_id="mem-1", category="identity", skip_backfill=True)

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=_Gate(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)),
        )

        async def _run():
            turn = pipeline.build_turn(
                chat_id="chat-3",
                user_text="我叫小明",
                assistant_text="好的",
                source="test",
            )
            result = await pipeline.record_turn(turn)
            gate = await pipeline.process_instant_gate(turn)
            return result, gate, turn

        result, gate, turn = asyncio.run(_run())

        self.assertTrue(result["performed"])
        self.assertTrue(gate.hit)
        self.assertTrue(turn.instant_gate_hit)
        # OPT-05/ML-10: 缓冲改结构化条目（旧拼接串连 sender 都没有，摘要解析器
        # 只能全落 unknown）；渲染成 "[序号] 发送者: 内容" 交给解析器
        self.assertEqual(
            pipeline._session_history_buffer["chat-3"]["buffer"],
            [{"sender": "旁白", "text": "我叫小明"}, {"sender": "Bot", "text": "好的"}],
        )

    def test_memory_turn_pipeline_ignores_proactive_turns(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)),
        )

        turn = pipeline.build_turn(
            chat_id="chat-4",
            user_text="hello",
            assistant_text="hi",
            source="test",
            is_proactive=True,
        )
        result = asyncio.run(pipeline.record_turn(turn))

        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], "proactive_ignored")

    def test_memory_turn_pipeline_event_drop_still_keeps_buffered_turn(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)

        class _EventBus:
            TOPIC_MEMORY_TURN_COMMITTED = "memory.turn_committed"

            def subscribe(self, *_args, **_kwargs):
                return None

            def unsubscribe(self, *_args, **_kwargs):
                return None

            async def publish_memory_turn_committed(self, _payload):
                raise RuntimeError("queue dropped")

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=99, cleanup_interval=3600))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            event_bus=_EventBus(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=99, cleanup_interval=3600)),
        )

        async def _run():
            turn = pipeline.build_turn(
                chat_id="chat-drop",
                user_text="hello",
                assistant_text="hi",
                source="test",
            )
            record_result = await pipeline.record_turn(turn)
            with self.assertRaises(RuntimeError):
                await pipeline.publish_turn_committed(turn)
            eligibility = await pipeline.describe_session_eligibility("chat-drop")
            return record_result, eligibility

        record_result, eligibility = asyncio.run(_run())
        self.assertTrue(record_result["performed"])
        self.assertEqual(
            pipeline._session_history_buffer["chat-drop"]["buffer"],
            [{"sender": "旁白", "text": "hello"}, {"sender": "Bot", "text": "hi"}],
        )
        self.assertTrue(eligibility["candidate_present"])
        self.assertEqual(eligibility["reason"], "below_threshold")

    def test_memory_turn_pipeline_idle_timeout_becomes_eligible_even_below_threshold(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=10, cleanup_interval=3600))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=10, cleanup_interval=3600)),
        )
        pipeline._session_history_buffer["chat-idle"] = {
            "buffer": ["u1", "a1"],
            "last_update": 1.0,
            "cooldown_until": 0.0,
            "failures": 0,
            "last_run_at": 0.0,
        }

        original_time = pipeline_mod.time.time
        try:
            pipeline_mod.time.time = lambda: 1.0 + pipeline.TURN_FORCE_SUMMARIZE_AFTER_SECONDS + 5.0
            result = asyncio.run(pipeline.describe_session_eligibility("chat-idle"))
        finally:
            pipeline_mod.time.time = original_time

        self.assertTrue(result["eligible"])
        self.assertTrue(result["candidate_present"])
        self.assertEqual(result["reason"], "idle_timeout")

    def test_memory_turn_pipeline_queue_full_publish_does_not_drop_buffer(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)
        bus_mod = importlib.import_module("astrmai.infrastructure.runtime.event_bus")
        bus_mod = importlib.reload(bus_mod)

        event_bus = bus_mod.EventBus()
        event_bus.subscribers = {event_bus.TOPIC_MEMORY_TURN_COMMITTED: [lambda _payload: None]}
        event_bus._workers_started = False
        event_bus._background_tasks = set()
        full_queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait(("occupied", {}))
        event_bus._event_queue = full_queue

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=99, cleanup_interval=3600))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            event_bus=event_bus,
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=99, cleanup_interval=3600)),
        )

        async def _run():
            turn = pipeline.build_turn(
                chat_id="chat-queue-full",
                user_text="hello",
                assistant_text="hi",
                source="test",
            )
            record_result = await pipeline.record_turn(turn)
            await pipeline.publish_turn_committed(turn)
            tasks = list(getattr(event_bus, "_background_tasks", set()) or set())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            return record_result, turn

        record_result, turn = asyncio.run(_run())
        self.assertTrue(record_result["performed"])
        self.assertFalse(turn.instant_gate_hit)
        buffered = pipeline._session_history_buffer["chat-queue-full"]["buffer"]
        self.assertEqual(len(buffered), 2)
        self.assertIn("hello", buffered[0]["text"])
        self.assertIn("hi", buffered[1]["text"])

    def test_memory_turn_pipeline_sweep_loop_triggers_idle_timeout_maintenance(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=10, cleanup_interval=3600))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=10, cleanup_interval=3600)),
        )
        pipeline._running = True
        pipeline._session_history_buffer["chat-idle-sweep"] = {
            "buffer": ["u1", "a1"],
            "last_update": 1.0,
            "cooldown_until": 0.0,
            "failures": 0,
            "last_run_at": 0.0,
        }
        maintenance_calls = []

        async def _run_maintenance(chat_id):
            maintenance_calls.append(chat_id)
            pipeline._running = False
            return {"performed": True, "reason": "summarized"}

        sleep_calls = {"count": 0}
        original_sleep = pipeline_mod.asyncio.sleep
        original_time = pipeline_mod.time.time
        try:
            pipeline.run_maintenance_for_session = _run_maintenance

            async def _fake_sleep(_seconds):
                sleep_calls["count"] += 1
                if sleep_calls["count"] > 1:
                    raise asyncio.CancelledError()
                return None

            pipeline_mod.asyncio.sleep = _fake_sleep
            pipeline_mod.time.time = lambda: 1.0 + pipeline.TURN_FORCE_SUMMARIZE_AFTER_SECONDS + 5.0
            asyncio.run(pipeline._sweep_loop())
        finally:
            pipeline_mod.asyncio.sleep = original_sleep
            pipeline_mod.time.time = original_time

        self.assertEqual(maintenance_calls, ["chat-idle-sweep"])

    def test_memory_turn_pipeline_maintenance_rolls_back_buffer_and_sets_cooldown_on_failure(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)

        class _FailingSummarizer:
            async def summarize_session(self, session_id, chat_history_text, persona_id=None, messages=None):
                raise RuntimeError("summary failed")

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600))),
            engine=SimpleNamespace(),
            session_summarizer=_FailingSummarizer(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)),
        )
        pipeline._session_history_buffer["chat-fail"] = {
            "buffer": ["u1", "a1", "u2", "a2"],
            "last_update": 1.0,
            "cooldown_until": 0.0,
            "failures": 0,
            "last_run_at": 0.0,
        }

        result = asyncio.run(pipeline.run_maintenance_for_session("chat-fail"))
        session = pipeline._session_history_buffer["chat-fail"]

        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], "summary_failed")
        self.assertEqual(session["buffer"], ["u1", "a1", "u2", "a2"])
        self.assertEqual(session["failures"], 1)
        self.assertGreater(session["cooldown_until"], 0.0)

    def test_memory_turn_pipeline_stop_checkpoints_without_summarizing(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)
        summaries = []
        saved = {}

        class _SessionSummarizer:
            async def summarize_session(self, session_id, chat_history_text, persona_id=None, messages=None):
                summaries.append((session_id, chat_history_text))

        class _CheckpointStore:
            async def save_many(self, sessions):
                saved.update(sessions)

            async def delete(self, chat_id):
                saved.pop(chat_id, None)

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=10, cleanup_interval=3600))),
            engine=SimpleNamespace(),
            session_summarizer=_SessionSummarizer(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=10, cleanup_interval=3600)),
            checkpoint_store=_CheckpointStore(),
        )
        pipeline._session_history_buffer["short-chat"] = {
            "buffer": ["用户/旁白：短会话", "Bot：收到"],
            "last_update": time.time(),
            "cooldown_until": 0.0,
            "failures": 0,
            "last_run_at": 0.0,
        }

        asyncio.run(pipeline.stop())

        self.assertEqual(summaries, [])
        self.assertEqual(saved["short-chat"]["buffer"], ["用户/旁白：短会话", "Bot：收到"])
        self.assertEqual(pipeline._session_history_buffer["short-chat"]["buffer"], ["用户/旁白：短会话", "Bot：收到"])

    def test_memory_turn_pipeline_restores_checkpoint_before_start(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline_mod = importlib.reload(pipeline_mod)

        class _CheckpointStore:
            async def load_all(self):
                return {
                    "restored-chat": {
                        "buffer": [{"sender": "user-1", "text": "还没总结"}, {"sender": "Bot", "text": "收到"}],
                        "last_update": 123.0,
                        "cooldown_until": 0.0,
                        "failures": 0,
                        "last_run_at": 0.0,
                    }
                }

            async def save_many(self, sessions):
                return None

        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=10))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            checkpoint_store=_CheckpointStore(),
        )

        async def _run():
            await pipeline.start()
            self.assertEqual(
                pipeline._session_history_buffer["restored-chat"]["buffer"][0]["text"],
                "还没总结",
            )
            await pipeline.stop()

        asyncio.run(_run())

    def test_compat_summarizer_still_reexports_chat_history_summarizer(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        compat_mod = importlib.import_module("astrmai.memory.services.summarizer")
        compat_mod = importlib.reload(compat_mod)

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None), config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)))
        summarizer = compat_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(),
            config=gateway.config,
        )
        self.assertIsNotNone(summarizer)

    def test_compat_summarizer_describe_session_eligibility_forwards_to_pipeline(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        compat_mod = importlib.import_module("astrmai.memory.services.summarizer")
        compat_mod = importlib.reload(compat_mod)

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None), config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)))
        engine = SimpleNamespace(
            memory_pipeline=SimpleNamespace(
                describe_session_eligibility=lambda chat_id: asyncio.sleep(
                    0,
                    result={"eligible": True, "candidate_present": True, "reason": "eligible", "pending_messages": 2},
                )
            )
        )
        summarizer = compat_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=engine,
            config=gateway.config,
        )

        result = asyncio.run(summarizer.describe_session_eligibility("chat-5"))

        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")

    def test_compat_summarizer_describe_session_eligibility_falls_back_without_pipeline(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        compat_mod = importlib.import_module("astrmai.memory.services.summarizer")
        compat_mod = importlib.reload(compat_mod)

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None), config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=3, cleanup_interval=3600)))
        summarizer = compat_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(),
            config=gateway.config,
        )

        result = asyncio.run(summarizer.describe_session_eligibility("chat-fallback"))

        self.assertFalse(result["eligible"])
        self.assertFalse(result["candidate_present"])
        self.assertEqual(result["reason"], "memory_pipeline_unavailable")
        self.assertEqual(result["threshold_messages"], 6)

    def test_compat_summarizer_run_once_for_session_forwards_to_pipeline(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        compat_mod = importlib.import_module("astrmai.memory.services.summarizer")
        compat_mod = importlib.reload(compat_mod)

        calls = []

        class _Pipeline:
            async def run_maintenance_for_session(self, chat_id):
                calls.append(chat_id)
                return {"performed": True, "chat_id": chat_id}

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None), config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)))
        summarizer = compat_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(memory_pipeline=_Pipeline()),
            config=gateway.config,
        )

        result = asyncio.run(summarizer.run_once_for_session("chat-run"))

        self.assertEqual(calls, ["chat-run"])
        self.assertEqual(result, {"performed": True, "chat_id": "chat-run"})

    def test_compat_summarizer_ingest_committed_turn_forwards_to_pipeline(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        compat_mod = importlib.import_module("astrmai.memory.services.summarizer")
        compat_mod = importlib.reload(compat_mod)

        class _GateResult:
            hit = True

        class _Pipeline:
            def __init__(self):
                self.turns = []
                self._session_history_buffer = {"chat-ingest": {"buffer": ["u", "a", "u2", "a2"]}}

            def build_turn(self, **kwargs):
                turn = SimpleNamespace(**kwargs)
                self.turns.append(turn)
                return turn

            async def record_turn(self, turn):
                self.recorded = turn
                return {"performed": True, "pending_messages": 1}

            async def process_instant_gate(self, turn):
                self.gated = turn
                return _GateResult()

        pipeline = _Pipeline()
        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None), config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)))
        summarizer = compat_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(memory_pipeline=pipeline),
            config=gateway.config,
        )

        result = asyncio.run(
            summarizer.ingest_committed_turn(
                "chat-ingest",
                "hello",
                "hi",
                source="reply_post_send",
                is_proactive=True,
            )
        )

        self.assertEqual(pipeline.turns[0].chat_id, "chat-ingest")
        self.assertEqual(pipeline.turns[0].user_text, "hello")
        self.assertEqual(pipeline.turns[0].assistant_text, "hi")
        self.assertEqual(pipeline.turns[0].source, "reply_post_send")
        self.assertTrue(pipeline.turns[0].is_proactive)
        self.assertIs(pipeline.recorded, pipeline.gated)
        self.assertEqual(result["source"], "reply_post_send")
        self.assertTrue(result["instant_gate_hit"])
        self.assertEqual(result["pending_messages"], 4)

    def test_compat_summarizer_ingest_committed_turn_degrades_without_pipeline(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        compat_mod = importlib.import_module("astrmai.memory.services.summarizer")
        compat_mod = importlib.reload(compat_mod)

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None), config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)))
        summarizer = compat_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(memory_pipeline=None),
            config=gateway.config,
        )

        result = asyncio.run(
            summarizer.ingest_committed_turn("chat-none", "hello", "hi", source="reply_post_send")
        )

        self.assertEqual(
            result,
            {"performed": False, "reason": "memory_pipeline_unavailable", "source": "reply_post_send"},
        )

    def test_compat_summarizer_ingest_committed_turn_preserves_source_when_record_skips(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        compat_mod = importlib.import_module("astrmai.memory.services.summarizer")
        compat_mod = importlib.reload(compat_mod)

        class _Pipeline:
            def build_turn(self, **kwargs):
                return SimpleNamespace(**kwargs)

            async def record_turn(self, turn):
                return {"performed": False, "reason": "not_eligible"}

            async def process_instant_gate(self, turn):
                raise AssertionError("instant gate should not run when record_turn skips")

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None), config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)))
        summarizer = compat_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(memory_pipeline=_Pipeline()),
            config=gateway.config,
        )

        result = asyncio.run(
            summarizer.ingest_committed_turn("chat-skip", "hello", "hi", source="reply_post_send")
        )

        self.assertEqual(result, {"performed": False, "reason": "not_eligible", "source": "reply_post_send"})

    def test_compat_summarizer_start_and_stop_toggle_periodic_task(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        compat_mod = importlib.import_module("astrmai.memory.services.summarizer")
        compat_mod = importlib.reload(compat_mod)

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None), config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600)))
        summarizer = compat_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(),
            config=gateway.config,
        )

        async def _run():
            await summarizer.start()
            first_task = summarizer._periodic_task
            await summarizer.start()
            second_task = summarizer._periodic_task
            await summarizer.stop()
            await asyncio.sleep(0)
            return first_task, second_task

        first_task, second_task = asyncio.run(_run())

        self.assertIs(first_task, second_task)
        self.assertFalse(summarizer._running)
        self.assertTrue(first_task.cancelled() or first_task.done())

    def test_compat_summarizer_periodic_loop_runs_eligible_sessions_and_prunes(self):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        compat_mod = importlib.import_module("astrmai.memory.services.summarizer")
        compat_mod = importlib.reload(compat_mod)

        calls = []

        class _Pipeline:
            _session_history_buffer = {"chat-eligible": {"buffer": ["u", "a"]}}

            async def describe_session_eligibility(self, chat_id):
                calls.append(("describe", chat_id))
                return {"eligible": True}

            async def run_maintenance_for_session(self, chat_id):
                calls.append(("maintenance", chat_id))
                return {"performed": True}

        class _Engine:
            def __init__(self):
                self.memory_pipeline = _Pipeline()

            async def prune_low_importance(self, threshold):
                calls.append(("prune", threshold))

        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None), config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2, cleanup_interval=3600, prune_threshold=0.4)))
        summarizer = compat_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=_Engine(),
            config=gateway.config,
        )
        summarizer._running = True
        summarizer.check_interval = 0
        original_sleep = compat_mod.asyncio.sleep
        sleep_calls = {"count": 0}

        async def _sleep(_seconds):
            sleep_calls["count"] += 1
            if sleep_calls["count"] > 1:
                raise asyncio.CancelledError()

        compat_mod.asyncio.sleep = _sleep
        try:
            asyncio.run(summarizer._periodic_check_loop())
        finally:
            compat_mod.asyncio.sleep = original_sleep

        self.assertEqual(
            calls,
            [
                ("describe", "chat-eligible"),
                ("maintenance", "chat-eligible"),
                ("prune", 0.4),
            ],
        )


if __name__ == "__main__":
    unittest.main()
