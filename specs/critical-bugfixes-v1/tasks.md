# Implementation Plan

> 本任务列表派生自同目录 `requirements.md` 与 `design.md`。
> **执行原则**：任务**严格串行**，编号 1 → N，后续任务依赖前一任务完成。
> **状态规则**：所有任务初始状态为 `- [ ]` 未完成。

## Overview

本任务列表把 5 条 CRITICAL 需求与 5 个模块设计翻译为 **9 个严格串行**的可执行任务，组织为 3 个 Phase。

| Phase | 主题 | 任务 | 改动文件 |
|-------|------|------|---------|
| Phase 1 | 最简单单文件修复 | Task 1–3 | `gateway_call.py`, `database_review.py`, `group_reply_wait_manager.py` + `private_chat_manager.py` |
| Phase 2 | 架构级修复 | Task 4–5 | `chat_runtime_coordinator.py`, `router.py` |
| Phase 3 | 验证与清理 | Task 6–9 | 全部变更文件 + `tests/` |

---

## Tasks

### Phase 1: 简单单文件修复（R3, R2, R4）

- [ ] **1. R3: 修复 `raw_completion_text` NameError**
  - **Goal**: 在 `gateway_call.py` 的内层 `try` 块之前初始化 `raw_completion_text = ""`
  - **Files**: 
    - ✏️ `astrmai/infrastructure/gateway/gateway_call.py` (L205 后插入 1 行)
  - **Steps**:
    1. 打开 `gateway_call.py`，定位到 L205（外层 `except Exception` 块的 `continue` 之后）
    2. 在 L205 和 `try:` (L206) 之间插入 `raw_completion_text = ""`
    3. 确认插入后 L206-309 的 `try/except` 块内所有使用 `raw_completion_text` 的位置（L209, L309, L313）不再有 `NameError` 风险
  - **Acceptance Criteria**:
    - `raw_completion_text = ""` 位于 L206 的 `try:` 语句之前
    - `lsp_diagnostics` 对 `gateway_call.py` 无新增 error
  - **Forbidden**: 不重构嵌套 try/except 结构；不修改 `_open_model_cooldown()` 调用
  - **Check Commands**: `lsp_diagnostics("astrmai/infrastructure/gateway/gateway_call.py")`
  - **Risk Notes**: 🟢 单行修复，零风险
  - _Requirements: R3_

- [ ] **2. R2: 为 `save_pattern` 的 fire-and-forget 任务附加错误回调**
  - **Goal**: 为 `database_review.py:77` 的 `asyncio.create_task` 附加 `add_done_callback` 以记录任务失败
  - **Files**: 
    - ✏️ `astrmai/infrastructure/persistence/database_review.py` (L77 附近)
  - **Steps**:
    1. 打开 `database_review.py`，定位 `save_pattern()` 方法中的 L75-79
    2. 将 L77 的 `asyncio.create_task(self._save_pattern_to_canonical_async(pattern))` 改为赋值给局部变量 `task`
    3. 在 L77 之后添加 `task.add_done_callback(lambda t, p=pattern: logger.exception(f"[DatabaseReview] canonical save failed for pattern {getattr(p, 'id', '?')}") if t.exception() else None)`
    4. 确认 import 区域有 `from astrbot.api import logger`（或等价 import）
  - **Acceptance Criteria**:
    - L77 的 `create_task` 返回值被赋给变量 `task`
    - `task.add_done_callback(...)` 调用存在，lambda 内使用 `t.exception()` 检查并调用 `logger.exception()`
  - **Forbidden**: 不将 `save_pattern()` 改为异步方法；不修改 `_save_pattern_to_canonical_async()` 本身
  - **Check Commands**: `lsp_diagnostics("astrmai/infrastructure/persistence/database_review.py")`
  - **Risk Notes**: 🟢 仅添加回调，不影响主流程
  - _Requirements: R2_

