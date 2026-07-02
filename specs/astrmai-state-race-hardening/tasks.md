# Implementation Plan

> 本任务列表派生自同目录 `requirements.md` 与 `design.md`。
> **执行原则**：任务**严格串行**，编号 1 → N，后续任务依赖前一任务完成。
> **状态规则**：所有任务初始状态为 `- [ ]` 未完成。

## Overview

本任务列表把 8 条需求与 8 个模块设计翻译为 **8 个严格串行**的可执行任务。

| Phase | 主题 | 任务 | 改动类型 |
|-------|------|------|---------|
| Phase 1 | 持久化一致性 | Tasks 1-3 | 重构 + 文档 |
| Phase 2 | 并发写入竞态 | Tasks 4-6 | 并发修复 |
| Phase 3 | 会话/防御 | Tasks 7-8 | 防御性加固 |
| Phase 4 | 最终验证 | Tasks 9-10 | 验证 |

---

## Tasks

### Phase 1: 持久化一致性

- [ ] 1. R1: ChatState dirty-flag 统一 — immediate-save 一致化
  - **Goal**: ChatState 所有写路径统一为 immediate-save，消除 dirty-flag 混用
  - **Files**:
    - ✏️ `astrmai/state/chat_state_service.py` — `_get_state_inner()` + `should_drop_by_energy()`
  - **Steps**:
    1. `chat_state_service.py`: `_get_state_inner()` 中 daily reset 分支（L119 `_mark_dirty(state)` 之后）增加 `await self.persistence.save_chat_state(chat_id, state); state.is_dirty = False`
    2. `chat_state_service.py`: `should_drop_by_energy()` L158 条件从 `if should_drop and getattr(state, "is_dirty", False)` 改为 `if getattr(state, "is_dirty", False)`（去掉 `should_drop` 条件）
    3. 确认 `mark_energy_consumed()`（L141-152）和 `atomic_update_mood()`（L124-139）已是 immediate-save，无需修改
    4. 确认 `_persist_if_dirty()`（L80）保留作为防御性兜底
  - **Acceptance Criteria**:
    - daily reset 触发后 `save_chat_state()` 被同步调用
    - `should_drop_by_energy()` 在 `should_drop=False` 但 `is_dirty=True` 时仍然 persist
    - `mark_energy_consumed()` 和 `atomic_update_mood()` 行为不变
  - **Forbidden**: 不修改 `_persist_if_dirty()` 实现；不修改 `_mark_dirty()` 语义；不修改 energy/mood 计算逻辑
  - **Check Commands**: `pytest tests/ -v -k "chat_state_service"` ； `python -c "from astrmai.state.chat_state_service import ChatStateService; print('import OK')"`
  - **Risk Notes**: 🟢 daily reset 每天仅一次，DB 写入频率影响可忽略
  - _Requirements: R1_

- [ ] 2. R2: RelationshipVector 单写 — 消除双写 + 加载迁移
  - **Goal**: `relationship_vector` 仅写 `UserProfile.relationship_vector` 字段，`profile_metadata` 仅作为加载时单向迁移源
  - **Files**:
    - ✏️ `astrmai/state/user_profile_service.py` — `update_social_score()` + 新增 `_migrate_relationship_vector()` + `get_user_profile()` 加载时调用
  - **Steps**:
    1. `user_profile_service.py`: `update_social_score()` L167 删除 `meta["relationship_vector"] = relationship_vector`
    2. `user_profile_service.py`: 新增 `_migrate_relationship_vector(self, profile: UserProfile) -> None` 方法——若 `meta` 含 `relationship_vector` 且字段为空则迁移，否则清理 meta
    3. `user_profile_service.py`: `get_user_profile()` 加载 profile 后调用 `_migrate_relationship_vector(profile)`
    4. 搜索全项目 `["relationship_vector"]` 确认没有外部直接读 `meta["relationship_vector"]`
  - **Acceptance Criteria**:
    - `update_social_score()` 后 `meta["relationship_vector"]` 不存在
    - 旧 DB（meta 含 `relationship_vector`）→ 加载后自动迁移到字段
    - `profile.relationship_vector` 字段非空
  - **Forbidden**: 不修改 `relationship_vector` 数据结构；不修改 `_save_profile()` 序列化；不新增 DB 列
  - **Check Commands**: `pytest tests/ -v -k "user_profile_service or relationship"` ； `python -c "from astrmai.state.user_profile_service import UserProfileService; print('import OK')"`
  - **Risk Notes**: 🟡 需搜索确认无外部代码直接读 `meta["relationship_vector"]`
  - _Requirements: R2_

