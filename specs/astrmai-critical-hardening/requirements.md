# Requirements Document

## Introduction

本 Spec 为「AstrMai」插件中识别出的 **7 个生产阻断级硬伤** 制定修复需求文档。这些缺陷涉及安全沙箱缺失、LLM 调用可靠性、上下文计量、数据库迁移、安全模块空壳、模型冷却冲突、主动调度竞态。不含新功能开发。

当前阶段产出物为 `specs/astrmai-critical-hardening/` 下的 `requirements.md` / `design.md` / `tasks.md`。**本轮仅产出 requirements.md，不进入 design 或 tasks 阶段**。

明确不在本 Spec 范围：
- 状态机竞态风险修复（S1–S8，已单独识别，不在本次 7 项硬伤内）
- 决策点优化（D1–D11）
- 多 Agent 安全加固（M1–M6，H1 除外）
- 资源配置优化（R1–R6）
- WebUI 安全加固（W1–W5）
- 新功能开发
- 代码质量基线治理（Any 类型、异常处理、测试覆盖）
- 依赖升级（`requirements.txt` 不新增/删除/升级依赖）

---

## Glossary

- **AstrMai**：基于 AstrBot 框架的 Multi-Agent 拟人聊天插件，入口 `main.py`，插件类 `AstrMaiPlugin(Star)`。
- **AstrBot**：底层聊天机器人框架 ≥ v4.14.0，提供 `Star` 基类、`@filter` 装饰器、`Context`、`FunctionTool`、`tool_loop_agent()` 等 API。
- **ComputerAgent**：`workmode/subagents/computer_agent.py` 中的 Sys3 子代理，负责代码执行和系统操作，内部使用 AstrBot 框架的 `LocalPythonTool` 和 `ExecuteShellTool`。
- **GlobalModelGateway**：`infrastructure/gateway/model_gateway.py` 中的 LLM 调用网关，混合 Policy/Call/Lane/Task/Result 五个 Mixin，是插件所有 LLM 调用的唯一出口。
- **GatewayPolicy**：`infrastructure/gateway/gateway_policy.py` 中的冷却引擎，负责模型失败后的 cooldown 管理和 fatal 判定。
- **ModelRouter**：`infrastructure/gateway/model_router.py` 中的模型路由器，基于健康评分 [-10,+10] 和 Round-Robin 进行模型选择，同时维护自适应冷却。
- **ContextEconomyCenter**：`infrastructure/context_economy/center.py` 中的上下文经济中心，负责任务策略决策、指标记录和 provider session 管理。
- **PersistenceSchemaMixin**：`infrastructure/persistence/persistence_schema.py` 中的持久化 Schema 管理，包含手动 ALTER TABLE 迁移。
- **ProactiveDispatcher**：`proactive/dispatcher.py` 中的主动消息分发器，将 Wakeup/Heartflow/GroupSignin 产生的主动意图注入到主对话链路。
- **ChatRuntimeCoordinator**：`infrastructure/runtime/chat_runtime_coordinator.py` 中的聊天运行时协调器，维护 per-chat 锁、等待目标和活跃追踪。
- **LaneManager**：`infrastructure/runtime/lane_manager.py` 中的 Lane 管理器，基于 `LaneKey` 隔离不同子系统/任务族的对话上下文。
- **FunctionTool**：AstrBot 框架中的工具基类，SubAgent 本质上是一个 `FunctionTool`。
- **Sys3**：插件的工作模式（System 3），通过 `/work` 命令或 Planner 的 tool_call_mode 进入，由 `Sys3Router` 调度子代理执行任务。
- **EARS**：Easy Approach to Requirements Syntax，本文档所有 Acceptance Criteria 遵循的句式（WHEN/IF/WHERE/THEN/THE ... SHALL ...）。
- **P0**：优先级分级 — 本 Spec 所有 7 项均为 P0 生产阻断级。

---

## Requirements

### Wave 1：安全隔离（2 项）

---

### Requirement 1: ComputerAgent 零沙箱隔离 — 增加执行环境安全边界