- [ ] **3. R4: 统一群聊等待和私聊管理的时钟源**
  - **Goal**: 将 `group_reply_wait_manager.py` 和 `private_chat_manager.py` 中与 `monotonic()` 比较的 `time.time()` 调用替换为 `monotonic()`
  - **Files**: 
    - ✏️ `astrmai/state/group_wait/group_reply_wait_manager.py` (L171)
    - ✏️ `astrmai/state/private_chat/private_chat_manager.py` (L144, L171)
  - **Steps**:
    1. 打开 `group_reply_wait_manager.py`，确认顶部已有 `from time import monotonic`（L140 已使用，应已存在）
    2. 将 L171 `now = time.time()` 改为 `now = monotonic()`
    3. 若顶部有 `import time` 且无其他用途，移除该 import
    4. 打开 `private_chat_manager.py`，确认顶部已有 `from time import monotonic`
    5. 将 L144 `"silence_sec": time.time() - session.last_message_time` 改为 `"silence_sec": monotonic() - session.last_message_time`
    6. 将 L171 `now = time.time()` 改为 `now = monotonic()`
    7. 若顶部有 `import time` 且无其他用途，移除该 import
  - **Acceptance Criteria**:
    - `group_reply_wait_manager.py:171` 使用 `monotonic()` 而非 `time.time()`
    - `private_chat_manager.py:144` 使用 `monotonic()` 而非 `time.time()`
    - `private_chat_manager.py:171` 使用 `monotonic()` 而非 `time.time()`
  - **Forbidden**: 不修改其他模块中对 `time.time()` 的正确使用
  - **Check Commands**: `lsp_diagnostics("astrmai/state/group_wait/group_reply_wait_manager.py")`, `lsp_diagnostics("astrmai/state/private_chat/private_chat_manager.py")`
  - **Risk Notes**: 🟡 需确认两个文件均已导入 `monotonic`（L140/L89 使用了 `monotonic()` 表明已导入）
  - _Requirements: R4_

---

### Phase 2: 架构级修复（R5, R1）

- [ ] **4. R5: 修复 `executor_lock` 取消泄漏**
  - **Goal**: 在 `try_acquire_executor()` 的 `await executor_lock.acquire()` 处添加 `CancelledError` 处理以递减 `executor_pending`
  - **Files**: 
    - ✏️ `astrmai/infrastructure/runtime/chat_runtime_coordinator.py` (L48 附近)
  - **Steps**:
    1. 打开 `chat_runtime_coordinator.py`，定位 `try_acquire_executor()` 方法 (L41-49)
    2. 将 L48 `await executor_lock.acquire()` 包裹在 `try/except asyncio.CancelledError` 中
    3. 在 `except asyncio.CancelledError` 块中：
       - 获取 `async with self._lock:`
       - 检查 `chat_id in self._states`
       - 递减 `self._states[chat_id].executor_pending = max(0, self._states[chat_id].executor_pending - 1)`
       - 释放锁后 `raise` 以传播 `CancelledError`
    4. 确认 `asyncio` 已在文件顶部导入
  - **Acceptance Criteria**:
    - L48 的 `await executor_lock.acquire()` 被 `try/except CancelledError` 包裹
    - `except` 块中正确递减 `executor_pending` 并 `raise`
    - `release_executor()` 方法保持原样不变
  - **Forbidden**: 不修改 `executor.py` 中的调用方；不修改 `ChatRuntimeState` 数据结构
  - **Check Commands**: `lsp_diagnostics("astrmai/infrastructure/runtime/chat_runtime_coordinator.py")`
  - **Risk Notes**: 🔴 取消路径的正确性依赖于 `CancelledError` 不被上游 `except Exception` 吞掉。需确保 `raise` 存在
  - _Requirements: R5_

