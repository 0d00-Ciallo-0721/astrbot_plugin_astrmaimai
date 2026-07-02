# Requirements Document

## Introduction

本 Spec 为「AstrMai」插件中识别出的 **8 个状态机竞态风险** 制定修复需求文档。这些缺陷涉及 ChatState 脏标记不一致、RelationshipVector 双写不同步、EnergyManager 副作用未文档化、Mood CAS 精度窗口、FrequencyController 无锁读取、会话状态零持久化、UserProfile 锁间隙覆盖、迭代不持锁。不含新功能开发。

当前阶段产出物为 `specs/astrmai-state-race-hardening/` 下的 `requirements.md` / `design.md` / `tasks.md`。**本轮仅产出 requirements.md，不进入 design 或 tasks 阶段**。

明确不在本 Spec 范围：
- 第一轮 7 项硬伤修复（已完成）
- 决策点优化（D1–D11）
- 多 Agent 安全加固
- 资源配置优化
- WebUI 安全加固
- 新功能开发
- 代码质量基线治理
- 依赖升级

---

## Glossary

- **ChatState**：`astrmai/state/chat_state_service.py` 中的聊天级状态，包含 energy（0-1）、mood（-1~1）、is_dirty、total_replies 等字段。
- **UserProfile**：`astrmai/state/user_profile_service.py` 中的用户级状态，包含 social_score、tags、nickname、relationship_vector、profile_metadata 等字段。
- **EnergyManager**：`astrmai/state/energy/energy_manager.py` 中的精力管理器，`should_drop_by_energy()` 自带恢复 side-effect。
- **FrequencyController**：`astrmai/state/energy/frequency_controller.py` 中的频控器，在决策前读取 energy/mood 但不持有 ChatState 锁。
- **MoodManager + mood_decay**：分别负责 LLM 驱动的情绪分析和自然衰减函数。
- **GroupReplyWaitManager**：`astrmai/state/group_wait/` 中的群组回复等待管理器，纯内存状态，30s TTL。
- **PrivateChatManager**：`astrmai/state/private_chat/` 中的私聊会话管理器，纯内存状态，30min 静默清理。
- **RelationshipVector**：4 维关系向量（trust/familiarity/emotion_bond/respect），存储在 `UserProfile.relationship_vector` 和 `profile_metadata["relationship_vector"]` 两个位置。
- **CAS (Compare-And-Swap)**：`StateEngine.update_mood()` 使用的乐观并发控制模式。
- **TOCTOU (Time-Of-Check-Time-Of-Use)**：检查条件到使用数据之间的时间窗口竞态。
- **EARS**：Easy Approach to Requirements Syntax。
- **P1/P2**：P1 为高优先级数据一致性风险、P2 为中优先级工程健壮性。

---

## Requirements

### Wave 1：P1 数据持久化一致性（3 项）

---

### Requirement 1: ChatState dirty-flag 不一致 — 统一持久化策略

**User Story:** 作为运维人员，当 `mark_energy_consumed()` 和 `atomic_update_mood()` 在修改 ChatState 后直接调用 `save_chat_state()`（immediate-save），而 `should_drop_by_energy()` 和 `get_state()` 中的 daily reset 仅设置 `is_dirty=True`（lazy-save），我不希望同一个 ChatState 的更新因调用路径不同而部分丢失，所以所有写路径的持久化行为一致、可预测。

#### Acceptance Criteria

1. THE `ChatStateService` SHALL 对所有 ChatState 写入路径使用统一的持久化策略（全部 immediate-save 或全部 dirty-flag + 统一 flush）。
2. WHEN `mark_energy_consumed()`（L141-152）写入 `state.energy` 后，THE 函数 SHALL NOT 在锁内直接调用 `save_chat_state()` 后手动清零 `is_dirty`——这与 dirty-flag 模式冲突，会导致 `_get_state_inner()` 中的延迟 flush 逻辑失准。
3. WHEN `should_drop_by_energy()`（L154-161）依赖 `is_dirty` 判断是否持久化，THE 函数 SHALL 使用与 `mark_energy_consumed()` 一致的持久化策略，而非依赖调用方对 `is_dirty` 状态的假设。
4. THE `_get_state_inner()`（L107）中的 daily reset（L119 `self._mark_dirty(state)`）SHALL 在设置 `is_dirty=True` 后立即调用 `save_chat_state()`（与 `mark_energy_consumed` 一致），而非依赖后续的 `_persist_if_dirty()`。
5. THE `_persist_if_dirty()`（L80）SHALL 被保留作为兜底（defensive flush），但不再作为主要持久化路径。

