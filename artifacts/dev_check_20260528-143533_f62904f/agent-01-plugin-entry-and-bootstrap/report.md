# Agent 01

Agent ID:
`019e6d48-1dde-7962-a424-d89bbdad59a5`

状态：
已完成

模块：
插件入口与装配层

职责：
`main.py` 负责 AstrBot 插件入口、hook/命令注册与 runtime/facade 装配；`bootstrap/runtime_context/lifecycle/plugin_facade` 负责运行时组装、生命周期控制、compat 导出与 WebUI 绑定。

关键文件：
[main.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/main.py:33>)、[bootstrap.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/bootstrap.py:64>)、[plugin_facade.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/plugin_facade.py:15>)、[runtime_context.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/runtime_context.py:105>)、[lifecycle.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/lifecycle.py:14>)、[plugin_pages.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/plugin_pages.py:600>)、[plugin_api.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/adapters/plugin_api.py:13>)

现有测试：
[tests/unit/conversation/test_context_runtime_wiring.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/unit/conversation/test_context_runtime_wiring.py:1>)、[tests/test_plugin_pages_admin_refactor.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_plugin_pages_admin_refactor.py:1>)、[tests/test_main_reverse_session_hook_refactor.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_main_reverse_session_hook_refactor.py:1>)、[tests/test_presentation_commands_refactor.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_presentation_commands_refactor.py:1>)、[tests/test_webui_backend_refactor.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_webui_backend_refactor.py:951>)、[tests/integration/runtime/test_runtime_contracts_migrated.py](<C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/integration/runtime/test_runtime_contracts_migrated.py:1>)。已实跑两组：相关入口/装配测试 28 通过，`apply_config` 相关单测 1 通过。

主要发现：
1. 热配置链存在明确的“配置态/运行态漂移”风险。`PluginApiAdapter.apply_config()` 会直接替换 `runtime.config` 并调用 `rebuild_infrastructure_settings()`（`plugin_api.py:168-202`），但 `PluginRuntimeContext._refresh_live_config_refs()` 只刷新已有对象上的 `config/settings` 引用，不会重建或清空 `dialogue_store`、`context_compaction`、`sys3_router`、`cron_guard`、`visual_cortex`、`proactive_task`（`runtime_context.py:163-214`）；这些对象却是在 bootstrap 阶段一次性创建的（`bootstrap.py:131-165`, `182-197`, `317-351`）。结果是 WebUI/runtime diagnostics 可能同时看到“feature flag 已关闭”和“旧对象仍存在/仍可汇报状态”的混合状态（`runtime_context.py:382-447`）。现有测试只验证了 `rebuild_infrastructure_settings()` 被调用（`tests/test_webui_backend_refactor.py:951-977`），没有覆盖热切换后的对象一致性。
2. `terminate()` 的清理对状态位不对称，退出后诊断信息会残留“已启动”标记。启动流程会把 `memory_initialized`、`proactive_started`、`visual_started`、`cron_guard_started` 置为 `True`（`lifecycle.py:35-115`），但终止流程只重置了 `is_running`、`lifecycle_started` 和 `boot_phase`，没有把上述状态位清回去（`lifecycle.py:152-190`）。而管理页的 runtime status 直接透传 `get_runtime_diagnostics()`（`admin_ui_service.py:148-153`），`build_help_text()` 也会消费这些位（`plugin_facade.py:108-135`），所以停机/重载后页面与帮助文案都可能继续显示某些组件“仍在运行”。
3. 插件页绑定还留着一条条件性高风险链。`PluginFacade` 初始化时会写全局 `ACTIVE_FACADE`（`plugin_facade.py:16-20`），`register_astrmai_admin_pages()` 又会再次 `set_active_facade(facade)`，并把每个 handler 以闭包形式绑定到当前 `facade`（`plugin_pages.py:600-605`, `724-732`）。仓库内没有任何 `unregister_web_api`/覆盖注册逻辑，现有测试也只覆盖“单次注册成功”，不覆盖 reload/重复注册（搜索结果仅命中 `plugin_pages.py` 与测试桩）。如果宿主的 `register_web_api` 是追加而非覆盖，旧 handler 就可能继续抓着旧 facade/旧 runtime。

未实现/不完整项：
1. 没有看到针对 `terminate()` 的单测，尤其缺少“状态位复位、`ACTIVE_FACADE` 释放、后台任务收尾、重复初始化后状态一致”的验证。
2. `bind_host_plugin()/sync_host_compat_attrs()` 这条 compat 同步链没有对应测试；`LEGACY_RUNTIME_ATTRS` 也仍然很宽（`runtime_context.py:472-505`），当前只有限制“不要继续往 WebUI/observability 扩面”的架构测试，没有验证热配置后 host 侧 compat 属性是否仍然一致。

高风险点：
1. 运行中切换 `conversation.*`、`sys3.*`、`vision.*`、`life.*` 一类配置时，页面展示、capability 概览与真实运行对象可能不一致，严重时只能靠整插件重载纠偏。
2. 若 AstrBot 宿主对同路径 Web API 采用追加注册，插件 reload 后可能出现旧页面 handler 指向旧 facade 的悬挂状态。

建议下一步：
1. 增加一组 app-layer 热配置测试，直接覆盖 `apply_config -> rebuild_infrastructure_settings` 后 `dialogue_store/context_compaction/sys3/proactive/vision` 的对象存在性、capability 输出、host compat 属性是否一致；对不支持热切换的项，明确改成“只写配置并返回 `reload_required`，不改 live runtime”。
2. 增加 `PluginLifecycleManager.terminate()` 与插件页重复注册测试，至少断言状态位复位、`ACTIVE_FACADE` 释放，以及 reload 后不会保留旧 facade/旧 route handler。