**User Story:** 作为插件管理员，当用户通过 `/work` 命令或 Sys2 Planner 的 tool_call_mode 触发代码执行时，我不希望 `LocalPythonTool` 和 `ExecuteShellTool` 在宿主机上直接执行且没有任何代码级安全防护，所以恶意 Prompt Injection 或 LLM 幻觉不会导致宿主机被控。

#### Acceptance Criteria

1. THE ComputerAgent SHALL 在实例化时默认不加载 `LocalPythonTool` 和 `ExecuteShellTool`（即 `_COMPUTER_TOOLS_AVAILABLE` 默认为 `False`），仅当配置中显式开启 `computer_agent_sandbox_enabled` 时才加载。
2. WHEN 配置项 `computer_agent_sandbox_enabled` 为 `False`（默认值），THE ComputerAgent SHALL 返回空 `ToolSet([])` 并触发 `[SUBAGENT_DECLINE]` 降级，告知用户"代码执行功能未启用"。
3. WHEN 配置项 `computer_agent_sandbox_enabled` 为 `True`，THE ComputerAgent SHALL 在执行前通过 `ExecuteShellTool` 的 `sandbox_mode` 参数启用沙盒（若 AstrBot 框架版本支持），而非直接 `is_local=True`。
4. THE ComputerAgent SHALL NOT 在没有管理员权限验证的情况下加载任何执行工具。
5. THE system prompt（第 33–42 行）中的"不执行删除系统文件"安全提示 SHALL 被保留，作为 LLM 层面的第二道防线（defense-in-depth）。

#### Notes / Constraints

- 涉及文件：`astrmai/workmode/subagents/computer_agent.py`（全文 57 行）。
- 根因：第 49–50 行 `LocalPythonTool()` + `ExecuteShellTool(is_local=True)` 在 `_COMPUTER_TOOLS_AVAILABLE = True`（即 AstrBot 框架有此模块）时无条件加载，唯一的防护是第 37–42 行的 LLM system prompt 文本提示——这是提示级防护，不是代码级防护。
- 风险场景：用户通过 `/work 帮我检查服务器状态` → Router 选 `transfer_to_computer` → LLM 被 prompt injection 诱导执行 `os.system("rm -rf /")` → 宿主机被控。
- 修复方式：
  1. 在 `_conf_schema.json` 的 `sys3` 分组中新增 `computer_agent_sandbox_enabled: bool (default=false)`。
  2. 在 `config.py` 的 `Sys3Settings` 中新增对应字段。
  3. 修改 `ComputerAgent.__init__` 接收 config，根据 `sandbox_enabled` 决定是否加载工具。
  4. 默认 `_COMPUTER_TOOLS_AVAILABLE = False`，仅当 config + import 同时满足时才设为 `True`。
- 涉及 AstrBot 框架：需确认 `ExecuteShellTool` 是否支持 `sandbox_mode` 参数。若框架不支持，第一期至少实现配置开关 + admin 权限检查。
- 验证：构造 `/work` 请求确认默认不加载工具 → 修改配置 → 重启插件 → 确认工具可用。

---

### Requirement 2: Security 模块空壳 — 建立集中安全入口

**User Story:** 作为安全审查者，我希望插件有一个集中的安全模块入口（而非安全逻辑散落在 gateway/output_guard、conversation/contracts/prompt_envelope、persistence 等多个文件中），所以未来的安全加固有明确的挂载点和统一的调用约定。

#### Acceptance Criteria

1. THE `astrmai/infrastructure/security/__init__.py` SHALL 从仅一行 docstring 的空壳变为至少提供以下子模块的集中 re-export：`input_sanitizer`、`output_guard`、`rate_limiter`。
2. THE `input_sanitizer` SHALL 提供统一的输入净化入口（`sanitize(text: str) -> str`），当前至少封装 `PromptEnvelope.sanitize_user_input()` 和 `PromptEnvelope.sanitize_memory_content()`，并预留扩展点（WebUI API body 净化、DB 写入前净化）。
3. THE `output_guard` SHALL 从 `gateway/output_guard.py` 迁移到 `security/` 下（或通过 re-export 桥接），确保输出安全检查有统一的 `security.OutputGuard` 入口。
4. THE `rate_limiter` SHALL 提供 TokenBucket 或 SlidingWindow 基础实现（至少提供接口定义 + 内存实现），供未来 WebUI API 和 LLM 调用复用。
5. THE 原有散落在各处的安全逻辑 SHALL NOT 被删除（第一期仅做集中入口 + 标注 TODO），确保不引入回归。