#### Notes / Constraints

- 涉及文件：`astrmai/state/chat_state_service.py`
  - `_mark_dirty()` L76 — 仅设置 `is_dirty=True`
  - `_persist_if_dirty()` L80 — 条件持久化
  - `_get_state_inner()` L107-119 — daily reset 仅 mark_dirty，不 persist
  - `atomic_update_mood()` L124-139 — immediate save + 手动清零 is_dirty（L151）
  - `mark_energy_consumed()` L141-152 — immediate save + 手动清零 is_dirty（L151）
  - `should_drop_by_energy()` L154-161 — 依赖 `is_dirty` 判断
- 根因：代码中存在两种持久化模式混用——immediate-save（L138/L151）直接写 DB 并手动清零 is_dirty，dirty-flag（L119/L148）仅标记不写。如果 `mark_energy_consumed()` 写入后 `is_dirty` 被手动清零，但 `_get_state_inner()` 先设置了 `is_dirty=True`（daily reset），后者可能被覆盖。
- 修复方式：统一为 immediate-save 模式——所有写路径在锁内修改 state 后直接 `save_chat_state()`，废弃手动 `is_dirty = False` 操作，`is_dirty` 仅用于 `_persist_if_dirty()` 防御性兜底。
- 验证：并发构造 daily reset + energy 消耗 → 确认 `save_chat_state()` 被调用且值正确。

---

### Requirement 2: RelationshipVector 双写不同步 — 统一写入路径

**User Story:** 作为依赖好感度系统的用户，当 `update_social_score()` 同时写入 `profile.relationship_vector` 和 `profile_metadata["relationship_vector"]` 两个位置时，我不希望并发或异常路径导致两处值不一致，所以关系向量始终有单一权威来源。

#### Acceptance Criteria

1. THE `UserProfile` SHALL 将 `relationship_vector` 作为唯一权威存储位置（single source of truth），`profile_metadata["relationship_vector"]` SHALL 仅作为只读镜像，不在写入路径中同步更新。
2. WHEN `update_social_score()`（L143-169）写入 `relationship_vector`，THE 函数 SHALL 仅写 `profile.relationship_vector` 字段，不再同步写 `meta["relationship_vector"]`（L167）。
3. THE `get_user_profile()` SHALL 在从 DB 加载 profile 后，自动将 `profile_metadata["relationship_vector"]`（如存在）同步到 `profile.relationship_vector`（向后兼容已有数据），然后从 `profile_metadata` 中移除该 key。
4. THE `_load_profile_with_relationship()`（L155-167 区域）SHALL 被重构——先获取锁 → 加载 profile → 填充 relationship_vector → 释放锁，消除锁间隙。

#### Notes / Constraints

- 涉及文件：`astrmai/state/user_profile_service.py`
  - `update_social_score()` L143-169 — 双写 `profile.relationship_vector`（L165）和 `meta["relationship_vector"]`（L167）
  - `_load_profile_with_relationship()` — 在 `get_user_profile()` 之后、锁获取之前填充 relationship_vector
- 根因：L164-167 同时写两个字段，但后续的 `_save_profile()` 只序列化 `profile_metadata` JSON 字段，`relationship_vector` 作为 dataclass 字段也保存在另一列。两个路径可能被不同协程分别读取 → 读到过期值。
- 修复方式：`relationship_vector` 作为唯一写入目标；`profile_metadata["relationship_vector"]` 在 `get_user_profile()` 加载时单向同步（read-once migration）；写入路径只写 `profile.relationship_vector`。
- 验证：`update_social_score()` → 确认 `profile_metadata["relationship_vector"]` 未被写入 → 重启加载 → 确认 `relationship_vector` 被正确恢复。

---

### Requirement 3: EnergyManager `should_drop_by_energy()` 恢复 energy 副作用文档化

**User Story:** 作为代码维护者，当 `EnergyManager.should_drop_by_energy()` 在判定 drop 的同时静默恢复 energy（`msg_count * cost_per_reply`），我不希望这个副作用仅存在于代码注释中而不被调用方显式感知，所以所有调用方都明确知道调用后的 state 变化。

#### Acceptance Criteria

