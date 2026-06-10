# 窗口3-Prompt03-04-Standalone-Backend断裂收口审查报告

## 审查结论
- 本次针对 Prompt03 / Prompt04 范围内的 standalone backend route/service 断裂链做了代码审查与复核。
- 在本窗口范围内，未再发现“route 多传构造参数”“route 直接调用 service 中不存在的方法”“route 调用 service 时签名不兼容”“影子聚合入口继续双份漂移”“learning reflect HTTP 契约未被测试锁死”这类历史遗留问题。
- 结论：**本窗口审查通过，可视为已收口。**

## 审查范围
- `astrmai/webui/backend/routes/cognition_routes.py`
- `astrmai/webui/backend/routes/runtime_routes.py`
- `astrmai/webui/backend/routes/heartflow_routes.py`
- `astrmai/webui/backend/routes/learning_routes.py`
- `astrmai/webui/backend/routes/tools_routes.py`
- `astrmai/webui/backend/routes.py`
- `astrmai/webui/backend/services/cognitionservice.py`
- `astrmai/webui/backend/services/learningservice.py`
- `astrmai/webui/backend/services/toolsservice.py`
- `tests/test_webui_backend_refactor.py`
- `artifacts/review_new/07-模块-M7-页面入口与管理端链路.md`
- `artifacts/review_new/11-深度验证-M7-FastAPI断裂面.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 已确认结果
- 5 个目标 route 的 `_service()` 都已改为只传 `PluginApiAdapter()`，不再多传 `get_db`。
- `LearningService.run_reflect_once()`、`ToolsService.recent_tool_traces()` 已补齐为真实委托，不是空壳兼容。
- `CognitionService` 已补齐并对齐当前 route 真实调用的 turn-trace、unified-timeline、observability、context-economy、scheduler 相关方法签名。
- `POST /api/learning/reflect/run-once` 当前同时兼容 JSON body 与 query `chat_id`，并已验证 body 与 query 同时存在时以 body 为准。
- `astrmai/webui/backend/routes.py` 当前已收敛为转发到包入口 `astrmai.webui.backend.routes` 的 shim，不再保留第二份 router 装配实现。
- Plugin Page 与前端中仍在真实使用的相关 backend 能力，当前没有复现直接 `AttributeError` / `TypeError` 断裂。
- `tests/test_webui_backend_refactor.py` 已补做真实 FastAPI 请求级 smoke，`tests/unit/webui/test_w10_webui_plan_migrated.py` 也已锁定 `routes.py` 必须保持 shim 形态。

## 验证记录
- 运行：`python -m pytest tests/test_webui_backend_refactor.py tests/unit/webui -q`
  - 结果：`56 passed, 4 warnings`
- 补做了 Prompt03/04 范围内 route 的真实请求级与最小调用复核，确认此前会报错的这些入口现在都能返回 `status=ok`：
  - `learning/reflect/run-once`
  - `tools/recent-calls`
  - `tools/chats/{chat_id}/recent-calls`
  - `cognition/chats/{chat_id}/turns`
  - `cognition/chats/{chat_id}/unified-timeline`
  - `cognition/observability/*`
  - `cognition/scheduler/*`
- 额外复核：`POST /api/learning/reflect/run-once?chat_id=query-id` 携带 body `{"chat_id":"body-id"}` 时，实际执行的是 `body-id`，与本窗口约定契约一致。

## 备注
- 本报告只覆盖窗口3对应的 Prompt03 / Prompt04 范围，不等同于整个仓库所有问题已清零。
- 当前仓库其余未在本窗口处理的配置链、并发保护、记忆过滤等问题，仍应以后续窗口结论为准。
