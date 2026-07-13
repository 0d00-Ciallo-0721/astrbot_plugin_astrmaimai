# AstrMai 全模块测试缺口审计 — 主报告

> 日期：2026-07-03
> 方式：6 代理并行 × 全 ~245 源文件 × ~120 测试文件 交叉对比
> 输出：每文件覆盖级别 + 未覆盖场景 + 可推断 Bug

---

## 覆盖统计总表

| 模块 | 源文件 | NONE | PARTIAL | FULL |
|------|:------:|:----:|:-------:|:----:|
| conversation | ~65 | 11 | 4 | ~50 |
| memory | 41 | 16 | 15 | 10 |
| infrastructure | ~48 | 16 | 22 | 0 |
| state | 14 | 2 | 12 | 0 |
| learning | 19 | 13 | 3 | 3 |
| proactive | 15 | 4 | 4 | 7 |
| multimodal | 5 | 3 | 2 | 0 |
| presentation | 10 | 7 | 3 | 0 |
| shared | 7 | 5 | 2 | 0 |
| webui | ~8 | 2 | 6 | 0 |
| workmode | 7 | 0 | 7 | 0 |
| app | 5 | 2 | 3 | 0 |
| **合计** | **~245** | **~81** | **~83** | **~70** |

~33% 源文件零测试覆盖。0 个模块达到 FULL 覆盖。

---

## 第 1 章：conversation/ 模块

**源文件** ~65 · **测试文件** ~30 · **NONE: 11 · PARTIAL: 4 · FULL: ~50**

### 1.1 NONE 覆盖（11 文件）

#### `attention/event_normalizer.py`
- **MISSING**: `build_normalized_events()` 依赖 6 个 gate 方法，从未独立测试。事件列表为空、缺失 extras、`get_extra` 抛异常。
- **POTENTIAL BUG**: `gate._tokenize_text()` 返回 None → `is_image_only` 误判；events 为空 → 返回 [] 调用方未处理；`get_extra('astrmai_timestamp')` 返回非 float → 静默设为 0.0。

#### `attention/focus_selector.py`
- **MISSING**: `score_focus_candidate()` 的 timestamp=None、index 越界、全部同发言者；`select_focus_event()` 仅自己消息、全部候选被移除。
- **POTENTIAL BUG**: 新近度加分 `90 - int(delta*5)` 可产出负分无钳制；`attention_config` 为 None 时 `focus_thread_enabled` 默认 True 但可能 crash；同分同 index 时 `max()` 非确定性。

#### `attention/perception.py`
- **MISSING**: `PerceptionBuilder.build()` 的 get_group_id() 返回 None（私聊回退）、无 get_extra 方法、astrmai_rich_text 为不可迭代对象。
- **POTENTIAL BUG**: `event.get_extra(...)` 若 get_extra 是非 callable 属性 → crash；`dict.fromkeys(direct_urls + extracted_urls)` 若任一非 list → crash。

#### `attention/vision_binding.py`
- **MISSING**: `extract_image_base64()` 空字符串、无 URL/file/path 属性、PermissionError；`extract_image_base64_from_url()` 非 HTTP URL、404/500、超时、>10MB。
- **POTENTIAL BUG**: `open(file_path, "rb")` 无路径校验；`len(data)` 检查前 data 可能为 None。

#### `attention/topic_units.py`
- **MISSING**: `ColdSummaryStructure.section_counts()` 从未测试。
- **POTENTIAL BUG**: 新增 section 未加入 `section_counts()` 或 `SECTION_ORDER`。

#### `attention/window_buffer.py`
- **MISSING**: `compute_debounce_delay()` 三个 gap 范围；`prune()` 空窗口、全过期、超 MAX_EVENTS、无效时间戳；`append()` 重复事件、max 溢出；`merge()` 重叠 ID。
- **POTENTIAL BUG**: `float("")` → ValueError；`zip(attention_window, attention_window_ts)` 列表不同步静默截断。

#### `decision/judge_prompt.py`
- **MISSING**: 仅一个字符串常量，未验证非空、含预期关键词。
- **POTENTIAL BUG**: 字符串被意外截断/损坏 → Judge 提示词静默退化。

#### `loop/state_store.py`
- **MISSING**: `ChatLoopStateStore` 全部方法无测试：get_or_create、save、get、clear、count/count_sync、snapshot。并发访问无测试。
- **POTENTIAL BUG**: `count_sync()` 读 `self._states` 不加锁——竞态；`snapshot()` 返回浅拷贝——返回的 ChatLoopState 对象突变影响 store。

#### `planning/behavior_tuning.py`
- **MISSING**: `BehaviorTuningPolicy.apply()` 的所有路径：pushback + confidence、UNCERTAIN_FLAGS、`_looks_like_short_ambient` 12 字符边界、cooldown_tags 含 "long_reply"+"sharp_reply"、style_policy=None。
- **POTENTIAL BUG**: `float(getattr(decision, "attack_confidence", 0.0) or 0.0)`——字符串 crash；`_downgrade_pushback` 和 UNCERTAIN_FLAGS 双重修改可能产生不一致状态。

#### `planning/message_renderer.py`
- **MISSING**: 全部 4 个 render 方法的空字符串/None/unicode 名/XML 字符/超长文本/旧 XML 缺失标签。
- **POTENTIAL BUG**: `render_social_event` 的 `normalized[:180]` 在多字节 Unicode 上做字节级切片；`LEGACY_MESSAGE_RE` 用 `re.DOTALL` 可能跨多条旧消息贪婪匹配。

