# AstrMai 第五轮深度审查报告

> 日期: 2026-07-02 | 9 领域 | QQ Only

---

## 一、并发与稳定性 (5 bugs)

### 1. 共享字典 TOCTOU — `gate.py:114-118`
`get_proactive_lock()` 无锁读写 `_proactive_injection_lock` dict。两个并发协程同时发现 key 不存在 → 都创建 asyncio.Lock → 第二个覆盖第一个 → 被覆盖的协程锁成为孤儿，消息处理串行化混乱。

### 2. 共享字典 TOCTOU — `memory_turn_pipeline.py:401-406`
`_get_memory_lock()` 同样无锁读写 `_memory_locks` dict。两个并发调用为同一 chat_id 创建两个不同锁对象 → 会话 buffer 被不同锁保护 → 数据损坏。

### 3. 任务风暴 — `gate.py:898`
`sys2_process` 通过裸 `asyncio.create_task()` 触发，**无信号量限制**。消息爆发时无限并发 LLM 调用 → API 429 耗尽。

### 4. Worker 任务泄漏 — `memory_turn_pipeline.py:149,390-393`
Sweep 清理时只 pop 不 cancel。任务卡死在 `queue.get()` → 永久泄漏。`stop()` 时已 pop 的 stale worker 不被 cancel。

### 5. 定时器漂移 — `proactive_task.py:767`
`asyncio.sleep(interval)` 不计工作时间。实际间隔 = sleep + work_time。每天损失 ~2.4 小时调度精度。同样模式: `heartbeat.py:53`、`expression_governance_runner.py:86`、`memory_turn_pipeline.py:374`。

---

## 二、冷启动/引导 (5 bugs)

### 1. DB 异步 init 竞态 — `persistence_schema.py:173-183`
`_schedule_init_db()` 创建后台任务初始化 chat_states/user_profiles 等表，`_init_ready` 事件被创建但**无人 await**。首次启动时若 `get_chat_state()` 在初始化完成前调用 → `OperationalError: no such table: chat_states`。

