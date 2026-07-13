# AstrMai 最终功能审计：Learning 与 Proactive

## 审计结论

本报告基于当前工作树（包含未提交的生产代码变更）审计 `astrmai/learning/` 与 `astrmai/proactive/`。为证明真实生产可达性，仅追踪了装配、生命周期、消息入口、Planner/Executor、状态持久化、Memory API 与热配置入口；未读取测试，`astrmai/infrastructure/security/` 按要求视为不透明依赖。

共确认 **15 项可达功能缺陷**：**P1 5 项、P2 9 项、P3 1 项**。未发现 P0 缺陷。

## Finding 10-01：普通消息日志没有接入 mining 触发链，表达与黑话挖掘不会自动运行

- **ID / 严重级别**：AM-LP-10-01 / **P1**
- **文件:行**：`astrmai/learning/evolution_manager.py:247`；`astrmai/learning/evolution_manager.py:255`；`astrmai/learning/evolution_manager.py:358`；`astrmai/presentation/events/message_entry.py:200`；`astrmai/app/plugin_facade.py:385`
- **触发条件**：任意正常用户消息和 Bot 回复持续进入生产聊天链，即使未处理日志数量达到 `evolution.mining_trigger`。
- **真实调用链**：`main.py:on_global_message()` → `handle_global_message()` → `PluginFacade.record_and_dispatch_attention()` → `EvolutionManager.record_user_message()` → `_append_message_log()` / `MessageRecorder.record()`；回复侧为 `Executor._finalize_reply()` → `EvolutionManager.process_bot_reply()` → `BotReplyRecorder.record()`。
- **实际行为**：用户消息路径调用 `self.recorder.record()` 后忽略其布尔触发结果，也没有调度 `_try_trigger_mining()`；Bot 回复路径同样只写日志和发布事件。当前生产调用图中，唯一调度 `_try_trigger_mining()` 的位置在 `process_feedback()`，但该方法没有生产调用者。因此 `_load_unprocessed_logs()`、`process_logs_and_mine()`、表达挖掘、黑话挖掘和 `MiningCompletedEvent` 都不会由真实聊天流量触发。
- **期望行为**：达到时间窗/消息阈值时，正常用户或 Bot 日志路径应可靠调度每会话 mining，并由现有会话锁完成去重和串行化。
- **生产影响**：消息日志会持续积累为未处理状态，表达模式和群内黑话长期不产生，后续自动审核、人格表达注入和知识更新事件失去数据源。
- **现有守卫为何失效**：`MessageRecorder` 正确计算了时间窗、最小消息数和冷却，但调用方没有消费返回值；`_try_trigger_mining()` 本身虽有阈值与每组锁，却没有可达入口。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 10-02：两个生命周期调度器并发消费同一反思队列，会重复调权并误删未处理批次

- **ID / 严重级别**：AM-LP-10-02 / **P1**
- **文件:行**：`astrmai/app/bootstrap.py:378`；`astrmai/app/bootstrap.py:387`；`astrmai/app/lifecycle.py:61`；`astrmai/app/lifecycle.py:62`；`astrmai/proactive/proactive_task.py:506`；`astrmai/proactive/proactive_task.py:802`；`astrmai/learning/review/expression_governance_runner.py:72`；`astrmai/learning/review/reflector.py:81`；`astrmai/learning/review/reflector.py:149`
- **触发条件**：主动服务开启且反思队列至少有 3 条记录时，`ProactiveTask` 的 5-15 秒循环与 `ExpressionGovernanceRunner` 的周期循环重叠。
- **真实调用链**：Bootstrap 创建一个共享 `ExpressionReflector`，同时传给 `ExpressionGovernanceRunner` 和 `ProactiveTask` → 生命周期依次启动两个循环 → 两边分别调用同一个 `reflector.reflect_batch(chat_id)`。
- **实际行为**：`reflect_batch()` 仅在锁内复制队首最多 8 条，随后释放锁执行 LLM 与权重更新；两个调用可拿到完全相同的 batch 并重复调整权重。完成时两边都按当前队列执行 `self._pending_reflections = self._pending_reflections[len(batch):]`，第二个完成者可能把第一批之后从未评估的记录一并切掉。
- **期望行为**：同一反思器只能有一个批次消费者，或在锁内原子 claim 具体记录并按记录 ID ack；并发完成不能删除不属于自身批次的项目。
- **生产影响**：表达权重被重复放大/降低，后续待反思记录静默丢失，同时产生重复 LLM 成本；表达治理结果不再对应真实使用反馈。
- **现有守卫为何失效**：`asyncio.Lock` 只保护“读取快照”和“按长度切片”两个短区段，没有保护批次所有权或整个处理事务；两个生命周期服务也没有共享调度锁。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 10-03：反思批次部分写入失败时整批重试，已成功的权重调整会被重复应用

