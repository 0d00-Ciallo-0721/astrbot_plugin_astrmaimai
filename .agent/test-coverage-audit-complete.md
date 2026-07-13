# AstrMai 全模块测试覆盖率审计 — 完整报告

> 日期：2026-07-03
> 方式：6 代理并行 × 全 ~237 源文件 × ~120 测试文件
> 输出：每文件测试函数计数 + 覆盖级别 + 缺失场景 + 风险排序

---

## 总览

| 模块 | 源文件 | NONE | MINIMAL | PARTIAL | ADEQUATE |
|------|:------:|:----:|:-------:|:-------:|:--------:|
| conversation | 55 | 8 | 15 | 12 | 20 |
| memory | 39 | 12 | 7 | 7 | 10 |
| infrastructure | 42 | 11 | ~5 | ~18 | ~15 |
| state | 14 | 2 | 3 | 3 | 6 |
| learning | 20 | 5 | 9 | 5 | 1 |
| proactive | 13 | 5 | 3 | 4 | 1 |
| multimodal | 6 | 4 | 0 | 2 | 0 |
| presentation | 10 | 6 | 1 | 3 | 0 |
| shared | 7 | 5 | 0 | 2 | 0 |
| webui | ~20 | 0 | 1 | ~6 | ~13 |
| workmode | 6 | 1 | 1 | 4 | 0 |
| app | 5 | 2 | 1 | 2 | 0 |
| **合计** | **~237** | **~61** | **~46** | **~68** | **~66** |

**~45% 的源文件仅有 0-2 个测试。仅 ~28% 达到 ADEQUATE 级别。**

---

## 风险排名 Top 10（行数 × 覆盖缺口）

| # | 模块 | 文件 | 行数 | 级别 | 风险分 | 缺失的核心场景 |
|---|------|------|:---:|:----:|:-----:|------|
| 1 | proactive | `heartflow/manager.py` | 1095 | MINIMAL | **876** | tick_chat 全部决策类型、session 生命周期、topic_heat 计算、visible candidate 评分 |
| 2 | infrastructure | `gateway/gateway_lane.py` | 672 | MINIMAL | **571** | chat_in_lane_result 完整管道、tool_chat 重试、lane_request_kwargs |
| 3 | proactive | `proactive_task.py` | 812 | PARTIAL | **406** | 完整维护循环（decay→diary→dream→review）、错误恢复、stop 取消 |
| 4 | memory | `persona/persona_summarizer.py` | 703 | NONE | **374** | 全部 20 个方法零测试——包括 9 个 shard summarizer |
| 5 | infrastructure | `runtime/observability.py` | 262 | NONE | **262** | 全部 8 个公开方法零测试 |
| 6 | memory | `memory_engine.py` | 980 | MINIMAL | **214** | 47 个方法仅 2 测试——add_memory, search, recall, FAISS 全部未测 |
| 7 | infrastructure | `gateway/gateway_tasks.py` | 421 | MINIMAL | **253** | _task_thread_pool, _task_dispatch, 串行/并行执行分支 |
| 8 | infrastructure | `gateway/gateway_call.py` | 383 | MINIMAL | **230** | _elastic_call 池分发、重试+退避、image payload 组装 |
| 9 | memory | `services/topic_summarizer.py` | 407 | NONE | **161** | 全部 15 个方法零测试——segment_by_silence, detect_topic_shift, batch_summarize |
| 10 | memory | `services/memory_observer.py` | 315 | NONE | **158** | 全部 12 个方法零测试——record, snapshot, recent_events, reset |

---

## 第 1 章：conversation/ 模块

**源文件** 55 · **测试文件** ~30 · **覆盖统计：NONE: 8, MINIMAL: 15, PARTIAL: 12, ADEQUATE: 20**

### 1.1 NONE 覆盖（8 文件）

| 文件 | 行数 | 风险 | 缺失场景 |
|------|:---:|:---:|------|
| `attention/compaction_providers.py` | 280 | 中 | `_resolve_provider_candidates` 未隔离测试；`_render_compaction_envelope` 的 gateway 缺失；`_build_summary_with_provider_v2` 多 provider fallback |
| `attention/perception.py` | 55 | 低 | `PerceptionBuilder.build()` 从未测试：正常事件、带图事件、缺失 get_group_id、rich_text extra、is_strong_wakeup 组合 |
| `attention/topic_units.py` | 46 | 低 | `TopicUnit` 数据类从未单元测试；`ColdSummaryStructure.section_counts()` 空 sections；SECTION_ORDER 完整性 |
| `ingress/command_guard.py` | 14 | 低 | `check_framework_command()` 从未测试 |
| `ingress/dedupe.py` | 38 | 低 | `check_message_dedup()` 从未测试；`build_message_signature_text()` 空消息；TTL 过期逻辑；跨聊天去重 |
| `ingress/permission_guard.py` | 27 | 低 | `check_message_scope_access()` 从未测试：白名单、管理绕过、私聊禁用/启用、空白名单 |
| `planning/behavior_tuning.py` | 177 | 高 | 全部方法零测试：`apply()` 的 18 个 risk flags、pushback + confidence、UNCERTAIN_FLAGS、`_looks_like_short_ambient` |
| `planning/message_renderer.py` | 60 | 中 | 全部 4 个 render 方法零测试：空字符串、None、unicode 名、XML 字符、超长文本、旧 XML 缺失标签 |

