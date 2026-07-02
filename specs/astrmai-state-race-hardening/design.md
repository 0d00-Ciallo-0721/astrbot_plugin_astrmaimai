# Design Document

> 本文档对应 Spec `astrmai-state-race-hardening`，描述 8 个状态机竞态风险的修复设计方案。
> 不包含第一轮硬伤修复、决策点优化、多 Agent 安全、资源泄漏、WebUI 加固。
> 凡涉及 `ChatStateService` 持久化策略和 `UserProfileService` 关系向量的改动，本阶段一律先方案后落地。

## 1. Overview

### 1.1 整体策略

按「持久化一致性 → 并发写入竞态 → 会话持久化」三波推进。核心思路：统一 immediate-save 策略、消除双写、补齐锁保护。

| Wave | 主要动作 | 改动文件 | 改动类型 |
|------|---------|---------|---------|
| ① Wave 1 | 统一 ChatState 持久化 + RelationshipVector 单写 + EnergyManager 文档量化 | `chat_state_service.py`, `user_profile_service.py`, `energy_manager.py` | 重构 + 文档 |
| ② Wave 2 | CAS 阈值放宽 + FrequencyController 锁内调用 + profile 锁间隙消除 | `chat_state_service.py`, `frequency_controller.py`, `gate.py`, `user_profile_service.py` | 并发修复 |
| ③ Wave 3 | GroupWait event extra + PrivateChat KV storage + flush 迭代锁 | `group_reply_wait_manager.py`, `private_chat_manager.py`, `user_profile_service.py` | 防御性加固 |

### 1.2 设计边界（重申）

- 不修改状态机行为逻辑（energy/mood 计算公式、频控策略、情绪判定规则）
- 不引入新的 DB 表或列
- 不修改 AstrBot 框架 API
- 不新增 pip 依赖
- 不删除任何现有方法（仅重构内部实现）

### 1.3 与 AstrBot 框架的接口预留

| 预留点 | 位置 | 用途 |
|--------|------|------|
| `event.set_extra("astrmai_group_wait", ...)` | `group_reply_wait_manager.py` | R7 重启感知降级 |
| `self.put_kv_data("pending_private_sessions", ...)` | `private_chat_manager.py` | R7 会话残留清理 |

---

## 2. Architecture

### 2.1 核心不变量（本 Spec 阶段冻结）

| 不变量 | 来源 | 冻结理由 |
|--------|------|---------|
| ChatState 所有写路径统一 immediate-save | `chat_state_service.py` | R1 消除 dirty-flag 混用 |
| `relationship_vector` 单写 `UserProfile.relationship_vector` | `user_profile_service.py` | R2 消除双写 |
| `profile_metadata["relationship_vector"]` 仅读不写 | `user_profile_service.py` | R2 单向迁移 |
| `_load_profile_with_relationship` 移入锁内 | `user_profile_service.py` | R6 消除锁间隙 |
| `flush_message_counters` 持 `_profiles_dict_lock` | `user_profile_service.py` | R8 TOCTOU 防御 |
| CAS 阈值 `0.0001`（放宽 10x） | `chat_state_service.py` | R4 浮点误差 |
| GroupWait 元数据写入 event extra | `group_reply_wait_manager.py` | R7 轻量恢复 |
| PrivateChat session IDs 写入 KV storage | `private_chat_manager.py` | R7 残留清理 |

### 2.2 持久化策略统一（R1 核心变更）

```
修复前：                              修复后：
mark_energy_consumed()                 mark_energy_consumed()
  → save_chat_state() (immediate)        → save_chat_state() (immediate)  ✓ 不变
  → is_dirty = False (手动清零)          → is_dirty = False (手动清零)

_get_state_inner()                     _get_state_inner()
  → daily reset                          → daily reset
  → _mark_dirty() (lazy)                 → _mark_dirty() → save_chat_state()  ★ 改为 immediate

should_drop_by_energy()                should_drop_by_energy()
  → if should_drop and is_dirty:         → save_chat_state()  ★ 始终 persist
       save_chat_state()

_persist_if_dirty()                    _persist_if_dirty()
  → 条件持久化                            → 保留作为防御性兜底（不再作为主路径）
```