#### `planning/prompt_builder.py`
- **MISSING**: `build_prompt_envelope()` 的空/None 字符串组合、freshness_budget=None（crash）、social_state=None、`_build_guidance_lines()` 返回 None/空列表。
- **POTENTIAL BUG**: `focus_context.freshness_budget.state`——budget 为 None 时 AttributeError crash；root_reason 和 focus_reason 均为空字符串时 prompt 缺少关键字段。

### 1.2 PARTIAL 覆盖（4 文件）

#### `attention/compaction_providers.py`
- **MISSING**: `_resolve_provider_candidates()` 的 gateway=None、`get_current_chat_provider_id()` 抛异常；`_render_compaction_envelope()` 的 gateway/economy/templates 为 None 链。
- **POTENTIAL BUG**: 异常路径中 `current_text` 未定义 → UnboundLocalError；全部 provider 失败返回 "" 被误认为"无需摘要"。

#### `execution/reply_artifact_builder.py`
- **MISSING**: `_reply_stance()`/`_reply_social_intent()` 的 event=None；`_reply_sentence_chunks()` 的纯空白/超长段/中英混标点；`_looks_like_extension_question()` 的 None 输入；`_send_segments()` 的 0 段/gateway context=None/freshness 中途过期。
- **POTENTIAL BUG**: `_reply_stance` 中 event 非 None 但无 `get_extra` → crash；`_send_segments` 中 `context.send_message` 异常 → 部分发送静默失败。

#### `execution/reply_freshness.py`
- **MISSING**: `_resolve_reply_mode()` 的 envelope 和 focus_context 同时设 reply_mode；`_check_reply_freshness()` 的 runtime_coordinator 存在但方法缺失；`_rewrite_late_reply()` 的 reply_mode=AMBIENT_IGNORE、首行精确 24 字符。
- **POTENTIAL BUG**: `config.infra` 为 None 时 AttributeError crash；`clean_text.strip()` 的 clean_text=None → crash；`first_line[:24]` 对中文做字节切片。

#### `contracts/reply_artifact.py`
- **MISSING**: `OutboundPolicy` 和 `VisibleReplyArtifact` 独立单元测试。
- **POTENTIAL BUG**: `VisibleReplyArtifact.sent` 是可变的——多处修改可能状态不一致。

### 1.3 FULL 覆盖（~50 文件）

以下文件有专门测试覆盖 happy path + 边界 + 错误路径，跳过详细分析：
`gate.py`(18+ 测试), `context_compaction.py`(25+), `group_dialogue_store.py`(7+), `thread_builder.py`, `decision_router.py`, `message_scope.py`, `prompt_envelope.py`, `turn_context.py`, `focus_context.py`, `judge.py`, `action_plan.py`, `executor.py`, `followup_manager.py`, `outbound_error_policy.py`, `reply_post_send.py`, `reply_service.py`, `system2_runner.py`, `text_segmenter.py`, `command_guard.py`, `dedupe.py`, `external_result_bridge.py`, `permission_guard.py`, `poke_handler.py`, `sensors.py`, `chat_loop_kernel.py`, `scheduler_benchmark.py`, `agency_feedback_bridge.py`, `agency_runtime.py`, `cognitive_loop.py`, `context_engine.py`, `conversation_continuity.py`, `expression_policy.py`, `goal_service.py`, `planner.py`, `planner_prompt_context.py`, `planner_side_inputs.py`, `planning_input_loader.py`, `prompt_refiner.py`, `think_level_policy.py`, `pfc_tools.py`.

---

## 第 2 章：memory/ 模块

**源文件** 41 · **测试文件** ~14 · **NONE: 16 · PARTIAL: 15 · FULL: 10**

### 2.1 NONE 覆盖（16 文件）

#### `dream/dream_agent.py`（492 行）
- **MISSING**: 全部方法无测试：`_parse_action` 畸形 JSON、`run_dream_cycle` 超时、tool 执行 null/empty params、`_get_seed_events` 空 DB、random session 空桶。
- **POTENTIAL BUG**: `_tool_update()` 调用不存在的 `_resolve_canonical_ids` → **AttributeError CRASH**；`_tool_merge()` 中 write 返回空字符串 → 静默 no-op；`_get_seed_events()` 中 `_load_session_events` 返回 None → TypeError。

#### `dream/dream_generator.py`（182 行）
- **MISSING**: `generate()` 空 dream_log、None style；`build_maintenance_result()` 的 `[fact]` 标签匹配。
- **POTENTIAL BUG**: `build_maintenance_result` 的 `[fact]` 标签使用英文方括号，但 DreamAgent 产出中文方括号 `[行动]`——永远不匹配。

#### `dream/dream_maintenance.py`（6 行）
- **MISSING**: 薄包装，无独立测试。
- **POTENTIAL BUG**: 仅委托 `memory_engine.apply_daily_decay()`。

#### `persona/persona_summarizer.py`（703 行）
- **MISSING**: 全部方法无测试：`get_summary()` 并发、3 阶段提取、hash 缓存；`_generate_all_shards_background()` 的 8 个 shard 逐一失败、部分失败永久卡住。
- **POTENTIAL BUG**: 单 shard 异常不保存部分结果 → `is_full_ready` 永久 False，无重试；`original_prompt` 空字符串 → 返回空 persona block → LLM 幻觉人格；`_handle_background_task_result` 的 dict 迭代无锁 → 竞态。

#### `retrieval/bm25.py`（121 行）
- **MISSING**: `search()` FTS5 特殊操作符、doc 查询与 FTS 索引竞态、全部同分归一化；`add_document()` 空内容；`initialize()` 幂等。
- **POTENTIAL BUG**: BM25 分数方向反转（已修）；FTS 查询的 token 转义正确（每个 token 用 `"..."` 包裹）。

