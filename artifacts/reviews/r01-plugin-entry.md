# 审查报告：astrmai/app/ + main.py
> task_id: r12-plugin-entry | 审查时间: 2025-07-08T10:00:00

## 概述
- 审查文件数: 7 (bootstrap.py, plugin_facade.py, runtime_facade_protocol.py, lifecycle.py, runtime_context.py, __init__.py, main.py)
- 发现总数: 22
- 严重: 3 | 中等: 11 | 建议: 8

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | **astrmai/app/lifecycle.py:211-216** | **_reset_runtime_status_flags 未复位所有启动期标志**。`bootstrap_completed`、`boot_logged`、`work_mode_enabled` 三个 RuntimeStatus 字段在 shutdown 时未被重置。如果 PluginRuntimeContext 被复用或热重启，这些残余值为 True 会导致诊断显示"bootstrap 已完成"但实际并未重新引导。终止逻辑将 `is_running` 和 `lifecycle_started` 放在方法外直接赋值为 False，而其余标志在方法内复位——两种复位方式散落两处，易因代码重排遗漏。 |
| 2 | **astrmai/app/plugin_facade.py:105-106** | **submit_expression_review 参数类型与 Protocol 不一致**。Protocol (`runtime_facade_protocol.py:80`) 声明 `pattern_id: str`，但 PluginFacade 实现为 `pattern_id: int`。由于 `Protocol` 是结构子类型，mypy/pyright 会在静态检查时报类型不兼容。若调用方按 Protocol 类型传入字符串（例如从 HTTP 路由解析的 `"123"`），实现方接收 int 会导致运行时 TypeError 或隐式转换失败。 |
| 3 | **astrmai/app/lifecycle.py:56-57** | **on_program_start 执行期间 is_running 尚未设为 False 的竞态窗口**。`terminate()` 执行时先设置 `is_running = False` 再取消后台任务；但 `is_running` 默认值在 `RuntimeStatus` 中为 `True`，后台循环 (`_db_sync_task`、`_memory_gc_task`) 直到 `terminate()` 被调用前都以 `is_running` 为 True 持续运行。如果 `on_program_start` 抛出异常或启动中途被宿主框架中断，后台任务会继续运行而不会收到终止信号，造成悬挂协程。 |

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | **astrmai/app/bootstrap.py:40-85** | **PluginBootstrap.build() 中 system2_callback 双向绑定存在时序脆弱性**。AttentionGate 初始化时通过 `_build_system2_bridge` 传入一个闭包，该闭包在真正被调用时依赖 `runtime.system2_callback` 已由 PluginFacade.__init__ 设置。在 `build()` 返回至 `PluginFacade.__init__` 设置 callback 之间的时间窗口内，若有消息到达触发 AttentionGate 的 system2 调用，将抛出 `RuntimeError("System2 callback has not been bound yet.")`。这是一个小概率但真实的竞态条件。 |
| 5 | **astrmai/app/bootstrap.py:249-250** | **_build_proactive_task 中构造后立即覆写自身属性为 None 是脆弱模式**。`proactive_task.auto_check_task = None`、`proactive_task.reflect_tracker = None`、`proactive_task.review_dispatcher.reflect_tracker = None` —— 这三行在对 ProactiveTask 无公开契约的情况下直接穿透访问其内部属性。如果 ProactiveTask 的构造函数未来改变这些字段的初始值方式，这些覆写可能变得无效或破坏内部状态。建议在 ProactiveTask 上提供显式方法（如 `clear_redundant_refs()`）或通过构造参数控制。 |
| 6 | **astrmai/app/lifecycle.py:154-157** | **terminate() 中 is_running / lifecycle_started 在 _reset_runtime_status_flags 之前直接赋值，复位逻辑散落两处**。`is_running = False` 和 `lifecycle_started = False` 在 `_reset_runtime_status_flags()` 方法外赋值，而其余 5 个标志在方法内复位。如果未来重构调换调用顺序（先调用 `_reset_runtime_status_flags` 再设置 is_running），会导致 is_running/lifecycle_started 永远不被复位。建议将所有标志复位统一到 `_reset_runtime_status_flags` 方法中。 |
| 7 | **astrmai/app/bootstrap.py:76-86** | **bootstrap.build() 设置 bootstrap_completed = True 后，若后续构造的组件（如 ProactiveTask）异常，状态已标记完成但组件实际 degraded**。`runtime.status.bootstrap_completed = True` 在第 85 行设置，但 `_build_proactive_task` 中可能因 `_record_optional_failure` 记录 degraded 而不会阻止 bootstrap_completed。调用方可能误认为所有组件都已就绪。建议在设置 completed 前汇总所有 degraded 状态，或增加 `bootstrap_with_errors` 之类的补充标志。 |
| 8 | **astrmai/main.py:20-22** | **main.py 中 check_command_access 同时存在两条调用路径**。`mai_help`（第 97 行）和 `enter_sys3_direct`（第 133 行）直接调用模块级 `check_command_access(self.runtime, event)`，而 `on_global_message`（第 127 行）通过 `self.facade.on_global_message(event)` 间接调用 `PluginFacade.check_command_access`。两条路径语义相同，但 import 路径多出一条未使用的顶层 import（第 12/21 行），且绕过 facade 的调用方式不利于统一切面（如日志、指标埋点）。 |
| 9 | **astrmai/app/runtime_facade_protocol.py:107-196** | **Protocol 中 26 个 `get_*` 方法均返回 `Any`，丢失类型信息**。如 `get_memory_engine() -> Any` 而非 `-> MemoryEngine`。虽然这在大型重构中是可接受的过渡策略，但长期会削弱 Protocol 作为契约的价值。每新增一个 `get_*` 方法时应有对应的类型返回标注。 |
| 10 | **astrmai/app/plugin_facade.py:124-125** | **PluginFacade.get_chat_loop_kernel() 存在两个 fallback 路径但无缓存**。每次调用都执行 `getattr(self.runtime, "chat_loop_kernel", None)` 检查，若不命中再调 `self.get_proactive_task()` 并再次 getattr。高频调用路径（如诊断刷新）中每次重新解析属性有轻微性能损失，且若 ProactiveTask 中途被替换（热重启场景），每次返回不同对象可能造成调用方混淆。 |
| 11 | **astrmai/app/plugin_facade.py:66** | **set_active_facade(self) 在 try 块中静默忽略所有异常**。若 `webui.backend.adapters.plugin_api` 导入失败（如 WebUI 组件未部署），Facade 的 WebUI 绑定静默失效。虽不影响核心功能，但若 WebUI 后续查询 facade 会返回 None 或旧实例，难以排查。建议降级日志记录一个 `logger.debug` 级别的消息。 |
| 12 | **astrmai/app/lifecycle.py:96-101** | **load_command_metadata 依赖可选传感器且静默跳过**。方法体第一行检查 `if not self.runtime.sensors or not hasattr(self.runtime.sensors, "load_foreign_commands"): return`。如果 `self.runtime.sensors` 为 None（非典型但可能发生在 degraded 状态），该方法无提示地返回。调用方（on_program_start）无法区分"传感器已加载"和"传感器不存在"。 |
| 13 | **astrmai/app/runtime_context.py:55-63** | **CoreServices 等 @dataclass(slots=True) 类所有字段默认值为 None，类型为 Any**。这使得 IDE 无法提供有意义的补全和类型检查，且 `slots=True` 阻止动态赋值（虽然后续代码确实通过属性赋值而非动态赋值）。建议在重构稳定后为每个字段加上具体类型，至少使用 `Optional[ConcreteType]`。 |
| 14 | **astrmai/app/runtime_context.py:220** | **sync_host_compat_attrs 方法每次调用都会遍历全部 LEGACY_RUNTIME_ATTRS**。如果被高频调用（如 `apply_hot_config` 中调用了它），会在每次配置热更新时对 32 个属性执行 getattr + setattr，包含若干 `None` 值的属性。建议缓存上次的非 None 结果或仅增量更新。 |

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 15 | **astrmai/app/bootstrap.py:23-34** | **PluginBootstrap 各 `_build_*` 方法可进一步拆分**。如 `_build_core_services` 方法（第 93-118 行）内部调用了 6 个二级构建方法，参数传递层级较深（`runtime` 对象被反复传递）。D24/D25 注释已承认跨服务接线存在，建议在后续迭代中为每个服务引入独立的"接线器"类。 |
| 16 | **astrmai/main.py:12** | **ImportError fallback 路径中的 import 语句未使用 `from __future__ import annotations`**。主 try 块和 except 块各有一套 import，后者缺少 `from __future__ import annotations`。Python 3.12+ 中无实际影响，但对工具链（如 isort、autoflake）可能造成误判。 |
| 17 | **astrmai/app/runtime_context.py:229-258** | **export_legacy_attrs 函数选择性跳过 None 值属性**。`if value is not None: attrs[name] = value` 这一过滤逻辑意味着导入该函数的主机插件不会收到任何值为 None 的属性，可能导致主机插件中依赖这些属性存在的代码（如 `hasattr(self, "xxx")` 检查）误判。建议至少返回属性名与 None 的键值对，或由主机插件侧统一处理。 |
| 18 | **astrmai/app/plugin_facade.py:162-175** | **get_runtime_diagnostics 和 build_help_text 多次调用 getattr 获取 models 列表**。`task_pool`、`agent_pool`、`embedding_pool`、`fallback_pool` 分别用 `getattr(…, …, [])` 获取并立刻转 list。可将这四次调用合并为一个辅助方法 `_get_model_pools()`。 |
| 19 | **astrmai/app/lifecycle.py:111-115** | **start_proactive_services 中 status.proactive_started 的赋值与报错在不同层级**。方法内部设置标志，而异常在 `_build_proactive_task` 中就已经被 `_record_optional_failure` 记录了 degraded。如果 `proactive_task.start()` 抛异常，标志不会设为 True（正确行为），但 degraded 状态被记录在 status 中，同时又抛出 warning，造成双重报告。建议统一为要么全部通过 status.degraded 报告，要么全部通过 logger.warning。 |
| 20 | **astrmai/main.py:37-62** | **inject_gemini_reverse_session 方法约 25 行，包含三层嵌套条件/赋值，可提取辅助函数**。`post_hook_hash` 的计算、`existing_trace` 的获取/合并逻辑可分别提取为 `_compute_post_hook_hash` 和 `_merge_request_trace` 两个辅助方法，提高可读性。 |
| 21 | **astrmai/app/plugin_facade.py:200** | **build_help_text 中的 capabilities 通过 get_capability_overview_sync() 获取，但 diagnostics 已包含部分 capabilities 信息，存在冗余调用**。`get_runtime_diagnostics()` 在第 194 行调用，内部第 197 行又调用 `self.get_capability_overview_sync()`，随后 `build_help_text` 再次调用 `self.get_capability_overview_sync()`。建议 diagnostics 返回的 `capabilities` 字段直接被 `build_help_text` 复用。 |
| 22 | **astrmai/app/bootstrap.py:266-272** | **_build_system2_bridge 返回的闭包 _bridge 引用了 self（通过 runtime 参数），而 self 是 PluginBootstrap 实例**。在 PluginBootstrap.build() 返回后，PluginBootstrap 实例可能被 GC，但闭包保留了对 runtime 的引用，而 runtime 又引用了大量服务对象，形成隐式的长生命周期引用链。建议改用 `lambda` 或独立的模块级函数以避免闭包生命周期问题。 |

