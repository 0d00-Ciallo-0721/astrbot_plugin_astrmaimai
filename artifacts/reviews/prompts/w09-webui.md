# 开发窗口 09：WebUI 修复

## 必须先读取的审查报告
1. `artifacts/reviews/r08-webui.md` — 完整发现清单（5🔴 7🟡 5🟢）
2. `artifacts/reviews/r15-master.md` — 总报告

## 目标文件
- `astrmai/webui/backend/services/admin_ui_service.py` — 核心（~1148 行，God Object）
- `astrmai/webui/backend/services/memory_ui_service.py` — 记忆 UI
- `astrmai/webui/backend/services/persona_ui_service.py` — 人设 UI
- `astrmai/webui/backend/services/review_ui_service.py` — 审查 UI
- `astrmai/webui/backend/services/dashboard_service.py` — 仪表盘
- `astrmai/webui/backend/adapters/plugin_api.py` — API 适配器
- `astrmai/webui/backend/auth.py` — 认证
- `astrmai/webui/backend/routes/*.py` — 路由（8 个文件）

## 依赖
所有模块（WebUI 是系统最高层）

---

## 🔴 严重（5 项）

### P9-1：AdminUiService God Object 反模式
- **文件**：`astrmai/webui/backend/services/admin_ui_service.py:26-37`（及全文件 1148 行）
- **问题**：7 个领域服务类（`ObservabilityService`、`HeartflowService`、`CognitionService`、`ChatRuntimeService`、`SchedulerService`、`LearningService`、`ToolsService`）的构造函数仅保存 `plugin_api` 引用，其所有 `async` 方法均通过运行时 `import` 委托回 `AdminUiService`：
  ```python
  from .admin_ui_service import AdminUiService
  return await AdminUiService(self._api).xxx()
  ```
- **后果**：修改任一领域逻辑都需修改同一个 1148 行文件；运行时 import 绕过循环依赖但逻辑耦合极深
- **方案**：逐步拆分。优先级从最小的领域服务开始：
  1. `SchedulerService` — 方法最少，最容易独立
  2. `ToolsService` — 同上
  3. 改为直接调用 facade 方法，而非委托回 `AdminUiService`
  - 每完成一个领域服务就运行测试验证

### P9-2~P9-5：详见 r08-webui.md
（涵盖路由认证一致性、auth 中间件覆盖、MemoryUiService SQL fallback）

---

## 🟡 中等（7 项）

详见 `r08-webui.md`，重点：
- 8 个路由文件的认证装饰器是否统一
- `auth.py` 会话管理与 token 过期策略
- `MemoryUiService` 的 5 个 SQL fallback 迁移状态（D33 确认不修但需文档化）
- `plugin_api.py` 中 `_get_runtime()` 暴露（D27 — 独立窗口）
- `dashboard_service.py` 数据聚合性能

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_webui_backend_refactor.py tests/unit/webui/ -q
```

## 成功标准
- P9-1：至少拆分 1-2 个最小的领域服务（SchedulerService/ToolsService）
- 🔴 5 项优先修复
- WebUI 测试全部通过
