# Implementation Plan — AstrMai P1 Fix Round-2

> 本任务列表派生自同目录 `requirements.md` 与 `design.md`。  
> **执行原则**：任务**严格串行**，编号 1 → 21，后续任务依赖前一任务完成。  
> **状态规则**：所有任务初始状态为 `- [ ]` 未完成。

## Overview

本任务列表把 21 条需求与 21 个模块设计翻译为 21 个**严格串行**的可执行任务。

| Phase | 主题 | 任务 | 改动类型 |
|-------|------|------|---------|
| Phase 1 | Wave 1: 生命周期/资源泄漏 | Tasks 1-9 | 补丁 (patch) |
| Phase 2 | Wave 2: 数据一致性/竞态 | Tasks 10-14 | 补丁 (patch) |
| Phase 3 | Wave 3: 错误处理/LLM韧性 | Tasks 15-19 | 补丁 (patch) |
| Phase 4 | Wave 4: 事件流/Hook | Tasks 20-21 | 新增 (add) |
| Phase 5 | 最终验证 | Task 22 | 验证 |

## Tasks

### Phase 1: Wave 1 — 生命周期/资源泄漏修复 (Tasks 1-9)

- [ ] 1. R1: persona_summarizer.py — create_task 无 done_callback
  - **Goal**: 添加 `_handle_background_task_result` 回调防止后台异常静默丢失
  - **Files**: `astrmai/memory/persona/persona_summarizer.py` (write)
  - **Steps**:
    1. 在类中新增 `_handle_background_task_result(self, task)` 方法（参照 lifecycle.py:28-34）
    2. line 196 后加 `task.add_done_callback(self._handle_background_task_result)`
    3. line 272 后加 `task.add_done_callback(self._handle_background_task_result)`
  - **Acceptance Criteria**:
    - 方法包含 `self.pending_tasks.pop` 和 `logger.error` on exception
    - 两处 create_task 均有 done_callback
  - **Forbidden**: 不修改 `_generate_all_shards_background` 内部
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险，纯增量代码
  - _Requirements: R1

- [ ] 2. R2: v2_store.py — _session_locks 无界增长
  - **Goal**: 限制 `_session_locks` dict 上限为 200
  - **Files**: `astrmai/memory/services/v2_store.py` (write)
  - **Steps**:
    1. line 60: `dict[str, asyncio.Lock]` → `OrderedDict[str, asyncio.Lock]`，添加 `from collections import OrderedDict`
    2. line 74 (在 `self._session_locks[scope] = lock` 之后): 添加 `if len(self._session_locks) > 200: self._session_locks.popitem(last=False)`
  - **Acceptance Criteria**:
    - OrderedDict 正确导入
    - LRU 淘汰位于 `_session_locks_guard` 锁内
  - **Forbidden**: 不添加额外清理方法
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟡 中风险，误淘汰活跃 lock 时可重新创建
  - _Requirements: R2

- [ ] 3. R3: memory_engine.py — _cognitive_feedback_cache 无界增长
  - **Goal**: 限制 `_cognitive_feedback_cache` dict 上限为 100
  - **Files**: `astrmai/memory/services/memory_engine.py` (write)
  - **Steps**:
    1. 搜索 `_cognitive_feedback_cache[` 所有写入点 (grep)
    2. 每个写入点后添加 `if len(self._cognitive_feedback_cache) > 100: del self._cognitive_feedback_cache[next(iter(self._cognitive_feedback_cache))]`
  - **Acceptance Criteria**:
    - 所有写入点均有上限检查
  - **Forbidden**: 不修改内部 list 的 32-item 上限
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险
  - _Requirements: R3

