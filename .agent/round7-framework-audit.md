# AstrMai 第七轮深度审计报告 — 框架集成/配置/心跳/迁移/性能/编码

> 日期: 2026-07-03 | 6 领域 | QQ Only | ~30 bugs

---

## 一、AstrBot 框架集成 (5 bugs)

### 1. [CRITICAL] system_prompt 重赋值破坏 provider 前缀缓存 — `main.py:120`
```python
request.system_prompt = maybe_attach_reverse_session_block(...)
```
每 `on_llm_request` 钩子调用都重新赋值 `system_prompt`。按 AstrBot Skill §7.1.1，这会破坏模型服务端的提示词缓存，**增加约 7-20 倍 API 成本**。正确做法是用 `request.extra_user_content_parts.append(TextPart(...))` 或 `.mark_as_temp()`。插件在 line 115-117 的 ponytail 注释中已承认此问题但未修复。

### 2. [HIGH] send_message 接收字符串而非 UnifiedMessageOrigin — `review_dispatcher.py:18`
```python
await self.context.send_message(item["group_id"], MessageChain().message(item["question"]))
```
`item["group_id"]` 是纯字符串（如 `"123456"`），但 AstrBot `send_message()` 需要 `unified_msg_origin` 对象。框架会在第一个参数上调用 `.group_id`/`.session_id` 等 —— `str` 没有这些属性 → 运行时 `AttributeError`。

### 3. [HIGH] send_message 同样类型错误 — `dream_scheduler.py:150`
```python
target = getattr(self.config.life, "dream_send_target", "") or session_id
await self.context.send_message(target, MessageChain().message(dream_text))
```
`target` 解析为纯字符串。同 Bug 2。

### 4. [MEDIUM] get_using_provider 是未文档化/已废弃 API — `main.py:110`
```python
provider = self.context.get_using_provider(event.unified_msg_origin)
```
AstrBot Skill (≥v4.14.0) 不记录 `get_using_provider()`。正确 API 是 `get_current_chat_provider_id()`。另外内层 `except Exception` 静默吞所有错误，API 失效时不可见。

### 5. [MEDIUM] send_message 传入字符串序列化 UMO — `executor.py:917`
```python
admin_umo = f"{platform_id}:FriendMessage:{admin_id}"
await self.context.send_message(admin_umo, chain)
```
构造 `"aiocqhttp:FriendMessage:905617992"` 字符串而非 UMO 对象。框架的 `send_message` 实现是否接受字符串表示是版本依赖的，脆弱。

---

## 二、配置 Schema 校验 (5 bugs)

### 1. [HIGH] Pydantic `__init__` 覆盖是错误生命周期钩子 — `config.py:251-252`
```python
def __init__(self, **data):
    super().__init__(**self._normalize_legacy_memory_namespace(data))
```
在 Pydantic v2.10.4 上直接覆盖 `__init__` 绕过 `model_post_init` 钩子、私有属性初始化，用部分数据调用 `model_validate()` 时可能破坏字段默认解析。正确模式：`@model_validator(mode='before')` 处理归一化，`model_post_init()` 处理警告。

### 2. [MEDIUM] 浅拷贝变异在热重载时损坏内部 dict — `config.py:233-235`
```python
normalized = dict(data)  # 浅拷贝 → 内部 dict 仍共享
global_settings[field_name] = normalized[field_name]  # 原地变异
```
AstrBot 热重载时可能传递相同内部 dict 对象。`global_settings` 原地变异可能导致跨重载累积遗留字段。`memory_values = dict(memory)` 正确拷贝但 `global_settings` 没有。

### 3. [MEDIUM] auto_recall_probability 无边界 — `config.py:185`
```python
auto_recall_probability: float = 0.0  # 无 Field(ge=0.0, le=1.0)
```
Schema 提示说 `0.0=禁用, 1.0=每次触发` 但 Pydantic 接受 `-999` 或 `999`。

### 4. [MEDIUM] review_runner_* 无边界 — `config.py:107-108`
```python
review_runner_interval_sec: int = 60      # 无 ge/le
review_runner_min_interval_sec: int = 45  # 无 ge/le
```
Schema 提示指定 `范围 30-600` 和"防止同一聊天连续触发"。设为 0 或负数会导致死循环/敲击。

