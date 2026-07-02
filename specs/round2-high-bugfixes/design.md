# Design Document — Round 2 HIGH Bugfixes

## Overview

13 个修复均为局部变更（1-5行），修改 10 个文件，~25 行。

| # | 文件 | 修复 |
|---|------|------|
| R1 | `config.py:122` | `ge=0` → `ge=1` |
| R2 | `dream_scheduler.py:59` | 节流检查移入信号量内 |
| R3 | `bootstrap.py:357` | 添加 `logger.warning` |
| R4 | `executor.py:211` | `time.time()` → `monotonic()` |
| R5 | `think_level_policy.py:202` | 移除 `len<=4` 规则 |
| R6 | `orm_models.py:22` + `database_review.py:88` | UNIQUE约束 + IntegrityError |
| R7 | `persistence_schema.py:251` | 调用 `_run_migrations` |
| R8 | `gateway_call.py:183` | 移除 `raise TimeoutError` |
| R9 | `gateway_policy.py:51` | `time.time()` → `monotonic()` |
| R10 | `bm25.py:108` | score_range 下限修正 |
| R11 | `visual_cortex.py:21` | `maxsize=100` |
| R12 | `gate.py:649` | 添加 `_deferred_messages` |
| R13 | `startup_hooks.py:13` | 添加 `raise` |

---

## Module Designs

### R1: `config.py:122`
```python
# BEFORE: dream_interval_min: int = Field(default=30, ge=0)
# AFTER:  dream_interval_min: int = Field(default=30, ge=1)
```

### R2: `dream_scheduler.py:59,79`
```python
# 将 should_run 检查移到 async with self._bg_semaphore: 代码块内
# 确保只有一个协程同时通过节流检查
```

### R3: `bootstrap.py:357-358`
```python
if runtime.attention_gate is not None:
    message_handler = runtime.attention_gate.process_event
else:
    logger.warning("[Bootstrap] attention_gate is None; ChatLoopKernel will have no message handler")
    message_handler = None
```

### R4: `executor.py:211`
```python
# BEFORE: if breaker_until > time.time():
# AFTER:  if breaker_until > monotonic():
```

### R5: `think_level_policy.py:202-203`
```python
# BEFORE: return len(compact_text) <= 4 or lowered in ThinkLevelPolicy.SHORT_ACKS
# AFTER:  return lowered in ThinkLevelPolicy.SHORT_ACKS
```

### R6: `orm_models.py` + `database_review.py`
```python
# orm_models.py: 添加 __table_args__ = (UniqueConstraint("group_id","situation","expression"),)
# database_review.py: try/except IntegrityError around session.add() + session.commit()
```

### R7: `persistence_schema.py:251`
```python
# _init_db() 末尾: await self._run_migrations()  # 更新 PRAGMA user_version
```

### R8: `gateway_call.py:183-185`
```python
# BEFORE: raise TimeoutError(...)  # ← 被外层 except Exception 捕获
# AFTER:  last_error = str(exc); is_fatal = False; continue  # 标记为可重试
```

### R9: `gateway_policy.py:51`
```python
# BEFORE: "until": time.time() + duration
# AFTER:  "until": monotonic() + duration
```

### R10: `bm25.py:108-112`
```python
# BEFORE: score_range = max_score - min_score if max_score != min_score else 1.0
# AFTER:  score_range = max_score - min_score if max_score != min_score else max(abs(max_score), 1.0)
```

### R11: `visual_cortex.py:21`
```python
# BEFORE: self.queue = asyncio.Queue()
# AFTER:  self.queue = asyncio.Queue(maxsize=100)
# submit_task: put_nowait → QueueFull 时 logger.warning
```

### R12: `gate.py:649`
```python
# PROACTIVE_BLOCKED 时: self._deferred_messages[chat_id].append(event)
# proactive 完成时: 重放延迟消息
```

### R13: `startup_hooks.py:13-16`
```python
# except Exception: logger.exception(...); raise  # ← 添加 raise
```

---

## Risk / Verification

| 风险 | 等级 | 缓解 |
|------|------|------|
| R6 UNIQUE 约束可能触发已有重复行 → migration 失败 | 🟡 | 添加前先 `DELETE` 重复行（保留最新） |
| R12 延迟消息队列可能内存泄漏 | 🟡 | maxsize=5，超出丢弃最旧 |

| 需求 | 验证 |
|------|------|
| R1–R13 | `lsp_diagnostics` + 源码检查 + `pytest tests/` |