- [ ] 4. R4: memory_turn_pipeline.py — 4 dict 无界增长
  - **Goal**: 在 `_sweep_loop` 中清理 30 分钟无活动的 chat 条目
  - **Files**: `astrmai/memory/services/memory_turn_pipeline.py` (write)
  - **Steps**:
    1. 读取 `_sweep_loop` 方法完整实现
    2. 在循环体中添加清理逻辑：检查 `_instant_llm_last_check` 获取最后活动时间
    3. 清理 `_session_history_buffer`、`_memory_locks`、`_worker_tasks`、`_worker_queues` 中过期 (>1800s) 条目
  - **Acceptance Criteria**:
    - 有活跃时间检查逻辑
    - 40 个 dict 的过期 key 被移除
  - **Forbidden**: 不修改 worker/queue 处理语义
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险，已有 sweep_loop 框架
  - _Requirements: R4

- [ ] 5. R5: reflector.py — _pending_reflections 无界增长
  - **Goal**: 限制 `_pending_reflections` list 最大长度为 200
  - **Files**: `astrmai/learning/review/reflector.py` (write)
  - **Steps**:
    1. 在 `record_usage` 的 `self._pending_reflections.append(...)` 后添加长度检查
    2. `if len(self._pending_reflections) > 200: self._pending_reflections = self._pending_reflections[-200:]; logger.warning(...)`
  - **Acceptance Criteria**:
    - list 长度永不超 200
  - **Forbidden**: 不修改 reflect_batch 逻辑
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险
  - _Requirements: R5

- [ ] 6. R6: event_bus.py — affection_changed 不重置
  - **Goal**: 在 trigger_affection_change 末尾清除 Event
  - **Files**: `astrmai/infrastructure/runtime/event_bus.py` (write)
  - **Steps**:
    1. line 66 `await self.publish(...)` 之后添加 `self.affection_changed.clear()`
  - **Acceptance Criteria**:
    - `.clear()` 在 `.publish()` 之后
  - **Forbidden**: 不修改 publish 逻辑
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险，纯增量
  - _Requirements: R6

- [ ] 7. R7: lane_manager.py — _lane_locks 无界增长
  - **Goal**: 限制 `_lane_locks` 上限为 100
  - **Files**: `astrmai/infrastructure/runtime/lane_manager.py` (write)
  - **Steps**:
    1. 将 `_lane_locks` 类型改为 `OrderedDict`
    2. 在 `_get_lane_lock` 的 guard 块中添加 `if len(self._lane_locks) > 100: self._lane_locks.popitem(last=False)`
  - **Acceptance Criteria**:
    - OrderedDict 正确导入
    - LRU 淘汰在 lock 内
  - **Forbidden**: 不修改锁语义
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险
  - _Requirements: R7

- [ ] 8. R8: chat_runtime_coordinator.py — _states 从未清理
  - **Goal**: 添加 `prune_inactive()` 方法并在 terminate 中调用
  - **Files**: `astrmai/infrastructure/runtime/chat_runtime_coordinator.py` (write), `astrmai/app/plugin_facade.py` (write)
  - **Steps**:
    1. 在 ChatRuntimeCoordinator 添加 `prune_inactive(self, max_idle_sec=1800)` 方法
    2. 遍历 `_states`，移除 `latest_activity_ts < now - max_idle_sec` 的条目
    3. 在 plugin_facade.py 的 `terminate()` 中添加 `await self.runtime.runtime_coordinator.prune_inactive()` (如果有) 或直接调用
  - **Acceptance Criteria**:
    - prune_inactive 方法存在且正确
  - **Forbidden**: 不添加定期调度器
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险
  - _Requirements: R8

- [ ] 9. R9: persistence_manager.py — dispose() 从未被调用
  - **Goal**: 在 terminate 链路中调用 dispose()
  - **Files**: `astrmai/app/plugin_facade.py` (write)
  - **Steps**:
    1. 在 `terminate()` 方法中添加 `self.runtime.persistence.dispose()`
  - **Acceptance Criteria**:
    - dispose() 在 terminate 中被调用
  - **Forbidden**: 不修改 dispose() 实现
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险
  - _Requirements: R9

### Phase 2: Wave 2 — 数据一致性/竞态修复 (Tasks 10-14)

