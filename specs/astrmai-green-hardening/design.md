# Design Document

> 本文档对应 Spec `astrmai-green-hardening`，描述 15 个 🟢 级问题的加固设计方案。
> R4（FaissVecDB 退避）和 W4（SQL 注入）经审计确认无需修改，不包含在本设计中。

## 1. Overview

### 1.1 整体策略

| Wave | 主题 | 改动数 | 改动类型 |
|------|------|:--:|---------|
| ① 资源与调度 | R1–R3, R5–R6 | 5 | 修复 + 文档 |
| ② WebUI 安全 | W1–W3, W5 | 4 | 修复 + 文档 |
| ③ 代码质量 | Q1–Q4 | 4 | Protocol + 修复 + 统计 |

### 1.2 设计边界

- 不修改业务逻辑（仅加固 + 文档）
- 不新增 pip 依赖
- 不新增 DB 表或列
- 不修改 AstrBot 框架 API

### 1.3 审计确认项（零改动）

| 项 | 结论 |
|----|------|
| R4 FaissVecDB | 指数退避已实现：`30 * 2^(failures-1)`s，上限 3600s ✅ |
| W4 SQL 注入 | 全部使用参数化查询 `?` 占位符 ✅ |

---

## 2. Architecture — 关键不变量

| 不变量 | 来源 | 冻结理由 |
|--------|------|---------|
| `_remote_sessions_ttl` 降低至 300s | `lane_manager.py:L67` | R1 减少 provider 侧泄漏窗口 |
| `EventBus._dropped_count` 新增计数器 | `event_bus.py` | R2 溢出可观测 |
| `_handle_task_result` 增加 CancelledError debug 日志 | `lifecycle.py:L34` | R3 取消可追溯 |
| `_get_cached_memory` 异步化 | `visual_cortex.py:L82` | R6 消除事件循环阻塞 |
| `_body()` 增加 warning 日志 | `plugin_pages.py:L104` | W3 解析失败可观测 |
| `ingress` 关键路径 fail-secure | `message_entry.py` | W5 权限守卫默认拒绝 |

---

## 3. Wave 1 — 资源与调度（R1–R3, R5–R6）

### 3.1 R1: Lane rotation TTL 降低

**涉及文件**: `astrmai/infrastructure/runtime/lane_manager.py`

#### 当前状态

```python
# L67: TTL 3600s（1 小时）
self._remote_sessions_ttl: float = 3600.0
```

`_cleanup_remote_sessions()` (L162-169) 按此 TTL 清理过期条目。但 rotation 时仅清理本地映射（`lane_storage.py:L58`），provider 侧 session 持续存活直到自身 TTL。

#### 设计决策

**降低 TTL 至 300s（5 分钟）。** 与 Anthropic/OpenAI 的服务端 session TTL 对齐。

```python
self._remote_sessions_ttl: float = 300.0  # 5 min, aligns with provider session TTL
```

#### 影响范围：1 文件，+0/-0（仅改值）

#### 禁止：不修改 rotation 逻辑

---

### 3.2 R2: EventBus 溢出计数器

**涉及文件**: `astrmai/infrastructure/runtime/event_bus.py`

#### 当前状态

```python
# L184-189: 溢出时仅 log，无计数
except asyncio.QueueFull:
    logger.warning(f"[EventBus] 事件积压超限 (1000)...")
```

#### 设计决策

**新增 `_dropped_count` 计数器 + 采样日志。**

```python
# __init__ 新增：
self._dropped_count = 0

# publish() 修改：
except asyncio.QueueFull:
    self._dropped_count += 1
    if self._dropped_count % 100 == 1:
        logger.warning(f"[EventBus] queue full, dropped {self._dropped_count} total")
```

#### 影响范围：1 文件，+3

---

### 3.3 R3: CancelledError debug 日志

**涉及文件**: `astrmai/app/lifecycle.py`

#### 当前状态

```python
# L34-35: 静默吞 CancelledError
except asyncio.CancelledError:
    pass
```

`discard()` 在 L29 已执行（try 之前），无内存泄漏。仅缺可观测性。

#### 设计决策

**`pass` → `logger.debug()`。**

```python
except asyncio.CancelledError:
    logger.debug(f"[AstrMai-Background] task cancelled: {task.get_name()}")
```

#### 影响范围：1 文件，+1/-1

---

### 3.4 R5: DreamScheduler docstring 增强

**涉及文件**: `astrmai/proactive/dream_scheduler.py`

#### 设计决策

`run_once_for_session()` 增加 docstring 说明节流是全局的。

```python
async def run_once_for_session(self, session_id: str):
    """Trigger dream for a specific session.

    Note: throttle is **global** — ``_last_dream_time`` is shared across
    all sessions.  ``session_id`` is passed to the dream agent but does NOT
    affect the throttle decision.
    """
```

#### 影响范围：1 文件，+3

---

### 3.5 R6: VisualCortex to_thread() 修复

**涉及文件**: `astrmai/multimodal/visual_cortex.py`

#### 当前状态

