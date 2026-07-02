# Design Document

> 本文档对应 Spec `high-bugfixes-v2`，描述 9 个 HIGH 运行时缺陷的修复设计方案。

## 1. Overview

| 阶段 | 主要动作 | 改动文件 | 改动类型 |
|------|---------|---------|---------|
| ① R14, R13, R11, R12 | 防御性修复 | `main.py`, `gate.py`, `group_dialogue_store.py`, `bootstrap.py` | 局部修复 |
| ② R7, R8, R9 | EventBus 三连修复 | `event_bus.py` | 局部修复 |
| ③ R6, R10 | 核心链路修复 | `main.py`, `message_entry.py` | 架构适配 |

### 1.1 设计边界
- 不新增外部依赖
- 不修改 AstrBot 核心库
- 不修改配置系统

---

## 2. Wave 1 — 核心链路缺陷（R6–R10）

### 2.1 R6: `on_llm_request` 缓存破坏 — 条件赋值 + `extra_user_content_parts`

**当前状态** (`main.py:88-105`)：
```python
request.system_prompt = maybe_attach_reverse_session_block(...)  # ⚠ 每次请求都赋值
```

**设计决策**：条件保护 — 仅在 provider 为 gemini-reverse 且 system_prompt 中不存在 reverse-session 块时才修改。

**修复后**：
```python
provider = None
try:
    provider = self.context.get_using_provider(event.unified_msg_origin)
except Exception:
    pass

sp = getattr(request, "system_prompt", "") or ""
# ponytail: only modify system_prompt when necessary to avoid breaking provider prefix caching
needs_reverse_block = provider is not None and "astrbot_reverse_session" not in sp
if needs_reverse_block:
    request.system_prompt = maybe_attach_reverse_session_block(sp, provider, ...)
else:
    # Keep original prompt intact; trace hash still works
    pass
```

**影响**：`main.py:88-105`，+3/-1 行。

---

### 2.2 R7: 未追踪任务 — 加入 `_background_tasks`

**当前状态** (`event_bus.py:69,203`)：
```python
safe_create_task(self.publish(...))       # L69 — 未追踪
safe_create_task(self._worker_health_check())  # L203 — 未追踪
```

**设计决策**：两处均加入 `_background_tasks`。

**修复后**：
```python
# L68-69
t = safe_create_task(self.publish(self.TOPIC_KNOWLEDGE_UPDATED))
self._background_tasks.add(t)

# L203
t = safe_create_task(self._worker_health_check())
self._background_tasks.add(t)
```

**影响**：`event_bus.py:68-69,203`，+4 行。

---

### 2.3 R8: 健康检查误数 — 新增 `_worker_tasks` 集合

**当前状态** (`event_bus.py:178`)：
```python
active = sum(1 for t in list(self._background_tasks) if not t.done())  # ⚠ 混合计数
```

**设计决策**：新增 `_worker_tasks: set` 专门追踪 `_worker_loop` 创建的任务。`_background_tasks` 保留用于 `stop()` 的完整追踪。健康检查只统计 `_worker_tasks`。

**修复后** (`event_bus.py:_init_bus`)：
```python
self._worker_tasks: set[asyncio.Task] = set()  # worker-only tracking
```

在 worker 创建处 (`_worker_loop` 的启动)：
```python
task = safe_create_task(self._worker_loop())
self._background_tasks.add(task)
self._worker_tasks.add(task)  # ← 新增
```

健康检查：
```python
active = sum(1 for t in list(self._worker_tasks) if not t.done())
```

`stop()` 中同样清理 `_worker_tasks`。

**影响**：`event_bus.py:_init_bus`, L152-153, L172-184, `stop()`，+5/-2 行。

---

### 2.4 R9: QueueFull 丢弃 — 提高日志频率 + 暴露计数器

**当前状态** (`event_bus.py:207-212`)：
```python
except asyncio.QueueFull:
    self._dropped_count += 1
    if self._dropped_count % 100 == 1:  # ⚠ 99% 的丢弃不可见
        logger.warning(...)
```

**设计决策**：每次丢弃都记录 warning（含 topic 和 qsize）。`_dropped_count` 通过现有 admin API 暴露。

**修复后**：
```python
except asyncio.QueueFull:
    self._dropped_count += 1
    logger.warning(
        f"[EventBus] queue full (size={self._event_queue.qsize()}), "
        f"dropped topic={topic}, total_dropped={self._dropped_count}"
    )
```

**admin API 暴露**：在 `event_bus.py` 添加 `get_dropped_count()` 方法，由 admin API 的 `/runtime/health` 端点调用。

**影响**：`event_bus.py:208-212`，+4/-3 行。

---

### 2.5 R10: 消息静默丢弃 — 错误时发送 fallback

**当前状态** (`message_entry.py:93-97`)：
```python
try:
    status = await facade.record_and_dispatch_attention(event, scope)
except Exception:
    logger.exception(...)
    status = "error"
    is_direct_call = False
# ⚠ status="error" 后续未被使用，用户无任何反馈
```

**设计决策**：当 `status == "error"` 时，向用户发送 fallback 错误提示。

**修复后**：
```python
except Exception:
    logger.exception(...)
    status = "error"
    is_direct_call = False

if status == "error":
    yield event.plain_result(
        getattr(facade.config.reply, "fallback_text", "处理出错，请稍后重试")
    )
    return  # 阻止后续 ghost sentinel 逻辑
```

**影响**：`message_entry.py:93-97`，+4 行。

