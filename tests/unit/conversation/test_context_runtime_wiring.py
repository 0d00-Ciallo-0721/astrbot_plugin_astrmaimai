from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class ContextRuntimeWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _build_config(self, *, enable_dialogue_store=True, enable_context_compaction=True, enable_prefix_caching=True):
        return SimpleNamespace(
            provider=SimpleNamespace(
                task_models=["task-model"],
                agent_models=["agent-model"],
                fallback_models=["fallback-model"],
                vision_models=[],
            ),
            infra=SimpleNamespace(
                max_concurrent_llm_calls=1,
                llm_retries=1,
                backoff_factor=1.0,
                api_timeout=5.0,
            ),
            global_settings=SimpleNamespace(debug_mode=False, enable_private_chat=False),
            system1=SimpleNamespace(nicknames=["Mai"]),
            sys3=SimpleNamespace(enable_work_mode=False),
            vision=SimpleNamespace(enable_vision=False),
            life=SimpleNamespace(enable_proactive=False, dream_visible=False),
            reply=SimpleNamespace(meme_probability=0),
            conversation=SimpleNamespace(
                enable_dialogue_store=enable_dialogue_store,
                enable_context_compaction=enable_context_compaction,
                enable_prefix_caching=enable_prefix_caching,
                hot_zone_ttl_seconds=30.0,
                warm_zone_ttl_seconds=300.0,
                warm_zone_max_tokens=1200,
                compaction_trigger_segments=3,
                compaction_trigger_tokens=5,
                compaction_keep_recent_segments=1,
                compaction_summary_max_tokens=200,
                compaction_provider_id="",
            ),
        )

    def test_core_services_wires_dialogue_store_and_compaction_when_enabled(self):
        defaults_mod = importlib.import_module("astrmai.shared.constants.defaults")
        runtime_context_mod = importlib.import_module("astrmai.app.runtime_context")
        bootstrap_mod = importlib.import_module("astrmai.app.bootstrap")

        class DummyDBService:
            def __init__(self, persistence):
                self.persistence = persistence
                self.dialogue_store = None
                self.context_compaction = None
                self.memory_engine = None

        class DummyGateway:
            def __init__(self, context, config, settings=None):
                self.context = context
                self.config = config
                self.settings = settings
                self.lane_manager = None

            def set_lane_manager(self, lane_manager):
                self.lane_manager = lane_manager

        class DummyLaneManager:
            def __init__(self, conversation_manager, config=None, settings=None):
                self.conversation_manager = conversation_manager
                self.config = config
                self.settings = settings

        class DummyMemoryEngine:
            def __init__(self, context, gateway, embedding_models=None):
                self.context = context
                self.gateway = gateway
                self.embedding_models = embedding_models
                self.tool_service = SimpleNamespace(db_service=None)
                self.db_service = None

        class DummyStateEngine:
            def __init__(self, persistence, gateway, config=None, event_bus=None):
                self.persistence = persistence
                self.gateway = gateway
                self.config = config if config is not None else gateway.config
                self.event_bus = event_bus

        class DummyJudge:
            def __init__(self, gateway, state_engine):
                self.gateway = gateway
                self.state_engine = state_engine

        class DummyPreFilters:
            def __init__(self, config):
                self.config = config

        config = self._build_config(enable_dialogue_store=True, enable_context_compaction=True)
        runtime = SimpleNamespace(
            config=config,
            infrastructure_settings=defaults_mod.build_infrastructure_settings(config),
            feature_flags=defaults_mod.build_infrastructure_settings(config).features,
            status=SimpleNamespace(),
        )
        bootstrap = bootstrap_mod.PluginBootstrap(
            context=SimpleNamespace(conversation_manager=object()),
            config=config,
            raw_config={},
        )

        with patch.object(bootstrap_mod, "PersistenceManager", lambda: object()), \
             patch.object(bootstrap_mod, "DatabaseService", DummyDBService), \
             patch.object(bootstrap_mod, "GlobalModelGateway", DummyGateway), \
             patch.object(bootstrap_mod, "LaneManager", DummyLaneManager), \
             patch.object(bootstrap_mod, "MemoryEngine", DummyMemoryEngine), \
             patch.object(bootstrap_mod, "StateEngine", DummyStateEngine), \
             patch.object(bootstrap_mod, "Judge", DummyJudge), \
             patch.object(bootstrap_mod, "PreFilters", DummyPreFilters):
            core = bootstrap._build_core_services(runtime)

        self.assertIsNotNone(core.dialogue_store)
        self.assertIsNotNone(core.context_compaction)
        self.assertIs(core.state_engine.dialogue_store, core.dialogue_store)
        self.assertIs(core.state_engine.context_compaction, core.context_compaction)
        self.assertIs(core.gateway.dialogue_store, core.dialogue_store)
        self.assertIs(core.gateway.context_compaction, core.context_compaction)
        self.assertIs(core.db_service.dialogue_store, core.dialogue_store)
        self.assertIs(core.db_service.context_compaction, core.context_compaction)
        self.assertIsInstance(core, runtime_context_mod.CoreServices)
        self.assertEqual(core.context_compaction.compaction_trigger_segments, 3)
        self.assertEqual(core.context_compaction.compaction_keep_recent_segments, 1)

    def test_core_services_skips_dialogue_store_and_compaction_when_disabled(self):
        defaults_mod = importlib.import_module("astrmai.shared.constants.defaults")
        bootstrap_mod = importlib.import_module("astrmai.app.bootstrap")

        class DummyDBService:
            def __init__(self, persistence):
                self.persistence = persistence
                self.dialogue_store = None
                self.context_compaction = None
                self.memory_engine = None

        class DummyGateway:
            def __init__(self, context, config, settings=None):
                self.context = context
                self.config = config
                self.settings = settings

            def set_lane_manager(self, lane_manager):
                self.lane_manager = lane_manager

        class DummyLaneManager:
            def __init__(self, conversation_manager, config=None, settings=None):
                self.conversation_manager = conversation_manager
                self.config = config
                self.settings = settings

        class DummyMemoryEngine:
            def __init__(self, context, gateway, embedding_models=None):
                self.tool_service = SimpleNamespace(db_service=None)
                self.db_service = None

        class DummyStateEngine:
            def __init__(self, persistence, gateway, config=None, event_bus=None):
                self.persistence = persistence
                self.gateway = gateway
                self.config = config if config is not None else gateway.config

        class DummyJudge:
            def __init__(self, gateway, state_engine):
                self.gateway = gateway
                self.state_engine = state_engine

        class DummyPreFilters:
            def __init__(self, config):
                self.config = config

        config = self._build_config(enable_dialogue_store=False, enable_context_compaction=False)
        settings = defaults_mod.build_infrastructure_settings(config)
        runtime = SimpleNamespace(
            config=config,
            infrastructure_settings=settings,
            feature_flags=settings.features,
            status=SimpleNamespace(),
        )
        bootstrap = bootstrap_mod.PluginBootstrap(
            context=SimpleNamespace(conversation_manager=object()),
            config=config,
            raw_config={},
        )

        with patch.object(bootstrap_mod, "PersistenceManager", lambda: object()), \
             patch.object(bootstrap_mod, "DatabaseService", DummyDBService), \
             patch.object(bootstrap_mod, "GlobalModelGateway", DummyGateway), \
             patch.object(bootstrap_mod, "LaneManager", DummyLaneManager), \
             patch.object(bootstrap_mod, "MemoryEngine", DummyMemoryEngine), \
             patch.object(bootstrap_mod, "StateEngine", DummyStateEngine), \
             patch.object(bootstrap_mod, "Judge", DummyJudge), \
             patch.object(bootstrap_mod, "PreFilters", DummyPreFilters):
            core = bootstrap._build_core_services(runtime)

        self.assertIsNone(core.dialogue_store)
        self.assertIsNone(core.context_compaction)
        self.assertIsNone(core.db_service.dialogue_store)
        self.assertIsNone(core.db_service.context_compaction)

    def test_runtime_exports_context_compaction_and_prefix_cache_can_be_disabled(self):
        runtime_context_mod = importlib.import_module("astrmai.app.runtime_context")
        context_engine_mod = importlib.import_module("astrmai.conversation.planning.context_engine")

        runtime = runtime_context_mod.PluginRuntimeContext(
            host_context=SimpleNamespace(),
            raw_config={},
            config=SimpleNamespace(),
            runtime_coordinator=SimpleNamespace(),
            host_bridge=SimpleNamespace(),
        )
        marker = object()
        runtime.core = runtime_context_mod.CoreServices(context_compaction=marker)
        exported = runtime_context_mod.export_legacy_attrs(runtime)
        self.assertIs(runtime.context_compaction, marker)
        self.assertIs(exported["context_compaction"], marker)

        class FakeSummarizer:
            def __init__(self):
                self.gateway = SimpleNamespace(
                    config=SimpleNamespace(conversation=SimpleNamespace(enable_prefix_caching=False)),
                    context=SimpleNamespace(),
                )

            async def get_summary(self, **kwargs):
                return {
                    "summary": "persona summary",
                    "style": "natural",
                    "shards": {},
                    "raw": "persona raw",
                    "is_full_ready": True,
                }

        async def run():
            engine = context_engine_mod.ContextEngine(
                db=SimpleNamespace(dialogue_store=None, get_chat_state=lambda chat_id: None),
                persona_summarizer=FakeSummarizer(),
            )
            await engine.build_prompt(chat_id="chat-1", event_messages=[], prompt_envelope=None)
            self.assertEqual(engine.get_last_prefix_hash("chat-1"), "")
            status = engine.get_last_prefix_status("chat-1")
            self.assertEqual(status["prefix_changed_reason"], "disabled")
            self.assertFalse(status["prefix_stable"])
            self.assertEqual(status["frozen_prefix_length"], 0)
            self.assertEqual(status["semi_stable_length"], 0)
            self.assertEqual(status["frozen_prefix_blocks"], {})
            self.assertEqual(status["semi_stable_blocks"], {})

        asyncio.run(run())

    def test_bootstrap_build_attaches_chat_loop_kernel(self):
        bootstrap_mod = importlib.import_module("astrmai.app.bootstrap")

        config = self._build_config()
        bootstrap = bootstrap_mod.PluginBootstrap(
            context=SimpleNamespace(conversation_manager=object()),
            config=config,
            raw_config={},
        )

        with patch.object(bootstrap, "_build_core_services", return_value=SimpleNamespace()), \
             patch.object(bootstrap, "_build_work_mode", return_value=SimpleNamespace()), \
             patch.object(bootstrap, "_build_cognition_stack", return_value=SimpleNamespace()), \
             patch.object(
                 bootstrap,
                 "_build_interaction_stack",
                 return_value=SimpleNamespace(attention_gate=SimpleNamespace(process_event=lambda event: "BUFFERED")),
             ), \
             patch.object(bootstrap, "_build_chat_loop_kernel", return_value="kernel-marker"), \
             patch.object(bootstrap, "_build_lifecycle_stack", return_value=SimpleNamespace()):
            runtime = bootstrap.build()

        self.assertEqual(runtime.chat_loop_kernel, "kernel-marker")

    def test_runtime_diagnostics_include_chat_loop_status(self):
        runtime_context_mod = importlib.import_module("astrmai.app.runtime_context")

        runtime = runtime_context_mod.PluginRuntimeContext(
            host_context=SimpleNamespace(),
            raw_config={},
            config=SimpleNamespace(),
            runtime_coordinator=SimpleNamespace(),
            host_bridge=SimpleNamespace(),
        )
        runtime.chat_loop_kernel = SimpleNamespace(describe_status_sync=lambda: {"enabled": True, "tracked_chats": 2})

        diagnostics = runtime.build_diagnostics()

        self.assertTrue(diagnostics["components"]["chat_loop_kernel"])
        self.assertEqual(diagnostics["chat_loop"]["tracked_chats"], 2)

    def test_bootstrap_uses_new_compaction_defaults_when_config_values_are_absent(self):
        defaults_mod = importlib.import_module("astrmai.shared.constants.defaults")
        bootstrap_mod = importlib.import_module("astrmai.app.bootstrap")

        class DummyDBService:
            def __init__(self, persistence):
                self.persistence = persistence
                self.dialogue_store = None
                self.context_compaction = None
                self.memory_engine = None

        class DummyGateway:
            def __init__(self, context, config, settings=None):
                self.context = context
                self.config = config
                self.settings = settings

            def set_lane_manager(self, lane_manager):
                self.lane_manager = lane_manager

        class DummyLaneManager:
            def __init__(self, conversation_manager, config=None, settings=None):
                self.conversation_manager = conversation_manager

        class DummyMemoryEngine:
            def __init__(self, context, gateway, embedding_models=None):
                self.tool_service = SimpleNamespace(db_service=None)

        class DummyStateEngine:
            def __init__(self, persistence, gateway, config=None, event_bus=None):
                self.config = config if config is not None else gateway.config

        class DummyJudge:
            def __init__(self, gateway, state_engine):
                pass

        class DummyPreFilters:
            def __init__(self, config):
                self.config = config

        config = self._build_config()
        config.conversation = SimpleNamespace(
            enable_dialogue_store=True,
            enable_context_compaction=True,
            enable_prefix_caching=True,
            hot_zone_ttl_seconds=30.0,
            warm_zone_ttl_seconds=300.0,
            warm_zone_max_tokens=1200,
            compaction_provider_id="",
        )
        settings = defaults_mod.build_infrastructure_settings(config)
        runtime = SimpleNamespace(
            config=config,
            infrastructure_settings=settings,
            feature_flags=settings.features,
            status=SimpleNamespace(),
        )
        bootstrap = bootstrap_mod.PluginBootstrap(
            context=SimpleNamespace(conversation_manager=object()),
            config=config,
            raw_config={},
        )
        with patch.object(bootstrap_mod, "PersistenceManager", lambda: SimpleNamespace(cache_dir=self.temp_dir.name)), \
             patch.object(bootstrap_mod, "DatabaseService", DummyDBService), \
             patch.object(bootstrap_mod, "GlobalModelGateway", DummyGateway), \
             patch.object(bootstrap_mod, "LaneManager", DummyLaneManager), \
             patch.object(bootstrap_mod, "MemoryEngine", DummyMemoryEngine), \
             patch.object(bootstrap_mod, "StateEngine", DummyStateEngine), \
             patch.object(bootstrap_mod, "Judge", DummyJudge), \
             patch.object(bootstrap_mod, "PreFilters", DummyPreFilters):
            core = bootstrap._build_core_services(runtime)
        self.assertEqual(core.context_compaction.compaction_trigger_segments, 40)
        self.assertEqual(core.context_compaction.compaction_keep_recent_segments, 16)

    def test_recent_turn_traces_can_read_persisted_samples(self):
        admin_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        store_mod = importlib.import_module("astrmai.infrastructure.runtime.turn_trace_store")

        async def run():
            store = store_mod.TurnTraceSampleStore(self.temp_dir.name, max_per_chat=50)
            await store.append(
                {
                    "created_at": 123.0,
                    "chat_id": "chat-1",
                    "conversation_compression": {
                        "warm_summary_preview": "summary",
                        "warm_quotes_preview": "quotes",
                        "recent_used": False,
                        "recent_reason": "warm_sufficient",
                        "eligibility_reason": "eligible",
                        "focus_tail_overlap": False,
                        "recompact_armed": True,
                    },
                }
            )
            runtime = SimpleNamespace(system2_planner=SimpleNamespace(turn_trace_history=[], turn_trace_store=store))
            plugin_api = SimpleNamespace(
                get_runtime=lambda: runtime,
                get_planner=lambda: runtime.system2_planner,
            )
            service = admin_mod.AdminUiService(plugin_api)
            result = await service.recent_turn_traces(chat_id="chat-1", limit=10)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["conversation_compression"]["warm_summary_preview"], "summary")

        asyncio.run(run())

    def test_chat_trace_events_prefers_raw_trace_store(self):
        admin_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        raw_store_mod = importlib.import_module("astrmai.infrastructure.runtime.raw_trace_store")

        async def run():
            store = raw_store_mod.RawTraceEventStore(self.temp_dir.name, max_per_chat=50)
            await store.append_many(
                "chat-1",
                [
                    {
                        "created_at": 456.0,
                        "chat_id": "chat-1",
                        "trace_id": "trace-1",
                        "stage": "execution.executor.model_failure",
                        "failure_kind": "provider_failure_text",
                        "attempted_models": ["model-a", "model-b"],
                    }
                ],
            )
            runtime = SimpleNamespace(system2_planner=SimpleNamespace(turn_trace_history=[], turn_trace_store=None, raw_trace_store=store))
            plugin_api = SimpleNamespace(
                get_runtime=lambda: runtime,
                get_planner=lambda: runtime.system2_planner,
            )
            service = admin_mod.AdminUiService(plugin_api)
            result = await service.chat_trace_events("chat-1", limit=10)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["stage"], "execution.executor.model_failure")
            self.assertEqual(result["items"][0]["failure_evidence"]["failure_kind"], "provider_failure_text")

        asyncio.run(run())

    def test_trace_stores_write_json_atomically(self):
        raw_store_mod = importlib.import_module("astrmai.infrastructure.runtime.raw_trace_store")
        turn_store_mod = importlib.import_module("astrmai.infrastructure.runtime.turn_trace_store")

        async def run():
            raw_store = raw_store_mod.RawTraceEventStore(self.temp_dir.name, max_per_chat=50)
            turn_store = turn_store_mod.TurnTraceSampleStore(self.temp_dir.name, max_per_chat=50)
            await raw_store.append({"created_at": 1.0, "chat_id": "chat-1", "stage": "raw"})
            await turn_store.append({"created_at": 2.0, "chat_id": "chat-1", "stage": "turn"})
            raw_payload = json.loads(raw_store.path.read_text(encoding="utf-8"))
            turn_payload = json.loads(turn_store.path.read_text(encoding="utf-8"))
            self.assertEqual(raw_payload["by_chat"]["chat-1"][0]["stage"], "raw")
            self.assertEqual(turn_payload["by_chat"]["chat-1"][0]["stage"], "turn")

        asyncio.run(run())

    def test_planner_trace_runtime_reads_store_fields_without_threadsafe_bridge(self):
        planner_mod = importlib.import_module("astrmai.conversation.planning.planner")

        class DummyEvent:
            def __init__(self):
                self._extra = {}

            def get_extra(self, key, default=None):
                return self._extra.get(key, default)

            def set_extra(self, key, value):
                self._extra[key] = value

        async def run():
            store = SimpleNamespace(
                snapshot_counts=lambda chat_id: asyncio.sleep(0, result={"segments": 7, "tokens": 42, "has_summary": True}),
                get_cold_summary=lambda chat_id: asyncio.sleep(0, result="cold summary here"),
                get_cold_summary_structure=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(section_counts=lambda: {"topics": 1, "open_items": 2})),
            )
            planner = planner_mod.Planner.__new__(planner_mod.Planner)
            planner.dialogue_store = store
            planner.context_compaction = SimpleNamespace(
                get_trace_status=lambda chat_id, focus_context=None: asyncio.sleep(
                    0,
                    result={
                        "state": "DEFERRED_FOR_STABILITY",
                        "eligibility_reason": "focus_tail_overlap",
                        "recompact_armed": False,
                        "focus_tail_overlap": True,
                        "delta_old_segments": 3,
                        "delta_old_message_load": 6.0,
                        "delta_old_long_message_count": 1,
                        "message_count_since_last_compaction": 96,
                        "next_eval_at_count": 100,
                        "final_score": 71.0,
                        "count_score": 40.0,
                        "closure_score": 8.0,
                        "tail_activity_score": -5.0,
                        "topic_density_score": 6.0,
                        "stability_score": 2.0,
                        "benefit_score": 20.0,
                        "is_forced": False,
                        "is_safe_to_compact": False,
                        "closure_signals": ["answered_unconfirmed"],
                        "tail_activity_signals": ["reply_chain_active"],
                        "topic_density_signals": ["parallel_subthreads"],
                        "stability_signals": ["focus_tail_overlap"],
                        "benefit_signals": ["old_zone_ratio_high"],
                        "forced_pending_message_delta": 4,
                        "last_safe_window_seen_at_count": 90,
                        "post_compaction_recovery_rounds": 2,
                        "evaluation_count": 90,
                        "current_message_count": 96,
                        "queued_eval_node": 90,
                        "pending_eval_nodes_count": 2,
                        "pending_eval_nodes": [100, 110],
                        "force_execute_on_next_safe_hook": True,
                        "safe_hook_block_reason": "forced_waiting_for_safe_hook",
                        "last_hook_source": "assistant",
                        "last_safe_hook_checked_at": 96,
                    },
                )
            )
            planner.context_engine = SimpleNamespace(
                get_last_prefix_status=lambda chat_id: {
                    "prefix_hash": "abc123",
                    "semantic_system_hash": "semantic123",
                    "semantic_system_length": 240,
                    "prefix_stable": True,
                    "prefix_changed_reason": "",
                }
            )
            event = DummyEvent()
            prompt_envelope = SimpleNamespace(
                warm_zone_transcript="warm transcript",
                warm_zone_transcript_source="store",
                warm_zone_summary="warm summary",
                warm_zone_quotes="warm quotes",
                warm_topics_preview="topic:缓存命中 | event:继续推进",
                recent_transcript_reason="warm_sufficient",
                recent_transcript="recent fallback line",
                focus_message_text="keep exact mainline",
            )
            await planner._update_turn_trace_runtime(
                event,
                "chat-1",
                prompt_envelope=prompt_envelope,
                reply_text="reply body",
            )
            turn_context = planner_mod.ensure_turn_context(event)
            self.assertEqual(turn_context.attention.cold_summary_preview, "cold summary here")
            self.assertEqual(turn_context.continuity.dialogue_store_version, "segments:7")
            self.assertEqual(turn_context.continuity.compaction_status, "DEFERRED_FOR_STABILITY")
            self.assertEqual(turn_context.continuity.compaction_eligibility_reason, "focus_tail_overlap")
            self.assertTrue(turn_context.continuity.focus_tail_overlap)
            self.assertEqual(turn_context.attention.warm_summary_preview, "warm summary")
            self.assertEqual(turn_context.attention.warm_quotes_preview, "warm quotes")
            self.assertIn("topic:", turn_context.attention.warm_topics_preview)
            self.assertEqual(turn_context.attention.recent_transcript_preview, "recent fallback line")
            self.assertEqual(turn_context.attention.reply_prompt_focus_anchor, "keep exact mainline")
            self.assertEqual(turn_context.continuity.cold_summary_section_counts["topics"], 1)
            self.assertEqual(turn_context.continuity.message_count_since_last_compaction, 96)
            self.assertEqual(turn_context.continuity.next_eval_at_count, 100)
            self.assertEqual(turn_context.continuity.final_score, 71.0)
            self.assertFalse(turn_context.continuity.is_safe_to_compact)
            self.assertIn("answered_unconfirmed", turn_context.continuity.closure_signals)
            self.assertEqual(turn_context.continuity.forced_pending_message_delta, 4)
            self.assertEqual(turn_context.continuity.last_safe_window_seen_at_count, 90)
            self.assertEqual(turn_context.continuity.post_compaction_recovery_rounds, 2)
            self.assertEqual(turn_context.continuity.evaluation_count, 90)
            self.assertEqual(turn_context.continuity.current_message_count, 96)
            self.assertEqual(turn_context.continuity.queued_eval_node, 90)
            self.assertEqual(turn_context.continuity.pending_eval_nodes_count, 2)
            self.assertEqual(turn_context.continuity.pending_eval_nodes, [100, 110])
            self.assertTrue(turn_context.continuity.force_execute_on_next_safe_hook)
            self.assertEqual(turn_context.continuity.safe_hook_block_reason, "forced_waiting_for_safe_hook")
            self.assertEqual(turn_context.continuity.last_hook_source, "assistant")
            self.assertEqual(turn_context.continuity.last_safe_hook_checked_at, 96)
            self.assertEqual(turn_context.continuity.prefix_hash, "abc123")
            self.assertEqual(turn_context.continuity.semantic_system_hash, "semantic123")
            self.assertEqual(turn_context.continuity.semantic_system_length, 240)
            self.assertTrue(turn_context.continuity.prefix_stable)

        asyncio.run(run())

    def test_update_turn_trace_runtime_captures_request_trace_fields(self):
        planner_mod = importlib.import_module("astrmai.conversation.planning.planner")

        class DummyEvent:
            def __init__(self):
                self._extra = {
                    "astrmai_request_trace": {
                        "semantic_system_hash": "semantic8888",
                        "semantic_system_length": 260,
                        "gateway_system_hash": "gateway1111",
                        "gateway_prompt_hash": "gateway2222",
                        "provider_visible_system_hash": "syshash1111",
                        "provider_visible_prompt_hash": "prompthash2222",
                        "post_hook_system_hash": "posthook3333",
                        "request_session_id": "session-abc",
                        "request_cache_control": '{"type":"ephemeral"}',
                        "request_provider_family": "anthropic",
                        "request_model_id": "claude-3-5-sonnet",
                        "usage_input_tokens": 900,
                        "usage_input_cached": 700,
                        "usage_output_tokens": 80,
                    },
                    "astrmai_cache_affinity_enabled": True,
                }

            def get_extra(self, key, default=None):
                return self._extra.get(key, default)

            def set_extra(self, key, value):
                self._extra[key] = value

        async def run():
            planner = object.__new__(planner_mod.Planner)
            planner.context_engine = SimpleNamespace(
                get_last_prefix_status=lambda chat_id: {
                    "prefix_hash": "",
                    "semantic_system_hash": "semantic8888",
                    "semantic_system_length": 260,
                    "prefix_stable": False,
                    "prefix_changed_reason": "first_seen",
                    "frozen_prefix_length": 0,
                    "semi_stable_length": 0,
                    "frozen_prefix_blocks": {},
                    "semi_stable_blocks": {},
                    "system_rules_items": [],
                    "system_rules_candidate_items": [],
                }
            )
            planner.dialogue_store = None
            planner.context_compaction = None
            event = DummyEvent()
            await planner._update_turn_trace_runtime(event, "chat-1", prompt_envelope=None, reply_text="ok")
            turn_context = planner_mod.ensure_turn_context(event)
            self.assertEqual(turn_context.continuity.semantic_system_hash, "semantic8888")
            self.assertEqual(turn_context.continuity.semantic_system_length, 260)
            self.assertEqual(turn_context.continuity.gateway_system_hash, "gateway1111")
            self.assertEqual(turn_context.continuity.gateway_prompt_hash, "gateway2222")
            self.assertEqual(turn_context.continuity.provider_visible_system_hash, "syshash1111")
            self.assertEqual(turn_context.continuity.provider_visible_prompt_hash, "prompthash2222")
            self.assertEqual(turn_context.continuity.post_hook_system_hash, "posthook3333")
            self.assertEqual(turn_context.continuity.request_session_id, "session-abc")
            self.assertEqual(turn_context.continuity.request_cache_control, '{"type":"ephemeral"}')
            self.assertEqual(turn_context.continuity.request_provider_family, "anthropic")
            self.assertEqual(turn_context.continuity.request_model_id, "claude-3-5-sonnet")
            self.assertEqual(turn_context.continuity.usage_input_tokens, 900)
            self.assertEqual(turn_context.continuity.usage_input_cached, 700)
            self.assertEqual(turn_context.continuity.usage_output_tokens, 80)
            self.assertTrue(turn_context.continuity.cache_ready)
            self.assertTrue(turn_context.continuity.cache_hit)
            self.assertTrue(turn_context.continuity.cache_hit_evidence_supported)
            self.assertIn("explicit_cache_hint", turn_context.continuity.cache_ready_reasons)
            self.assertIn("session_reuse", turn_context.continuity.cache_ready_reasons)
            self.assertIn("cache_affinity_enabled", turn_context.continuity.cache_ready_reasons)

        asyncio.run(run())

    def test_update_turn_trace_runtime_marks_provider_visible_hash_stable_against_previous_turn(self):
        planner_mod = importlib.import_module("astrmai.conversation.planning.planner")

        class DummyEvent:
            def __init__(self):
                self._extra = {
                    "astrmai_request_trace": {
                        "gateway_system_hash": "gateway-new",
                        "gateway_prompt_hash": "gateway-prompt",
                        "provider_visible_system_hash": "provider-same",
                        "provider_visible_prompt_hash": "prompt-same",
                        "post_hook_system_hash": "provider-same",
                        "request_session_id": "",
                        "request_cache_control": "",
                        "request_provider_family": "anthropic",
                        "request_model_id": "claude-3-5-sonnet",
                        "usage_input_tokens": 500,
                        "usage_input_cached": 0,
                        "usage_output_tokens": 60,
                    }
                }

            def get_extra(self, key, default=None):
                return self._extra.get(key, default)

            def set_extra(self, key, value):
                self._extra[key] = value

        async def run():
            planner = object.__new__(planner_mod.Planner)
            planner.context_engine = SimpleNamespace(
                get_last_prefix_status=lambda chat_id: {
                    "prefix_hash": "",
                    "semantic_system_hash": "semantic9999",
                    "semantic_system_length": 260,
                    "prefix_stable": False,
                    "prefix_changed_reason": "first_seen",
                    "frozen_prefix_length": 0,
                    "semi_stable_length": 0,
                    "frozen_prefix_blocks": {},
                    "semi_stable_blocks": {},
                    "system_rules_items": [],
                    "system_rules_candidate_items": [],
                }
            )
            planner.dialogue_store = None
            planner.context_compaction = None
            planner._provider_visible_system_hash_history = {"chat-1": "provider-same"}
            event = DummyEvent()
            await planner._update_turn_trace_runtime(event, "chat-1", prompt_envelope=None, reply_text="ok")
            turn_context = planner_mod.ensure_turn_context(event)
            self.assertIn("provider_visible_hash_stable", turn_context.continuity.cache_ready_reasons)

        asyncio.run(run())

    def test_recent_fallback_relaxes_during_post_compaction_recovery(self):
        mixin_mod = importlib.import_module("astrmai.conversation.planning.planner_prompt_context")

        class DummyPlanner(mixin_mod.PlannerPromptContextMixin):
            pass

        planner = DummyPlanner()
        warm_bundle = SimpleNamespace(summary_text="topic: mainline", quote_text="Alice: follow up", has_latest_assistant=True)
        include_recent, reason = planner._should_include_recent_transcript(
            "这个还要继续吗",
            warm_bundle,
            "Mai: we just compacted this thread",
            post_compaction_recovery_rounds=2,
        )
        self.assertTrue(include_recent)
        self.assertEqual(reason, "post_compaction_recovery")

    def test_recent_fallback_keeps_tail_followup_context_for_direct_question(self):
        mixin_mod = importlib.import_module("astrmai.conversation.planning.planner_prompt_context")

        class DummyPlanner(mixin_mod.PlannerPromptContextMixin):
            pass

        planner = DummyPlanner()
        warm_bundle = SimpleNamespace(
            summary_text="topic: delay compaction while the reply chain stays live",
            quote_text="AstrMai: We should delay until the chain settles.\nAlice: Should we still delay compaction while this same chain is live?",
            has_latest_assistant=True,
        )
        include_recent, reason = planner._should_include_recent_transcript(
            "Should we still delay compaction while this same chain is live?",
            warm_bundle,
            "AstrMai: We should delay until the chain settles.\nAlice: Should we still delay compaction while this same chain is live?",
            post_compaction_recovery_rounds=0,
        )
        self.assertTrue(include_recent)
        self.assertEqual(reason, "tail_followup_recent")

    def test_recent_fallback_skips_tail_recent_when_recent_tail_is_not_same_chain(self):
        mixin_mod = importlib.import_module("astrmai.conversation.planning.planner_prompt_context")

        class DummyPlanner(mixin_mod.PlannerPromptContextMixin):
            pass

        planner = DummyPlanner()
        warm_bundle = SimpleNamespace(
            summary_text="topic: delay compaction while the reply chain stays live",
            quote_text="AstrMai: We should delay until the chain settles.\nAlice: Should we still delay compaction while this same chain is live?",
            has_latest_assistant=True,
        )
        include_recent, reason = planner._should_include_recent_transcript(
            "Should we still delay compaction while this same chain is live?",
            warm_bundle,
            "Bob: lunch first?\nCarol: milk tea also works.",
            post_compaction_recovery_rounds=0,
        )
        self.assertFalse(include_recent)
        self.assertEqual(reason, "warm_sufficient")

    def test_recent_fallback_keeps_recent_for_vision_mainline_question(self):
        mixin_mod = importlib.import_module("astrmai.conversation.planning.planner_prompt_context")

        class DummyPlanner(mixin_mod.PlannerPromptContextMixin):
            pass

        planner = DummyPlanner()
        warm_bundle = SimpleNamespace(
            summary_text="topic: keep the compaction mainline while image context is arriving",
            quote_text="Alice (图片): here is the screenshot\nAstrMai: I am following the compaction mainline.",
            has_latest_assistant=True,
        )
        include_recent, reason = planner._should_include_recent_transcript(
            "AstrMai, keep the compaction mainline while I send a screenshot.",
            warm_bundle,
            "Alice: here is the screenshot\nAstrMai: I am following the compaction mainline.",
            post_compaction_recovery_rounds=0,
        )
        self.assertTrue(include_recent)
        self.assertEqual(reason, "vision_mainline_recent")


if __name__ == "__main__":
    unittest.main()
