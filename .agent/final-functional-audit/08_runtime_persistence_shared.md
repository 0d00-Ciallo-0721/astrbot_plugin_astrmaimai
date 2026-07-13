# Assignment 08 - Runtime, Persistence, Compat, and Shared Functional Audit

审计日期：2026-07-13
审计对象：当前工作树（含 `astrmai/infrastructure/runtime/chat_runtime_coordinator.py` 的未提交修改及其当前生产调用方）
结论：确认 6 条可达生产功能问题：P0 0、P1 1、P2 4、P3 1。

## 审计边界

- 已按要求完整读取全局 `astrbot-plugin-dev/SKILL.md`、本审计目录的 `README.md`，并在续审时重新读取 `DISPATCH_PLAN.md` 第 08 节。
- 仅审计 `astrmai/infrastructure/runtime/`、`astrmai/infrastructure/persistence/`、`astrmai/infrastructure/compat/`、`astrmai/shared/`，并为证明可达性读取必要的生产调用方。
- 未读取或评估任何测试、测试覆盖率、安全策略、认证、授权、代码风格、重复代码、死代码或重构机会。
- `astrmai/infrastructure/security/` 被视为不透明模块，未检查。
- 未修改任何生产代码；本文件是唯一写入的审计产物。

## Findings

### [P1] 同一 lane 的并发成功调用会以整段历史覆盖，后完成者永久擦除先完成者的 exchange

- **位置**：`astrmai/infrastructure/runtime/lane_storage.py:199`（无锁 read-modify-write 起点）；覆盖写发生于 `astrmai/infrastructure/runtime/lane_storage.py:161`。
- **触发条件**：两个生产协程对同一个 `lane_umo` 并发完成模型调用。例如同一 chat 的两个 System 2 执行都使用 `LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id)`，并在任一协程保存前都从 AstrBot conversation manager 读到相同历史 `H`。
- **精确生产调用链**：
  1. `System2Executor._run_text_mode()` (`astrmai/conversation/execution/executor.py:534`) -> `GlobalModelGateway.chat_in_lane_result()` (`astrmai/conversation/execution/executor.py:567`)；
  2. `GlobalModelGateway.chat_in_lane_result()` (`astrmai/infrastructure/gateway/gateway_lane.py:224`) -> `LaneManager.ensure_lane()` (`gateway_lane.py:280`) 读取历史 `H` -> 执行 LLM；
  3. 成功后 `_finalize_success_artifacts()` (`gateway_lane.py:350`) -> `LaneManager.append_visible_reply_artifact()` (`gateway_lane.py:114`)；
  4. `append_visible_reply_artifact()` (`astrmai/infrastructure/runtime/lane_storage.py:229`) -> `append_exchange()` (`lane_storage.py:255`)；
  5. 每个协程分别在 `append_exchange()` (`lane_storage.py:199-215`) 再次取得/修改自己的历史副本，并由 `save_lane_history()` (`lane_storage.py:146`) 调用 `conversation_manager.update_conversation(history=normalized)` (`lane_storage.py:161-168`) 整体替换持久化历史。
- **实际行为**：A、B 都基于 `H` 追加；若 A 先写 `H+A`、B 后写 `H+B`，最终历史是 `H+B`，A 的 user/assistant exchange 被永久丢失。
- **预期行为**：同一 lane 的历史追加应在覆盖整个 conversation history 的 read-modify-write 边界内串行化，或在提交时基于最新历史合并，最终保留 `H+A+B`（顺序按提交规则确定）。
- **生产影响**：后续 judge/dialog/retrieval 模型看到不完整上下文，可能遗忘已经处理的用户消息、重复回答或基于错误上下文作出判断；丢失已进入 AstrBot conversation manager 的持久 lane 历史，进程重启后也无法恢复。
- **为什么现有 guard 无效**：`LaneManager._get_lane_lock()` 确实存在 (`lane_manager.py:90`)，但 `ensure_lane()` 仅在更新 `_runtime_meta` 时持有它 (`lane_storage.py:129-143`)；`append_exchange()` 的历史读取、列表追加和 `update_conversation()` 覆盖写不在该锁内。`_lane_creation_lock` 只保护 conversation 创建，`_meta_lock` 只保护内存元数据，均不保护历史事务。
- **分类**：并发事务 / lost update / lane 持久化契约破坏。
- **置信度**：高。

### [P2] 有界 thread-generation 表驱逐活跃线程后会复用 generation，旧回复可通过 freshness 并与新回复争抢同一 send claim