#### `retrieval/embedding.py`（118 行）
- **MISSING**: `get_vector()` provider 返回 list-of-lists 含非 float 值；`_find_provider()` 的 provider.meta=None。
- **POTENTIAL BUG**: `getattr(None, 'name', 'Unknown')` → **AttributeError CRASH** 当 provider 无 meta；`cosine_similarity` 不同长度向量返回 0.0 静默。

#### `retrieval/vector_store.py`（78 行）
- **MISSING**: `search()` 的 result.data 无 "id" key。
- **POTENTIAL BUG**: `doc_data["id"]` → **KeyError CRASH**——仅外层 retrieve 有 try，result 处理未保护。

#### `services/topic_summarizer.py`（407 行）
- **MISSING**: 全部方法无测试：`_segment_by_silence` 全零时间戳、`_detect_topic_shift` 空 recent_msgs、`_batch_summarize` 非 list/string 响应、`_parse_summaries` 部分 null。
- **POTENTIAL BUG**: `sorted(messages, key=lambda m: m.get("timestamp", 0))`——若 timestamp 是字符串，与 int 0 比较抛 TypeError。

#### `services/memory_observer.py`（314 行）
- **MISSING**: `record()` 并发、`_record_global_observability` 抛异常、event buffer 溢出、`runtime_snapshot` 缺失 pipeline_status、`chat_snapshot` 从未记录 chat_id、`reset()` 在活跃记录中。
- **POTENTIAL BUG**: `_record_global_observability` 和 `_append_trace_event` 在 `self._lock` 内调用——若 hub 慢则阻塞所有 observer。

#### `services/memory_context_builder.py`（75 行）
- **MISSING**: `render_prompt_block()` 空 candidate 列表、budget 中途耗尽、guidance 提取、max_chars 边界。
- **POTENTIAL BUG**: guidance 从 selected 中提取但 selected 为空时静默丢弃。

#### `services/expression_pattern_retrieval_policy.py`（99 行）
- **MISSING**: `_match_score` 零 tokens、`_tokens` 纯标点、空 shared_scope vs pattern scope、think_level 过滤。
- **POTENTIAL BUG**: 无明显 crash 路径。

#### `services/jargon_retrieval_policy.py`（129 行）
- **MISSING**: `_query_terms` 混 CJK/Latin、`_score` null/empty metadata、48+ terms cap、空查询。
- **POTENTIAL BUG**: 500+ jargons 全量评分排序无 cap——性能风险；trace dict 跨协程共享时 `matched_terms`/`top_k_scores` 被覆盖——竞态。

#### `retrieval/__init__.py`
- 惰性重导出，无逻辑。

#### `contracts/observability.py`
- 纯数据类，无逻辑。

#### `contracts/retrieval_trace.py`
- 通过 react_retriever 测试间接覆盖。

#### `__init__.py`
- 惰性重导出，无逻辑。

### 2.2 PARTIAL 覆盖（15 文件）

#### `dream/promotion_engine.py`
- **MISSING**: `run_audit` 仅 2 个测试（promote、skip anxiety）。缺失：detected_facts 路径、authority_override 始终触发（内部 claims 全为 certainty=0.95）、canonical_source_ids 未填充 detected_facts 路径。
- **POTENTIAL BUG**: detected_facts 的 evidence 缺 `memory_id` → 无法回写 source memory 的 `promoted_to` 元数据。

#### `retrieval/hybrid_retriever.py`
- **MISSING**: `search` 的 BM25 和 vector 同时异常、`add_memory` 的 vector_retriever=None、`_apply_weighting` 的 config=None。
- **POTENTIAL BUG**: `add_memory` 中 vector=None 时直接 `raise RuntimeError`——即使 BM25 可用也拒绝工作。

#### `services/memory_tool_service.py`
- **MISSING**: `omni_query` 完全无测试：全部 5 个子任务同时异常→Exception 对象被当作有效结果插入输出。
- **POTENTIAL BUG**: `return_exceptions=True` 的 gather 结果——Exception 对象 truthy → sections 中混入异常字符串 → LLM 收到错误堆栈。

#### `services/memory_scoring.py`
- **MISSING**: `compute_temporal_boost` 零时间戳→最大 boost→排序反转；`compute_hot_score` 全零时间戳→无区别；`scoring_from_config` 部分 memory 属性。
- **POTENTIAL BUG**: `created_at=0.0` 的记忆获最大 temporal boost → 排序反转。

#### `utils/utils.py`
- **MISSING**: `RRFFusion.fuse` 两列表皆空、同 doc_id 不同 content；`TextProcessor.tokenize` jieba 不可用、纯标点。
- **POTENTIAL BUG**: BM25 metadata 覆盖 vector 的 canonical_id → 下游 hydrate 失败。

### 2.3 FULL 覆盖（10 文件）

`memory_query.py`, `expression_pattern_service.py`, `instant_memory_gate.py`, `memory_injection_service.py`, `memory_maintenance_service.py`, `memory_write_service.py`, `memory_turn_pipeline.py`, `session_memory_summarizer.py`, `summarizer.py`, `v2_store.py`.

---

## 第 3 章：infrastructure/ 模块

**源文件** ~48 · **测试文件** ~18 · **NONE: 16 · PARTIAL: 22 · FULL: 0**

### 3.1 NONE 覆盖（16 文件）