### 1.2 MINIMAL 覆盖（15 文件）

| 文件 | 行数 | 测试数 | 缺失场景 |
|------|:---:|:-----:|------|
| `attention/decision_router.py` | 110 | 2 | 自定义 mood 配置；`route()` 未知 action；并发路由调用；mood_adjust 零/负值 |
| `attention/event_normalizer.py` | 92 | 1 | `normalize_events()` 空列表、缺失 message_obj、重复事件、混私聊+群聊事件 |
| `attention/window_buffer.py` | 73 | 2 | 缓冲区满时 add；get_recent 空缓冲；monotonic 时钟推进间 TTL 过期；并发 add+get |
| `execution/reply_freshness.py` | 137 | 2 | `check_freshness()` 从未单元测试：过期 TTL、未来时间戳、reply mode 转换；compute_freshness_budget 从未测试 |
| `ingress/external_result_bridge.py` | 64 | 1 | kernel 为 None 时注入；message text 为空；缺失 unified_msg_origin |
| `contracts/message_scope.py` | 7 | 1 | from_event 私聊事件；缺失 unified_msg_origin |
| `planning/prompt_builder.py` | 39 | 1 | 仅 1 测试 |
| `planning/goal_service.py` | 220 | 2 | 仅 2 测试 |
| `planning/agency_feedback_bridge.py` | 106 | 2 | 仅 2 测试 |
| `planning/agency_runtime.py` | 93 | 3 | 仅 3 测试 |
| `decision/action_plan.py` | 20 | 1 | `BrainActionPlan.should_act()` 仅通过 ORM 测试检查 |
| `loop/models.py` | 100 | 3 | 仅 3 测试 |
| `loop/state_store.py` | 42 | 2 | 仅 2 测试 |

### 1.3 PARTIAL 覆盖（12 文件）

| 文件 | 行数 | 测试数 | 缺失场景 |
|------|:---:|:-----:|------|
| `attention/focus_selector.py` | 88 | ~6 | 仅环境事件（无 focus thread）；全部 bot 自消息 |
| `attention/thread_builder.py` | 246 | ~6 | 空事件列表构建；混合图+文事件；畸形事件无 message_str |
| `attention/vision_binding.py` | 60 | ~4 | 无效 URL；视觉服务不可用时降级；多 URL；混合直接+提取 URL |
| `execution/reply_post_send.py` | 256 | ~5 | 合成/工具生成事件；memory engine 为 None 时 feed；proactive 事件 |
| `execution/text_segmenter.py` | 236 | 4 | 中文文本；混排 CJK+ASCII；空输入；超长单段；max_segments 强制；并发 |
| `execution/system2_runner.py` | 62 | ~5 | queue 耗尽；execute 异常；focus_context=None；expired wait 时 run_private_followup |
| `execution/outbound_error_policy.py` | 54 | 3 | alert service 为 None；rate-limit 错误模式；未知错误类型 |
| `ingress/poke_handler.py` | 26 | ~3 | poke 目标为 bot 自身；畸形 poke 事件缺失 sender；管理员在受限群 |
| `loop/scheduler_benchmark.py` | 471 | 3 | 仅 3 测试 |
| `planning/expression_policy.py` | 620 | 4 | 仅 4 测试 |
| `planning/planning_input_loader.py` | 448 | 5 | 仅 5 测试 |
| `contracts/reply_artifact.py` | 38 | ~5 | 超大内容 validate；空 segments is_sendable；纯空白 clean_text |

### 1.4 ADEQUATE 覆盖（20 文件）

省略详细分析——这些文件有专门测试文件覆盖 happy path + 边界 + 错误路径：
`gate.py`(37+), `context_compaction.py`(26+), `group_dialogue_store.py`(23+), `focus_context.py`(10+), `prompt_envelope.py`(35+), `turn_context.py`(30+), `judge.py`(15+), `executor.py`(30+), `reply_service.py`(20+), `reply_artifact_builder.py`(12+), `followup_manager.py`(7+), `sensors.py`(8+), `chat_loop_kernel.py`(50+), `cognitive_loop.py`(28+), `context_engine.py`(20+), `conversation_continuity.py`(10+), `planner.py`(30+), `planner_prompt_context.py`(6+), `planner_side_inputs.py`(21+), `prompt_refiner.py`(30+), `think_level_policy.py`(12+), `pfc_tools.py`(10+)

---

## 第 2 章：memory/ 模块

**源文件** 39 · **测试文件** ~14 · **覆盖统计：NONE: 12, MINIMAL: 7, PARTIAL: 7, ADEQUATE: 10**

### 2.1 NONE 覆盖（12 文件）