- **位置**：`astrmai/infrastructure/runtime/chat_runtime_coordinator.py:114`；generation 重置发生于 `chat_runtime_coordinator.py:118-120`。
- **触发条件**：chat 中线程 A 的 generation 1 回复仍在执行；随后 128 个此前未记录的不同 thread ID 调用 `advance_generation()`，使 A 成为被驱逐项；在旧回复结束前，线程 A 又收到新 turn。不同 reply message ID 会形成不同 thread ID (`astrmai/conversation/threading/group_thread_resolver.py:42-48`)，因此活跃大群或长时间运行的群聊可以达到该条件。
- **精确生产调用链**：
  1. 每条生产消息进入 `handle_global_message()`，由 `_bind_turn_identity()` (`astrmai/presentation/events/message_entry.py:162`) 调用 `PluginFacade.prepare_conversation_turn()` (`astrmai/app/plugin_facade.py:279`)；
  2. group thread 由 `resolve_group_thread()` (`plugin_facade.py:289`) 解析，随后 `ChatRuntimeCoordinator.advance_generation()` (`plugin_facade.py:299`)；
  3. generation 表达到 128 项时，`advance_generation()` 驱逐最早插入项 (`chat_runtime_coordinator.py:114-118`)；A 再次出现时从缺省 0 计算为 1 (`chat_runtime_coordinator.py:119`)；
  4. 旧 A turn 和新 A turn 都携带 generation 1。回复发送前 `ReplyFreshnessMixin._check_reply_freshness()` (`astrmai/conversation/execution/reply_freshness.py:54`) 调用 `is_current_turn()` (`reply_freshness.py:63`)，二者都与当前值 1 相等；
  5. `ReplyArtifactBuilder._send_segments()` 使用 `build_turn_send_key()` (`astrmai/conversation/execution/reply_artifact_builder.py:380`)，而 key 仅包含 mode/chat/thread/generation/kind (`astrmai/conversation/contracts/turn_identity.py:26-28`)，因此旧、新 turn 还会生成同一个 key 并在 `claim_send()` (`reply_artifact_builder.py:381`) 处竞争。
- **实际行为**：发生 ABA。旧的 stale turn 可被误判为 current 并先发送；随后真正的新 turn会因相同 send key 被当作 duplicate 阻止。若新 turn 先 claim，则旧 turn被阻止，但 freshness 仍未正确识别 stale。
- **预期行为**：只要旧 turn 仍可能在飞行中，线程 generation/turn token 就不能被复用；驱逐不能让同一 thread 回到先前可观察值。
- **生产影响**：繁忙群聊中可能发送已经过时、针对旧消息的回复，并抑制当前消息的正确回复；generation cancellation 与 exactly-once send 两道保护同时失效。
- **为什么现有 guard 无效**：驱逐策略只按 dict 插入顺序删除，不跟踪 in-flight turn，也不保存每线程单调 epoch；`is_current_turn()` 只做整数相等比较 (`chat_runtime_coordinator.py:144`)，send claim 又复用同一 generation 构造 key，二者没有第二个不可复用身份字段。
- **分类**：有界运行时状态 / ABA / generation cancellation 契约破坏。
- **置信度**：高。

### [P2] 用户活动的即时画像 flush 使用错误的持久化签名，每条普通消息都先写失败并暴露 5 秒数据丢失窗口

- **位置**：`astrmai/infrastructure/persistence/state_profile_persistence.py:233`（真实签名仅接收 `profile`）；错误调用位于 `astrmai/state/user_profile_service.py:50`。
- **触发条件**：任意非匿名用户消息进入生产消息入口；或 learning message event 调用同一 `observe_user_activity()` 路径。
- **精确生产调用链**：
  1. `handle_global_message()` 在 `astrmai/presentation/events/message_entry.py:176-178` 调用 `PluginFacade.track_incoming_user_activity()`；
  2. `track_incoming_user_activity()` (`astrmai/app/plugin_facade.py:376-378`) 调度 `update_user_stats()` (`plugin_facade.py:170`)；
  3. `StateEngine.increment_user_message_count()` (`astrmai/state/chat_state_service.py:445-446`) -> `UserProfileService.observe_user_activity()` (`astrmai/state/user_profile_service.py:253`)；
  4. `observe_user_activity()` 更新 name/last_seen/group footprint 并标记 dirty (`user_profile_service.py:266-284`) -> `_flush_profile()`；
  5. `_flush_profile()` 调用 `save_user_profile(user_id, profile.as_dict())` (`user_profile_service.py:50`)，但当前 `PersistenceManager` 继承的实现是 `save_user_profile(self, profile)` (`state_profile_persistence.py:233`)，立即抛出 `TypeError`；异常被 `user_profile_service.py:52-55` 捕获，仅记录 warning。
