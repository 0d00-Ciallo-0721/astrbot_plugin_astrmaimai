# AstrMai 第六轮深度审计报告 — 人格/定时/前端/社交/测试/记忆

> 日期: 2026-07-02 | 6 领域 | QQ Only | ~30 bugs

---

## 一、人格表达系统 (5 bugs)

### 1. [HIGH] _fast_select 零情境匹配 — `expression_policy.py:441-452`
`_fast_select` 是默认路径(think_level ≤ 0)。它按 `(weight, count, last_active_time)` 排序加载已审核语料，然后应用冷却——但**从不检查 `pattern.situation` 是否匹配 `context_text`**。一句情境为"当有人夸你时"的语料在用户说"你滚"时同样会被选中。冷却机制只屏蔽了最近使用过的和短重复语料，不屏蔽情境完全不匹配的语料。

### 2. [HIGH] `rejected=True` 覆盖 `review_status="approved"` — `expression_pattern_service.py:277-282`
```python
if review_status is not None:
    metadata["review_status"] = str(review_status or "").strip().lower()
if checked is not None and checked:
    metadata["review_status"] = "approved"
if rejected is not None and rejected:
    metadata["review_status"] = "rejected"  # ← 永远胜出
```
三个独立赋值按顺序执行，`rejected=True` 永远覆盖前面的 `review_status="approved"`。若管理员 UI 同时发送 `checked=True` 和 `rejected=True`(复选框 bug、过期表单状态)，调用 `update_review(id, checked=True, rejected=True)` 静默将审核状态设为 "rejected"。

### 3. [MEDIUM] 无时间衰减——过期语料满权重存活 — `reflector.py:26, 174-180`
`WEIGHT_FLOOR=0.1` 仅按绝对权重门控自动拒绝。30 天未使用的语料保持 weight=1.0，积极使用但获 3 次差评的语料从 1.0 降至 0.1 接着被自动拒绝。`last_active_time` 字段虽在 `adjust_weight` 中更新，但从未被消耗用于衰减。

### 4. [MEDIUM] 合并审核中被拒语料可被静默重新批准 — `expression_pattern_service.py:92-98`
`_normalize_incoming_review_status` 仅对 `source="learning_expression_pattern"` 降级 "approved" → "pending"。非学习来源可通过 `write_pattern` 将被拒语料重新批准。

### 5. [LOW] 大小写敏感的语料冷却键 — `expression_policy.py:349-350`
`_pattern_key` 仅用 `.strip()` 无 `.lower()`，而存储级去重 `normalize_text` 使用了 `.lower()`。结果："Hey" 和 "hey" 在存储中是同一条记录，但在冷却追踪器中被视为不同的键，可以绕过后者的冷却立即重新选中。

---

## 二、定时调度系统 (5 bugs)

### 1. [HIGH] `_revive_job()` 调用不存在的方法——心跳恢复永久失效 — `heartbeat.py:101-115`
`_revive_job` 调用 `cron_mgr.add_job(job)`，但 `CronJobManager` 只有 `add_basic_job()` 和 `add_active_job()`——无 `add_job()`。`hasattr` 检查永远返回 False → 静默返回 False → **整个心跳守护的任务恢复功能是空操作**。

### 2. [HIGH] `run_at` 永远无法捕获——一次性任务过期检查被绕过 — `cron_agent.py:116-117`
`getattr(job, "run_at", None)` 永远返回 None，因为框架 `CronJob` 模型没有 `run_at` 字段——一次性任务的执行时间存储在 `payload["run_at"]` 中。导致所有一次性快照的 `run_at` 为 None，心跳守护的过期检查 `snap.run_once and snap.run_at and snap.run_at < now` 永远不触发。

### 3. [HIGH] 无时区参数——cron 表达式使用系统本地时间 — `cron_tools.py:82-89`
`CreateActiveCronTool.call()` 从未传入 `timezone` 参数给 `cron_mgr.add_active_job()`。系统提示词只说五段式 cron 表达式，未提及时区。服务器 UTC 时，用户北京时间"每天早上8点提醒" → `'0 8 * * *'` → UTC 8:00 触发 = 北京时间 16:00。

