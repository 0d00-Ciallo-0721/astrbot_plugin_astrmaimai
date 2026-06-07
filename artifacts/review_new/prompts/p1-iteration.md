# P1 — 首迭代（~1.5h）

> 基于终审报告未修复项 | 4 项 | 🟡 中等

---

## #1 `proactive_task.py` — Profiling 信号量隔离

**文件：** `astrmai/proactive/proactive_task.py:455`
**问题：** `_run_profiling_task` 使用全局 `self._bg_semaphore`（容量 2），与 diary、dream、heartflow 共享。profiling 调用多次 LLM，持锁期间阻塞其他服务。
**修复：**

```python
# __init__ 中新增
self._profile_semaphore = asyncio.Semaphore(1)

# _run_profiling_task 中将
async with self._bg_semaphore:
# 改为
async with self._profile_semaphore:
```

**验证：** `python -m pytest tests/regression/proactive/ -q`

---

## #2 `context_engine.py` — DB session 移出循环

**文件：** `astrmai/conversation/planning/context_engine.py:529`
**问题：** `_resolve_visual_memory_refs` 的 `with self.db.get_session() as session:` 在 for 循环内，每个 picid 独立打开 session。
**修复：**

```python
# 将 with self.db.get_session() as session: 移到 for picid in set(picids): 之前
# 循环内复用同一个 session
with self.db.get_session() as session:
    for picid in set(picids):
        memory = session.get(VisualMemory, picid)
        ...
```

**验证：** `python -m pytest tests/test_cognitive_loop_refactor.py tests/test_conversation_continuity_refactor.py -q`

---

## #3 `prompt_refiner.py` — 同上

**文件：** `astrmai/conversation/planning/prompt_refiner.py:541`
**问题：** `_resolve_visual_memory` 存在与 #2 完全相同的问题。
**修复：** 同步修改——`with self.db.get_session() as session:` 移到循环外。

---

## #4 `model_router.py` — 清理误导注释

**文件：** `astrmai/infrastructure/gateway/model_router.py:168`
**问题：** 注释 `"如果模型在冷却中但成功返回，则提前解除冷却"` 对应逻辑未实现，是死文档。
**修复：** 删除该行注释，或补全冷却解除逻辑（设 `cooldown_until = 0`）。

**验证：** `python -m pytest tests/ -q --tb=line -k "not (admin_full_fixture)"`