1. THE `EnergyManager.should_drop_by_energy()`（L20-42）SHALL 在 docstring 中明确标注恢复量公式 `recover_amount = msg_count * cost_per_reply` 和恢复上限（min(1.0, current + recover)）。
2. THE `ChatStateService.should_drop_by_energy()`（L154-161）SHALL 在调用 `energy_manager.should_drop_by_energy()` 后，无论返回 `True` 还是 `False`，如果 `state.is_dirty` 为 `True` 都立即 `save_chat_state()`（统一 R1 的 immediate-save 策略），而非仅在 `should_drop=True` 时保存。
3. THE `StateEngine` 中所有调用 `consume_energy` / `should_drop_by_energy` 的公共方法 SHALL 在 docstring 中注明"此调用可能修改 `state.energy`（恢复 side-effect）"。
4. THE `self._energy_config()` 返回的配置 SHALL 被缓存（当前每次调用重新获取），以避免在 drop 判定和恢复计算之间配置被热更新导致不一致。

#### Notes / Constraints

- 涉及文件：`astrmai/state/energy/energy_manager.py`
  - `should_drop_by_energy()` L20-42 — L39-41 恢复 `state.energy += recover_amount` 并设置 `state.is_dirty = True`
  - docstring L23-28 已部分说明但未量化
- 根因：L25-28 docstring 说明"may increase after this call"但调用方 `ChatStateService.should_drop_by_energy()` L154-161 仅在 `should_drop=True` 时 persist，如果 `should_drop=False` 但 `is_dirty=True`（由 daily reset 设置），脏数据不会被及时保存。结合 R1 的 immediate-save 统一后此问题解决。
- 修复方式：增强 docstring + 与 R1 统一持久化策略联动 + 缓存 `_energy_config()`。
- 验证：调用 `should_drop_by_energy` 后检查 `state.is_dirty` → 确认脏标记被正确处理。

---

### Wave 2：P1 并发写入竞态（3 项）

---

### Requirement 4: Mood CAS `abs(diff) < 0.001` 精度窗口 — 放宽阈值或改用版本号

**User Story:** 作为依赖情绪系统准确性的用户，当 `StateEngine.update_mood()` 使用 CAS 模式比较 snapshot_mood 和 current_mood 时，我不希望 `abs(diff) < 0.001` 的精度判断漏掉介于 0.0005~0.00099 之间的合法并发变化，所以情绪更新的并发检测是可靠的。

#### Acceptance Criteria

1. THE `StateEngine.update_mood()` SHALL 将 CAS 比较阈值从 `abs(diff) < 0.001` 改为 `abs(diff) < 0.0001`（放宽 10 倍，仅过滤浮点舍入误差），或改用单调递增的 `mood_version` 整数版本号。
2. WHEN 并发 mood 更新的差值 ≥ 0.0001（即真实并发变化），THE CAS 逻辑 SHALL 识别为冲突并应用 delta（而非"无变化"分支直接覆盖）。
3. THE `_clamp_mood()` SHALL 保留现有逻辑（clamp 到 [-1, 1]），不修改。
4. THE 修复 SHALL 不改变 CAS 的三阶段结构（snapshot → LLM 分析 → re-read under lock → compare → apply）。

#### Notes / Constraints

- 涉及文件：`astrmai/state/chat_state_service.py` — `StateEngine.update_mood()`（约 L438+）
- 根因：CAS 比较使用 `abs(diff) < 0.001`，如果 snapshot_mood=0.5000 且并发更新将其改为 0.5005，`abs(0.5005 - 0.5000) = 0.0005 < 0.001` → 被视为"无变化" → 绝对覆盖而非 delta 应用。
- 修复方式：阈值改为 `0.0001` 过滤浮点误差，真正的并发变化（≥0.0001）触发 delta 应用。
- 验证：并发调 `update_mood(delta=0.0005)` → 确认最终 mood 反映两次 delta 而非仅一次。

---

### Requirement 5: FrequencyController 读 energy/mood 不持锁 — 获取最新快照

**User Story:** 作为依赖频控准确性来避免过度发言的用户，当 `FrequencyController.should_reply()` 读取 `energy` 和 `mood` 来判断是否回复时，我不希望读到的值是过期的（可能已被 `ChatStateService` 的 daily reset 或 energy 消耗修改），所以频控决策基于最新的状态快照。

#### Acceptance Criteria