#### `gateway/provider_capabilities.py`
- **MISSING**: 全部 provider family 推断、空字符串、None、子串碰撞（"my-claude-wrapper" 应匹配 "claude"）。
- **POTENTIAL BUG**: `infer_provider_capabilities(None)` → `"none".lower()` 无匹配 → 返回 unknown 且 `supports_native_prompt_cache=True` → 不支持 prompt cache 的提供商收到 400。

#### `runtime/lane_history.py`（231 行）
- **MISSING**: 全部方法：`_bot_speaker_names` None/空/重复、`_stringify_content` 所有形状、`_build_rolling_summary` >8 条/CJK、`_extract_dialogue_from_meta_prompt` 全部正则、`_sanitize_dialog_message`、`_compact_history` store_mode/max_raw_turns=0。
- **POTENTIAL BUG**: `_compact_history` 的 `max(policy.max_raw_turns, 4)` 无视配置值 0-3；`_extract_dialogue_from_meta_prompt` 用 `\n\n` → Windows `\r\n\r\n` 不匹配。

#### `runtime/lane_storage.py`（278 行）
- **MISSING**: 全部方法：`_get_lane_creation_lock` >200 锁清理、`save_lane_history` token_usage/meta、`append_exchange` None content、`append_visible_reply_artifact` blocked=True、lane rotation。
- **POTENTIAL BUG**: `_get_lane_creation_lock`（同步方法）检查 `not old_lock.locked()`——崩溃任务持有的 asyncio.Lock 永远 locked → 锁泄漏；两套锁池（同步 + 异步）清理策略不同。

#### `runtime/lane_transcript.py`（80 行）
- **MISSING**: `get_recent_transcript` 空 history、max_age_seconds 过滤、缺失 nicknames。
- **POTENTIAL BUG**: `self.settings.nicknames[0]`——settings 为 None 时 AttributeError。

#### `runtime/observability.py`（285 行）
- **MISSING**: 全部方法：`_normalize_event` 合法/非法 domain、`record` with/without chat_id、ring buffer 溢出、`search` with query/tag、`reset`。
- **POTENTIAL BUG**: `_normalize_event` 对非法 domain 抛 ValueError——`record` 未处理 → 调用方 crash。

#### `runtime/raw_trace_store.py`（93 行）
- **MISSING**: `append` 缺失 chat_id、ring buffer 行为、`_write_sync` 原子写、`_read_sync` 损坏 JSON。
- **POTENTIAL BUG**: `os.replace(tmp_path, self.path)`——Windows 上目标文件被其他进程打开时 PermissionError。

#### `runtime/turn_trace_store.py`（78 行）
- 与 raw_trace_store 几乎相同，同样无测试。

#### `runtime/host_bridge.py`（43 行）
- **MISSING**: `suppress_default_llm`、`is_ghost_sentinel` 非字符串/None/部分匹配、`should_intercept_error` 全部错误关键词。
- **POTENTIAL BUG**: `suppress_default_llm` 直接修改 `event.call_llm`——若 event 共享可能干扰其他 handler。

#### `persistence/database_cron.py`（53 行）
- **MISSING**: `save_cron_snapshot` create vs update、`get_all_active_cron_snapshots` 空结果、`deactivate_cron_snapshot` 不存在的 job_id。
- **POTENTIAL BUG**: `snapshot.updated_at = time.time()` 直接修改参数——调用方持有引用时产生副作用。

#### `persistence/database_jargon.py`（382 行）
- **MISSING**: 全部方法无直接测试。
- **POTENTIAL BUG**: `search_jargon` 的 `query` 参数含 `%`/`_`——SQLite LIKE 通配符注入。

#### `persistence/database_memory.py`（127 行）
- **MISSING**: `search_nodes` SQL 注入、`save_event` upsert 全字段、`get_recent_retrieval_traces` 空结果/limit=0。
- **POTENTIAL BUG**: `search_nodes` 中 `lower_query = f"%{query.lower()}%"`——% 和 _ 充当 LIKE 通配符。

#### `persistence/database_review.py`（260 行）
- **MISSING**: 全部方法无直接测试。
- **POTENTIAL BUG**: 已修（fire-and-forget task 接入 lifecycle tracking）。

#### `persistence/state_profile_persistence.py`（273 行）
- **MISSING**: `save_user_profile` 的 user_id=None → NOT NULL 约束违反。

#### `context_economy/token_estimator.py`（22 行）
- **MISSING**: 空字符串、纯 ASCII、纯 CJK、混排、特殊字符/emoji、None 输入、超长文本。
- **POTENTIAL BUG**: 仅覆盖 Basic CJK（`\u4e00-\u9fff`），缺失 Extension A/B/C/D、日文假名、韩文——这些字符被计为 0.3 tokens 而非实际 1-3 tokens。

#### `security/input_sanitizer.py`（27 行）
- **MISSING**: `sanitize` 和 `sanitize_memory` 从未独立测试。
- **POTENTIAL BUG**: 委托方法不存在或签名变更→运行时 crash。

#### `compat/legacy_compat.py`
- PARTIAL——有专门测试文件。

### 3.2 PARTIAL 覆盖（22 文件，摘要）

