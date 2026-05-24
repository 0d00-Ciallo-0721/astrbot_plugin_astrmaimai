# 窗口 1：插件入口与装配层

模块：
插件入口与装配层（`main.py` / `astrmai.app`）

职责：
构建 `PluginRuntimeContext`，把 runtime、facade、lifecycle 接到 AstrBot 插件入口、命令、事件钩子和 WebUI 适配层。

关键文件：
- `main.py`
- `astrmai/app/runtime_context.py`
- `astrmai/app/bootstrap.py`
- `astrmai/app/plugin_facade.py`
- `astrmai/app/lifecycle.py`
- `astrmai/webui/backend/adapters/plugin_api.py`
- `astrmai/infrastructure/runtime/event_bus.py`
- `astrmai/state/group_wait/group_reply_wait_manager.py`

现有测试：
- `tests/unit/conversation/test_context_runtime_wiring.py`
- `tests/integration/runtime/test_runtime_contracts_migrated.py`
- 实跑：`python -m pytest tests/unit/conversation/test_context_runtime_wiring.py tests/integration/runtime/test_runtime_contracts_migrated.py -q`
- 结果：`19 passed`

主要发现：
1. 旧兼容桥仍然偏重，而且已经开始制造运行时一致性债。
   - 依据：`main.py:43` 通过 `_apply_runtime_compat()` 把 `export_legacy_attrs()` 返回的大量旧字段重新挂回插件实例；`astrmai/app/runtime_context.py:383` 的导出面包含 `config/raw_config/_background_tasks` 与 30+ 个服务对象。
   - 进一步依据：`astrmai/webui/backend/adapters/plugin_api.py:171` 热配置只替换 `runtime.config/raw_config` 并 `rebuild_infrastructure_settings()`，不会重新 export compat，也不会刷新已构造服务里的 `config` 引用。
2. `PluginFacade` 仍带有隐藏式总管特征，职责已经超出薄门面。
   - 依据：`astrmai/app/plugin_facade.py:15` 同时承载生命周期触发、review API、命令识别、`/work` 直入 Sys3、运行时诊断、WebUI `ACTIVE_FACADE` 注册和 `system2_callback` 绑定。
   - 进一步依据：`astrmai/app/plugin_facade.py:228` 虽然线上优先转发到 `runtime.system2_runner.run()`，但 facade 内仍保留一整套后备执行/跟进逻辑，和 `astrmai/conversation/execution/followup_manager.py:34` 形成重复路径。
3. 未发现直接 `import` 循环，但存在顺序敏感的运行时依赖环。
   - 依据：`astrmai/app/bootstrap.py:250`、`astrmai/app/bootstrap.py:381` 先把 `AttentionGate.system2_callback` 绑定成闭包桥；真正的 `runtime.system2_callback` 要到 `astrmai/app/plugin_facade.py:18` 才绑定。
   - 风险：绕过 `main.py` 正常装配路径时，可能触发 `System2 callback has not been bound yet`。

未实现/不完整项：
1. `terminate()` / 重载路径缺少对应测试。
   - 缺口：未覆盖 `AstrMaiPlugin.terminate()`、`PluginLifecycleManager.terminate()`、`ACTIVE_FACADE` 解绑、`EventBus` worker 回收。
2. 热配置测试没有验证已构造服务是否同步刷新。
   - 依据：`tests/test_webui_backend_refactor.py:951` 只验证 `rebuild_infrastructure_settings()` 被调用，没有覆盖 `model_gateway.py:28`、`lane_manager.py:55`、`gate.py:59`、`proactive_task.py:37` 这类 bootstrap 时持有 `config` 的对象。

高风险点：
1. `terminate()` 存在明确漏清理窗口。
   - 依据：`astrmai/app/lifecycle.py:168` 只收口 `runtime.iter_task_owners()` 返回的 owner。
   - 进一步依据：`astrmai/infrastructure/runtime/event_bus.py:162` 会懒启动长期 worker 并持有 `_background_tasks`；`astrmai/state/group_wait/group_reply_wait_manager.py:35` 也会创建 `_timeout_tasks`，但不在 terminate 收口面内。
2. WebUI 热应用配置后的行为可能“部分新、部分旧”。
   - 依据：`plugin_api.py:171` 只替换 `runtime.config/raw_config`，而 `model_gateway.py:28`、`lane_manager.py:55`、`private_chat_manager.py:23`、`gate.py:59`、`proactive_task.py:37` 这类核心服务已经把 `config` 固化在实例字段里。

建议下一步：
1. 先补入口层生命周期/重载测试，覆盖 `terminate()` 后 `EventBus`、group wait、`ACTIVE_FACADE` 的清理，以及热配置后 plugin 实例字段、runtime、已构造服务三者是否一致。
2. 收缩 compat 面和 facade 面：压缩 `LEGACY_RUNTIME_ATTRS` 到最小集合，并拆出 `PluginFacade` 中的 system2 后备逻辑和全局激活逻辑。
