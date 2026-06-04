# 审查报告：astrmai/proactive/
> task_id: r12-proactive | 审查时间: 2025-07-10

## 概述
- 审查文件数: 12
- 发现总数: 18
- 严重: 4 | 中等: 8 | 建议: 6

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `rhythm.py:72-76` | **`evaluate_proactive_rhythm` 中 `getattr(None, …)` 崩溃风险。** `reply = getattr(config, "reply", None)` 在 `config` 不包含 `reply` 属性时返回 `None`，随后 `getattr(reply, "base_frequency", 0.7)` 等价于 `getattr(None, …)`，在 Python 中会抛出 `AttributeError`。任何通过 `dispatcher._safety_check` / `heartflow` 等路径调用该函数且 `config` 缺少 `reply` 段的场景都会崩溃。|
| 2 | `dream_scheduler.py:99-104` | **`session_id` 被私有属性 `_last_session_id` 覆盖，丢失调用方上下文。** `run_dream_cycle(session_id=session_id)` 被调用后，第 104 行用 `self.dream_agent._last_session_id` 重新赋值 `session_id`。如果 `DreamAgent` 内部将 `session_id=None` 视为"全局"并返回 `"global"`，则第 99 行传入的具体 `session_id`（例如来自 `run_once_for_session("GroupMessage:xxx")`）会被丢弃，导致后续记忆操作（第 107–137 行）作用在错误的 session 上。|
| 3 | `decay_service.py:19-28` | **DecayService 修改状态但依赖外部持久化，无显式 flush 保障。** `profile.is_dirty = True` 仅标记脏状态，但 DecayService 自身不调用任何 `save_*` 方法。若 `state_engine` 或其他消费方未能及时 flush，所有 socre 衰减和 `last_access_time` 更新的内存修改会在进程重启时丢失。|
| 4 | `dream_scheduler.py:77-78` | **每次调用 `_run_for_session` 都修改 `dream_agent` 的类级别属性 `MIN_EVENTS_TO_DREAM`。** 第 77–78 行无条件执行 `self.dream_agent.MIN_EVENTS_TO_DREAM = min_events`，在 `DreamAgent` 可能被多个消费者共享（或此类属性被当作常量引用）时，存在竞态和不可预期的副作用。|

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 5 | `diary_service.py:29-43` | **`prompt_registry` 为 None 时，记忆提取成为无意义的 I/O 开销。** 第 33–35 行的 `extract_and_summarize_history` 在每个活跃群上执行一次记忆提取，但当 `prompt_registry` 为 None 时，`diary` 始终为 None，后续的 `record_cognitive_feedback` 和 `add_memory` 全部跳过。这些提取工作产生了副作用但没有被使用，浪费后端资源。|
| 6 | `dream_scheduler.py:104` | **通过 `self.dream_agent._last_session_id` 访问私有属性，产生紧耦合。** `DreamAgent` 变更该属性名或移除此属性时，`DreamScheduler` 静默失效。应通过公有 API（如 `dream_agent.last_session_id` property）暴露。|
| 7 | `heartflow/manager.py:503` | **`del now` 在 `_compute_visible_candidate_score` 中是死参数。** 参数 `now` 被接受后立即被删除，表明该参数是在重构中引入但未使用后遗留下来的。应移除签名中的 `now: float` 或将 `del now` 改为 `_` 前缀。|
| 8 | `proactive_task.py:251-253` | **`_save_user_profile` 的 `except TypeError` 回退模式掩盖了其他 TypeError 含义。** 如果 `persistence.save_user_profile(profile)` 因参数数目以外的原因抛出 `TypeError`（例如 profile 对象为 None），回退调用 `save_user_profile(user_id, profile)` 也可能失败，异常向上传播但原始错误信息丢失。|
| 9 | `proactive_task.py:206` | **`_bind_dream_dependencies` 在 `start()` 和 `set_db_service()` 间存在潜在的双重初始化竞态。** `start()` 检查 `self.dream_agent is None` 并调用 `_bind_dream_dependencies`；`set_db_service()` 同样检查相同条件。虽然 asyncio 是单线程，但如果两个方法在同一个事件循环 tick 中被连续调用，`dream_agent` 可被创建两次（第二次覆盖第一次）。|
| 10 | `diary_service.py:35` | **`render_template` 调用缺少异常保护。** 第 35 行 `self.prompt_registry.render_template(...)` 若抛出异常（例如模板未找到、参数缺失），将直接导致整个 `run_once` 方法崩溃，影响所有正处理的群状态。应包裹 `try-except` 并降级为 `diary = None`。|
| 11 | `group_signin_service.py:114-131` | **`_resolve_api` 的属性遍历链过于脆弱。** 通过 `context.client.api` / `gateway.client.api` / `self.client.api` + `get_client()` 回调的多层 fallback 链高度依赖运行时对象的内部属性结构。任一环节的属性缺失或类型变更都会导致签到静默跳过，无有效日志。|
| 12 | `proactive_task.py:434` | **`handle_chat_heartbeat` 硬编码 `dispatch_mode = "observe_only"`。** 心跳处理始终返回 `observe_only` 模式，意味着此 handler 不能根据实际心跳决策动态调整调度模式。如果将来需要心跳触发主动行为（如低优先级唤醒），需要重构 handler 签名。|

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 13 | `diary_service.py:52` | **空日记字符串 `""` 被静默跳过。** `if diary and …` 中，空字符串为 falsy，导致日记反馈和记忆录制被跳过。调用方无法区分"prompt 返回空结果"和"未生成日记"。建议明确判断 `if diary is not None and diary.strip():`。|
| 14 | `decay_service.py:24-28` | **分数调整逻辑无效应更新 `last_access_time`。** 当 `|old_score| > 10` 时调整 ±1（在 `[-100, 100]` 范围内），但如果分值的绝对值在 10–11 之间，调整后可能仍在同一范围，下次再进入此分支时才可能跨越 10 边界。`last_access_time` 被重置为 `now` 导致 86400 秒的保护间隔无效延长。建议仅在净变化非零时更新 `last_access_time`。|
| 15 | `proactive_task.py:__init__` | **`ProactiveTask.__init__` 约有 50 个实例属性。** 大量的标量属性（`_last_*` 计数器、状态标记）使 `__init__` 从第 30 行延伸到第 100+ 行。建议将调度统计（如 `_last_due_chat_count` 等）归组到 `@dataclass` 中以提升可读性。|
| 16 | `heartflow/manager.py:396-420` | **`_build_chat_state` 和 `_refresh_session_rhythm` 中存在大量魔法数字。** 例如 `0.12`、`0.45`、`0.28`、`0.35`、`0.08`、`0.10`、`0.22`、`0.24`、`0.34`、`0.26` 等权重散落在公式中。建议提取为类级别命名常量（如 `INTEREST_BASE = 0.12`），便于调参和文档化。|
| 17 | `proactive_task.py:_loop` | **主循环开始时先 sleep 再执行任务。** `_loop` 第 721 行 `await asyncio.sleep(...)` 位于循环体开头，意味着启动后第一个全局维护周期（`decay_service` / `diary_service` 等）需要等待一个完整的 poll 间隔（至少 5–15 秒）才能执行。建议先执行一次 `_run_chat_heartbeat_pass()` + 全局维护再进入 `while` 循环。|
| 18 | `__init__.py:53-66` | **Lazy import `__getattr__` 中的 `module_map` 可自动生成。** 当前手动维护字符串到模块名的映射，与 `__all__` 存在重复。可通过检查 `__all__` 并基于约定（`PascalCase` → `snake_case`）自动生成映射，减少维护负担。|