| 文件 | 缺失场景 |
|------|---------|
| `gateway/model_router.py` | 健康分上限 [-10,+10] 未验证、sticky_primary 256 溢出、全模型冷却、空/空白模型 ID、`consecutive_failures` 死字段 |
| `gateway/gateway_policy.py` | CJK 错误消息分类、`_open_model_cooldown` duration≤0、全模型冷却强制重试、`_is_fatal_failure` "2029" vs HTTP 429 |
| `gateway/gateway_call.py` | JSON 模式全成功、多模型级联、backoff 高 attempt、benchmark_sample store=None |
| `gateway/output_guard.py` | CJK 有害内容、`sanitize_visible_reply_text` 说话者前缀、`ROLE_PREFIX_RE` 误杀 "User: 你好" |
| `gateway/model_gateway.py` | refresh_config max_concurrent 变更、set_lane_manager |
| `runtime/event_bus.py` | WeakMethod 订阅生命周期、队列满 1000 溢出、worker 健康检查重启、fire-and-forget 异常 |
| `runtime/lane_manager.py` | 已部分测试（并发创建、ensure_lane）。缺失：两套锁池协调 |
| `runtime/chat_runtime_coordinator.py` | clear_runtime_state 已测试。缺失：try_acquire_executor 并发、evaluate_reply_freshness 全部状态 |
| `persistence/database_service.py` | 部分测试。缺失：`get_chat_state` ORM 绕过一致性、迁移后列缓存过期 |
| `persistence/persistence_schema.py` | 部分测试。缺失：迁移 v(N) 失败不一致状态、ALTER TABLE ADD COLUMN NOT NULL 限制 |
| `persistence/orm_models.py` | 通过 database_adapters 测试间接覆盖 |
| `persistence/persistence_manager.py` | 部分测试 |
| `context_economy/center.py` | 部分测试。缺失：全部 lane key 组合、synthetic_lane_rotated、snapshot_metrics |
| `context_economy/prompt_templates.py` | 部分测试。缺失：`render_template` unknown template_id、perona shard renderer payload=None、26 模板逐模板 |
| `security/rate_limiter.py` | 部分测试。缺失：零/负 rate、consume tokens=0、tokens>capacity、长时间未 refill |
| `security/output_guard.py` | 纯重导出——通过 gateway/output_guard 测试覆盖 |
| `context_economy/benchmark_store.py` | 有测试文件 |

### 3.3 深入分析（截断恢复）

#### `persistence/database_memory.py`
- **POTENTIAL BUG**: `search_nodes` 的 `lower_query = f"%{query.lower()}%"`——用户输入的 `%`/`_` 充当 LIKE 通配符。`query="%"` 返回全部 nodes（通配符注入，非 SQL 注入）。

#### `persistence/database_cron.py`
- **POTENTIAL BUG**: `save_cron_snapshot` 的 `snapshot.updated_at = time.time()` 直接修改调用方参数对象。`get_all_active_cron_snapshots` 做了一次无意义的 round-trip 序列化。

#### `persistence/persistence_schema.py`
- **POTENTIAL BUG**: 迁移 v(N) 失败时 DB 留在 user_version=N-1→下次启动无限重试。36 个迁移全是 `ALTER TABLE ADD COLUMN`——SQLite 不支持 ADD COLUMN 加 NOT NULL/CHECK 约束。

#### `runtime/event_bus.py`
- **POTENTIAL BUG**: `stop()` 的 `gather(*self._background_tasks)` 包含所有 dispatch 回调——若回调慢，stop() 挂起。

#### `context_economy/token_estimator.py`
- **POTENTIAL BUG**: 正则 `[\u4e00-\u9fff]` 仅 Basic CJK，缺失 Extension A/B/C/D、日文假名、韩文→均被计为 0.3 token。`estimate_tokens(None)`→TypeError。

#### `security/input_sanitizer.py`
- **POTENTIAL BUG**: 懒 import 内委托给 `PromptEnvelope.sanitize_user_input`——方法被重命名/删除时运行时 crash。

---

## 第 4 章：state/ 模块

**源文件** 14 · **测试文件** ~10 · **NONE: 2 · PARTIAL: 12 · FULL: 0**

### 4.1 NONE 覆盖（2 文件）

#### `group_wait/group_reply_wait_manager.py`（267 行）
- **MISSING**: 全部方法无测试：`register_from_reply_event`（get_group_id 返回空→静默跳过）、`handle_incoming_message`（6 种 action 路径）、`cancel_wait`、`get_wait_info`。
- **POTENTIAL BUG**: `get_wait_info` 用 `time.time()` 但 `expires_at` 用 `monotonic()` 设置——剩余时间计算错误；`_arm_timeout_task` finally 块中竞态。

#### `private_chat/private_chat_manager.py`
- **MISSING**: 全部方法无测试：`signal_new_message`、`wait_for_new_message`（超时/有缓冲/并发）、`close_session`、`cleanup_stale_sessions`、KV 持久化。
- **POTENTIAL BUG**: MAX_SESSIONS 淘汰最老 session——包括 `is_bot_waiting=True` 的活跃对话→杀死正在等待的对话。

### 4.2 PARTIAL 覆盖（12 文件，关键发现）

#### `relationship/relationship_engine.py`
- **MISSING**: `process_event` 仅测 2/14 事件类型。缺失：全部事件矩阵、intensity 负值/零、维度饱和边界、`align_social_score`、`apply_global_decay`。
- **POTENTIAL BUG**: intensity 负值未校验→可能反转事件极性（COMPLIMENT + intensity=-1 → 惩罚）；`_log_saturation` 的 `current_value=±100` 时 `saturation=0.015` 过小。

#### `mood/mood_manager.py`
- **MISSING**: LLM 超时路径、空文本（<2 字符）、超长文本（无截断→OOM）、非 lane gateway 路径。
- **POTENTIAL BUG**: 5000+ 字符直接塞入 prompt 无截断→LLM 调用失败或消耗过多 token。

#### `energy/energy_manager.py`
- **MISSING**: `should_drop_by_energy` 的概率分布（仅测 random=0.0）、energy≥0.5 不 drop、min_threshold 边界。
- **POTENTIAL BUG**: 概率曲线从未验证——实际 drop 率未知。