- [ ] 10. R10: reflect_tracker.py — try_consume_feedback TOCTOU 竞态
  - **Goal**: 消除 candidates 读取和 pop 之间的 TOCTOU 窗口
  - **Files**: `astrmai/learning/review/reflect_tracker.py` (write)
  - **Steps**:
    1. 将 line 70-75 的 `async with self._lock:` 块合并：在锁内读取并 pop candidates
    2. 在锁内 `pop` 掉候选条目，LLM 调用移至锁外
    3. 移除 line 132 多余的 lock+pop
  - **Acceptance Criteria**:
    - LLM 调用和 DB 更新在 lock 外进行
    - candidate pop 在 lock 内原子完成
  - **Forbidden**: 不修改 `_parse_feedback` 方法
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟡 中风险，修改锁范围
  - _Requirements: R10

- [ ] 11. R11: reflect_tracker.py — get_unsent_requests 标记 ALL 为 sent
  - **Goal**: 仅标记返回给调用者的条目为 sent
  - **Files**: `astrmai/learning/review/reflect_tracker.py` (write)
  - **Steps**:
    1. 修改 line 56 的 `for item in self._pending.values():` → 仅遍历已筛选的 requests 对应条目
    2. 使用 pattern_id 反查标记
  - **Acceptance Criteria**:
    - 未被返回的条目保持 `sent=False`
  - **Forbidden**: 不改变返回类型
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟡 中风险，需确认 Plugin Pages API 消费者
  - _Requirements: R11

- [ ] 12. R12: database_service.py — get_chat_state 无锁读取
  - **Goal**: 启用 WAL 模式支持读写并发
  - **Files**: `astrmai/infrastructure/persistence/database_service.py` (write)
  - **Steps**:
    1. line 190 `with sqlite3.connect(...)` 后添加 `conn.execute("PRAGMA journal_mode=WAL")`
  - **Acceptance Criteria**:
    - PRAGMA 语句在 connect 后立即执行
  - **Forbidden**: 不添加 asyncio Lock (方法为 sync)
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险，WAL 从 sqlite 3.7.0 广泛支持
  - _Requirements: R12

- [ ] 13. R13: context_compaction.py — 压缩任务创建竞态
  - **Goal**: 用 lock 保护任务创建区域
  - **Files**: `astrmai/conversation/attention/context_compaction.py` (write)
  - **Steps**:
    1. 在类中查找是否有现成 `asyncio.Lock`（如 `_compaction_lock`）
    2. 如有则复用；如无则在 `__init__` 中添加 `self._task_creation_locks: dict[str, asyncio.Lock] = {}`
    3. 在 `schedule_compaction_evaluation` 的 check-create 区域用 lock 保护
  - **Acceptance Criteria**:
    - check 和 create 在同一个 lock 保护下
  - **Forbidden**: 不修改 _create_task 方法
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险
  - _Requirements: R13

- [ ] 14. R14: gate.py — Session worker 阻塞
  - **Goal**: sys2_process 异步化，不阻塞 session 循环
  - **Files**: `astrmai/conversation/attention/gate.py` (write)
  - **Steps**:
    1. line 840 `await self.sys2_process(...)` → `task = asyncio.create_task(self.sys2_process(...)); self._background_tasks.add(task); task.add_done_callback(...)`
    2. 在同一 lock 块内立即设置 `session.is_evaluating = False`
  - **Acceptance Criteria**:
    - sys2_process 不阻塞 session worker
    - task 被追踪防止 GC
  - **Forbidden**: 不修改 sys2_process 签名
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟡 中风险，需确保 task 被正确追踪
  - _Requirements: R14

### Phase 3: Wave 3 — 错误处理/LLM韧性 (Tasks 15-19)

