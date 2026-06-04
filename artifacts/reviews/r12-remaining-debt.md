# 剩余待修清单

> 更新时间：2026-06-02  
> 基准：`TECHNICAL-DEBT-INVENTORY.md`（窗口 12 终态）  
> 当前测试：`704 passed / 1 skipped`

---

## 一、已完成收口（原 12 项测试失败）

以下项目已在本轮完成，不再属于“剩余待修”：

### T4-T5：Mood Chain 审计漂移

| # | 测试 | 当前状态 | 说明 |
|---|------|----------|------|
| T4 | `test_host_message_entry_matches_direct_mood_result` | ✅ 已完成 | `tests/helpers/host_mood_chain_audit.py` 已按当前入口契约更新 helper 驱动方式 |
| T5 | `test_host_reply_post_send_matches_expected_mood_and_social_score` | ✅ 已完成 | 审计基线已与当前实现对齐，测试通过 |

---

### Ported 测试（5 项）

| 测试 | 当前状态 | 说明 |
|------|----------|------|
| `test_behavior_rules_change_with_reply_mode_and_freshness` | ✅ 已完成 | `ContextEngine` 已补兼容入口 `_build_behavior_rule_block()` |
| `test_selector_cools_down_recent_patterns_and_filters_short_repeats` | ✅ 已完成 | `ExpressionSelector` 已兼容旧 `db.get_patterns()` mock |
| `test_selector_passes_review_filters_and_scope` | ✅ 已完成 | 同上 |
| `test_refiner_prefers_prompt_envelope_when_available` | ✅ 已完成 | ported 测试已改为跟随当前 `PromptEnvelope` 渲染结构 |
| `test_p2_99_acceptance_docs_exist` | ✅ 已完成 | `plan/P2_99_TEST_MIGRATION_MATRIX.md` 与 `plan/P2_99_ACCEPTANCE_CHECKLIST.md` 已恢复 |

---

### T9-T11：State Schema / Reset

| # | 测试 | 当前状态 | 说明 |
|---|------|----------|------|
| T9 | `test_chat_state_roundtrip_preserves_decay_fields` | ✅ 已完成 | `chat_states` 已补 `last_reply_time` / `last_passive_decay_time` 落库与兼容升级 |
| T10 | `test_database_service_get_chat_state_preserves_decay_fields` | ✅ 已完成 | `DatabaseService.get_chat_state()` 已同步读取新增字段 |
| T11 | `test_get_state_persists_daily_reset_on_first_load` | ✅ 已完成 | `ChatStateService` 初次读取时已正确处理日切重置与持久化 |

---

### T12-T13：Profile Roundtrip 序列化

| # | 测试 | 当前状态 | 说明 |
|---|------|----------|------|
| T12 | `test_get_user_profile_does_not_rebuild_runtime_vector_from_social_score` | ✅ 已完成 | `relationship_vector` 已统一回填到顶层读取路径 |
| T13 | `test_relationship_vector_roundtrip_preserves_last_decay_time` | ✅ 已完成 | `profile_metadata["relationship_vector"]` 与顶层 `relationship_vector` 已同步 |

---

## 二、延期至独立窗口

以下项目仍未完成，且不应在“顺手修 bug”时混入：

### 🔴 P1：`context_compaction.py` 拆分

| 项 | 描述 |
|----|------|
| 文件 | `astrmai/conversation/attention/context_compaction.py`（约 1999 行） |
| 问题 | 单体文件同时承担压缩触发、provider 调用、摘要生成、段管理等多个职责 |
| 当前状态 | 未拆分 |
| 风险 | D6 的 provider session 损坏轮换已修，但完整重试/降级策略仍耦合在大文件中 |

---

### 🟡 P2：D27 `_get_runtime()` 迁移

| 项 | 描述 |
|----|------|
| 文件 | `astrmai/webui/backend/adapters/plugin_api.py` |
| 问题 | `_get_runtime()` 仍被 10+ accessor 调用，底层继续暴露完整 `PluginRuntimeContext` |
| 当前状态 | 未迁移 |
| 目标 | 逐 accessor 替换为 `self.facade.*` 或更窄域 accessor，最后删除 `_get_runtime()` |

