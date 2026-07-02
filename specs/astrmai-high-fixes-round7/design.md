# Design Document — AstrMai 第二轮审查高危修复

> 本文档对应 Spec `astrmai-high-round7-20260630`
> 基于 `requirements.md` 中 9 条需求（R1–R9），按 3 个 Wave 展开。

---

## 1. Overview

### 1.1 整体策略

| 阶段 | 主题 | 需求 | 改动文件 | 改动类型 |
|------|------|:--:|------|:--:|
| ① Wave 1 | 消息可靠性 | R1–R3 | 3 | 局部修复 |
| ② Wave 2 | 数据完整性 | R4–R6 | 3 | 逻辑变更 |
| ③ Wave 3 | 基础设施 | R7–R9 | 3 | 补调用/导入 |

### 1.2 设计边界

| 禁止项 | 原因 |
|--------|------|
| **不修改 DB schema** | 不新增表/列 |
| **不改变外部 API 契约** | `ToolExecResult` 格式不变 |
| **不引入新依赖** | 仅用已有库 |

---

## 2. Wave 1 — 消息可靠性（R1–R3）

### 2.1 R1: 权限守卫补 `stop_event()`

**文件**: `astrmai/presentation/events/message_entry.py:54`

#### 当前状态
```python
# line 52-58 — 当前（BUG: 缺少 stop_event）
except Exception:
    logger.exception("[AstrMai] check_message_scope_access failed — denying by default")
    return  # ← 未调用 event.stop_event()
```

对比文件中其他 6 个提前返回全部调用了 `event.stop_event()`。

#### 设计决策
```python
# 修复后
except Exception:
    logger.exception("[AstrMai] check_message_scope_access failed — denying by default")
    event.stop_event()
    return
```

#### 影响范围
| 文件 | 改动 | 行数 |
|------|------|:--:|
| `message_entry.py` | +1 行 | +1 |

---

### 2.2 R2: 主动分发不阻塞并发消息

**文件**: `astrmai/proactive/dispatcher.py:304-311`

#### 当前状态
```python
# line 304-311 — 当前（BUG: 全局副作用）
original_runtime_coordinator = getattr(self.attention_gate, "runtime_coordinator", None)
if hasattr(self.attention_gate, "runtime_coordinator"):
    setattr(self.attention_gate, "runtime_coordinator", None)  # ← 全局副作用!
    runtime_coordinator_detached = True
```

`gate.py:643` 检查 `runtime_coordinator is None` → 返回 `"PROACTIVE_BLOCKED"`，丢弃并发用户消息。

#### 设计决策

用 `dict[str, bool]` 替代全局 `setattr`：
```python
# gate.py — 新增 per-chat 标志
self._proactive_dispatching: dict[str, bool] = {}

# dispatcher.py — 修复后
chat_id = intent.chat_id
self.attention_gate._proactive_dispatching[chat_id] = True
try:
    result = await self.attention_gate.inject_external_event(intent.chat_id, event_data)
finally:
    self.attention_gate._proactive_dispatching.pop(chat_id, None)

# gate.py:643 — 修复后
if self._proactive_dispatching.get(chat_id, False):
    return "PROACTIVE_BLOCKED"
```

#### 影响范围
| 文件 | 改动 | 行数 |
|------|------|:--:|
| `dispatcher.py` | 替换 setattr 为 dict 操作 | +5 / -5 |
| `gate.py` | 加 `_proactive_dispatching` dict + 替换检查 | +3 / -1 |

---

### 2.3 R3: stop() 后不重新激活调度器

**文件**: `astrmai/proactive/proactive_task.py:204-215`

#### 当前状态
```python
def _on_loop_done(self, task):
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        if self._is_running:
            asyncio.get_event_loop().call_later(5, lambda: asyncio.ensure_future(self.start()))
        return
    if exc and self._is_running:
        asyncio.get_event_loop().call_later(5, lambda: asyncio.ensure_future(self.start()))
```

**BUG**: `_is_running` 在回调注册时检查为 `True`，但 5 秒后 `stop()` 将其改为 `False`，`start()` 仍会执行。

#### 设计决策

在 lambda 内二次检查 + 替换废弃 API：
```python
def _on_loop_done(self, task):
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        if self._is_running:
            loop = asyncio.get_running_loop()
            loop.call_later(5, lambda: self._restart_if_still_running())
        return
    if exc and self._is_running:
        loop = asyncio.get_running_loop()
        loop.call_later(5, lambda: self._restart_if_still_running())

def _restart_if_still_running(self):
    """ponytail: re-check _is_running after 5s delay"""
    if self._is_running:
        asyncio.ensure_future(self.start())
```

#### 影响范围
| 文件 | 改动 | 行数 |
|------|------|:--:|
| `proactive_task.py` | 替换 restart 逻辑 | +12 / -8 |

---

## 3. Wave 2 — 数据完整性（R4–R6）

### 3.1 R4: UserProfile 缓存失效 + 即时持久化

**文件**: `astrmai/state/user_profile_service.py`

#### 当前状态
- 内存缓存 `self.user_profiles` 永不清除
- `observe_user_activity` 仅设 `is_dirty=True`，依赖 15s 定时 flush
- 无 `invalidate_cache` 方法

#### 设计决策

```python
# 新增方法
def invalidate_cache(self, user_id: str = None):
    """ponytail: invalidate cached profile(s) after external modification"""
    if user_id:
        self.user_profiles.pop(user_id, None)
    else:
        self.user_profiles.clear()

# observe_user_activity — 即时持久化
async def observe_user_activity(self, user_id, ...):
    profile = await self.get_user_profile(user_id, ...)
    # ... modify profile ...
    await self._touch_profile(user_id, profile)
    await self._flush_profile(user_id, profile)  # ← 新增：即时写入

# _flush_profile — 抽取自 flush_message_counters
async def _flush_profile(self, user_id, profile):
    if not profile.is_dirty:
        return
    try:
        await self.persistence.save_user_profile(user_id, profile.as_dict())
        profile.is_dirty = False
    except Exception as exc:
        logger.warning(f"[AstrMai-profile] flush failed for {user_id}: {exc}")
```