### 2. Windows 路径大小写 — `webui/backend/db.py:17-30`
`startswith(root_norm + os.sep)` 是大小写敏感字符串比较。NTFS 上 `C:\Data\` ≠ `c:\data\` → 路径验证失败 → 回退到默认路径。

### 3. 配置迁移静默跳过 — `config.py:224-245`
当 raw config 无 `global_settings` key 时创建 `global_settings = {}`，所有迁移字段 `not in {}` → 全部跳过 → 用户旧配置值被默认值替代，无警告。

### 4. 插件构造器触碰文件系统 — `main.py:58-63` → `persistence_manager.py:17-43`
`PersistenceManager.__init__` 在 `__init__`（非 `on_program_start`）中运行，创建引擎、运行 `create_all()`。若 `get_astrbot_data_path` 导入失败，回退到相对路径 `data/plugin_data/astrmai`。

### 5. 首次启动 ~72 次浪费的 DDL — `persistence_schema.py`
4 个表由 3 个独立路径创建。迁移 #36 (memoryevent session_id) 被手动重新应用——每启动抛一次 `duplicate column name` 异常被 catch。

---

## 三、数值累加器 (5 bugs)

### 1. [CRITICAL] 纪元 0 灾难性衰减 — `relationship_engine.py:78-91,447-452`
`from_dict()` 中 `data.get("last_decay_time", time.time())` — 若持久化数据有 `last_decay_time: 0`，加载时 `hours_since_decay = (now-0)/3600 ≈ 20000+` → 所有关系维度乘以 `(1-rate)^833` → 归零。

### 2. monotonic/wall-clock 混用 — `frequency_controller.py:161 vs 174,178`
`on_message_received()` 使用 `monotonic()`，但 `_record_message()` 使用 `time.time()` → 同一字段两种纪元 → 静默时长计算出错。

### 3. 心情衰减时钟漂移 — `mood_decay.py:39,47-48`
`decay_steps = int(elapsed/decay_interval)` 截断余数。`advanced_decay_time` 向下舍入 → 每个周期丢失最多一个完整间隔 → 累积漂移。

### 4. 每日重置不累积 — `chat_state_service.py:134-142`
`_check_daily_reset()` 只检查 `!= today`，不计算错过的天数。10 天不活跃 → 只恢复 `1 * daily_recovery` 而非 `10 * daily_recovery`。

### 5. 连续互动 off-by-one — `relationship_engine.py:334-366`
`streak_multiplier` 在增量**前**计算 → 第 3 次连续事件才首次获得加成。连续互动计数对事件强度不敏感。

---

## 四、Facade/API 层 (5 bugs)

### 1. `_call_facade` 静默 None — `plugin_api.py:191-197`
facade 未初始化或方法不存在时返回 None → 30+ getter 方法全部返回 None/{} → WebUI 显示空数据而非"不可用"。

### 2. 热应用部分失败 — `plugin_facade.py:90-123`
`apply_hot_config` 遍历 11 个组件，单个 `refresh_config` 失败被静默 catch → 始终返回 True → 新旧配置混用。

### 3. `persistence.dispose()` 双调 — `plugin_facade.py:77-78 + lifecycle.py:272-274`
两次调用且都不 await → 若 dispose 是异步的，协程泄漏。生命周期先调用 dispose，facade 再重复调用。

### 4. 通用异常吞没 — `plugin_facade.py:522-529`
`_system2_entry` 的 `except Exception` 捕获所有编程错误 → 返回中文 fallback → 用户看到"陷入了短暂的沉默"。

### 5. `sync_host_compat_attrs` 部分 setattr — `runtime_context.py:140-150`
`setattr` 循环中无 try/except → 若某属性设置失败，已设置的属性不回滚 → host plugin 处于部分脏状态。

---

## 五、QQ 群行为 (5 bugs)

### 1. 无 bot-kick 清理 — `main.py:194-199`
只有 `filter.EventMessageType.ALL`，无 group_decrease 处理。被踢后 7+ 内存字典不清理：chat_states、coordinator._states、state_store._states、dialogue_store._threads、focus_pools、group_wait._states、user_profiles。

### 2. 重新加入状态损坏 — 同上文件
Stale wait targets、cooldowns、dialogue threads、focus pools 在新会话中持久存在 → 旧状态污染新会话。

### 3. 匿名消息 (80000000) 无防护 — `message_scope.py:49-57`
QQ 匿名消息的 sender_id 前缀 80000000 被当作真实用户 → 创建 UserProfile、跟踪社交关系、记录对话片段。

### 4. 无消息撤回处理 — `gate.py:461-493`
消息被记录到 dialogue store 但无 recall 事件处理 → bot 可能引用已删除的消息。

### 5. 管理员权限静态 — `permission_guard.py:11-13`
`admin_ids` 从配置读取，不反映运行时管理员变更 → 新管理员无权限，被撤管理员保留权限。

---

## 六、数据一致性 (5 bugs)

### 1. 双写无原子性 — `memory_write_service.py:94-106`
`v2_store.upsert()` 提交到 memory_v2.db，然后 `project()` 写入 docs.db (Faiss)。无分布式事务，中间崩溃 → SQL 有记录但无向量投影 → 对混合搜索不可见。

### 2. read-modify-write 竞态 — `state_profile_persistence.py:63,107`
`load_chat_state` 和 `save_chat_state` 各自打开新的 aiosqlite 连接，无乐观锁 → 后写覆盖先写 → 更新丢失。

### 3. INSERT OR REPLACE 字段丢失 — `state_profile_persistence.py:249-265`
`INSERT OR REPLACE = DELETE + INSERT` → 未包含在 INSERT 列表中的列被重置为 DEFAULT → schema 演进时数据丢失。对比 `save_chat_state()` 使用安全的 `ON CONFLICT DO UPDATE`。

### 4. 多连接碰撞 — `database_service.py:106 vs state_profile_persistence.py:107`
SQLModel engine 和原始 aiosqlite 两条独立连接栈访问同一个 `astrmai.db` → 无 shared lock → 并发写竞争 → `SQLITE_BUSY`。

### 5. `mark_merged` 遗漏 FTS 同步 — `v2_store.py:1328-1348`
`soft_delete()` 和 `mark_stale()` 调用 `_sync_fts(delete_only=True)`，但 `mark_merged()` 没有 → 已合并记忆的 FTS 条目残留 → 搜索结果可能包含 phantom 条目。

---

## 七、外部故障恢复 (5 bugs)

### 1. SQLite 无 busy_timeout — `persistence_manager.py:34`
`create_engine(self.db_url)` 无 `connect_args={'timeout': 30}` → 并发写入时 `SQLITE_BUSY` → 写入任务崩溃，不可恢复。

### 2. Raw sqlite3 绕过引擎 — `database_service.py:190`
`get_chat_state()` 用裸 `sqlite3.connect()` 无 timeout 参数 → 默认 5 秒 → 争用中线程池饥饿 → `OperationalError`。

### 3. 模型池级联无部分结果 — `gateway_call.py:336-345`
全部模型失败时 `LLMCascadeFailureException` → 无 `raw_completion_text` 保留 → 用户看不到任何输出。

### 4. 429 打断前无退避睡眠 — `gateway_call.py:208-211`
`_is_fatal_failure` 将 429 标记为 fatal → 立即 `break` → 同模型无重试 → 若全部模型共享 API key 配额 → 一遍过全部放弃。

### 5. QQ 断连不检测 — `main.py:184-199`
无 `on_astrbot_adapter_error`、无发送失败监听、无消息排队重放 → 断连后消息静默丢失，bot 不自知。

---

## 八、LLM Prompt/上下文 (5 bugs)

### 1. [CRITICAL] user_input 标签从未应用 — `prompt_refiner.py:923`
`sanitize_user_input()` 被定义但从未在 prompt 组装路径中调用。`focus_message_text` 直接作为 `---眼前正在对我说的---` 注入，无 `<user_input>` 包裹。system rules 告诉 LLM 尊重 `<user_input>` 标签，但标签从未出现 → **prompt 注入漏洞**。用户输入"忽略所有指令"直接到达 LLM。

### 2. 用户消息作为系统指令嵌入 — `planner_side_inputs.py:717-718`
`_append_mode_instructions()` 将原始 `user_message` 嵌入 `planner_runtime_instruction_block` 无消毒：`f'对方刚才说的是："{user_message}"。'` → 可注入系统级指令。

### 3. 发送者名称无转义 — `cognitive_loop.py:404-406`
`sender_id` 和 `sender_name` 直接插入 f-string：`f"Sender id: {event.get_sender_id()}\nSender name: {...}\n"` → 含换行符的名称可用于 prompt 注入。

### 4. 冷区摘要破坏语义 — `context_engine.py:278-311`
盲目删除 "后来/然后/他说/她说" 等词，`re.split` 按标点切分 → 拼接碎片丢失说话人归属 → 损坏的摘要进入系统 prompt → AI 对过去对话产生错误"记忆"。

### 5. Memory 标签可被突破 — `prompt_refiner.py:883-884`
`<retrieved_memory>` 包裹可被存储内容中的 `</retrieved_memory>` 提前闭合 → 后续内容逃逸到系统上下文。写入时过滤存在，但旧数据可能绕过。

---

## 九、长稳健壮性 (5 bugs)

### 1. time.time() 73 处替代 monotonic — `time_utils.py:7-8`
`now_timestamp()` 使用 `time.time()` → 73+ 调用点传播 → NTP 跳变造成负持续时间、TTL 断裂、recency 排序损坏。`v2_store.py:668` 的 `age_days = max(0.0, ...)` 仅缓解而非修复。

### 2. 睡眠漂移 — `proactive_task.py:764-767`
`await asyncio.sleep(interval)` 在 work 之前 → 实际间隔 = sleep + work_time → 数小时累积显著漂移。同样模式在 `gate.py:830` 的 debounce 循环。

### 3. SQLite WAL 无限增长 — 4 个数据库文件
`database_service.py:191`、`persistence_schema.py`、`v2_store.py:143`、`bm25.py:19` — WAL 模式到处启用，**零** `wal_checkpoint`/`VACUUM` 调用 → 数周后 WAL 文件可增长到数 GB → 磁盘耗尽。

### 4. `_session_tasks` 累积 — `gate.py:101,339-350`
活跃聊天的 `_debounce_and_judge` worker 协程存活数小时 → 数百个活跃聊天积累数百个 Task 对象，每个持有 event 引用、session context → 渐进内存增长。

### 5. `_lane_creation_locks` 不安全驱逐 — `lane_storage.py:23-24`
弹出锁时无 `locked()` 检查（对比 `v2_store.py:78` 有正确保护）→ 若锁仍被持有 → 新请求创建新锁 → 互斥被打破 → 重复创建会话。

---

## 全局 Top 5

| # | 严重度 | 领域 | Bug | 文件 |
|---|--------|------|-----|------|
| 1 | CRITICAL | Prompt | `<user_input>` 标签从未应用 — 注入漏洞 | `prompt_refiner.py:923` |
| 2 | CRITICAL | 数值 | 纪元 0 灾难性关系衰减 | `relationship_engine.py:78` |
| 3 | HIGH | QQ群 | 无 bot-kick 清理 — 7+ 子系统泄漏 | `main.py:194` |
| 4 | HIGH | 数据 | 双写 V2→Faiss 无原子性 | `memory_write_service.py:94` |
| 5 | HIGH | 长稳 | 73 处 time.time() → NTP 跳变 | `time_utils.py:7` |