1. THE `FrequencyController.should_reply()`（L61-130）SHALL 在读取 `energy` 和 `mood` 参数时，要求调用方传入的是通过 `ChatStateService.get_state()` 获取的最新值（已在锁内），而非外部缓存的过期值。
2. THE `AttentionGate` 中调用 `should_reply()` 的位置 SHALL 在获取 ChatState 锁后传入 `state.energy` 和 `state.mood`（当前行为需确认是否已在锁内）。
3. THE `FrequencyController` SHALL 在 docstring 中明确标注"调用方负责保证传入的 `energy`/`mood` 是最新值（建议在 `ChatStateService` 锁内调用）"。
4. WHERE `energy` 或 `mood` 参数为 `None`（表示调用方未提供），THE `should_reply()` SHALL 使用保守默认值（energy=1.0, mood=0.0）并记录 debug 日志。

#### Notes / Constraints

- 涉及文件：
  - `astrmai/state/energy/frequency_controller.py` — `should_reply()` L61-130
  - `astrmai/conversation/attention/gate.py` — 调用 `should_reply()` 的位置
- 根因：`should_reply(energy=1.0, mood=0.0)` 的默认值意味着调用方如果不传参，会用满 energy + 中性 mood 做决策——可能在低 energy 时错误地决定回复。
- 修复方式：调用方在 `ChatStateService.get_state()` 锁内获取 energy/mood 后传入；`AttentionGate` 确认调用点是否在锁内；如果当前不在锁内，改为先获取 state 再调用。
- 验证：模拟低 energy 场景 → 确认 `should_reply()` 正确接收最新 energy 值 → 确认频控决策准确。

---

### Requirement 6: `_load_profile_with_relationship` 锁间隙被并发覆盖

**User Story:** 作为依赖用户画像准确性的系统，当 `update_social_score()` 在 `get_user_profile()`（获取锁）之后、`relationship_vector` 赋值之前存在锁间隙时，我不希望并发的另一个 `update_social_score()` 在此窗口内覆盖了刚赋值的 relationship_vector，所以画像更新是原子的。

#### Acceptance Criteria

1. THE `update_social_score()`（L143-169）SHALL 将 `get_user_profile()` 和 `relationship_vector` 赋值放在同一个 `async with self._get_user_lock(user_id)` 锁块内，消除 L155-167 之间的锁间隙。
2. THE `_load_profile_with_relationship()` 逻辑 SHALL 被内联到 `update_social_score()` 的锁块内，不再作为独立步骤在锁外执行。
3. WHEN 两个并发的 `update_social_score()` 调用同一 user_id，THE 第二个调用的 relationship_vector SHALL 基于第一个调用已更新的 profile 进行操作，而非基于旧快照。

#### Notes / Constraints

- 涉及文件：`astrmai/state/user_profile_service.py`
  - `update_social_score()` L143-169 — `get_user_profile()` 在 L143（获取锁）之后，`relationship_vector` 赋值在 L165，但 `_load_profile_with_relationship` 在锁外执行
  - `_load_profile_with_relationship()` — 在 `get_user_profile` 返回后、锁获取前填充 relationship_vector
- 根因：`get_user_profile()` 内部获取锁读取 DB → 释放锁返回 profile → 调用方在锁外填充 relationship_vector → 重新获取锁写入。锁间隙内，另一个协程可能已经修改了同一个 profile。
- 修复方式：将 relationship_vector 的加载和赋值移入锁内；`update_social_score()` 在锁内完成全部读写操作。
- 验证：并发 2 个 `update_social_score()` → 确认两次更新的 relationship_vector 正确叠加。

---

### Wave 3：P2 会话持久化与文档化（2 项）

---

### Requirement 7: GroupReplyWaitManager + PrivateChatManager 零持久化 — 重启感知降级

**User Story:** 作为依赖群组回复等待和私聊连续对话功能的用户，当插件重启时，我不希望正在等待中的会话静默丢失导致用户困惑（Bot 说了一句期待回复的话但重启后再也不等了），所以重启后有明确的降级行为（而非静默丢弃）。

#### Acceptance Criteria

1. THE `GroupReplyWaitManager` SHALL 在 `register_from_reply_event()`（L108）时，将 wait state 的元数据（chat_id、target_user_id、reason、expires_at）通过 `event.set_extra()` 附加到事件上，使 AstrBot 框架在重启后能从事件中恢复 wait 上下文。
2. THE `PrivateChatManager` SHALL 在插件 `terminate()` 时记录当前活跃会话的 user_id 列表到 KV storage（`self.put_kv_data("pending_private_sessions", [...])`），在 `on_program_start()` 时清理过期的 pending session。
3. WHEN 插件重启后检测到残留的 pending private session（TTL 已过期），THE `PrivateChatManager` SHALL 静默清理而非尝试恢复。
4. THE 修复 SHALL NOT 将完整的 wait/session 状态持久化到 DB（状态生命周期短，DB 开销过大），仅通过 event extra 和 KV storage 做轻量恢复提示。
5. THE `GroupReplyWaitManager` 和 `PrivateChatManager` 的 docstring SHALL 明确标注"重启后状态丢失，依赖 event extra / KV storage 做降级恢复"。