### 4. [MEDIUM] 通过工具删除的任务无法清理 CronSnapshot — `cron_agent.py:63-69`
`_sync_dual_write()` 只在 `CronAgent.call()` 完成时清理快照。LLM 通过 `delete_future_task` 工具直接删除任务时，`DeleteCronJobTool.call()` 调用 `cron_mgr.delete_job()`，框架删除完成但插件自己的 `CronSnapshot` 表未更新。

### 5. [MEDIUM] Payload 类型不匹配——存 JSON 字符串但读时期望 dict — `cron_agent.py:125` + `heartbeat.py:112`
写入 `json.dumps(payload)` → JSON 字符串。恢复时 `CronJob(payload=snap.payload)` → 传入字符串但期望 dict → `payload.get("note")` 抛出 `AttributeError`。

---

## 三、Plugin Pages 前端 JS (5 bugs)

### 1. [HIGH] 33处静默吞错导致数据损坏 — `app.js:455-461, 501-507, 701-708`
`Promise.all` 中 `.catch(() => ({}))` / `.catch(() => ({ items: [] }))` 静默将 API 错误替换为空数据。`state` 字段被空对象覆盖，之前加载的真实数据丢失。用户看到空白页面或全零指标，不知道后端已崩溃。

### 2. [MEDIUM] 快速切换标签页的竞态条件 — `app.js:241-245, 1335-1352`
`navigate()` 调用 `loadCurrent()` 时不加 await，`loadCurrent()` 完成时不检查 `state.current` 是否仍然匹配。快速切换时较慢请求的渲染会覆盖较快请求的结果。

### 3. [MEDIUM] 调度器轮询并发渲染 — `app.js:635-641, 700-788`
每 5 秒触发 `renderDashboardCognition()`，无并发防护。若 API 调用 >5 秒，多个实例同时争夺 `content().innerHTML`。

### 4. [LOW] `apiErrorMessage` 泄漏内部数据 — `app.js:184`
错误负载匹配不到已知格式时，回退 `return json(payload)` 将整个原始响应 JSON 化显示，可能暴露服务器内部结构。

### 5. [LOW] `openFormModal` 零表单验证 — `app.js:372-400`
`Number.parseFloat("")` 和 `Number.parseInt("abc", 10)` 产生 NaN，被静默发送到后端。

---

## 四、社交关系引擎 (5 bugs)

### 1. [HIGH] 跨群好感污染——group_id 是死参数 — `chat_state_service.py:351-402`
`calculate_and_update_affection` 接收 `group_id: str` 参数但整个方法体内从未使用。好感向量 `_vectors[user_id]` 不分群。用户在群A的辱骂会影响群B中的好感评分。

### 2. [HIGH] 消息质量评分惩罚长消息 — `affection_router.py:97-107`
```python
if length <= 150: return 1.5
return 0.1  # >150 字 → 最低分
```
>150 字的消息得 0.1，比 1 字消息(0.5)更低。深思熟虑的长回复在好感路由中几乎不可见。

### 3. [MEDIUM] Bot 自身消息未过滤 — `affection_router.py:53-56`
`_extract_info` 处理 `AstrMessageEvent` 时不检查 role。Bot 消息若泄漏到 window/history 事件中会污染路由分数。Dict 分支有角色过滤，AstrMessageEvent 分支没有。

### 4. [MEDIUM] 信任衰减不对称——正面关系更"粘滞" — `relationship_engine.py:467-470`
`if vec.trust > 50: trust_rate *= 0.5`。正面信任衰减减半，但 `vec.trust < -50` 时没有对应的减半逻辑。严重负面关系以完整速率衰减。

### 5. [LOW] `negative_streak` 追踪但从未参与计算 — `relationship_engine.py:380`
负面连续互动计数只在每次负面事件时递增、正面事件时归零，但从未被任何评分函数读取。对比 `positive_streak` 驱动 `streak_multiplier`，负面方向缺少对应的惩罚放大器。

