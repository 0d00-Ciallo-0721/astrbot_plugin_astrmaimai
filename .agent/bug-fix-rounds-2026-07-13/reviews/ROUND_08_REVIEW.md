# Round 08 审查报告：配置、状态与持久化一致性

**审查日期**：2026-07-14
**审查类型**：源码静态审查（只读，未修改生产代码）
**审查方法**：逐条对照 ROUND_08 修复边界，追踪调用链并验证关键路径
**结论**：全部 9 个修复 (R08-01 ~ R08-09) **均已实现**，关键路径覆盖完整。

---

## R08-01 / P2：Memory pipeline/maintenance 热配置继续使用旧对象

**状态**：✅ 已实现

**源码证据**：

| 文件 | 行号 | 关键逻辑 |
|------|------|---------|
| `memory_engine.py` | 103-138 | `refresh_config()` 刷新自身 config，传播至 `injection_service`、`retrieval_service`、`write_service`、`retriever`；优先调用 `pipeline.refresh_config()`，fallback 为单独刷新 `session_summarizer`/`instant_gate`；再刷新 `maintenance_service`/`tool_service`；最后重算 `embedding_models` |
| `memory_turn_pipeline.py` | 47-54 | `refresh_config()` 更新自身 config 并传播至 `session_summarizer`/`instant_gate`，fallback 为直接赋 `component.config = config` |
| `plugin_facade.py` | 202-258 | `_apply_hot_config_locked()` 包含 `memory_engine` 在 components 列表中；失败时回滚所有 component 的 `refresh_config(old_config)` |

**回归目标验证**：
- summary threshold/maintenance 参数在下一轮生效：`MemoryEngine.refresh_config` → `memory_turn_pipeline.refresh_config` → `session_summarizer`/`instant_gate` 均被刷新 ✅
- 失败回滚恢复所有 memory children：`_apply_hot_config_locked` 的 except 块遍历所有 components 并回滚 ✅

---

## R08-02 / P2：画像每消息即时 flush 使用不存在的方法形状

**状态**：✅ 已实现

**源码证据**：

| 文件 | 行号 | 关键逻辑 |
|------|------|---------|
| `user_profile_service.py` | 73-83 | `_flush_profile()` 单参数调用 `self.persistence.save_user_profile(profile)`，成功后 `profile.is_dirty = False` |
| `user_profile_service.py` | 93-97 | `_save_profile()` 先尝试单参数，`TypeError` 时回退到双参数 `(user_id, profile)` — 防御性兼容 |
| `user_profile_service.py` | 322 | `observe_user_activity()` 末尾调用 `await self._flush_profile(user_id, profile)` |
| `state_profile_persistence.py` | 233-263 | `save_user_profile(self, profile)` 接收单个 `UserProfile` 参数，全部字段通过 `INSERT OR REPLACE` 持久化 |

**回归目标验证**：
- 普通消息即时写入且无 warning：每次 `observe_user_activity` 末尾 flush，`is_dirty` 立即清除 ✅
- 崩溃/重载前数据已落盘：flush 是 await 的同步写入，无缓冲延迟 ✅

---

## R08-03 / P1：wakeup cooldown 用单参数调用双参数 `save_chat_state`

**状态**：✅ 已实现

**源码证据**：

| 文件 | 行号 | 关键逻辑 |
|------|------|---------|
| `wakeup_service.py` | 178-195 | `_on_complete()` 首选 `settle_proactive_wakeup(chat_id, amount=wakeup_cost, next_wakeup_timestamp=...)`；fallback 路径手动设置 `next_wakeup_timestamp`、`is_dirty = True`，然后 `save_chat_state(target_state.chat_id, target_state)` — 双参数调用 |
| `chat_state_service.py` | 230-243 | `settle_wakeup()` 在 lock + generation 保护下原子写入 energy、total_replies、last_reply_time、next_wakeup_timestamp，调用 `save_chat_state(chat_id, state)` |
| `chat_state_service.py` | 587-596 | `settle_proactive_wakeup()` 包裹层，对 FriendMessage 跳过 energy 扣减 |