## 亮点

- **模块解耦优秀**：`DiaryService`、`DecayService`、`DreamScheduler`、`HeartflowManager` 等子服务职责清晰，通过 `ProactiveTask` 作为编排层聚合，整体架构易于理解和扩展。
- **降级设计良好**：全模块大量使用 `try-except` + logger.debug 的降级模式（degraded pattern），确保单个子任务失败时不会拖垮整个调度循环。
- **`ProactiveDispatcher` 安全校验完整**：`_safety_check` 方法覆盖了注意力门控、静默时段、cooldown、能量阈值、talk_willingness 等多个维度的检查，安全性意识强。
- **`HeartflowManager` 的 session 生命周期管理**：从 `_materialize_session` 到 `_refresh_session_rhythm` 的时序逻辑设计精细，topic_heat 的保留/衰减算法考虑了 tick 间隔，展现了对实时聊天状态的深入建模。
- **丰富的可观测性**：`describe_status()` 方法在几乎所有子服务中实现，返回大量调试指标，便于线上问题定位。

## 总结

`astrmai/proactive/` 模块整体质量较高，架构清晰，降级处理和可观测性强。关键的四个 🔴 严重问题集中在**错误路径的安全性**上：`rhythm.py` 在 config 不完整时会崩溃、`dream_scheduler.py` 的 `session_id` 传递可能丢失调用方上下文、`decay_service.py` 的持久化责任边界模糊、`dream_agent.MIN_EVENTS_TO_DREAM` 的类属性副作用。这些问题在正常配置和典型运行时路径下不会触发，但在边缘情况（config 缺少 reply 段、非标准 session_id 传入）存在真实风险。建议优先修复 `rhythm.py:76`（添加类型守卫）和 `dream_scheduler.py:104`（改为公有 API 访问），其余中等和建议项可在后续迭代中逐步优化。