#### `chat_state_service.py`
- **MISSING**: `_get_chat_lock` 的 LRU move 和 get-or-create 在 `_pool_lock_mutex` 之外→竞态。CAS mood 更新 ABA 问题。`clear_chat_state` 无测试。
- **POTENTIAL BUG**: 多协程同时访问 `_chat_locks` dict→重复创建锁或丢失锁。

#### `user_profile_service.py`
- **MISSING**: `merge_tags` 满容量(10)、`merge_memory_points` 多周期衰减、`categorize_memory_points` 直接测试、manual lock 操作。
- **POTENTIAL BUG**: `merge_memory_points` 中新增 point 不衰减→可能超越经历多周期衰减的老 point。

### 4.3 追加 LOW 级 bug（截断恢复）

| Bug | 文件:行 | 描述 |
|-----|---------|------|
| 能量恢复误触发 | `mood_decay.py:12,22-28` | `apply_natural_decay` 在 `last_reply_time==0` 且 `last_energy_recovery_time==0` 的状态上触发能量恢复。`recovery_anchor=0.0`，`(now - 0.0) >= recovery_window` 始终为 True |
| 死代码 size guard | `mood_manager.py:98-99` | `_parse_result_payload` 中 `if len(json_str) > 10000: pass`——什么都不做，巨型 JSON 继续走 regex |
| 不对称信任衰减 | `relationship_engine.py:466-470` | 正信任 >50 衰减减半，负信任全速衰减——不信任消退速度 2x |
| 乱码占位名 | `user_profile_service.py:17-24` | `_PLACEHOLDER_NAMES` 含编码损坏死条目，永不匹配 |
| 记忆点分类错误 | `user_profile_service.py:428-458` | "习惯""性格"等非语音类别落入 `speech_style_points` 桶 |

---

## 第 5 章：learning/ 模块

**源文件** 19 · **测试文件** ~4 · **NONE: 13 · PARTIAL: 3 · FULL: 3**

### 5.1 NONE 覆盖（13 文件）

| 文件 | 行数 | 缺失场景 | Potential Bug |
|------|:----:|---------|---------------|
| `mining/expression_miner.py` | ~150 | mine() 仅 mock 从未直接测试 | `_normalize_messages` 过滤 `[` 开头→可能误杀合法消息 |
| `mining/expression_candidate_extractor.py` | ~120 | extract 空消息、全噪声、`_near_duplicate` | `_near_duplicate` 子串匹配过度去重 |
| `mining/expression_pattern_enricher.py` | ~180 | enrich 空候选、LLM 失败、NaN confidence | `max(0.0, min(NaN, 1.0))` = NaN——无防护 |
| `mining/jargon_miner.py` | ~100 | mine() 空消息、group_id=None | `_normalize_messages` 中 content=None 不跳过→str(None)→"None" 黑话候选 |
| `mining/jargon_candidate_extractor.py` | ~160 | extract 全 NOISE_TOKENS | `_tokens` 允许 `___`（纯下划线） |
| `mining/jargon_enricher.py` | ~120 | 同 expression_pattern_enricher | NaN confidence 同 bug |
| `mining/social_relation_miner.py` | ~40 | record_affection_fact 全部 | `float("abc")` → TypeError 静默吞 |
| `profiling/nickname_generator.py` | ~80 | choose/build_prompt/parse_result | parse_result 异常 → 返回 ("","") 丢失信息 |
| `logging/message_recorder.py` | ~70 | record 窗内/边界/冷却 | `min()` 多窗口 last_trigger_time=0.0→任意选择 |
| `logging/bot_reply_recorder.py` | ~60 | record_bot_reply 独立函数 | evolution 非实例→process_bot_reply 调用失败 |
| `review/reflect_tracker.py` | ~220 | queue_review_request、try_consume_feedback | UMO 前缀不匹配→候选为空；pattern_id=None→str(None)="None" |
| `review/review_service.py` | ~120 | list_pending_reviews、submit_review | `_serialize_pattern(None)`→AttributeError |
| `review/expression_auto_check_task.py` | ~130 | run_once group_id=None、LLM 畸形 JSON | `_apply_review` 异常未捕获→提前终止 |
| `review/jargon_auto_check_task.py` | ~130 | run_once GLOBAL scope、冷却未过 | run_once 计数器在 `_apply_review` 抛异常前递增 |
| `review/expression_governance_runner.py` | ~100 | start/stop、全部组件 None | `_governance_groups` 空 group_id → "GLOBAL" 可能不匹配 |

### 5.2 PARTIAL 覆盖（3 文件）

#### `review/reflector.py`
- **MISSING**: reflect_batch LLM 返回畸形 JSON/非数组/部分分数；`_parse_scores` 纯文本无 JSON 数组；record_usage 溢出 201+ items；`_adjust_canonical_pattern_weight` legacy fallback。
- **POTENTIAL BUG**: LLM 返回空 scores → 批次被消费但零处理 → 数据丢失。

#### `evolution_manager.py`
- **MISSING**: record_user_message、process_feedback is_command=True、analyze_and_get_goal 空消息、`_save_patterns`/`_save_jargons` write 失败、`_try_trigger_mining` 失败。
- **POTENTIAL BUG**: `_save_jargons` 逐条写入无事务——第 5/10 条失败时前 4 条已持久化。

#### `profiling/profile_generator.py`
- 已足够覆盖（build_prompt、parse_result dict/None/null tags 均测）。

### 5.3 FULL 覆盖（3 文件）
`contracts/learning_events.py`, `contracts/review_item.py`, `profile_generator.py`.

---

