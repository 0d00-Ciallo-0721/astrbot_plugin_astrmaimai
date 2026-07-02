# Design Document

## 1. Overview

7 个 MEDIUM 缺陷均为局部修复，每个 1–5 行变更。所有修复可并行执行。

| # | 模块 | 改动 |
|---|------|------|
| R15 | `plugin_helpers.py:36` | `ensure_future` → `create_task` + 守卫 `get_name` |
| R16 | `memory_write_service.py:100-103` | 投影失败添加 `logger.warning` |
| R17 | `v2_store.py:75-77` | pop 前检查 `lock.locked()` |
| R18 | `handoff_registry.py:15-18` | 移除 `_loaded` 一次性缓存 |
| R19 | `lifecycle.py:173-174` | `pass` → `logger.warning` |
| R20 | `user_profile_service.py:254-274` | 锁内重读 profile |
| R21 | `memory_scoring.py:113` | `max(0, access_count)` |

## 2. Module Designs

### R15: `safe_create_task` → `create_task`

```python
# BEFORE (plugin_helpers.py:36)
task = asyncio.ensure_future(coro) if not isinstance(coro, asyncio.Task) else coro

# AFTER
task = asyncio.create_task(coro) if not isinstance(coro, asyncio.Task) else coro
# Also guard get_name:
name = name or (t.get_name() if hasattr(t, 'get_name') else '')
```

### R16: 投影失败 warning

```python
# AFTER (memory_write_service.py:102-103)
if self.index_projector and memory_id and not new_record_is_superseded:
    try:
        await self.index_projector.project(memory_id=memory_id, request=normalized)
    except Exception as exc:
        logger.warning(f"[MemoryWrite] index projection failed for {memory_id}: {exc}")
```

### R17: LRU 驱逐跳过活跃锁

```python
# AFTER (v2_store.py:75-77)
if len(self._session_locks) > 200:
    oldest = next(iter(self._session_locks))
    # ponytail: skip locks held by active coroutines
    if not self._session_locks[oldest].locked():
        self._session_locks.pop(oldest, None)
```

### R18: HandoffRegistry TTL 刷新

```python
# AFTER (handoff_registry.py:15-18)
async def discover(self, static_names: set[str]) -> list[Any]:
    # ponytail: re-scan every call to pick up newly registered SubAgents (R18)
    orchestrator = self._find_orchestrator()
    existing_names = {getattr(a, "name", "") for a in self._dynamic_agents}
    if not orchestrator or not hasattr(orchestrator, "handoffs"):
        return list(self._dynamic_agents)
    for handoff in getattr(orchestrator, "handoffs", []) or []:
        agent_name = getattr(handoff, "name", "")
        if not agent_name or agent_name in static_names or agent_name in existing_names:
            continue
        if not getattr(handoff, "active", True):
            continue
        self._dynamic_agents.append(handoff)
        existing_names.add(agent_name)
    return list(self._dynamic_agents)
```

### R19: shutdown flush 日志

```python
# AFTER (lifecycle.py:173-174)
except Exception:
    logger.warning("[AstrMai] shutdown flush failed", exc_info=True)
```

### R20: TOCTOU 锁内重读

```python
# AFTER (user_profile_service.py:254-255)
profile = await self.get_user_profile(user_id)
async with self._get_user_lock(user_id):
    profile = await self._get_profile_inner(user_id)  # re-read under lock
    # ... rest unchanged
```

### R21: access_count 守卫

```python
# AFTER (memory_scoring.py:113)
return beta * freshness + (1.0 - beta) * math.log(max(0.0, float(candidate.access_count or 0)) + 1.0)
```

## 3. Risk Assessment / Verification

| 风险 | 等级 | 缓解 |
|------|------|------|
| R18 每次调用都扫描 orchestrator 可能有性能影响 | 🟢 | orchestrator.handoffs 数量通常 < 10 |
| R17 被驱逐但持有锁的会话继续使用旧 Lock 对象 | 🟢 | Lock 对象仍在内存中，不影响已持有者 |

| 需求 | 验证方式 |
|------|---------|
| R15 | `plugin_helpers.py` 含 `create_task` |
| R16 | `memory_write_service.py` 含 `logger.warning` |
| R17 | `v2_store.py` 含 `.locked()` 检查 |
| R18 | `handoff_registry.py` 不含 `_loaded` 一次性逻辑 |
| R19 | `lifecycle.py` 含 `logger.warning` |
| R20 | `user_profile_service.py` 锁内重读 |
| R21 | `memory_scoring.py` 含 `max(0.0` |