---

## 五、测试质量 (5 bugs)

### 1. [HIGH] 同义反复测试——测试 Python str.replace 而非业务逻辑 — `test_memory_refactor.py:59-69`
创建 MemoryProcessor 但从不调用，直接对模板字符串执行 `.replace()`，断言 Python 内置 `str.replace()` 能工作。只要 CPython 存在该测试就永远通过。

### 2. [HIGH] 源码检查反模式 —— `test_clock_source_regression.py:48-58`
5 个测试使用 `inspect.getsource()` 验证源码中包含特定字符串。空白符改动、变量重命名或代码注释变动都会破坏测试，而实际行为破损时若源码巧合匹配则测试通过。

### 3. [MEDIUM] Mock 自证预言 —— `test_cognitive_feedback_refactor.py:81-125`
构造 MemoryEngine(None)，禁用真实初始化，mock 写入和检索两条路径，插入反馈后用 mock 检索返回相同数据，断言反馈已被过滤——插入和过滤都由测试 mock 执行，非真实 MemoryEngine 逻辑。

### 4. [MEDIUM] sys.modules 突变无清理 —— `test_database_adapters_refactor.py:12-22`
setUp 弹出 7 个模块但 tearDown 从不恢复。多个测试文件使用 importlib.reload() 无模块状态重置。测试相互依赖执行顺序。

### 5. [LOW] 架构测试实则静态分析 —— `test_import_boundaries_refactor.py:55-142`
解析 AST 检查导入字符串，从不执行任何运行时代码路径。

---

## 六、记忆维护 (5 bugs)

### 1. [HIGH] `apply_decay` 统一衰减所有记忆无 recency 权重 — `v2_store.py:1042-1056`
每条活跃记忆 `decay_score *= (1 - decay_rate)^days` 完全一致。5分钟前访问和2个月未触碰衰减相等。约55天后(decay_rate=0.08)所有记忆变成 stale。

### 2. [HIGH] 搜索自动恢复不重建向量索引 — `v2_store.py:815`
`search()` 恢复 stale 记忆时调用 `self.restore()`(存储级)，该版本重新添加 FTS 但不调用 `index_projector.project()`。维护级 `MemoryMaintenanceService.restore()` 会重建向量——但搜索路径走的是存储级，向量索引永久丢失。

### 3. [MEDIUM] `run_once()` 从未调度——死代码 — `memory_maintenance_service.py:37`
调度器仅调用 `decay_service.run_once()` → `apply_daily_decay`，从不调用 `maintenance_service.run_once()`。后者处理的时序 hot-score staleness 检测、行话候选清理、表达模式清理全部是死代码。

### 4. [MEDIUM] 无记忆总量上限 — `v2_store.py:448`
INSERT 前不检查 `COUNT(*)`，无 `MAX(capacity)` 配置。仅靠衰减和 `stale_grace_seconds`(默认 7 天) 限制。受保护的 `persona_lore`(importance>=0.95) 永不被物理删除。

### 5. [LOW] `update_content` 恢复 stale 不重置 decay_score — `v2_store.py:1369-1377`
更新内容时 status 翻转为 'active' 但 `decay_score` 和 `last_access_time` 不变。下一次 `apply_decay` 立刻重新标记为 stale 并可能物理删除。

---

## 全局 Top 5

| # | 严重度 | 领域 | Bug | 文件 |
|---|--------|------|-----|------|
| 1 | HIGH | 前端 | 33处静默吞错→数据损坏 | `app.js:455+` |
| 2 | HIGH | 定时 | _revive_job永久失效(add_job不存在) | `heartbeat.py:101` |
| 3 | HIGH | 社交 | 跨群好感污染(group_id死参数) | `chat_state_service.py:354` |
| 4 | HIGH | 记忆 | apply_decay无recency权重 | `v2_store.py:1042` |
| 5 | HIGH | 表达 | _fast_select零情境匹配 | `expression_policy.py:441` |