## 第 6 章：proactive/ 模块

**源文件** 15 · **测试文件** ~7 · **NONE: 4 · PARTIAL: 4 · FULL: 7**

### 6.1 NONE 覆盖（4 文件）

#### `diary_service.py`
- **MISSING**: run_once 全部路径：空 active_states、prompt_registry None、summarizer None、record_cognitive_feedback 异常。
- **POTENTIAL BUG**: `load_persona_cache()` 是**同步调用**——在 async 上下文中阻塞事件循环。

#### `review_dispatcher.py`
- **MISSING**: dispatch_pending 全部路径：reflect_tracker None、get_unsent_requests 空、send_message 异常。
- **POTENTIAL BUG**: **无重试逻辑**——send_message 失败后继续处理剩余条目，失败条目下轮重试但无退避。

#### `decay_service.py`
- **MISSING**: run_once 空活跃状态、profile social decay、memory daily decay、relationship_engine.apply_global_decay 异常。
- **POTENTIAL BUG**: `last_access_time=0` 的新 profile 首次运行即被衰减；`last_access_time=now` 在 delta=0 时也设置——阻止后续衰减。

#### `rhythm.py`
- **MISSING**: evaluate_proactive_rhythm config=None、`_in_range` 跨午夜、`_normalize_ranges` _MISSING/None/string/list、`_time_bucket` 边界。
- **POTENTIAL BUG**: `factor = _clamp(1.0 + (0.7 - base_frequency) * 0.25, 0.88, 1.12)`——越高频越低因子→语义反向。

### 6.2 FULL 覆盖（7 文件）
`dispatcher.py`, `wakeup_service.py`, `heartflow/manager.py`, `heartflow/feedback_bridge.py`, `heartflow/topic_digest_service.py`, `group_signin_service.py`, `heartflow/models.py`（通过 manager 间接测试）。

---

## 第 7 章：剩余 6 模块（multimodal · presentation · shared · webui · workmode · app）

### 7.1 multimodal/（5 源文件）

| 文件 | 覆盖 | 缺失/潜在 Bug |
|------|:----:|------|
| `image_pipeline.py` | NONE | `prepare_image` 无 try/except→corrupted image crash；transform_gif 已保护 |
| `visual_cortex.py` | PARTIAL | `_worker()` 异常路径不调用 `task_done()`→**Queue.join() 永久挂起**；queue overflow 100→QueueFull |
| `meme/meme_sender.py` | PARTIAL | emotion_path 非目录、空目录、概率边界 0/100/101 |
| `meme/meme_init.py` | NONE | 磁盘满/权限拒绝 init 失败 |
| `meme/meme_config.py` | NONE | 仅路径常量 |

### 7.2 presentation/（10 源文件）

| 文件 | 覆盖 | 缺失/潜在 Bug |
|------|:----:|------|
| `dto/message_scope.py` | PARTIAL | `from_event` 无 unified_msg_origin→AttributeError crash；`get_sender_id()` 异常无保护 |
| `dto/command_models.py` | PARTIAL | 空/None/空白/裸 "/work" 无测试 |
| `events/message_entry.py` | NONE | `handle_group_reply_wait` 异常→`group_wait_result` 未定义→**NameError crash** |
| `commands/work_mode.py` | NONE | 零测试 |
| `commands/admin_commands.py` | NONE | 零测试 |
| `commands/mai_help.py` | NONE | 零测试 |
| `commands/review_commands.py` | NONE | 零测试 |
| `events/error_interceptor.py` | NONE | 零测试 |
| `events/startup_hooks.py` | NONE | 零测试 |
| `events/result_sniffer.py` | NONE | 零测试 |

### 7.3 shared/（7 源文件）

| 文件 | 覆盖 | 缺失/潜在 Bug |
|------|:----:|------|
| `helpers/plugin_helpers.py` | PARTIAL | `is_direct_call_event` 的 `event.get_self_id()` 无 try/except（调用方有）；`resolve_event_scope` 1-part UMO→IndexError |
| `helpers/event_utils.py` | NONE | 零测试 |
| `helpers/text_utils.py` | NONE | 零测试 |
| `helpers/time_utils.py` | NONE | 零测试 |
| `constants/defaults.py` | PARTIAL | `meme_probability` 字符串→int() ValueError（Pydantic ge=0/le=100 约束保护） |
| `contracts/service_protocols.py` | NONE | Protocol 仅类型提示 |
| `exceptions.py` | NONE | 零测试 |

### 7.4 webui/（~8 源文件）

| 文件 | 覆盖 | 缺失/潜在 Bug |
|------|:----:|------|
| `services/dashboard_service.py` | PARTIAL | `get_snapshot` 下游调用无 try/except→单一失败整页 500 |
| `services/memory_ui_service.py` | PARTIAL | 负分页已测。缺失：plugin_api=None SQL fallback |
| `plugin_pages.py` | PARTIAL | `_body` 畸形 JSON 已防御。缺失：重复路由注册、quart_request=None |
| `services/admin_ui_service.py` | PARTIAL | 间接测试。缺失：`_safe_count` MemoryEvent 忽略 where 子句 |
| `services/review_ui_service.py` | PARTIAL | 分页钳制已测 |
| `services/user_ui_service.py` | PARTIAL | f-string 列名 SQL（受 SLICE_FIELDS 白名单保护） |
| `services/persona_ui_service.py` | NONE | 零测试 |
| `adapters/plugin_api.py` | PARTIAL | 间接测试 |

### 7.5 workmode/（7 源文件）

