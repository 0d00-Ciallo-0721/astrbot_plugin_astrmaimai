# M7: FastAPI 独立服务器断裂面深度验证报告

**验证日期**: 2025-07-16  
**审查范围**: FastAPI :8765 独立服务器 routes → services 层构造函数及方法签名一致性  
**审查方法**: 逐文件对比 route 层 `_service()` 工厂函数与对应 service 类的 `__init__` 及公开方法签名

---

## 总体结论

**M7 报告全部 5 个 Blocking 问题均真实存在，且具有系统性根源。AstrBot Plugin Pages Bridge 路径完全不受影响。**

| 问题 | 状态 | 根因 | 严重性 |
|------|------|------|--------|
| B1: cognition_routes → CognitionService 构造函数参数不匹配 | ✅ **确认** | routes 传 2 参数，service 只收 1 个 | Blocking |
| B2: cognition_routes 调用 CognitionService 不存在的多个方法 | ✅ **确认** | cognition_routes 中 13 个 endpoint 调用的方法不在 CognitionService 中 | Blocking |
| B3: cognition_unified_timeline 签名不匹配 | ✅ **确认** | routes 传 4 参数（含 limit/level/include），service 只收 1 个 chat_id | Blocking |
| B4: runtime_routes → ObservabilityService 构造函数参数不匹配 | ✅ **确认** | routes 传 2 参数，service 只收 1 个 | Blocking |
| B5: heartflow/learning/tools routes 传递多余 get_db 参数 | ✅ **确认** | 3 个 route 文件均多传了 get_db 参数 | Blocking |

---

## 1. B1: cognition_routes → CognitionService 构造函数参数不匹配

### 代码对照

**`routes/cognition_routes.py:10`** — 工厂函数：
```python
def _service() -> CognitionService:
    return CognitionService(PluginApiAdapter(), get_db)   # ← 传了 2 个参数
```

**`services/cognitionservice.py:9`** — 构造函数：
```python
def __init__(self, plugin_api: PluginApiAdapter):    # ← 只收 1 个参数
    self._api = plugin_api
```

### 运行后果
```
TypeError: CognitionService.__init__() takes 2 positional arguments but 3 were given
```
FastAPI 启动后第一次请求该路由即崩溃，**所有 cognition 端点完全不可用**。

### 修复方向
**建议改 routes**：删除多余的 `get_db` 参数：
```python
def _service() -> CognitionService:
    return CognitionService(PluginApiAdapter())
```

---

## 2. B2: cognition_routes 调用 CognitionService 不存在的多个方法

`cognition_routes.py` 中 19 个 endpoint 全部通过 `_service()` 调用 CognitionService。但 CognitionService 只定义了 5 个方法，以下 **13 个方法调用将全部抛出 `AttributeError`**：

| route 端点 | 调用的方法 | CognitionService 中存在? | 正确所在 Service |
|---|---|---|---|
| `/api/cognition/observability/overview` | `observability_overview()` | ❌ 不存在 | ObservabilityService |
| `/api/cognition/observability/timeline` | `observability_timeline(...)` | ❌ 不存在 | ObservabilityService |
| `/api/cognition/observability/chats/{chat_id}` | `observability_chat(chat_id)` | ❌ 不存在 | ObservabilityService |
| `/api/cognition/observability/errors` | `observability_errors(...)` | ❌ 不存在 | ObservabilityService |
| `/api/cognition/observability/search` | `observability_search(...)` | ❌ 不存在 | ObservabilityService |
| `/api/cognition/context-economy` | `context_economy_overview_view(limit)` | ❌ 不存在 | ObservabilityService |
| `/api/cognition/context-economy/templates` | `context_economy_templates_view(...)` | ❌ 不存在 | ObservabilityService |
| `/api/cognition/scheduler/status` | `scheduler_status_view()` | ❌ 不存在 | AdminUiService/SchedulerService |
| `/api/cognition/scheduler/due-selection` | `scheduler_due_selection_view()` | ❌ 不存在 | AdminUiService/SchedulerService |
| `/api/cognition/scheduler/chats/{chat_id}` | `scheduler_chat_view(chat_id)` | ❌ 不存在 | AdminUiService/SchedulerService |
| `/api/cognition/recent-turns` (带 chat_id) | `recent_turn_traces(chat_id=chat_id, limit=limit)` | ❌ 签名不匹配 | AdminUiService |
| `/api/cognition/chats/{chat_id}/turns` (带 chat_id) | `recent_turn_traces(chat_id=chat_id, limit=limit)` | ❌ 签名不匹配 | AdminUiService |
| `/api/cognition/chats/{chat_id}/unified-timeline` | `cognition_unified_timeline(chat_id, limit, level, include)` | ❌ 签名不匹配 | AdminUiService（参见 B3）|

