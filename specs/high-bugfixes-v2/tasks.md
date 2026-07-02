# Implementation Plan

> 本任务列表派生自同目录 `requirements.md` 与 `design.md`。
> **执行原则**：任务**严格串行**，编号 1 → N。

## Overview

| Phase | 主题 | 任务 | 改动文件 |
|-------|------|------|---------|
| Phase 1 | 防御性修复 | Task 1–3 | `main.py`(hooks), `gate.py`, `bootstrap.py`, `group_dialogue_store.py` |
| Phase 2 | EventBus 三连 | Task 4–6 | `event_bus.py` |
| Phase 3 | 核心链路 | Task 7–8 | `main.py`(on_llm_request), `message_entry.py` |
| Phase 4 | 验证 | Task 9 | 全部变更文件 |

---

## Tasks

### Phase 1: 防御性修复（R14, R13, R12, R11）

- [ ] **1. R14: `main.py` 钩子 `except Exception: pass` → `logger.debug`**
  - **Goal**: 三处完全静默的 `pass` 替换为 `logger.debug(exc_info=True)`
  - **Files**: ✏️ `main.py` (L133-134, L141-142, L148-149)
  - **Steps**:
    1. L133 `except Exception: pass` → `except Exception: logger.debug("[AstrMai] on_llm_response hook failed", exc_info=True)`
    2. L141 `except Exception: pass` → `except Exception: logger.debug("[AstrMai] on_agent_begin hook failed", exc_info=True)`
    3. L148 `except Exception: pass` → `except Exception: logger.debug("[AstrMai] on_agent_done hook failed", exc_info=True)`
  - **AC**: 三处无 `pass`，有 `logger.debug`
  - **Forbidden**: 不修改 try 块的逻辑
  - **Check**: `lsp_diagnostics("main.py")`
  - **Risk**: 🟢
  - _Requirements: R14_

- [ ] **2. R13 + R12: `gate.py` 传感器 + `bootstrap.py` 日志增强**
  - **Goal**: gate.py 用 `logger.exception()` 替代 warning+pass；bootstrap.py 添加明确禁用日志
  - **Files**: ✏️ `gate.py` (L540-548), ✏️ `bootstrap.py` (L483-485)
  - **Steps**:
    1. gate.py L540-542: `except Exception:` 内改为 `logger.exception(f"[AttentionGate] sensor is_command check failed on msg={msg_str[:100]!r}")`
    2. gate.py L546-548: `except Exception:` 内改为 `logger.exception("[AttentionGate] sensor should_process_message check failed, defaulting to pass")`
    3. bootstrap.py L483-485: 在 `_record_optional_failure` 后添加 `logger.warning(f"[Bootstrap] ProactiveTask creation failed: {type(exc).__name__}: {exc}")` 和 `logger.warning("[Bootstrap] 主动发言、梦境整理等功能将不可用")`
  - **AC**: gate.py 两处使用 `logger.exception()`；bootstrap.py 有新增 warning 日志
  - **Forbidden**: 不修改 fail-open/fail-closed 行为
  - **Check**: `lsp_diagnostics("gate.py")`, `lsp_diagnostics("bootstrap.py")`
  - **Risk**: 🟢
  - _Requirements: R12, R13_

- [ ] **3. R11: `group_dialogue_store.py` `str(chat_id or "")` sentinel 防护**
  - **Goal**: 添加 `_resolve_chat_key()` 方法，对所有 `str(chat_id or "")` 模式加防护
  - **Files**: ✏️ `group_dialogue_store.py`
  - **Steps**:
    1. 添加静态方法 `_resolve_chat_key(chat_id)`：若 `str(chat_id or "").strip()` 为空则 `raise ValueError`
    2. 替换 L126, L134, L139（及其他 `str(chat_id or "")` 出现处）为 `self._resolve_chat_key(chat_id)`
    3. 验证所有替换点编译通过
  - **AC**: `_resolve_chat_key` 存在；所有 `str(chat_id or "")` 被替换；空值抛 ValueError
  - **Forbidden**: 不修改调用方的 chat_id 保证逻辑
  - **Check**: `lsp_diagnostics("group_dialogue_store.py")`
  - **Risk**: 🟡 上游需保证 chat_id 非空
  - _Requirements: R11_

---

### Phase 2: EventBus 三连（R7, R8, R9）