#### Notes / Constraints

- 涉及文件：
  - `astrmai/infrastructure/security/__init__.py`（当前：1 行 docstring）— **主改动文件**。
  - `astrmai/conversation/contracts/prompt_envelope.py`（第 11–32 行 `sanitize_user_input` / `sanitize_memory_content`）— **仅引用，不修改**。
  - `astrmai/infrastructure/gateway/output_guard.py` — **仅 re-export 桥接，不修改**。
- 根因：`security/` 目录在初始重构时被预留但从未填充，安全逻辑（input sanitization、output guard、rate limiting）分散在 3 个不同模块中，缺乏统一入口和审计点。
- 修复方式：在 `security/` 下新增 3 个子模块文件（`input_sanitizer.py`、`output_guard.py`、`rate_limiter.py`），`output_guard.py` 从 `gateway/output_guard.py` re-export，`input_sanitizer.py` 封装 `PromptEnvelope` 的 sanitize 方法，`rate_limiter.py` 提供 TokenBucket 基础实现。
- 第一期不删除原有分散逻辑，仅在关键调用点增加 `# TODO: migrate to security.xxx` 注释。
- 验证：`from astrmai.infrastructure.security import InputSanitizer, OutputGuard, RateLimiter` 可正常导入，`InputSanitizer.sanitize("test")` 返回正确净化结果。

---

### Wave 2：LLM 调用可靠性（2 项）

---

### Requirement 3: `_is_fatal_failure` 将网络超时误判为致命错误

**User Story:** 作为依赖 LLM 回复质量的终端用户，当网关因瞬时网络超时（`asyncio.TimeoutError`）而放弃某个模型时，我不希望该模型被永久标记为 fatal 并跳过重试，所以短暂的网络抖动不会导致可用模型池意外缩小。

#### Acceptance Criteria

1. WHEN `_is_fatal_failure()` 检测到异常消息中包含 `"timeout"` 关键字，THE 函数 SHALL 区分 `asyncio.TimeoutError`（客户端超时，非 fatal，应重试）和 provider 返回的 `408 Request Timeout` / `504 Gateway Timeout`（服务端超时，可选 fatal）。
2. THE `fatal_keywords` 元组（第 145–159 行）SHALL 移除裸 `"timeout"` 关键字，替换为 `"request timed out"`、`"timed out"`、`"408"`、`"504"` 等精确匹配。
3. THE `asyncio.TimeoutError` SHALL 在 `_classify_failure_kind()` 中被分类为 `FailureKind.TIMEOUT`（保留用于统计），但在 `_is_fatal_failure()` 中 SHALL NOT 被判定为 fatal。
4. THE 修复 SHALL 不改变 429/403/quota/permission_denied 等其他 fatal 条件的判定逻辑。
5. WHERE 异常类型为 `asyncio.TimeoutError`，THE 重试逻辑 SHALL 正常执行（最多 `llm_retries` 次），重试失败后再切换下一个候选模型。

#### Notes / Constraints

- 涉及文件：`astrmai/infrastructure/gateway/gateway_policy.py`，`_is_fatal_failure()` 方法（第 143–161 行），`_classify_failure_kind()` 方法（第 113–141 行）。
- 根因：第 159 行 `"timeout"` 是裸关键字匹配，会匹配到任何包含 `timeout` 字符串的异常消息（包括 `asyncio.TimeoutError` 的标准消息 `"Timeout context manager should be used inside a task"` 或类似的框架级超时消息）。这导致所有超时都被判定为 fatal → 模型被立即放弃 → 可用模型池缩小 → 严重时全池耗尽抛出 `LLMCascadeFailureException`。
- `asyncio.TimeoutError` 在 Python 中的默认消息不一定包含 `"timeout"`，但 AstrBot 框架的 LLM 调用可能将其 wrap 为包含 `"timeout"` 的 `Exception`。
- 修复方式：
  1. 修改 `_classify_failure_kind()`：增加 `isinstance(error, asyncio.TimeoutError)` 检查，优先返回 `FailureKind.TIMEOUT`。
  2. 修改 `_is_fatal_failure()`：移除 `"timeout"` 裸关键字，增加 `"408"`、`"504"`、`"request timed out"`、`"timed out"` 等精确匹配；增加 `isinstance(error, asyncio.TimeoutError)` 检查并返回 `False`。