- **实际行为**：即时写入永远失败，profile 保持 dirty。正常运行时，生命周期每 5 秒调用 `flush_message_counters()` (`astrmai/app/lifecycle.py:160-169`)，其 `_save_profile(profile)` 使用正确的一参数调用，才可能补写；进程崩溃、强制终止或 flush task 异常发生在该窗口内时，本轮 last_seen、展示名和 group footprint 更新丢失。
- **预期行为**：`observe_user_activity()` 返回前应按实际 repository/service contract 成功 upsert，并清除 dirty；至少不应把确定性的参数错误当作暂态写入失败。
- **生产影响**：每条普通消息产生一次必然失败的数据库尝试；画像活动数据最多延迟 5 秒落盘，并在异常退出窗口内丢失。依赖最新 footprint/name/last_seen 的后续画像与提示词可能读取旧数据。
- **为什么现有 guard 无效**：`except Exception` 只吞掉错误并保留 dirty，没有签名兼容分支。周期 flush 是延迟补偿而非即时事务保证，且只在生命周期任务继续运行时有效；它不能覆盖崩溃窗口。
- **分类**：repository/service 参数契约不一致 / 延迟持久化 / crash-window data loss。
- **置信度**：高。

### [P2] 主动唤醒成功后的 cooldown 状态用单参数调用双参数 `save_chat_state`，该次结算不会落盘

- **位置**：`astrmai/infrastructure/persistence/state_profile_persistence.py:105`（真实签名为 `save_chat_state(chat_id, state)`）；错误调用位于 `astrmai/proactive/wakeup_service.py:188`。
- **触发条件**：wakeup signal 通过节奏/能量/静默期检查，主动消息经主回复链成功发送，completion callback 收到 `reply_sent=True`。
- **精确生产调用链**：
  1. `ProactiveTask.handle_wakeup_signal()` (`astrmai/proactive/proactive_task.py:542-547`) -> `WakeupService.run_for_chat()` (`astrmai/proactive/wakeup_service.py:157`) -> `ProactiveDispatcher.dispatch(..., on_complete=_on_complete)` (`wakeup_service.py:192`)；
  2. dispatcher 把 `_completion` 放入事件 extra (`astrmai/proactive/dispatcher.py:277-309`)；
  3. planner 完成主动回复后调用该 callback (`astrmai/conversation/planning/planner.py:882-887`) -> `ProactiveDispatcher.complete()` (`dispatcher.py:281`) -> `_on_complete()`；
  4. `_on_complete()` 先调用 `StateEngine.consume_energy()` (`wakeup_service.py:183`)，它通过 `mark_energy_consumed()` 用正确的 `(chat_id, state)` 签名保存当前 state (`astrmai/state/chat_state_service.py:173-184`)；
  5. 随后 callback 才设置 `target_state.next_wakeup_timestamp` (`wakeup_service.py:186`)，却以 `save_chat_state(target_state)` (`wakeup_service.py:188`) 调用必须接收两个参数的实现 (`state_profile_persistence.py:105`)，抛出 `TypeError`；planner 在 `planner.py:888-889` 将异常降级吞掉。
- **实际行为**：内存 state 得到新 cooldown，dispatcher 内存 cooldown 也已设置，但该次结算留在 SQLite 中的是步骤 4 的旧 `next_wakeup_timestamp`。只有后续不相关的 chat-state 保存碰巧覆盖同一行时才可能补写；在此之前重载/重启会使 cooldown 回退，completion 日志也不会执行到 `wakeup_service.py:189`。
- **预期行为**：成功发送后应原子或至少可靠地持久化扣除能量、reply metadata 和新的 `next_wakeup_timestamp`，重启后仍禁止冷却期内再次唤醒。
- **生产影响**：在主动发言后重载或重启插件，会丢失持久 cooldown，可能在原冷却期内再次主动发言；数据库和运行时状态在每次成功 wakeup 后确定性分叉。
- **为什么现有 guard 无效**：`consume_energy()` 的兼容 `TypeError` 分支只包围 energy 调用 (`wakeup_service.py:182-185`)，不覆盖 `save_chat_state`；planner 的 completion guard 只吞异常，不重试、不修正签名，也不回滚已发送消息。
- **分类**：service contract mismatch / 非原子状态结算 / cooldown persistence failure。
- **置信度**：高。

### [P2] EventBus 单例 stop 后保留队列，插件重载会把旧 runtime 的事件投递给新 runtime