- **ID / 严重级别**：AM-LP-10-03 / **P2**
- **文件:行**：`astrmai/learning/review/reflector.py:121`；`astrmai/learning/review/reflector.py:132`；`astrmai/learning/review/reflector.py:145`；`astrmai/learning/review/reflector.py:149`；`astrmai/learning/review/reflector.py:262`
- **触发条件**：同一反思 batch 中有多条需要加权/降权的记录，前面的 `adjust_weight()` 成功，而后面的记录发生瞬时存储失败或返回失败。
- **真实调用链**：Planner 记录表达使用 → 任一反思调度器调用 `reflect_batch()` → LLM 返回评分 → 逐条 `_adjust_canonical_pattern_weight()` → canonical store 更新权重。
- **实际行为**：权重更新逐条提交且没有事务。任一后续更新失败会把 `weight_update_failed` 置真并保留整批；下一轮会再次处理整个 batch，使此前已成功的非幂等 `delta` 再应用一次。连续失败会反复叠加同一反馈。
- **期望行为**：批次应原子提交，或逐项记录成功状态，仅重试未成功项目；同一评分不能重复作用于已提交记录。
- **生产影响**：单条存储故障可使无关表达的权重持续漂移，最终造成错误淘汰、过度强化和注入排序失真。
- **现有守卫为何失效**：失败时“保留批次”保护了数据不被整体丢弃，却没有回滚前序写入，也没有幂等键或逐项 ack，因此重试会重复副作用。
- **分类**：confirmed（已确认）
- **置信度**：0.99

## Finding 10-04：`pending_human` 表达会被反复自动审核并重置 sent 标记，群里可每轮重复收到同一审核问题

- **ID / 严重级别**：AM-LP-10-04 / **P1**
- **文件:行**：`astrmai/learning/review/expression_auto_check_task.py:36`；`astrmai/learning/review/expression_auto_check_task.py:50`；`astrmai/learning/review/expression_auto_check_task.py:113`；`astrmai/learning/review/expression_auto_check_task.py:128`；`astrmai/learning/review/reflect_tracker.py:35`；`astrmai/proactive/review_dispatcher.py:17`；`astrmai/memory/services/expression_pattern_service.py:208`
- **触发条件**：自动审核把某表达判为 `revision_needed`，并且后续模型仍返回 `revision_needed`，人工尚未处理该问题。
- **真实调用链**：`ExpressionGovernanceRunner._loop()` → `ExpressionAutoCheckTask.run_once()` → `list_reviewable_patterns()` → `_review_pattern()` → `_apply_review()` → `ReflectTracker.queue_review_request()` → `ReviewDispatcher.dispatch_pending()` → `context.send_message()`；下一治理周期重复同一路径。
- **实际行为**：规范查询明确把 `pending_human` 继续列为自动可审核项。每次再次得到 `revision_needed` 时，`queue_review_request()` 用同一 pattern ID 覆盖 `_pending` 项并把 `sent` 重置为 `False`；随后 Dispatcher 再次发送并标记已发。默认间隔下，同一人工审核问题可以约每分钟重复推送。
- **期望行为**：进入 `pending_human` 后应退出自动审核队列，保留一次稳定的人工请求，直到人工决策或显式重新排队。
- **生产影响**：群聊被重复审核消息打扰，管理员可能对同一条目多次操作；主动消息噪声还会干扰正常会话与 Heartflow 活跃度判断。
- **现有守卫为何失效**：`_last_run_at` 只提供最短时间间隔；`sent` 是内存字段且会被重新排队覆盖；数据库状态没有把“等待人工”与“可继续自动审核”分离。
- **分类**：confirmed（已确认）
- **置信度**：0.99

## Finding 10-05：每日规范记忆衰减缺少必填参数，失败又被当作当天已完成