- 验证：模拟 `asyncio.TimeoutError` → 确认 `_is_fatal_failure` 返回 `False` → 确认重试逻辑正常执行 → 确认 429/403 仍然返回 `True`。

---

### Requirement 4: Gateway 双冷却系统冲突 — 统一冷却入口

**User Story:** 作为运维人员，当某个模型因 429 或连续失败被冷却时，我不希望 `ModelRouter` 和 `GatewayPolicy` 各自独立维护冷却状态导致同一模型被双重冷却（冷却时间叠加或矛盾），所以模型冷却策略是确定性的、可预测的。

#### Acceptance Criteria

1. THE GatewayPolicy SHALL 作为模型冷却的唯一权威入口（single source of truth），`ModelRouter._cooldown_until` SHALL 从 `GatewayPolicy._model_cooldowns` 读取而非独立维护。
2. WHEN `GatewayPolicy._open_model_cooldown()` 设置冷却，THE `ModelRouter.get_ranked_models()` SHALL 通过查询 `GatewayPolicy` 的冷却状态来过滤冷却中的模型，而非使用自己的 `_cooldown_until`。
3. THE `ModelRouter` 的健康评分机制（`report_success` +1 / `report_failure` -2/-4）SHALL 被保留，用于模型排序优先级，但不再兼任冷却职责。
4. THE 修复 SHALL 不改变冷却时长配置（rate_limit=120s、quota=1800s、自适应 30-120s）。
5. THE 修复 SHALL 不改变 `_classify_cooldown_reason()` 的分类逻辑和 `_is_fatal_failure()` 的判定逻辑。

#### Notes / Constraints

- 涉及文件：
  - `astrmai/infrastructure/gateway/gateway_policy.py`：`_model_cooldowns` 属性（第 53 行）、冷却逻辑（全文 161 行）。
  - `astrmai/infrastructure/gateway/model_router.py`：`_cooldown_until` 属性（第 27 行）、冷却检查（第 111 行）、`BASE_COOLDOWN_SEC=30` / `MAX_COOLDOWN_SEC=120`（第 50–51 行）。
- 根因：`ModelRouter`（第 27 行 `cooldown_until: float = 0.0`、第 111 行 `if state.cooldown_until > now`）和 `GatewayPolicy`（第 53 行 `_model_cooldowns`）各自独立维护冷却状态，无同步机制。同一个模型可能被 ModelRouter 冷却 30-120s 又被 GatewayPolicy 冷却 120s-1800s，双重冷却导致可用模型池被不必要地缩小。
- 修复方式：
  1. `ModelRouter` 在 `get_ranked_models()` 中接收 `cooldown_checker: Callable[[str, str], bool]` 参数，由 `GatewayPolicy._is_model_cooldown(pool_name, model_id)` 提供。
  2. 废弃 `ModelRouter._cooldown_until`，改为仅维护健康评分。
  3. `GatewayPolicy._filter_cooldown_attempt_queue()` 在调用 `ModelRouter.get_ranked_models()` 时注入 `cooldown_checker`。
- 验证：模拟模型连续失败 → 确认冷却时长唯一 → 确认 `get_ranked_models()` 正确过滤冷却模型。

---

### Wave 3：数据完整性（2 项）

---

### Requirement 5: 上下文压缩无 Token 计数 — 集成 Token 估算

**User Story:** 作为依赖长对话上下文的用户，当对话累积到触发压缩阈值时，我不希望压缩基于消息条数而非实际 token 数进行，所以长消息（粘贴代码/长文）不会导致上下文溢出、LLM 截断回复或报错。

#### Acceptance Criteria