- [ ] 15. R15: memory_retrieval_service.py — LLM 调用缺 lane_key
  - **Goal**: 向 `call_data_process_task` 传递 `lane_key` 参数
  - **Files**: `astrmai/memory/services/memory_retrieval_service.py` (write)
  - **Steps**:
    1. line 383 `gateway.call_data_process_task(prompt=prompt, is_json=True)` 添加 `lane_key` 参数
    2. 导入 `LaneKey` (检查是否已有 import)
    3. `lane_key=LaneKey(subsystem="bg", task_family="query_rewrite", scope_id="global")`
  - **Acceptance Criteria**:
    - call_data_process_task 调用包含 lane_key
  - **Forbidden**: 不修改 call_data_process_task 签名
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险，lane_key 为可选参数
  - _Requirements: R15

- [ ] 16. R16: hybrid_retriever.py — add_memory 返回 None
  - **Goal**: vector 离线时抛出异常而非返回 None
  - **Files**: `astrmai/memory/retrieval/hybrid_retriever.py` (write)
  - **Steps**:
    1. line 31 `return None` → `raise RuntimeError("Vector store offline, cannot add memory")`
  - **Acceptance Criteria**:
    - RuntimeError 替代 return None
  - **Forbidden**: 不修改 caller 代码
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟡 中风险，需确认调用者已处理异常
  - _Requirements: R16

- [ ] 17. R17: bootstrap.py — 闭包捕获 pre-binding
  - **Goal**: 审计确认闭包安全性
  - **Files**: `astrmai/app/bootstrap.py` (read-only audit)
  - **Steps**:
    1. 读取 `_build_system2_bridge` 方法 (lines 504-510)
    2. 确认每次调用 `_bridge` 时都会重新检查 `runtime.system2_callback is None`
    3. 确认 `bind_system2_callback` 在 bridge 构造后调用
  - **Acceptance Criteria**:
    - 审计确认当前实现正确，无需修改
    - 如有问题标记注释
  - **Forbidden**: 不添加新代码
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 审计任务，当前代码已有检查
  - _Requirements: R17

- [ ] 18. R18: lifecycle.py — track_task 无 RuntimeError guard
  - **Goal**: 替换 create_task 为 safe_create_task
  - **Files**: `astrmai/app/lifecycle.py` (write)
  - **Steps**:
    1. line 23 `task = asyncio.create_task(coro)` → `task = safe_create_task(coro)`
    2. 确认 `safe_create_task` 已在文件顶部导入
  - **Acceptance Criteria**:
    - 使用 safe_create_task 而非 asyncio.create_task
  - **Forbidden**: 不重写 safe_create_task
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险，safe_create_task 已在多处使用
  - _Requirements: R18

- [ ] 19. R19: plugin_facade.py — 异常仅捕获 LLMCascade
  - **Goal**: 添加 except Exception 兜底处理
  - **Files**: `astrmai/app/plugin_facade.py` (write)
  - **Steps**:
    1. line 505 `except LLMCascadeFailureException:` 后添加:
    ```python
    except Exception as e:
        logger.error(f"[AstrMai] System2 unexpected error for {chat_id}: {e}", exc_info=True)
        fallback = str(getattr(getattr(self.runtime.config, "reply", None), "fallback_text", "") or "（陷入了短暂的沉默...）")
        await self.runtime.reply_engine.handle_reply(main_event, fallback, chat_id)
    ```
  - **Acceptance Criteria**:
    - 非 LLMCascade 异常被捕获并发送 fallback
  - **Forbidden**: 不修改 LLMCascadeFailureException 处理
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险，纯增量兜底
  - _Requirements: R19

### Phase 4: Wave 4 — 事件流/Hook 修复 (Tasks 20-21)

- [ ] 20. R20: main.py — heartflow_is_command 未实现
  - **Goal**: 检查并跳过已被 HeartCore 标记为命令的消息
  - **Files**: `main.py` (write)
  - **Steps**:
    1. 在 `on_global_message` handler (line 135) 开头添加:
    ```python
    if event.get_extra("heartflow_is_command"):
        return
    ```
  - **Acceptance Criteria**:
    - heartflow_is_command 标记时 handler 不 yield
  - **Forbidden**: 不修改 facade.on_global_message
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟡 中风险，需确认 AstrBot HeartCore 标记语义
  - _Requirements: R20