| 文件 | 大小 | 方法数 | 风险 | 缺失场景 |
|------|:---:|:-----:|:---:|------|
| `persona/persona_summarizer.py` | 37.4KB | 20 | 🔴374 | 全部方法零测试——9 个 shard summarizer、_build_first_person_rewrite、_generate_all_shards_background、_persist_cache、并发 get_summary |
| `services/topic_summarizer.py` | 16.1KB | 15 | 🔴161 | 全部方法零测试——process_history、_segment_by_silence、_detect_topic_shift、_batch_summarize、_parse_summaries |
| `services/memory_observer.py` | 15.8KB | 12 | 🔴158 | 全部方法零测试——record、runtime_snapshot、chat_snapshot、recent_events、reset |
| `retrieval/embedding.py` | 5.3KB | 4 | 🟡53 | get_vector provider 返回 list-of-lists、_find_provider meta=None、cosine_similarity 不同长度 |
| `retrieval/bm25.py` | 5.0KB | 4 | 🟡50 | FTS5 查询特殊操作符、doc 与 FTS 索引竞态、全部同分归一化、空内容 add_document |
| `services/jargon_retrieval_policy.py` | 5.0KB | 6 | 🟡50 | CJK n-gram 上限 48、null/empty metadata、空查询、trace dict 跨协程竞态 |
| `services/expression_pattern_retrieval_policy.py` | 3.7KB | ~5 | 🟡37 | 零 tokens _match_score、纯标点 _tokens、空 shared_scope、think_level 过滤 |
| `retrieval/vector_store.py` | 3.3KB | 3 | 🟡33 | Faiss result 缺 id/text/metadata、空 result 列表 |
| `services/memory_context_builder.py` | 3.1KB | 4 | 🟡31 | 空 candidate 列表、budget 中途耗尽、guidance 提取、max_chars 边界 |
| `contracts/observability.py` | 1.6KB | — | 低 | 纯数据类 |
| `services/claim_rules.py` | 487B | — | 低 | 微小规则定义 |
| `dream/dream_maintenance.py` | 280B | — | 低 | 琐碎包装器 |

### 2.2 MINIMAL 覆盖（7 文件）

| 文件 | 大小 | 测试数 | 风险 | 缺失场景 |
|------|:---:|:-----:|:---:|------|
| `services/memory_engine.py` | 42.8KB | 2 | 🔴214 | 47 方法仅 2 测试——add_memory, search_memories, recall, query, 全部 legacy import, FAISS 初始化, cognitive feedback, persona lore |
| `services/expression_pattern_service.py` | 18.1KB | 2 | 🟠90 | 20 方法仅 2 测试——get_pattern, list_patterns, update_review, adjust_weight, 全部 CRUD 边界 |
| `services/memory_processor.py` | 9.3KB | 2 | 🟠47 | 仅 prompt 模板和 lane scoping 测试——核心 process_conversation 未隔离测试 |
| `dream/promotion_engine.py` | 8.6KB | 2 | 🟠43 | detected_facts 路径、authority_override 始终触发、canonical_source_ids 未填充 |
| `dream/dream_generator.py` | 8.1KB | 1 | 🟠40 | generate() 空 dream_log、None style、build_maintenance_result [fact] 标签不匹配 |
| `retrieval/hybrid_retriever.py` | 4.3KB | 1 | 🟡22 | add_memory vector=None 直接 RuntimeError、search 双通道异常 |
| `contracts/memory_query.py` | 5.2KB | ~2 | 低 | 间接测试 |

### 2.3 PARTIAL 覆盖（7 文件）

| 文件 | 大小 | 测试数 | 缺失场景 |
|------|:---:|:-----:|------|
| `dream/dream_agent.py` | 23.7KB | 3 | run_dream_cycle 入口未测试、_execute_tool、全部 tool 方法、_parse_action、_get_seed_events |
| `retrieval/react_retriever.py` | 17.1KB | 5 | retrieve() 完整 ReAct 循环、planner 畸形 tool call、max steps 超限、memory engine recall 失败 |
| `services/session_memory_summarizer.py` | 14.6KB | 4 | process_history 路径、claim 提取失败、冲突解决失败、认知反馈写入 |
| `services/memory_injection_service.py` | 11.0KB | 4 | think_level=0 和 think_level=3+、prompt 注入超大 prompt、has_memory_intent 边界 |
| `services/memory_index_projector.py` | 11.1KB | 4 | rebuild_all、rebuild_session、cleanup_deleted、一致性检查边界 |
| `services/memory_scoring.py` | 4.5KB | 4 | compute_hot_score 全零时间戳、compute_temporal_boost 零 created_at、scoring_from_config 部分属性 |
| `services/summarizer.py` | 8.6KB | 5 | compat 导出、管道转发 |

### 2.4 ADEQUATE 覆盖（10 文件）

`v2_store.py`(25), `memory_retrieval_service.py`(15), `memory_turn_pipeline.py`(10), `instant_memory_gate.py`(9), `memory_claim_service.py`(8), `memory_tool_service.py`(7), `memory_maintenance_service.py`(5), `memory_write_service.py`(5+), `memory_migration_service.py`(7), `claim_rules_zh.py`(4)

---

## 第 3 章：infrastructure/ 模块

**源文件** 42 · **测试文件** ~18 · **覆盖统计：NONE: 11, MINIMAL: ~5, PARTIAL: ~18, ADEQUATE: ~15**

### 3.1 NONE 覆盖（11 文件）