1. THE `ContextCompactionEngine` SHALL 在压缩决策时使用 token 估算值（而非纯消息条数 `max_raw_turns`）判断是否触发压缩，至少对 `warm_zone_max_tokens` 和 `compaction_trigger_tokens` 两个配置项生效。
2. THE 插件 SHALL 集成一个轻量级 Token 估算器（字符/4 粗略估算或 tiktoken 精确计数），通过 `InfrastructureSettings` 的新增字段 `token_estimator_enabled` 控制开关。
3. WHEN `token_estimator_enabled` 为 `True`，THE `ContextEconomyCenter` SHALL 在 `build_request()` 中估算当前请求的 token 消耗并记录到 `WorkloadMetrics`。
4. WHEN `token_estimator_enabled` 为 `False`（默认值，保持向后兼容），THE 插件 SHALL 回退到消息条数估算，行为与当前一致。
5. THE `warm_zone_max_tokens`（默认 1200）和 `compaction_trigger_tokens`（默认 1800）的配置项语义 SHALL 被保留，不修改 `_conf_schema.json` 中的配置项名称。

#### Notes / Constraints

- 涉及文件：
  - `astrmai/infrastructure/context_economy/center.py`：`ContextEconomyCenter` 类，`build_request()` / `resolve_policy()` 方法。
  - `astrmai/infrastructure/runtime/lane_manager.py`：`LaneManager` 类，`DEFAULT_POLICIES` 配置（`max_raw_turns` 字段）。
  - `astrmai/shared/constants/defaults.py`：`InfrastructureSettings`，可能需要新增字段。
  - `astrmai/_conf_schema.json`：`conversation.warm_zone_max_tokens` / `compaction_trigger_tokens` 配置项。
- 根因：`warm_zone_max_tokens=1200` 配置项语义为 token 数，但实际压缩在 `_compact_history()` 中按消息条数截断。没有集成 tiktoken 或任何 tokenizer。当用户发送长消息（粘贴代码/长文）时，实际 token 消耗远超预算。
- 修复方式：
  1. 新增 `astrmai/infrastructure/context_economy/token_estimator.py`，提供 `estimate_tokens(text: str) -> int`（字符/4 或 tiktoken）。
  2. 在 `build_infrastructure_settings()` 中新增 `token_estimator_enabled: bool = False`。
  3. 在 `LaneManager._compact_history()` 中，当 `token_estimator_enabled` 时改用 token 估算判断压缩触发。
- 依赖：若使用 tiktoken，需在 `requirements.txt` 中新增 `tiktoken`。第一期可选字符/4 粗略估算（零依赖）。
- 验证：构造 2000 token 的对话 → 确认压缩在 ~1800 token 时触发而非 ~40 条消息时。

---

### Requirement 6: 数据库迁移零版本管理 — 引入轻量 Schema 版本追踪

**User Story:** 作为插件维护者，当未来需要修改数据库列类型或新增列时，我不希望依赖 `try/except` 捕获 "duplicate column name" 错误来跳过已存在的列，所以数据库迁移是可追溯、可回滚、可诊断的。

#### Acceptance Criteria

1. THE `PersistenceSchemaMixin` SHALL 在数据库中维护一个 `schema_version` 元数据表（或通过 `PRAGMA user_version`），记录当前已应用的迁移版本号。
2. WHEN 插件启动时，THE `_init_db()` 方法 SHALL 读取当前 `schema_version`，并按版本号顺序执行尚未应用的迁移语句（而非无条件执行所有 ALTER TABLE 并用 try/except 跳过重复列）。
3. THE 现有所有 ALTER TABLE 语句（第 130–240+ 行）SHALL 被迁移到版本化的迁移列表中，每条迁移关联一个唯一的版本号（如 `v1`、`v2`、…）。
4. THE 迁移执行 SHALL 在事务中进行，任一条失败则回滚整个迁移批次，并记录错误日志。
5. THE 修复 SHALL 不改变现有 `CREATE TABLE IF NOT EXISTS` 的建表逻辑（向后兼容全新安装）。

#### Notes / Constraints

