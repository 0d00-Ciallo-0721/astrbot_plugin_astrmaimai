# 审查报告：astrmai/state/（含 proactive/decay_service.py）
> task_id: r12-state | 审查时间: 2025-01-17T09:00:00Z

## 概述
- 审查文件数: 16（state 模块 13 个 + decay_service.py + orm_models.py 相关部分）
- 发现总数: 16
- 严重: 3 | 中等: 8 | 建议: 5

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `astrmai/state/mood/mood_decay.py:11` | **`config.energy` 无空安全访问。** 函数直接访问 `config.energy.recovery_silence_min`，但 `config.energy` 没有 `getattr` 保护。如果 config 没有 `energy` 属性，抛出 `AttributeError`。`getattr` 仅保护二级属性。同样问题出现在行 19：`config.mood.decay_interval`。 |
| 2 | `astrmai/state/chat_state_service.py:170-191` | **`update_mood` CAS 比较点不一致造成逻辑错误。** Phase 1 读取 state 后调用 `apply_natural_decay(state, …)`，`snapshot_mood = state.mood` 取的是**衰减后**值。Phase 3 重新读取 `current_state`，`current_mood = current_state.mood` 取的是**衰减前**值，然后才调用 `apply_natural_decay`。所以 `abs(current_mood - snapshot_mood)` 比较的是"衰减前 vs 衰减后"。若衰减改变了 mood，CAS 会误判为"被其他写入者修改"，即使没有并发写入。此 CAS 条件无法达到设计意图。 |
| 3 | `astrmai/state/chat_state_service.py:208-213` | **`_check_daily_reset` 中 `self.config.energy.daily_recovery` 无空安全。** 与 mood_decay.py 相同模式：直接访问 `config.energy` 属性。如果调用链中 config 对象缺少 `energy` 或 `energy.daily_recovery`，将抛出 AttributeError，导致整个 get_state 链路崩溃。 |

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | `astrmai/state/chat_state_service.py:173-174` | **Phase 1 中的 `apply_natural_decay` 修改共享 state 但不持久化。** `get_state(chat_id)` 返回的是 `chat_states` 字典中的同一对象。`apply_natural_decay` 可能设置 `state.is_dirty = True` 并修改 `state.energy`，但这些改动不会被保存到数据库（Phase 1 不调用 `save_chat_state`）。如果 Phase 2 LLM 调用耗时较长，另一个路径（如 `atomic_update_mood`）可能在锁外读到这个"已脏但未持久化"的 state。 |
| 5 | `astrmai/proactive/decay_service.py:26-42` | **`DecayService.run_once` 绕过 `UserProfileService` 直接修改 profile。** 直接赋值 `profile.social_score` 和 `profile.is_dirty`，不经过 `update_social_score()`。调用 `align_social_score` 只更新引擎的 `RelationshipVector`，但 profile 的 `relationship_vector` 字段未被同步。后续 `flush_message_counters` 持久化时，关系向量可能是旧数据。 |
| 6 | `astrmai/proactive/decay_service.py:30` | **`run_once` 中 profile 的 `last_access_time` 被无条件改写。** `profile.last_access_time = now`（行 42）在 decay 循环末尾执行，包括那些 `now - last_access_time <= 86400` 的 profile（跳过分数衰减的）。这会将所有活跃 profile 的 `last_access_time` 刷新到当前时间，使得下次 `run_once` 的 86400 筛选永远不命中——只有完全冷门的 profile 才会被衰减。耦合了"衰减"和"时间戳刷新"两个逻辑。 |
| 7 | `astrmai/state/relationship/relationship_engine.py:181-199` | **`align_social_score` 收敛算法存在边界不收敛风险。** 最多 4 次迭代，当多维度接近边界（±100）时，`adjustable` 维度减少使 `adj_weight` 变小，`shared_delta` 可能过大导致超调。`abs(new_rem) >= abs(remaining)` 的保护退出可能留在距离目标很远的位置。调用链上的 `update_social_score_from_fact` 期望 `aligned_score` 接近 `new_score`，但实际偏差可能达数个点。 |
| 8 | `astrmai/state/chat_state_service.py:214-219` | **`consume_energy` 中 `"FriendMessage" in chat_id` 的字符串检测不可靠。** 设计意图是私有聊天不消耗能量，但依赖 chat_id 字符串包含 `"FriendMessage"` 的约定。如果 chat_id 格式发生变化（如包含 `"FriendMessage"` 但不代表私聊，或私聊用其他标识），逻辑失效。应使用明确的标识字段而非字符串子串匹配。 |
| 9 | `astrmai/state/energy/energy_manager.py:21-30` | **`should_drop_by_energy` 中 `state.energy += recover_amount` 直接在参数对象上修改。** 函数名"should_drop"暗示纯判断，但副作用是修改 `state.energy` 和 `state.is_dirty`。调用方（`StateEngine` 外部）未必预期此修改。应分离判断逻辑和状态变更（如返回 `(should_drop, recovery_amount)`）。 |
| 10 | `astrmai/state/user_profile_service.py:44-47` | **`_save_profile` 的 `except TypeError` 回退存在隐式假设。** 第一调用尝试 `save_user_profile(profile)`，如果失败（TypeError），回退为 `save_user_profile(profile.user_id, profile)`。回退的逻辑假设第一个调用失败原因是"缺少 user_id 作为第一个参数"。如果 TypeError 由其他原因（如序列化错误）引发，回退后可能仍然失败或以错误方式写入。 |
| 11 | `astrmai/state/chat_state_service.py:54-57` | **`_persist_if_dirty` 中 `state.is_dirty` 重置与持久化脱节。** 调用 `persistence.save_chat_state` 后立即设 `is_dirty = False`，不确认保存是否成功。如果保存因网络/I/O 异常静默失败（`save_chat_state` 未抛异常但实际未写入），脏标记被错误清除，数据丢失。 |

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 12 | `astrmai/state/mood/mood_decay.py:21-28` | **衰减是离散跳跃而非连续。** `int(elapsed / decay_interval)` 取整意味着 3599 秒不触发任何衰减，3600 秒触发完整一步。长时间不互动时，用户可能感受到突然的 mood 跳变。考虑支持部分步长（如 `elapsed / interval * rate`）使衰减更平滑。 |
| 13 | `astrmai/state/mood/mood_decay.py:17` | **`or now` 脆弱模式。** `getattr(state, "last_passive_decay_time", 0) or now` 将 0.0 视为"未初始化"并用 `now` 替代。但如果某路径意外地将 `last_passive_decay_time` 设为 0.0（如从数据库加载未设此字段的行），会导致初始化逻辑重复触发。建议使用 `None` 哨兵或单独 `is_initialized` 标记。 |
| 14 | `astrmai/state/chat_state_service.py:155` | **`update_mood` 中 `self.get_state(chat_id)` 获取锁后，Phase 3 重用 `_get_chat_lock` 和 `_get_state_inner` 的锁处理模式不一致。** Phase 1 通过 `get_state`（内部锁）获取，Phase 3 直接操作 `_get_chat_lock` 和 `_get_state_inner`。虽然功能正确，但内部 API 的边界不清晰。建议封装一个 `_update_mood_under_lock` 方法清晰表达"锁内执行"的契约。 |
| 15 | `astrmai/state/chat_state_service.py:67-68` | **`chat_states` 和 `_chat_locks` 字典无大小限制。** 长期运行的服务中，每个 chat_id 都会在内存中积累 state 和 lock 对象。缺少 LRU 淘汰或 TTL 清理机制。建议在 `_get_state_inner` 中集成容量控制。 |
| 16 | `astrmai/state/relationship/relationship_engine.py:113-120` | **关系等级描述硬编码中文。** `_get_relationship_level` 返回的 emoji+中文标签（如"💖 至亲挚友"）硬编码在代码中。如果项目需要多语言，这些字符串需要抽取。当前设计明确面向中文用户，建议至少通过常量或配置管理作为未来多语言的准备。 |

