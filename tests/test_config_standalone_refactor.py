import unittest
import json
from pathlib import Path

from config import AstrMaiConfig, MemoryConfig
from astrmai.shared.emotion_tags import build_emotion_tag_catalog


class ConfigStandaloneRefactorTests(unittest.TestCase):
    def test_astrmai_config_instantiates_with_expected_defaults(self):
        config = AstrMaiConfig()
        self.assertEqual(config.agent.max_steps, 5)
        self.assertTrue(hasattr(config.provider, "embedding_models"))
        self.assertTrue(hasattr(config, "performance"))
        self.assertEqual(config.performance.summary_threshold, 300)
        self.assertTrue(hasattr(config.global_settings, "enable_private_chat"))
        self.assertTrue(hasattr(config.sys3, "enable_work_mode"))
        self.assertTrue(hasattr(config.vision, "use_native_main_reply_vision"))
        self.assertTrue(hasattr(config.vision, "native_main_reply_failure_cooldown_sec"))
        self.assertEqual(config.vision.vision_reply_policy, "超时后忽略图片并继续回复")
        self.assertEqual(config.vision.image_analysis_retries, 2)
        self.assertTrue(config.vision.enable_visual_result_cache)
        self.assertFalse(config.vision.store_visual_asset_files)
        self.assertEqual(config.vision.visual_asset_retention_days, 30)
        self.assertEqual(config.vision.visual_asset_max_disk_mb, 512)
        self.assertEqual(config.vision.visual_asset_max_edge_px, 1600)
        self.assertEqual(config.vision.visual_prompt_version, "v1")
        self.assertEqual(config.vision.visual_failure_cooldown_sec, 120)
        self.assertEqual(config.vision.max_images_per_turn, 1)
        self.assertTrue(config.vision.ignore_placeholder_without_question)
        self.assertTrue(hasattr(config, "conversation"))
        self.assertEqual(config.conversation.compaction_trigger_segments, 40)
        self.assertEqual(config.conversation.compaction_keep_recent_segments, 16)
        self.assertTrue(config.memory.memory_query_builder_enabled)
        self.assertFalse(config.memory.intent_rerank_enabled)
        self.assertFalse(config.memory.adaptive_top_k_enabled)
        self.assertFalse(config.memory.memory_retrieval_debug_trace_enabled)
        self.assertEqual(config.attention.judge_timeout, 3.0)
        self.assertTrue(config.attention.participation_drop_enabled)
        self.assertEqual(config.attention.judge_ambient_cooldown_sec, 3.0)
        self.assertEqual(config.sys3.max_steps, 30)
        self.assertEqual(config.sys3.tool_timeout, 120)
        self.assertEqual(config.timing.model_request_timeout_sec, 15.0)
        self.assertEqual(config.timing.fast_mode_execution_timeout_sec, 15)
        self.assertEqual(config.timing.reply_max_age_sec, 0.0)
        self.assertEqual(config.timing.sys2_lock_wait_timeout_sec, 20.0)
        self.assertEqual(config.timing.executor_lock_wait_timeout_sec, 15.0)
        self.assertEqual(config.timing.lane_prepare_timeout_sec, 20.0)
        self.assertEqual(config.timing.lane_persist_timeout_sec, 5.0)
        self.assertEqual(config.timing.faiss_timeout_sec, 20.0)
        self.assertEqual(config.timing.faiss_query_concurrency, 2)
        self.assertEqual(config.timing.faiss_thread_count, 1)
        self.assertEqual(config.infra.background_task_concurrency, 2)
        self.assertEqual(config.infra.background_task_queue_limit, 64)
        self.assertEqual(config.infra.background_task_wait_timeout_sec, 120.0)
        self.assertEqual(config.memory.maintenance_concurrency, 1)
        self.assertEqual(config.timing.image_resolve_timeout_sec, 15.0)
        self.assertEqual(config.timing.image_analysis_timeout_sec, 90.0)
        self.assertEqual(config.timing.vision_barrier_total_timeout_sec, 300.0)
        self.assertEqual(config.private_chat.image_barrier_timeout_sec, 90.0)
        self.assertEqual(config.evolution.jargon_min_count, 2)
        self.assertEqual(config.evolution.expression_min_count, 2)
        self.assertEqual(config.evolution.learning_pipeline_concurrency, 1)
        self.assertEqual(config.life.daily_schedule_max_retries, 2)
        self.assertEqual(config.life.daily_schedule_retry_base_sec, 300)
        self.assertEqual(config.life.proactive_quiet_recheck_sec, 7200)
        self.assertEqual(config.life.proactive_failure_backoff_factor, 2.0)
        self.assertEqual(config.life.profiling_user_cooldown_sec, 21600)
        self.assertEqual(config.reply.emotion_relationship_mapping, [])

    def test_emotion_catalog_accepts_deployed_custom_tags_and_relationship_override(self):
        deployed_tags = [
            "angry: 生气、愤怒、不满",
            "happy: 开心、积极、感谢",
            "sad: 悲伤、难过、遗憾",
            "surprised: 惊讶、震惊、意外",
            "confused: 困惑、疑惑、不理解",
            "color: 暧昧、调情、心动",
            "cpu: 卡顿、宕机、无语",
            "fool: 幽默、自嘲、开玩笑",
            "like: 喜欢、欣赏、认同",
            "see: 看见、注意到、发现",
            "shy: 害羞、扭捏、不好意思",
            "meow: 猫叫、撒娇、卖萌",
            "baka: 笨蛋、吐槽、调侃",
            "morning: 早安、晨间、打招呼",
            "sleep: 困倦、晚安、休息",
            "sigh: 叹气、无奈、疲惫",
            "hello: 你好、问候、招呼",
            "work: 工作、忙碌、加班",
            "givemoney: 打钱、红包、奖励",
            "reply: 回复、回应、收到",
        ]
        config = AstrMaiConfig(
            reply={
                "emotion_mapping": deployed_tags,
                "emotion_relationship_mapping": ["surprised: shared_interest"],
            }
        )
        catalog = build_emotion_tag_catalog(config)

        self.assertEqual(catalog.tags, {item.split(":", 1)[0] for item in deployed_tags})
        self.assertTrue(catalog.contains("SURPRISED"))
        self.assertEqual(
            catalog.resolve_relationship_event("surprised").event_type,
            "shared_interest",
        )
        self.assertEqual(
            catalog.resolve_relationship_event("confused").source,
            "fallback_normal_chat",
        )

    def test_astrmai_config_migrates_legacy_timing_fields_and_syncs_aliases(self):
        config = AstrMaiConfig(
            infra={"api_timeout": 240.0},
            agent={"timeout": 600},
            attention={"judge_timeout": 120.0},
            private_chat={
                "image_resolve_timeout_sec": 150.0,
                "image_barrier_timeout_sec": 210.0,
            },
        )

        self.assertEqual(config.timing.model_request_timeout_sec, 240.0)
        self.assertEqual(config.timing.agent_execution_timeout_sec, 600)
        self.assertEqual(config.timing.attention_judge_timeout_sec, 120.0)
        self.assertEqual(config.timing.image_resolve_timeout_sec, 150.0)
        self.assertEqual(config.timing.image_analysis_timeout_sec, 210.0)
        self.assertEqual(config.infra.api_timeout, 240.0)
        self.assertEqual(config.agent.timeout, 600)
        self.assertEqual(config.attention.judge_timeout, 120.0)
        self.assertEqual(config.private_chat.image_resolve_timeout_sec, 150.0)
        self.assertEqual(config.private_chat.image_barrier_timeout_sec, 210.0)

    def test_central_timing_fields_override_legacy_locations(self):
        config = AstrMaiConfig(
            infra={"api_timeout": 30.0},
            agent={"timeout": 60},
            timing={
                "model_request_timeout_sec": 300.0,
                "agent_execution_timeout_sec": 900,
                "reply_max_age_sec": 1200.0,
            },
        )

        self.assertEqual(config.infra.api_timeout, 300.0)
        self.assertEqual(config.agent.timeout, 900)
        self.assertEqual(config.reply.stale_reply_max_age_sec, 1200.0)

    def test_legacy_private_vision_retries_migrate_to_vision_namespace(self):
        config = AstrMaiConfig(private_chat={"image_analysis_retries": 4})

        self.assertEqual(config.vision.image_analysis_retries, 4)
        self.assertEqual(config.private_chat.image_analysis_retries, 4)

    def test_vision_policy_aliases_and_invalid_values_are_normalized(self):
        strict = AstrMaiConfig(vision={"vision_reply_policy": "strict"})
        invalid = AstrMaiConfig(vision={"vision_reply_policy": "unknown"})

        self.assertEqual(strict.vision.vision_reply_policy, "必须识别成功后再回复")
        self.assertEqual(invalid.vision.vision_reply_policy, "超时后忽略图片并继续回复")

    def test_workmode_timeout_accepts_long_running_model_budget(self):
        config = AstrMaiConfig(timing={"workmode_execution_timeout_sec": 9999})

        self.assertEqual(config.timing.workmode_execution_timeout_sec, 9999)
        self.assertEqual(config.sys3.tool_timeout, 9999)

    def test_astrmai_config_accepts_conversation_and_memory_namespace_fields(self):
        config = AstrMaiConfig(
            conversation={
                "compaction_trigger_segments": 64,
                "compaction_keep_recent_segments": 20,
            },
            memory={
                "deep_temporal_alpha": 0.5,
                "maintenance_hot_beta": 0.25,
            },
        )

        self.assertEqual(config.conversation.compaction_trigger_segments, 64)
        self.assertEqual(config.conversation.compaction_keep_recent_segments, 20)
        self.assertEqual(config.memory.deep_temporal_alpha, 0.5)
        self.assertEqual(config.memory.maintenance_hot_beta, 0.25)

    def test_astrmai_config_migrates_legacy_global_memory_fields(self):
        config = AstrMaiConfig(
            global_settings={
                "debug_mode": True,
                "maintenance_hot_beta": 0.3,
                "deep_temporal_alpha": 0.4,
            }
        )

        self.assertTrue(config.global_settings.debug_mode)
        self.assertEqual(config.memory.maintenance_hot_beta, 0.3)
        self.assertEqual(config.memory.deep_temporal_alpha, 0.4)

    def test_memory_namespace_overrides_legacy_global_fields(self):
        config = AstrMaiConfig(
            global_settings={"maintenance_hot_beta": 0.3},
            memory={"maintenance_hot_beta": 0.6},
        )

        self.assertEqual(config.memory.maintenance_hot_beta, 0.6)

    def test_memory_config_instance_is_preserved(self):
        config = AstrMaiConfig(
            memory=MemoryConfig(
                deep_temporal_alpha=0.9,
                maintenance_hot_beta=0.1,
            )
        )

        self.assertEqual(config.memory.deep_temporal_alpha, 0.9)
        self.assertEqual(config.memory.maintenance_hot_beta, 0.1)

    def test_astrmai_config_accepts_performance_and_evolution_fields(self):
        config = AstrMaiConfig(
            performance={"summary_threshold": 512},
            evolution={"jargon_min_count": 5},
        )

        self.assertEqual(config.performance.summary_threshold, 512)
        self.assertEqual(config.evolution.jargon_min_count, 5)

    def test_schema_json_is_parseable_and_contains_runtime_config_fields(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        performance_items = schema["performance"]["items"]
        vision_items = schema["vision"]["items"]
        evolution_items = schema["evolution"]["items"]
        attention_items = schema["attention"]["items"]
        infra_items = schema["infra"]["items"]
        timing_items = schema["timing"]["items"]
        self.assertEqual(timing_items["sys2_lock_wait_timeout_sec"]["default"], 20.0)
        self.assertEqual(timing_items["executor_lock_wait_timeout_sec"]["default"], 15.0)
        self.assertEqual(timing_items["lane_prepare_timeout_sec"]["default"], 20.0)
        self.assertEqual(timing_items["lane_persist_timeout_sec"]["default"], 5.0)
        memory_items = schema["memory"]["items"]
        reply_items = schema["reply"]["items"]
        self.assertIn("summary_threshold", performance_items)
        self.assertIn("use_native_main_reply_vision", vision_items)
        self.assertIn("native_main_reply_failure_cooldown_sec", vision_items)
        self.assertIn("background_task_concurrency", infra_items)
        self.assertIn("background_task_queue_limit", infra_items)
        self.assertIn("background_task_wait_timeout_sec", infra_items)
        self.assertIn("judge_ambient_cooldown_sec", attention_items)
        self.assertIn("faiss_query_concurrency", timing_items)
        self.assertIn("faiss_thread_count", timing_items)
        self.assertIn("maintenance_concurrency", memory_items)
        self.assertEqual(
            vision_items["vision_reply_policy"]["options"],
            ["超时后忽略图片并继续回复", "必须识别成功后再回复"],
        )
        self.assertEqual(vision_items["image_analysis_retries"]["default"], 2)
        self.assertTrue(vision_items["enable_visual_result_cache"]["default"])
        self.assertFalse(vision_items["store_visual_asset_files"]["default"])
        self.assertEqual(vision_items["visual_asset_retention_days"]["default"], 30)
        self.assertEqual(vision_items["visual_asset_max_disk_mb"]["default"], 512)
        self.assertEqual(vision_items["visual_asset_max_edge_px"]["default"], 1600)
        self.assertEqual(vision_items["visual_prompt_version"]["default"], "v1")
        self.assertIn("emotion_relationship_mapping", reply_items)
        self.assertEqual(reply_items["emotion_relationship_mapping"]["default"], [])
        self.assertEqual(vision_items["visual_failure_cooldown_sec"]["default"], 120)
        self.assertEqual(vision_items["visual_failure_cooldown_sec"]["maximum"], 1800)
        self.assertEqual(vision_items["max_images_per_turn"]["default"], 1)
        self.assertEqual(vision_items["max_images_per_turn"]["maximum"], 8)
        self.assertTrue(vision_items["ignore_placeholder_without_question"]["default"])
        self.assertNotIn("image_analysis_retries", schema["private_chat"]["items"])
        self.assertIn("jargon_min_count", evolution_items)
        self.assertIn("expression_min_count", evolution_items)
        self.assertEqual(performance_items["summary_threshold"]["default"], 300)
        self.assertEqual(evolution_items["jargon_min_count"]["default"], 2)
        self.assertEqual(evolution_items["expression_min_count"]["default"], 2)
        self.assertEqual(schema["conversation"]["items"]["compaction_trigger_segments"]["default"], 40)
        self.assertEqual(schema["conversation"]["items"]["compaction_keep_recent_segments"]["default"], 16)
        conversation_items = schema["conversation"]["items"]
        self.assertEqual(conversation_items["group_actor_tail_ttl_sec"]["default"], 1200)
        self.assertEqual(conversation_items["group_actor_tail_max_segments"]["default"], 8)
        self.assertEqual(conversation_items["group_pending_direct_ttl_sec"]["default"], 1200)
        self.assertEqual(conversation_items["group_social_incident_ttl_sec"]["default"], 1800)
        self.assertEqual(conversation_items["group_context_snapshot_max_chars"]["default"], 5500)
        self.assertTrue(conversation_items["group_pre_send_freshness_enabled"]["default"])
        memory_items = schema["memory"]["items"]
        self.assertTrue(memory_items["memory_query_builder_enabled"]["default"])
        self.assertFalse(memory_items["intent_rerank_enabled"]["default"])
        self.assertFalse(memory_items["adaptive_top_k_enabled"]["default"])
        self.assertFalse(memory_items["memory_retrieval_debug_trace_enabled"]["default"])
        timing_items = schema["timing"]["items"]
        self.assertEqual(timing_items["attention_judge_timeout_sec"]["default"], 3.0)
        self.assertEqual(timing_items["agent_execution_timeout_sec"]["default"], 60)
        self.assertEqual(timing_items["fast_mode_execution_timeout_sec"]["default"], 15)
        self.assertEqual(timing_items["workmode_execution_timeout_sec"]["default"], 120)
        self.assertEqual(timing_items["workmode_execution_timeout_sec"]["maximum"], 86400)
        self.assertEqual(timing_items["faiss_timeout_sec"]["default"], 20.0)
        self.assertEqual(timing_items["image_resolve_timeout_sec"]["maximum"], 600)
        self.assertEqual(timing_items["image_analysis_timeout_sec"]["default"], 90.0)
        self.assertEqual(timing_items["vision_barrier_total_timeout_sec"]["default"], 300.0)
        self.assertEqual(timing_items["vision_barrier_total_timeout_sec"]["maximum"], 3600)
        self.assertNotIn("judge_timeout", schema["attention"]["items"])
        self.assertEqual(schema["sys3"]["items"]["max_steps"]["default"], 30)
        self.assertNotIn("tool_timeout", schema["sys3"]["items"])
        self.assertEqual(schema["life"]["items"]["energy_exhaustion"]["default"], 0.1)
        self.assertEqual(schema["life"]["items"]["profiling_user_cooldown_sec"]["default"], 21600)
        meme_mapping_hint = schema["reply"]["items"]["emotion_mapping"]["hint"]
        self.assertIn("memes_data/memes/<标签名>/", meme_mapping_hint)
        self.assertIn("给模型看的情绪说明", meme_mapping_hint)


if __name__ == "__main__":
    unittest.main()