- **ID / 严重级别**：AM-LP-10-05 / **P1**
- **文件:行**：`astrmai/proactive/decay_service.py:59`；`astrmai/proactive/decay_service.py:61`；`astrmai/proactive/decay_service.py:63`；`astrmai/proactive/decay_service.py:65`；`astrmai/memory/services/memory_engine.py:668`
- **触发条件**：Proactive 维护循环启动后首次达到记忆日衰减检查。
- **真实调用链**：`PluginLifecycle.start_proactive_services()` → `ProactiveTask._loop()` → `_run_maintenance_cycle()` → `DecayService.run_once()` → `MemoryEngine.apply_daily_decay()`。
- **实际行为**：`MemoryEngine.apply_daily_decay(decay_rate, days=1)` 要求必填 `decay_rate`，调用点却不传参数，必然抛出 `TypeError`。同时 `_last_memory_decay` 在调用前已经设为当前时间；异常被吞掉后，接下来 24 小时所有维护轮次都被节流守卫跳过。
- **期望行为**：从当前 Memory 配置传入衰减率，并只在衰减成功后推进日执行标记；失败应保留重试机会。
- **生产影响**：规范记忆不会执行日衰减和伴随的过期治理，旧记忆长期保持过高分值并持续累积，影响检索排序与容量。
- **现有守卫为何失效**：异常捕获保护了主循环，却没有回滚已经推进的 `_last_memory_decay`；节流逻辑因此把失败当成成功。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 10-06：每次成功 wakeup 后持久化 cooldown 都以错误参数调用，重启后可提前再次主动回复

- **ID / 严重级别**：AM-LP-10-06 / **P1**
- **文件:行**：`astrmai/proactive/wakeup_service.py:178`；`astrmai/proactive/wakeup_service.py:183`；`astrmai/proactive/wakeup_service.py:186`；`astrmai/proactive/wakeup_service.py:188`；`astrmai/infrastructure/persistence/state_profile_persistence.py:105`；`astrmai/conversation/planning/planner.py:882`
- **触发条件**：wakeup 合成事件通过正常 Planner 链产生并发送一条可见回复。
- **真实调用链**：Heartbeat → `ProactiveTask.handle_wakeup_signal()` → `WakeupService.run_for_chat()` → `ProactiveDispatcher.dispatch()` → `AttentionGate.inject_external_event()` → Planner/Executor/ReplyService 发送 → Planner 完成回调 → Wakeup `_on_complete()`。
- **实际行为**：完成回调先消耗能量，再设置 `target_state.next_wakeup_timestamp`，随后调用 `self.persistence.save_chat_state(target_state)`。真实持久化 API 签名是 `save_chat_state(chat_id, state)`，所以该调用每次都抛 `TypeError`；Planner 捕获回调异常后仍把本次主动回复视为已完成。cooldown 只留在当前内存对象中，没有写入数据库。
- **期望行为**：成功发送后的能量、回复时间和 `next_wakeup_timestamp` 应作为同一状态更新可靠持久化，完成回调不应发生签名错误。
- **生产影响**：插件重载/进程重启后会恢复旧 cooldown；当静默阈值短于 wakeup cooldown 时，Bot 可远早于配置再次主动回复，造成重复打扰。日志中还会在每次成功 wakeup 后出现被降级吞掉的完成回调异常。
- **现有守卫为何失效**：Dispatcher 的内存 cooldown 和状态对象在重启后都消失；Planner 对回调异常的捕获只保证回复链不崩溃，无法补偿持久化失败。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 10-07：Heartflow 在分发前就提交 15 分钟可见候选 cooldown，发送失败也会压制后续候选