- [ ] 3. R3: EnergyManager 副作用文档化 — docstring + 配置缓存
  - **Goal**: `should_drop_by_energy()` 副作用在 docstring 中量化，配置缓存避免热更新不一致
  - **Files**:
    - ✏️ `astrmai/state/energy/energy_manager.py` — docstring + 配置缓存
  - **Steps**:
    1. `energy_manager.py`: `should_drop_by_energy()` 的 docstring（L23-28）增加量化公式：`recover = min(msg_count * cost_per_reply, 0.5 - current_energy); state.energy = min(1.0, current + recover)` 和 "Callers MUST persist state.energy after this call"
    2. `energy_manager.py`: `__init__()` 中缓存 `self._cached_energy_config = self._energy_config()`，`should_drop_by_energy()` 改用 `self._cached_energy_config`（若已实现 `_energy_config()` 每次都重新读取配置）
    3. 确认 `_energy_config()` 的调用方式——若已是属性缓存则跳过步骤 2
  - **Acceptance Criteria**:
    - docstring 包含量化公式
    - `state.is_dirty=True` 在 drop 时正确设置
    - 配置热更新一致性（缓存或重新读取有明确语义）
  - **Forbidden**: 不修改 `should_drop_by_energy()` 的判定和恢复逻辑；不修改 `_energy_config()` 的配置来源
  - **Check Commands**: `python -c "from astrmai.state.energy.energy_manager import EnergyManager; print(EnergyManager.should_drop_by_energy.__doc__[:100])"`
  - **Risk Notes**: 🟢 纯文档 + 缓存优化，零行为变更
  - _Requirements: R3_

---

### Phase 2: 并发写入竞态

- [ ] 4. R4: Mood CAS 阈值放宽 — `0.001` → `0.0001`
  - **Goal**: CAS 比较阈值仅过滤浮点舍入误差，不误杀真实并发变化
  - **Files**:
    - ✏️ `astrmai/state/chat_state_service.py` — `StateEngine.update_mood()`
  - **Steps**:
    1. 定位 `StateEngine.update_mood()` 中 CAS 比较代码（约 L438+）
    2. 将 `if diff < 0.001:` 改为 `if diff < 0.0001:`
    3. 确认 `_clamp_mood()` 和 CAS 三阶段结构不变
  - **Acceptance Criteria**:
    - 并发 `update_mood(delta=0.0005)` → 两次 delta 均反映
    - 并发 `update_mood(delta=0.00005)` → 视为无变化（浮点误差），绝对覆盖
    - `_clamp_mood()` 行为不变
  - **Forbidden**: 不修改 CAS 三阶段结构；不修改 `_clamp_mood()`；不修改 LLM 分析阶段
  - **Check Commands**: `pytest tests/ -v -k "mood or atomic_update"` ； `python -c "from astrmai.state.chat_state_service import StateEngine; print('import OK')"`
  - **Risk Notes**: 🟢 `0.0001` 远大于 Python float ulp（~1e-16），不会误判
  - _Requirements: R4_