---

## 3. Wave 1 — P1 数据持久化一致性（R1–R3）

### 3.1 R1: ChatState dirty-flag 不一致

**涉及文件**: `astrmai/state/chat_state_service.py`

#### 3.1.1 当前状态

四种写路径混用两种持久化策略：

| 方法 | 行号 | 持久化方式 | 问题 |
|------|:--:|-----------|------|
| `atomic_update_mood()` | L124-139 | immediate save (L138) + 手动 `is_dirty=False` (L151) | 手动清零与 dirty-flag 模式冲突 |
| `mark_energy_consumed()` | L141-152 | immediate save (L150) + 手动 `is_dirty=False` (L151) | 同上 |
| `_get_state_inner()` daily reset | L107-119 | `_mark_dirty()` (L119) → lazy | 不回写 DB，依赖后续调用方 flush |
| `should_drop_by_energy()` | L154-161 | 条件持久化（L158: `if should_drop and is_dirty`） | `should_drop=False` 但 `is_dirty=True` 时不写 |

```python
# L151: 手动清零 is_dirty — 这覆盖了 L119 中 daily reset 设置的 is_dirty=True
state.is_dirty = False  # ← 问题行

# L158-160: 仅条件持久化
if should_drop and getattr(state, "is_dirty", False):
    await self.persistence.save_chat_state(chat_id, state)
    state.is_dirty = False
```

#### 3.1.2 设计决策

**统一为 immediate-save 模式。** 所有写路径在锁内修改 state 后直接 `save_chat_state()`。

```python
# _get_state_inner() 修改后：
def _get_state_inner(self, chat_id: str) -> ChatState:
    # ... daily reset logic ...
    if daily_reset_triggered:
        state.energy = min(1.0, state.energy + recovery)
        state.mood = 0.0
        state.last_reset_date = today_str
        self._mark_dirty(state)
        await self.persistence.save_chat_state(chat_id, state)  # ★ 新增 immediate save
        state.is_dirty = False

# should_drop_by_energy() 修改后：
async def should_drop_by_energy(self, chat_id, msg_count, energy_manager):
    async with self._get_chat_lock(chat_id):
        state = await self._get_state_inner(chat_id)
        should_drop = energy_manager.should_drop_by_energy(state, msg_count)
        if getattr(state, "is_dirty", False):  # ★ 去掉 should_drop 条件
            await self.persistence.save_chat_state(chat_id, state)
            state.is_dirty = False
        return should_drop
```

#### 3.1.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `chat_state_service.py` | `_get_state_inner()` daily reset 后增加 `save_chat_state()` | +3 |
| `chat_state_service.py` | `should_drop_by_energy()` 去掉 `should_drop` 条件 | -1 |

#### 3.1.4 禁止改动

- **不**修改 `mark_energy_consumed()` 和 `atomic_update_mood()` 的已有逻辑（已是 immediate-save）
- **不**修改 `_persist_if_dirty()` 实现（保留作为兜底）
- **不**修改 `_mark_dirty()` 的语义

---

### 3.2 R2: RelationshipVector 双写不同步

**涉及文件**: `astrmai/state/user_profile_service.py`

#### 3.2.1 当前状态

```python
# L164-167: 双写
if relationship_vector:
    profile.relationship_vector = relationship_vector          # L165: 字段写
    meta = self._profile_metadata(profile)
    meta["relationship_vector"] = relationship_vector          # L167: metadata 也写
```

#### 3.2.2 设计决策

**`profile.relationship_vector` 作为唯一写入目标。** `profile_metadata["relationship_vector"]` 仅作为加载时的单向迁移源。

```python
# update_social_score() 修改后：
if relationship_vector:
    profile.relationship_vector = relationship_vector  # L165: 仅写字段
    # meta["relationship_vector"] = ...  ★ 删除此行

# get_user_profile() 新增加载迁移逻辑：
def _migrate_relationship_vector(self, profile: UserProfile) -> None:
    """单向迁移：将 profile_metadata 中的旧 relationship_vector 移至字段。"""
    meta = self._profile_metadata(profile)
    if "relationship_vector" in meta:
        if not profile.relationship_vector:  # 字段为空时才迁移
            profile.relationship_vector = meta.pop("relationship_vector")
        else:
            meta.pop("relationship_vector", None)  # 字段已有值，清理 metadata
```