## 亮点

1. **`update_mood` 的三阶段 CAS 设计思路值得肯定**——先无锁做 LLM 推理（高延迟），再在锁内做 CAS 写入（低延迟），最大化并发吞吐。虽然当前实现的 CAS 比较点有逻辑偏差，但架构方向正确。
2. **`RelationshipEngine` 四维向量模型设计成熟**——对数饱和、共振放大、信任惩罚放大的算法精细，做到了零 LLM 消耗的情感建模。文档注释清晰完整。
3. **`FrequencyController` 冷场激励 + 密集发言惩罚的双向调节**——算法丰富，模拟人类自然节奏，且有默认参数和配置覆盖的灵活设计。
4. **`UserProfileService` 的 `_is_placeholder_name` 和手动锁定机制**——防止昵称被低质量数据覆盖，有较好的防御性编程意识。
5. **`ChatStateService` 的 per-chat async lock 粒度**——锁粒度细到 chat 级别而非全局，并发性能好。

## 总结

`astrmai/state/` 模块整体设计质量较高，架构分层清晰（`StateEngine` → `ChatStateService` / `UserProfileService` → 子管理器），并发控制细致。最大的风险点在两个地方：**(1) `apply_natural_decay` 和 `_check_daily_reset` 对 `config.energy` / `config.mood` 的无保护访问**会在配置不完整时导致崩溃，属于严重稳定性隐患；**(2) `update_mood` 的 CAS 比较点在设计上存在逻辑瑕疵**（衰减前 vs 衰减后比较），虽然在实际运行中因 chat 级别锁的存在不太可能触发误判，但代码语义与注释不一致，长期维护风险高。

中等程度的问题集中在 **`DecayService.run_once` 绕过服务层直接修改 profile**、**`align_social_score` 收敛精度边界** 和 **`should_drop_by_energy` 的副作用隐藏** 上。建议优先修复严重问题（添加 `getattr` 保护、修正 CAS 比较逻辑），然后逐步收敛中等问题的边界情况。