| 文件 | 行数 | 风险 | 缺失场景 |
|------|:---:|:---:|------|
| `runtime/observability.py` | 262 | 🔴262 | 全部 8 公开方法零测试——RuntimeObservabilityHub: record(domain 校验, level 归一化), recent(过滤), recent_errors, global_snapshot(聚合), catalog_snapshot, search(q+tags), format_timeline_item, _append_trace_event(raw_store 集成) |
| `runtime/lane_transcript.py` | 71 | 🟢 | get_recent_transcript max_age_seconds 过滤、social rendered line 检测、bot_name fallback |
| `runtime/trace_runtime.py` | 88 | 🟢 | new_trace_id 唯一性、preview_text 截断、ensure_trace_id 生成、append_trace_stage 字段排除、debug_trace 字段预览 |
| `runtime/turn_trace_store.py` | 84 | 🟢 | append/recent 同 raw_trace_store 模式 |
| `runtime/context_economy_benchmark_store.py` | 83 | 🟢 | seed 写/读、replay cursor 推进 |
| `persistence/database_cron.py` | 44 | 🟢 | save/load/deactivate CRUD、upsert vs insert 分支 |
| `persistence/database_profile_relation.py` | 174 | 🟠 | 关系图 CRUD、多群隔离、user_id 原始查询 SQL 注入 |
| `persistence/persona_cache.py` | 37 | 🟢 | 缓存键生成、TTL 过期、缓存未命中→加载路径 |
| `security/input_sanitizer.py` | 18 | 🟢 | sanitize/sanitize_memory 均未测试（委托包装器，但无契约测试） |
| `security/output_guard.py` | 23 | 🟢 | 重导出模块——函数通过 gateway/output_guard 测试，但重导出表面本身未测试 |
| `gateway/gateway_exceptions.py` | 23 | 🟢 | LLMCascadeFailureException 属性 |

### 3.2 MINIMAL 覆盖（5 文件）

| 文件 | 行数 | 风险 | 缺失场景 |
|------|:---:|:---:|------|
| `gateway/gateway_lane.py` | 672 | 🔴571 | chat_in_lane_result 完整管道（UMO→conversation→elastic_call→artifact→trace）；tool_chat_in_lane_result terminal_yield/wait_signal 子路径；_record_event_request_trace hash 复用；_lane_request_kwargs session_id/cache_control 工厂 |
| `gateway/gateway_call.py` | 383 | 🔴230 | _elastic_call 池分发；重试+退避循环；image payload 组装；JSON parse fallback |
| `gateway/gateway_tasks.py` | 421 | 🔴253 | _task_thread_pool；_task_dispatch；user/assistant/summary turn 渲染；串行/并行执行分支 |
| `runtime/lane_storage.py` | 263 | 🔴224 | ensure_lane 完整管道（lock→create→load→normalize→compact→save）；append_exchange 双 turn；append_visible_reply_artifact blocked 分支；_get_lane_creation_lock 200 上限 |
| `gateway/model_router.py` | 215 | 🟠 | 健康评分生命周期（report_success +5→+10 上限；report_failure -2/-4；下限 -10）；sticky_key 偏好解析；池修剪（>50 池, >24h）；consecutive_failures 跟踪（死字段） |

### 3.3 PARTIAL 覆盖（关键文件）

| 文件 | 行数 | 关键缺失 |
|------|:---:|------|
| `gateway/output_guard.py` | 273 | 仅 validate_visible_output_text 测试。缺失：looks_like_harmful_content, looks_like_provider_failure_text, looks_like_prompt_scaffold_text, is_noise_line, sanitize_visible_reply_text, is_safe_visible_text, is_sendable_segment |
| `gateway/gateway_policy.py` | 161 | _build_attempt_queue fallback splice；_filter_cooldown_attempt_queue；_open_model_cooldown 全部 3 分支；_is_fatal_failure 分类 |
| `gateway/gateway_result.py` | 226 | _enrich_cache_debug_meta 全部 4 条件；_build_cache_observation 吞吐 guard；_elapsed_s 格式化 |
| `gateway/provider_capabilities.py` | 43 | infer_provider_capabilities 仅间接测试——缺失空/None→"unknown"；Dify/Coze/DashScope/Bailian→"runner" |
| `context_economy/prompt_templates.py` | 609 | 19 模板仅 ~3 使用——缺失 payload 键渲染；template_registry.get() |
| `context_economy/token_estimator.py` | 15 | CJK 范围仅 \u4e00-\u9fff——缺失 \u3400-\u4dbf, \uf900-\ufaff, 日文假名, 韩文, 全角 ASCII |
| `runtime/event_bus.py` | 212 | 仅 worker 重启测试——缺失 subscribe/unsubscribe weakref 生命周期；publish nowait 溢出→dropped_count；MPSC 队列满；worker_loop 回调异常；_worker_health_check 自动重启 |
| `runtime/lane_history.py` | 209 | _sanitize_dialog_message 角色分支；_build_rolling_summary 截断；_compact_history summary_only vs full vs sys2 dialog 模式；build_history_turn 全部角色 |
| `persistence/database_jargon.py` | 363 | 部分测试——缺失 save_entry 参数未消毒；批量操作 |
| `persistence/database_memory.py` | 116 | memory node 冲突解决；get_memory_fragments 过滤 |
| `persistence/persistence_schema.py` | 408 | 迁移链完整性：36 迁移中仅 2 测试；迁移失败回滚；并发迁移 guard |
| `persistence/state_profile_persistence.py` | 258 | 9 测试——相对充分 |
| `persistence/sqlite_helpers.py` | 22 | busy_timeout 传播；异常时上下文管理器清理 |
| 全部 4 个 `repositories/*.py` | ~140 | 基本委托通过 database_adapters 测试——缺失每个仓库的专用边界测试 |