- [ ] 5. R5: FrequencyController 文档化 + 防御 — 明确调用约定
  - **Goal**: `should_reply()` docstring 明确调用方责任，增加 None 参数防御
  - **Files**:
    - ✏️ `astrmai/state/energy/frequency_controller.py` — docstring + None 防御
    - 📖 `astrmai/conversation/attention/gate.py` — 确认调用点（可能无需改动）
  - **Steps**:
    1. `frequency_controller.py`: `should_reply()` docstring 增加 Concurrency Note：调用方必须在 `ChatStateService` 锁内获取最新 energy/mood 后传入
    2. `frequency_controller.py`: `should_reply()` 增加 `if energy is None: energy = 1.0; logger.debug(...)` 和 `if mood is None: mood = 0.0`
    3. `gate.py`: 搜索 `should_reply(` 调用点，确认传入的 `energy`/`mood` 来自 `ChatStateService.get_state()` 锁内获取的值
    4. 若当前调用点不在锁内，增加注释 `# TODO: wrap in ChatStateService lock`
  - **Acceptance Criteria**:
    - `should_reply(energy=None)` 不抛异常，使用默认值 + debug 日志
    - docstring 明确调用约定
    - 确认调用点状态（锁内/锁外）并记录
  - **Forbidden**: 不修改 `should_reply()` 的频控算法；不修改 `AttentionGate.process_event()` 主流程
  - **Check Commands**: `pytest tests/ -v -k "frequency_controller"` ； `python -c "from astrmai.state.energy.frequency_controller import FrequencyController; print('import OK')"`
  - **Risk Notes**: 🟡 若调用点不在锁内，先增加 TODO 注释，不强制迁移（避免影响正常消息流）
  - _Requirements: R5_

- [ ] 6. R6: profile 锁间隙消除 — `update_social_score()` 锁内加载
  - **Goal**: `update_social_score()` 在锁内完成全部读写，消除 relationship_vector 赋值前的锁间隙
  - **Files**:
    - ✏️ `astrmai/state/user_profile_service.py` — `update_social_score()`
  - **Steps**:
    1. `user_profile_service.py`: 新增 `_load_profile_locked(self, user_id)` 方法——内联 `get_user_profile()` 核心逻辑但不获取锁（调用方已持有）
    2. `user_profile_service.py`: 重构 `update_social_score()`——将 `async with self._get_user_lock(user_id):` 移到 `get_user_profile()` 之前，锁内完成加载→赋值→保存
    3. `user_profile_service.py`: 确认 `_save_profile()` 调用在锁内
  - **Acceptance Criteria**:
    - 并发 2 个 `update_social_score()` → 第二次基于第一次结果操作
    - relationship_vector 正确叠加
    - `get_user_profile()` 公共 API 保持不变（其他调用方不受影响）
  - **Forbidden**: 不修改 `_save_profile()` 逻辑；不修改 `get_user_profile()` 公共 API 签名
  - **Check Commands**: `pytest tests/ -v -k "user_profile_service or social_score"` ； `python -c "from astrmai.state.user_profile_service import UserProfileService; print('import OK')"`
  - **Risk Notes**: 🟡 锁持有时间略增，但 `get_user_profile()` 内部使用内存缓存（首次才读 DB）
  - _Requirements: R6_

---

### Phase 3: 会话/防御

- [ ] 7. R7: GroupWait + PrivateChat 重启感知 — event extra + KV storage
  - **Goal**: GroupReplyWaitManager 在 event extra 写入 wait 元数据；PrivateChatManager 在 terminate/start 时通过 KV storage 做残留清理
  - **Files**:
    - ✏️ `astrmai/state/group_wait/group_reply_wait_manager.py` — `register_from_reply_event()`
    - ✏️ `astrmai/state/private_chat/private_chat_manager.py` — 新增 `_persist_pending_sessions()` + `_cleanup_stale_pending_sessions()`
  - **Steps**:
    1. `group_reply_wait_manager.py`: `register_from_reply_event()` 返回前增加 `event.set_extra("astrmai_group_wait", {chat_id, target_user_id, target_name, reason, expires_at, thread_signature})`
    2. `private_chat_manager.py`: 新增 `_persist_pending_sessions()`——`self.host_plugin.put_kv_data("pending_private_sessions", list(self._sessions.keys()))`（包裹 try/except）
    3. `private_chat_manager.py`: 新增 `_cleanup_stale_pending_sessions()`——`self.host_plugin.get_kv_data("pending_private_sessions", default=[])` 读取并清理（包裹 try/except）
    4. 在插件的 `terminate()` 中调用 `_persist_pending_sessions()`（通过注入 host_plugin 引用或 facade 方法）
    5. 在插件的 `on_program_start()` 中调用 `_cleanup_stale_pending_sessions()`
  - **Acceptance Criteria**:
    - `register_from_reply_event()` 后 `event.get_extra("astrmai_group_wait")` 非空
    - `terminate()` 后 KV storage 含 `pending_private_sessions`
    - `on_program_start()` 后 KV storage 中过期条目被清理
    - KV storage 不可用时静默降级（不抛异常）
  - **Forbidden**: 不将完整 wait/session 状态持久化到 DB；不修改 wait/session TTL 逻辑；不新增 pip 依赖
  - **Check Commands**: `pytest tests/ -v -k "group_wait or private_chat"` ；手工：触发 wait → 检查 event extra → 重启 → 检查 KV storage
  - **Risk Notes**: 🟢 轻量级恢复，KV storage 不可用时静默降级
  - _Requirements: R7_