- 涉及文件：`astrmai/infrastructure/persistence/persistence_schema.py`，`PersistenceSchemaMixin` 类（全文 350 行），关键方法 `_init_db_sync()`（第 79 行）、`_apply_schema_patch_batch_sync()`（约第 135 行）。
- 根因：第 127–133 行的 `_apply_schema_patch_batch_sync()` 通过 `for ddl in ddl_statements: try: db.execute(ddl) except: pass` 执行所有 ALTER TABLE。如果列已存在，sqlite3 抛出 `OperationalError: duplicate column name`，被静默吞掉。没有版本号追踪 → 无法处理列类型变更 → 无法回滚 → 生产问题难以诊断。
- 修复方式：
  1. 在 `_init_db_sync()` 中新增 `PRAGMA user_version` 读取/写入。
  2. 定义 `MIGRATIONS: list[tuple[int, str]]` 列表（版本号 + DDL），按版本号升序排列。
  3. 当前 `user_version` 为 0 时，执行全部迁移；已有版本号时仅执行更高版本的迁移。
  4. 每条迁移执行后用 `db.execute(f"PRAGMA user_version = {version}")` 更新版本号。
- 第一期不引入 Alembic（过度设计），使用 `PRAGMA user_version` 轻量方案。
- 验证：全新安装 → 确认 `user_version` 设为最新版本号 → 重启 → 确认无重复迁移执行 → 模拟新增迁移 → 确认仅新迁移被执行。

---

### Wave 4：并发正确性（1 项）

---

### Requirement 7: ProactiveDispatcher `runtime_coordinator` detach/restore 竞态窗口

**User Story:** 作为依赖主动消息（Wakeup/Heartflow/Dream）功能的用户，当 ProactiveDispatcher 在注入主动消息前暂时卸下 `attention_gate.runtime_coordinator` 时，我不希望并发的 ChatLoopKernel 心跳在 detach/restore 之间触发消息处理导致状态不一致或消息丢失。

#### Acceptance Criteria

1. THE `ProactiveDispatcher._dispatch_locked()` 方法（第 301–318 行）SHALL 在执行 `setattr(self.attention_gate, "runtime_coordinator", None)` 之前获取一个 per-chat 的 `asyncio.Lock`，确保同一 chat 的并发注入被序列化。
2. WHEN `runtime_coordinator_detached` 为 `True` 且 `inject_external_event()` 抛出异常，THE `finally` 块（第 311–313 行）SHALL 正确恢复 `original_runtime_coordinator`——当前逻辑已正确，但需新增 per-chat Lock 防止并发 attach/detach。
3. THE `ChatRuntimeCoordinator` 在检测到 `attention_gate.runtime_coordinator is None` 时 SHALL 进入等待模式（而非静默跳过消息），并在 coordinator 恢复后处理缓存的消息，或至少记录 warning 日志。
4. THE 修复 SHALL 在 `AttentionGate` 上新增 `_proactive_injection_lock: dict[str, asyncio.Lock]`（per-chat），`ProactiveDispatcher` 通过 `attention_gate` 获取对应 chat 的锁。
5. THE 修复 SHALL 不改变现有 ProactiveMessageIntent 的构建逻辑和 `inject_external_event()` 的接口契约。

#### Notes / Constraints

- 涉及文件：
  - `astrmai/proactive/dispatcher.py`：`ProactiveDispatcher._dispatch_locked()` 方法（第 301–318 行）。
  - `astrmai/conversation/attention/gate.py`：`AttentionGate` 类（需新增 `_proactive_injection_lock`）。
  - `astrmai/infrastructure/runtime/chat_runtime_coordinator.py`：`ChatRuntimeCoordinator`（需处理 coordinator 为 None 的情况）。
- 根因：第 301–306 行通过 `setattr(self.attention_gate, "runtime_coordinator", None)` 卸下 coordinator 以阻止并发消息处理，在 `inject_external_event()` 完成后通过 `finally`（第 311–313 行）恢复。此窗口期间（通常 <1s）：
  1. 并发的 ChatLoopKernel 心跳可能在 detach/restore 之间触发同一 chat 的消息处理 → `AttentionGate` 没有 coordinator → 消息被静默跳过。
  2. 同一 chat 的另一个 ProactiveMessageIntent 可能并发进入 `_dispatch_locked()` → 同时 detach → 恢复时 coordinator 混乱。
