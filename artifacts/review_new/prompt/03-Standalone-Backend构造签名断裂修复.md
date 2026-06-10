# Prompt 03：Standalone Backend 构造签名断裂修复

## 任务目标
修复 standalone FastAPI backend 中最直接的 route/service 构造参数断裂。

本轮只聚焦：
- route 多传 `get_db`
- service 实际只接收 `plugin_api`

不要在这一轮处理 route 调用了不存在方法的问题，那是下一轮。

## 必读报告
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`
- `artifacts/review_new/07-模块-M7-页面入口与管理端链路.md`
- `artifacts/review_new/11-深度验证-M7-FastAPI断裂面.md`

## 必读代码
- `astrmai/webui/backend/routes/cognition_routes.py`
- `astrmai/webui/backend/routes/runtime_routes.py`
- `astrmai/webui/backend/routes/heartflow_routes.py`
- `astrmai/webui/backend/routes/learning_routes.py`
- `astrmai/webui/backend/routes/tools_routes.py`
- 对应的：
  - `services/cognitionservice.py`
  - `services/observabilityservice.py`
  - `services/heartflowservice.py`
  - `services/learningservice.py`
  - `services/toolsservice.py`

## 必须完成的修复
1. 消除 route/service 构造参数不匹配。
2. 明确每个 `_service()` 的真实依赖形态。
3. 如果某个 service 确实不需要 `get_db`，就不要伪造参数兼容。

## 实施要求
- 优先保持最小改动。
- 不要在本轮里顺手补不存在的业务方法。
- 不要扩大到 Plugin Page 主桥接链以外。

## 验证要求
至少执行：
- 与 backend routes 相关的测试
- 一次最小导入 / 路由构造验证，确认这些 route 文件不再因构造参数直接炸掉

优先考虑：
- `tests/test_webui_backend_refactor.py`
- 任何直接覆盖 routes 聚合注册的测试

## 完成标准
- 五个 route 文件不再把 `get_db` 多传给只接收 `plugin_api` 的 service
- 相关测试通过
- 修复后同步更新：
  - `artifacts/review_new/07-模块-M7-页面入口与管理端链路.md`
  - `artifacts/review_new/11-深度验证-M7-FastAPI断裂面.md`

