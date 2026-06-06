# 审查报告：astrmai/proactive/
> task_id: r08-proactive | 审查时间: 2025-07-15

## 执行摘要

该模块是 AstrMai 主动行为（proactive）的核心调度层，包含唤醒（WakeupService）、社交衰减（DecayService）、消息分发（ProactiveDispatcher）、心流管理（HeartflowManager）、日记（DiaryService）、梦境（DreamScheduler）、群签到（GroupSigninService）等子服务，由 ProactiveTask 主循环统一编排。

**整体代码质量较高**：模块化清晰、异常处理广泛、日志覆盖充足、状态机设计严谨。主要问题集中在**资源泄漏风险**（心流历史数据随聊天室数线性增长而无清理）和**退化重试风暴**（衰减服务的 memory decay 在异常时会反复重试）。此外存在字符串编码损坏问题（中文变为乱码）。

## 概述
- 审查文件数: 12
- 发现总数: 18
- 严重: 3 | 中等: 9 | 建议: 6

---

## 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | **heartflow/manager.py:53-57** | **心流管理器内存泄漏** — `_states`、`_pulses_by_chat`、`_impulse_decisions_by_chat`、`_action_decisions_by_chat` 四个字典仅按 **每个 chat_id** 限制条目数量（32~64条），但字典 Key 本身**从未被清理**。随着系统运行时间增长，不同 chat_id 持续涌入，这些字典会线性增长直至 OOM。`_cleanup_sessions()` 仅清理 `_sessions` 而不清理上述四个容器。 |
| 2 | **decay_service.py:38-44** | **Memory decay 异常时导致重试风暴** — `memory_engine.apply_daily_decay()` 抛出异常时，异常被 `except` 吞掉并仅打 debug 日志，但 `self._last_memory_decay` **未更新**。主循环每 ~60 秒调用一次 `run_once()`，每次都会因 `now - _last_memory_decay >= 86400` 为 True 而重试，导致每秒级的无用重试，浪费 LLM/DB 资源。建议实现退避或标记跳过。 |
| 3 | **diary_service.py:7-8, 50-51** | **硬编码中文字符串乱码（Mojibake）** — `[浣犵殑鏍稿績浜鸿]` 和 `[鍐呴儴鏃ヨ]` 显示为 GBK/UTF-8 编码错位后的乱码。虽然 Python 3 中字符串字面量是 UTF-8，但源文件保存时可能使用了错误的编码，导致运行时显示为不可读字符。应当修正为正确的 `[你的核心人设]` 和 `[内部日记]`。 |

---