- 修复方式：
  1. `AttentionGate` 新增 `_proactive_injection_lock: dict[str, asyncio.Lock]`，通过 `get_proactive_lock(chat_id)` 获取。
  2. `ProactiveDispatcher._dispatch_locked()` 在执行 detach 前先 `async with attention_gate.get_proactive_lock(chat_id)`。
  3. `AttentionGate.process_event()` 在检测到 `runtime_coordinator is None` 时打印 warning 日志并返回 `"PROACTIVE_BLOCKED"`。
- 验证：并发构造 2 个 ProactiveMessageIntent 对同一 chat → 确认被锁序列化执行 → 确认 coordinator 恢复正确 → 确认无消息丢失。

---

## Out of Scope（不在本 Spec 范围内）

以下项目在审查中被识别但**明确排除**在本次 7 项硬伤修复之外：

- **状态机竞态风险**（S1–S8）：ChatState dirty-flag 不一致、RelationshipVector 双写、Mood CAS 窗口、FrequencyController 不持锁等。这些已在审查中识别，但修复涉及广泛的状态管理重构，风险较高，应在独立的 Spec 中处理。
- **决策点优化**（D1–D11）：去重 TTL、快速唤醒阈值、Judge 超时回退、CognitiveLoop 静默等。属于行为调优，非生产阻断。
- **多 Agent 安全加固**（M2–M6）：Router light_tool_set、Sys2→Sys3 交接、HandoffRegistry 覆盖、CronHeartbeatGuard 恢复等。M1（ComputerAgent 沙箱）已纳入本 Spec 的 H1。
- **资源泄漏修复**（R1–R6）：Lane rotation provider session 泄漏、EventBus 丢弃、CancelledError discard、FaissVecDB 重试、DreamScheduler 全局 throttle、VisualCortex to_thread 不一致等。
- **WebUI 安全加固**（W1–W5）：85 端点鉴权、前端契约不一致、Body 静默吞异常等。
- **代码质量基线治理**：Any 类型治理、异常处理搜索、AstrBot API 合规审查、测试覆盖审计。
- **新功能开发**：本次 Spec 仅修复已有代码缺陷，不新增任何功能。
- **依赖升级**：`requirements.txt` 不新增（除 R5 的可选 `tiktoken`）、不删除、不升级任何依赖。
- **数据库 Schema 结构性变更**：R6 仅引入版本追踪机制，不修改现有表结构或数据。

---

## High-Risk Confirmation List（高风险确认清单）

| # | 风险事项 | 风险等级 | 触发条件 | 缓解措施 |
|---|---------|:------:|---------|---------|
| HK1 | H1 修改 `ComputerAgent` 工具加载逻辑后，`transfer_to_computer` SubAgent 在 Sys3 中被 Router 选中但返回空 ToolSet → `[SUBAGENT_DECLINE]` 需要前端正确处理 | 🟡 | Sys3 Router 选中 `transfer_to_computer` 且 `sandbox_enabled=False` | `AstrMaiBaseSubAgent.call()` 已有 DECLINE 处理（`base_agent.py` 第 72 行），需验证前端对 DECLINE 消息的展示 |
| HK2 | H2 移除 `"timeout"` 裸关键字可能导致某些 provider 的超时错误不再被正确分类为 `FailureKind.TIMEOUT` | 🟡 | Provider 返回非标准超时错误消息（不含 `"timed out"` / `"408"` / `"504"`） | `_classify_failure_kind()` 保留兜底 `FailureKind.UNKNOWN`；建议增加 provider-specific 超时检测 |
| HK3 | H3 Token 估算器默认 `False` 意味着现有行为不变，但开启后压缩触发时机改变，可能导致旧对话的压缩行为和用户预期不一致 | 🟢 | `token_estimator_enabled=True` 且用户有长消息习惯 | 默认 `False`（向后兼容），用户主动开启后才生效 |
| HK4 | H4 `PRAGMA user_version` 在 sqlite3 中的并发安全性——如果多个进程/线程同时执行迁移 | 🟡 | 多进程部署（罕见，AstrBot 通常单进程） | 第一期加 `asyncio.Lock` + 迁移前检查 `user_version` 的 CAS 语义 |
| HK5 | H6 移除 `ModelRouter._cooldown_until` 后，依赖该字段的外部代码（如果有）将失效 | 🟡 | 存在外部代码直接访问 `ModelRouter._cooldown_until` | 先搜索 `_cooldown_until` 的所有引用，确认仅内部使用；提供 deprecation warning 过渡期 |
| HK6 | H7 新增 per-chat Lock 可能引入死锁：如果 `attention_gate.process_event()` 内部也要获取同一把锁 | 🟡 | `process_event()` 新增 `async with proactive_lock` 与 `ProactiveDispatcher` 竞争同一锁 | Lock 仅在 `ProactiveDispatcher._dispatch_locked()` 中获取，`process_event()` 检测 `coordinator is None` 即返回，不获取锁 |
| HK7 | H5 Security 模块第一期仅做集中入口 + re-export，原有分散逻辑不动 → 可能出现新代码同时 import `security.InputSanitizer` 和直接使用 `PromptEnvelope.sanitize_user_input()` 的情况 | 🟢 | 新旧代码共存过渡期 | 在 `PromptEnvelope.sanitize_*` 方法上增加 deprecation comment，标注迁移路径 |

