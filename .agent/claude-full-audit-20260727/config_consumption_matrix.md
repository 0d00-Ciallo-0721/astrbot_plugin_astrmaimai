# 配置项全量落地矩阵（_conf_schema.json ↔ config.py ↔ 业务代码）

> 生成方法：脚本提取 schema 全部叶子键（197 个）与 AstrMaiConfig pydantic 字段（205 个），对每个键在 astrmai/（排除 webui/venv）+ main.py 做三级模式匹配（`section.key` 点式 / `.key` 属性 / `"key"` 字符串），零命中键逐个人工 Read 复核动态访问。证据列取首个消费点。

## ① 有 schema 无消费（死配置 — 用户改了没效果）：9 项

| # | 配置键 | schema 默认值 | 判死依据（人工复核） |
|---|--------|---------------|----------------------|
| 1 | `attention.debounce_window` | `2.0` | gate 组内防抖硬编码 0.25/0.45/0.70s（window_buffer.py L17-24），私聊用 private_chat.input_settle_sec |
| 2 | `attention.max_message_length` | `100` | 全代码库无任何引用 |
| 3 | `attention.repeater_threshold` | `3` | 复读判定硬编码 repeat_count>=2（gate.py L928），配置不生效 |
| 4 | `attention.throttle_min_entropy` | `2` | 同上，无引用 |
| 5 | `attention.throttle_probability` | `0.1` | 限流已改为纯能量驱动 should_drop_by_energy（gate.py L913-920） |
| 6 | `evolution.enable_relationship_engine` | `True` | RelationshipEngine 无条件实例化（chat_state_service.py L271），开关无效 |
| 7 | `mood.unknown_decay` | `0.1` | mood_decay.py 只用 decay_interval/decay_rate，无引用 |
| 8 | `reply.enable_content_safety_filter` | `False` | 全代码库不存在任何 NSFW/自残/PII 过滤实现，纯虚假开关 |
| 9 | `timing.turn_merge_enabled` | `True` | schema 有、TimingConfig 无此字段且 legacy 映射未含 → pydantic 静默丢弃，业务读 private_chat.turn_merge_enabled 恒为默认 True（运行时已实证） |

注：`timing.private_input_settle_sec` / `timing.private_wait_timeout_sec` / `timing.workmode_execution_timeout_sec` 三个零命中键**不是**死配置——它们在 config.py `_sync_legacy_timing_aliases` 内被写回 private_chat.*/sys3.tool_timeout 后被业务消费（间接生效）。

## ② 有消费无 schema（隐藏配置）：12 项 — 全部为 legacy 别名容器，无失控项

| 配置键（pydantic-only） | 实际取值来源 |
|--------------------------|--------------|
| `agent.timeout` | timing.agent_execution_timeout_sec 经 _sync_legacy_timing_aliases 写回 |
| `attention.affection_weights` | schema 以嵌套 object(trigger/window/history) 表达，pydantic 为 Dict[str,float]，等价 |
| `attention.judge_timeout` | timing.attention_judge_timeout_sec 写回 |
| `infra.api_timeout` | timing.model_request_timeout_sec 写回 |
| `private_chat.image_analysis_retries` | vision.image_analysis_retries 经 _sync_legacy_vision_aliases 写回 |
| `private_chat.image_barrier_timeout_sec` | timing.image_analysis_timeout_sec 写回 |
| `private_chat.image_resolve_timeout_sec` | timing.image_resolve_timeout_sec 写回 |
| `private_chat.input_settle_sec` | timing.private_input_settle_sec 写回 |
| `private_chat.turn_merge_enabled` | !! schema 侧对应键 timing.turn_merge_enabled 是死键（见①-9），此字段只能吃默认值 True |
| `private_chat.wait_timeout_sec` | timing.private_wait_timeout_sec 写回 |
| `reply.stale_reply_max_age_sec` | timing.reply_max_age_sec 写回 |
| `sys3.tool_timeout` | timing.workmode_execution_timeout_sec 写回 |

结论：不存在"业务读了一个 UI 完全看不到的键"的失控隐藏配置；12 项全部是 timing/vision 集中化（commit 4417ece）留下的别名回写目标，UI 侧由 timing.*/vision.* 键控制。唯一漏网是 turn_merge_enabled（见①-9）。

## ③ schema default 与 config.py 默认值不一致：直接比对 0 项；第三层（业务代码 getattr fallback）漂移 11 处