- [ ] 8. R8: `flush_message_counters` 迭代锁 — TOCTOU 防御
  - **Goal**: `flush_message_counters()` 遍历 `user_profiles` dict 时持锁，防止并发删除导致异常
  - **Files**:
    - ✏️ `astrmai/state/user_profile_service.py` — `__init__` + `flush_message_counters()` + 所有 dict 结构修改点
  - **Steps**:
    1. `user_profile_service.py`: `__init__` 新增 `self._profiles_dict_lock = asyncio.Lock()`
    2. `user_profile_service.py`: `flush_message_counters()` 的 list comprehension 包裹 `async with self._profiles_dict_lock:`
    3. `user_profile_service.py`: `flush_message_counters()` 的 `for` 循环中增加 `if profile is None: logger.debug(...); continue`
    4. `user_profile_service.py`: 搜索 `user_profiles[` 和 `del user_profiles` 的所有位置，每个 dict 结构变更点增加 `async with self._profiles_dict_lock:`
  - **Acceptance Criteria**:
    - 并发 `flush + del user_profiles[id]` → 无 RuntimeError
    - `flush` 中 `profile is None` → 跳过 + debug 日志
    - 所有 dict 结构修改点均已加锁
  - **Forbidden**: 不修改 `_save_profile()` 的 DB 写入逻辑；不修改 `_get_user_lock()` 的 per-user 锁逻辑
  - **Check Commands**: `pytest tests/ -v -k "user_profile_service or flush"` ； `python -c "from astrmai.state.user_profile_service import UserProfileService; print('import OK')"`
  - **Risk Notes**: 🟡 需确认所有 dict 修改点均已加锁（搜索覆盖）
  - _Requirements: R8_

---

### Phase 4: 最终验证

- [ ] 9. 全量回归验证
  - **Goal**: 确认 8 项修复未引入回归，全部现有测试通过
  - **Files**: 无新增文件
  - **Steps**:
    1. `pytest tests/ -v --tb=short` 确认 ≥ 70 passed
    2. 运行所有变更文件的 `lsp_diagnostics`
    3. 手工验证 R2：加载旧 DB → 确认 relationship_vector 迁移正确
  - **Acceptance Criteria**: ≥ 70 passed；0 新增 failure；lsp_diagnostics 0 error
  - **Forbidden**: 不跳过任何已有测试；不修改已有测试的断言
  - **Check Commands**: `pytest tests/ -v --tb=short 2>&1 | tail -5`
  - **Risk Notes**: 🟢 纯验证
  - _Requirements: R1–R8_

- [ ] 10. LSP 诊断清理 + 最终检查
  - **Goal**: 确认全部变更文件 LSP 诊断通过，提交前最终检查
  - **Files**: 所有变更文件（~6 个）
  - **Steps**:
    1. `lsp_diagnostics` 对每个变更文件
    2. `git diff --stat` 确认改动范围
    3. 确认 R2 中 `["relationship_vector"]` 搜索无遗漏引用
    4. 确认 R8 中所有 dict 修改点均已加锁
  - **Acceptance Criteria**: 0 lsp error；git diff 与 Summary 一致
  - **Forbidden**: 不在此任务中做代码修改
  - **Check Commands**: `lsp_diagnostics` × 6；`git diff --stat`
  - **Risk Notes**: 🟢 纯验证
  - _Requirements: ALL_