## 亮点

1. **message_entry.py 重构成功**：旧版内联的 60+ 行守卫逻辑已全部委托给 PluginFacade 的窄域方法，`handle_global_message` 现在是一条清晰的流水线，每个步骤由一个显式命名的 facade 方法完成。
2. **RuntimeFacadeProtocol 定义完整**：54 个方法覆盖了生命周期、诊断、内存、交互、系统2/3 入口等全部使用场景，WebUI 侧也已全面迁移至 facade 访问器模式。
3. **_reset_runtime_status_flags 的退出安全设计**：terminate() 中先设置 `is_running = False` 终止后台循环后再回收任务，`CancelledError` 在各协程中被正确捕获并记录日志，关闭流程稳健。
4. **PluginApiAdapter 迁移彻底**：所有 `self.plugin_api._get_runtime()` 调用已被替换为窄域访问器，`apply_hot_config` 的主路径优先走 facade，fallback 路径打 WARNING 日志以便追踪。

## 总结

本轮重构（D58/D59）核心成就是将 PluginFacade 从"无类型壳"升级为显式实现 RuntimeFacadeProtocol 的正式门面，并将原来散落在 message_entry.py 和 PluginApiAdapter 中的守卫逻辑、运行时访问逻辑收拢到 facade 的窄域方法中。整体架构更清晰，职责边界更明确。

三个严重问题需要优先处理：(1) `_reset_runtime_status_flags` 复位不完整，可能导致重启后状态误报；(2) `submit_expression_review` 的 `pattern_id` 类型在 Protocol 与实现间不一致，lint 工具和调用方都会遇到问题；(3) 启动中途中断时后台任务缺乏安全终止机制。

中等严重度的发现集中在双向绑定时序脆弱性（findings #4）、构造后覆写内部属性（#5）、以及复位逻辑散落两处（#6）。这些属于设计层面的技术债务，建议在一个迭代内清理。

代码整体质量良好，测试覆盖充分（从 full_diff 可见 message_entry 和 host_mock 的测试都已适配新 facade 接口），回归风险可控。