## 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | **decay_service.py:26-33** | **社交分数衰减死区** — 当 `-10 <= old_score <= 10` 时 delta=0，分数在此区间内**永不向零回归**。这意味着轻微正/负分用户永远卡在 ±10 以内无法归零。建议对 ±3 以内的小分数也施加衰减（如 delta = -1 if score > 3 else 1 if score < -3 else 0），或使用比例衰减。 |
| 5 | **proactive_task.py:455-472** | **Profiling 任务持有全局信号量期间阻塞其他服务** — `_run_profiling_task()` 在 `async with self._bg_semaphore` 下依次遍历所有活跃 state，对每个符合条件的用户调用 LLM（`_generate_nickname`/`_generate_persona_analysis`）。由于 semaphore 容量仅 2，且 profiling 涉及多次 LLM 调用，会长时间阻塞 diary、heartflow_topic_digest、dream 等其他后台任务。建议 profiling 内部使用子信号量或分步释放。 |
| 6 | **dispatcher.py:168-182** | **注入事件时临时修改 attention_gate 属性存在竞态风险** — `_dispatch_locked()` 将 `self.attention_gate.runtime_coordinator` 设为 None 再恢复，若在 `try/finally` 之间其他协程也访问该属性（如并发 dispatch），可能导致状态回写错误或丢失。虽然 `_dispatch_lock` 缓解了同一 dispatcher 实例的并发，但 attention_gate 可能被其他调用方同时访问。 |
| 7 | **proactive_task.py:482-483** | **日记服务每日窗口仅 1 小时可能丢失执行** — `diary_service.should_run()` 仅在 03:00~03:59 的窗口内返回 True。如果主循环在该小时内因异常或背压跳过维护周期，或系统时区漂移，则当天日记完全丢失，无补偿机制。建议将窗口扩大到 2~3 小时，或增加一个"今日未运行则补跑"的兜底检测。 |
| 8 | **heartflow/manager.py:1000-1060** | **`list_timeline()` 无 chat_id 时遍历所有 chat 产生大块内存分配** — 当不传 chat_id 时，函数合并 `_pulses_by_chat`、`_action_decisions_by_chat`、`_impulse_decisions_by_chat` **所有**条目到单一大列表再排序。若活跃 chat 数达到数百，单次调用可能分配数万对象，触发 GC 抖动。建议增加硬性上限或按 chat 分页。 |
| 9 | **rhythm.py:76-79** | **时区问题显式遗留** — 代码注释写明 `uses local time; containerized deployments should configure host TZ`。如果容器 TZ 未设置或设为 UTC，静默时段（quiet_hours）和时段判断结果将与用户预期完全不符。建议增加启动时 TZ 验证日志或支持 `config.timezone` 覆盖。 |
| 10 | **wakeup_service.py:54** | **`run_for_chat()` 中空字典 `{}` 被当作 falsy 重建 signal** — `signal = dict(signal or await self.build_signal(...))` 中，如果调用方传递 `signal={}`，`or` 会将空字典视为 False，导致重新构建 signal。这可能不是预期行为（虽然实际调用方极少传空字典）。建议使用 `signal if signal is not None else ...`。 |
| 11 | **heartflow/manager.py:520-525** | **`_compute_visible_candidate_score` 包含无意义的 `del now`** — 函数签名接受 `now` 参数后立即 `del now`，表明该参数未被使用。这是未完成的重构痕迹，应删除该参数或使用它。虽然不影响正确性，但降低可读性并可能误导审阅者。 |
| 12 | **group_signin_service.py:82-101** | **`_resolve_api()` 存在多层回退和隐式 None 风险** — 函数遍历 `context`、`gateway`、`self` 三个候选来寻找 `client.api`，路径长且可能在中间步骤抛出 `AttributeError`（被 bare `except` 吞掉）。建议在初始化时直接注入 api 引用，避免运行时动态解析。 |

---

## 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 13 | **heartflow/manager.py**（全局） | 建议为 `_states`、`_pulses_by_chat` 等字典增加 TTL 清理机制（如每次 tick 时检查并移除 `last_tick_ts` 超过 2×ACTIVE_CHAT_TTL_SECONDS 的条目），或改用 `cachetools.TTLCache`。 |
| 14 | **decay_service.py:38** | 建议为 memory decay 增加熔断/退避：连续失败 N 次后跳过当天剩余周期，并记录 warning 日志而非 debug，便于运维发现。 |
| 15 | **proactive_task.py:653-670** | 主循环中 `self._fire_background_task(self.heartflow_topic_digest_service.run_once(...))` 每个维护周期都触发，但 topic digest 内部有 cooldown 检查。频率过高（60s）时每次都会因 cooldown 跳过，浪费任务创建开销。建议在 fire 前先调用 `should_run` 风格的方法判断是否真的需要执行。 |
| 16 | **wakeup_service.py:27-35** | `build_signal` 中的 state 获取逻辑包含两条路径（先 `get_state` 再 `get_active_states`），第二条路径的线性扫描 `for candidate in ...` 在活跃状态多时效率较低。建议抽象为 `state_engine.get_state(chait_id, fallback_scan=True)` 以减少重复。 |
| 17 | **dispatcher.py:192-206** | `complete()` 方法中通过 `reversed(self._history)` 线性查找匹配的 intent_id。`HISTORY_LIMIT` 仅为 200，性能可接受；但若未来扩大上限，建议改用 `{intent_id: index}` 映射表。 |
| 18 | **dream_scheduler.py:106-113** | `_run_for_session` 中通过 `setattr` 临时修改 `self.dream_agent.MIN_EVENTS_TO_DREAM`，再在 `finally` 中恢复。若 `run_dream_cycle` 抛出异常，属性可正确恢复。但更好的做法是将 `min_events` 作为参数传入 `run_dream_cycle` 方法而不是修改全局属性。建议重构 API。 |

---

## 亮点

