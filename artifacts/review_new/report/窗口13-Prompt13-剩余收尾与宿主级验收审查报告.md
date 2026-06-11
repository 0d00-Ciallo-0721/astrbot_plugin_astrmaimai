# 窗口13-Prompt13-剩余收尾与宿主级验收审查报告

## 审查结论

- 针对上一轮深度审查发现的 2 个残留问题，本轮已完成修复并补上针对性回归。
- 本轮复审后，**未发现新的 P1/P2 级残留缺陷**。
- 当前可将窗口 13 判定为：**本窗口无历史遗留问题**。

## 本轮修复内容

### 1. 修复 `runtime_bound` 判断失真
- `PluginApiAdapter` 新增统一的真实绑定判断 `has_bound_facade()`。
- WebUI backend 相关 service 不再直接使用 `self.plugin_api.facade is not None` 或 `self._api.facade is not None` 判定运行时绑定状态。
- 现在 `PluginApiAdapter()` 在没有 `ACTIVE_FACADE` 时会稳定返回 `runtime_bound = false`，不会再把 sentinel 误报成已绑定运行时。

### 2. 修复 facade 异常被静默吞掉的问题
- `PluginApiAdapter` 的 `_call_facade()` 不再吞掉已绑定 facade 的真实异常。
- `get_runtime_diagnostics()`、`get_capability_overview()`、`list_pending_reviews()`、`list_recent_reviews()`、`get_review_detail()`、`submit_review()` 也不再把真实 facade 故障伪装成 `{}` / `[]` / `ok`。
- 现在只有“无 facade / facade 缺少对应方法”的场景才会走受控降级；一旦是已绑定 facade 的真实运行时错误，会如实向上暴露。

### 3. 补充并收紧回归测试
- 新增回归覆盖：
  - 无 facade 时 `RuntimeUiService.runtime_status()` 必须报告 `runtime_bound = false`
  - 已绑定 facade 的运行时诊断异常不得被包装成正常 `ok` 数据
  - `ReviewUiService.list_pending()` 不得把运行时 review 故障伪装成空列表
  - `test_backend_route_safe_endpoints_construct_without_typeerror` 现在主动隔离 `ACTIVE_FACADE` 全局状态，避免测试依赖外部泄漏

## 复审核验结果

### 1. 宿主 / WebUI adapter 残留问题已关闭
- 生产代码中已不再残留 `facade is not None` 这一类 sentinel 误判写法。
- adapter 公共读接口已恢复“真实错误向上暴露、缺绑定时受控降级”的语义。
- 上一轮审查指出的两条问题均已被当前代码和新回归覆盖关闭。

### 2. Prompt 13 其他正向结论保持成立
- `EvolutionManager` 主链仍继续走 async API，未发现重新依赖同步包装。
- `config.py` 与 `_conf_schema.json` 的六个重点配置段在本轮复核范围内未发现新增断裂。
- 宿主级最小验收证据仍成立，且本轮 adapter 修复没有破坏原有 host mock / Plugin Page / backend smoke。

## 已执行验证

```powershell
python -m unittest tests.test_webui_backend_refactor.WebuiBackendRefactorTests.test_runtime_ui_service_reports_unbound_when_no_facade_resolved tests.test_webui_backend_refactor.WebuiBackendRefactorTests.test_runtime_ui_service_does_not_mask_bound_facade_failures_as_ok_data tests.test_webui_backend_refactor.WebuiBackendRefactorTests.test_review_ui_service_does_not_mask_bound_runtime_failures_as_empty_pending_list tests.test_webui_backend_refactor.WebuiBackendRefactorTests.test_backend_route_safe_endpoints_construct_without_typeerror -q
python -m unittest tests.integration.host.test_host_mock_validation tests.test_plugin_pages_admin_refactor tests.test_webui_backend_refactor -q
python -m unittest tests.test_learning_refactor tests.test_planner_side_inputs_refactor tests.test_planning_input_loader_refactor tests.test_webui_backend_refactor tests.test_plugin_pages_admin_refactor tests.integration.host.test_host_mock_validation tests.unit.conversation.test_context_runtime_wiring tests.test_executor_refactor tests.test_sensors_refactor -q
python -m pytest tests/test_config_standalone_refactor.py tests/test_infrastructure_settings_refactor.py tests/test_persona_context_refactor.py tests/unit/learning/test_mining_helpers_migrated.py tests/original_ported/test_expression_governance_ported.py -q
```

结果摘要：

- 新增 WebUI / adapter 针对性回归：`4 tests OK`
- host mock / Plugin Page / backend WebUI：`57 tests OK (1 skipped)`
- 组合回归补证：`122 tests OK (1 skipped)`
- 配置链与消费链：`46 passed`

## 边界说明

- 本报告说明的是窗口 13 在当前插件仓库范围内已完成实现、回归和深度复审闭环。
- 本轮仍未把结论扩大成“真实外部 AstrBot 安装环境 + live provider 的完整发布级验收已完成”。
- 该边界属于发布前更高层级验证问题，不再属于窗口 13 的仓内历史遗留问题。

## 最终判定

- 当前窗口可判定为**无历史遗留问题**。
- `artifacts/review_new/report/窗口12-剩余开发项清单.md` 中与 Prompt 13 相关的收尾项，当前已具备代码、测试和复审三重闭环证据。
