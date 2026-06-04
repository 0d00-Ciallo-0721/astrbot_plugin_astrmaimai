# 本轮会话修复清单

> 会话范围：窗口 4 Gateway/Provider/Runtime + 技术债务 Phase 1-3 + 窗口 1 残差 + 测试修复  
> 测试结果：692 passed / 12 failed / 1 skipped（起始 686/18 → 终态 692/12，**+6 通过，−6 失败**）

---

## 一、窗口 4：Gateway/Provider/Runtime（9 项）

### 🔴 V1 未修复（5 项 → 全部修复）

| # | 修复项 | 文件:行 | 改动 |
|---|--------|---------|------|
| D1 | `_record_event_request_trace` bool-sticky 陷阱 | `gateway_lane.py:45-55` | `_reuse_or_hash` helper → `is not None` 替代 `or`；`request_session_id` 显式 `None` 检查 |
| D2 | `_runtime_meta` asyncio.Lock 保护 | `lane_manager.py:68,124-182` + `lane_storage.py:104,142` | 新增 `_meta_lock`；`_rotation_reason`/`_should_rotate`/`get_runtime_meta` → async；4 处 `_runtime_meta` 读写包裹 `async with` |
| D3 | `_remote_sessions` 无限增长 | `lane_manager.py:66-68,145-165` | 值改为 `Tuple[str, float]`；惰性 TTL 清理（3600s TTL, 300s 间隔） |
| D4 | lane 旋转后无 terminate 信号 | `lane_storage.py:67-74` | 旋转时调 `expire_remote_sessions_for_lane` + warning 日志 |
| D5 | `_sticky_primary` 无限增长 | `model_router.py:67-68,249-258` | `OrderedDict` + `maxsize=256` + `move_to_end` LRU + FIFO 淘汰 |

### 🟡 本轮新发现（4 项 → 全部修复）

| # | 修复项 | 文件:行 | 改动 |
|---|--------|---------|------|
| D14 | `_rotation_reason` 无锁读取 | `lane_manager.py:124` | `async with self._meta_lock` 包裹 `.get()` + 锁外安全注释 |
| D15 | `get_runtime_meta` 快照过期 | `lane_manager.py:182` | `async with self._meta_lock` 包裹 |
| D16 | `model_id` 回退不精确 | `gateway_lane.py:251-258` | `model_hint` 改用 cooldown 过滤后的 `attempt_queue[0]` + 诊断 warning |
| D17 | `chat_in_lane_result` 无冷却过滤 | `gateway_lane.py:197-210` | 添加 `_build_attempt_queue` + `_filter_cooldown_attempt_queue` |

---

## 二、技术债务 Phase 1：低风险快速修复（6 项）

| # | 修复项 | 文件:行 | 改动 |
|---|--------|---------|------|
| D18 | `_build_cache_observation` 数据源 | `gateway_call.py:214-217` + `gateway_lane.py:462-466,545-549` | `request_session_id`/`request_cache_control` 注入 `log_meta`，启发 `explicit_cache_hint` + `session_reuse` |
| D20 | `del event` → `_ = ...` | `gate.py:458,472,500` | 3 处 `del event`/`del bot_name, event` → `_ = event`/`_ = (bot_name, event)` + 接口一致性注释 |
| D21 | `_build_warm_quotes` O(n) | `group_dialogue_store.py:547-549` | `reversed(segments)` → `reversed(segments[-64:])`，固定窗口 |
| D22 | continuity `lightweight_event` 文档化 | `conversation_continuity.py:212-216` | 添加设计意图注释，标记为有意设计非 bug |
| D41 | 冷却过滤残差审计 | — | 审计 6 条 `_filter_cooldown_attempt_queue` 调用路径，chat/tool_chat 已对齐 |
| D43 | chat trace 冷却跳过记录 | `gateway_lane.py:281-293` | `append_trace_stage` 追加 `skipped_cooldown_models`/`cooldown_overridden` |

---

## 三、技术债务 Phase 2：Compaction 🔴（2 项）

| # | 修复项 | 文件:行 | 改动 |
|---|--------|---------|------|
| D6 | compaction session 损坏轮换 | `compaction_providers.py:165-171,267-273` | v1+v2 异常处理中调 `expire_remote_sessions_for_lane` |
| D7 | AttentionGate `context_compaction` None | `gate.py:86-90` | `__init__` 中 `logger.warning` 当 None |

---

## 四、技术债务 Phase 3：Bootstrap/Facade（4 项）