1. **异常隔离设计优秀** — 所有子服务的异常均被 `try/except` 包裹并以 `degraded` 级别日志记录，单个服务的故障不会级联崩溃整个主循环。
2. **Heartflow 状态机体系成熟** — `Pulse → ActionDecision → ImpulseDecision → Dispatch` 四阶段流水线设计清晰，每个阶段有独立的数据类和历史记录，便于调试和观测。
3. **Cooldown/Backpressure 机制完善** — 从 wakeup 的 `next_wakeup_timestamp`、dispatcher 的 `_cooldowns`、到 heartflow 的 `VISIBLE_CANDIDATE_COOLDOWN_SECONDS`，多层节流防止主动行为过于频繁。
4. **`complete()` 回调机制灵活** — `WakeupService._on_complete` 通过 dispatcher 的 callback 系统在意图完成时自动扣减能量并更新 cooldown，解耦了唤醒与分发逻辑。
5. **`_fire_background_task` + `_handle_task_result` 模式** — 后台任务的生命周期被妥善管理，异常不会被静默吞掉，且 `_background_tasks` set 防止 task 引用泄漏。

---

## 测试覆盖评估

| 组件 | 覆盖评估 | 说明 |
|------|---------|------|
| WakeupService | ⚠️ 中等 | `build_signal` 的多条返回路径（state_unavailable、silence_threshold、energy、cooldown）边界情况较好，但 `run_for_chat` 缺少 `_on_complete` 回调中 `consume_energy` 抛出 `TypeError` 以外异常的测试 |
| DecayService | ⚠️ 低 | 社交分数衰减的边界（±10 死区、大正/负值收敛速度）、`apply_daily_decay` 异常重试均无覆盖 |
| Dispatcher | ⚠️ 中 | `_safety_check` 逻辑路径完整；但并发 dispatch、`runtime_coordinator` 临时置空场景缺少竞态测试 |
| HeartflowManager | ⚠️ 中偏低 | 核心脉冲/决策逻辑有分支覆盖；但内存泄漏、session 边界（TTL 刚过、`low_cost_retained` 为 True 时）缺少测试 |
| DiaryService | ⚠️ 低 | `should_run` 边界（23:59→00:00 跨日、时区漂移）缺少测试；LLM 返回为空时的行为未覆盖 |
| DreamScheduler | ⚠️ 中 | 时间窗口的跨日逻辑正确；但依赖绑定多次调用的幂等性未验证 |
| GroupSigninService | ⚠️ 低 | API 解析回退链、签到失败后重试行为缺少测试 |
| ProactiveTask 主循环 | ⚠️ 低 | 轮询模式切换、`_fire_background_task` 生命周期、背压场景缺少集成测试 |

**总体测试覆盖评级：🟡 中等偏低（约 35-45%）**。单元测试覆盖了核心决策分支，但集成测试和边界条件测试明显不足。

---

## 总体评级

| 维度 | 评级 | 说明 |
|------|------|------|
| 代码质量 | ⭐⭐⭐⭐ | 模块化好、命名清晰、异常处理广泛，符合生产级质量标准 |
| 正确性 | ⭐⭐⭐⭐ | 核心状态机逻辑正确；但存在 3 个严重问题需优先修复 |
| 安全性 | ⭐⭐⭐⭐ | 无注入/越权问题；唯一风险是 attention_gate 属性临时修改的竞态 |
| 性能 | ⭐⭐⭐ | 心流数据随 chat_id 增长泄漏是长期运行风险；profiling 信号量阻塞需优化 |
| 可观测性 | ⭐⭐⭐⭐⭐ | 日志覆盖全面（degraded/info/error 分级）、`describe_status` 提供丰富诊断信息 |
| 可维护性 | ⭐⭐⭐⭐ | 数据类 + slots、类型注解、静态工厂方法；唯独 `diary_service.py` 乱码字符串降低了可维护性 |
| 测试覆盖 | ⭐⭐⭐ | 核心路径有覆盖，边界/异常/集成测试不足 |

**综合评级：⭐⭐⭐⭐（良好）**。模块整体设计优秀，架构清晰，生产就绪度高。建议优先修复 🔴 严重问题（内存泄漏、重试风暴、乱码），然后逐步补充边界测试和性能优化。