- **ID / 严重级别**：AM-LP-10-07 / **P2**
- **文件:行**：`astrmai/proactive/heartflow/manager.py:402`；`astrmai/proactive/heartflow/manager.py:404`；`astrmai/proactive/heartflow/manager.py:405`；`astrmai/proactive/heartflow/manager.py:748`；`astrmai/proactive/heartflow/manager.py:795`；`astrmai/proactive/heartflow/manager.py:858`；`astrmai/proactive/heartflow/manager.py:961`
- **触发条件**：Heartflow 产生 `proactive_candidate`，但 Dispatcher 随后因运行时快照变化、注入异常、缺少 AttentionGate 或合成事件未排队而失败/阻止。
- **真实调用链**：Heartbeat → `ProactiveTask.handle_heartflow_signal()` → `HeartflowManager.tick_chat()` → `_build_action_decision()` → `_remember_action_decision()` → `_maybe_dispatch_visible_candidate()` → `ProactiveDispatcher.dispatch()`。
- **实际行为**：`tick_chat()` 在调用 Dispatcher 前先记录 action；`_remember_action_decision()` 看到 `proactive_candidate` 就立即把 `session.last_visible_candidate_ts` 设为当前时间。即使后续 dispatch 返回 blocked、抛异常或 `synthetic_event_queued=False`，该时间也不会回滚。后续 `_build_impulse_decision()` 将它解释为真实的 recent proactive cooldown，并阻止 15 分钟内的新候选。
- **期望行为**：可见候选 cooldown 应在合成事件确认排队，最好在可见回复确认发送后提交；失败/阻止不能消耗 cooldown。
- **生产影响**：一次瞬时分发故障会让 Heartflow 在接下来 15 分钟保持沉默，造成主动回复无故延迟或丢失。
- **现有守卫为何失效**：Dispatcher 自身返回了准确的 allowed/queued 结果，但 action 状态已在调用它之前提交；后续错误分支只修改 impulse decision，没有恢复 session 时间戳。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 10-08：日记任务在随机延迟和实际处理前先确认当天完成，单会话失败会跳过其余会话且不再重试

- **ID / 严重级别**：AM-LP-10-08 / **P2**
- **文件:行**：`astrmai/proactive/proactive_task.py:519`；`astrmai/proactive/proactive_task.py:804`；`astrmai/proactive/proactive_task.py:805`；`astrmai/proactive/proactive_task.py:806`；`astrmai/proactive/diary_service.py:31`；`astrmai/proactive/diary_service.py:38`；`astrmai/proactive/diary_service.py:55`
- **触发条件**：凌晨日记窗口命中后，在最多 300 秒 jitter 期间发生热重载/停止，或任一 active state 的摘要、记忆读取、模板/LLM 调用抛出异常。
- **真实调用链**：`ProactiveTask._loop()` → `DiaryService.should_run()` → 先设置 `_last_diary_date` → fire-and-forget `_run_daily_diary_task_with_jitter()` → sleep → `DiaryService.run_once(active_states)` → 逐会话摘要与日记生成。
- **实际行为**：日期在后台任务开始前就被推进。`DiaryService` 的会话循环没有每会话异常隔离，前一会话异常会中止整个任务并跳过后续会话；任务完成回调只记录异常。当前运行时同一天不会再次调度。jitter 期间被取消也具有相同结果。
- **期望行为**：应在成功完成后按会话/日期确认，单会话失败不影响其他会话，并为失败或取消保留窗口内重试。
- **生产影响**：一次局部故障或正常热重载即可使部分甚至全部活跃会话缺失当天日记与认知反馈，连续运行状态与长期记忆出现不可见空洞。
- **现有守卫为何失效**：`should_run()` 只检查全局 `_last_diary_date`；后台任务集合负责取消和日志，不保存逐会话进度，也不会撤销已写日期。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 10-09：黑话审核先提交 active 状态再投影，投影失败后该记录永久退出重试队列

- **ID / 严重级别**：AM-LP-10-09 / **P2**
- **文件:行**：`astrmai/learning/review/jargon_auto_check_task.py:98`；`astrmai/learning/review/jargon_auto_check_task.py:101`；`astrmai/learning/review/jargon_auto_check_task.py:183`；`astrmai/learning/review/jargon_auto_check_task.py:196`；`astrmai/learning/review/jargon_auto_check_task.py:203`；`astrmai/learning/review/expression_governance_runner.py:78`
- **触发条件**：LLM 批准一个 `review_pending` 黑话；规范 store 的 `update_memory()` 成功，但随后 `projector.project()` 发生瞬时失败。
- **真实调用链**：Governance runner → `JargonAutoCheckTask.run_once()` → `_review_candidate()` → `_apply_review()` → `MemoryV2Store.update_memory(status="active")` → `MemoryIndexProjector.project()`。
- **实际行为**：规范记录先成为 `active/auto_and_tool`，投影异常随后向上抛出。下一轮查询只取 `statuses=["review_pending"]`，因此这个已 active 但未投影的记录不会被再次选择，投影缺口没有修复路径。
- **期望行为**：状态激活和索引投影应具备可恢复提交协议；投影失败时记录应保持待投影状态或进入独立重试队列。
- **生产影响**：后台显示已批准的黑话可能无法通过向量/索引检索参与实际对话；同一异常还会中断该轮剩余治理任务。
- **现有守卫为何失效**：`changed` 只证明规范记录更新成功；没有 pending-projection 标记、补偿任务或对 active 未投影记录的扫描。
- **分类**：confirmed（已确认）
- **置信度**：0.99