- **位置**：`astrmai/infrastructure/runtime/event_bus.py:225`；单例创建与一次性初始化位于 `event_bus.py:12-18`。
- **触发条件**：插件卸载/热重载时 `_event_queue` 仍有至少一个未消费事件；随后同一 Python 进程中重新构建 AstrMai，并发生下一次 event publish。
- **精确生产调用链**：
  1. 生产事件由 `EvolutionManager.record_user_message()` (`astrmai/learning/evolution_manager.py:247-262`) 调用 `EventBus.publish_learning_message_recorded()` (`astrmai/infrastructure/runtime/event_bus.py:83-84`)，`publish()` 在 `event_bus.py:193-216` 将 payload 放入单例队列；
  2. 卸载时 `PluginLifecycleManager._terminate_impl()` 调用 `event_bus.stop()` (`astrmai/app/lifecycle.py:266-272`)；`stop()` 只取消任务、清空 task sets、复位 `_workers_started` (`event_bus.py:226-233`)，没有 drain/replace `_event_queue`，也没有重置单例；
  3. 新 bootstrap 在 `astrmai/app/bootstrap.py:175` 再次调用 `EventBus()`，`__new__()` 返回旧 `_instance`，不会执行 `_init_bus()`；
  4. `_bind_learning_collaboration()` 为新 runtime 订阅 state/memory callbacks (`bootstrap.py:496-510`)；
  5. 下一次 publish 将 `_workers_started` 设回 true 并启动 workers (`event_bus.py:204-212`)；workers 从同一个保留队列读取旧 payload (`event_bus.py:139-171`) 并向当前 subscribers 派发。
- **实际行为**：上一个 runtime 尚未处理的 learning/memory 事件跨越重载边界，在新 runtime 中延迟执行；它们可能与新消息的事件交错，造成重复或乱序的画像、学习、memory turn 副作用。
- **预期行为**：`stop()` 后的 EventBus 要么彻底丢弃并 `task_done` 旧队列内容，要么新 runtime 获得全新 bus/queue；旧 runtime payload 不应自动进入新 runtime。
- **生产影响**：热重载后的学习与记忆状态可能包含上一实例的过期事件，造成重放、顺序颠倒或重复计数；问题只在重载边界出现，普通日志无法区分事件来自哪一 runtime generation。
- **为什么现有 guard 无效**：worker cancellation 只停止消费者；`_event_queue`、`subscribers`、signals 和 `_dropped_count` 都保存在单例实例中。WeakMethod 只避免旧绑定方法被强引用，不会隔离已入队 payload，也不会阻止新订阅者消费旧 payload。
- **分类**：singleton lifecycle leakage / stale event replay / reload isolation failure。
- **置信度**：高。

### [P3] hot config 重建了 runtime lane settings，但 `LaneManager.refresh_config` 不替换自身不可变 settings 快照

- **位置**：`astrmai/infrastructure/runtime/lane_manager.py:77`。
- **触发条件**：通过 WebUI/runtime hot apply 修改 `system1.nicknames` 或 `global_settings.debug_mode`，且该变更被判定可热应用而无需立即重启。
- **精确生产调用链**：
  1. `PluginFacade.apply_hot_config()` -> `_apply_hot_config_locked()` (`astrmai/app/plugin_facade.py:185-190`)；
  2. `_apply_runtime()` 更新 `runtime.config` 并调用 `runtime.rebuild_infrastructure_settings()` (`plugin_facade.py:209-213`)；该方法生成新的 frozen `InfrastructureSettings` (`astrmai/app/runtime_context.py:152-153`)；
  3. `_refresh_components()` 调用 `LaneManager.refresh_config(parsed_config)` (`plugin_facade.py:215-218`)；
  4. `LaneManager.refresh_config()` 只赋值 `self.config` (`lane_manager.py:77-78`)，没有像 gateway 的 refresh 那样重建 `self.settings`。原 settings 是构造时从 `runtime.infrastructure_settings.lane` 捕获的 frozen 快照 (`lane_manager.py:57-66`)；
  5. 后续 lane history sanitation 仍读取旧 `self.settings.nicknames` (`astrmai/infrastructure/runtime/lane_history.py:81-83`)，transcript 渲染和 debug 仍读取旧值 (`astrmai/infrastructure/runtime/lane_transcript.py:41-43,78`)。
