# AstrMai 测试覆盖审计复审报告

> 复审对象：`.agent/test-coverage-audit-complete.md`（451 行，13 章）
> 复审基线：提交 `5e458e7`
> 验证：`1037 passed, 1 skipped, 1 deselected`
> 测量：Coverage.py 行覆盖率，项目总体 `72.9%`

## 判定口径

- `确认`：报告指出的核心覆盖缺口仍存在。
- `部分成立`：仍有分支未覆盖，但“零测试/覆盖极低/具体数量”等描述已经过期。
- `误判`：现有测试已执行主要路径，或报告描述的方法、风险不存在。
- `低价值`：确实没有直接测试，但文件是数据类、重导出或极薄包装器。

报告原始分类不能作为统计基线：

- memory 分类相加为 36，但报告声称 39 个文件。
- infrastructure 分类相加约为 49，但报告声称 42 个文件。
- `mood_decay.py` 同时列入 MINIMAL 和 ADEQUATE。
- `bot_reply_recorder.py` 同时列入 NONE 和 MINIMAL。
- 测试数量和文件名匹配不能替代真实代码覆盖率。

## 第 1 章：conversation

模块真实行覆盖率：`77.7%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `attention/compaction_providers.py` | 78% | 部分成立；provider fallback 仍可补，不能称 NONE |
| `attention/perception.py` | 100% | 误判 |
| `attention/topic_units.py` | 100% | 误判 |
| `ingress/command_guard.py` | 86% | 部分成立；缺少独立契约测试，但主要路径已执行 |
| `ingress/dedupe.py` | 88% | 部分成立；TTL/跨聊天边界可补 |
| `ingress/permission_guard.py` | 58% | 确认；白名单和管理绕过分支值得补 |
| `planning/behavior_tuning.py` | 66% | 部分成立；有间接覆盖，风险 flags 缺隔离测试 |
| `planning/message_renderer.py` | 85% | 误判“全部方法零测试”；仅剩少量格式边界 |
| `attention/decision_router.py` | 73% | 部分成立 |
| `attention/event_normalizer.py` | 100% | 误判 |
| `attention/window_buffer.py` | 82% | 部分成立；并发/TTL 边界可补 |
| `execution/reply_freshness.py` | 70% | 部分成立 |
| `ingress/external_result_bridge.py` | 82% | 部分成立，优先级低 |
| `contracts/message_scope.py` | 100% | 误判 |
| `planning/prompt_builder.py` | 100% | 过期 |
| `planning/goal_service.py` | 55% | 确认 |
| `planning/agency_feedback_bridge.py` | 83% | 过期 |
| `planning/agency_runtime.py` | 98% | 误判 |
| `decision/action_plan.py` | 100% | 误判 |
| `loop/models.py` | 100% | 误判 |
| `loop/state_store.py` | 87% | 过期 |
| `attention/focus_selector.py` | 65% | 部分成立 |
| `attention/thread_builder.py` | 74% | 部分成立 |
| `attention/vision_binding.py` | 13% | 确认，且报告低估了缺口 |
| `execution/reply_post_send.py` | 77% | 部分成立 |
| `execution/text_segmenter.py` | 81% | 过期；已有中文和混排补缺测试 |
| `execution/system2_runner.py` | 100% | 误判 |
| `execution/outbound_error_policy.py` | 80% | 部分成立 |
| `ingress/poke_handler.py` | 59% | 确认 |
| `loop/scheduler_benchmark.py` | 97% | 误判 |
| `planning/expression_policy.py` | 81% | 过期 |
| `planning/planning_input_loader.py` | 90% | 误判 |
| `contracts/reply_artifact.py` | 100% | 误判 |

原 ADEQUATE 列表也有高估：`pfc_tools.py` 64%、`context_engine.py` 70%、`judge.py` 73%、`executor.py` 73%。其余主要文件约 76%-100%，总体方向成立。

## 第 2 章：memory

模块真实行覆盖率：`65.3%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `persona/persona_summarizer.py` | 48% | 确认缺口，但已有 4 条直接测试，不是零测试 |
| `services/topic_summarizer.py` | 36% | 确认；已有时间戳回归测试，不是零测试 |
| `services/memory_observer.py` | 18% | 确认，优先级高 |
| `retrieval/embedding.py` | 84% | 过期；meta=None 和不同长度已测试 |
| `retrieval/bm25.py` | 90% | 过期；排序正确性已有回归测试 |
| `services/jargon_retrieval_policy.py` | 87% | 误判为 NONE |
| `services/expression_pattern_retrieval_policy.py` | 86% | 误判为 NONE |
| `retrieval/vector_store.py` | 69% | 部分成立；畸形 result 已补测 |
| `services/memory_context_builder.py` | 90% | 过期 |
| `contracts/observability.py` | 100% | 低价值 |
| `services/claim_rules.py` | 100% | 低价值 |
| `dream/dream_maintenance.py` | 琐碎包装 | 低价值 |
| `services/memory_engine.py` | 41% | 确认，优先级高 |
| `services/expression_pattern_service.py` | 67% | 部分成立；已有多条 CRUD/回归测试 |
| `services/memory_processor.py` | 68% | 部分成立；`process_conversation` 已直接执行 |
| `dream/promotion_engine.py` | 87% | 过期；detected facts 回写已修复并测试 |
| `dream/dream_generator.py` | 51% | 确认 |
| `retrieval/hybrid_retriever.py` | 54% | 部分成立；“vector=None 直接 RuntimeError”描述不符合当前实现 |
| `contracts/memory_query.py` | 98% | 误判 |
| `dream/dream_agent.py` | 38% | 确认；resolver/merge 已测试，但完整 dream cycle 仍缺 |
| `retrieval/react_retriever.py` | 67% | 部分成立 |
| `services/session_memory_summarizer.py` | 56% | 确认 |
| `services/memory_injection_service.py` | 91% | 误判 |
| `services/memory_index_projector.py` | 79% | 部分成立 |
| `services/memory_scoring.py` | 99% | 过期 |
| `services/summarizer.py` | 25% | 确认，报告反而低估了缺口 |