#### 3.2.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `user_profile_service.py` | `update_social_score()` 删除 `meta["relationship_vector"]` 写入 | -1 |
| `user_profile_service.py` | 新增 `_migrate_relationship_vector()` + 在 `get_user_profile()` 加载时调用 | +12 |

#### 3.2.4 禁止改动

- **不**修改 `relationship_vector` 的数据结构（4 维 float dict）
- **不**修改 `_save_profile()` 的序列化逻辑
- **不**新增 DB 列

---

### 3.3 R3: EnergyManager 恢复 energy 副作用文档化

**涉及文件**: `astrmai/state/energy/energy_manager.py`

#### 3.3.1 当前状态

```python
# L20-42: 副作用已在 docstring 中提及但未量化
def should_drop_by_energy(self, state: Any, msg_count: int) -> bool:
    """
    ...
    .. note::
        This method has a **side effect**: when it returns ``True``, it
        also recovers a small amount of energy (``msg_count * cost_per_reply``)
        and sets ``state.is_dirty = True`` ...
    """
    # L39-41: 恢复逻辑
    recover_amount = float(msg_count) * self._energy_config().cost_per_reply
    state.energy = min(1.0, current_energy + recover_amount)  # 上限 1.0
    state.is_dirty = True
```

#### 3.3.2 设计决策

**增强 docstring + 缓存配置。** 不改变行为逻辑。

```python
def should_drop_by_energy(self, state: Any, msg_count: int) -> bool:
    """Return True when the message should be dropped due to low energy.

    Side Effect (documented):
        When this method returns True, it recovers energy as:
            recover = min(msg_count * cost_per_reply, 0.5 - current_energy)
            state.energy = min(1.0, current_energy + recover)
        This implements a "skip-to-recharge" design: the bot skips a reply
        to preserve energy for more important messages.
        Callers MUST persist state.energy after this call (state.is_dirty=True).

    Returns:
        True if the message should be dropped (with energy recovery side-effect).
    """
```

配置缓存（在 `__init__` 中）：
```python
def __init__(self, config):
    self._cached_energy_config = self._energy_config()  # 缓存
```

#### 3.3.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `energy_manager.py` | 增强 docstring + 配置缓存 | +8/-2 |

#### 3.3.4 禁止改动

- **不**修改 `should_drop_by_energy()` 的判定和恢复逻辑
- **不**修改 `_energy_config()` 的配置读取逻辑

---

## 4. Wave 2 — P1 并发写入竞态（R4–R6）

### 4.1 R4: Mood CAS 精度窗口

**涉及文件**: `astrmai/state/chat_state_service.py` — `StateEngine.update_mood()`

#### 4.1.1 当前状态

CAS 三阶段结构（在 `StateEngine` 中，约 L438+）：
```python
# Phase 1: snapshot (no lock)
snapshot_mood = state.mood
# Phase 2: LLM analysis (no lock)
new_mood = await self.mood_manager.analyze_mood(...)
# Phase 3: re-read under lock, compare, apply
async with self._get_chat_lock(chat_id):
    current_mood = state.mood
    diff = abs(snapshot_mood - current_mood)
    if diff < 0.001:         # ← 精度窗口
        state.mood = new_mood  # 绝对覆盖
    else:
        state.mood = clamp(current_mood + delta)  # delta 应用
```

#### 4.1.2 设计决策

**放宽阈值到 `0.0001`，仅过滤浮点舍入误差。**

```python
if diff < 0.0001:  # ★ 从 0.001 改为 0.0001
    state.mood = new_mood  # 无并发变化 → 绝对覆盖
else:
    state.mood = self._clamp_mood(current_mood + delta)  # 有并发变化 → delta 应用
```

#### 4.1.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `chat_state_service.py` | `StateEngine.update_mood()` CAS 阈值 | +1/-1 |

#### 4.1.4 禁止改动

- **不**修改 CAS 三阶段结构
- **不**修改 `_clamp_mood()` 逻辑
- **不**修改 LLM 分析阶段

---