- **实际行为**：runtime 顶层配置显示新昵称/debug 值，但 LaneManager 在进程重启前继续使用旧昵称和旧 debug 标志。
- **预期行为**：成功 hot apply 后，所有列入 refresh component 列表的组件应在下一次调用时使用同一版解析配置；LaneManager 的 settings 应与 `runtime.infrastructure_settings.lane` 同步。
- **生产影响**：改名后 lane transcript 仍以旧 bot 名渲染，assistant history sanitation 仍按旧 speaker names 处理，可能保留新前缀或错误剥离旧名称；debug trace 开关也与界面显示不一致。重启后自行恢复，因此定为 P3。
- **为什么现有 guard 无效**：`sync_host_compat_attrs()` 只导出 runtime 属性，不修改 LaneManager 内部快照；配置回滚机制同样只再次调用这个不完整的 `refresh_config()`，没有其他读取 `runtime.infrastructure_settings.lane` 的路径。
- **分类**：hot-config propagation / immutable settings snapshot divergence。
- **置信度**：高。

## 已审查的目标生产路径

```text
astrmai/infrastructure/runtime/__init__.py
astrmai/infrastructure/runtime/chat_runtime_coordinator.py
astrmai/infrastructure/runtime/context_economy_benchmark.py
astrmai/infrastructure/runtime/context_economy_benchmark_store.py
astrmai/infrastructure/runtime/event_bus.py
astrmai/infrastructure/runtime/host_bridge.py
astrmai/infrastructure/runtime/lane_history.py
astrmai/infrastructure/runtime/lane_manager.py
astrmai/infrastructure/runtime/lane_storage.py
astrmai/infrastructure/runtime/lane_transcript.py
astrmai/infrastructure/runtime/observability.py
astrmai/infrastructure/runtime/raw_trace_store.py
astrmai/infrastructure/runtime/reverse_session.py
astrmai/infrastructure/runtime/runtime_contracts.py
astrmai/infrastructure/runtime/trace_runtime.py
astrmai/infrastructure/runtime/turn_trace_store.py

astrmai/infrastructure/persistence/__init__.py
astrmai/infrastructure/persistence/database_cron.py
astrmai/infrastructure/persistence/database_jargon.py
astrmai/infrastructure/persistence/database_memory.py
astrmai/infrastructure/persistence/database_profile_relation.py
astrmai/infrastructure/persistence/database_review.py
astrmai/infrastructure/persistence/database_service.py
astrmai/infrastructure/persistence/orm_models.py
astrmai/infrastructure/persistence/persistence_manager.py
astrmai/infrastructure/persistence/persistence_schema.py
astrmai/infrastructure/persistence/persona_cache.py
astrmai/infrastructure/persistence/repositories/__init__.py
astrmai/infrastructure/persistence/repositories/chat_repository.py
astrmai/infrastructure/persistence/repositories/memory_repository.py
astrmai/infrastructure/persistence/repositories/profile_repository.py
astrmai/infrastructure/persistence/repositories/review_repository.py
astrmai/infrastructure/persistence/sqlite_helpers.py
astrmai/infrastructure/persistence/state_profile_persistence.py

astrmai/infrastructure/compat/__init__.py
astrmai/infrastructure/compat/legacy_compat.py

astrmai/shared/__init__.py
astrmai/shared/constants/defaults.py
astrmai/shared/contracts/service_protocols.py
astrmai/shared/exceptions.py
astrmai/shared/helpers/__init__.py
astrmai/shared/helpers/event_utils.py
astrmai/shared/helpers/plugin_helpers.py
astrmai/shared/helpers/text_utils.py
astrmai/shared/helpers/time_utils.py
```

为确认上述问题的生产可达性，额外核对了相关生产调用链：`astrmai/app/bootstrap.py`、`app/lifecycle.py`、`app/plugin_facade.py`、`app/runtime_context.py`、`presentation/events/message_entry.py`、`conversation/threading/group_thread_resolver.py`、`conversation/contracts/turn_identity.py`、`conversation/execution/executor.py`、`conversation/execution/reply_freshness.py`、`conversation/execution/reply_artifact_builder.py`、`conversation/decision/judge.py`、`conversation/attention/gate.py`、`conversation/attention/decision_router.py`、`conversation/planning/planner.py`、`infrastructure/gateway/gateway_lane.py`、`state/chat_state_service.py`、`state/user_profile_service.py`、`learning/evolution_manager.py`、`proactive/dispatcher.py`、`proactive/proactive_task.py`、`proactive/wakeup_service.py`。

## 非 findings 说明

- SQLite schema 初始化、SQLModel session 创建/释放、WAL/busy timeout、repository 转发、trace JSON 文件原子替换、reverse-session/typed runtime contracts、compat extra 同步等其余已审查路径，未确认到同时满足“精确可达生产链 + 明确实际/预期偏差”的额外功能问题。
- 未以测试缺失、覆盖率、风格、重复、死代码、重构建议或任何安全/权限判断生成 finding。