---

## 3. Wave 2 — 防御性修复（R11–R14）

### 3.1 R11: `str(chat_id or "")` → sentinel 防护

**当前状态** (`group_dialogue_store.py:126,134,139+`)：
```python
thread = self._threads.get(str(chat_id or ""))  # chat_id=None → key=""
```

**设计决策**：添加 `_resolve_chat_key()` 方法，对 None/空字符串抛出明确异常。

**修复后**：
```python
@staticmethod
def _resolve_chat_key(chat_id: str | None) -> str:
    key = str(chat_id or "").strip()
    if not key:
        raise ValueError("chat_id must be a non-empty string")
    return key
```

在所有 `str(chat_id or "")` 出现处替换为 `self._resolve_chat_key(chat_id)`。

**影响**：`group_dialogue_store.py`，+6 行，所有调用点替换。

---

### 3.2 R12: ProactiveTask 失败 → 增强日志

**设计决策**：在 `except` 块中添加 `logger.warning`。

**修复后** (`bootstrap.py:483-485`)：
```python
except Exception as exc:
    self._record_optional_failure(runtime, "proactive.task", exc)
    logger.warning(f"[Bootstrap] ProactiveTask creation failed: {type(exc).__name__}: {exc}")
    logger.warning("[Bootstrap] 主动发言、梦境整理等功能将不可用")
    return None
```

**影响**：`bootstrap.py:483-485`，+2 行。

---

### 3.3 R13: `gate.py` 传感器 → `logger.exception()`

**设计决策**：两处用 `logger.exception()` 替代 `logger.warning(exc_info=True)`。

**修复后** (`gate.py:540-548`)：
```python
except Exception:
    logger.exception(f"[AttentionGate] sensor is_command check failed on msg={msg_str[:100]!r}")
except Exception:
    logger.exception("[AttentionGate] sensor should_process_message check failed, defaulting to pass")
    return True
```

**影响**：`gate.py:540-548`，+2/-4 行。

---

### 3.4 R14: `main.py` 钩子 → `logger.debug(exc_info=True)`

**设计决策**：`main.py:133,141,148` 的 `except Exception: pass` 替换为 `logger.debug(..., exc_info=True)`。

**修复后**：
```python
# L133-134
except Exception:
    logger.debug("[AstrMai] on_llm_response hook failed", exc_info=True)

# L141-142
except Exception:
    logger.debug("[AstrMai] on_agent_begin hook failed", exc_info=True)

# L148-149
except Exception:
    logger.debug("[AstrMai] on_agent_done hook failed", exc_info=True)
```

**影响**：`main.py:133-134,141-142,148-149`，+6/-3 行。

---

## 4. Risk Assessment

| 风险 | 等级 | 触发条件 | 缓解 |
|------|------|---------|------|
| R6: reverse-session 块在 user content 中影响 Gemini 解析 | 🔴 | Gemini 反向代理期望 block 在 system_prompt 中 | 条件保护优先：仅在 gemini-reverse + block 不存在时修改 system_prompt |
| R8: `_worker_tasks` 与 `_background_tasks` 不同步 | 🟡 | worker 被取消但未从 `_worker_tasks` 移除 | `add_done_callback` 中同时从两个集合移除 |
| R11: `_resolve_chat_key` 新增 ValueError 可能影响上游调用方 | 🟡 | chat_id 可能在某些路径合法为空 | 仅在 GroupDialogueStore 内部使用，上游已保证 chat_id 非空 |

## 5. Verification Matrix

| 需求 | 验证方式 | 通过标准 |
|------|---------|---------|
| R6 | 检查 `main.py:97` 前有条件判断，非 gemini 时跳过赋值 | `"astrbot_reverse_session" not in sp` 和 provider 检查存在 |
| R7 | 检查 L69 和 L203 的任务变量被加入 `_background_tasks` | `.add(t)` 调用存在 |
| R8 | 检查 `_init_bus` 中 `_worker_tasks` 初始化，健康检查使用 `_worker_tasks` | `_worker_tasks` 字段存在且被使用 |
| R9 | 检查丢弃日志含 `topic` 和 `qsize` | `logger.warning` 含 topic 和 qsize |
| R10 | 检查 `status=="error"` 后有 yield 和 return | `yield event.plain_result` 和 `return` 存在 |
| R11 | 检查 `_resolve_chat_key` 存在且被调用 | 新方法存在，调用点替换 |
| R12 | 检查 except 块中有 `logger.warning` | 新增 warning 日志存在 |
| R13 | 检查 `except Exception` 内使用 `logger.exception()` | `logger.exception` 替换 `logger.warning(exc_info=True)` |
| R14 | 检查三处 `except Exception: pass` 被替换为 `logger.debug` | 无 `pass`，有 `logger.debug` |
| 全量 | `pytest tests/` | 测试通过数不减少 |

## 6. 变更文件汇总

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| 1 | `main.py` | R6 条件赋值 + R14 debug logging | +10/-4 |
| 2 | `event_bus.py` | R7 task tracking + R8 worker_tasks + R9 logging | +12/-6 |
| 3 | `message_entry.py` | R10 error fallback | +4 |
| 4 | `group_dialogue_store.py` | R11 sentinel key | +8/-2 |
| 5 | `bootstrap.py` | R12 warning logs | +2 |
| 6 | `gate.py` | R13 logger.exception | +2/-4 |
| **Total** | **6 个文件** | | **~40 行** |