---

## Dependency Chain（依赖链）

```
Task 1 (R1 dirty-flag) ──► Task 2 (R2 vector) ──► Task 3 (R3 energy doc)
    │                            │
    └────────────────────────────┼──► Task 4 (R4 CAS)
                                 │
                                 ├──► Task 5 (R5 freq-ctrl)
                                 │
                                 └──► Task 6 (R6 lock-gap)
                                          │
                                          ├──► Task 7 (R7 session)
                                          │
                                          └──► Task 8 (R8 flush-lock)
                                                   │
                                                   ├──► Task 9 (regression)
                                                   │
                                                   └──► Task 10 (LSP)
```

| 严格串行原因 |
|---|
| Task 2 修改 `user_profile_service.py`，与 Task 1 修改的文件不同，但 R2 的 `update_social_score()` 调用链受 R1 持久化策略影响 → 建议 R1 先完成 |
| Task 4-6 各自修改独立文件/独立方法，可并行执行但因涉及状态一致性验证，建议串行 |
| Task 7-8 完全独立，可与 Task 4-6 并行 |

## Summary（变更汇总）

| # | 文件 | 改动 | 行数估计 |
|---|------|------|:------:|
| 1 | `astrmai/state/chat_state_service.py` | `_get_state_inner()` immediate-save + `should_drop_by_energy()` 去掉条件 | +3/-1 |
| 2 | `astrmai/state/chat_state_service.py` | `StateEngine.update_mood()` CAS 阈值 `0.001` → `0.0001` | +1/-1 |
| 3 | `astrmai/state/user_profile_service.py` | `update_social_score()` 删除双写 + 新增 `_migrate_relationship_vector()` | +12/-1 |
| 4 | `astrmai/state/user_profile_service.py` | `update_social_score()` 锁内加载 + `flush_message_counters()` 锁 + `_profiles_dict_lock` | +20/-5 |
| 5 | `astrmai/state/energy/energy_manager.py` | docstring 增强 + 配置缓存 | +8/-2 |
| 6 | `astrmai/state/energy/frequency_controller.py` | docstring + None 防御 | +6 |
| 7 | `astrmai/state/group_wait/group_reply_wait_manager.py` | `register_from_reply_event()` event extra | +8 |
| 8 | `astrmai/state/private_chat/private_chat_manager.py` | KV storage 持久化/清理 | +20 |
| 9 | `astrmai/conversation/attention/gate.py` | 确认 `should_reply()` 调用点（可能无需改动） | 0 or +3 |
| **Total** | **6–7 个文件** | **~+78 / -10 行** | |

## 执行检查清单

- [ ] Task 1–8 全部完成（代码改动）
- [ ] 全量测试 `pytest tests/ -v --tb=short` ≥ 70 passed
- [ ] 全部变更文件 `lsp_diagnostics` 0 error
- [ ] R1: daily reset 后 `save_chat_state()` 被调用
- [ ] R2: `meta["relationship_vector"]` 不再被写入
- [ ] R2: 旧 DB 加载后 relationship_vector 迁移正确
- [ ] R3: docstring 含量化公式
- [ ] R4: 并发 `update_mood(delta=0.0005)` → 两次 delta 反映
- [ ] R5: `should_reply(energy=None)` 不抛异常
- [ ] R6: 并发 `update_social_score()` → vector 正确叠加
- [ ] R7: event extra 含 wait 元数据
- [ ] R7: KV storage 写/读/清理正确
- [ ] R8: 并发 flush + del → 无异常
- [ ] `git diff --stat` 与 Summary 表一致
- [ ] `["relationship_vector"]` 搜索无遗漏外部引用

---

> **任务文档完成。** 全部 10 个任务 + Dependency Chain + Summary + 执行检查清单已写入。

