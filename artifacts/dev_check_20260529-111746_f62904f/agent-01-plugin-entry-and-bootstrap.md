# Agent 01

Agent ID:
`019e71b7-7621-79a0-9f65-e8be74efcbba`

状态：
已完成

模块：
插件入口与装配层（`main.py` / `astrmai.app` 装配链）

职责：
当前入口层主要负责装配、委派和兼容导出；`main.py` 职责膨胀本轮未复现。`compat/export` 扩面本轮也未复现，但兼容桥仍然存在。

关键文件：
[main.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/main.py:33>)、[bootstrap.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/bootstrap.py:64>)、[plugin_facade.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/plugin_facade.py:15>)、[runtime_context.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/runtime_context.py:105>)、[lifecycle.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/lifecycle.py:14>)、[plugin_api.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/adapters/plugin_api.py:13>)、[plugin_pages.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/plugin_pages.py:601>)

现有测试：
`tests/unit/conversation/test_context_runtime_wiring.py`、`tests/integration/runtime/test_runtime_contracts_migrated.py`、`tests/test_webui_backend_refactor.py`、`tests/test_plugin_pages_admin_refactor.py`。

验证：
已实际执行 `PYTHONPATH=. pytest tests/unit/conversation/test_context_runtime_wiring.py tests/integration/runtime tests/test_webui_backend_refactor.py tests/test_plugin_pages_admin_refactor.py -q`，结果 `69 passed`。

主要发现：
1. `life.enable_proactive` 目前会被当成“可热应用配置”，但运行时不会补建/启动 `proactive_task`，会造成配置页状态与真实运行状态漂移。[plugin_api.py:157](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/adapters/plugin_api.py:157>) 没把 `life.*` 纳入 `reload_required`，而 [bootstrap.py:329](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/bootstrap.py:329>) 只在 bootstrap 时创建 `ProactiveTask`，[lifecycle.py:70](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/lifecycle.py:70>) 也只在启动阶段 `start()`。只读动态验证里，`apply_config({'life': {'enable_proactive': True}})` 返回 `runtime_bound=True`，但 `runtime.proactive_task is None`。
2. `terminate()` 清理链路还不够异常安全；`memory_pipeline.stop()`、`cron_guard.stop()`、`unsubscribe_all_events()` 任一处抛错，后续任务取消、状态复位和 `shutdown.complete` 都会被短路。[lifecycle.py:152](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/lifecycle.py:152>) 缺少总的 `try/finally` 保护。现有测试只覆盖了全成功路径 [test_context_runtime_wiring.py:371](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/unit/conversation/test_context_runtime_wiring.py:371>)，没有失败分支验证。
3. 插件页“最新 facade 绑定”本轮未复现，`register_astrmai_admin_pages()` 的幂等+最新 facade 行为已有测试；`LEGACY_RUNTIME_ATTRS` 扩面也被架构测试冻结。但 `ACTIVE_FACADE` / `APPLY_STATUS` 仍是进程级全局单例 [plugin_api.py:13](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/adapters/plugin_api.py:13>)、[plugin_api.py:195](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/adapters/plugin_api.py:195>)；只读实测里，第二个 `PluginApiAdapter` 会读到第一个 adapter 的 `apply_status`，多实例/重载场景仍有状态串味风险。

未实现/不完整项：
1. 没有覆盖 `life.enable_proactive` / `life.dream_visible` 热配置路径的测试。
2. `tests/integration/runtime` 实际只验证 `runtime_contracts` 数据结构，并没有对 `main -> facade -> lifecycle` 的真实启动/终止链路做集成验证。

高风险点：
1. WebUI/配置层可能显示“已开启主动模式”，但后台并没有创建或启动 `proactive_task`。
2. 一旦 shutdown 某一环抛异常，当前实现可能留下未完全清理的后台任务或状态标记；再叠加进程级 `APPLY_STATUS`，重载后的诊断信息容易失真。

建议下一步：
1. 把 `life.enable_proactive` 明确纳入 `reload_required`，或者实现真正的 runtime 级 `ProactiveTask` 热创建/热停止，并补一条配置热切换测试。
2. 给 `terminate()` 增加分段 `try/finally` 清理，并补失败路径测试；同时把 `APPLY_STATUS` 从进程级全局收敛到 facade/runtime 级，至少在绑定/终止时显式重置。
