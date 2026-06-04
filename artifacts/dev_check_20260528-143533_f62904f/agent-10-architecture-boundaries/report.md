# Agent 10

Agent ID:
`019e6d5c-5e61-7ca3-b080-f0401d0a60fa`

状态：
已完成

模块：
`conversation/ingress`、`presentation/events`、`webui/backend/services`、`webui/plugin_pages`、`app/runtime_context`、`infrastructure/compat`

职责：
消息入口编排、WebUI 管理/诊断接口、运行时上下文装配、旧接口兼容桥

关键文件：
[presentation/dto/message_scope.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/presentation/dto/message_scope.py:1)，[conversation/ingress/command_guard.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/command_guard.py:1)，[presentation/events/message_entry.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/presentation/events/message_entry.py:1)，[webui/backend/services/admin_ui_service.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/admin_ui_service.py:1)，[webui/backend/services/memory_ui_service.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/memory_ui_service.py:1)，[webui/backend/adapters/plugin_api.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/adapters/plugin_api.py:1)，[webui/plugin_pages.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/plugin_pages.py:600)，[app/runtime_context.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/runtime_context.py:139)，[infrastructure/compat/legacy_compat.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/compat/legacy_compat.py:1)

现有测试：
[tests/regression/architecture/test_import_boundaries_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/regression/architecture/test_import_boundaries_refactor.py:1)，[tests/regression/architecture/test_memory_runtime_boundaries_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/regression/architecture/test_memory_runtime_boundaries_refactor.py:1)，[tests/regression/architecture/test_directory_contracts_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/regression/architecture/test_directory_contracts_refactor.py:1)，[tests/regression/architecture/test_shared_test_support_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/regression/architecture/test_shared_test_support_refactor.py:1)  
实跑：`$env:PYTHONPATH='.'; pytest tests/regression/architecture -q`，`31 passed, 4 warnings`

主要发现：
1. `webui/backend/services` 仍在直接碰 persistence 和 runtime 内部细节。`DashboardService`、`UserUiService`、`ReviewUiService`、`MemoryUiService`、`AdminUiService` 都直接持有 `db_factory`/`sqlite3` 并操作真实表或数据库文件，例如 `canonical_memories`、`user_profiles`、`MemoryEvent`、`default_db_path()`；同时 `AdminUiService`、`MemoryUiService`、`PersonaUiService`、`ReviewUiService` 通过 `PluginApiAdapter.get_runtime()` 下钻 `memory_engine`、`gateway`、`runtime_coordinator`、`proactive_task`、`observability_hub` 等内部对象。现有架构测试只限制 namespace，不限制这类直接 SQL 和 runtime 属性钻取。
2. 临时 compat 桥已经明显长期化。`legacy_compat.py` 仍被生产代码直接使用，调用点在 `conversation/attention/gate.py`、`conversation/planning/prompt_refiner.py`、`conversation/planning/planner_prompt_context.py`、`conversation/execution/reply_service.py`、`conversation/execution/reply_artifact_builder.py`；`PluginRuntimeContext` 还通过 `LEGACY_RUNTIME_ATTRS` + `export_legacy_attrs()` 向宿主导出大面积旧属性；`PluginApiAdapter` 公开 `get_runtime()`，并在 `apply_config()` 里直接改 `runtime.raw_config`、`runtime.config`、`rebuild_infrastructure_settings()`；`app/plugin_facade.py:26` 与 `webui/plugin_pages.py:604` 都会写全局 `ACTIVE_FACADE`。这已经不是一次性迁移桥，而是被测试和调用链共同固化的运行时通道。
3. 存在明确的模块反向依赖，而且当前架构测试没有覆盖。`conversation/ingress/command_guard.py`、`dedupe.py`、`permission_guard.py`、`external_result_bridge.py`、`poke_handler.py` 反向 import `presentation.dto.message_scope`，形成 `conversation -> presentation` 依赖；同时 `presentation/events/message_entry.py` 直接驱动 `runtime.group_reply_wait_manager`、`chat_loop_kernel`、`reflect_tracker`、`evolution`、`host_bridge` 等 runtime 内部对象。测试目前只检查 `presentation` 不碰 `persistence`，没有约束这类 runtime/internal 直连或 `conversation -> presentation` 反向边界。

未实现/不完整项：
1. 缺少针对 `conversation/* -> presentation/*`、`presentation` 直读 runtime 内部、`webui/backend/services` 直接 SQL/数据库文件访问的架构回归测试；当前测试主要是在给现有逃生口做 allowlist。
2. 尚未看到替代 `ACTIVE_FACADE`、`PluginApiAdapter.get_runtime()`、`LEGACY_RUNTIME_ATTRS`、`legacy_compat.py` 的收口接口；测试文本里已经把 `get_runtime` 定义成“temporary compatibility seam”，但仓内没有看到继续收缩这条 seam 的机制。

高风险点：
1. God Object 持续膨胀：`AdminUiService` `1132` 行 / `76` 方法，`MemoryUiService` `843` 行 / `49` 方法，`MemoryEngine` `861` 行 / `45` 方法，`PluginRuntimeContext` `447` 行 / `53` 方法。边界职责已经混入运行时诊断、聊天编排、持久化、迁移、主动任务、兼容导出多个方向。
2. 全局 facade + 直接 runtime/DB 变更的组合风险很高。`plugin_facade`、`plugin_pages` 双写 `ACTIVE_FACADE`，`plugin_api` 热改 live runtime 配置，而 WebUI service 又直接操作数据库；一旦 standalone WebUI、Plugin Pages、宿主 runtime 状态不一致，问题会跨层扩散且很难被现有测试发现。

建议下一步：
1. 先补架构回归测试，优先禁止 `conversation` import `presentation`，禁止 `webui/backend/services` 直接使用 `sqlite3`/`db_factory`/`default_db_path()`/`get_runtime()` 访问内部细节，把现在的隐式允许面显式收紧。
2. 设计真正的边界替代层：把 WebUI 需要的 runtime 诊断、memory 管理、review 管理收敛到 facade/DTO 或 application service；把 `MessageScope`/`IngressDecision` 下沉到 `conversation.contracts` 或 `shared`；逐步删除 `ACTIVE_FACADE`、`LEGACY_RUNTIME_ATTRS`、`legacy_compat.py`。
