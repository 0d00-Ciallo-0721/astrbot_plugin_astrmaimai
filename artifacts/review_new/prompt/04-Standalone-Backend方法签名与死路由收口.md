# Prompt 04：Standalone Backend 方法签名与死路由收口

## 任务目标
在 Prompt 03 之后，继续收口 standalone FastAPI backend 的第二层断裂：route 调用了 service 中不存在或签名不兼容的方法。

这轮允许你做一个明确判断：
- 如果这批 route 仍是受支持入口，就修到可用。
- 如果某些 route 已经是 dead path 且不再受支持，可以在核实引用后做删减或下线。

## 必读报告
- `artifacts/review_new/07-模块-M7-页面入口与管理端链路.md`
- `artifacts/review_new/11-深度验证-M7-FastAPI断裂面.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 必读代码
- `astrmai/webui/backend/routes/cognition_routes.py`
- `astrmai/webui/backend/routes/learning_routes.py`
- `astrmai/webui/backend/routes/tools_routes.py`
- `astrmai/webui/backend/routes/routes.py` / 聚合注册入口
- 对应 service 文件
- `astrmai/webui/backend/server.py`

## 必须完成的修复
至少处理这些当前已确认问题：
- `tools_routes.py -> recent_tool_traces()` 缺失
- `learning_routes.py -> run_reflect_once()` 缺失
- `cognition_routes.py` 多个方法转发与 `CognitionService` 当前签名不兼容

## 实施要求
- 先查清这些 route 是否仍被受支持入口真实使用。
- 不要为了“让测试静音”而给 service 乱加空壳方法。
- 如果你决定删除/下线路由，必须先核实引用关系并同步更新测试。

## 验证要求
至少执行：
- backend route 相关测试
- 一次最小路由调用或导入验证，证明本轮断裂面已收住

## 完成标准
- 不再存在“route 直接调用 service 中不存在的方法”这类显性断裂
- `CognitionService` 相关 route 转发签名对齐
- 更新：
  - `artifacts/review_new/07-模块-M7-页面入口与管理端链路.md`
  - `artifacts/review_new/11-深度验证-M7-FastAPI断裂面.md`
  - 如影响总判断，再更新 `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

