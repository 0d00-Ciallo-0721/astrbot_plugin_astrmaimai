# M7：FastAPI 断裂面深度验证报告

## 说明
- 本报告已按当前仓库代码重新校正。
- 仅修正结论，不修改业务代码。
- 本报告聚焦的是 `astrmai/webui/backend/server.py` 这条 standalone FastAPI backend 链。

## 总体结论

以下结论当前仍然成立：

- `standalone FastAPI backend` 确实存在真实断裂
- `AstrBot Plugin Pages` 主桥接链不应与这条 backend 断裂链混为一谈

## 当前已确认的真实问题

### B1. cognition_routes -> CognitionService 构造参数不匹配
- `routes/cognition_routes.py`
- `services/cognitionservice.py`

当前仍然是：
- route 传了 `PluginApiAdapter(), get_db`
- service 只收 `plugin_api`

### B2. cognition_routes 调用了 CognitionService 中不存在或签名不兼容的方法
当前仍然成立的包括：

- `observability_overview`
- `observability_timeline`
- `observability_chat`
- `observability_errors`
- `observability_search`
- `context_economy_overview_view`
- `context_economy_templates_view`
- `scheduler_status_view`
- `scheduler_due_selection_view`
- `scheduler_chat_view`

以及：
- `recent_turn_traces(chat_id=..., limit=...)` 与当前 `CognitionService` 签名不匹配
- `cognition_unified_timeline(chat_id, limit, level, include)` 与当前 `CognitionService` 签名不匹配

### B3. runtime_routes -> ObservabilityService 构造参数不匹配
- `routes/runtime_routes.py`
- `services/observabilityservice.py`

当前仍然成立。

### B4. heartflow / learning / tools routes 仍多传 `get_db`
以下当前仍然成立：

- `routes/heartflow_routes.py`
- `routes/learning_routes.py`
- `routes/tools_routes.py`

### B5. 旧报告未覆盖但当前已核验出的额外问题

#### tools_routes
- 当前调用了 `recent_tool_traces(...)`
- `ToolsService` 中没有这个方法

#### learning_routes
- 当前调用了 `run_reflect_once(chat_id)`
- `LearningService` 中没有这个方法

## 当前影响面结论

### 确认受影响
- standalone FastAPI backend 路由链
- 如果直接跑 `astrmai/webui/backend/server.py` 并访问这些 routes，会进入真实错误面

### 不应直接判死
- `main.py` 注册的 AstrBot Plugin Page 主入口
- `astrmai/webui/plugin_pages.py` 通过 `AdminUiService` 等桥接的主页面路径

## 修正后的最终结论
- 旧版“5 个 blocking 全都真实存在”的方向是对的
- 但当前实际问题不止 5 条，至少还应补记：
  - `tools_routes -> ToolsService.recent_tool_traces` 缺失
  - `learning_routes -> LearningService.run_reflect_once` 缺失
- 同时必须强调：
  - 这份报告验证的是 standalone FastAPI backend
  - 不应直接把 Plugin Page 主入口一并判为不可用