## Finding 10-10：Dream 写回或可见推送失败后仍报告 performed 并消耗全局间隔

- **ID / 严重级别**：AM-LP-10-10 / **P2**
- **文件:行**：`astrmai/proactive/dream_scheduler.py:119`；`astrmai/proactive/dream_scheduler.py:132`；`astrmai/proactive/dream_scheduler.py:147`；`astrmai/proactive/dream_scheduler.py:153`；`astrmai/proactive/dream_scheduler.py:155`
- **触发条件**：Dream Agent 和 Generator 已产生结果，但认知反馈写入、两条记忆写入或 `dream_visible` 的主动发送任一失败。
- **真实调用链**：Heartbeat → `ProactiveTask.handle_dream_signal()` → `DreamScheduler.run_once_for_session()` → Dream Agent/Generator → promotion/feedback/memory writeback → 可选 `context.send_message()`。
- **实际行为**：所有写回和发送异常都被单独吞掉；无论是否存在失败，末尾仍更新全局 `_last_dream_time` 并返回 `performed=True`。后续所有会话在 `_dream_interval` 内均被全局 cooldown 阻止，失败副作用没有重试。
- **期望行为**：结果应区分维护完成、写回完成和可见发送完成；可恢复失败应进入补偿/重试，不能把全链成功与部分失败混为一体。
- **生产影响**：Dream 可能实际没有进入记忆或没有发给配置目标，但运行态显示已完成，并在整个间隔内不再尝试；部分写入成功时还会形成不一致的双写状态。
- **现有守卫为何失效**：逐步骤 try/except 只保证调度循环存活，错误没有汇总到返回值；全局 cooldown 无条件在末尾推进。
- **分类**：confirmed（已确认）
- **置信度**：0.99

## Finding 10-11：群签到成功后的状态持久化失败被吞掉，重启会重复签到并可能重复主动消息

- **ID / 严重级别**：AM-LP-10-11 / **P2**
- **文件:行**：`astrmai/proactive/group_signin_service.py:57`；`astrmai/proactive/group_signin_service.py:63`；`astrmai/proactive/group_signin_service.py:65`；`astrmai/proactive/group_signin_service.py:145`；`astrmai/proactive/group_signin_service.py:148`；`astrmai/proactive/group_signin_service.py:150`
- **触发条件**：平台 `set_group_sign` 已成功，但保存 `group_config.group_signin.last_date` 时数据库暂时失败；当天 8 点窗口内随后发生插件或进程重启。
- **真实调用链**：每分钟维护 → `GroupSigninService.run_once()` → `_sign_group()` 外部动作成功 → `_mark_signed()` → `save_chat_state()` 失败并吞掉 → `_dispatch_after_sign()`；重启后从旧持久化状态再次进入相同链路。
- **实际行为**：内存 bucket 在保存前已经改成今天，所以当前进程不重试；保存失败不向调用方返回失败，代码仍记录 signed 并发送 follow-up。重启恢复旧日期后会再次执行外部签到并再次尝试主动消息。
- **期望行为**：外部成功与本地幂等标记之间应有可恢复状态；持久化失败至少不能被当成完整完成，重启后应能识别已执行动作而不重复可见副作用。
- **生产影响**：群签到和伴随主动发言在同一天重复，给群成员造成明显打扰；运行日志还会错误声明状态已经可靠记录。
- **现有守卫为何失效**：`_already_signed_today()` 只读取本地 state；平台动作没有幂等记录，保存异常又被 `_mark_signed()` 内部吞掉，调用者无法停止后续 dispatch。
- **分类**：confirmed（已确认）
- **置信度**：0.98

## Finding 10-12：热配置只替换顶层引用，学习治理参数与 Dream 派生字段继续使用旧值

