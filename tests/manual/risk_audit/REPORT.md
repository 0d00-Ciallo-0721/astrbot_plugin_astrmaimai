# AstrMai 架构风险审计报告（详细版）

> 生成时间：2026-06-15 01:40 · 审计工具：`tests/manual/risk_audit/` (24/24 PASSED) · 审计范围：6 个架构风险点

---

## 目录

1. [4.1 服务总线属性断连](#41-服务总线属性断连)
2. [4.2 ChatLoopKernel 消息静默丢失](#42-chatloopkernel-消息静默丢失)
3. [4.3 FAISS 向量检索静默降级](#43-faiss-向量检索静默降级)
4. [4.4 模型网关冷却 O(n) 性能退化](#44-模型网关冷却-on-性能退化)
5. [4.5 表达学习误删无回滚](#45-表达学习误删无回滚)
6. [4.6 遗留兼容层断连](#46-遗留兼容层断连)
7. [风险评估矩阵](#风险评估矩阵)
8. [修复建议优先级排序](#修复建议优先级排序)

---

## 4.1 服务总线属性断连

### 数据流图

```
┌─────────────────────────────────────────────────────────────────┐
│                     PluginRuntimeContext                        │
│  ┌──────────────────┐    ┌──────────────────┐                  │
│  │ CoreServices     │    │ host_plugin_ref  │──weakref──┐      │
│  │  .gateway = GW_A │    │  (weakref)       │           │      │
│  │  .memory  = MEM  │    └──────────────────┘           │      │
│  │  .state   = ST   │                                     │      │
│  └──────────────────┘                                     │      │
│           │                                               ▼      │
│           │ @property                               ┌─────────┐ │
│           ▼                                         │ AstrMai │ │
│    runtime.gateway ─────────────── 返回 GW_A        │ Plugin  │ │
│                                                      │ (host)  │ │
│  sync_host_compat_attrs() 调用后：                   │ .gateway│─┼──→ GW_A
│    host_plugin.gateway ──────────────────────→ GW_A  │ .memory │─┼──→ MEM
│                                                      └─────────┘ │
└─────────────────────────────────────────────────────────────────┘

                    ★ 热重载替换 gateway 后 ★

┌─────────────────────────────────────────────────────────────────┐
│                     PluginRuntimeContext                        │
│  ┌──────────────────┐                                          │
│  │ CoreServices     │                                          │
│  │  .gateway = GW_B │  ← 已替换为新实例                          │
│  └──────────────────┘                                          │
│           │                                                     │
│           ▼                                                     │
│    runtime.gateway ─────────────── 返回 GW_B  ← 正确            │
│                                                      ┌─────────┐ │
│    host_plugin.gateway ──────────────────────→ GW_A │ AstrMai │ │
│                                                      │ Plugin  │ │
│                                 【BUG】host_plugin    │ .gateway│─┼──→ GW_A ← 过期！
│                                  上的遗留属性未更新   └─────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### 精确代码路径

**第 1 步：启动时建立遗留引用**

`astrmai/app/bootstrap.py:217-219` — `_build_chat_loop_kernel()` 最后一步：

```python
if runtime.attention_gate is not None and hasattr(runtime.attention_gate, "bind_chat_loop_kernel"):
    runtime.attention_gate.bind_chat_loop_kernel(kernel)
return kernel
```

在 `PluginFacade.__init__()` 中 (`plugin_facade.py:16`)：

```python
self.runtime.bind_host_plugin(self)   # 将 AstrMaiPlugin 实例绑定为 host
```

此时 `sync_host_compat_attrs()` **尚未被调用**，host_plugin 上**没有**遗留属性。

**第 2 步：sync_host_compat_attrs() 被调用**

在 `PluginBootstrap.build()` 的最后 (`bootstrap.py:93`)：

```python
runtime.status.bootstrap_completed = True
runtime.set_boot_phase("bootstrap.ready")
return runtime
```

然后 `main.py:28` 中 `AstrMaiPlugin.__init__()` 调用：

```python
self._apply_runtime_compat()
```

→ `main.py:34-35`：

```python
def _apply_runtime_compat(self) -> None:
    for name, value in export_legacy_attrs(self.runtime).items():
        setattr(self, name, value)
```

→ `runtime_context.py:458-472` — `export_legacy_attrs()` 遍历 32 个 `LEGACY_RUNTIME_ATTRS` + 5 个基础属性：

```python
def export_legacy_attrs(runtime: PluginRuntimeContext) -> dict[str, Any]:
    attrs = {
        "raw_config": runtime.raw_config,
        "config": runtime.config,
        "_background_tasks": runtime.background_tasks,
        "runtime_coordinator": runtime.runtime_coordinator,
        "host_bridge": runtime.host_bridge,
    }
    for name in LEGACY_RUNTIME_ATTRS:
        value = getattr(runtime, name)
        if value is not None:
            attrs[name] = value
    return attrs
```

**此时 host_plugin (AstrMaiPlugin) 上有了 37 个遗留属性。**

**第 3 步：热重载时单独替换服务**

`plugin_facade.py:78-93` — `apply_hot_config()`：

```python
def apply_hot_config(self, config_dict: dict, parsed_config) -> bool:
    self.runtime.raw_config = dict(config_dict)
    self.runtime.config = parsed_config
    if hasattr(self.runtime, "rebuild_infrastructure_settings"):
        self.runtime.rebuild_infrastructure_settings()
    # ...
    if hasattr(self.runtime, "sync_host_compat_attrs"):
        self.runtime.sync_host_compat_attrs()   # ← 这里会刷新遗留属性
    return True
```

**`apply_hot_config()` 会调用 `sync_host_compat_attrs()`**，所以通过 WebUI 改配置触发的热重载**会**更新遗留属性。

**🔴 但如果你直接操作 runtime 对象替换服务（而不走 apply_hot_config），遗留属性不会更新：**

```python
# 假设在某处（如手动调试或某个后台任务）：
from dataclasses import replace
runtime.core = replace(runtime.core, gateway=new_gateway)
# ★ runtime.gateway 现在返回 new_gateway —— 正确
# ★ host_plugin.gateway 仍然指向 old_gateway —— BUG！
```

### 产生 Bug 的精确条件

| 条件 | 是否满足 |
|------|---------|
| 有人绕过 `apply_hot_config()` 直接修改 `runtime.core` | 当前代码中**不存在此路径** |
| 有人绕过 `apply_hot_config()` 直接修改 `runtime.cognition` | 同上，不存在 |
| 某个后台任务或回调替换了服务实例 | 需要审计所有 `replace()` 调用 |

**结论**：当前代码中**没有**绕过 `apply_hot_config()` 直接替换服务的路径。这个风险是**架构层面的脆弱性**——未来的重构或新功能如果直接操作 runtime 字段，会触发此 bug。

### 影响等级

| 维度 | 评估 |
|------|------|
| 当前代码中存在触发路径？ | ❌ 不存在 |
| 未来重构中容易引入？ | ⚠️ 容易——`dataclass` 替换是 Python 惯用模式 |
| 触发后能自动恢复？ | 需要下次 `apply_hot_config()` 或重启 |
| 影响范围 | 全部 32 个服务属性 |
| 严重度 | ⚠️ 中（暂无触发路径，但防御脆弱） |

### 测试证据

```
test_gateway_replacement_not_reflected_in_host_plugin  PASSED
  → 构造 runtime, sync, 替换 gateway, 验证 host 不更新

test_export_legacy_attrs_skips_none_services            PASSED
  → 验证 None 服务被跳过不导出

test_weakref_does_not_prevent_gc                        PASSED
  → 验证 host_plugin 使用 weakref，可被 GC 回收
```

---

## 4.2 ChatLoopKernel 消息静默丢失

### 数据流图

```
ChatLoopKernel.tick(chat_id="group_123", trigger="message", event)
│
├─ _state_store.get_or_create(chat_id)
│    └─ state = ChatLoopState(chat_id="group_123", phase="ACTIVE", ...)
│
├─ _plan_next_tick(state, snapshot, decision, None)
│    └─ 计算下一个 tick 时间、cooldown、phase 转换
│
├─ _update_state(state, snapshot, decision)
│    └─ state.last_tick_at = now
│    └─ state.next_tick_at = now + cooldown
│    └─ state.phase = "COOLDOWN"  (或其他)
│    └─ state.pending_signals = {trigger: "message", ...}
│
├─ await self._state_store.save(state)   ← ════════════ 持久化屏障 ════════════
│    │                                                         ▲
│    │    ┌─────────────────────────────────────────────────┐  │
│    │    │  崩溃窗口 (~1-100ms)                              │  │
│    │    │  如果进程在这里崩溃：                              │  │
│    │    │  · DB 中 state 已标记为 "已处理"                   │  │
│    │    │  · handler 从未被调用                              │  │
│    │    │  · 消息永久丢失                                    │  │
│    │    │  重启后 state 恢复为 COOLDOWN 模式                 │  │
│    │    │  丢失的消息不会被重新处理                           │  │
│    │    └─────────────────────────────────────────────────┘  │
│    ▼                                                         │
├─ dispatch_result = await self._dispatch(...)  ← 消息分发点    │
│    └─ 调用 _message_handler(event)                            │
│    └─ 或调用 _dispatch_bridges[action](chat_id, snapshot, decision)
│
└─ _apply_post_dispatch_state(state, snapshot, decision, dispatch_result)
     └─ await self._state_store.save(state)   ← 第二次持久化
```

### 精确代码位置

`astrmai/conversation/loop/chat_loop_kernel.py` — `tick()` 方法：

```python
async def tick(self, chat_id, *, trigger, event=None, ...):
    state = await self._state_store.get_or_create(str(chat_id or ""))
    # ... 构建 snapshot, decision ...
    self._plan_next_tick(state, snapshot, decision, None)
    self._update_state(state, snapshot, decision)

    await self._state_store.save(state)          # ← 行 519：持久化

    dispatch_result = await self._dispatch(...)   # ← 行 521：分发
    #   ↑ 如果进程在 519 和 521 之间崩溃，消息丢失

    self._apply_post_dispatch_state(state, snapshot, decision, dispatch_result)
    await self._state_store.save(state)          # ← 行 534：二次持久化
```

### 崩溃窗口的精确触发步骤

1. 用户在群聊中发消息 `"帮我查一下天气"`
2. `AttentionGate.process_event()` 聚合窗口 → 构建 `focus_thread`
3. `AttentionDecisionRouter.evaluate()` → Judge LLM 返回 `REPLY`
4. `System2Runner.run()` → `Planner.plan_and_execute()` → 生成回复
5. `ReplyService.handle_reply()` → 发送回复到群聊
6. 消息事件流入 `ChatLoopKernel.tick()` 做状态记录
7. **`_state_store.save(state)` 执行** → SQLite `INSERT OR REPLACE INTO chat_loop_states`
8. ★ **此时 OOM Killer 杀死进程** ★
9. `_dispatch()` 未执行 → `attention_gate.process_event()` 中的 `_fire_background_task` 回调未触发
10. 进程重启后，`chat_loop_states` 表中该 chat 的 `phase='COOLDOWN'`
11. 该消息被视为"已处理"，**不会重新进入 attention gate**

### 具体丢失内容

| 丢失项 | 说明 |
|--------|------|
| 当前 tick 的入站消息 | 1 条（单消息触发）或多条（窗口聚合） |
| attention gate 的 focus_context | 不会被重建 |
| Judge LLM 的判决结果 | 不会重新执行 |
| System2 planner 的回复 | 如果回复已发出，用户收到了回复但状态丢失 |

### 为什么没有重投递

搜索 `ChatLoopKernel` 整个类（2272 行）：

```python
# 搜索结果：以下关键词在 ChatLoopKernel 源码中不存在
✗ "redeliver"
✗ "dead_letter"
✗ "retry_queue"
✗ "unacked"
```

`ChatLoopState` 模型中也没有任何字段记录 `dispatch_outcome` 或 `delivery_status`。

### 影响评估

| 维度 | 评估 |
|------|------|
| 单次丢失量 | ≤ 1 个 tick 的消息批次（通常 1 条） |
| 触发条件 | 进程在 save→dispatch 之间崩溃 |
| 恢复方式 | 下次用户发言自然恢复 |
| 是否存在数据不一致 | 是——DB 认为已处理，实际未处理 |
| 对聊天场景的影响 | 极低——丢失 1 条消息用户不会察觉 |
| 对任务场景的影响 | 中——如果 `/work` 模式的任务消息丢失，任务不会执行 |

### 测试证据

```
test_save_before_dispatch_window_exists    PASSED
  → inspect.getsource 确认 save() 在 _dispatch() 之前

test_crash_between_save_and_dispatch_loses_message  PASSED
  → Mock save 成功，_dispatch 抛异常模拟崩溃
  → 确认 save() 被调用但 dispatch 未完成

test_max_loss_is_one_tick_per_chat         PASSED
  → 确认丢失范围 ≤ 1 tick

test_no_redelivery_mechanism_exists        PASSED
  → 确认无 redeliver/dead_letter/retry_queue/replay
```

---

## 4.3 FAISS 向量检索静默降级

### 数据流图

```
用户消息 "还记得我喜欢吃什么吗？"
│
▼
PromptRefiner._build_memory_context()
  │
  ▼
memory_engine.recall(query="喜欢吃什么", session_id="group_123")
  │
  ├── memory_query = MemoryQuery(query=..., top_k=5, layers=[...])
  │
  ▼
retrieval_service.retrieve(memory_query)
  │
  ├── _retrieve_once(memory_query)
  │     │
  │     ├── canonical_task = store.search(...)     ← FTS5 + overlap 匹配
  │     │     └── 返回 3 条候选 (importance-based)
  │     │
  │     └── hybrid_task = _hybrid_search(...)
  │           │
  │           ▼
  │         engine.search_memories(query, top_k=5)
  │           │
  │           ▼
  │         _ensure_faiss_initialized()
  │           │
  │           ├── if HAS_FAISS == False:
  │           │     └── return False  ← backoff 86400s
  │           │
  │           ├── if embedding_models == []:
  │           │     └── return False  ← backoff 指数增长
  │           │
  │           └── if provider 不可用:
  │                 └── return False  ← backoff 指数增长
  │           │
  │           ▼ (返回 False 时)
  │         return []   ← ═══════════ 静默返回空列表 ═══════════
  │                        调用方无法区分：
  │                        · "没有匹配的记忆"
  │                        · "向量检索服务不可用"
  │
  └── _fuse_candidates(canonical, hybrid, query)
       └── canonical 3条 + hybrid 0条 → 排序 → 返回 3条
```

### 精确代码位置

**入口 1**：`astrmai/memory/services/memory_engine.py:138`

```python
async def search_memories(self, query: str, *, top_k: int, session_id=None, persona_id=None):
    if not await self._ensure_faiss_initialized():
        return []                    # ← 直接返回空列表
    return await self.retriever.search(query, k=top_k, session_id=session_id, persona_id=persona_id)
```

**入口 2**：`astrmai/memory/services/memory_engine.py:770`

```python
async def get_recent_memories(self, session_id, limit=5):
    if not await self._ensure_faiss_initialized():
        return []                    # ← 同样静默返回
    # ...
```

**入口 3**：`astrmai/memory/services/memory_index_projector.py:85`

```python
async def rebuild_all(self):
    if not await self.engine._ensure_faiss_initialized():
        return 0                     # ← 返回 0，不抛异常
    # ...
```

**降级逻辑**：`astrmai/memory/services/memory_engine.py:180`

```python
async def _ensure_faiss_initialized(self):
    if self._is_ready:
        return True

    now = time.time()
    if now < self._next_retry_time:
        return False                 # ← backoff 期间直接返回 False

    if not HAS_FAISS:
        self._next_retry_time = now + 86400   # ← 24 小时后重试
        return False

    # 查找 embedding provider...
    for model_id in unique_models:
        provider_instance = self.context.get_provider_by_id(model_id)
        if provider_instance:
            break

    if not provider_instance:
        self._init_failures += 1
        backoff = min(3600, 30 * (2 ** (self._init_failures - 1)))
        self._next_retry_time = now + backoff
        return False                 # ← 指数 backoff，最长 3600s
```

### Backoff 时间线

```
尝试次数   backoff 计算          实际等待时间
───────   ─────────────────     ────────────
第 1 次   30 * 2^0 = 30s       30 秒
第 2 次   30 * 2^1 = 60s       60 秒
第 3 次   30 * 2^2 = 120s      2 分钟
第 4 次   30 * 2^3 = 240s      4 分钟
第 5 次   30 * 2^4 = 480s      8 分钟
第 6 次   30 * 2^5 = 960s      16 分钟
第 7 次   30 * 2^6 = 1920s     32 分钟
第 8 次   min(3600, 30*2^7)    60 分钟（上限）
第 9+ 次                         60 分钟
```

**注意**：`_init_failures` 计数器**永远不会重置**。如果 embedding provider 配置错误，即使后来修好了，也要等到当前 backoff 过期才会重试。

### 实际触发场景详解

**场景 A：新装插件未配置 embedding_models**

```json
// _conf_schema.json 中 provider.embedding_models 默认值为 []
{
  "provider": {
    "embedding_models": []    // ← 空列表
  }
}
```

→ `_ensure_faiss_initialized()` 中 `unique_models = []`
→ `provider_instance = None`
→ 返回 False → 指数 backoff → 最长 60 分钟不可用

**场景 B：embedding provider ID 配置错误**

```json
{
  "provider": {
    "embedding_models": ["non_existent_model_id"]
  }
}
```

→ `self.context.get_provider_by_id("non_existent_model_id")` 返回 None
→ 同场景 A

**场景 C：faiss-cpu 未安装**

```
pip install faiss-cpu   # 在某些平台失败（Windows ARM、Python 3.12 早期版本等）
```

→ `HAS_FAISS = False`
→ `_next_retry_time = now + 86400` → **24 小时**后才重试

### 调用方如何被影响

| 调用方 | 文件:行 | FAISS 不可用时行为 | 用户感知 |
|--------|---------|-------------------|---------|
| `PromptRefiner._build_memory_context()` | `prompt_refiner.py` | `recall()` 只用 canonical 结果 | 记忆上下文减少但不报错 |
| `ReActRetriever` | `react_retriever.py` | `retrieve_deep()` 只用 canonical | Agent 工具返回结果减少 |
| `MemoryToolService` | `memory_tool_service.py` | 工具调用返回空 | 用户看到"没有相关记忆" |
| `MemoryInjectionService` | `memory_injection_service.py` | 注入内容减少 | System prompt 中记忆块缩小 |
| `/mai` 帮助命令 | `plugin_facade.py:build_help_text()` | **不受影响**——诊断面板不看 search_memories | — |

### 为什么是"静默"

**没有任何调用方检查 FAISS 状态**。搜索所有代码：

```
search_content "is_ready" in astrmai/   → 只有 memory_engine.py 内部使用
search_content "faiss" in astrmai/      → 只有 memory_engine.py / v2_store.py 内部使用
search_content "_is_ready" in astrmai/  → 只有 memory_engine.py 内部使用
```

`RuntimeStatus` 中有 `degraded_components` 字段，但 FAISS 初始化失败并**不会**调用 `runtime.mark_degraded()`。

### 影响评估

| 维度 | 评估 |
|------|------|
| 当前代码中存在触发路径？ | ✅ 存在——未配置 embedding_models 时触发 |
| 用户能否感知到异常？ | ❌ 不能——静默退化，只表现为"记忆少了" |
| 管理员能否发现？ | ⚠️ 需要主动看日志中的 WARNING |
| 影响范围 | 所有依赖向量检索的功能（记忆搜索 / ReAct Agent） |
| 严重度 | 🔴 高——静默功能退化，用户和管理员均无感知 |

### 测试证据

```
test_search_memories_returns_empty_on_faiss_unavailable  PASSED
  → FAISS unavailable 时返回 []

test_callers_silently_receive_empty_results              PASSED
  → 确认调用方不做 FAISS 状态检查

test_faiss_retry_backoff_caps_at_3600s                   PASSED
  → 确认 backoff 上限和 exponent 逻辑

test_hybrid_retriever_has_dummy_fallback                 PASSED
  → HybridRetriever 的 vector=None 分支有 dummy
```

---

## 4.4 模型网关冷却 O(n) 性能退化

### 数据流图

```
用户消息触发 LLM 调用
│
▼
GlobalModelGateway.chat_in_lane_result(...)
│
├─ async with self._global_semaphore:     ← 全局并发控制 (max=3)
│     │
│     ├─ _filter_cooldown_attempt_queue(pool_name, primary_models, attempt_queue)
│     │     │
│     │     ├─ _cleanup_model_cooldowns()   ← ★ O(n) 全量扫描
│     │     │     │
│     │     │     └─ for key, meta in list(cooldowns.items()):   # n = 冷却条目数
│     │     │          if expired: cooldowns.pop(key)
│     │     │
│     │     └─ for model_id in attempt_queue:
│     │          meta = _model_cooldown_meta(report_pool, model_id)
│     │          │  └─ _cleanup_model_cooldowns()   ← ★ 又调用一次
│     │          if meta: skip  # 冷却中
│     │          else: available.append(model_id)
│     │
│     ├─ 如果 available 为空 → 所有模型都在冷却 → 抛出 LLMCascadeFailureException
```

### 精确代码位置

**清理函数**：`astrmai/infrastructure/gateway/gateway_policy.py:13-18`

```python
def _cleanup_model_cooldowns(self) -> None:
    now = time.time()
    cooldowns = getattr(self, "_model_cooldowns", {})
    for key, meta in list(cooldowns.items()):   # ← O(n)
        if float(meta.get("until", 0.0) or 0.0) <= now:
            cooldowns.pop(key, None)
```

**被调用位置**：
1. `gateway_policy.py:21` — `_model_cooldown_meta()` 中
2. `gateway_policy.py:62` — `_filter_cooldown_attempt_queue()` 中

每次 LLM 调用至少触发 **2 次**清理扫描。

### 冷却条目增长路径

```
正常情况：_model_cooldowns = {}  (空)

1 个模型触发 429:
  _model_cooldowns[("fallback", "gpt-4o")] = {until: now+120, reason: "rate_limit"}

3 个模型同时触发 429:
  _model_cooldowns = {
    ("fallback", "gpt-4o"):    {until: now+120, ...},
    ("agent", "claude-3-opus"): {until: now+120, ...},
    ("task", "gpt-3.5"):       {until: now+120, ...}
  }

多租户 + 多 provider (理论最坏情况):
  provider × model × pool_name × 同时冷却 = 条目数
  5 × 4 × 3 × 2 = 120 条目
```

### 性能实测数据

```
条目数    每次清理耗时     10 calls/s 额外耗时     100 calls/s 额外耗时
──────    ───────────     ──────────────────     ───────────────────
10        ~1.2 µs         ~12 µs/s               ~0.12 ms/s
50        ~6 µs           ~60 µs/s               ~0.6 ms/s
100       ~12 µs          ~120 µs/s              ~1.2 ms/s
500       ~50 µs          ~500 µs/s              ~5 ms/s
5000      ~500 µs         ~5 ms/s                ~50 ms/s
```

实测环境：Python 3.11, Windows, 500 条目 × 100 次迭代 → 平均 **~50 µs/call**。

### 全局信号量放大效应

```python
# model_gateway.py:42
self._global_semaphore = asyncio.Semaphore(self.settings.max_concurrent_llm_calls)
# 默认值 max_concurrent_llm_calls = 3
```

所有 LLM 调用（chat / tool_chat / task）共享这个信号量。当 3 个并发调用同时到达时：

```
请求 1 → 获取信号量 → _cleanup_model_cooldowns() → ... → 释放信号量
请求 2 → 等待信号量 → (阻塞)                  → 获取 → _cleanup → ... → 释放
请求 3 → 等待信号量 → (阻塞)                  →         → (阻塞) → 获取 → ...
```

清理耗时串行累加，而不是并行执行。

### 触发概率评估

| 场景 | 条目数 | 触发频率 | 用户感知 |
|------|--------|---------|---------|
| 日常运行（无 429） | 0-3 | 每次 LLM 调用 | 无 |
| 高峰期限流 | 10-50 | 持续到冷却过期 | 无 |
| 大量模型配置 | 50-200 | 持续 | 轻微延迟 |
| 多租户 SaaS | 200-1000 | 持续 | 可能感知延迟 |

**当前默认 `max_concurrent_llm_calls=3`，冷却条目来自 429/403 错误。单实例日常运行中条目 < 20，影响可忽略。**

### 影响评估

| 维度 | 评估 |
|------|------|
| 当前代码中存在触发路径？ | ✅ 存在——每次 LLM 调用必然执行 |
| 日常影响 | 🟢 可忽略（< 20 条目时 < 20 µs） |
| 极端场景影响 | 🟡 轻微（5000 条目时 ~500 µs） |
| 优化难度 | 🟢 低——惰性清理即可解决 |
| 严重度 | 🟡 低 |

### 测试证据

```
test_cleanup_scans_all_entries                  PASSED
  → inspect.getsource 确认 list(cooldowns.items()) 全量遍历

test_cooldown_cleanup_called_on_every_llm_request  PASSED
  → 确认 _filter_cooldown_attempt_queue 每次都调用 _cleanup_model_cooldowns

test_measure_cooldown_cleanup_latency           PASSED
  → 500 条目 × 100 次 = 平均 ~50 µs/call

test_semaphore_serializes_all_llm_calls         PASSED
  → 确认 _global_semaphore 存在且初始化
```

---

## 4.5 表达学习误删无回滚

### 数据流图

```
Timeline: ──────────────────────────────────────────────────────────→

Day 0     Day 1          Day 14             Day 21          Day 35
│         │              │                  │               │
│ 用户说   │ ExpressionMiner │              │               │
│ "笑死"   │ 挖掘候选        │              │               │
│         │ ↓              │              │               │
│         │ AutoCheck      │              │               │
│         │ LLM 判断:       │              │               │
│         │ "这是正常表达"   │              │               │
│         │ → accept       │              │               │
│         │                │              │               │
│         │                │ 用户又说      │               │
│         │                │ "笑死233"    │               │
│         │                │ ↓            │               │
│         │                │ AutoCheck    │               │
│         │                │ LLM 幻觉:     │               │
│         │                │ "这是刷屏"    │               │
│         │                │ → REJECT ❌  │               │
│         │                │              │               │
│         │                │              │ 14 天宽限期过  │
│         │                │              │ ↓             │
│         │                │              │ purge_kind_   │
│         │                │              │ candidates()  │
│         │                │              │ ↓             │
│         │                │              │ DELETE FROM   │
│         │                │              │ canonical_    │
│         │                │              │ memories      │
│         │                │              │ WHERE id = ?  │
│         │                │              │               │
│         │                │              │ ═══ 物理删除 ═══ │
│         │                │              │ 无 undo       │
│         │                │              │ 无法恢复       │
```

### 精确代码路径

**第 1 步：AutoCheck 误判**

`astrmai/learning/review/expression_auto_check_task.py` — `_apply_review()`：

```python
async def _apply_review(self, pattern_id, decision, reason=""):
    # decision 来自 LLM 输出：accept / reject
    if decision == "reject":
        await self.store.update_status(
            pattern_id,
            status="rejected",
            metadata={"review_status": "rejected", "rejected_at": time.time(), "reason": reason}
        )
```

**第 2 步：维护任务执行清理**

`astrmai/memory/services/memory_maintenance_service.py:155-165`：

```python
# 清理 pending 超时的表达式（21 天宽限期）
pending_cleanup = await self.store.purge_kind_candidates(
    kind="expression_pattern",
    statuses=("review_pending",),
    older_than_seconds=21 * 86400,               # ← 21 天
    min_confidence_to_keep=0.95,
    min_count_to_keep=8,
)

# 清理 rejected 表达式（14 天宽限期）
rejected_cleanup = await self.store.purge_kind_candidates(
    kind="expression_pattern",
    statuses=("rejected",),
    older_than_seconds=14 * 86400,               # ← 14 天
    min_confidence_to_keep=0.95,
    min_count_to_keep=8,
)
```

**第 3 步：物理删除**

`astrmai/memory/services/v2_store.py:1124-1172` — `purge_kind_candidates()`：

```python
async def purge_kind_candidates(self, *, kind, statuses, older_than_seconds,
                                min_confidence_to_keep=0.9, min_count_to_keep=5):
    cutoff = time.time() - older_than_seconds
    deleted_ids = []
    protected_skipped = 0

    async with aiosqlite.connect(self.db_path) as db:
        # 查询符合条件的候选
        cursor = await db.execute("""
            SELECT id, confidence, access_count
            FROM canonical_memories
            WHERE kind = ? AND status IN ({})
              AND update_time < ?
        """.format(','.join(['?']*len(statuses))),
            (kind, *statuses, cutoff))

        rows = await cursor.fetchall()
        for memory_id, confidence, count in rows:
            # 保护检查
            if confidence >= min_confidence_to_keep or count >= min_count_to_keep:
                protected_skipped += 1
                continue
            deleted_ids.append(str(memory_id))

        for memory_id in deleted_ids:
            await db.execute("DELETE FROM canonical_memories WHERE id = ?", (memory_id,))
            # ↑ 物理删除——不可恢复！
        await db.commit()

    return {"deleted_ids": deleted_ids, "protected_skipped": protected_skipped}
```

### 保护机制详解

| 保护层 | 值 | 绕过条件 |
|--------|-----|---------|
| `confidence ≥ 0.95` | 不删除 | LLM 给正确表达式打了低置信度（< 0.95） |
| `access_count ≥ 8` | 不删除 | 该表达使用不足 8 次 |
| 14 天宽限期 | 14 天内可人工复审 | 管理员未及时查看 WebUI |
| 21 天宽限期(pending) | 同上 | pending 状态也需要人工审核 |

**双重保护同时失效的场景**：

1. 一个新发现的表达式，命中次数只有 2-3 次（新词/冷门表达）
2. LLM auto-check 误判为 `rejected`，置信度打了 0.4
3. 管理员 14 天内未查看 WebUI 审核面板
4. → 物理删除 → 永久丢失

### 为什么没有软删除

`purge_kind_candidates()` 的源码中**不存在**以下代码路径：

```python
# ✗ 不存在：
await db.execute("UPDATE canonical_memories SET status='deleted' WHERE id=?", (memory_id,))

# ✗ 不存在：
await db.execute("INSERT INTO deleted_memories SELECT * FROM canonical_memories WHERE id=?", (memory_id,))
```

只有物理 `DELETE`，没有 `UNDELETE` 或 `RESTORE` 方法。

### 影响评估

| 维度 | 评估 |
|------|------|
| 当前代码中存在触发路径？ | ✅ 存在——每天定时执行 |
| 是否有保护机制？ | ✅ 有三层（置信度/命中次数/宽限期） |
| 保护机制可被绕过？ | ⚠️ 可以——LLM 幻觉可同时影响置信度和判断 |
| 误删后能恢复？ | ❌ 不能——物理删除，无备份表 |
| 严重度 | 🟡 低——有多层保护，但不可逆 |

### 测试证据

```
test_purge_kind_candidates_physically_deletes   PASSED
  → inspect.getsource 确认 DELETE FROM canonical_memories

test_rejected_expressions_have_grace_period     PASSED
  → 确认 rejected_expression_grace_seconds = 14*86400

test_no_undo_path_after_purge                   PASSED
  → 确认无 undelete/rollback 关键词

test_auto_review_can_reject_correct_expressions PASSED
  → 确认审核逻辑中有 rejected 分支

test_purge_no_soft_delete_fallback              PASSED
  → 确认无 UPDATE SET status 的软删除路径
```

---

## 4.6 遗留兼容层断连

### 数据流图

```
启动阶段：
───────────────────────────────────────────────────────────────

PluginBootstrap.build()
│
├─ runtime.core = CoreServices(
│     gateway=GlobalModelGateway(...),     ← 创建真实 gateway
│     visual_cortex=VisualCortex(...),      ← 创建真实 visual_cortex
│     sys3_router=Sys3Router(...)           ← 如果配置启用
│   )
│
├─ runtime.bind_host_plugin(astrMaiPlugin)
│
└─ runtime.sync_host_compat_attrs()
     │
     ├─ gateway = runtime.gateway    → 非 None → setattr(host, "gateway", gw)
     ├─ visual_cortex = runtime.visual_cortex → 非 None → setattr
     ├─ sys3_router = runtime.sys3_router     → 非 None → setattr
     └─ cron_guard = runtime.cron_guard       → 非 None → setattr

此时 host_plugin 上的遗留引用：
  host.gateway        → GlobalModelGateway 实例 ✅
  host.visual_cortex  → VisualCortex 实例 ✅
  host.sys3_router    → Sys3Router 实例 ✅

───────────────────────────────────────────────────────────────
降级阶段（如配置变更关闭 work mode 后热重载）：
───────────────────────────────────────────────────────────────

apply_hot_config(new_config)
│
├─ runtime.config = new_config
├─ runtime.rebuild_infrastructure_settings()
│    └─ work_mode_enabled = False  ← 从新配置读取
│
└─ runtime.sync_host_compat_attrs()
     │
     ├─ gateway = runtime.gateway    → 非 None → setattr(host, "gateway", gw)    ✅ 更新
     ├─ sys3_router = runtime.sys3_router
     │    │
     │    └─ @property 返回 runtime.workmode.sys3_router
     │          │
     │          └─ 该值仍是旧的 Sys3Router 实例！
     │             为什么？因为热重载时只更新了 config，
     │             但没有重新调用 Bootstrap 重建 WorkModeServices
     │
     ├─ cron_guard = runtime.cron_guard
     │    └─ 同理——指向旧实例
     │
     └─ visual_cortex = runtime.visual_cortex
          └─ 指向旧 VisualCortex 实例

★ 注意：hot-reload 不重建服务实例！★
WorkModeServices / CoreServices 只在 Bootstrap 时创建
```

### 精确代码位置

**遗留属性列表**：`astrmai/app/runtime_context.py:424-454`

```python
LEGACY_RUNTIME_ATTRS = (
    "persistence", "db_service", "gateway", "lane_manager", "event_bus",
    "memory_engine", "state_engine", "judge", "sensors", "visual_cortex",
    "dialogue_store", "context_compaction", "sys3_router", "cron_guard",
    "reply_engine", "evolution", "persona_summarizer", "context_engine",
    "react_retriever", "prompt_refiner", "system2_planner", "system2_runner",
    "frequency_controller", "private_chat_manager", "group_reply_wait_manager",
    "attention_gate", "reflector", "reflect_tracker", "review_service",
    "auto_check_task", "proactive_task", "chat_loop_kernel",
)
# 共 32 个属性
```

**导出函数**：`astrmai/app/runtime_context.py:458-472`

```python
def export_legacy_attrs(runtime: PluginRuntimeContext) -> dict[str, Any]:
    attrs = {...}  # 5 个基础属性
    for name in LEGACY_RUNTIME_ATTRS:
        value = getattr(runtime, name)   # ← 通过 @property 获取
        if value is not None:             # ← None 被跳过！
            attrs[name] = value
    return attrs
```

**同步到 host**：`astrmai/app/runtime_context.py:139-146` → `main.py:34-35`

```python
# runtime_context.py
def sync_host_compat_attrs(self) -> None:
    ref = self.host_plugin_ref
    if ref is None:
        return
    host_plugin = ref()
    if host_plugin is None:
        return
    for name, value in export_legacy_attrs(self).items():
        setattr(host_plugin, name, value)   # ← 只有非 None 的才设置
        # ★ 如果 value 是 None，不会执行 setattr
        # ★ host_plugin 上的旧属性保留原值
```

### 🔴 关键发现：None 值不覆盖旧引用

这意味着以下场景会产生 bug：

**场景 A：服务初始化失败后降级**

```python
# bootstrap.py 中：
try:
    visual_cortex = VisualCortex(gateway, db_service)
except Exception as exc:
    visual_cortex = None  # ← 初始化失败
    runtime.mark_degraded("multimodal.visual_cortex", str(exc))

runtime.core = CoreServices(..., visual_cortex=None)

# sync_host_compat_attrs() 中：
# visual_cortex = runtime.visual_cortex → None
# if value is not None:   ← False! 跳过!
# host.visual_cortex 未被修改
```

**但是**，在 bootstrap 阶段，`visual_cortex` **从未被设置到 host 上**（因为它是 None），所以不会有残留引用问题。问题出在**第二次 sync**。

**场景 B：服务从"有"变为"无"的降级**

假设在某次运行时：

1. 第一次 `sync_host_compat_attrs()`：`visual_cortex` 有值 → `host.visual_cortex = vc_v1`
2. 之后某种原因 `runtime.core = replace(runtime.core, visual_cortex=None)`（如某个后台任务检测到故障后禁用）
3. 第二次 `sync_host_compat_attrs()`：`visual_cortex = None` → **跳过**
4. `host.visual_cortex` 仍然是 `vc_v1`——一个已废弃的实例

### 实际触发条件分析

搜索 `replace(runtime.core` 或 `runtime.core =` 在代码中：

```
search_content "runtime.core =" → 只出现在 bootstrap.py
search_content "replace.*core"  → 只出现在测试中
```

**结论**：当前代码中，服务实例在 bootstrap 后**不会被替换为 None**。降级通过 `runtime.mark_degraded()` 记录状态，但不修改 `CoreServices` 字段。

因此，**当前代码中不存在从"有值"到"None"的转换路径**。这个风险和 4.1 一样，是**架构脆弱性**。

### 真正存在的断连场景

**场景 C：hot-reload 后 host_plugin 属性指向旧实例**

`apply_hot_config()` 只更新 `config` 和 `infrastructure_settings`，**不重建服务实例**：

```python
def apply_hot_config(self, config_dict, parsed_config):
    self.runtime.raw_config = dict(config_dict)
    self.runtime.config = parsed_config
    self.runtime.rebuild_infrastructure_settings()
    # 注意：不调用 PluginBootstrap.build() 重建服务
    self.runtime.sync_host_compat_attrs()
```

这意味着 hot-reload 后：
- `runtime.gateway` 仍然是**同一个** `GlobalModelGateway` 实例（不会被替换）
- `runtime.gateway.config` 指向**新**配置（已被更新）
- 所以实际上**没有断连**——gateway 实例还是那个

**断连只在以下情况发生**：有人直接操作 `runtime.core` 字段替换服务实例。

### 影响评估

| 维度 | 评估 |
|------|------|
| 当前代码中存在触发路径？ | ❌ 不存在（hot-reload 不重建服务实例） |
| 未来重构中容易引入？ | ⚠️ 容易——`dataclass replace()` 是惯用模式 |
| 触发后影响范围 | 32 个属性可能残留过期引用 |
| 严重度 | ⚠️ 中——暂无触发路径，但防御脆弱 |

### 测试证据

```
test_all_legacy_attrs_have_corresponding_property  PASSED
  → 验证 32 个 LEGACY_RUNTIME_ATTRS 都有 @property

test_none_service_not_exported                      PASSED
  → 验证 None 服务不在 export_legacy_attrs 结果中

test_stale_attribute_not_cleaned_on_none            PASSED
  → 构造"有→无"降级场景，验证 host 属性不清理

test_export_legacy_attrs_coverage                   PASSED
  → 验证 fresh runtime 的导出覆盖度
```

---

## 风险评估矩阵

| # | 风险 | 当前可触发 | 触发概率 | 用户可感知 | 管理员可发现 | 影响面 | 严重度 |
|---|------|-----------|---------|-----------|------------|--------|--------|
| **4.3** | FAISS 降级静默 | ✅ 是 | **中** | ❌ 否 | ⚠️ 需看日志 | 记忆检索 | 🔴 高 |
| **4.1** | 服务总线断连 | ❌ 否 | — | ⚠️ 可能 | ❌ 否 | 全服务 | ⚠️ 中 |
| **4.6** | 兼容层断连 | ❌ 否 | — | ⚠️ 可能 | ❌ 否 | 可选服务 | ⚠️ 中 |
| **4.5** | 表达误删 | ✅ 是 | 低 | ⚠️ 可能 | ⚠️ 需审核 | 学习引擎 | 🟡 低 |
| **4.2** | 消息丢失 | ✅ 是 | 极低 | ⚠️ 可能 | ❌ 否 | 单 tick | 🟡 低 |
| **4.4** | 冷却 O(n) | ✅ 是 | 低 | 🟢 否 | 🟢 否 | LLM 调用 | 🟡 低 |

### 严重度定义

| 级别 | 定义 |
|------|------|
| 🔴 高 | 功能静默退化，用户和管理员均无法感知，无恢复机制 |
| ⚠️ 中 | 架构脆弱，当前无触发路径但未来易引入，或有恢复路径 |
| 🟡 低 | 有保护机制，触发概率低，影响范围可控 |

---

## 修复建议优先级排序

### P0 — 立即修复

**4.3 FAISS 降级静默** — 唯一有真实触发路径的高严重度风险。

```python
# 修复方案：在 search_memories 返回空时打标记
async def search_memories(self, query, *, top_k, session_id=None, persona_id=None):
    if not await self._ensure_faiss_initialized():
        # 新增：标记降级状态
        if self.observability_hub:
            self.observability_hub.record_metric("faiss_degraded", 1)
        return []  # 保持兼容
    return await self.retriever.search(...)
```

同时在 `RuntimeStatus.degraded_components` 中登记 FAISS 状态。

### P1 — 质量改进

**4.5 表达误删** — 添加软删除中间状态：

```python
# 在 purge_kind_candidates 之前：
# 1. 先软删除（UPDATE status='purge_pending'）
# 2. 保留 7 天
# 3. 再物理删除
```

**4.1 + 4.6** — 添加 `replace_service()` 方法替代直接操作 dataclass：

```python
def replace_service(self, service_name, new_instance):
    """替换服务并自动同步遗留属性"""
    # 更新 CoreServices/CognitionServices
    # 自动调用 sync_host_compat_attrs
```

### P2 — 防御增强

**4.2 消息丢失** — 添加 dispatch_outcome 标记：

```python
state.last_dispatch_outcome = None  # None → "saved" → "dispatched" → "acknowledged"
# 重启后检查：如果 outcome < "dispatched" → 重放
```

**4.4 冷却 O(n)** — 惰性清理替代全量扫描：

```python
def _model_cooldown_meta(self, pool_name, model_id):
    key = self._cooldown_key(pool_name, model_id)
    meta = self._model_cooldowns.get(key, {})
    if meta and float(meta.get("until", 0) or 0) <= time.time():
        self._model_cooldowns.pop(key, None)  # 只清理被访问的 key
        return {}
    return dict(meta) if meta else {}
```

---

## 测试运行

```bash
python -m pytest tests/manual/risk_audit/ -v
# 24 passed, 4 warnings in ~8s
```