原 ADEQUATE 列表存在高估：`memory_tool_service.py` 49%、`memory_turn_pipeline.py` 61%、`memory_maintenance_service.py` 72%。`v2_store.py` 76%，其余核心服务约 76%-91%。

## 第 3 章：infrastructure

模块真实行覆盖率：`77.4%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `runtime/observability.py` | 90% | 误判“全部方法零测试” |
| `runtime/lane_transcript.py` | 84% | 过期 |
| `runtime/trace_runtime.py` | 93% | 过期 |
| `runtime/turn_trace_store.py` | 73% | 部分成立；PermissionError 专项测试仍缺 |
| `runtime/context_economy_benchmark_store.py` | 89% | 误判 |
| `persistence/database_cron.py` | 19% | 确认 |
| `persistence/database_profile_relation.py` | 23% | 确认覆盖缺口；“原始 SQL 注入”误判，当前使用 SQLModel 参数化表达式 |
| `persistence/persona_cache.py` | 33% | 确认 |
| `security/input_sanitizer.py` | 60% | 部分成立，低风险包装器 |
| `security/output_guard.py` | 100% | 误判 |
| `gateway/gateway_exceptions.py` | 100% | 低价值 |
| `gateway/gateway_lane.py` | 88% | 误判为 MINIMAL；完整入口和 tool 路径已有多条测试 |
| `gateway/gateway_call.py` | 91% | 误判为 MINIMAL |
| `gateway/gateway_tasks.py` | 42% | 确认，优先级高 |
| `runtime/lane_storage.py` | 87% | 误判为 MINIMAL |
| `gateway/model_router.py` | 94% | 过期 |
| `gateway/output_guard.py` | 79% | 部分成立；sanitize 已有多条测试 |
| `gateway/gateway_policy.py` | 92% | 过期 |
| `gateway/gateway_result.py` | 72% | 部分成立 |
| `gateway/provider_capabilities.py` | 89% | 过期；None/unknown 已补测 |
| `context_economy/prompt_templates.py` | 89% | 误判 |
| `context_economy/token_estimator.py` | 0% | 确认，但仅 8 条语句，低风险 |
| `runtime/event_bus.py` | 79% | 部分成立 |
| `runtime/lane_history.py` | 75% | 部分成立 |
| `persistence/database_jargon.py` | 66% | 部分成立 |
| `persistence/database_memory.py` | 36% | 确认 |
| `persistence/persistence_schema.py` | 83% | 报告严重低估 |
| `persistence/state_profile_persistence.py` | 84% | 基本正确 |
| `persistence/sqlite_helpers.py` | 100% | 过期；busy timeout 已测试 |
| `repositories/*.py` | 56%-71% | 部分成立，不是“几乎零覆盖” |

## 第 4 章：state

模块真实行覆盖率：`79.5%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `contracts/profile_summary.py` | 93% | 误判为 NONE |
| `contracts/wait_state.py` | 83% | 误判为 NONE |
| `energy/energy_manager.py` | 89% | 误判为高风险 MINIMAL；仅 3 条语句未执行 |
| `mood/mood_decay.py` | 95% | 误判且在报告中重复分类 |
| `private_chat/private_chat_manager.py` | 73% | 部分成立；等待/关闭/KV 路径仍缺，但并非核心循环完全未测 |
| `mood/mood_manager.py` | 67% | 确认部分解析和 timeout 分支缺口 |
| `relationship/affection_router.py` | 81% | 部分成立 |
| `group_wait/group_reply_wait_manager.py` | 82% | 部分成立；cancel_wait 和线程恢复已有测试 |
| `relationship/relationship_engine.py` | 84% | 基本充分，事件矩阵可补 |
| `chat_state_service.py` | 85% | 基本充分 |
| `user_profile_service.py` | 72% | 部分成立 |
| `energy/frequency_controller.py` | 92% | 正确，充分 |

## 第 5 章：learning

模块真实行覆盖率：`73.1%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `profiling/nickname_generator.py` | 48% | 确认缺口；parse_result 已有回归测试，不是零测试 |
| `contracts/review_item.py` | 93% | 误判为 NONE |
| `logging/bot_reply_recorder.py` | 82% | 误判，并且报告重复分类 |
| `mining/expression_miner.py` | 38% | 确认；真实类几乎未直接测试 |
| `mining/social_relation_miner.py` | 83% | 过期 |
| `review/review_service.py` | 62% | 部分成立；list/submit 正常路径已有测试 |
| `review/jargon_auto_check_task.py` | 70% | 部分成立 |
| `review/expression_auto_check_task.py` | 78% | 部分成立 |
| `review/expression_governance_runner.py` | 69% | 部分成立 |
| `mining/expression_pattern_enricher.py` | 89% | 误判为 MINIMAL |
| `mining/jargon_candidate_extractor.py` | 85% | 误判为 MINIMAL |
| `mining/jargon_miner.py` | 73% | 部分成立 |
| `contracts/learning_events.py` | 100% | 误判 |
| `evolution_manager.py` | 70% | 部分成立 |
| `review/reflector.py` | 72% | 部分成立；成功、空分数和失败保留路径已有测试 |
| `review/reflect_tracker.py` | 72% | 部分成立 |
| `mining/expression_candidate_extractor.py` | 84% | 报告低估 |
| `mining/jargon_enricher.py` | 94% | 误判 |

## 第 6 章：proactive

模块真实行覆盖率：`76.7%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `diary_service.py` | 83% | 误判“零测试”；run_once、认知反馈、should_run 3-5 点均已测试 |
| `group_signin_service.py` | 80% | 误判“零测试”；已有 5 条直接测试 |
| `heartflow/feedback_bridge.py` | 87% | 误判 |
| `heartflow/models.py` | 100% | 误判 |
| `heartflow/topic_digest_service.py` | 75% | 部分成立 |
| `heartflow/manager.py` | 82% | 严重误判；已有 20 条主测试和补缺测试 |
| `dream_scheduler.py` | 67% | 确认部分关键分支缺口 |
| `rhythm.py` | 88% | 误判“0 测试”；四时段和默认配置均有执行 |
| `proactive_task.py` | 60% | 确认，维护编排和错误隔离仍是主要缺口 |
| `dispatcher.py` | 88% | 报告低估 |
| `wakeup_service.py` | 75% | 部分成立 |
| `review_dispatcher.py` | 74% | 基本正确；规模较小 |

## 第 7 章：multimodal

模块真实行覆盖率：`58.7%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `image_pipeline.py` | 46% | 确认；坏 payload 已测，`transform_gif` 仍零直接测试 |
| `visual_cortex.py` | 60% | 确认部分缺口；异常/取消时 `task_done()` 不执行是实际挂起风险 |
| `meme/meme_sender.py` | 80% | 报告严重低估 |
| `meme/meme_init.py` | 47% | 确认低覆盖，但磁盘满测试价值较低 |
| `meme/meme_config.py` | 100% | N/A 判断正确 |

## 第 8 章：presentation

模块真实行覆盖率：`66.1%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `events/message_entry.py` | 50% | 确认分支缺口，但“零测试”错误；已有直接和宿主集成测试 |
| `events/error_interceptor.py` | 80% | 误判为 NONE |
| `events/result_sniffer.py` | 80% | 误判为 NONE |
| `events/startup_hooks.py` | 44% | 确认 |
| `dto/message_scope.py` | 94% | 过期；非标准 accessor 已补测 |
| `dto/command_models.py` | 97% | 误判为 NONE |
| `commands/admin_commands.py` | 56% | 部分成立 |
| `commands/review_commands.py` | 25% | 确认 |
| `commands/work_mode.py` | 88% | 误判“约 15%” |
| `commands/mai_help.py` | 100% | 误判“约 15%” |

## 第 9 章：shared

模块真实行覆盖率：`59.1%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `helpers/plugin_helpers.py` | 58% | 确认；scope/direct-call/result 提取仍缺 |
| `helpers/event_utils.py` | 0% | 确认，但仅 15 条语句 |
| `helpers/text_utils.py` | 0% | 确认，低风险 |
| `helpers/time_utils.py` | 0% | 确认，极低价值 |
| `constants/defaults.py` | 90% | 报告低估 |
| `contracts/service_protocols.py` | Protocol | 无需补运行时测试 |
| `exceptions.py` | 0% | 低价值异常定义 |

## 第 10 章：webui

模块真实行覆盖率：`66.8%`

| 区域/文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `backend/services/dashboard_service.py` | 87% | 误判“约 15%”；降级路径已补测 |
| `backend/services/memory_ui_service.py` | 71% | 报告严重低估 |
| `backend/adapters/plugin_api.py` | 83% | 报告严重低估 |
| `plugin_pages.py` | 55% | 部分成立 |
| `backend/routes/*.py` | 37%-95% | “约 5%”误判 |
| `backend/services/*.py` | 26%-94% | “约 30%”过度概括 |

路由真实低点：`users_routes.py` 37%、`config_routes.py` 44%；其余主要路由 67%-95%。

服务真实低点：`chatruntimeservice.py` 26%、`runtimeuiservice.py` 42%、`review_ui_service.py` 58%、`admin_ui_service.py` 59%。报告没有识别出最薄弱的 `chatruntimeservice.py`。

## 第 11 章：workmode

模块真实行覆盖率：`69.9%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `cron_guard/heartbeat.py` | 62% | 部分成立；reload 已测试，长期循环异常/取消仍缺 |
| `router.py` | 82% | 报告低估 |
| `subagents/computer_agent.py` | 57% | 确认部分缺口 |
| `subagents/base_agent.py` | 65% | 部分成立 |
| `subagents/cron_agent.py` | 76% | 报告中的 `_call_add_job` 在当前文件不存在，具体描述误判 |
| `tools/handoff_registry.py` | 96% | 报告低估 |

## 第 12 章：app

模块真实行覆盖率：`54.4%`

| 文件 | 实际覆盖 | 复审结论 |
|---|---:|---|
| `plugin_facade.py` | 49% | 确认仍有较大缺口；apply_hot_config 已有成功和回滚测试 |
| `lifecycle.py` | 17% | 确认，当前最高优先级测试缺口 |
| `runtime_context.py` | 91% | 严重误判“约 5%” |
| `bootstrap.py` | 61% | 部分成立；已有多条 wiring 测试，不是仅 import |
| `runtime_facade_protocol.py` | 100% | N/A 判断基本正确 |

`bootstrap.py` 的 `Path(getattr(persistence, "cache_dir", default))` 在属性存在但值为 `None` 时仍会 `Path(None)`；是否可达取决于 `PersistenceManager` 构造约束，应作为独立契约测试，而不是直接断言生产必崩。

## 修正后的优先级

### P0

1. `multimodal/visual_cortex.py`：将每次成功 `queue.get()` 对应的 `task_done()` 放入可靠的 `finally`，补处理异常和取消测试。
2. `app/lifecycle.py`：补 terminate 幂等、部分组件失败、任务取消、关闭顺序测试。

### P1

1. `memory/services/memory_observer.py`（18%）
2. `memory/services/summarizer.py`（25%）
3. `memory/services/topic_summarizer.py`（36%）
4. `memory/dream/dream_agent.py`（38%）
5. `memory/services/memory_engine.py`（41%）
6. `infrastructure/gateway/gateway_tasks.py`（42%）
7. `memory/persona/persona_summarizer.py`（48%）
8. `presentation/events/message_entry.py`（50%）
9. `proactive/proactive_task.py`（60%，重点维护编排）

### P2

1. `conversation/attention/vision_binding.py`（13%）
2. `infrastructure/persistence/database_cron.py`（19%）
3. `infrastructure/persistence/database_profile_relation.py`（23%）
4. `webui/backend/services/chatruntimeservice.py`（26%）
5. `infrastructure/persistence/persona_cache.py`（33%）
6. `infrastructure/persistence/database_memory.py`（36%）
7. `learning/mining/expression_miner.py`（38%）
8. `presentation/events/startup_hooks.py`（44%）
9. `multimodal/image_pipeline.py`（46%）
10. `learning/profiling/nickname_generator.py`（48%）

### 不建议机械补测

- 纯数据类、Protocol、异常定义、重导出模块。
- 已达到约 85%-100% 且没有高风险未执行分支的文件。
- 仅为了让每个文件拥有一个测试而增加的 import/常量断言。

## 最终结论

原报告适合作为候选清单，不适合作为执行计划。其风险方向在 lifecycle、memory 核心服务、image pipeline 等处有参考价值，但“NONE/MINIMAL”分类和测试数量大面积过期或错误。后续补测应以本报告的真实覆盖率和未执行分支为基线。
