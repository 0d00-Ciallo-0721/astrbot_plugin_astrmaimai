# AstrMai 脆弱点深度审计报告（第二阶段）

> 生成时间：2026-06-15 · 审计工具：`tests/manual/risk_audit/` (28/28 PASSED)
> 审计范围：7 个代码脆弱点（6.1 ~ 6.7）

---

## 目录

1. [6.1 ChatLoopKernel 状态机复杂度 (2272 行)](#61-chatloopkernel-状态机复杂度-2272-行)
2. [6.2 AttentionGate _pool_lock 全局锁竞争](#62-attentiongate-_pool_lock-全局锁竞争)
3. [6.3 Judge prompt 注入：无界消息字段](#63-judge-prompt-注入无界消息字段)
4. [6.4 Memory v2 多步迁移链一致性](#64-memory-v2-多步迁移链一致性)
5. [6.5 配置热应用：frozen dataclass 过期引用](#65-配置热应用frozen-dataclass-过期引用)
6. [6.6 _conf_schema.json 乱码字段](#66-_conf_schemajson-乱码字段)
7. [6.7 FAISS 惰性初始化指数退避](#67-faiss-惰性初始化指数退避)

---

## 6.1 ChatLoopKernel 状态机复杂度 (2272 行)

### 源码规模

| 指标 | 数值 |
|------|------|
| 文件 | `astrmai/conversation/loop/chat_loop_kernel.py` |
| 总行数 | 2272 |
| 核心类 | `ChatLoopKernel` |
| 状态机 | `_derive_phase()` — 7 条状态转换路径 |
| 决策机 | `_decide()` — 13+ 条件分支 |
| 调度器 | `_select_due_entries()` — 多策略评分 + 饥饿检测 |

### 状态转换路径 (`_derive_phase` — `chat_loop_kernel.py:1879-1895`)

```
触发动作                          → 目标阶段
─────────────────────────────────────────────────
INGRESS_MESSAGE / RESUME_WAIT     → ACTIVE
INGRESS_EXTERNAL / INTERRUPT_WAIT → ACTIVE
WAIT  (等待中)                     → WAITING
SKIP_BUSY (正忙/锁冲突)            → BUSY
COMPACTION_EVALUATE               → MAINTENANCE
MEMORY_MAINTENANCE / DREAM        → MAINTENANCE
NOOP (cooldown_blocked)           → COOLDOWN
NOOP (maintenance_budget_blocked) → MAINTENANCE 或 IDLE
fallthrough                       → IDLE
```

### 决策分支 (`_decide` — `chat_loop_kernel.py:1460-1583`)

| 触发类型 | 子条件数 | 决策输出 |
|---------|---------|---------|
| `INGRESS_MESSAGE` | 3 | RESUME_WAIT / INTERRUPT_WAIT / INGRESS_MESSAGE |
| `INGRESS_EXTERNAL` | 1 | INGRESS_EXTERNAL |
| Heartbeat (9 条子路径) | 4+4+... | SKIP_BUSY / WAIT / PROACTIVE_WAKEUP / HEARTFLOW_EVALUATE / COMPACTION / MEMORY / DREAM / NOOP |

### 潜在未测试路径

**DREAM_MAINTENANCE 调度路径**：`_decide()` 中存在 `DREAM_MAINTENANCE` 的调度分支，但现有测试仅覆盖其节流行为（`test_dream_summary_marks_global_throttle_reason`），未显式断言该路径被选中并正确 dispatch。

**COOLDOWN 阶段映射**：`_derive_phase` 中 `NOOP + cooldown_blocked → COOLDOWN` 的映射，现有测试未显式验证阶段输出为 `COOLDOWN`（仅验证了 NOOP 决策本身）。

### 触发概率：极低

状态机 bug 需要特定的边界条件组合（如恰好 cooldown 过期 + 消息到达 + 梦循环触发同时发生），日常运行中概率极低。但一旦触发，影响是**消息静默丢失或被路由到错误处理器**。

### 影响评估

| 场景 | 后果 |
|------|------|
| 状态机漏掉某触发类型 | 消息进入 fallthrough → IDLE，永久等待 |
| 阶段映射错误 | 消息被路由到错误 handler（如对话消息触发维护逻辑）|
| 调度评分边界 | 某群永久饿死或某群霸占总调度槽位 |

### 测试证据

`test_risk_chat_loop_state_machine.py::test_derive_phase_has_7_paths` — PASSED — 确认 7 条路径存在。

`test_risk_chat_loop_state_machine.py::test_decide_has_ingress_message_branch` — PASSED — 确认决策分支完整。

`test_risk_chat_loop_state_machine.py::test_dream_maintenance_path_exists` — PASSED — DREAM_MAINTENANCE 路径存在但缺少显式 dispatch 测试。

`test_risk_chat_loop_state_machine.py::test_state_machine_lines_of_code` — PASSED — 确认 2272 行规模。

---

## 6.2 AttentionGate _pool_lock 全局锁竞争

### 锁的位置

**代码位置**：`astrmai/conversation/attention/gate.py:82`

```python
self._pool_lock = asyncio.Lock()
```

### 临界区分析 (`_get_or_create_session` — `gate.py:104-115`)

```python
async def _get_or_create_session(self, chat_id: str) -> SessionContext:
    async with self._pool_lock:                          # ← 获取全局锁
        session = self.focus_pools.get(chat_id)           # O(1) dict 查找
        if session is None:
            session = SessionContext()                     # 对象创建
            session.last_message_hash = ""                 # 字段赋值
            session.repeat_count = 0
            session.last_active_user_time = 0.0
            session.last_window_open_ts = 0.0
            self.focus_pools[chat_id] = session           # O(1) dict 插入
        session.last_active_time = time.time()            # 时间戳更新
        return session                                    # ← 释放全局锁
```

### 锁竞争量化

| 并发调用方 | 每个 chat_id 调用频率 | 锁持有时间 | 竞争概率 |
|-----------|---------------------|-----------|---------|
| `process_event()` | 每条消息 1 次 | ~1 µs | 极低 |
| `_record_event_activity()` | 每条消息 1 次 | ~1 µs | 极低 |
| `inject_external_event()` | 每个外部事件 1 次 | ~1 µs | 极低 |
| `_resume_fast_wakeup()` | 每次强唤醒 1 次 | ~1 µs | 极低 |

**结论：临界区内只有 dict 操作和时间戳更新，完全无 I/O。O(1) 复杂度 + µs 级持锁 = 不存在真正的瓶颈风险。**

### 触发概率：极低

即使在 100+ 群聊同时活跃的场景下，asyncio 的协作式调度也能在微秒级完成锁的获取和释放。这不是一个真正的性能瓶颈。

### 测试证据

`test_risk_attention_gate_pool_lock.py::test_pool_lock_is_single_global_lock` — PASSED — 确认单个全局锁。

`test_risk_attention_gate_pool_lock.py::test_critical_section_is_constant_time` — PASSED — **临界区内无 await**（无 I/O）。

`test_risk_attention_gate_pool_lock.py::test_measure_lock_contention_with_concurrent_callers` — PASSED — 50 并发调用在 <2s 完成。

---

## 6.3 Judge prompt 注入：无界消息字段

### 数据流

```
用户发送消息（任意长度）
  ↓
AttentionGate.process_event()
  ↓  focus_event.message_str = 用户原始消息
Judge.evaluate(chat_id, message, ...)
  ↓  message 参数无长度检查
prompt = f"""
  【近期发生的连续对话】:
  {message}                         ← ★ 直接注入到 LLM prompt
"""
  ↓
LLM 调用（prompt 长度 = 用户消息长度 + 约 1600 字符开销）
```

### 源码证据

**`judge.py:269-270`** — 用户消息直接注入：
```python
【近期发生的连续对话 (请重点基于以上历史语境和以下近期对话进行最终裁决)】:
{message}
```

**`judge.py:250`** — persona_summary 同样无界：
```python
[你的核心人设]: {persona_summary if persona_summary else '保持你原本的性格特征'}
```

**`judge.py:132-133`** — 历史记录有截断（作为对比）：
```python
# 历史记录被截断到 60 字符
clean_content = self._flatten_history_content(raw_content).strip()
lines.append(f"... {clean_content[:60]}")
```

### 攻击向量

| 攻击方式 | 严重度 | 说明 |
|---------|--------|------|
| 超长消息注入 | ⚠️ 中 | 10K+ 字符消息撑爆 LLM context window，可能触发 token 超额错误 |
| Prompt 注入 | ⚠️ 中 | 用户在消息中嵌入 "Ignore all previous instructions..." 可劫持 Judge 决策 |
| Token 消耗 | 🟡 低 | 超长消息消耗大量 token，增加 API 费用 |

### 保护缺失

- ❌ `evaluate()` 不检查 `len(message)`
- ❌ 无消息截断（与历史记录的 `[:60]` 截断形成对比）
- ❌ 无 `persona_summary` 长度限制
- ✅ `_build_dynamic_actions()` 有界（最多 5 个 action 追加）
- ✅ 历史记录有 60 字符截断保护
- ✅ `_flatten_history_content` 对消息组件有深度限制（`depth > 3 → truncate`）

### 触发概率：中

任何用户都可以发送超长消息。Prompt 注入需要恶意意图，但技术上完全可行。

### 测试证据

`test_risk_judge_prompt_injection.py::test_message_field_no_length_limit` — PASSED — `{message}` 直接注入，无长度检查。

`test_risk_judge_prompt_injection.py::test_evaluate_has_no_input_validation` — PASSED — `len(message)` 未被检查。

`test_risk_judge_prompt_injection.py::test_history_truncation_exists` — PASSED — 历史有截断但消息没有。

`test_risk_judge_prompt_injection.py::test_calculate_max_prompt_size` — PASSED — 消息注入路径确认。

---

## 6.4 Memory v2 多步迁移链一致性

### 迁移步骤顺序

**代码位置**：`astrmai/memory/services/memory_engine.py:166-186` (`initialize()`)

```
Step  1: await self.v2_store.initialize()           ← SQL 表创建 | ❌ 无外层 try
Step  2-9: 子组件构造函数赋值 (8 个 assign)           ← 纯 Python | N/A
Step 10: await self.v2_store.import_legacy_documents()   ← ✅ 内部 try/except
Step 11: await self.v2_store.import_persona_cache()      ← ✅ 内部 try/except
Step 12: await self.import_legacy_memory_events()         ← ✅ 内部 try/except
Step 13: await self.import_legacy_jargons()               ← ✅ 内部 try/except
Step 14: await self.import_legacy_expression_patterns()   ← ✅ 内部 try/except
Step 15: bm25_retriever = BM25Retriever(self.db_path)    ← ❌ 无 try/except
Step 16: await self.bm25_retriever.initialize()           ← ❌ 无 try/except
Step 17: index projector rebuild (条件性)                 ← ⚠️ 部分保护
```

### 错误传播分析

| 失败点 | 是否阻断后续步骤 | 遗留状态 |
|--------|----------------|---------|
| Step 1 (v2_store.init) | **是** — 整条链终止 | memory_engine 部分初始化 |
| Step 10-14 (各 import) | **否** — 内部 catch + 记录 "failed" | 该步骤数据缺失，其他正常 |
| Step 15 (BM25Retriever) | **是** — 整条链终止 | 无 BM25 回退，hybrid 检索退化 |
| Step 16 (bm25.init) | **是** — 整条链终止 | 同上 |

### 不一致场景

**场景 A**：Steps 1-12 成功，Step 13 (import_legacy_jargons) 内部失败
- 结果：`memory_v2_migrations` 表记录 `2_jargon_import = "failed"`
- 影响：黑话数据未迁移，但不影响记忆和表达式检索

**场景 B**：Step 15 (BM25Retriever) 构造函数抛异常
- 结果：`initialize()` 整体失败，异常传播到 `PluginLifecycleManager`
- 影响：`memory_initialized = False`，memory engine 标记为 degraded
- 但此时 Steps 10-14 的数据已写入 v2_store！下次重新初始化会跳过（migration_applied 检查）

**场景 C**：Step 14 和 Step 15 之间的崩溃
- 结果：数据在 v2_store 中，但 memory_engine 未标记为 ready
- 下次 reboot 时，migration 检查 `import_legacy_expression_patterns` 已完成 → 跳过
- BM25Retriever 重新初始化 → 成功
- 影响：无数据丢失，但首次启动不完整

### 触发概率：低

Steps 10-14 都有内部 try/except 保护，实际失败率极低。Step 15/16 的 BM25Retriever 失败概率也极低（只是文件读取 + tokenization）。

### 测试证据

`test_risk_memory_v2_migration.py::test_initialize_runs_sequential_migration_steps` — PASSED — 5 步迁移全部存在。

`test_risk_memory_v2_migration.py::test_migration_steps_have_internal_try_except` — PASSED — Steps 10-14 有内部保护。

`test_risk_memory_v2_migration.py::test_v2_store_initialize_has_no_outer_try_except` — PASSED — **无外层保护**。

`test_risk_memory_v2_migration.py::test_bm25_retriever_init_can_fail` — PASSED — BM25 init 无保护。

`test_risk_memory_v2_migration.py::test_migration_failure_records_status` — PASSED — 失败记录为 "failed"。

---

## 6.5 配置热应用：frozen dataclass 过期引用

### 数据流

```
启动时 (bootstrap.py:157-163):
  settings = build_infrastructure_settings(config)
  ↓
  runtime.infrastructure_settings = settings           ← frozen dataclass 实例 A
  ↓
  gateway = GlobalModelGateway(context, config, settings=settings.gateway)
  ↓  gateway.settings = settings.gateway              ← 保存引用到 GatewaySettings 实例 A.gateway

  lane_manager = LaneManager(..., settings=settings.lane)
  ↓  lane_manager.settings = settings.lane            ← 保存引用到 LaneRuntimeSettings 实例 A.lane

─── 时间流逝，用户通过 WebUI 修改配置 ───

热应用时 (plugin_facade.py:83-93):
  apply_hot_config(config_dict, parsed_config):
    runtime.raw_config = config_dict
    runtime.config = parsed_config
    runtime.rebuild_infrastructure_settings()          ← 创建全新 frozen 实例 B
    ↓
    runtime.infrastructure_settings = build_infrastructure_settings(config)
    ↓
    runtime.infrastructure_settings is now 实例 B      ← ★ 但 gateway.settings 仍指向 实例 A.gateway！
```

### 过期引用清单

| 对象 | 引用位置 | 过期后果 |
|------|---------|---------|
| `GlobalModelGateway.settings` | `model_gateway.py:31` | `max_concurrent_llm_calls` 不更新 → semaphore 仍用旧值 |
| `gateway._global_semaphore` | `model_gateway.py:36` | `asyncio.Semaphore(old_max)` — 永不更新 |
| `LaneManager.settings` | `lane_manager.py:64` | lane 配置热应用后不生效 |
| `gateway.context_economy` benchmark ref | `model_gateway.py:33` | benchmark store 引用可能指向旧路径 |

### 仍然保持新鲜的引用

| 对象 | 为什么 |
|------|--------|
| `runtime.feature_flags` | `@property` — 每次访问读取 `self.infrastructure_settings.features` |
| `runtime.build_diagnostics()` | 直接访问 `self.infrastructure_settings.gateway.X` |
| `context_engine.prefix_caching_enabled` | 从 `self.config.conversation` 读取 — 走 config 而非 settings |

### 实际影响

| 配置变更 | 是否即时生效 | 备注 |
|---------|------------|------|
| `max_concurrent_llm_calls` 从 3 → 10 | **否** | Semaphore 创建于初始化时，不会重建 |
| `debug_mode` 切换 | **否**（Gateway）| Gateway 有自己的 settings 副本 |
| `debug_mode` 切换 | **是**（Runtime）| Runtime 通过 property 动态读取 |
| `enable_work_mode` | **是** | feature_flags 属性动态读取 |
| Model pool 列表变更 | **是** | 通过 config.provider 访问，不走 settings |
| `backoff_factor` / `llm_retries` | **否** | Gateway 持有自己的 settings 快照 |

### 触发概率：中

任何通过 WebUI 修改 `max_concurrent_llm_calls` / `backoff_factor` 等 gateway 级配置时必然触发。功能级开关 (work_mode/vision/dialogue_store) 不受影响。

### 测试证据

`test_risk_config_hot_apply.py::test_infrastructure_settings_is_frozen_dataclass` — PASSED — `frozen=True` 确认。

`test_risk_config_hot_apply.py::test_rebuild_creates_new_instance` — PASSED — 每次 `build_infrastructure_settings()` 创建新实例。

`test_risk_config_hot_apply.py::test_gateway_holds_its_own_settings_copy` — PASSED — Gateway 保存自己的快照。

`test_risk_config_hot_apply.py::test_semaphore_not_recreated_on_hot_apply` — PASSED — Semaphore 不更新。

`test_risk_config_hot_apply.py::test_feature_flags_property_always_fresh` — PASSED — feature_flags 正确动态读取。

---

## 6.6 _conf_schema.json 乱码字段

### 根因

JSON 文件中的中文字段描述经历了 **UTF-8 → Latin-1 双重编码**，导致显示为乱码 (mojibake)。

### 受影响字段（9 个）

| 文件行号 | 字段 | 乱码内容示例 |
|---------|------|------------|
| 438 | `memory.deep_temporal_alpha` | `鏃堕棿浠叉潈淇濆簳绯绘暟` |
| 443 | `memory.deep_temporal_tau_seconds` | `鏃堕棿琛板噺褰掍竴鍖栫獥鍙ｏ紙绉掞級` |
| 448 | `memory.deep_temporal_lambda_default` | `鏅€氳蹇嗙殑 deep retrieval 鏃堕棿琛板噺寮哄害` |
| 453 | `memory.deep_temporal_lambda_fact` | `fact 绫昏蹇嗙殑 deep retrieval 缂撹...` |
| 458 | `memory.deep_temporal_candidate_pool_factor` | `鍊欓€夋睜鏀惧ぇ鍊嶆暟` |
| 463 | `memory.deep_temporal_candidate_pool_min` | `鍊欓€夋睜鏈€灏忔潯鏁?` |
| 468 | `memory.deep_temporal_llm_window` | `缁忔湰鍦版椂闂翠话瑁佸悗...` |
| 473 | `memory.maintenance_hot_beta` | `鐑害鍒嗙殑鏃堕棿涓庤闂粺璁℃潈閲?` |
| 478 | `memory.maintenance_temporal_stale_hot_threshold` | `鍩轰簬鐑害鎵规爣 stale 鐨勯槇鍊?` |

### 影响

- **用户无法理解配置含义**：WebUI 配置面板中这 9 个字段的 description 显示为乱码
- **不影响功能**：字段 key 和 default 值正常，只是描述文本损坏
- **修复难度**：需要找到原始中文文本并重新编码保存为正确的 UTF-8

### 触发概率：100%

每个用户打开 WebUI 配置面板时，这 9 个字段的描述必然显示为乱码。

### 测试证据

`test_risk_conf_schema_garbled.py::test_memory_section_has_garbled_text_detected` — PASSED — **确认 9 个字段存在乱码**。

`test_risk_conf_schema_garbled.py::test_count_garbled_fields` — PASSED — 全局扫描统计乱码字段数。

---

## 6.7 FAISS 惰性初始化指数退避

### 退避算法

**代码位置**：`astrmai/memory/services/memory_engine.py:180-212` (`_ensure_faiss_initialized`)

```python
backoff = min(3600, 30 * (2 ** (self._init_failures - 1)))
self._next_retry_time = now + backoff
```

### 退避时间线

| 失败次数 | 退避时间 | 累计不可用时间 |
|---------|---------|-------------|
| 1 | 30s | 30s |
| 2 | 60s | 1m30s |
| 3 | 120s | 3m30s |
| 4 | 240s | 7m30s |
| 5 | 480s | 15m30s |
| 6 | 960s | 31m30s |
| 7 | 1920s | 1h3m |
| 8 | 3600s (cap) | 2h3m |
| 9+ | 3600s (cap) | 每次+1h |

### 触发条件

| 条件 | 说明 |
|------|------|
| `faiss-cpu` 未安装 | `HAS_FAISS = False` → **直接返回 False，退避 86400s** |
| embedding provider 找不到 | `get_provider_by_id(model_id)` 返回 None → 退避指数增长 |
| FaissVecDB 初始化抛异常 | 构造函数失败 → 退避指数增长 |

### 退避上限

- **faiss 未安装**：`86400s`（24 小时）— 几乎永久
- **provider 找不到**：`3600s`（1 小时）上限
- **初始化失败**：`3600s`（1 小时）上限

### 与 4.3 的关系

此风险点 (6.7) 与第一阶段的 4.3 (FAISS 降级静默) 是同一根因的不同视角：
- **4.3** 关注「降级后调用方无感知」
- **6.7** 关注「退避时间过长导致长时间不可用」

两者叠加导致：即使 provider 恢复可用，系统也要等退避过期才重试。如果在退避窗口内 provider 恢复了，用户仍然享受不到向量检索能力。

### 触发概率：中

与 4.3 相同——未配置 embedding 模型、provider 故障、faiss 未安装都会触发。

### 测试证据

已在第一阶段测试中覆盖：
- `test_risk_faiss_silent_degradation.py::test_faiss_retry_backoff_caps_at_3600s` — PASSED — 确认退避上限。


## 综合风险评估矩阵

| # | 风险 | 触发概率 | 影响面 | 严重度 | 类型 |
|---|------|---------|--------|--------|------|
| 6.1 | ChatLoopKernel 状态机复杂度 | 极低 | 消息路由 | 🟡 低 | 设计复杂度 |
| 6.2 | AttentionGate 锁竞争 | 极低 | 并发性能 | 🟢 无 | **误报** — 临界区 O(1) 无 I/O |
| 6.3 | Judge prompt 注入 | **中** | LLM 上下文 | ⚠️ 中 | 输入校验缺失 |
| 6.4 | Memory v2 迁移链 | 低 | 数据完整性 | 🟡 低 | 错误处理不完整 |
| 6.5 | 配置热应用过期引用 | **中** | Gateway/Lane 配置 | ⚠️ 中 | 架构设计问题 |
| 6.6 | _conf_schema.json 乱码 | **100%** | WebUI 可读性 | 🟡 低 | 编码错误 |
| 6.7 | FAISS 退避过长 | **中** | 向量检索 | 🔴 高 | 同 4.3 |

## 修复优先级

| 优先级 | 风险 | 修复方案 | 改动量 |
|--------|------|---------|--------|
| 🔴 P0 | 6.7+4.3 FAISS 降级 | 降级时向上层传递状态标记 + 缩短退避时间 | 小 |
| ⚠️ P1 | 6.5 热应用过期引用 | Gateway/LaneManager 不存 settings 副本，直接读 runtime | 中 |
| ⚠️ P1 | 6.3 Judge prompt 注入 | 消息截断到 500 字符 + persona_summary 限制 | 小 |
| 🟡 P2 | 6.6 _conf_schema 乱码 | 修复 9 个字段的 UTF-8 编码 | 极小 |
| 🟡 P2 | 6.4 迁移链 | 为 v2_store.init 和 BM25 init 增加外层 try/except | 小 |
| 🟡 P3 | 6.1 状态机覆盖 | 补充 DREAM_MAINTENANCE 和 COOLDOWN 映射测试 | 小 |
| 🟢 无需 | 6.2 锁竞争 | **不是真实瓶颈** — 临界区 O(1) 无 I/O | 不修 |