schema 与 pydantic 的同名键默认值 **全部一致（0 mismatch）**。但业务代码 `getattr(cfg_section, key, fallback)` 的 fallback 常量与 pydantic 默认值不一致——仅当配置分节对象缺失（测试注入部分配置 / 热更失败回滚残局）时生效，正常运行不影响：

| 文件:行 | 键 | pydantic 默认 | 代码 fallback | 缺失分节时的行为差异 |
|---------|----|----------------|----------------|----------------------|
| `astrmai/state/chat_state_service.py:167` | `energy.daily_recovery` | 0.2 | 0.05 | 每日能量恢复缩水 4 倍 |
| `astrmai/state/energy/energy_manager.py:18` | `energy.cost_per_reply` | 0.05 | 0.1 | 回复成本翻倍→更快沉默 |
| `astrmai/state/mood/mood_decay.py:36` | `mood.decay_rate` | 0.1 | 0.05 | 情绪衰减减半 |
| `astrmai/conversation/ingress/sensors.py:255` | `vision.image_recognition_probability` | 0.5 | 1.0 | 识图概率翻倍→视觉成本上升 |
| `astrmai/conversation/ingress/command_guard.py:72` | `tts.enabled` | False | True | TTS 默认反转为开启 |
| `astrmai/conversation/execution/followup_manager.py:19` | `attention.thread_same_speaker_followup_sec` | 8 | 0.0 | 同人跟帖窗口失效 |
| `astrmai/presentation/events/message_entry.py:33` | `reply.fallback_text` | '（陷入了短暂的沉默...）' | '' | 失败兜底文案变为空 |
| `astrmai/memory/services/memory_write_service.py:39` | `memory.min_memory_confidence` | 0.3 | 0.0 | 低置信记忆全部入库 |
| `astrmai/app/plugin_facade.py:620` | `global_settings.command_prefixes` | ['/','!','！'] | [] | 命令前缀检测失效 |
| `astrmai/conversation/attention/compaction_providers.py:66` | `infra.api_timeout` | 15.0 | 60.0 | 压缩兜底超时放宽 4 倍（分层兜底，影响极小） |
| `astrmai/infrastructure/gateway/model_gateway.py:47` | `infra.max_concurrent_llm_calls` | 3 | 1 | 热更比较基线错位（无行为影响） |

## ④ 约束层缺口（schema min/max vs pydantic ge/le）

- schema 声明 `minimum`/`maximum` 的键仅 **25 个**（timing 全部 20 + private_chat.topic_* 4 + memory.min_memory_confidence），
  其余 **约 90 个数值键有 pydantic ge/le 约束但 schema 无 min/max**（完整清单见 05 报告附录）。
- 后果：AstrBot 配置 UI 不做范围校验的键，用户填越界值（如 meme_probability=101、bg_pool_size=0、负超时）→ `AstrMaiConfig(**raw)` 在 main.py L65 抛 `pydantic.ValidationError` 且**无 try/except** → 插件整体加载失败（详见 PL-06）。
- 反向：schema 有 min/max 的 25 个键与 pydantic 边界一致，无"UI 合法但 pydantic 拒绝"的键。

## ⑤ 全量键位明细（197 schema 叶子 + 12 pydantic-only）