### 4.2 R5: FrequencyController 读 energy/mood 不持锁

**涉及文件**: `astrmai/state/energy/frequency_controller.py`, `astrmai/conversation/attention/gate.py`

#### 4.2.1 当前状态

```python
# frequency_controller.py L61-130:
def should_reply(self, chat_id, is_mentioned, energy=1.0, mood=0.0, ...):
    # 使用传入的 energy/mood 参数做决策，不主动获取最新值
```

调用方 `AttentionGate` 传入 `energy`/`mood` 的方式需确认。如果调用方使用缓存值而非 `ChatStateService.get_state()` 的最新值，决策基于过期数据。

#### 4.2.2 设计决策

**防御性增强：`should_reply()` 增加参数校验 + docstring 明确调用约定。**

```python
def should_reply(self, chat_id, is_mentioned, energy=1.0, mood=0.0, ...):
    """...
    Concurrency Note:
        Callers MUST pass the latest energy/mood values obtained under
        ChatStateService's per-chat lock. Passing stale values may cause
        incorrect frequency decisions.
    """
    if energy is None:
        logger.debug(f"[FreqCtrl] energy=None for {chat_id}, using default 1.0")
        energy = 1.0
    if mood is None:
        mood = 0.0
    # ... 原有逻辑不变
```

调用方（`AttentionGate`）确认：在 `process_event()` 中，`should_reply()` 的调用应放在 `ChatStateService.get_state()` 之后、锁内。

#### 4.2.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `frequency_controller.py` | docstring 增强 + None 参数防御 | +6 |
| `gate.py` | 确认 `should_reply()` 调用点是否在锁内（可能无需改动） | 0 or +3 |

#### 4.2.4 禁止改动

- **不**修改 `should_reply()` 的频控算法
- **不**修改 `AttentionGate.process_event()` 的主流程

---

### 4.3 R6: `_load_profile_with_relationship` 锁间隙

**涉及文件**: `astrmai/state/user_profile_service.py`

#### 4.3.1 当前状态

```python
# update_social_score() 当前的锁间隙：
profile = await self.get_user_profile(user_id)  # 获取锁 → 读 DB → 释放锁
# ... 锁间隙开始 ...
# 在锁外填充 relationship_vector（_load_profile_with_relationship）
profile.relationship_vector = relationship_vector
# ... 锁间隙结束 ...
async with self._get_user_lock(user_id):  # 重新获取锁
    # 写入
```

#### 4.3.2 设计决策

**将 relationship_vector 填充移入锁内，消除锁间隙。**

```python
# 修改后：
async with self._get_user_lock(user_id):
    profile = await self._load_profile_locked(user_id)  # ★ 锁内加载
    profile.social_score = score
    if relationship_vector:
        profile.relationship_vector = relationship_vector
    self._touch_profile(profile, now=now)
    await self._save_profile(profile)
```

`_load_profile_locked()` 内联 `get_user_profile()` 的核心逻辑但不获取锁（调用方已持有）。

#### 4.3.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `user_profile_service.py` | `update_social_score()` 重构：锁内加载 + 赋值 | +8/-5 |

#### 4.3.4 禁止改动

- **不**修改 `_save_profile()` 逻辑
- **不**修改 `get_user_profile()` 的公共 API（保留供其他调用方使用）

---

## 5. Wave 3 — P2 会话持久化与文档化（R7–R8）

### 5.1 R7: GroupWait + PrivateChat 零持久化

**涉及文件**: `astrmai/state/group_wait/group_reply_wait_manager.py`, `astrmai/state/private_chat/private_chat_manager.py`

#### 5.1.1 当前状态

```python
# GroupReplyWaitManager: 纯内存 dict，无持久化
self._wait_states: Dict[str, GroupReplyWaitState] = {}

# PrivateChatManager: 纯内存 dict，无持久化
self._sessions: Dict[str, PrivateSession] = {}
```

#### 5.1.2 设计决策

**GroupWait: 元数据写入 event extra 实现重启感知。**