### 5. [LOW] enable_token_estimator 初始化不一致 — `config.py:163`
```python
enable_token_estimator: bool = False  # 裸值，其他所有 bool 使用 Field(default=...)
```
在 `model_validate(strict=True)` 或 `from_attributes=True` 下行为可能不同。

---

## 三、心跳/情绪引擎 (5 bugs)

### 1. [HIGH] 卡死状态：consecutive_no_reply_count 永不重置 — `manager.py:927-942`
圆环依赖。`consecutive_no_reply_count` 仅在 `action_type == "proactive_candidate"` (line 937-939) 时重置。但 `proactive_candidate` 只能在 `no_reply` 阻塞之后到达 (line 662 → line 658 `consecutive_no_reply_count >= 3`)。一旦计数到 3，`no_reply` 永远胜出，`proactive_candidate` 不可达，聊天在整个会话期间永久静音。

### 2. [MEDIUM] safety_checks["dispatch_enabled"] 永久 False — `manager.py:759`
```python
safety_checks: dict[str, object] = {"dispatch_enabled": False, ...}
```
`safety_checks` dict 在行 759 初始化后**从未更新**。即使调度成功 (line 867)，dict 仍然保留 `dispatch_enabled: False`。调试/监控误导。

### 3. [MEDIUM] old_topic_blocked 中死分支 — `manager.py:638`
`age_seconds > self.ACTIVE_CHAT_TTL_SECONDS` 永远不可能为真——调用方 `_get_or_refresh_session` (line 188-191) 已经对过期聊天返回 `None`。死代码。

### 4. [MEDIUM] 反馈桥接时钟混用 — `feedback_bridge.py:107 vs :40`
`_last_flush_ts[chat_id] = time.time()` (时钟) vs `now` 用 `monotonic()` 比较。时钟会在 NTP/夏令时调整时跳变，monotonic 只前进。跳变后冷却期可能过长或过短。

### 5. [MEDIUM] 情绪矛盾：`_resolve_impulse` vs `_build_pulse` 不一致 — `manager.py:488-498 vs :524-525`
同一 tick 对同一状态产生矛盾输出。当 interest=0.75, silence_pressure=0.50 时：
- `_resolve_impulse` 返回 `"join"`
- `_build_pulse` 返回 `"prepare_reply"`
下游代码看到矛盾信号。

---

## 四、升级迁移兼容 (5 bugs)

### 1. [HIGH] Quart→FastAPI 管理页面静默断裂 — `plugin_pages.py:575-576`
AstrBot v4.26.0 从 Quart 切换到 FastAPI。代码检查 `hasattr(context, "register_web_api")`——若方法名在 FastAPI AstrBot 中改变或移除 → 约 85 条管理 API 端点**静默停用**，零日志输出。`main.py:66` ponytail 注释确认这是已知但未处理的风险。

### 2. [MEDIUM] 整个 legacy_compat 模块废弃但未替换 — `legacy_compat.py:1-3`
模块声明"v2.0 废弃，v3.0 移除"——但全部 5 个函数仍被 6 个生产模块调用（`gate.py`、`reply_artifact_builder.py`、`reply_service.py`、`planner_prompt_context.py`、`prompt_refiner.py`）。每次调用发出 `DeprecationWarning`——若用户启用 `-W error` 则直接崩溃。

### 3. [MEDIUM] 同步主机兼容属性部分失败风险 — `runtime_context.py:140-150`
`setattr` 循环中无 try/except。若 28 个属性中任意一个为 `None`（服务未初始化）→ 下游代码 `self.persistence.do_something()` 崩溃 `AttributeError`。Ponytail 注释承认风险但无回滚或守卫。

### 4. [MEDIUM] 配置字段迁移读取错误源 — `config.py:224-249`
`_normalize_legacy_memory_namespace` 试图从 `global_settings` 复制 `LEGACY_MEMORY_NAMESPACE_FIELDS` 到 `memory`。但 schema 将这些字段放在 `memory.items` 下，不在 `global_settings.items` 下。升级正确嵌套格式的用户的这些设置会**静默丢失**。

### 5. [LOW] 硬编码相对缓存路径 — `bootstrap.py:192-194`
回退路径 `Path("data") / "plugin_data" / ...` 是相对路径，根据 CWD 解析。若 `persistence.cache_dir` 为 None，trace 缓存文件散落到随机位置。