- [ ] 21. R21: main.py — 缺 on_llm_response hook
  - **Goal**: 注册 LLM 响应后监控钩子
  - **Files**: `main.py` (write)
  - **Steps**:
    1. 在 `main.py` 中添加导入: `from astrbot.api.provider import LLMResponse` (如未导入)
    2. 在类中添加:
    ```python
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        try:
            chat_id = event.unified_msg_origin
            text_preview = str(response.completion_text or "")[:200]
            logger.debug(f"[AstrMai] LLM response for {chat_id}: {text_preview}")
        except Exception:
            pass
    ```
  - **Acceptance Criteria**:
    - on_llm_response handler 已注册
    - 全逻辑在 try/except 中
  - **Forbidden**: 不在 hook 中 yield 消息
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险，纯增量
  - _Requirements: R21

### Phase 5: 最终验证 (Task 22)

- [ ] 22. 全量回归验证
  - **Goal**: 确认全部修改无回归
  - **Files**: 全部变更文件 (verify)
  - **Steps**:
    1. 执行全量测试: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
    2. 执行导入检查: `python -c "import astrmai; print('OK')"`
    3. 检查 `git diff --stat` 确认仅修改目标文件
    4. 确认 diff 总行数 ≤ 100 行
  - **Acceptance Criteria**:
    - 全量测试 passing
    - import 无异常
    - 仅目标文件被修改
  - **Forbidden**: 不跳过任何测试检查
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 验证阶段
  - _Requirements: R1–R21

---

## Dependency Chain (依赖链)

```
Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8 → Task 9 →
Task 10 → Task 11 → Task 12 → Task 13 → Task 14 →
Task 15 → Task 16 → Task 17 → Task 18 → Task 19 →
Task 20 → Task 21 →
Task 22 (最终验证)
```

21 个修复任务严格串行执行，确保每次修改后立即验证。

## Summary (变更汇总)

| # | 文件 | 改动 | 行数估计 |
|---|------|------|:------:|
| 1 | persona_summarizer.py | 添加 done_callback 方法 + 2 处回调 | +8 |
| 2 | v2_store.py | dict→OrderedDict + LRU 淘汰 | +4/−1 |
| 3 | memory_engine.py | _cognitive_feedback_cache 上限 | +3 |
| 4 | memory_turn_pipeline.py | _sweep_loop 清理逻辑 | +10 |
| 5 | reflector.py | _pending_reflections 上限 | +3 |
| 6 | event_bus.py | affection_changed.clear() | +1 |
| 7 | lane_manager.py | _lane_locks LRU 淘汰 | +3 |
| 8 | chat_runtime_coordinator.py | prune_inactive 方法 | +8 |
| 9 | plugin_facade.py | dispose() 调用 + prune_inactive + except兜底 | +6 |
| 10 | reflect_tracker.py | TOCTOU 修复 | +10/−8 |
| 11 | reflect_tracker.py | get_unsent sent 标记 | +3/−2 |
| 12 | database_service.py | PRAGMA WAL | +1 |
| 13 | context_compaction.py | 任务创建 lock | +2 |
| 14 | gate.py | sys2_process 异步化 | +3/−1 |
| 15 | memory_retrieval_service.py | lane_key 参数 | +1 |
| 16 | hybrid_retriever.py | raise RuntimeError | +1/−1 |
| 17 | bootstrap.py | 审计确认 (无修改) | 0 |
| 18 | lifecycle.py | safe_create_task | +1/−1 |
| 19 | (included in #9) | — | — |
| 20 | main.py | heartflow_is_command 检查 | +2 |
| 21 | main.py | on_llm_response hook | +10 |
| **Total** | **16-17 个文件** | | **~+80/−15** |

## 执行检查清单

- [ ] 全量测试 ≥ 现有 passed 数量 (无回归)
- [ ] `import astrmai` 成功
- [ ] 所有变更文件 lsp_diagnostics 无 error
- [ ] git diff 仅含目标文件
- [ ] 总 diff ≤ 100 行
- [ ] round2-summary.md 已生成