#### 影响范围
| 文件 | 改动 | 行数 |
|------|------|:--:|
| `user_profile_service.py` | +`invalidate_cache` + `_flush_profile` | +20 |

---

### 3.2 R5: 双写统一为单写

**文件**: `astrmai/infrastructure/persistence/database_review.py:70-95`

#### 当前状态
```python
# save_pattern — 双写
pattern = ExpressionPattern(...)
session.add(pattern)           # ← ORM 写入
session.commit()
await _save_pattern_to_canonical_async(...)  # ← v2_store 写入
```

#### 设计决策

保留 ORM 读取兼容，统一写入到 v2_store：
```python
# save_pattern — 修复后
async def save_pattern(self, pattern_data):
    # 写入 v2_store（主路径）
    canonical_id = await self._save_pattern_to_canonical_async(pattern_data)
    # 同步写入 ORM（标记 deprecated，仅读兼容）
    try:
        pattern = ExpressionPattern(canonical_id=canonical_id, **pattern_data)
        session.add(pattern)
        session.commit()
    except Exception as exc:
        logger.warning(f"[AstrMai-review] ORM sync write failed (non-critical): {exc}")
    return canonical_id
```

#### 影响范围
| 文件 | 改动 | 行数 |
|------|------|:--:|
| `database_review.py` | 调换写入顺序 + deprecated 注释 | +5 / -3 |

---

### 3.3 R6: purge 同步清理 FTS

**文件**: `astrmai/memory/services/v2_store.py:1123,1177`

#### 当前状态
```python
# purge_jargon_candidates — 只删 canonical_memories
await db.execute(delete(CanonicalJargon).where(...))
# ← 未清理 canonical_fts
await db.commit()
```

#### 设计决策
```python
# 修复后
await db.execute(delete(CanonicalJargon).where(...))
# ponytail: sync FTS to prevent phantom search results
await db.execute(text("DELETE FROM canonical_fts WHERE memory_id IN (SELECT id FROM canonical_memories WHERE kind = 'jargon' AND status = 'stale')"))
await db.commit()
```

#### 影响范围
| 文件 | 改动 | 行数 |
|------|------|:--:|
| `v2_store.py:1123` | +1 FTS DELETE | +2 |
| `v2_store.py:1177` | +1 FTS DELETE | +2 |

---

## 4. Wave 3 — 基础设施（R7–R9）

### 4.1 R7: plugin_pages.py 补 logger 导入

**文件**: `astrmai/webui/plugin_pages.py`

#### 设计决策
```python
# 在文件顶部现有导入后追加
from astrbot.api import logger
```

#### 影响范围: +1 行

---

### 4.2 R8+R9: terminate 补 event_bus.stop + persistence.dispose

**文件**: `astrmai/app/lifecycle.py:175-240`

#### 当前状态
`terminate()` 未调用 `event_bus.stop()` 和 `persistence.dispose()`

#### 设计决策
```python
async def _terminate_impl(self):
    # ... existing cleanup ...
    
    # 停止 EventBus worker（在所有任务取消之后）
    event_bus = getattr(self.runtime, "event_bus", None)
    if event_bus is not None:
        await event_bus.stop()
    
    # 释放 DB 连接池（最后）
    persistence = getattr(self.runtime, "persistence", None)
    if persistence is not None:
        persistence.dispose()
```

#### 影响范围
| 文件 | 改动 | 行数 |
|------|------|:--:|
| `lifecycle.py` | +6 行 | +6 |

---

## 5. Risk Assessment

| 风险 | 等级 | 缓解 |
|------|:--:|------|
| R2 dict 并发访问 | 🟡 | asyncio 单线程无竞态，无需额外锁 |
| R4 即时持久化增加 IO | 🟡 | 仅 profile 变更触发，频率低 |
| R5 移除 ORM 写入可能影响 WebUI 读取 | 🟡 | 保留 ORM 读，仅改主写路径 |
| R6 FTS DELETE 跨表查询性能 | 🟢 | SQLite 子查询，数据量小 |

---

## 6. Verification Matrix

| 需求 | 验证 | 标准 |
|------|------|------|
| R1 | `grep "stop_event" message_entry.py` | 7 处 |
| R2 | `grep "runtime_coordinator.*None" dispatcher.py` | 0 处 setattr |
| R3 | `grep "_restart_if_still_running" proactive_task.py` | 存在 |
| R4 | `grep "invalidate_cache\|_flush_profile" user_profile_service.py` | 2 方法 |
| R5 | `grep "deprecated.*ORM" database_review.py` | 注释存在 |
| R6 | `grep "canonical_fts" v2_store.py` | purge 中有 DELETE |
| R7 | `grep "from astrbot.api import logger" plugin_pages.py` | 存在 |
| R8 | `grep "event_bus.stop" lifecycle.py` | 存在 |
| R9 | `grep "persistence.dispose" lifecycle.py` | 存在 |
| 全量 | `pytest tests/ -q` | ≥ 836 passed |

### 变更汇总

| Wave | 文件 | 行数 |
|------|------|:--:|
| ① R1–R3 | 3 | +18 / -14 |
| ② R4–R6 | 3 | +27 / -3 |
| ③ R7–R9 | 3 | +7 |
| **合计** | **9** | **~+52 / -17** |