### 关键发现

CognitionService 只有 5 个方法，而 `cognition_routes.py` 需要它提供至少 18 个方法。这些方法全部实现在 `AdminUiService` 中。

### 根因

routes 层直接引用 `CognitionService`，但实际需要的功能分布于 `ObservabilityService`（observability / context-economy 类端点）和 `AdminUiService`（scheduler 类端点）。CognitionService 仅实现了 cognition 专用端点（recent_decisions / recent_turn_traces / chat_trace_events / cognition_unified_timeline）的薄代理。

### 修复方向（3 选 1）

**方案 A（推荐）** — 重构路由层，让每个路由文件使用正确的 service：
- `/api/cognition/observability/*` → 改用 `ObservabilityService`
- `/api/cognition/context-economy/*` → 改用 `ObservabilityService`
- `/api/cognition/scheduler/*` → 改用 `AdminUiService` 或新增 `SchedulerService`
- `/api/cognition/*`（纯 cognition 端点）→ 保留 `CognitionService`

**方案 B** — 将 `CognitionService` 扩展为"统一门面"，包含所有需要的代理方法（如以下未实现的 13 个方法），但不推荐（导致 CognitionService 职责过重）。

**方案 C** — 让所有路由直接使用 `AdminUiService`，但需要解决构造参数不一致问题（参见 B1/B4/B5 修复）。

---

## 3. B3: cognition_unified_timeline 签名不匹配

### 代码对照

**`routes/cognition_routes.py:68-73`** — 调用处：
```python
return await _service().cognition_unified_timeline(
    chat_id=chat_id,
    limit=limit,       # ← routes 传了
    level=level,       # ← routes 传了
    include=include_values,  # ← routes 传了
)
```

**`services/cognitionservice.py:27`** — 实际签名：
```python
async def cognition_unified_timeline(self, chat_id: str) -> dict[str, Any]:
```

### 运行后果
```
TypeError: CognitionService.cognition_unified_timeline() got unexpected keyword arguments 'limit', 'level', 'include'
```

### 对照 AdminUiService

`AdminUiService.cognition_unified_timeline`（admin_ui_service.py:664）的签名为：
```python
async def cognition_unified_timeline(self, *, chat_id: str, limit: int = 80,
                                      include: list[str] | None = None, level: str = "")
```
与 routes 的调用完全匹配。CognitionService 的薄代理缺少了这些参数。

### 修复方向

**改 services**：更新 `Cognitionservice.cognition_unified_timeline` 的签名以匹配：
```python
async def cognition_unified_timeline(
    self, *, chat_id: str, limit: int = 80,
    include: list[str] | None = None, level: str = ""
) -> dict[str, Any]:
    from .admin_ui_service import AdminUiService
    return await AdminUiService(self._api).cognition_unified_timeline(
        chat_id=chat_id, limit=limit, include=include, level=level)
```

---

## 4. B4: runtime_routes → ObservabilityService 构造函数参数不匹配

### 代码对照

**`routes/runtime_routes.py:10`**：
```python
def _service() -> ObservabilityService:
    return ObservabilityService(PluginApiAdapter(), get_db)   # ← 2 参数
```

**`services/observabilityservice.py:9`**：
```python
def __init__(self, plugin_api: PluginApiAdapter):             # ← 只收 1 个
    self._api = plugin_api
```

### 运行后果
```
TypeError: ObservabilityService.__init__() takes 2 positional arguments but 3 were given
```
**所有 runtime 端点（/status、/capabilities、/models、/health）完全不可用。**

### 修复方向
**改 routes**：删除多余的 `get_db`：
```python
def _service() -> ObservabilityService:
    return ObservabilityService(PluginApiAdapter())
```

---

## 5. B5: heartflow_routes / learning_routes / tools_routes 传递多余 get_db 参数

三个 route 文件 100% 相同模式：