```python
# register_from_reply_event() 修改：
def register_from_reply_event(self, event):
    # ... 原有逻辑 ...
    wait_state = self._wait_states[chat_id]
    # ★ 新增：将 wait 元数据附加到 event
    event.set_extra("astrmai_group_wait", {
        "chat_id": chat_id,
        "target_user_id": wait_state.target_user_id,
        "target_name": wait_state.target_name,
        "reason": wait_state.reason,
        "expires_at": wait_state.expires_at,
        "thread_signature": wait_state.thread_signature,
    })
    return True
```

**PrivateChat: terminate() 时写入 KV storage，on_program_start() 清理过期。**

```python
# PrivateChatManager:
async def _persist_pending_sessions(self) -> None:
    """terminate() 时调用。"""
    try:
        active_ids = list(self._sessions.keys())
        await self.host_plugin.put_kv_data("pending_private_sessions", active_ids)
    except Exception:
        pass  # KV storage 不可用时静默降级

async def _cleanup_stale_pending_sessions(self) -> None:
    """on_program_start() 时调用。"""
    try:
        pending = await self.host_plugin.get_kv_data("pending_private_sessions", default=[])
        if pending:
            logger.info(f"[PrivateChat] cleaned up {len(pending)} stale pending sessions from previous run")
            await self.host_plugin.put_kv_data("pending_private_sessions", [])
    except Exception:
        pass
```

#### 5.1.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `group_reply_wait_manager.py` | `register_from_reply_event()` 增加 event.set_extra | +8 |
| `private_chat_manager.py` | 新增 `_persist_pending_sessions()` + `_cleanup_stale_pending_sessions()` | +20 |

#### 5.1.4 禁止改动

- **不**将完整 wait/session 状态持久化到 DB
- **不**修改 wait/session 的 TTL 逻辑
- **不**新增 pip 依赖

---

### 5.2 R8: `flush_message_counters` 迭代不持锁

**涉及文件**: `astrmai/state/user_profile_service.py`

#### 5.2.1 当前状态

```python
# L522-534: 遍历 self.user_profiles 不持锁
async def flush_message_counters(self) -> None:
    dirty_user_ids = [
        user_id
        for user_id, profile in self.user_profiles.items()  # ← 无锁遍历
        if getattr(profile, "is_dirty", False)
    ]
    for user_id in dirty_user_ids:
        async with self._get_user_lock(user_id):
            profile = self.user_profiles.get(user_id)  # TOCTOU
```

#### 5.2.2 设计决策

**新增 `_profiles_dict_lock`，保护 dict 结构变更。**

```python
# UserProfileService.__init__:
self._profiles_dict_lock = asyncio.Lock()

# flush_message_counters() 修改：
async def flush_message_counters(self) -> None:
    async with self._profiles_dict_lock:  # ★ 保护 dict 遍历
        dirty_user_ids = [
            user_id
            for user_id, profile in self.user_profiles.items()
            if getattr(profile, "is_dirty", False)
        ]
    for user_id in dirty_user_ids:
        async with self._get_user_lock(user_id):
            profile = self.user_profiles.get(user_id)
            if profile is None:  # ★ 防御性检查（被并发删除）
                logger.debug(f"[UserProfile] skip flushed profile {user_id}: already removed")
                continue
            if not getattr(profile, "is_dirty", False):
                continue
            await self._save_profile(profile)
            profile.is_dirty = False
```

所有 `self.user_profiles` dict 的结构修改点（`del`、`pop`、赋值）也需获取 `_profiles_dict_lock`（搜索确认全部修改点）。

#### 5.2.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `user_profile_service.py` | `__init__` 新增 `_profiles_dict_lock` | +1 |
| `user_profile_service.py` | `flush_message_counters()` 增加锁 + None 防御 | +5/-0 |
| `user_profile_service.py` | 所有 `del/pop user_profiles[...]` 增加锁 | +4 |

#### 5.2.4 禁止改动

- **不**修改 `_save_profile()` 的 DB 写入逻辑
- **不**修改 `_get_user_lock()` 的 per-user 锁逻辑

---

## 6. Risk Assessment