---

## 第 4 章：state/ 模块

**源文件** 14 · **测试文件** ~12 · **覆盖统计：NONE: 2, MINIMAL: 3, PARTIAL: 3, ADEQUATE: 6**

### 4.1 NONE 覆盖（2 文件）

| 文件 | 行数 | 缺失场景 |
|------|:---:|------|
| `contracts/profile_summary.py` | ~30 | 纯数据类 from_profile——低风险 |
| `contracts/wait_state.py` | ~25 | 纯数据类 from_mapping——低风险 |

### 4.2 MINIMAL 覆盖（3 文件）

| 文件 | 行数 | 风险 | 测试数 | 缺失场景 |
|------|:---:|:---:|:-----:|------|
| `energy/energy_manager.py` | ~50 | 🔴高 | 2 | 仅 extreme 低能量测试——缺失：energy≥0.5（应返回 False）、energy=0.3（中概率 ~0.5）、energy 精确在 min_threshold、random=0.99（应不 drop）、get_reply_cost 显式 amount |
| `mood/mood_decay.py` | ~55 | 🟠中 | 3 | 仅 epoch=0 guard 和能量恢复测试——缺失：多次衰减步骤、负情绪衰减、能量恢复 anchor fallback（last_energy_recovery_time=0→last_reply_time）、decay_interval=0 |
| `private_chat/private_chat_manager.py` | ~200 | 🔴高 | 2 | wait_for_new_message 真实超时、signal_new_message 中断等待、close_session、cleanup_stale_sessions、MAX_SESSIONS 淘汰、KV 持久化——核心私聊交互循环完全未测 |

### 4.3 PARTIAL 覆盖（3 文件）

| 文件 | 行数 | 测试数 | 缺失场景 |
|------|:---:|:-----:|------|
| `mood/mood_manager.py` | ~250 | 11 | LLM 超时路径（asyncio.wait_for 30s→TimeoutError）、空文本（<2 字符 guard）、超长文本（无截断→LLM OOM）、AST literal_eval fallback、正则提取 fallback、call_mood_task（非 lane）路径、JSON list-of-dict 解析 |
| `relationship/affection_router.py` | ~100 | 3 | 空 history/window、单用户（threshold=0.0）、空 total_scores、publish_change()、_extract_info 全部非 AstrMessageEvent 类型、全部长度 _calculate_mqs 边界 |
| `group_wait/group_reply_wait_manager.py` | ~270 | 4 | register_from_reply_event wait_targets（仅 direct_wakeup 测试）、handle_incoming_message EXPIRED_TIMEOUT、OBSERVED_TARGET（目标消息但非线程恢复）、非群组（无 group_id 返回 NONE）、cancel_wait、线程签名恢复匹配 |

### 4.4 ADEQUATE 覆盖（6 文件）

| 文件 | 行数 | 测试数 | 仍缺失 |
|------|:---:|:-----:|------|
| `relationship/relationship_engine.py` | ~590 | ~20 | 14 事件类型中仅 INSULT 和 HELPFUL_REPLY 直接测试——12 类型未作为直接 process_event 调用；get_context() 返回值；apply_global_decay() 多向量；_log_saturation 反向 delta |
| `chat_state_service.py` (ChatStateService) | ~200 | ~12 | 并发访问同 chat_id、CAS mood 更新竞态、锁池修剪（>500 锁）、clear_chat_state、get_active_states、atomic_update_mood absolute_val、_persist_if_dirty 非脏时 |
| `chat_state_service.py` (StateEngine) | ~350 | ~15 | calculate_and_update_affection "angry"/"curious"/"surprise" mood、_resolve_no_send_affection_event_type 组合 attack_confidence+风险标记、refresh_config、on_learning_message_recorded 空 payload、flush_message_counters |
| `user_profile_service.py` | ~580 | 5 | merge_tags 满容量 10 标签、memory_points 多周期衰减（POINT_DECAY=0.85 重复）、categorize_memory_points、build_recent_interaction_summary、get_profile_prompt_bundle_for_user、set_auto_nickname、invalidate_cache、observe_user_activity 重复消息去重 |
| `mood/mood_decay.py` | ~55 | 3 | 见 MINIMAL 节 |
| `energy/frequency_controller.py` | ~120 | 4 | DEPRECATED——充分覆盖 |

---

## 第 5 章：learning/ 模块

**源文件** 20 · **测试文件** ~8 · **覆盖统计：NONE: 5, MINIMAL: 9, PARTIAL: 5, ADEQUATE: 1**

### 5.1 NONE 覆盖（5 文件）

| 文件 | 行数 | 缺失场景 |
|------|:---:|------|
| `profiling/nickname_generator.py` | 47 | choose() 所有 3 参数、build_template_payload、build_prompt、parse_result 有效 JSON/畸形输入 |
| `contracts/review_item.py` | 23 | 纯数据类——低风险 |
| `logging/bot_reply_recorder.py` | 44 | 仅通过 evolution_manager 间接测试——_is_polluted() traceback/JSON 错误/空内容 |
| `mining/expression_miner.py` | 70 | mine() 仅 mock 从未直接测试——_normalize_messages 过滤 `[` 开头→误杀合法消息 |
| `mining/social_relation_miner.py` | 18 | record_affection_fact 全部——但仅 18 行，低风险 |

