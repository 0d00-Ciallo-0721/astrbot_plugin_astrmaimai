from __future__ import annotations

import unittest
from types import SimpleNamespace


class _Component:
    def __init__(self, config):
        self.config = config
        self.seen = [config]

    def refresh_config(self, config):
        self.config = config
        self.seen.append(config)


class _FailingComponent(_Component):
    def refresh_config(self, config):
        super().refresh_config(config)
        if getattr(config.reply, "fallback_text", "") == "bad":
            raise RuntimeError("bad config")


class HotConfigConsistencyIntegrationTests(unittest.TestCase):
    def _facade(self, config, *, failing=False):
        from astrmai.app.plugin_facade import PluginFacade
        from astrmai.conversation.execution.reply_service import ReplyService

        state_engine = _Component(config)
        reply_service = ReplyService(state_engine, mood_manager=SimpleNamespace(), config=config)
        memory_engine = _Component(config)
        attention_gate = _Component(config)
        lane_manager = _Component(config)
        context_compaction = _Component(config)
        persona_summarizer = _Component(config)
        proactive_task = _FailingComponent(config) if failing else _Component(config)
        system2_planner = _Component(config)
        runtime = SimpleNamespace(
            raw_config={"reply": {"fallback_text": config.reply.fallback_text}},
            config=config,
            background_tasks=set(),
            lifecycle=SimpleNamespace(manager=None),
            gateway=_Component(config),
            lane_manager=lane_manager,
            state_engine=state_engine,
            sensors=_Component(config),
            frequency_controller=_Component(config),
            private_chat_manager=_Component(config),
            attention_gate=attention_gate,
            evolution=_Component(config),
            memory_engine=memory_engine,
            judge=_Component(config),
            proactive_task=proactive_task,
            system2_planner=system2_planner,
            reply_engine=reply_service,
            context_compaction=context_compaction,
            persona_summarizer=persona_summarizer,
            sys3_router=None,
            cron_guard=None,
            bind_system2_callback=lambda callback: None,
            rebuild_infrastructure_settings=lambda: None,
            sync_host_compat_attrs=lambda: None,
        )
        facade = PluginFacade(runtime)
        return facade, runtime, reply_service, memory_engine, attention_gate

    def test_hot_config_refreshes_runtime_and_core_components(self):
        from config import AstrMaiConfig

        old_config = AstrMaiConfig(reply={"fallback_text": "old"}, memory={"recall_top_k": 3})
        new_config = AstrMaiConfig(
            reply={"fallback_text": "new", "segment_min_len": 7, "no_segment_max_len": 77, "meme_probability": 11},
            memory={"recall_top_k": 9},
            attention={"focus_thread_enabled": False},
        )
        facade, runtime, reply_service, memory_engine, attention_gate = self._facade(old_config)

        ok = facade.apply_hot_config({"reply": {"fallback_text": "new"}}, new_config)

        self.assertTrue(ok)
        self.assertIs(runtime.config, new_config)
        self.assertEqual(runtime.config.reply.fallback_text, "new")
        self.assertIs(reply_service.config, new_config)
        self.assertEqual(reply_service.config.reply.fallback_text, "new")
        self.assertEqual(reply_service.segmentation_threshold, 7)
        self.assertEqual(reply_service.no_segment_limit, 77)
        self.assertEqual(reply_service.meme_probability, 11)
        self.assertIs(memory_engine.config, new_config)
        self.assertEqual(memory_engine.config.memory.recall_top_k, 9)
        self.assertIs(attention_gate.config, new_config)
        self.assertFalse(attention_gate.config.attention.focus_thread_enabled)
        self.assertIs(runtime.context_compaction.config, new_config)
        self.assertIs(runtime.persona_summarizer.config, new_config)
        self.assertIs(runtime.system2_planner.config, new_config)

    def test_reply_service_refresh_config_is_idempotent_and_preserves_runtime_state(self):
        from config import AstrMaiConfig

        old_config = AstrMaiConfig(reply={"fallback_text": "old", "segment_min_len": 3, "no_segment_max_len": 33})
        new_config = AstrMaiConfig(reply={"fallback_text": "new", "segment_min_len": 8, "no_segment_max_len": 88})
        _facade, _runtime, reply_service, _memory_engine, _attention_gate = self._facade(old_config)
        runtime_marker = object()
        reply_service.runtime_coordinator = runtime_marker
        reply_service.pending_marker = ["in-flight"]

        reply_service.refresh_config(new_config)
        reply_service.refresh_config(new_config)

        self.assertIs(reply_service.config, new_config)
        self.assertEqual(reply_service.segmentation_threshold, 8)
        self.assertEqual(reply_service.no_segment_limit, 88)
        self.assertIs(reply_service.runtime_coordinator, runtime_marker)
        self.assertEqual(reply_service.pending_marker, ["in-flight"])
        self.assertIsNotNone(reply_service.segmenter)

    def test_hot_config_rolls_back_all_components_when_refresh_fails(self):
        from config import AstrMaiConfig

        old_config = AstrMaiConfig(reply={"fallback_text": "old"}, memory={"recall_top_k": 3})
        bad_config = AstrMaiConfig(
            reply={"fallback_text": "bad", "segment_min_len": 9, "no_segment_max_len": 99},
            memory={"recall_top_k": 9},
        )
        facade, runtime, reply_service, memory_engine, attention_gate = self._facade(old_config, failing=True)

        ok = facade.apply_hot_config({"reply": {"fallback_text": "bad"}}, bad_config)

        self.assertFalse(ok)
        self.assertIs(runtime.config, old_config)
        self.assertEqual(runtime.raw_config, {"reply": {"fallback_text": "old"}})
        self.assertIs(reply_service.config, old_config)
        self.assertEqual(reply_service.config.reply.fallback_text, "old")
        self.assertEqual(reply_service.segmentation_threshold, old_config.reply.segment_min_len)
        self.assertEqual(reply_service.no_segment_limit, old_config.reply.no_segment_max_len)
        self.assertIs(memory_engine.config, old_config)
        self.assertIs(attention_gate.config, old_config)
        self.assertIs(runtime.context_compaction.config, old_config)
        self.assertIs(runtime.persona_summarizer.config, old_config)
        self.assertIs(runtime.system2_planner.config, old_config)

    def test_hot_enable_work_mode_requires_runtime_stack_and_keeps_old_config(self):
        from config import AstrMaiConfig

        old_config = AstrMaiConfig(sys3={"enable_work_mode": False})
        new_config = AstrMaiConfig(sys3={"enable_work_mode": True})
        facade, runtime, _reply_service, _memory_engine, _attention_gate = self._facade(old_config)

        ok = facade.apply_hot_config({"sys3": {"enable_work_mode": True}}, new_config)

        self.assertFalse(ok)
        self.assertIs(runtime.config, old_config)
        self.assertFalse(runtime.config.sys3.enable_work_mode)

    def test_sys3_direct_reports_restart_required_when_stack_is_missing(self):
        import asyncio
        from config import AstrMaiConfig

        config = AstrMaiConfig(sys3={"enable_work_mode": True})
        facade, runtime, _reply_service, _memory_engine, _attention_gate = self._facade(config)
        runtime.rebuild_infrastructure_settings()
        runtime.feature_flags = SimpleNamespace(work_mode_enabled=True)
        event = SimpleNamespace(
            message_str="/work test",
            unified_msg_origin="default:GroupMessage:group-1",
            plain_result=lambda text: text,
        )

        async def _collect():
            return [item async for item in facade.enter_sys3_direct(event)]

        result = asyncio.run(_collect())

        self.assertEqual(len(result), 1)
        self.assertIn("restart", result[0].lower())


if __name__ == "__main__":
    unittest.main()