- [ ] **5. R1: 修复 Sys3 Planner TOOL_CALL 模式下 SubAgent 轻量工具集崩溃**
  - **Goal**: 在 `Sys3Router` 中维护 `_raw_agent_map`，在 `get_light_tools_for_planner()` 返回的轻量工具上注入真实 `handler` 引用
  - **Files**: 
    - ✏️ `astrmai/workmode/router.py` (L14, L30, L35)
  - **Steps**:
    1. 打开 `router.py`，在 `__init__()` 方法末尾（L25 之后）添加 `self._raw_agent_map: dict[str, object] = {}`
    2. 在 `get_all_agents()` 方法的 `return` 语句之前（L31 之前），插入构建 `_raw_agent_map` 的逻辑：
       ```python
       self._raw_agent_map = {getattr(a, "name", ""): a for a in agents if getattr(a, "name", "")}
       ```
    3. 修改 `get_light_tools_for_planner()` 方法（L33-35）：
       - 保留 `full_set.get_light_tool_set()` 调用
       - 在 `return light_set` 之前，遍历 `light_set.tools`，对每个 light_tool 查找 `_raw_agent_map` 中的真实 agent，若找到则将 `light_tool.handler = raw_agent.call`
    4. 验证 `planner_side_inputs.py` 中 `_build_execution_tools()` 无需修改
  - **Acceptance Criteria**:
    - `Sys3Router.__init__` 中存在 `self._raw_agent_map` 初始化
    - `get_all_agents()` 返回前更新了 `_raw_agent_map`
    - `get_light_tools_for_planner()` 返回的工具中，每个 SubAgent 对应的轻量工具 `handler` 不为 None
    - `get_full_tools_for_direct_entry()` 行为不变（返回全量 SubAgent 实例）
  - **Forbidden**: 不修改 AstrBot 核心库；不修改 `planner_side_inputs.py`
  - **Check Commands**: `lsp_diagnostics("astrmai/workmode/router.py")`
  - **Risk Notes**: 🔴 这是 5 条修复中复杂度最高的。handler 注入依赖 AstrBot `_handle_function_tools` 中 `if func_tool.handler:` 的检查逻辑（详见设计文档 RSK1）
  - _Requirements: R1_

---

### Phase 3: 验证与清理

- [ ] **6. LSP 诊断全量检查**
  - **Goal**: 对所有变更文件运行 `lsp_diagnostics`，确保无新增 error
  - **Files**: 全部 6 个变更文件
  - **Steps**:
    1. 对每个变更文件运行 `lsp_diagnostics`
    2. 若有 error，检查是否为预存在的（与本次修改无关）
    3. 若有新增 error，修复后重新验证
  - **Acceptance Criteria**:
    - 全部 6 个变更文件的 `lsp_diagnostics` 无 error（或仅预存在的 error）
  - **Forbidden**: 不修复预存在的 lint 问题
  - **Check Commands**: 
    ```
    lsp_diagnostics("astrmai/workmode/router.py")
    lsp_diagnostics("astrmai/infrastructure/persistence/database_review.py")
    lsp_diagnostics("astrmai/infrastructure/gateway/gateway_call.py")
    lsp_diagnostics("astrmai/state/group_wait/group_reply_wait_manager.py")
    lsp_diagnostics("astrmai/state/private_chat/private_chat_manager.py")
    lsp_diagnostics("astrmai/infrastructure/runtime/chat_runtime_coordinator.py")
    ```
  - **Risk Notes**: 🟢 纯验证
  - _Requirements: R1–R5_

- [ ] **7. R1 专项验证：Sys3 TOOL_CALL 路径**
  - **Goal**: 验证 `get_light_tools_for_planner()` 返回的轻量工具具有可用的 handler
  - **Files**: 📖 `router.py`, `planner_side_inputs.py`
  - **Steps**:
    1. 编写最小验证脚本（或使用 Python REPL）：导入 `Sys3Router`，mock config/context/db_service
    2. 调用 `await router.get_light_tools_for_planner()`
    3. 遍历返回的 `ToolSet.tools`，检查每个在 `_raw_agent_map` 中存在的工具的 `handler is not None`
    4. 确认 `transfer_to_cron`、`transfer_to_computer` 的 handler 已设置
  - **Acceptance Criteria**:
    - 所有 SubAgent 轻量工具 `handler is not None`
  - **Forbidden**: —
  - **Check Commands**: 编写内联验证函数或 pytest
  - **Risk Notes**: 🟡 需要 mock AstrBot Context 和 plugin_config
  - _Requirements: R1_