**回归目标验证**：
- 成功 wakeup 后重启仍处于 cooldown：`next_wakeup_timestamp` 被写入 DB ✅
- 失败发送不扣能量/写 cooldown：`_on_complete` 仅在 `reply_sent=True` 时执行 ✅

---

## R08-04 / P2：EventBus stop 保留旧队列并跨 runtime generation 重放

**状态**：✅ 已实现

**源码证据**：

| 文件 | 行号 | 关键逻辑 |
|------|------|---------|
| `event_bus.py` | 230-244 | `stop()` 依次取消所有 worker → 清空 `_background_tasks`/`_worker_tasks` → `_workers_started = False` → `_generation += 1` → **替换 `_event_queue` 为新队列** → 清空 `subscribers` → 重置所有 Event |
| `event_bus.py` | 135-179 | `_worker_loop()` 从队列取出 `(generation, topic, data)`，首行检查 `generation != self._generation`，不匹配则 `task_done()` 并 `continue` — 旧 payload 被明确丢弃 |
| `event_bus.py` | 197-227 | `publish()` 入队时附带当前 `self._generation`，`_workers_started` 为 False 时懒启动新 worker 池 |
| `lifecycle.py` | 276-278 | `_terminate_impl()` 调用 `await event_bus.stop()` |
| `bootstrap.py` | 175 | 新 bootstrap 创建 `EventBus()` — 因单例模式返回同一实例，但 `stop()` 已重置全部内部状态 |

**回归目标验证**：
- 带 pending event 重载后旧事件不触发新 subscriber：旧 queue 被替换，旧 generation 匹配失败被丢弃 ✅
- 下一新事件正常处理：publish 懒启动新 worker，新 generation 匹配 ✅

---

## R08-05 / P3：LaneManager hot refresh 不替换 frozen settings snapshot

**状态**：✅ 已实现

**源码证据**：

| 文件 | 行号 | 关键逻辑 |
|------|------|---------|
| `lane_manager.py` | 77-80 | `refresh_config()` 调用 `build_infrastructure_settings(config).lane` 重建 settings，直接替换 `self.settings` 和 `self.config` |
| `plugin_facade.py` | 203 | `lane_manager` 在 hot-apply components 列表中 |
| `plugin_facade.py` | 246-248 | 回滚路径调用 `comp.refresh_config(old_config)` |

**分析**：`lane_manager.py` 未维护任何独立的 frozen snapshot — `settings` 是直接属性引用，`refresh_config` 替换整个对象。所有读取 `self.settings` 的代码均实时获取最新值。热更新后下一 transcript 使用新 settings ✅。

---

## R08-06 / P2：State hot reload 不刷新 ChatStateService、timeout 和 emotion mapping

**状态**：✅ 已实现

**源码证据**：

| 文件 | 行号 | 关键逻辑 |
|------|------|---------|
| `chat_state_service.py` | 275-286 | `StateEngine.refresh_config()` 更新 `self.config` → `self.chat_state_service.config` → 遍历 `mood_manager`/`energy_manager`/`relationship_engine`，调用 `refresh_config()` 或直接赋 `config` |
| `mood_manager.py` | 64-67 | `refresh_config()` 重建 `emotion_mapping` 并更新 `self.config` |
| `private_chat_manager.py` | 32-34 | `refresh_config()` 更新 `self.config` → 调用 `_init_timeout(config)` 重算 timeout |
| `plugin_facade.py` | 205, 208 | `state_engine` 和 `private_chat_manager` 均在 hot-apply components 中 |

**关键验证**：`StateEngine.refresh_config()` 不触碰 `chat_state_service.chat_states` 字典，不调用 `clear_chat_state()` — live session/state 完全保留 ✅。

---

## R08-07 / P2：group departure 删除仍有 waiter 引用的 per-chat lock

**状态**：✅ 已实现

**源码证据**：