- [ ] **4. R7 + R8: 任务追踪 + 健康检查修复**
  - **Goal**: 未追踪任务加入 `_background_tasks`；新增 `_worker_tasks` 专门追踪 worker
  - **Files**: ✏️ `event_bus.py`
  - **Steps**:
    1. 在 `_init_bus()` 中添加 `self._worker_tasks: set = set()`
    2. L68-69: `t = safe_create_task(...)`, `.add(t)` 到 `_background_tasks`
    3. L152-153: 创建 worker 时同时加入 `_worker_tasks`，done_callback 中从 `_worker_tasks` 移除
    4. L178: `active = sum(1 for t in list(self._worker_tasks) if not t.done())`
    5. L203: 健康检查任务也加入 `_background_tasks`
    6. `stop()`: 清理 `_worker_tasks`
  - **AC**: `_worker_tasks` 存在；L69 和 L203 任务被追踪；健康检查使用 `_worker_tasks`
  - **Forbidden**: 不破坏分发任务的 done_callback 逻辑
  - **Check**: `lsp_diagnostics("event_bus.py")`
  - **Risk**: 🟡 需确保 done_callback 正确清理 `_worker_tasks`
  - _Requirements: R7, R8_

- [ ] **5. R9: QueueFull 丢弃日志增强**
  - **Goal**: 每次丢弃记录 warning（含 topic 和 qsize），暴露 `_dropped_count`
  - **Files**: ✏️ `event_bus.py` (L207-212)
  - **Steps**:
    1. L208-212: 替换为每次丢弃 `logger.warning(f"[EventBus] queue full (size={...}), dropped topic={topic}, total_dropped={...}")`
    2. 添加 `get_dropped_count()` 方法
  - **AC**: 丢弃日志含 topic 和 qsize；`get_dropped_count()` 存在
  - **Forbidden**: 不添加背压/重试机制
  - **Check**: `lsp_diagnostics("event_bus.py")`
  - **Risk**: 🟢
  - _Requirements: R9_

---

### Phase 3: 核心链路（R6, R10）

- [ ] **6. R6: `on_llm_request` 条件保护 system_prompt 赋值**
  - **Goal**: 仅在 gemini-reverse provider 且 block 不存在时才修改 system_prompt
  - **Files**: ✏️ `main.py` (L88-105)
  - **Steps**:
    1. 提取 `sp = getattr(request, "system_prompt", "") or ""`
    2. 添加 `needs_reverse_block = provider is not None and "astrbot_reverse_session" not in sp`
    3. `if needs_reverse_block:` 包裹原赋值和 trace 哈希计算
    4. 非 gemini 路径跳过修改，trace hash 基于原始 system_prompt
  - **AC**: 非 gemini 时 `request.system_prompt` 不被修改；gemini 且 block 存在时不被重新赋值
  - **Forbidden**: 不改变 trace 哈希计算语义
  - **Check**: `lsp_diagnostics("main.py")`
  - **Risk**: 🔴 Gemini 反向代理需验证
  - _Requirements: R6_

- [ ] **7. R10: `message_entry.py` 错误时发送 fallback**
  - **Goal**: `status == "error"` 时向用户发送错误提示
  - **Files**: ✏️ `message_entry.py` (L93-97)
  - **Steps**:
    1. 在 except 块后添加 `if status == "error": yield event.plain_result(fallback_text); return`
    2. fallback_text 取自 config 或默认值
  - **AC**: status=="error" 时用户收到提示，不进入后续 ghost sentinel 逻辑
  - **Forbidden**: 不修改正常路径
  - **Check**: `lsp_diagnostics("message_entry.py")`
  - **Risk**: 🟡
  - _Requirements: R10_

---

### Phase 4: 验证

- [ ] **8. LSP 诊断 + 回归测试**
  - **Goal**: 所有变更文件 LSP clean + pytest 通过
  - **Steps**:
    1. `lsp_diagnostics` 所有 6 个变更文件
    2. `pytest tests/ -v --tb=short` 全量回归
  - **AC**: 无新增 LSP error；测试通过数不减少
  - **Check**: `lsp_diagnostics(...)`, `pytest tests/`
  - **Risk**: 🟢
  - _Requirements: R6–R14_

---

## Dependency Chain

```
Task 1 (R14: hooks) → Task 2 (R12+R13) → Task 3 (R11) → Task 4 (R7+R8) → Task 5 (R9) → Task 6 (R6) → Task 7 (R10) → Task 8 (verify)
```

## Summary

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| 1 | `main.py` | R6 条件赋值 + R14 debug logging | +10/-4 |
| 2 | `event_bus.py` | R7+R8+R9 | +12/-6 |
| 3 | `message_entry.py` | R10 error fallback | +4 |
| 4 | `group_dialogue_store.py` | R11 sentinel key | +8/-2 |
| 5 | `bootstrap.py` | R12 warnings | +2 |
| 6 | `gate.py` | R13 logger.exception | +2/-4 |
| **Total** | **6 个文件** | | **~38 行** |

## 执行检查清单

- [ ] Task 1–7: 全部代码修改完成
- [ ] Task 8: 全部变更文件 LSP 无新增 error
- [ ] Task 8: pytest 全量回归通过数不减少