```python
# L82: 同步 DB 读阻塞事件循环
if self._get_cached_memory(picid):
    ...

# L109-115: 写路径正确异步化 ✅
await asyncio.to_thread(self._upsert_visual_memory, ...)
```

#### 设计决策

**L82 包裹 `asyncio.to_thread()`。**

```python
# 修改后：
cached = await asyncio.to_thread(self._get_cached_memory, picid)
if cached:
    ...
```

#### 影响范围：1 文件，+1/-1

---

## 4. Wave 2 — WebUI 安全（W1–W3, W5）

### 4.1 W1: API 鉴权文档化

**涉及文件**: `astrmai/webui/plugin_pages.py`

#### 设计决策

`register_astrmai_admin_pages()` 增加 docstring 说明安全模型。

```python
def register_astrmai_admin_pages(context, facade):
    """Register ~85 admin API endpoints.

    Security model: all endpoints rely on AstrBot Plugin Page isolation
    (iframe sandbox + SAMEORIGIN + CSP headers).  No standalone auth
    middleware is implemented — access is gated by the AstrBot WebUI
    admin panel login.
    """
```

#### 影响范围：1 文件，+5

---

### 4.2 W2: approve/approved 显式映射

**涉及文件**: `astrmai/webui/backend/services/review_ui_service.py`

#### 当前状态

```python
# L164: 脆弱的 else 回退
mapped = "approved" if action == "approve" else "rejected"
```

#### 设计决策

**显式映射字典。**

```python
ACTION_MAP = {"approve": "approved", "reject": "rejected", "revise": "revision_needed", "replace": "replace"}
mapped = ACTION_MAP.get(action)
if mapped is None:
    return {"status": "error", "message": f"Unknown action: {action!r}"}
```

#### 影响范围：1 文件，+5/-1

---

### 4.3 W3: _body() 增加 warning 日志

**涉及文件**: `astrmai/webui/plugin_pages.py`

#### 当前状态

```python
# L104-105: 静默返回 {}
except Exception:
    return {}
```

#### 设计决策

```python
except Exception as exc:
    logger.warning(f"[AstrMai] _body parse failed: {exc}")
    return {}
```

#### 影响范围：1 文件，+2/-1

---

### 4.4 W5: Ingress 关键路径 fail-secure

**涉及文件**: `astrmai/presentation/events/message_entry.py`

#### 当前状态

```python
# L47-52: 权限守卫异常→默认放行
try:
    if facade.check_message_scope_access(scope).should_stop:
        return
except Exception:
    logger.exception(...)  # 继续处理 ← 不安全
```

```python
# L54-58: group_wait 异常→静默丢消息
try:
    group_wait_result = await facade.handle_group_reply_wait(event, scope)
except Exception:
    logger.exception(...)
    return  # ← 消息丢失，无用户提示
```

#### 设计决策

**权限守卫：fail-secure（默认拒绝）。Group wait：兜底消息。**

```python
# L47-52 修改后：
try:
    if facade.check_message_scope_access(scope).should_stop:
        return
except Exception:
    logger.exception("[AstrMai] permission guard failed — denying by default")
    return

# L54-58 修改后：
try:
    group_wait_result = await facade.handle_group_reply_wait(event, scope)
except Exception:
    logger.exception("[AstrMai] handle_group_reply_wait failed")
    yield event.plain_result("处理消息时遇到问题，请稍后再试。")
    return
```

#### 影响范围：1 文件，+3/-1

---

## 5. Wave 3 — 代码质量（Q1–Q4）

### 5.1 Q1: 核心服务 Protocol

**涉及文件**: `astrmai/shared/contracts/service_protocols.py` (new)

#### 设计决策

第一期覆盖 `gateway` 和 `memory_engine`。

```python
from typing import Protocol

class GatewayProtocol(Protocol):
    """Minimal protocol for GlobalModelGateway."""
    async def chat_in_lane_result(self, *, lane_key, prompt, ...) -> Any: ...
    async def call_judge_task(self, *, prompt, ...) -> Any: ...

class MemoryEngineProtocol(Protocol):
    async def search_memories(self, query, top_k, ...) -> list: ...
    async def initialize(self) -> None: ...
```

`runtime_context.py` 中 gateway/memory_engine 字段改为 `GatewayProtocol | None`。

#### 影响范围：1 新文件 +20，1 修改 +1/-1

---

### 5.2 Q2: 静默 except 标注

**涉及文件**: `proactive/dispatcher.py`, `proactive/proactive_task.py`, `webui/plugin_pages.py`

#### 当前状态：8 处 `except Exception:` 无日志

#### 设计决策

每处增加 `# ponytail:` 注释标注意图或增加 debug 日志。

```python
# dispatcher.py:309
except Exception:  # ponytail: setattr fail means coordinator restore is best-effort
    runtime_coordinator_detached = False

# proactive_task.py:721
except Exception:  # ponytail: heartbeat context is non-critical
    final_context = {}
```

#### 影响范围：3 文件，+8

---

### 5.3 Q3: stop_event() 补充

**涉及文件**: `main.py`

#### 设计决策

在 `on_global_message` handler 中，当检查到 `should_stop` 时增加 `event.stop_event()`。