| 文件 | 行号 | 关键逻辑 |
|------|------|---------|
| `chat_state_service.py` | 175-179 | `clear_chat_state()` 先 `async with self._get_chat_lock(chat_id)` — 等待当前 holder 完成 → 移除 state → 递增 `_chat_generations[chat_id]` (tombstone) |
| `chat_state_service.py` | 155-160 | `get_state()` 在 lock 内检查 `_is_current_generation`，不匹配返回默认 state |
| `chat_state_service.py` | 193-194, 213-214, 246-248 | `atomic_update_mood()`、`mark_energy_consumed()`、`should_drop_by_energy()` 均检查 generation，不匹配时跳过操作 |
| `plugin_facade.py` | 124-155 | `clear_group_runtime_state()` 调用 `state_engine.clear_chat_state(chat_id)`，且 `group_reply_wait_manager.cancel_wait()` 在之后执行 |

**关键验证**：lock 在清理前被持有，waiter drain 完成后才释放；generation tombstone 防止旧操作重建状态 ✅。

---

## R08-08 / P2：relationship maintenance 双重 decay 且只持久化第一次

**状态**：✅ 已实现

**源码证据**：

| 文件 | 行号 | 关键逻辑 |
|------|------|---------|
| `decay_service.py` | 15-77 | `run_once()` 是唯一的周期性 decay 入口：遍历 active_states 应用 `apply_natural_decay` (mood)；遍历 active_profiles 应用社交分数回归 (social score) → 调用 `update_social_score_from_fact` 持久化 |
| `relationship_engine.py` | 339-340 | `process_event()` 内调用 `self._apply_decay(vec, now)` — 仅当用户交互触发，非独立周期 decay |
| `relationship_engine.py` | 421-429 | `apply_global_decay()` 方法存在但未被 `DecayService` 或其他周期任务调用 — 因此不会双重 decay |
| `decay_service.py` | 57-61 | `update_social_score_from_fact(profile.user_id, delta, touch_activity=False)` — `touch_activity=False` 确保 `last_seen` 不被伪造 ✅ |

**关键验证**：
- 每周期只衰减一次：`DecayService.run_once()` 是唯一周期入口 ✅
- runtime/profile/DB 值相同：`update_social_score_from_fact` → `UserProfileService.update_social_score` → `_save_profile` 直接写 DB，同时更新 `RelationshipEngine` 内 `_vectors` ✅

---

## R08-09 / P2：NaN mood 被 clamp 为最大正值并持久化

**状态**：✅ 已实现

**源码证据**：

| 文件 | 行号 | 关键逻辑 |
|------|------|---------|
| `mood_manager.py` | 147-163 | `_normalize_result()` 三处 `isfinite` 检查：(1) `fallback_mood` 非有限时重置为 0.0；(2) LLM 返回的 `mood_value` parse 后检查 `isfinite`；(3) clamp 到 [-1.0, 1.0] |
| `chat_state_service.py` | 107-114 | `_clamp_mood()` 先 try `float(value)` → `isfinite` 检查 → 不通过则返回 fallback → clamp [-1.0, 1.0] |
| `chat_state_service.py` | 187-192 | `atomic_update_mood()` 首行检查：`absolute_val` 或 `delta` 非有限时，直接返回当前 mood，**不进入 CAS 写路径** |
| `chat_state_service.py` | 381-385 | `StateEngine.update_mood()` LLM 结果先 `isfinite` 检查，非有限则返回 snapshot_mood，**不进入 CAS 写路径** |
| `chat_state_service.py` | 201-203 | 写入路径使用 `_clamp_mood()`，无论 absolute 还是 delta 都经过安全包裹 |

**回归目标验证**：
- NaN/Inf/-Inf 都不改变 mood：所有路径在进入 CAS 写之前被拦截 ✅
- 有限边界值正常 clamp：`_clamp_mood` 的最终 clamp 到 [-1.0, 1.0] ✅

---

## 总体评估

| 维度 | 评级 |
|------|------|
| 修复完整性 | 9/9 均已实现 |
| 关键路径覆盖 | 热配置传播链、回滚路径、generation tombstone、NaN 拦截均完整 |
| 可观测性 | 关键路径均有 debug/info/warning 日志 |
| 已知限制 | R08-08 `apply_global_decay` 未被调用（方法存在但未接入周期任务），当前无双重 decay 风险，但如果未来有人接入则需注意 |

**建议**：无需额外修复。R08-08 的 `RelationshipEngine.apply_global_decay()` 方法可以添加 `# ponytail: unused, remove or wire to DecayService if periodic vector decay needed` 注释以防止误用。