#### Notes / Constraints

- 涉及文件：
  - `astrmai/state/group_wait/group_reply_wait_manager.py` — 全文
  - `astrmai/state/private_chat/private_chat_manager.py` — 全文
- 根因：两个管理器均为纯内存状态，无任何持久化。GroupReplyWaitManager 的 wait state 在重启后完全丢失，PrivateChatManager 的 session 同样丢失——用户可能正在等待 Bot 的下一句回复。
- 修复方式：
  1. `GroupReplyWaitManager.register_from_reply_event()` 将 wait 元数据写入 `event.set_extra("astrmai_group_wait", {...})`
  2. `PrivateChatManager` 在 `terminate()` 时将活跃 session user_ids 写入 KV storage
  3. `on_program_start()` 读取 KV storage 并清理过期条目
- 验证：构造 wait → 模拟重启 → 确认 event extra 可读取 → 确认 KV storage 写入/读取正确。

---

### Requirement 8: `flush_message_counters` 迭代不持锁 — TOCTOU 防御

**User Story:** 作为依赖用户画像批量持久化的系统，当 `flush_message_counters()` 遍历 `self.user_profiles` dict 时，我不希望遍历过程中 profile 被并发删除导致 `KeyError` 或 `AttributeError`，所以批量 flush 是安全的。

#### Acceptance Criteria

1. THE `flush_message_counters()`（L522-534）SHALL 在遍历 `self.user_profiles.items()` 之前获取一个模块级 `asyncio.Lock`（`_profiles_dict_lock`），防止并发 `del self.user_profiles[user_id]` 导致迭代中断。
2. THE `UserProfileService` SHALL 在所有修改 `self.user_profiles` dict 结构的地方（`del`、`pop`、新增）同样获取 `_profiles_dict_lock`。
3. WHEN `flush_message_counters()` 在锁内遍历时发现 `profile is None`（被并发删除），THE 函数 SHALL 静默跳过该条目并记录 debug 日志，而非抛异常。
4. THE 锁 SHALL 仅在 dict 结构修改时持有（不在 `_save_profile()` 的 DB 写入期间持有，避免阻塞其他协程）。

#### Notes / Constraints

- 涉及文件：`astrmai/state/user_profile_service.py`
  - `flush_message_counters()` L522-534 — 遍历 `self.user_profiles.items()` 不持锁
  - `user_profiles` dict 的修改点（`del`、`pop`）需确认
- 根因：L523-527 的 list comprehension 在遍历 `self.user_profiles.items()` 时，如果另一个协程并发删除了某个 user_id，会触发 `RuntimeError: dictionary changed size during iteration` 或在 L530 的 `.get(user_id)` 返回 None。
- 修复方式：新增 `_profiles_dict_lock`，在遍历和 dict 结构变更时使用。
- 验证：并发 `flush_message_counters()` + `del user_profiles[id]` → 确认无异常 → 确认 flush 跳过已删除条目。

---

## Out of Scope（不在本 Spec 范围内）

- **第一轮 7 项硬伤修复**（已完成，独立 Spec）
- **决策点优化**（D1–D11）
- **多 Agent 安全加固**
- **资源泄漏修复**
- **WebUI 安全加固**
- **新功能开发**
- **代码质量基线治理**
- **依赖升级**
- **状态机行为变更**（如 energy/mood 计算公式调整、频控策略调整）

---

## High-Risk Confirmation List（高风险确认清单）