---

## 五、性能热路径 (5 bugs)

### 1. [CRITICAL] 调度器热路径深拷贝 — `chat_loop_kernel.py:302-315`
`_scheduler_policy_value()` 调用 `_scheduler_policy()` 对完整策略 dict 执行 `deepcopy` + 对每个 profile value 再次 `deepcopy`。 `_build_due_score_breakdown()` 对每个聊天调用 `_scheduler_policy_value()` **6 次**。在 `describe_due_selection()` 中，对所有跟踪聊天（可能数百个）执行。每次心跳 Tick：6 次深拷贝 × N 个聊天。**修复**：循环外缓存策略 dict。

### 2. [CRITICAL] 混合搜索 N+1 DB 查询 — `memory_retrieval_service.py:248-252`
`_hybrid_search()` 对每个带非索引 canonical_id 的混合结果执行 `await self.store.get_by_id(canonical_id)`——每次一个独立 DB 往返。top_k=100 → 高达 100 次顺序查询。**修复**：收集所有 ID，单次 `batch_get`。

### 3. [HIGH] update_mood 深拷贝整个 ChatState — `chat_state_service.py:303`
`copy.deepcopy(state)` 复制整个 ChatState 对象（包括嵌套 dataclass、LastMessageMetadata、group_config dict 等），发生在**每条消息**上——插件中最热的路径。**修复**：仅复制需要的浮点字段，或使用轻量快照 struct。

### 4. [HIGH] 同步 PIL Image.open 阻塞事件循环 — `image_pipeline.py:27` → `visual_cortex.py:97`
`ImagePipeline.prepare_image()` 在 async 协程内同步执行 `Image.open(io.BytesIO(...))`。**修复**：包装 `asyncio.to_thread`。

### 5. [MEDIUM] 列表推导内重复构造 set() — `chat_loop_kernel.py:1024`
`set(dialogue_selected + overflow_selected)` 在遍历 `selected_order` 的列表推导中**每次迭代**重建。N 个选中聊天 → N 次 set 构造。**修复**：提升到列表推导外。

---

## 六、i18n / 编码 (5 bugs)

### 1. [MEDIUM] JSON 缺 ensure_ascii=False → 数据存储为 \uXXXX 转义 — `state_profile_persistence.py:272`
同文件所有其他 `json.dumps()` 使用 `ensure_ascii=False`，唯独行 272 不用。`image_urls` 中的中文字符被存储为 `\uXXXX` 转义序列——数据退化 bug。

### 2. [MEDIUM] 同样 JSON 问题 — `memory_migration_service.py:92`
`json.dumps(report["imported"])` 缺 `ensure_ascii=False`。迁移日志中的中文元数据被存储为转义序列。

### 3. [LOW] 隐式 .encode() — `dedupe.py:34`
`msg_str.encode()` 无显式编码。Python 3 默认为 UTF-8 所以安全，但脆弱——若默认编码改变，哈希静默改变，破坏去重。

### 4. [LOW] 日志中英文混用 — `lifecycle.py`
7 条中文 + 31 条英文日志交织。行 186 英文 vs 行 188 中文背靠背。破坏基于 grep 的日志监控。

### 5. [LOW] 硬编码中文回复字符串 — `reply_freshness.py:122-128`
6 条面向用户的延迟回复回退字符串全部硬编码中文，无 locale 文件、无配置覆盖、无英文回退。其他文件同模式：`message_entry.py:104`、`reply_artifact_builder.py:63`、`expression_policy.py:301`。

---

## 全局 Top 5

| # | 严重度 | 领域 | Bug | 文件 |
|---|--------|------|-----|------|
| 1 | CRITICAL | 框架 | system_prompt 重赋值 → 7-20x 成本 | `main.py:120` |
| 2 | CRITICAL | 性能 | 调度器深拷贝 6×N/心跳 | `chat_loop_kernel.py:302` |
| 3 | HIGH | 心跳 | no_reply 卡死永不解锁 | `manager.py:927` |
| 4 | CRITICAL | 性能 | 混合搜索 N+1 查询 | `memory_retrieval_service.py:248` |
| 5 | HIGH | 迁移 | Quart→FastAPI 管理页面静默断裂 | `plugin_pages.py:575` |