| 配置键 | schema | pydantic | 消费方式 | 首个消费点 |
|--------|--------|----------|----------|-----------|
| `agent.max_steps` | Y | Y | getattr/str | `astrmai/app/plugin_facade.py:719 (+6)` |
| `agent.timeout` | - | Y | dotted | `astrmai/conversation/execution/executor.py:534` |
| `attention.adjudication_threshold` | Y | Y | getattr/str | `astrmai/state/relationship/affection_router.py:148` |
| `attention.affection_weights` | - | Y | getattr/str | `astrmai/state/relationship/affection_router.py:144` |
| `attention.affection_weights.history` | Y | - | attr | `astrmai/conversation/planning/planner_side_inputs.py:1249 (+2)` |
| `attention.affection_weights.trigger` | Y | - | getattr/str | `astrmai/state/relationship/affection_router.py:144 (+1)` |
| `attention.affection_weights.window` | Y | - | getattr/str | `astrmai/state/relationship/affection_router.py:144 (+1)` |
| `attention.ambient_background_max_messages` | Y | Y | getattr/str | `astrmai/conversation/attention/thread_builder.py:145` |
| `attention.bg_pool_size` | Y | Y | getattr/str | `astrmai/conversation/execution/reply_post_send.py:70` |
| `attention.debounce_window` | Y | Y | **DEAD** | `-` |
| `attention.focus_thread_core_max_messages` | Y | Y | getattr/str | `astrmai/conversation/attention/thread_builder.py:143` |
| `attention.focus_thread_enabled` | Y | Y | getattr/str | `astrmai/conversation/attention/focus_selector.py:78 (+1)` |
| `attention.focus_thread_related_max_messages` | Y | Y | getattr/str | `astrmai/conversation/attention/thread_builder.py:144` |
| `attention.judge_timeout` | - | Y | getattr/str | `astrmai/conversation/attention/decision_router.py:112 (+2)` |
| `attention.max_message_length` | Y | Y | **DEAD** | `-` |
| `attention.repeater_threshold` | Y | Y | **DEAD** | `-` |
| `attention.sensitive_words` | Y | Y | getattr/str | `astrmai/state/relationship/affection_router.py:151` |
| `attention.thread_reply_priority_enabled` | Y | Y | getattr/str | `astrmai/conversation/attention/focus_selector.py:12` |
| `attention.thread_same_speaker_followup_sec` | Y | Y | getattr/str | `astrmai/conversation/attention/focus_selector.py:11 (+3)` |
| `attention.throttle_min_entropy` | Y | Y | **DEAD** | `-` |
| `attention.throttle_probability` | Y | Y | **DEAD** | `-` |
| `conversation.autonomous_chat_tools_enabled` | Y | Y | getattr/str | `astrmai/conversation/planning/planner_side_inputs.py:1034 (+2)` |
| `conversation.compaction_keep_recent_segments` | Y | Y | attr | `astrmai/conversation/attention/context_compaction.py:1242 (+7)` |
| `conversation.compaction_provider_id` | Y | Y | getattr/str | `astrmai/app/bootstrap.py:246 (+1)` |
| `conversation.compaction_summary_max_tokens` | Y | Y | attr | `astrmai/conversation/attention/context_compaction.py:1719 (+5)` |
| `conversation.compaction_trigger_segments` | Y | Y | attr | `astrmai/conversation/attention/context_compaction.py:190 (+1)` |
| `conversation.compaction_trigger_tokens` | Y | Y | attr | `astrmai/conversation/attention/context_compaction.py:191 (+1)` |
| `conversation.context_dedup_enabled` | Y | Y | getattr/str | `astrmai/conversation/planning/prompt_refiner.py:917` |
| `conversation.context_dedup_observe_only` | Y | Y | getattr/str | `astrmai/conversation/planning/planner.py:1480 (+1)` |
| `conversation.conversation_concurrency_debug_trace_enabled` | Y | Y | getattr/str | `astrmai/conversation/concurrency/controls.py:49` |
| `conversation.conversation_generation_enabled` | Y | Y | getattr/str | `astrmai/conversation/attention/gate.py:545 (+1)` |
| `conversation.enable_context_compaction` | Y | Y | getattr/str | `astrmai/shared/constants/defaults.py:115` |
| `conversation.enable_dialogue_store` | Y | Y | getattr/str | `astrmai/shared/constants/defaults.py:114` |
| `conversation.enable_prefix_caching` | Y | Y | getattr/str | `astrmai/conversation/planning/context_engine.py:38 (+4)` |
| `conversation.enable_token_estimator` | Y | Y | getattr/str | `astrmai/shared/constants/defaults.py:118` |
| `conversation.explicit_tool_execution_enabled` | Y | Y | getattr/str | `astrmai/conversation/planning/planner_side_inputs.py:1109` |
| `conversation.group_thread_wait_enabled` | Y | Y | attr | `astrmai/app/plugin_facade.py:316 (+2)` |
| `conversation.hot_zone_ttl_seconds` | Y | Y | attr | `astrmai/conversation/attention/group_dialogue_store.py:55` |
| `conversation.non_conversational_guard_enabled` | Y | Y | attr | `astrmai/presentation/events/message_entry.py:259` |
| `conversation.qq_deferred_action_commit_enabled` | Y | Y | getattr/str | `astrmai/conversation/execution/qq_action_dispatcher.py:41 (+2)` |
| `conversation.qq_explicit_intent_override_enabled` | Y | Y | getattr/str | `astrmai/conversation/planning/planner_side_inputs.py:818` |
| `conversation.qq_native_tools_enabled` | Y | Y | getattr/str | `astrmai/conversation/execution/qq_action_dispatcher.py:40 (+2)` |
| `conversation.reply_send_claim_enabled` | Y | Y | getattr/str | `astrmai/conversation/concurrency/controls.py:43` |
| `conversation.tool_disclosure_allow_second_pass` | Y | Y | getattr/str | `astrmai/conversation/execution/executor.py:569 (+1)` |
| `conversation.tool_disclosure_max_tools_chat` | Y | Y | getattr/str | `astrmai/conversation/planning/planner_side_inputs.py:976` |
| `conversation.tool_disclosure_max_tools_task` | Y | Y | getattr/str | `astrmai/conversation/execution/executor.py:581 (+1)` |
| `conversation.tool_progressive_disclosure_enabled` | Y | Y | getattr/str | `astrmai/conversation/planning/planner_side_inputs.py:933` |
| `conversation.warm_zone_max_tokens` | Y | Y | attr | `astrmai/conversation/attention/group_dialogue_store.py:57 (+1)` |
| `conversation.warm_zone_ttl_seconds` | Y | Y | attr | `astrmai/conversation/attention/group_dialogue_store.py:56 (+1)` |
| `energy.cost_per_reply` | Y | Y | getattr/str | `astrmai/state/energy/energy_manager.py:18` |
| `energy.daily_recovery` | Y | Y | getattr/str | `astrmai/state/chat_state_service.py:167` |
| `energy.min_reply_threshold` | Y | Y | getattr/str | `astrmai/state/energy/energy_manager.py:38` |
| `energy.recovery_silence_min` | Y | Y | getattr/str | `astrmai/state/mood/mood_decay.py:19` |
| `evolution.backlog_batch_size` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:121` |
| `evolution.backlog_failure_cooldown_sec` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:133` |
| `evolution.backlog_group_limit` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:125` |
| `evolution.backlog_min_unprocessed_logs` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:114` |
| `evolution.backlog_scan_interval_sec` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:129` |
| `evolution.batch_size` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:248 (+2)` |
| `evolution.enable_backlog_mining` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:108` |
| `evolution.enable_expression_mining` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:107 (+1)` |
| `evolution.enable_relationship_engine` | Y | Y | **DEAD** | `-` |
| `evolution.expression_min_count` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:88 (+1)` |
| `evolution.jargon_min_count` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:93 (+2)` |
| `evolution.min_mining_context` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:115 (+3)` |
| `evolution.mining_cooldown_sec` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:49 (+1)` |
| `evolution.mining_trigger` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:47 (+1)` |
| `evolution.mining_window_min_messages` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:46 (+1)` |
| `evolution.mining_window_sec` | Y | Y | getattr/str | `astrmai/learning/evolution_manager.py:43 (+1)` |
| `evolution.review_batch_size` | Y | Y | getattr/str | `astrmai/learning/review/expression_auto_check_task.py:50 (+1)` |
| `evolution.review_min_count` | Y | Y | getattr/str | `astrmai/learning/review/expression_auto_check_task.py:51 (+1)` |
| `evolution.review_runner_interval_sec` | Y | Y | getattr/str | `astrmai/app/bootstrap.py:484 (+1)` |
| `evolution.review_runner_min_interval_sec` | Y | Y | getattr/str | `astrmai/learning/review/expression_auto_check_task.py:42 (+1)` |
| `global_settings.admin_ids` | Y | Y | getattr/str | `astrmai/conversation/execution/executor.py:1275 (+3)` |
| `global_settings.command_prefixes` | Y | Y | dotted | `astrmai/conversation/ingress/sensors.py:363` |
| `global_settings.debug_mode` | Y | Y | attr | `astrmai/app/runtime_context.py:346 (+2)` |
| `global_settings.enable_error_interception` | Y | Y | getattr/str | `astrmai/conversation/execution/executor.py:1272 (+2)` |
| `global_settings.enable_private_chat` | Y | Y | getattr/str | `astrmai/conversation/ingress/permission_guard.py:14 (+1)` |
| `global_settings.error_interception_mode` | Y | Y | getattr/str | `astrmai/conversation/execution/outbound_error_policy.py:34` |
| `global_settings.external_result_sources` | Y | Y | getattr/str | `astrmai/conversation/ingress/external_result_bridge.py:39` |
| `global_settings.whitelist_ids` | Y | Y | getattr/str | `astrmai/conversation/ingress/permission_guard.py:12` |
| `infra.api_timeout` | - | Y | attr | `astrmai/app/runtime_context.py:345 (+1)` |
| `infra.backoff_factor` | Y | Y | attr | `astrmai/app/runtime_context.py:344 (+1)` |
| `infra.llm_retries` | Y | Y | attr | `astrmai/app/runtime_context.py:343 (+1)` |
| `infra.max_concurrent_llm_calls` | Y | Y | attr | `astrmai/app/runtime_context.py:342 (+3)` |
| `infra.quota_model_cooldown_sec` | Y | Y | getattr/str | `astrmai/infrastructure/gateway/gateway_policy.py:54 (+1)` |
| `infra.rate_limit_model_cooldown_sec` | Y | Y | getattr/str | `astrmai/infrastructure/gateway/gateway_policy.py:52 (+1)` |
| `life.dream_interval_min` | Y | Y | getattr/str | `astrmai/proactive/dream_scheduler.py:26 (+1)` |
| `life.dream_send_target` | Y | Y | getattr/str | `astrmai/proactive/dream_scheduler.py:244` |
| `life.dream_time_ranges` | Y | Y | getattr/str | `astrmai/proactive/dream_scheduler.py:293` |
| `life.dream_visible` | Y | Y | attr | `astrmai/app/bootstrap.py:512 (+7)` |
| `life.enable_proactive` | Y | Y | getattr/str | `astrmai/shared/constants/defaults.py:111` |
| `life.energy_exhaustion` | Y | Y | getattr/str | `astrmai/conversation/planning/expression_policy.py:135` |
| `life.hostile_threshold` | Y | Y | getattr/str | `astrmai/conversation/planning/expression_policy.py:134` |
| `life.intimate_tool_threshold` | Y | Y | getattr/str | `astrmai/conversation/planning/expression_policy.py:133` |
| `life.min_memory_events_to_dream` | Y | Y | getattr/str | `astrmai/proactive/dream_scheduler.py:107 (+1)` |
| `life.proactive_quiet_hours` | Y | Y | getattr/str | `astrmai/proactive/dispatcher.py:179 (+1)` |
| `life.profiling_msg_threshold` | Y | Y | getattr/str | `astrmai/proactive/proactive_task.py:495` |
| `life.silence_threshold` | Y | Y | attr | `astrmai/proactive/wakeup_service.py:96` |
| `life.wakeup_cooldown` | Y | Y | attr | `astrmai/proactive/wakeup_service.py:173 (+1)` |
| `life.wakeup_cost` | Y | Y | attr | `astrmai/proactive/wakeup_service.py:172 (+1)` |
| `life.wakeup_min_energy` | Y | Y | attr | `astrmai/proactive/wakeup_service.py:97` |
| `memory.adaptive_top_k_enabled` | Y | Y | getattr/str | `astrmai/memory/services/memory_query_builder.py:249 (+3)` |
| `memory.auto_recall_probability` | Y | Y | getattr/str | `astrmai/conversation/planning/context_engine.py:531` |
| `memory.cleanup_interval` | Y | Y | getattr/str | `astrmai/memory/services/session_memory_summarizer.py:28 (+1)` |
| `memory.deep_temporal_alpha` | Y | Y | attr | `astrmai/memory/services/memory_scoring.py:61` |
| `memory.deep_temporal_candidate_pool_factor` | Y | Y | attr | `astrmai/memory/services/memory_retrieval_service.py:419` |
| `memory.deep_temporal_candidate_pool_min` | Y | Y | attr | `astrmai/memory/services/memory_retrieval_service.py:420` |
| `memory.deep_temporal_lambda_default` | Y | Y | attr | `astrmai/memory/services/memory_scoring.py:70` |
| `memory.deep_temporal_lambda_fact` | Y | Y | attr | `astrmai/memory/services/memory_scoring.py:68` |
| `memory.deep_temporal_llm_window` | Y | Y | attr | `astrmai/memory/services/memory_retrieval_service.py:428` |
| `memory.deep_temporal_tau_seconds` | Y | Y | attr | `astrmai/memory/services/memory_scoring.py:110 (+2)` |
| `memory.enable_react_agent` | Y | Y | attr | `astrmai/memory/retrieval/react_retriever.py:56` |
| `memory.intent_rerank_enabled` | Y | Y | getattr/str | `astrmai/memory/services/memory_query_builder.py:248 (+3)` |
| `memory.maintenance_hot_beta` | Y | Y | attr | `astrmai/memory/services/memory_scoring.py:104` |
| `memory.maintenance_temporal_stale_hot_threshold` | Y | Y | attr | `astrmai/memory/services/memory_maintenance_service.py:98` |
| `memory.memory_mmr_enabled` | Y | Y | getattr/str | `astrmai/memory/services/memory_query_builder.py:361 (+2)` |
| `memory.memory_quality_admission_enabled` | Y | Y | getattr/str | `astrmai/memory/services/memory_admission_service.py:41` |
| `memory.memory_query_builder_enabled` | Y | Y | getattr/str | `astrmai/memory/services/memory_query_builder.py:71` |
| `memory.memory_retrieval_debug_trace_enabled` | Y | Y | getattr/str | `astrmai/memory/services/memory_injection_service.py:306 (+6)` |
| `memory.memory_rrf_fusion_enabled` | Y | Y | getattr/str | `astrmai/memory/services/memory_query_builder.py:360 (+2)` |
| `memory.min_memory_confidence` | Y | Y | getattr/str | `astrmai/memory/services/memory_write_service.py:39` |
| `memory.prune_threshold` | Y | Y | getattr/str | `astrmai/memory/services/memory_engine.py:920 (+1)` |
| `memory.recall_top_k` | Y | Y | getattr/str | `astrmai/memory/services/memory_engine.py:843 (+3)` |
| `memory.summary_threshold` | Y | Y | attr | `astrmai/webui/backend/adapters/plugin_api.py:427` |
| `memory.time_decay_rate` | Y | Y | getattr/str | `astrmai/memory/retrieval/hybrid_retriever.py:85 (+1)` |
| `mood.decay_interval` | Y | Y | getattr/str | `astrmai/state/mood/mood_decay.py:35` |
| `mood.decay_rate` | Y | Y | getattr/str | `astrmai/memory/services/memory_maintenance_service.py:70 (+1)` |
| `mood.unknown_decay` | Y | Y | **DEAD** | `-` |
| `performance.summary_threshold` | Y | Y | dotted | `astrmai/webui/backend/adapters/plugin_api.py:427` |
| `persona.component_max_retries` | Y | Y | getattr/str | `astrmai/memory/persona/persona_summarizer.py:222` |
| `persona.include_self_lore_in_prompt` | Y | Y | getattr/str | `astrmai/app/plugin_facade.py:197 (+6)` |
| `persona.persona_id` | Y | Y | attr | `astrmai/conversation/planning/tools/pfc_tools.py:1759 (+7)` |
| `persona.retry_interval_sec` | Y | Y | getattr/str | `astrmai/app/lifecycle.py:73 (+1)` |
| `persona.retry_max_interval_sec` | Y | Y | getattr/str | `astrmai/app/lifecycle.py:74 (+1)` |
| `private_chat.image_analysis_retries` | - | Y | getattr/str | `astrmai/conversation/attention/private_turn_coordinator.py:119 (+1)` |
| `private_chat.image_barrier_timeout_sec` | - | Y | getattr/str | `astrmai/conversation/attention/private_turn_coordinator.py:94` |
| `private_chat.image_resolve_timeout_sec` | - | Y | getattr/str | `astrmai/conversation/attention/private_turn_coordinator.py:106 (+3)` |
| `private_chat.input_settle_sec` | - | Y | getattr/str | `astrmai/conversation/attention/private_turn_coordinator.py:127 (+1)` |
| `private_chat.topic_active_ttl_sec` | Y | Y | getattr/str | `astrmai/conversation/planning/conversation_continuity.py:65` |
| `private_chat.topic_confirm_after_sec` | Y | Y | getattr/str | `astrmai/conversation/planning/conversation_continuity.py:69` |
| `private_chat.topic_confirmation_wait_sec` | Y | Y | getattr/str | `astrmai/conversation/planning/conversation_continuity.py:73` |
| `private_chat.topic_continuity_enabled` | Y | Y | getattr/str | `astrmai/conversation/planning/conversation_continuity.py:61` |
| `private_chat.topic_summary_max_chars` | Y | Y | getattr/str | `astrmai/conversation/planning/conversation_continuity.py:77` |
| `private_chat.turn_merge_enabled` | - | Y | attr | `astrmai/conversation/attention/private_turn_coordinator.py:166 (+3)` |
| `private_chat.wait_timeout_sec` | - | Y | dotted | `astrmai/state/private_chat/private_chat_manager.py:83` |
| `provider.agent_models` | Y | Y | attr | `astrmai/infrastructure/gateway/model_gateway.py:70` |
| `provider.embedding_models` | Y | Y | dotted | `astrmai/memory/services/memory_engine.py:70` |
| `provider.fallback_models` | Y | Y | attr | `astrmai/infrastructure/gateway/model_gateway.py:64` |
| `provider.task_models` | Y | Y | attr | `astrmai/infrastructure/gateway/model_gateway.py:67` |
| `provider.vision_models` | Y | Y | attr | `astrmai/infrastructure/gateway/model_gateway.py:73` |
| `reply.base_frequency` | Y | Y | dotted | `astrmai/state/energy/frequency_controller.py:64 (+1)` |
| `reply.emotion_mapping` | Y | Y | dotted | `astrmai/state/mood/mood_manager.py:54` |
| `reply.enable_content_safety_filter` | Y | Y | **DEAD** | `-` |
| `reply.fallback_text` | Y | Y | attr | `astrmai/learning/evolution_manager.py:81 (+3)` |
| `reply.follow_up_probability` | Y | Y | getattr/str | `astrmai/conversation/planning/planner_side_inputs.py:1394 (+1)` |
| `reply.humanlike_short_reply_enabled` | Y | Y | getattr/str | `astrmai/conversation/reply_shape_policy.py:152` |
| `reply.meme_probability` | Y | Y | dotted | `astrmai/conversation/execution/reply_service.py:66 (+1)` |
| `reply.no_segment_max_len` | Y | Y | dotted | `astrmai/conversation/execution/reply_service.py:65 (+1)` |
| `reply.segment_min_len` | Y | Y | dotted | `astrmai/conversation/execution/reply_service.py:64 (+1)` |
| `reply.short_reply_allow_followup_question` | Y | Y | getattr/str | `astrmai/conversation/reply_shape_policy.py:173` |
| `reply.short_reply_max_chars` | Y | Y | getattr/str | `astrmai/conversation/reply_shape_policy.py:170` |
| `reply.short_reply_max_sentences` | Y | Y | getattr/str | `astrmai/conversation/reply_shape_policy.py:171` |
| `reply.stale_reply_max_age_sec` | - | Y | getattr/str | `astrmai/conversation/execution/reply_freshness.py:23` |
| `reply.typing_speed_factor` | Y | Y | getattr/str | `astrmai/conversation/execution/reply_artifact_builder.py:457` |
| `sys3.computer_agent_sandbox_enabled` | Y | Y | dotted | `astrmai/workmode/subagents/computer_agent.py:26` |
| `sys3.enable_work_mode` | Y | Y | getattr/str | `astrmai/app/plugin_facade.py:209 (+1)` |
| `sys3.max_steps` | Y | Y | getattr/str | `astrmai/app/plugin_facade.py:719 (+6)` |
| `sys3.tool_timeout` | - | Y | getattr/str | `astrmai/app/plugin_facade.py:720` |
| `system1.extra_command_list` | Y | Y | dotted | `astrmai/conversation/ingress/sensors.py:89` |
| `system1.keyword_reactions` | Y | Y | getattr/str | `astrmai/conversation/decision/judge.py:397` |
| `system1.nicknames` | Y | Y | dotted | `astrmai/conversation/ingress/sensors.py:322 (+2)` |
| `system1.wakeup_words` | Y | Y | getattr/str | `astrmai/conversation/decision/judge.py:319` |
| `timing.agent_execution_timeout_sec` | Y | Y | getattr/str | `astrmai/conversation/execution/reply_freshness.py:42` |
| `timing.attention_judge_timeout_sec` | Y | Y | getattr/str | `astrmai/conversation/decision/judge.py:470 (+1)` |
| `timing.cognitive_loop_timeout_sec` | Y | Y | getattr/str | `astrmai/conversation/planning/cognitive_loop.py:137` |
| `timing.compaction_timeout_sec` | Y | Y | getattr/str | `astrmai/conversation/attention/compaction_providers.py:53` |
| `timing.embedding_timeout_sec` | Y | Y | getattr/str | `astrmai/memory/retrieval/embedding.py:18` |
| `timing.fast_mode_execution_timeout_sec` | Y | Y | getattr/str | `astrmai/conversation/execution/executor.py:533` |
| `timing.image_analysis_timeout_sec` | Y | Y | getattr/str | `astrmai/conversation/attention/private_turn_coordinator.py:93` |
| `timing.image_resolve_timeout_sec` | Y | Y | getattr/str | `astrmai/conversation/attention/private_turn_coordinator.py:106 (+3)` |
| `timing.main_reply_reserve_sec` | Y | Y | attr | `astrmai/infrastructure/runtime/turn_call_ledger.py:147 (+2)` |
| `timing.memory_react_timeout_sec` | Y | Y | getattr/str | `astrmai/memory/retrieval/react_retriever.py:36` |
| `timing.model_request_timeout_sec` | Y | Y | getattr/str | `astrmai/conversation/execution/reply_freshness.py:35` |
| `timing.mood_analysis_timeout_sec` | Y | Y | getattr/str | `astrmai/state/mood/mood_manager.py:45` |
| `timing.private_input_settle_sec` | Y | Y | legacy-sync | `config.py:_sync_legacy_timing_aliases → private_chat/sys3` |
| `timing.private_wait_timeout_sec` | Y | Y | legacy-sync | `config.py:_sync_legacy_timing_aliases → private_chat/sys3` |
| `timing.query_rewrite_timeout_sec` | Y | Y | getattr/str | `astrmai/memory/services/memory_retrieval_service.py:799` |
| `timing.reply_max_age_sec` | Y | Y | getattr/str | `astrmai/conversation/execution/reply_freshness.py:23` |
| `timing.turn_merge_enabled` | Y | - | **DEAD** | `astrmai/conversation/attention/private_turn_coordinator.py:166 (+3)` |
| `timing.turn_total_budget_sec` | Y | Y | getattr/str | `astrmai/presentation/events/message_entry.py:152` |
| `timing.vision_barrier_total_timeout_sec` | Y | Y | getattr/str | `astrmai/conversation/attention/private_turn_coordinator.py:82` |
| `timing.workmode_execution_timeout_sec` | Y | Y | legacy-sync | `config.py:_sync_legacy_timing_aliases → private_chat/sys3` |
| `tts.enable_group` | Y | Y | getattr/str | `astrmai/conversation/execution/tts_bridge.py:67` |
| `tts.enable_private` | Y | Y | getattr/str | `astrmai/conversation/execution/tts_bridge.py:66` |
| `tts.enabled` | Y | Y | attr | `astrmai/memory/services/memory_admission_service.py:80` |
| `tts.group_probability` | Y | Y | getattr/str | `astrmai/conversation/execution/tts_bridge.py:71` |
| `tts.group_require_direct_trigger` | Y | Y | getattr/str | `astrmai/conversation/execution/tts_bridge.py:69` |
| `tts.max_text_length` | Y | Y | getattr/str | `astrmai/conversation/execution/tts_bridge.py:62` |
| `tts.min_text_length` | Y | Y | getattr/str | `astrmai/conversation/execution/tts_bridge.py:61` |
| `tts.plugin_name` | Y | Y | getattr/str | `astrmai/conversation/execution/tts_bridge.py:86` |
| `tts.send_text_with_audio` | Y | Y | getattr/str | `astrmai/conversation/execution/tts_bridge.py:82` |
| `tts.silent_on_failure` | Y | Y | getattr/str | `astrmai/conversation/execution/tts_bridge.py:114` |
| `vision.enable_vision` | Y | Y | getattr/str | `astrmai/conversation/attention/private_turn_coordinator.py:232 (+3)` |
| `vision.image_analysis_retries` | Y | Y | getattr/str | `astrmai/conversation/attention/private_turn_coordinator.py:119 (+1)` |
| `vision.image_recognition_probability` | Y | Y | getattr/str | `astrmai/conversation/ingress/sensors.py:255` |
| `vision.native_main_reply_failure_cooldown_sec` | Y | Y | getattr/str | `astrmai/conversation/execution/executor.py:348` |
| `vision.use_native_main_reply_vision` | Y | Y | getattr/str | `astrmai/conversation/execution/executor.py:336` |
| `vision.vision_reply_policy` | Y | Y | getattr/str | `astrmai/conversation/attention/private_turn_coordinator.py:71` |

统计：schema 叶子 197；pydantic 字段 205；死配置 9；隐藏配置 0（12 个 pydantic-only 全为 legacy 别名）；默认值直接不一致 0；getattr fallback 漂移 11。