---

## Dependency Map（需求依赖关系）

```
Wave 1 (安全隔离)          Wave 2 (LLM 可靠性)
  R1 ─────────────────────────┐
  R2                          │
    │                         ▼
    │              R3 (timeout fatal) ──► R4 (双冷却)
    │                         │               │
    └─────────────────────────┼───────────────┘
                              │
                    Wave 3 (数据完整性)
                      R5 (token 计数)
                      R6 (DB 迁移)
                              │
                    Wave 4 (并发正确性)
                      R7 (dispatcher 竞态)

R1 和 R3 可并行（独立模块）
R5 和 R6 可并行（独立模块）
R2 可与其他所有并行（仅新增文件，不改已有逻辑）
R4 依赖 R3（两者修改同一个文件 gateway_policy.py）
R7 可与其他所有并行（独立模块）
```

---

## Verification Strategy（验证策略）

| 验证层 | 命令/方式 | 覆盖需求 |
|--------|----------|:------:|
| **单元测试** | `pytest tests/ -v -k "computer_agent"` | R1 |
| **单元测试** | `pytest tests/ -v -k "security or sanitize"` | R2 |
| **单元测试** | `pytest tests/ -v -k "gateway_policy or fatal"` | R3, R4 |
| **单元测试** | `pytest tests/ -v -k "token_estimator or context_economy"` | R5 |
| **单元测试** | `pytest tests/ -v -k "persistence_schema or migration"` | R6 |
| **单元测试** | `pytest tests/ -v -k "dispatcher or proactive"` | R7 |
| **LSP** | `lsp_diagnostics` 对全部变更文件 | R1–R7 |
| **集成测试** | `/work` 命令端到端（H1 默认不加载工具） | R1 |
| **集成测试** | 模拟 `asyncio.TimeoutError` → 确认重试不 fatal | R3 |
| **集成测试** | 模拟 429 → 确认冷却唯一入口 | R4 |
| **集成测试** | 长对话上下文压缩 → 确认 token 估算触发 | R5 |
| **集成测试** | 全新安装 + 重启 → 确认 DB 迁移幂等 | R6 |
| **集成测试** | 并发 ProactiveMessageIntent → 确认序列化 | R7 |
| **手工验证** | `_conf_schema.json` 中修改 `computer_agent_sandbox_enabled` → 重启 → `/work` 确认工具可用 | R1 |
| **手工验证** | `from astrmai.infrastructure.security import InputSanitizer` 导入成功 | R2 |
| **手工验证** | 全量回归：`pytest tests/ -v --tb=short` ≥ 70 passed | R1–R7 |

---

> **写入 3 完成。** `requirements.md` 全部 7 条需求已写入。可进入 Kiro Phase 2（设计文档）或直接基于此需求文档进入任务分配。