### 5.2 MINIMAL 覆盖（9 文件）

| 文件 | 行数 | 测试数 | 缺失场景 |
|------|:---:|:-----:|------|
| `review/review_service.py` | 126 | 2 | list_pending_reviews 状态过滤、分页、空结果；submit_review 拒绝路径；DB 更新失败错误处理 |
| `review/jargon_auto_check_task.py` | 198 | 2 | auto_check 阈值 vs review_min_count；_parse_decision revision_needed 路径；拒绝路径附带原因；空 backlog、混合状态 |
| `review/expression_auto_check_task.py` | 122 | 2 | auto_check 拒绝路径；_parse_decision 缺失字段；tracker 集成——revision_needed 时 queue_review_request |
| `review/expression_governance_runner.py` | 82 | 2 | start→stop 生命周期；_run_loop 表达+黑话交错；空活跃状态；循环错误恢复（单次失败后继续） |
| `mining/expression_pattern_enricher.py` | 78 | 1 | 正常 LLM 响应路径（不仅降级）；超时处理；缺失字段响应（is_expression, style） |
| `mining/jargon_candidate_extractor.py` | 91 | 1 | 边界：精确 min_count；count=1 排除；多字符边界过滤；非 CJK 噪声词 |
| `mining/jargon_miner.py` | 55 | 2 | mine() 返回非空候选；_existing_terms() 查找失败 |
| `contracts/learning_events.py` | 24 | 0 | 数据类实例化仅在 event_collaboration 测试中使用 |
| `logging/bot_reply_recorder.py` | 44 | 0 | 记录为 NONE——仅间接覆盖 |

### 5.3 PARTIAL 覆盖（5 文件）

| 文件 | 行数 | 测试数 | 缺失场景 |
|------|:---:|:-----:|------|
| `evolution_manager.py` | 336 | 12 | record_user_message 并发锁竞争；process_logs_and_mine 挖掘返回模式时；_get_mining_lock 复用；refresh_config 部分更新；非群组 unified_msg_origin |
| `review/reflector.py` | 272 | 3 | record_usage() 从未测试；reflect_batch() LLM 成功路径（≥9/≤2 分路径）；pending_scope_ids()；_pending_reflections 溢出 200 上限；_parse_scores 畸形 JSON |
| `review/reflect_tracker.py` | 189 | 3 | try_consume_feedback 非管理员拒绝；_parse_feedback LLM fallback 路径；queue_review_request 缺失 id；TOCTOU 双重处理 guard |
| `mining/expression_candidate_extractor.py` | 122 | 2 | 大消息批次（>50）；全部低于 min_count；混标点变体；并发 extract() |
| `mining/jargon_enricher.py` | 76 | 2 | enrich 有效 LLM 响应含非 active review_status；_normalize_review_status 边界；大批次（>10 候选） |

---

## 第 6 章：proactive/ 模块

**源文件** 13 · **测试文件** ~7 · **覆盖统计：NONE: 5, MINIMAL: 3, PARTIAL: 4, ADEQUATE: 1**

### 6.1 NONE 覆盖（5 文件）

| 文件 | 行数 | 风险 | 缺失场景 |
|------|:---:|:---:|------|
| `diary_service.py` | 82 | 🔴 | run_once 全部路径零测试——persona cache、session_summarizer、memory_engine、cognitive_feedback、should_run 边界小时 |
| `group_signin_service.py` | 137 | 🔴 | run_once 全部路径零测试——sign-in 周期、_extract_group_id chat_id 格式、_within_sign_window 精确时间、dispatch 决策处理 |
| `heartflow/feedback_bridge.py` | 97 | 🟠 | record_heartflow_pulse、bridge_impulse_to_cognitive_feedback、record_action_outcome、memory_engine None fallback |
| `heartflow/models.py` | 97 | 🟠 | 全部数据类——字段默认值、不可变性、replace() 行为 |
| `heartflow/topic_digest_service.py` | 151 | 🟠 | digest_topic、_build_topic_query、LLM 响应解析、_extract_keywords、记忆检索集成 |

### 6.2 MINIMAL 覆盖（3 文件）

| 文件 | 行数 | 风险 | 测试数 | 缺失场景 |
|------|:---:|:---:|:-----:|------|
| `heartflow/manager.py` | 1095 | 🔴876 | 5 | **最高风险文件**——tick_chat visible candidate 评分（VISIBLE_CANDIDATE_* 阈值）、_evaluate_impulse 全部决策类型、_select_action dispatcher 绑定 intents、session 生命周期、topic_heat 计算、_build_visible_topic_candidate guidance 生成、_prune_expired 清理、信号量获取失败 |
| `dream_scheduler.py` | 204 | 🟠 | 2 | run_once 完整 dream 循环、_within_dream_time_range 复杂范围（跨夜 23:00-02:00）、_run_for_session 可见 dream 发送、promotion engine 集成、_maintenance_guidance 复合标签 |
| `rhythm.py` | 115 | 🟠 | 0 | evaluate_proactive_rhythm 午夜边界、全部 4 time_buckets、config=None 默认 fallback、_normalize_ranges _MISSING/None/空列表/无效字符串、_in_range 跨夜范围、threshold() 计算 |