- [ ] **8. R4 专项验证：时钟源一致性**
  - **Goal**: 验证修复后的群聊等待不会立即过期
  - **Files**: 📖 `group_reply_wait_manager.py`, `private_chat_manager.py`
  - **Steps**:
    1. 编写 Python 验证脚本：构造 MockEvent，设置 `unified_msg_origin` 和 `get_sender_id`
    2. 调用 `manager.register_from_reply_event(event, target_user_id="test_user")`
    3. 立即调用 `manager.handle_incoming_message(new_event)` 验证不返回 `EXPIRED_TIMEOUT`
    4. sleep 超过 timeout 后再次调用，验证返回 `EXPIRED_TIMEOUT`
    5. 对 `private_chat_manager`：调用 `signal_new_message` 后立即调用 `cleanup_stale_sessions(max_silence_min=0.001)`，验证会话未被清理
  - **Acceptance Criteria**:
    - 等待状态不会在创建后立即过期
    - 超时后正确过期
  - **Forbidden**: —
  - **Check Commands**: Python 内联验证脚本
  - **Risk Notes**: 🟡 依赖 `asyncio.sleep` 的精确性
  - _Requirements: R4_

- [ ] **9. 全量回归测试**
  - **Goal**: 运行现有测试套件，确认 5 条修复未破坏现有功能
  - **Files**: 📖 `tests/` 目录
  - **Steps**:
    1. 运行 `pytest tests/ -v --tb=short`
    2. 检查测试结果：通过的测试数量，失败/跳过的测试
    3. 若有新增失败（与当前 main 分支对比），分析是否与本次修改相关
    4. 若无关，记录为预存在失败
  - **Acceptance Criteria**:
    - 测试通过数量不减少（与修复前对比）
  - **Forbidden**: 不删除失败的测试；不修改测试代码
  - **Check Commands**: `pytest tests/ -v --tb=short`
  - **Risk Notes**: 🟡 可能发现预存在的失败
  - _Requirements: R1–R5_

---

## Dependency Chain（依赖链）

```
Task 1 (R3: gateway_call.py)
  → Task 2 (R2: database_review.py)
    → Task 3 (R4: clock sources)
      → Task 4 (R5: executor_lock)
        → Task 5 (R1: Sys3 light tools)
          → Task 6 (LSP diagnostics)
            → Task 7 (R1 verify)
              → Task 8 (R4 verify)
                → Task 9 (full regression)
```

> Task 1-5 虽然修改不同文件（无代码级依赖），但按从简到繁排列可确保每步验证后逐步积累信心。

## Summary（变更汇总）

| # | 文件 | 改动 | 行数估计 |
|---|------|------|:------:|
| 1 | `astrmai/infrastructure/gateway/gateway_call.py` | 变量预初始化 | +1 |
| 2 | `astrmai/infrastructure/persistence/database_review.py` | `create_task` + `add_done_callback` | +4/-1 |
| 3 | `astrmai/state/group_wait/group_reply_wait_manager.py` | `time.time()` → `monotonic()` | +1/-1 |
| 4 | `astrmai/state/private_chat/private_chat_manager.py` | `time.time()` → `monotonic()` ×2 | +2/-2 |
| 5 | `astrmai/infrastructure/runtime/chat_runtime_coordinator.py` | `CancelledError` handler | +6/-1 |
| 6 | `astrmai/workmode/router.py` | `_raw_agent_map` + handler 注入 | +7 |
| **Total** | **6 个文件** | | **~20 行** |

## 执行检查清单

- [ ] Task 1–5: 全部代码修改完成
- [ ] Task 6: 全部变更文件 `lsp_diagnostics` 无 error
- [ ] Task 7: R1 验证通过（handler 不为 None）
- [ ] Task 8: R4 验证通过（等待状态正常工作）
- [ ] Task 9: `pytest` 全量回归，测试通过数不减少