| 文件 | 行号 | 当前代码 | 应改为 |
|---|---|---|---|
| `heartflow_routes.py` | 11 | `HeartflowService(PluginApiAdapter(), get_db)` | `HeartflowService(PluginApiAdapter())` |
| `learning_routes.py` | 11 | `LearningService(PluginApiAdapter(), get_db)` | `LearningService(PluginApiAdapter())` |
| `tools_routes.py` | 11 | `ToolsService(PluginApiAdapter(), get_db)` | `ToolsService(PluginApiAdapter())` |

三个 service 类的构造函数均只接受 `plugin_api: PluginApiAdapter`：
- `HeartflowService.__init__(self, plugin_api)`
- `LearningService.__init__(self, plugin_api)`
- `ToolsService.__init__(self, plugin_api)`

### 运行后果
启动后这三个 prefix 下的所有 endpoint 全部返回 500：
```
TypeError: XxxService.__init__() takes 2 positional arguments but 3 were given
```

### 修复方向
**改 routes**：删除每个工厂函数中多余的 `get_db` 参数。

---

## 6. AstrBot Plugin Pages Bridge 路径是否受影响？

**完全不受影响。** 原因：

### 构造参数对照

| 路径 | 工厂调用 | 目标构造函数 | 结果 |
|---|---|---|---|
| **Plugin Pages Bridge** (`plugin_pages.py:118`) | `AdminUiService(self.plugin_api, get_db)` | `AdminUiService.__init__(self, plugin_api, db_factory=None)` | ✅ **匹配** — `get_db` 作为 `db_factory` 传入 |
| FastAPI routes (全部 5 个) | `XxxService(PluginApiAdapter(), get_db)` | `XxxService.__init__(self, plugin_api)` | ❌ **不匹配** — 多传了 `get_db` |

### 方法覆盖范围对照

| 路径 | 使用的 Service | 是否包含全部需要的方法 |
|---|---|---|
| **Plugin Pages Bridge** | `AdminUiService` | ✅ **是** — 80+ 方法，覆盖全部功能 |
| FastAPI cognition_routes | `CognitionService` | ❌ **否** — 只有 5 个方法，缺少 13 个被调用的方法 |

`AdminUiService` 的构造函数接受可选的第二个参数 `db_factory: Callable | None = None`，因此在 Plugin Pages Bridge 中传入 `get_db` 完全合法。而 CognitionService / ObservabilityService / HeartflowService / LearningService / ToolsService 的构造函数全部只接受 `plugin_api`，不接受 `db_factory`。

### 结论
**AstrBot Plugin Pages Bridge 路径完整、正确，不受以上 5 个 Blocking 问题影响。**

---

## 7. 系统性根因分析

### 根本原因：service 层构造参数不统一 + 路由层引用错误的 service 类

| 文件 | 使用的 Service | 是否正确 |
|---|---|---|
| `routes/cognition_routes.py` | `CognitionService`（薄代理） | ❌ — 需要 `AdminUiService` 或组合使用多个 service |
| `routes/runtime_routes.py` | `ObservabilityService`（薄代理） | ⚠️ — 构造函数参数要修，方法调用正确 |
| `routes/heartflow_routes.py` | `HeartflowService`（薄代理） | ⚠️ — 构造函数参数要修，方法调用正确 |
| `routes/learning_routes.py` | `LearningService`（薄代理） | ⚠️ — 构造函数参数要修，方法调用正确 |
| `routes/tools_routes.py` | `ToolsService`（薄代理） | ⚠️ — 构造函数参数要修，方法调用正确 |
| `plugin_pages.py` | `AdminUiService`（完整实现） | ✅ — 完全正确 |

### 修复策略优先级

1. **Stop the bleeding**（5 分钟）：修复 B1/B4/B5 — 删除 5 个 route 文件工厂函数中多余的 `get_db` 参数
2. **Fix the missing methods**（15 分钟）：修复 B2/B3 — 将 cognition_routes.py 中 observability/context-economy/scheduler 端点拆分为使用正确的 service 类
3. **Add thin-service missing params**（5 分钟）：修复 B3 中 CognitionService.cognition_unified_timeline 缺少的参数

### 总结

```
5 个 route 文件 × 多传 get_db = 5 次 TypeError         ← 阻塞，立即修复
1 个 route 文件 × 指向错误 service = 13 次 AttributeError  ← 阻塞，需拆分
1 个 thin service × 缺少代理参数 = 1 次 TypeError        ← 阻塞，补全签名
```