```python
@filter.event_message_type(filter.EventMessageType.ALL, priority=10)
async def on_global_message(self, event: AstrMessageEvent):
    if self.facade.check_command_access(event).should_stop:
        event.stop_event()
        return
    async for result in self.facade.on_global_message(event):
        yield result
```

#### 影响范围：1 文件，+1

---

### 5.4 Q4: 测试覆盖报告

**涉及文件**: `tests/COVERAGE.md` (new)

#### 设计决策

产出覆盖统计：

| 层 | 文件数 | 占比 |
|----|:--:|:--:|
| unit | 20 | 48% |
| integration | 3 | 7% |
| regression | 19 | 45% |
| **Total** | **42** | |

标注集成测试缺口（仅 3 文件）。

#### 影响范围：1 新文件，+30

---

## 6. Risk Assessment

| # | 风险 | 等级 | 缓解 |
|---|------|:--:|------|
| RSK1 | R6 `asyncio.to_thread` 包裹后，`_worker` 协程不再阻塞 → 但 DB 操作仍在 thread pool 中，高并发时可能耗尽线程 | 🟢 | 图片处理频率低（<1/s），线程池默认足够 |
| RSK2 | R1 TTL 降低至 300s → 若 lane 超过 5 分钟未使用，session 被清理，下次需重建 | 🟢 | 正常对话频率远高于 5 分钟 |
| RSK3 | W5 权限守卫改为默认拒绝 → 若 `check_message_scope_access` 偶发异常（如网络波动触发持久化错误），正常消息被误拒 | 🟡 | 增加 `logger.error` 便于排查；异常应极为罕见 |
| RSK4 | W2 显式映射字典新增 → 如果前端发送了字典未覆盖的新 action（如 `"delete"`），返回 error 而非静默映射为 `"rejected"` | 🟢 | 比旧行为更安全（fail-closed） |
| RSK5 | Q1 Protocol 定义可能不完整 → 如果 `GatewayProtocol` 遗漏了某些方法，类型检查会误报 | 🟢 | Protocol 仅做文档用途，不强制类型检查 |

## 7. Verification Matrix

| # | 需求 | 验证方式 | 通过标准 |
|---|------|---------|---------|
| V1 | R1 | 查看 `lane_manager.py` L67 | TTL = 300 |
| V2 | R2 | 构造 >1000 事件 → 检查日志 | `_dropped_count` 递增 |
| V3 | R3 | 取消后台任务 → 检查日志 | debug 日志输出 task name |
| V4 | R5 | 查看 docstring | 包含 "throttle is global" |
| V5 | R6 | 图片处理不阻塞事件循环 | `to_thread` 包裹确认 |
| V6 | W1 | 查看 docstring | 包含安全模型说明 |
| V7 | W2 | 发送 `action: "unknown"` → 返回 error | `"Unknown action"` |
| V8 | W3 | 发送非法 JSON → 检查日志 | warning 日志输出 |
| V9 | W5 | 模拟权限守卫异常 → 消息被拒绝 | 返回而非继续 |
| V10 | W5 | 模拟 group_wait 异常 → 兜底消息 | `"请稍后再试"` |
| V11 | Q1 | `from astrmai.shared.contracts.service_protocols import GatewayProtocol` | ImportError 为零 |
| V12 | Q2 | 搜索 `except Exception:` → 每处有注释 | 8/8 已标注 |
| V13 | Q3 | 搜索 `stop_event` 在 main.py | 至少 1 处调用 |
| V14 | Q4 | `tests/COVERAGE.md` 存在 | 文件可读 |
| V15 | ALL | `pytest tests/ -q --tb=short` | ≥ 68 passed；`lsp_diagnostics` 0 error |

## 8. Summary（变更汇总）

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| 1 | `lane_manager.py` | TTL 3600→300 | +0/-0 |
| 2 | `event_bus.py` | 新增 `_dropped_count` | +3 |
| 3 | `lifecycle.py` | CancelledError debug log | +1/-1 |
| 4 | `dream_scheduler.py` | docstring | +3 |
| 5 | `visual_cortex.py` | `to_thread` 包裹 | +1/-1 |
| 6 | `plugin_pages.py` | W1 docstring + W3 log | +7/-1 |
| 7 | `review_ui_service.py` | 显式 ACTION_MAP | +5/-1 |
| 8 | `message_entry.py` | fail-secure | +3/-1 |
| 9 | `service_protocols.py` | **新建** | +20 |
| 10 | `runtime_context.py` | Protocol 类型 | +1/-1 |
| 11 | `dispatcher.py` | except 注释 | +1/-0 |
| 12 | `proactive_task.py` | except 注释 ×2 | +2 |
| 13 | `main.py` | stop_event | +1 |
| 14 | `tests/COVERAGE.md` | **新建** | +30 |
| **Total** | **14 文件** | | **~+78 / -6** |

---

> **设计文档完成。** `design.md` 全部 13 个模块设计（R4/W4 审计跳过）+ 变更汇总已写入。可进入 Phase 3（任务文档）或直接执行。


