# 窗口 10：架构与边界专项

模块：
架构与边界专项（`infrastructure/runtime`、`presentation/webui`、`compat`）

职责：
对照 `tests/regression/architecture/*` 声明的边界规则，核查真实 import、目录依赖和旧兼容桥是否越过预期边界。

关键文件：
- `tests/regression/architecture/test_import_boundaries_refactor.py`
- `tests/regression/architecture/test_memory_runtime_boundaries_refactor.py`
- `astrmai/infrastructure/runtime/runtime_contracts.py`
- `astrmai/infrastructure/runtime/lane_history.py`
- `astrmai/infrastructure/runtime/lane_transcript.py`
- `astrmai/infrastructure/compat/legacy_compat.py`
- `astrmai/webui/backend/adapters/plugin_api.py`
- `astrmai/webui/backend/services/admin_ui_service.py`
- `astrmai/webui/backend/services/memory_ui_service.py`

现有测试：
- `tests/regression/architecture/test_import_boundaries_refactor.py:55-83` 只约束 `astrmai/presentation` 不直接 import `infrastructure.persistence`，以及 `webui/backend/routes` 不直接 import domain internals。
- `tests/regression/architecture/test_memory_runtime_boundaries_refactor.py:38-209` 主要约束 memory/runtime 兼容面与旧 adapter。
- `test_directory_contracts_refactor.py`、`test_shared_test_support_refactor.py` 不覆盖依赖图。

主要发现：
1. 存在明确的基础层反向依赖上层。
   - 依据：`astrmai/infrastructure/runtime/lane_history.py:10` 和 `lane_transcript.py:8` 直接 import `astrmai.conversation.planning.message_renderer.MessageRenderer`。
   - 进一步依据：`astrmai/infrastructure/runtime/runtime_contracts.py:13-30` 直接依赖 `astrmai.conversation.contracts.*`；`astrmai/infrastructure/gateway/gateway_result.py:8-9` 依赖 `astrmai.conversation.contracts.reply_artifact.VisibleReplyArtifact`。
   - 结论：现有 architecture test 没有任何一条扫描 `astrmai/infrastructure/**` 的上行 import。
2. 严格按 `astrmai/presentation/*` 范围，本轮未发现 presentation 直接碰 `persistence/gateway` 的明确缺陷；但真正高风险实际在 `webui/backend/services`，且未被测试覆盖。
   - 依据：`tests/regression/architecture/test_import_boundaries_refactor.py:67-83` 只检查 `routes`。
   - 进一步依据：`astrmai/webui/backend/services/memory_ui_service.py:16-18,122-156,158-168,213-341,504-509,679-719`、`review_ui_service.py:71-113`、`dashboard_service.py:18-37,54-63`、`user_ui_service.py:77-145` 都在直接读写 runtime 内部或 SQL 表结构；`plugin_api.py:171-176,191-192` 还直接暴露并改写 `runtime`。
3. `runtime/compat` 临时桥已经长期化，并伴随 God Object 扩散。
   - 依据：`astrmai/infrastructure/compat/legacy_compat.py:15-168` 仍被生产代码直接使用于 `attention/gate.py`、`planning/planner_prompt_context.py`、`planning/prompt_refiner.py`、`execution/reply_artifact_builder.py`。
   - 进一步依据：`tests/test_legacy_compat_refactor.py:20-40` 还把这座桥固化进测试；`git log` 显示近期仍有提交继续修改该文件。
   - 进一步依据：`astrmai/webui/backend/services/admin_ui_service.py:37-80,757,1199-1213` 与 `astrmai/app/runtime_context.py:12-380,386-436` 已接近“单文件知道一切”。

未实现/不完整项：
1. 没有回归测试约束 `astrmai/infrastructure/**` 不能反向 import `conversation/planning/presentation`。
2. 没有回归测试约束 `astrmai/webui/backend/services/**` 的数据库 / 运行时边界，也没有给 `legacy_compat.py`、`LEGACY_RUNTIME_ATTRS`、`PluginApiAdapter.get_runtime()` 设退出条件。

高风险点：
1. `webui` 服务层同时依赖 runtime 属性名和底层表结构名；memory/runtime/schema 再做一轮重构时，`memory_ui_service`、`admin_ui_service`、`dashboard_service` 极易出现页面静默退化。
2. `legacy_compat` extras 和 `export_legacy_attrs()` 的宽接口把 `attention -> planning -> execution -> webui` 串成隐式兼容链，一旦清理旧字段或拆 `PluginRuntimeContext`，影响面会跨多个子系统同时爆开。

建议下一步：
1. 新增 architecture regression：扫描 `astrmai/infrastructure/**` 的上行 import，禁止 `infrastructure -> conversation.planning/presentation/webui`，并把 `webui/backend/services/**` 纳入和 `routes` 同级的边界检查。
2. 给 `legacy_compat.py`、`PluginApiAdapter.get_runtime()`、`LEGACY_RUNTIME_ATTRS` 建显式收缩清单，优先拆 `astrmai/webui/backend/services/admin_ui_service.py` 这类横跨 runtime、observability、planner、heartflow 的 God Object。