| # | 修复项 | 文件:行 | 改动 |
|---|--------|---------|------|
| D24 | `_build_core_services` | `bootstrap.py:103-178` | 8 段逻辑分区注释（持久化/DB → Gateway/Lane → Memory → State/Dialogue → Compaction → Judge/Sensors/Vision） |
| D25 | `_build_lifecycle_stack` | `bootstrap.py:301-360` | 2 段逻辑分区注释（Reflection → Scheduled Tasks） |
| D26 | MemoryEngine ↔ DatabaseService | `bootstrap.py:126-129` | 双向绑定设计文档化 + 重构方向注释 |
| D27 | `_get_runtime()` | — | ⏭️ 延期（10+ 文件，需独立窗口） |

---

## 五、测试修复（6 项）

| # | 测试 | 文件 | 修复方式 |
|---|------|------|----------|
| T1 | `test_diary_service_writes_cognitive_feedback` | `tests/test_cognitive_feedback_refactor.py:236` | 添加 `_FakePromptRegistry` mock |
| T2 | `test_dream_scheduler_writes_cognitive_feedback` | `tests/test_cognitive_feedback_refactor.py:197` | `run_dream_cycle` 添加 `session_id=None` 参数 |
| T3 | `test_memory_engine_records_feedback_in_cache_and_filters_recall` | `tests/test_cognitive_feedback_refactor.py:89-112` | mock `write_service` + `retrieval_service` + `render_recall` |
| T6 | `test_update_mood_keeps_delta_contract` | `tests/test_state_services_refactor.py:55` | `state` 补全 `energy`/`last_reply_time`/`last_passive_decay_time`；CAS 路径断言适配 |
| T7 | `test_memory_engine_recall_accepts_and_forwards_top_k` | `tests/regression/persistence/test_persistence_regressions_migrated.py:125` | mock `retrieval_service.retrieve` + `MemoryQuery` 参数追踪；断言适配 |
| T8 | `test_run_once_persists_chat_decay_and_unifies_relationship_truth` | `tests/regression/state/test_decay_service_migrated.py:56` | 显式 `save_chat_state`/`save_user_profile`；断言适配新 CAS 路径 |

---

## 六、窗口 1 残差（2 项）

| # | 修复项 | 文件:行 | 改动 |
|---|--------|---------|------|
| D58 | PEP 8 空行 | `main.py:114` | `inject_gemini_reverse_session` 后补充空行 |
| D59 | hasattr 私有方法 | `sensors.py:21-23` + `lifecycle.py:61-64` | `_load_foreign_commands` → 公开 `load_foreign_commands()`；调用方改用公开方法 |

---

## 七、改动文件汇总

```
astrmai/app/bootstrap.py                        +30  (分段注释 + D26 文档化)
astrmai/app/lifecycle.py                         +1  (D59 hasattr→公开方法)
astrmai/conversation/attention/compaction_providers.py  +12  (D6 session 轮换)
astrmai/conversation/attention/gate.py           +11  (D7 warning + D20 del→_=)
astrmai/conversation/attention/group_dialogue_store.py  +4  (D21 O(n)→O(1))
astrmai/conversation/ingress/sensors.py           +4  (D59 公开接口)
astrmai/conversation/planning/conversation_continuity.py  +4  (D22 注释)
astrmai/infrastructure/gateway/gateway_call.py    +4  (D18 cache meta)
astrmai/infrastructure/gateway/gateway_lane.py    +106/-30  (D1/D16/D17/D18/D43)
astrmai/infrastructure/gateway/model_router.py    +9  (D5 LRU)
astrmai/infrastructure/runtime/lane_manager.py    +56  (D2/D3/D4/D14/D15)
astrmai/infrastructure/runtime/lane_storage.py    +65/-32  (D2/D4/D5)
main.py                                            +1  (D58 PEP 8)
tests/test_cognitive_feedback_refactor.py         +28  (T1-T3)
tests/test_state_services_refactor.py              +8  (T6)
tests/regression/persistence/test_persistence_regressions_migrated.py  +16  (T7)
tests/regression/state/test_decay_service_migrated.py  (T8)

总计: 17 个文件, +359/-62 行
```

---

## 八、测试结果变化

| 指标 | 起始 | 终态 | 变化 |
|------|:----:|:----:|:----:|
| 通过 | 686 | **692** | +6 |
| 失败 | 18 | **12** | −6 |
| 跳过 | 1 | 1 | — |

**0 新增回归。**
