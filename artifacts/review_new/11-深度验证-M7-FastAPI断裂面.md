# M7：FastAPI 断裂面深度验证报告

## 说明
- 本报告已按当前仓库代码重新校正。
- 仅修正结论，不修改业务代码。
- 本报告聚焦的是 `astrmai/webui/backend/server.py` 这条 standalone FastAPI backend 链。

## 总体结论

以下结论当前仍然成立：

- Prompt03/04 之前确认的 `standalone FastAPI backend` route/service 显性断裂已收口
- `AstrBot Plugin Pages` 主桥接链不应与这条 backend 断裂链混为一谈

## 当前已确认的真实问题

### B1. cognition_routes -> CognitionService 构造参数断裂已修复
- `routes/cognition_routes.py`
- `services/cognitionservice.py`

本轮已调整为：
- route 仅传 `PluginApiAdapter()`
- service 继续只收 `plugin_api`

### B2. cognition_routes 的方法委托与签名断裂已修复
本轮已补齐并对齐：

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
- `recent_turn_traces(chat_id=..., limit=...)` 已与当前 `CognitionService` 签名对齐
- `cognition_unified_timeline(chat_id, limit, level, include)` 已与当前 `CognitionService` 签名对齐

当前已改为与 route 调用形态一致，并通过最小路由调用复核。

### B3. runtime_routes -> ObservabilityService 构造参数断裂已修复
- `routes/runtime_routes.py`
- `services/observabilityservice.py`

本轮已调整为 route 仅传 `PluginApiAdapter()`。

### B4. heartflow / learning / tools routes 构造参数断裂已修复
以下 route 已不再多传 `get_db`：

- `routes/heartflow_routes.py`
- `routes/learning_routes.py`
- `routes/tools_routes.py`

### B5. 旧报告未覆盖的 learning/tools 死路由问题已修复

#### tools_routes
- `recent_tool_traces(...)` 已补齐真实委托，不再直连缺失方法

#### learning_routes
- `run_reflect_once(chat_id)` 已补齐真实委托，不再直连缺失方法

### B6. learning_routes 的 HTTP 请求契约已补强
- `POST /api/learning/reflect/run-once`

本轮已确认并收口为：
- 同时兼容 JSON body `{"chat_id": "..."}` 与旧的 query `?chat_id=...`
- body 优先于 query
- 当两者都缺失时，返回显式 `422 chat_id is required`

## 当前影响面结论

### 确认受影响
- 本报告曾覆盖的 standalone FastAPI backend 路由链显性断裂面
- 经 backend route 相关测试与最小路由调用复核，当前未再进入此前那组 `TypeError` / `AttributeError` 错误面
- 已补做真实 FastAPI 请求级 smoke，`/api/tools/recent-calls`、`/api/tools/chats/{chat_id}/recent-calls`、`/api/cognition/chats/{chat_id}/unified-timeline`、`/api/cognition/observability/overview`、`/api/cognition/scheduler/status`、`/api/learning/reflect/run-once` 均可返回 `status=ok`

### 不应直接判死
- `main.py` 注册的 AstrBot Plugin Page 主入口
- `astrmai/webui/plugin_pages.py` 通过 `AdminUiService` 等桥接的主页面路径

## 修正后的最终结论
- 旧版“5 个 blocking 全都真实存在”的方向是对的
- 但这些 blocking 当前已完成修复，不应继续记为“当前真实问题”
- 同时必须强调：
  - 这份报告验证的是 standalone FastAPI backend
  - 运行时聚合入口应以 `astrmai.webui.backend.routes` 包入口（`routes/__init__.py`）为准；同级 `backend/routes.py` 当前不是运行时主入口
  - 不应直接把 Plugin Page 主入口一并判为不可用
  - “这组 route/service 断裂已修复”也不自动等于“整个 standalone backend 已完成全量发布级验证”