| # | 风险 | 等级 | 触发条件 | 缓解措施 |
|---|------|:--:|---------|---------|
| RSK1 | R1 immediate-save 后 `_get_state_inner()` 每次 daily reset 都会写 DB — 但 daily reset 每天仅一次，实际频率极低 | 🟢 | 跨日期边界首次 `get_state()` | 频率可忽略（1次/chat/天） |
| RSK2 | R2 单向迁移后，如果旧代码中有路径直接读 `meta["relationship_vector"]` 而不读 `profile.relationship_vector` → 读到过期值 | 🟡 | 存在直接读 `meta["relationship_vector"]` 的外部代码 | 搜索 `["relationship_vector"]` 全项目，确认仅 `user_profile_service.py` 内部使用 |
| RSK3 | R4 CAS 阈值放宽 10 倍 → 极端频繁的 mood 更新可能产生累计误差 | 🟢 | 每秒 >100 次 mood 更新 | 正常对话频率 <1 次/秒，不会触发 |
| RSK4 | R5 调用方不在锁内 → 增加 None 防御后行为不变但决策仍基于可能过期的值 | 🟡 | AttentionGate 当前调用点不在锁内 | 先增加防御 + 日志，确认调用点后逐步迁移 |
| RSK5 | R6 锁内加载 profile 可能增加锁持有时间 → 如果 `get_user_profile()` 的 DB 读取慢，会阻塞其他协程 | 🟡 | DB 延迟高（>100ms） | `get_user_profile()` 内部已使用缓存（内存 dict），仅首次访问走 DB |
| RSK6 | R7 KV storage 写入失败 → 静默降级，但重启后 pending session 清理不执行 | 🟢 | KV storage 后端故障 | 影响仅限于残留 session 清理（非关键路径） |
| RSK7 | R8 `_profiles_dict_lock` 增加后，如果遗漏某个 dict 修改点未加锁 → 锁无效 | 🟡 | 存在未发现的 `del/pop user_profiles` 路径 | 搜索 `user_profiles\[` 和 `del user_profiles` 全项目 |

---

## 7. Verification Matrix

| # | 需求 | 验证方式 | 通过标准 |
|---|------|---------|---------|
| V1 | R1 | 单元：daily reset 后 `is_dirty=True` → `_get_state_inner()` 调用 `save_chat_state()` | `save_chat_state` 被调用 |
| V2 | R1 | 集成：`should_drop_by_energy()` 返回 `False` 但 `is_dirty=True` → 仍 persist | 无脏数据丢失 |
| V3 | R2 | 单元：`update_social_score()` 后 `meta["relationship_vector"]` 不存在 | key 被移除 |
| V4 | R2 | 集成：加载旧 DB（`meta` 含 `relationship_vector`）→ 自动迁移到字段 | `profile.relationship_vector` 非空 |
| V5 | R3 | 单元：`should_drop_by_energy()` 后 `state.is_dirty=True` | 脏标记正确 |
| V6 | R3 | 手工：docstring 包含量化公式 | docstring 可读 |
| V7 | R4 | 单元：并发 `update_mood(delta=0.0005)` → 两次 delta 均反映 | 最终 mood 反映两次 delta |
| V8 | R4 | 单元：并发 `update_mood(delta=0.00005)` → 视为无变化（浮点误差） | 绝对覆盖 |
| V9 | R5 | 单元：`should_reply(energy=None)` → 默认值 + debug 日志 | 不抛异常 |
| V10 | R5 | 集成：低 energy 场景 → `should_reply()` 正确决策 | 决策基于最新 energy |
| V11 | R6 | 单元：并发 `update_social_score()` × 2 → 第二次基于第一次结果 | vector 正确叠加 |
| V12 | R7 | 单元：`register_from_reply_event()` → event extra 含 wait 元数据 | `get_extra("astrmai_group_wait")` 非空 |
| V13 | R7 | 集成：`terminate()` → KV storage 写入 → `on_program_start()` → 清理过期 | pending 列表正确 |
| V14 | R8 | 单元：并发 `flush + del user_profiles[id]` → 无异常 | 无 RuntimeError |
| V15 | R8 | 单元：`flush_message_counters()` 中 `profile is None` → 跳过 | debug 日志输出 |
| V16 | ALL | 全量回归：`pytest tests/ -v --tb=short` | ≥ 70 passed |

---

> **设计文档完成。** `design.md` 全部 8 个模块设计 + Risk Assessment + Verification Matrix 已写入。