### 6.3 PARTIAL 覆盖（4 文件）

| 文件 | 行数 | 测试数 | 缺失场景 |
|------|:---:|:-----:|------|
| `proactive_task.py` | 812 | 15 | _run_maintenance_cycle 完整编排（decay→diary→dream→review→heartflow→group_signin）、stop 取消+清理、错误恢复（一个子服务异常，其他继续）、_run_chat_heartbeat_pass 0 活跃聊天 |
| `dispatcher.py` | 311 | 8 | list_intents >HISTORY_LIMIT 淘汰、重复 intent_id、complete 未知 intent_id、complete 同一 intent 调用两次、完成回调异常、并发 dispatch+complete 竞态、executor_pending 负数 |
| `wakeup_service.py` | 269 | 7 | run_once 多活跃聊天（批量循环）、build_signal last_reply_time=None、generate_opening_line 空 persona cache、run_for_chat dispatcher 阻塞 intent 时 |
| `review_dispatcher.py` | 39 | 2 | 通过 ported tests 覆盖——对大小来说充分 |

---

## 第 7 章：multimodal/ 模块

**源文件** 6 · **测试** 3 函数 · **估计覆盖率 ~10%**

| 文件 | 覆盖 | 缺失 |
|------|:----:|------|
| `image_pipeline.py` | NONE | transform_gif: 空 GIF (0 帧)、单帧 GIF、全部低于相似度阈值、非 GIF 格式、max_frames 边界、1px 高帧、base64 解码失败；prepare_image: 无效 base64、未知格式、GIF/webp 分支；cleanup: 文件已删除、权限拒绝 |
| `visual_cortex.py` | ~10% | 仅 1 个 happy-path 测试——缺失 queue overflow、worker 异常处理、缓存命中路径、gateway vision 任务失败 |
| `meme/meme_sender.py` | ~20% | emotion_path 非目录、空目录、概率边界 0/100/101、context=None 路径、损坏图片文件 |
| `meme/meme_init.py` | NONE | 磁盘满/权限拒绝 |
| `meme/meme_config.py` | N/A | 常量 |

---

## 第 8 章：presentation/ 模块

**源文件** 10 · **测试** 4 函数 · **估计覆盖率 ~15%**

| 文件 | 覆盖 | 缺失 |
|------|:----:|------|
| `events/message_entry.py` | NONE | **零测试——主入口管道**：全部 9 条代码路径（poke、framework_command、permission_guard、group_wait、reflect_feedback、attention dispatch 错误→fallback、ghost message 抑制） |
| `events/error_interceptor.py` | NONE | runtime=None、拦截期间错误 |
| `events/result_sniffer.py` | NONE | bridge 返回 None |
| `events/startup_hooks.py` | NONE | lifecycle_manager 失败 |
| `dto/message_scope.py` | ~25% | 仅 re-export 测试——缺失 from_event 非标准事件（无 get_sender_id、get_group_id 异常）、is_anonymous_sender 检测 |
| `dto/command_models.py` | NONE | 纯数据类 |
| `commands/*.py` (4 文件) | ~15% | 仅 parse work command 测试——缺失全部 error 路径、畸形输入 |

---

## 第 9 章：shared/ 模块

**源文件** 7 · **测试** 16 函数 · **估计覆盖率 ~35%**

| 文件 | 覆盖 | 缺失 |
|------|:----:|------|
| `helpers/plugin_helpers.py` | ~15% | 仅 safe_create_task 测试——缺失 resolve_event_scope 畸形 UMO（1-part、2-part、4+part）、is_direct_call_event 无 message_obj/空 message/At 组件 qq=""、extract_result_text 混合类型链、cleanup_stale_focus_pools attention_gate=None、collect_background_tasks 空 |
| `helpers/event_utils.py` | NONE | safe_get_sender_id get_sender_id 异常、fallback attr、fallback 也缺失——通过 handle_global_message 调用 |
| `helpers/text_utils.py` | NONE | normalize_text 非字符串、non_empty_text 空字符串 |
| `helpers/time_utils.py` | NONE | now > 0 基本检查 |
| `constants/defaults.py` | ~70% | 充分测试 |
| `contracts/service_protocols.py` | NONE | 仅结构化，无逻辑 |
| `exceptions.py` | NONE | 琐碎 |

---

## 第 10 章：webui/ 模块

**源文件** ~20 · **测试** ~78+ 函数 · **估计覆盖率 ~35%**

| 区域 | 覆盖 | 缺失 |
|------|:----:|------|
| `backend/services/dashboard_service.py` | ~15% | get_snapshot 下游服务全部失败、psutil 未安装 (ImportError)、db_path 不存在、_repo.snapshot_counts 异常、部分快照 |
| `backend/services/memory_ui_service.py` | ~25% | offset > total_results、空搜索、无效 memory_id、Unicode 搜索 |
| `backend/adapters/plugin_api.py` | ~20% | 仅默认路径+d 不 fallback 测试 |
| `plugin_pages.py` | ~30% | _body 畸形 JSON（语法错误、超大 payload、非 dict 根）、重复路由注册、quart_request=None、_werkzeug_path_alias 嵌套括号 |
| `backend/routes/*.py` (13 文件) | ~5% | 单个路由处理器——DB 查询异常、畸形请求参数——几乎零直接覆盖 |
| `backend/services/*.py` (15 文件) | ~30% | 大部分通过集成测试间接覆盖 |