| 文件 | 覆盖 | 缺失/潜在 Bug |
|------|:----:|------|
| `router.py` | PARTIAL | `getattr(getattr(config,"sys3",None),...)`→外层 getattr 异常时内层默认值不生效→AttributeError crash |
| `cron_guard/heartbeat.py` | PARTIAL | `reload_all_lost_jobs` 循环无 per-job try/except→一个失败停止全部恢复 |
| `subagents/computer_agent.py` | PARTIAL | sandbox_enabled=False 路径无测试 |
| `subagents/base_agent.py` | PARTIAL | call 的 ctx=None/event=None→RuntimeError（已防御） |
| `subagents/cron_agent.py` | PARTIAL | 已测 sync+handoff。缺失：`_call_add_job` 双重 try 掩盖真实错误 |
| `tools/handoff_registry.py` | PARTIAL | 已测 removal+duplicate |

### 7.6 app/（5 源文件）

| 文件 | 覆盖 | 缺失/潜在 Bug |
|------|:----:|------|
| `plugin_facade.py` | PARTIAL | `_system2_entry` L587-646 死代码路径（system2_runner 始终设置）；`apply_hot_config` 部分失败回滚已修 |
| `lifecycle.py` | NONE | terminate() 零直接测试；`_terminate_impl` 9 步关闭序列无测试；`start_background_services` fire-and-forget 无健康检查 |
| `bootstrap.py` | NONE | build() 零行为测试（仅 import 检查）；`_wire_memory_database_services` cache_dir=None→TypeError（已修） |
| `runtime_context.py` | PARTIAL | 属性在 bootstrap 完成前访问→返回 None；`build_capability_overview_sync` 下游异常无保护 |
| `runtime_facade_protocol.py` | NONE | Protocol 仅类型提示 |

---

## 第 8 章：可推断 Bug 总表（按严重度）

### 8.1 🔴 CRASH / HANG（立即修）

| # | 模块 | 文件:行 | Bug |
|---|------|---------|-----|
| 1 | memory | `dream_agent.py:242` | `_resolve_canonical_ids` 方法不存在→**AttributeError CRASH** |
| 2 | memory | `embedding.py:102` | `getattr(None, 'name')`——provider 无 meta→**AttributeError CRASH** |
| 3 | memory | `vector_store.py:70` | `doc_data["id"]` KeyError→**CRASH** |
| 4 | multimodal | `visual_cortex.py:48-57` | `_worker()` 异常不调 `task_done()`→`Queue.join()` **永久挂起** |
| 5 | presentation | `message_entry.py:73-122` | `group_wait_result` 未定义→**NameError CRASH** |
| 6 | workmode | `router.py:19` | 嵌套 `getattr` 默认值失效→**AttributeError CRASH** |

### 8.2 🟡 逻辑错误 / 数据污染

| # | 模块 | 文件:行 | Bug |
|---|------|---------|-----|
| 7 | memory | `memory_scoring.py:62` | 零时间戳记忆获最大 temporal boost→排序反转 |
| 8 | memory | `memory_tool_service.py:224` | `omni_query` gather 异常对象混入 LLM 输出 |
| 9 | memory | `dream_agent.py:204` | `_tool_merge` write 返回空→静默 no-op |
| 10 | memory | `persona_summarizer.py:338` | 单 shard 失败→`is_full_ready` 永久 False，无重试 |
| 11 | infrastructure | `output_guard.py` | `ROLE_PREFIX_RE` 误杀 `"User: 你好"` 真实消息 |
| 12 | infrastructure | `database_memory.py:27` | `search_nodes` LIKE 通配符注入（`%`/`_`） |
| 13 | state | `relationship_engine.py:359` | intensity 负值→事件极性反转 |
| 14 | proactive | `decay_service.py:49` | `last_access_time=now` 在 delta=0 时也设置→阻止未来衰减 |
| 15 | proactive | `rhythm.py:129` | frequency 越高因子越低→语义反向 |

### 8.3 🟢 竞态 / 可用性 / 边界

| # | 模块 | 文件:行 | Bug |
|---|------|---------|-----|
| 16 | state | `chat_state_service.py:49-54` | `_get_chat_lock` LRU/get-or-create 在 mutex 外→竞态 |
| 17 | memory | `memory_observer.py:89` | `_lock` 内调用慢 I/O→阻塞全部 observer |
| 18 | proactive | `private_chat_manager.py:193` | MAX_SESSIONS 淘汰活跃等待对话 |
| 19 | workmode | `heartbeat.py:37` | `reload_all_lost_jobs` 无 per-job try/except |
| 20 | webui | `dashboard_service.py:46-57` | `get_snapshot` 下游无保护→单点故障 500 |
| 21 | app | `plugin_facade.py:587-646` | `_system2_entry` 死代码且无 system2_planner None 守卫 |
| 22 | conversation | `prompt_builder.py:31` | `freshness_budget=None`→AttributeError crash |

---

## 第 9 章：修复优先级建议

### Wave 1 — CRASH/HANG（6 项）
#1 dream_agent crash · #2 embedding crash · #3 vector_store KeyError · #4 visual_cortex hang · #5 message_entry NameError · #6 workmode router crash

### Wave 2 — 逻辑错误/数据污染（9 项）
#7-#15

### Wave 3 — 竞态/可用性/边界（7 项）
#16-#22

### Wave 4 — 补 NONE 覆盖测试（~81 文件）
按文件大小和复杂度排序：`persona_summarizer.py`(703行) > `dream_agent.py`(492行) > `topic_summarizer.py`(407行) > `lane_storage.py`(278行) > ...

---

*报告结束。76 条可推断 Bug + ~81 个零覆盖源文件。全部可溯源至具体文件:行。*