| # | 风险事项 | 等级 | 触发条件 | 缓解措施 |
|---|---------|:--:|---------|---------|
| HK1 | R1 统一 immediate-save 后，DB 写入频率增加 → 如果 `save_chat_state()` 是同步阻塞调用，可能影响吞吐 | 🟡 | 高并发群聊 + 频繁状态变更 | `save_chat_state()` 已是 async，确认内部使用 `asyncio.to_thread()` 包装 DB 操作 |
| HK2 | R2 移除 `profile_metadata["relationship_vector"]` 写入 → 已有生产数据中 `profile_metadata` 仍含旧值，需做单向迁移 | 🟡 | 生产 DB 中 `profile_metadata` 含 `relationship_vector` key | R2 AC3 要求 `get_user_profile()` 加载时自动迁移 |
| HK3 | R1 统一持久化策略后，如果 `_get_state_inner()` 的 daily reset 也 immediate-save → 每次 `get_state()` 都可能触发 DB 写 | 🟡 | 高频读取 ChatState 的场景 | daily reset 仅每日触发一次，实际 DB 写频率低 |
| HK4 | R4 放宽 CAS 阈值后，浮点舍入误差可能被误判为真实并发变化 → delta 累积 | 🟢 | 连续大量小数 mood 更新 | `0.0001` 阈值远大于 Python float 的 ulp（~1e-16），不会误判 |
| HK5 | R5 要求调用方传入最新 energy/mood → 如果 AttentionGate 当前调用点不在锁内，修改可能影响正常消息流 | 🟡 | AttentionGate 当前通过外部缓存传参 | 先在 `should_reply()` 内部增加防御性日志，确认调用点后逐步迁移 |
| HK6 | R6 将 relationship_vector 加载移入锁内 → 锁持有时间增长，可能增加锁竞争 | 🟡 | 高并发 `update_social_score()` | `_load_profile_with_relationship` 仅做 dict 赋值（O(1)），不涉及 DB IO |
| HK7 | R7 KV storage 写入/读取增加插件启动/关闭开销 → 如果 KV storage 不可用，降级路径需健壮 | 🟢 | KV storage 后端故障 | 所有 KV 操作包裹 try/except，失败时静默降级 |
| HK8 | R8 新增 `_profiles_dict_lock` → 需要审计所有 `self.user_profiles` dict 修改点，遗漏一处即无效 | 🟡 | 存在未发现的 dict 修改路径 | 搜索 `user_profiles[` 和 `del user_profiles` 全项目 |

---

## Dependency Map（需求依赖关系）

```
Wave 1 (P1 持久化一致性)     Wave 2 (P1 并发竞态)      Wave 3 (P2 会话/文档化)
  R1 (dirty-flag) ──────────┐
  R2 (vector 双写) ────────┤
  R3 (energy 文档化) ──────┤   ← R1-3 均修改 chat_state_service.py
                            │      / user_profile_service.py
                            ├──► R4 (CAS 阈值) ──► R5 (频控锁)
                            │          │
                            ├──────────┘
                            │
                            ├──► R6 (profile 锁间隙)
                            │          │
                            └──────────┼──────────► R7 (会话持久化)
                                       │
                                       └──────────► R8 (flush 锁)

R1-3 可并行（修改不同方法，同一文件但互不冲突）
R4 和 R5 可并行
R6 独立于 R4-5
R7 和 R8 完全独立
```

---

## Verification Strategy（验证策略）

| 验证层 | 命令/方式 | 覆盖需求 |
|--------|----------|:------:|
| 单元测试 | `pytest tests/ -v -k "chat_state_service"` | R1, R3, R4 |
| 单元测试 | `pytest tests/ -v -k "user_profile_service"` | R2, R6, R8 |
| 单元测试 | `pytest tests/ -v -k "frequency_controller"` | R5 |
| 单元测试 | `pytest tests/ -v -k "group_wait or private_chat"` | R7 |
| LSP | `lsp_diagnostics` 对全部变更文件 | R1–R8 |
| 集成测试 | 并发 daily reset + energy 消耗 → 确认状态一致 | R1 |
| 集成测试 | `update_social_score()` 后重启 → 确认 relationship_vector 恢复正确 | R2 |
| 集成测试 | `should_drop_by_energy()` 后检查 `state.is_dirty` | R3 |
| 集成测试 | 并发 2 个 `update_mood(delta=0.0005)` → 确认最终 mood 反映两次 delta | R4 |
| 集成测试 | 低 energy 场景 → `should_reply()` 正确接收 energy | R5 |
| 集成测试 | 并发 2 个 `update_social_score()` → relationship_vector 正确叠加 | R6 |
| 集成测试 | 构造 wait → 模拟重启 → 确认 event extra 可读 + KV storage 正确 | R7 |
| 集成测试 | 并发 `flush_message_counters()` + `del user_profiles[id]` → 无异常 | R8 |
| 手工验证 | 全量回归：`pytest tests/ -v --tb=short` ≥ 70 passed | R1–R8 |

---

> **写入 3 完成。** `requirements.md` 全部 8 条需求已写入。可进入 Kiro Phase 2（设计文档）。