- **ID / 严重级别**：AM-LP-10-12 / **P2**
- **文件:行**：`astrmai/app/plugin_facade.py:193`；`astrmai/app/plugin_facade.py:203`；`astrmai/app/plugin_facade.py:206`；`astrmai/app/plugin_facade.py:215`；`astrmai/learning/evolution_manager.py:56`；`astrmai/proactive/proactive_task.py:156`；`astrmai/proactive/dream_scheduler.py:21`
- **触发条件**：运行中热应用 `evolution.review_runner_interval_sec`、`review_batch_size`、`review_min_count`、mining window/count，或 `life.dream_interval_min` / `dream_visible`。
- **真实调用链**：Plugin Pages 配置应用 → `PluginFacade.apply_hot_config()` → 遍历顶层组件 `refresh_config()` → 存活的 governance runner、auto-check tasks、miners、recorder 与 DreamScheduler 继续运行。
- **实际行为**：热应用列表不包含 `ExpressionGovernanceRunner`、Reflector 和两个 auto-check task；`EvolutionManager.refresh_config()` 只替换自身 `config`，没有刷新已构造的 miner/extractor/recorder。`ProactiveTask.refresh_config()` 会替换 DreamScheduler 的 `config` 引用，却不重新计算构造时缓存的 `_dream_interval`，也不更新单独的 `dream_visible` 字段。热应用返回成功后这些行为仍按旧值运行。
- **期望行为**：声明可热应用的配置应传播到全部存活消费者并重算派生字段；不能热更的字段应明确要求重启，而不是报告已应用。
- **生产影响**：管理员调低审核频率、调整 batch/阈值、关闭可见 Dream 或改变 Dream 周期后，生产行为与界面配置不一致，可能继续产生不期望的后台负载或可见消息。
- **现有守卫为何失效**：热应用只验证顶层 `refresh_config()` 没抛异常，没有检查下游对象仍持有旧配置或缓存值，也没有对实际运行参数做回读。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 10-13：Proactive 事件在 Executor 返回 `None` 时没有结算回调，wakeup 状态与 callback 会长期悬挂

- **ID / 严重级别**：AM-LP-10-13 / **P2**
- **文件:行**：`astrmai/proactive/dispatcher.py:274`；`astrmai/proactive/dispatcher.py:275`；`astrmai/conversation/execution/executor.py:551`；`astrmai/conversation/execution/executor.py:564`；`astrmai/conversation/planning/planner.py:1383`；`astrmai/conversation/planning/planner.py:1396`；`astrmai/conversation/planning/planner.py:1412`
- **触发条件**：wakeup 合成事件已被接受，但在模型执行前因 freshness 过期、工具返回 wait signal、模型池失败或其他合法无回复路径使 Executor 返回 `None`。
- **真实调用链**：`WakeupService.run_for_chat(on_complete=...)` → Dispatcher 保存 `_callbacks[intent_id]` → 注入正常 Planner/Executor 链 → Executor 返回 `None` → `Planner._finalize_plan_result()` 的 stale/no-reply 分支。
- **实际行为**：`reply_text is None` 分支记录 trace 后直接返回，没有调用 `_finalize_proactive_event()`；只有有文本的分支在后面结算回调。Dispatcher 的 callback 不会被 pop，wakeup 的能量/cooldown回调也不执行，且没有超时清理器。内存 callback 会持续增长，状态侧仍认为未完成，后续 cooldown 到期后可重复生成同类候选。
- **期望行为**：所有终止路径都应恰好一次调用 proactive completion，明确传入 `reply_sent=False`，释放 callback 并让来源服务按未发送结果处理。
- **生产影响**：高并发或慢模型场景会累积悬挂 callback 和重复 LLM 尝试；主动回复可能延迟到后续周期重新生成，而状态/诊断长期停留在 queued。
- **现有守卫为何失效**：事件上的 `astrmai_proactive_completed` 只在 finalize 被调用时生效；Dispatcher 没有 callback TTL、finally 结算或注入失败补偿。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 10-14：人工审核请求在解析和持久化前就出队，失败会丢失已经表达的人工决定