---

## 第 11 章：workmode/ 模块

**源文件** 6 · **测试** 10 函数 · **估计覆盖率 ~25%**

| 文件 | 覆盖 | 缺失 |
|------|:----:|------|
| `cron_guard/heartbeat.py` | ~15% | 仅 2 测试——缺失 reload_all_lost_jobs cron_manager=None/快照=None/无效 payload JSON/_call_add_job TypeError fallback；run_heartbeat 循环取消/全部活跃/心跳 tick 异常；_sync_revived_snapshot ID 替换 |
| `router.py` | ~30% | 仅 agent 暴露测试——缺失 get_all_agents 空静态+空动态、get_light_tools 空、get_full_tools_for_direct_entry、get_cron_service 无 cron agent、describe_status 边界 |
| `subagents/computer_agent.py` | ~10% | sandbox_enabled=False 路径零测试——空 ToolSet + _get_decline_reason；sandbox enabled 但 tools 不可用 (ImportError) |
| `subagents/base_agent.py` | ~20% | call ctx=None/event=None RuntimeError（已防御但未测试） |
| `subagents/cron_agent.py` | ~10% | 间接——缺失 _call_add_job 双重 try 掩盖真实错误 |
| `tools/handoff_registry.py` | ~40% | 已测 removal+duplicate |

---

## 第 12 章：app/ 模块

**源文件** 5 · **测试** ~3 函数 · **估计覆盖率 ~3% — 最差模块**

| 文件 | 覆盖 | 缺失 |
|------|:----:|------|
| `plugin_facade.py` + `lifecycle.py` | ~0% | **~975 行零直接测试**——terminate shutdown 序列（#1 生产风险）；_terminate_impl 9 步关闭；apply_hot_config 回滚路径；enter_sys3_direct work_mode 禁用/空 task_query/无 agent models；_system2_entry 死代码路径 |
| `lifecycle.py` | NONE | on_program_start 多服务失败序列；start_background_services 无健康检查；_handle_task_result CancelledError；_terminate_impl 部分失败 |
| `runtime_context.py` | ~5% | ~50 property 在 bootstrap 完成前返回 None；build_capability_overview_sync 全部下游异常无保护；export_legacy_attrs 部分失败 |
| `bootstrap.py` | ~10% | build() 零行为测试（仅 import 检查）；_wire_memory_database_services cache_dir=None→TypeError |
| `runtime_facade_protocol.py` | N/A | Protocol 仅类型提示 |

---

## 第 13 章：修复优先级矩阵

### 🔴 P0 — 上线前必须补测（崩溃/挂起风险）

| # | 文件 | 原因 |
|---|------|------|
| 1 | `app/plugin_facade.py` + `lifecycle.py` | terminate shutdown 零测试——生产 #1 风险 |
| 2 | `memory/dream_agent.py:242` | _resolve_canonical_ids 不存在→crash |
| 3 | `memory/embedding.py:102` | provider.meta=None→crash |
| 4 | `multimodal/visual_cortex.py:48-57` | task_done 异常路径不调用→hang |

### 🟡 P1 — 补 NONE 覆盖的核心文件

| # | 文件 | 原因 |
|---|------|------|
| 5 | `memory/persona_summarizer.py` | 703 行，20 方法，零测试 |
| 6 | `memory/topic_summarizer.py` | 407 行，15 方法，零测试 |
| 7 | `proactive/heartflow/manager.py` | 1095 行，仅 5 测试 |
| 8 | `infrastructure/gateway_lane.py` | 672 行，完整管道未测试 |
| 9 | `proactive/group_signin_service.py` | 137 行，零测试 |
| 10 | `proactive/diary_service.py` | 82 行，零测试 |
| 11 | `conversation/behavior_tuning.py` | 177 行，零测试 |
| 12 | `presentation/events/message_entry.py` | 主入口管道，零测试 |

### 🟢 P2 — 补 PARTIAL/MINIMAL 覆盖的场景

| # | 文件 | 缺失场景 |
|---|------|------|
| 13 | `state/energy_manager.py` | 完整概率曲线——仅 extreme 测试 |
| 14 | `state/private_chat_manager.py` | 核心交互循环——仅 2 测试 |
| 15 | `infrastructure/gateway/output_guard.py` | 8/9 函数未测试 |
| 16 | `infrastructure/gateway/model_router.py` | 健康评分生命周期未测试 |
| 17 | `infrastructure/runtime/observability.py` | 全部 8 方法零测试 |
| 18 | `shared/helpers/event_utils.py` | 全部函数零测试 |
| 19 | `workmode/cron_guard/heartbeat.py` | 恢复+心跳循环未测试 |
| 20 | `multimodal/image_pipeline.py` | transform_gif 零测试 |

---

*报告结束。覆盖 12 模块、~237 源文件、~120 测试文件。全部空缺可溯源至具体文件和方法。*