---

### 🟡 P2：D24/D25 Bootstrap 方法提取

| 项 | 描述 |
|----|------|
| 文件 | `astrmai/app/bootstrap.py` |
| 问题 | `_build_core_services` 与 `_build_lifecycle_stack` 仍是长方法，包含多段服务构建与绑定 |
| 当前状态 | 已补逻辑分区注释，但未提取子方法 |
| 阻塞 | `memory_engine ↔ db_service` 双向绑定使简单提取会引入复杂返回值结构 |

---

### 🟢 P3：D18 剩余 4 个 cache reason

| 项 | 描述 |
|----|------|
| 文件 | `astrmai/infrastructure/gateway/gateway_result.py:53-77` |
| 问题 | `prefix_stable`、`provider_visible_hash_stable`、`cache_affinity_enabled`、`cached_usage_supported` 的供给链仍未完全打通 |
| 当前状态 | `explicit_cache_hint` 与 `session_reuse` 已完成，其余 4 项仍待闭环 |

---

## 三、建议级别（P3，不阻塞）

| 来源 | # | 描述 | 位置 |
|------|---|------|------|
| 窗口 4 | D40 | `DEFAULT_POLICIES` 中 `("sys2","dialog")` 存储策略与其他 lane 差 4 倍 | `lane_storage.py` |
| 窗口 4 | D42 | `model_hint` 冷却窗口不一致 | `model_router.py` |
| 窗口 3 | D19 | `trigger_phrases` 含 `"这个"` `"那个"` 死代码 | `gate.py:183-184` |
| 窗口 3 | D44 | `AttentionGate.__init__` 接受 11 个独立参数 | `gate.py:99-112` |
| 窗口 3 | D46 | `_handle_fast_wakeup` 魔法数字 `12`/`2`/`3` | `gate.py:301-314` |
| 窗口 2 | D49 | stale_drop 的 debug_trace 信息不足 | `executor.py:741` |
| 窗口 2 | D50 | wait/ignore 分支与 reply 路径收尾重复 | `planner.py:1040-1062` |
| 窗口 2 | D51 | `_adjust_expression_habits_for_behavior` 返回合并策略不够健壮 | `planner.py:1096-1097` |
| 窗口 2 | D52 | `ContinuitySnapshot` 字段膨胀约 120 字段 | `contracts/turn_context.py` |
| 窗口 7 | D62 | `_last_*` 状态字段无清理 | `proactive/` |
| 窗口 7 | D63 | 魔法数字 `999.0` | `proactive/` |
| 窗口 7 | D64 | `heartbeat.py` snapshot 迭代逻辑重复 | `heartbeat.py` |
| 窗口 6 | D66 | 5 项预存建议未修复 | `state/` |

---

## 四、确认不修（已评估）

| 来源 | # | 描述 | 原因 |
|------|---|------|------|
| 窗口 4 | D61 | `reverse_session` 死代码 | 窗口 4/10 确认保留 |
| 窗口 3 | D19 | trigger_phrases `"这个"` / `"那个"` | 低优先级，非 bug |
| 窗口 3 | D45 | `context_compaction.py` 1999 行 | 需独立重构窗口 |
| 窗口 10 | D33 | 5 个 SQL fallback | 防御性代码，运行时优先走运行时 |
| 窗口 8 | D35 | AdminUiService God Object | 延迟导入避免循环崩溃 |
| 窗口 6 | D36 | `get_active_states()` 返回可变引用 | 建议级别 |
| 窗口 6 | D37 | `_apply_decay` 时间漂移 | 累积误差极微 |
| 窗口 5 | D38 | `_init_failures` 非原子 | asyncio 单线程无竞态 |
| 窗口 5 | D39 | `v2_store.py` 拆分 | 架构重构 |
| 窗口 1 | D57 | `apply_hot_config()` fallback | 正确决策 |
| 窗口 1 | D60 | system2_bridge RuntimeError | 代码已重构，不存在 |

---

## 五、下轮优先级建议

1. `context_compaction.py` 拆分
2. `plugin_api.py` 去 `_get_runtime()`
3. Bootstrap 子方法提取
4. D18 剩余 cache reason 闭环