- **ID / 严重级别**：AM-LP-10-14 / **P2**
- **文件:行**：`astrmai/learning/review/reflect_tracker.py:97`；`astrmai/learning/review/reflect_tracker.py:103`；`astrmai/learning/review/reflect_tracker.py:105`；`astrmai/learning/review/reflect_tracker.py:107`；`astrmai/learning/review/reflect_tracker.py:149`；`astrmai/presentation/events/message_entry.py:184`
- **触发条件**：管理员回复审核问题后，LLM 解析（非固定关键词路径）失败，或 canonical `update_review()` / legacy DB 更新发生异常或返回未更新。
- **真实调用链**：正常消息入口 → `PluginFacade.try_consume_reflect_feedback()` → `ReflectTracker.try_consume_feedback()` → 从 `_pending` pop → 解析反馈 → 持久化审核决定。
- **实际行为**：请求在解析和写库之前原子 pop。解析返回空、DB 抛错或 `updated` 为假时，没有把请求和原始人工决定放回队列；消息入口捕获异常后继续走普通聊天。之后自动治理可能重新审核并生成新的问题，但本次人工决策已丢失。
- **期望行为**：应先解析并成功提交审核，再 ack/remove pending；失败时保留请求和反馈以便重试或明确向用户返回失败。
- **生产影响**：人工“通过/拒绝/修改”可能不生效且没有可靠确认，后续 AI 决策可覆盖人工意图，治理状态与管理员认知不一致。
- **现有守卫为何失效**：锁解决了双消费 TOCTOU，却把出队位置放在不可失败操作之前；没有 nack/requeue 或持久化 inbox。
- **分类**：confirmed（已确认）
- **置信度**：0.99

## Finding 10-15：画像与昵称模板的默认语义字符串已损坏并直接进入后台 LLM 输入

- **ID / 严重级别**：AM-LP-10-15 / **P3**
- **文件:行**：`astrmai/learning/profiling/profile_generator.py:19`；`astrmai/learning/profiling/profile_generator.py:21`；`astrmai/learning/profiling/profile_generator.py:23`；`astrmai/learning/profiling/profile_generator.py:25`；`astrmai/learning/profiling/nickname_generator.py:13`；`astrmai/learning/profiling/nickname_generator.py:15`；`astrmai/proactive/proactive_task.py:384`
- **触发条件**：小时画像任务处理缺少旧画像、标签、记忆点或最近互动摘要的 profile，或昵称任务处理缺少画像/标签的 profile；Prompt Registry 正常可用。
- **真实调用链**：`ProactiveTask._loop()` → `_run_profiling_task()` → `_generate_persona_analysis()` / `_generate_nickname()` → `build_template_payload()` → Prompt Registry render → background LLM lane。
- **实际行为**：模板 payload 的多个默认值是源文件中的 mojibake 字符串（如 `鏆傛棤...`），不是可读的“暂无画像/标签/记忆点/最近互动摘要”。这些值直接进入后台模型提示；该路径不会使用同文件中可读的 fallback prompt。
- **期望行为**：缺失字段应使用清晰、编码正确且语义稳定的默认文本，或用结构化空值交给模板处理。
- **生产影响**：首次画像和昵称生成收到损坏上下文，模型可能误解缺失状态、输出无关分析或降低画像质量；错误会被保存并参与后续称呼与人格侧输入。
- **现有守卫为何失效**：字段只做 `or default` 和字符串裁剪，没有编码/可读性校验；Prompt Registry 的存在使可读 fallback builder 不会执行。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## 已核验的生产路径

- `astrmai/learning/`：消息与 Bot 回复记录、EventBus 发布、mining trigger/locks、表达与黑话候选/增强/写入、画像与昵称生成、自动审核、人工审核、反思批次、治理 runner、ReviewService 与 contracts。
- `astrmai/proactive/`：主 scheduler 生命周期与任务集合、heartbeat bridges、wakeup、dispatcher completion、decay、diary、Dream、review dispatch、group signin、rhythm、Heartflow 状态/决策/反馈/topic digest。
- 为证明可达性核验的相邻生产路径：`main.py`、`config.py`、`_conf_schema.json`、`astrmai/app/bootstrap.py`、`astrmai/app/lifecycle.py`、`astrmai/app/plugin_facade.py`、`astrmai/app/runtime_context.py`、`astrmai/presentation/events/message_entry.py`、`astrmai/conversation/loop/chat_loop_kernel.py`、`astrmai/conversation/planning/planner.py`、`astrmai/conversation/execution/executor.py`、状态持久化与 Memory 对外契约。

以上 findings 均有当前生产工作树中的真实入口和可执行调用链；未把仅有不可达代码、假设性问题、风格/重复/死代码或重构建议列为缺陷。
