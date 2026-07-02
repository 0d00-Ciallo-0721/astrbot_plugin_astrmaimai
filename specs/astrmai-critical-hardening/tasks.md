# Implementation Plan

> 本任务列表派生自同目录 `requirements.md` 与 `design.md`。
> **执行原则**：任务**严格串行**，编号 1 → N，后续任务依赖前一任务完成。
> **状态规则**：所有任务初始状态为 `- [ ]` 未完成。

## Overview

本任务列表把 7 条需求与 7 个模块设计翻译为 **10 个严格串行**的可执行任务。

| Phase | 主题 | 任务 | 改动类型 |
|-------|------|------|---------|
| Phase 1 | 安全隔离 | Tasks 1-2 | 配置 + 新增模块 |
| Phase 2 | LLM 可靠性 | Tasks 3-4 | Bug 修复 |
| Phase 3 | 数据完整性 | Tasks 5-6 | 新增功能 + 重构 |
| Phase 4 | 并发正确性 | Task 7 | 并发修复 |
| Phase 5 | 配置收尾 + 最终验证 | Tasks 8-10 | 配置 + 验证 |

### 依赖关系

```
Task 1 (R1 config) → Task 2 (R2 security)  ← 可并行，但串行排布
    ↓
Task 3 (R3 timeout) → Task 4 (R4 cooldown)  ← R4 修改同一个文件，依赖 R3
    ↓
Task 5 (R5 token)   → Task 6 (R6 db migration) ← 可并行，但串行排布
    ↓
Task 7 (R7 dispatcher lock)
    ↓
Task 8 (R1 _conf_schema 收尾) → Task 9 (回归验证) → Task 10 (LSP 清理)
```

---

## Tasks

### Phase 1: 安全隔离

- [ ] 1. R1: ComputerAgent 配置开关 — config.py + computer_agent.py + router.py
  - **Goal**: ComputerAgent 默认不加载执行工具，仅当配置 `computer_agent_sandbox_enabled=True` 时才启用
  - **Files**:
    - ✏️ `config.py` — `Sys3Settings` 新增字段
    - ✏️ `astrmai/workmode/subagents/computer_agent.py` — 模块变量 + 字段 + `get_tool_set`
    - ✏️ `astrmai/workmode/router.py` — `__init__` 注入 config
  - **Steps**:
    1. `config.py:L200-201`: 在 `Sys3Settings` 中新增 `computer_agent_sandbox_enabled: bool = Field(default=False, description="是否启用 ComputerAgent 的代码执行能力")`
    2. `computer_agent.py:L9`: 将 `_COMPUTER_TOOLS_AVAILABLE = True` 改为 `_COMPUTER_TOOLS_AVAILABLE = False`
    3. `computer_agent.py:L17`: `ComputerAgent` dataclass 新增字段 `sandbox_enabled: bool = False`
    4. `computer_agent.py:L45-51`: `get_tool_set()` 在 `not _COMPUTER_TOOLS_AVAILABLE` 之前增加 `if not self.sandbox_enabled: return ToolSet([])`
    5. `computer_agent.py:L53-54`: `_get_decline_reason()` 增加提示：当 `sandbox_enabled=False` 时返回"代码执行功能未启用，请管理员在配置中开启"
    6. `router.py:L14-23`: `Sys3Router.__init__` 从 `plugin_config.sys3.computer_agent_sandbox_enabled` 读取配置，注入 `ComputerAgent(sandbox_enabled=...)`
    7. `router.py:L18`: 确认 `ComputerAgent()` 创建时传入 `sandbox_enabled` 参数
  - **Acceptance Criteria**:
    - `ComputerAgent(sandbox_enabled=False).get_tool_set()` 返回空 `ToolSet([])`
    - `ComputerAgent(sandbox_enabled=True)` 且 `_COMPUTER_TOOLS_AVAILABLE=True` 时加载工具
    - `Sys3Router` 从 config 正确读取并注入 `sandbox_enabled`
  - **Forbidden**: 不修改 `base_agent.py` 的 `call()` 方法；不修改 `ComputerAgent.name`；不修改 `get_system_prompt()` 安全提示文本
  - **Check Commands**: `pytest tests/ -v -k "computer_agent"` ； `python -c "from config import Sys3Settings; s = Sys3Settings(); assert s.computer_agent_sandbox_enabled == False"`
  - **Risk Notes**: 🟡 升级后现有 `/work` 流程返回 `[SUBAGENT_DECLINE]`，需在 DECLINE 消息中提示用户开启配置
  - _Requirements: R1_

- [ ] 2. R2: Security 模块建立集中入口 — 新增 3 个子模块 + re-export
  - **Goal**: `astrmai/infrastructure/security/` 从空壳变为提供 `InputSanitizer`、`OutputGuard`、`TokenBucket` 三个子模块的集中安全入口
  - **Files**:
    - ➕ `astrmai/infrastructure/security/input_sanitizer.py` — 新建
    - ➕ `astrmai/infrastructure/security/output_guard.py` — 新建
    - ➕ `astrmai/infrastructure/security/rate_limiter.py` — 新建
    - ✏️ `astrmai/infrastructure/security/__init__.py` — 替换为 re-export
    - ✏️ `astrmai/conversation/contracts/prompt_envelope.py` — 添加迁移注释（只读）
    - ✏️ `astrmai/infrastructure/gateway/output_guard.py` — 添加迁移注释（只读）
  - **Steps**:
    1. 创建 `input_sanitizer.py`：`InputSanitizer` 类，`sanitize(text)` 封装 `PromptEnvelope.sanitize_user_input()`，`sanitize_memory(text)` 封装 `PromptEnvelope.sanitize_memory_content()`
    2. 创建 `output_guard.py`：re-export `validate_visible_output_text`、`extract_provider_failure_text_hints`、`extract_prompt_scaffold_hints` 从 `gateway/output_guard.py`
    3. 创建 `rate_limiter.py`：`TokenBucket` 类，`__init__(rate, capacity)` + `async consume(tokens=1) -> bool`，使用 `asyncio.Lock` + `time.monotonic()` 实现
    4. 修改 `__init__.py`：`from .input_sanitizer import InputSanitizer` / `from .output_guard import validate_visible_output_text` / `from .rate_limiter import TokenBucket`
    5. `prompt_envelope.py:L20` 和 `L32` 后各加一行 `# TODO: migrate callers to security.InputSanitizer`
    6. `gateway/output_guard.py` 主要导出函数上方加 `# re-exported via security.output_guard`
  - **Acceptance Criteria**:
    - `from astrmai.infrastructure.security import InputSanitizer, TokenBucket` 导入成功
    - `InputSanitizer.sanitize("<script>x</script>")` 返回 `<user_input>\n<script>x</script>\n</user_input>`
    - `InputSanitizer.sanitize_memory("记忆内容")` 返回 `<retrieved_memory>\n记忆内容\n</retrieved_memory>`
    - `TokenBucket(rate=1.0, capacity=5).consume(6)` 异步返回 `False`
  - **Forbidden**: 不修改 `PromptEnvelope.sanitize_*` 的内部实现；不删除 `gateway/output_guard.py` 中任何函数；不引入第三方限流库；不修改任何调用方改用新模块
  - **Check Commands**: `pytest tests/ -v -k "security"` ； `python -c "from astrmai.infrastructure.security import InputSanitizer, TokenBucket; print('OK')"`
  - **Risk Notes**: 🟢 纯新增文件 + 注释，零回归风险
  - _Requirements: R2_

---

> **写入 1 完成。** 接下来写入 2 将填充 Phase 2–4 全部 Tasks（Task 3–7）。

---

### Phase 2: LLM 调用可靠性

- [ ] 3. R3: 修复 `_is_fatal_failure` 将 `asyncio.TimeoutError` 误判为致命错误
  - **Goal**: `asyncio.TimeoutError`（客户端超时）不再被判定为 fatal，允许重试；provider 返回的 408/504 服务端超时仍然判 fatal
  - **Files**:
    - ✏️ `astrmai/infrastructure/gateway/gateway_policy.py` — `_classify_failure_kind()` + `_is_fatal_failure()`
    - ✏️ `astrmai/infrastructure/gateway/gateway_call.py` — 调用点传递 `error=exc`
  - **Steps**:
    1. `gateway_policy.py:L123`: `_classify_failure_kind()` 签名改为 `def _classify_failure_kind(self, error_message: str, error: Exception | None = None) -> FailureKind:`
    2. `gateway_policy.py:L124`: 在函数体开头增加 `if error is not None and isinstance(error, asyncio.TimeoutError): return FailureKind.TIMEOUT`
    3. `gateway_policy.py:L143`: `_is_fatal_failure()` 签名改为 `def _is_fatal_failure(self, error_message: str, error: Exception | None = None) -> bool:`
    4. `gateway_policy.py:L144`: 函数体开头增加 `if error is not None and isinstance(error, asyncio.TimeoutError): return False`
    5. `gateway_policy.py:L145-160`: `fatal_keywords` 元组中移除 `"timeout"`（裸关键字），新增 `"timed out"`、`"408"`、`"504"`
    6. `gateway_policy.py`: 文件顶部新增 `import asyncio`（若尚未导入）
    7. `gateway_call.py`: 搜索 `_is_fatal_failure(str(exc))` → 改为 `_is_fatal_failure(str(exc), error=exc)`
    8. `gateway_call.py`: 搜索 `_classify_failure_kind(str(exc))` → 改为 `_classify_failure_kind(str(exc), error=exc)`（如有调用）
  - **Acceptance Criteria**:
    - `_is_fatal_failure("asyncio.TimeoutError: ...", error=asyncio.TimeoutError())` 返回 `False`
    - `_is_fatal_failure("HTTP 429 rate limit exceeded")` 返回 `True`（429 仍 fatal）
    - `_is_fatal_failure("HTTP 408 Request Timeout")` 返回 `True`（408 判 fatal）
    - `_is_fatal_failure("random timeout message", error=None)` 返回 `False`（保守策略）
    - `_classify_failure_kind("...", error=asyncio.TimeoutError())` 返回 `FailureKind.TIMEOUT`
  - **Forbidden**: 不修改 429/403/quota/permission_denied 等其他 fatal_keywords 条目；不修改 `_open_model_cooldown` 的冷却时长；不修改 `_classify_cooldown_reason` 的分类逻辑
  - **Check Commands**: `pytest tests/ -v -k "gateway_policy or fatal or timeout"` ； `python -c "import asyncio; from astrmai.infrastructure.gateway.gateway_policy import GatewayPolicyMixin; print('import OK')"`
  - **Risk Notes**: 🟡 移除裸 `"timeout"` 后，部分 provider 非标准超时消息可能不被识别为 fatal → 保守策略不判 fatal（允许重试），优于误判 fatal（放弃模型）
  - _Requirements: R3_

- [ ] 4. R4: 统一 Gateway 双冷却入口 — ModelRouter 废弃 `_cooldown_until`，GatewayPolicy 成为冷却唯一入口
  - **Goal**: `ModelRouter` 仅维护健康评分，冷却统一由 `GatewayPolicy._model_cooldowns` 管理
  - **Files**:
    - ✏️ `astrmai/infrastructure/gateway/model_router.py` — 删除 `cooldown_until` + `get_ranked_models()` 新增 `cooldown_checker` 参数 + `report_failure` 不再设置冷却
    - ✏️ `astrmai/infrastructure/gateway/gateway_policy.py` — 新增 `_is_model_cooldown()` + `_build_attempt_queue()` 传递 `cooldown_checker` + 合并冷却常量
  - **Steps**:
    1. `model_router.py:L27`: 从 `ModelState` dataclass 中删除 `cooldown_until: float = 0.0` 字段
    2. `model_router.py:L50-51`: 删除 `BASE_COOLDOWN_SEC` / `MAX_COOLDOWN_SEC` 常量（移至 `gateway_policy.py`）
    3. `model_router.py:L73-79`: `get_ranked_models()` 签名新增 `cooldown_checker: Callable[[str, str], bool] | None = None` 参数
    4. `model_router.py:L109-114`: 将 `if state.cooldown_until > now` 替换为 `if cooldown_checker and cooldown_checker(pool_name, mid):`
    5. `model_router.py:L144-145`: 冷却中模型按 `cooldown_until` 排序的逻辑 → 若传入 `cooldown_checker`，冷却模型排在队尾（不按时间排序，简化实现）
    6. `model_router.py`: `report_failure()` 中删除设置 `state.cooldown_until = ...` 的逻辑（约 L195-210 区域）
    7. `gateway_policy.py`: 新增 `BASE_COOLDOWN_SEC = 30.0` / `MAX_COOLDOWN_SEC = 120.0` 常量（从 model_router 迁移）
    8. `gateway_policy.py`: 新增 `def _is_model_cooldown(self, pool_name: str, model_id: str) -> bool:` 方法，调用 `_model_cooldown_meta()` 并返回 `bool(meta)`
    9. `gateway_policy.py:L93-98`: `_build_attempt_queue()` 中 `get_ranked_models()` 调用增加 `cooldown_checker=self._is_model_cooldown`
  - **Acceptance Criteria**:
    - `ModelState` 不再有 `cooldown_until` 属性
    - `GatewayPolicy._is_model_cooldown("dialog", "model-a")` 在冷却中返回 `True`，冷却结束后返回 `False`
    - `get_ranked_models("dialog", ["a","b"], cooldown_checker=checker)` 正确过滤冷却模型
    - `report_failure()` 不再设置冷却时间（仅扣健康分）
    - `_build_attempt_queue()` 传递了 `cooldown_checker=self._is_model_cooldown`
  - **Forbidden**: 不修改 `_classify_cooldown_reason()`；不修改冷却时长 120s/1800s；不修改健康评分算法（SUCCESS_REWARD=1, FAILURE_PENALTY=-2, FATAL_PENALTY=-4）；不修改 `_filter_cooldown_attempt_queue()` 的兜底逻辑
  - **Check Commands**: `pytest tests/ -v -k "model_router or gateway_policy or cooldown"` ； `python -c "from astrmai.infrastructure.gateway.model_router import ModelRouter, ModelState; assert not hasattr(ModelState(), 'cooldown_until')"`
  - **Risk Notes**: 🟡 删除 `cooldown_until` 字段前需搜索所有引用（当前仅 `model_router.py` 内部 L27/L111/L145 三处）
  - _Requirements: R4_

---

### Phase 3: 数据完整性

- [ ] 5. R5: 集成 Token 估算器 — 新增 `token_estimator.py` + LaneManager 支持 token 阈值压缩
  - **Goal**: 上下文压缩可选基于 token 估算而非纯消息条数，`warm_zone_max_tokens` 配置项语义兑现
  - **Files**:
    - ➕ `astrmai/infrastructure/context_economy/token_estimator.py` — 新建
    - ✏️ `astrmai/shared/constants/defaults.py` — `InfrastructureSettings` 新增字段
    - ✏️ `astrmai/infrastructure/runtime/lane_manager.py` — `DEFAULT_POLICIES` 增加 token 阈值 + `_compact_history` 增加 token 估算分支
  - **Steps**:
    1. 创建 `token_estimator.py`：`estimate_tokens(text: str) -> int` 函数，实现字符/4 粗略估算（中文 ×1.5 + 英文 ×0.3）
    2. `defaults.py:L49`: `InfrastructureSettings` 新增 `token_estimator_enabled: bool = False`
    3. `defaults.py:L56-95`: `build_infrastructure_settings()` 中新增读取 `config.conversation.enable_token_estimator`（从 `_conf_schema.json` 读取）
    4. `lane_manager.py:L42`: `LanePolicy` 的 `("sys2", "dialog")` 条目增加 `summarize_threshold_tokens=1800`
    5. `lane_manager.py` 的 `LaneHistoryMixin._compact_history()` 中增加分支：当 `token_estimator_enabled` 且 `policy.summarize_threshold_tokens > 0` 时，用 `sum(estimate_tokens(msg) for msg in history)` 判断是否触发压缩
  - **Acceptance Criteria**:
    - `estimate_tokens("你好世界")` 返回 >0 的合理值
    - `estimate_tokens("Hello World")` 返回 >0 的合理值
    - `LanePolicy(store_mode="full", max_raw_turns=12, summarize_threshold_tokens=1800)` 的 `summarize_threshold_tokens == 1800`
    - `InfrastructureSettings().token_estimator_enabled` 默认为 `False`
  - **Forbidden**: 不修改 `DEFAULT_POLICIES` 中已有的 `max_raw_turns` 值；不修改 `LanePolicy` 现有字段签名；不引入 `tiktoken` 作为必须依赖
  - **Check Commands**: `pytest tests/ -v -k "token_estimator or context_economy"` ； `python -c "from astrmai.infrastructure.context_economy.token_estimator import estimate_tokens; assert estimate_tokens('hello') > 0"`
  - **Risk Notes**: 🟢 默认 `False`（向后兼容），开启后压缩行为变化但消息条数阈值仍为兜底
  - _Requirements: R5_

- [ ] 6. R6: 数据库迁移引入 Schema 版本追踪 — `PRAGMA user_version` 轻量方案
  - **Goal**: 数据库迁移从 try/except 批量硬扛改为版本号驱动的有序迁移，支持幂等和回滚诊断
  - **Files**:
    - ✏️ `astrmai/infrastructure/persistence/persistence_schema.py` — `_init_db_sync()` + `_init_db()` 重构
  - **Steps**:
    1. `persistence_schema.py`: 在文件顶部（class 之前）定义 `MIGRATIONS: list[tuple[int, str]]` 列表，将现有所有 ALTER TABLE 语句按版本号从 1 开始排序
    2. `persistence_schema.py:L79`: 修改 `_init_db_sync()`：
       a. 保留 `CREATE TABLE IF NOT EXISTS` 建表逻辑
       b. `current_version = db.execute("PRAGMA user_version").fetchone()[0]`
       c. `for version, ddl in MIGRATIONS: if version <= current_version: continue`
       d. `try: db.execute(ddl); db.execute(f"PRAGMA user_version = {version}")`
       e. `except sqlite3.OperationalError as e: if "duplicate column name" in str(e).lower(): db.execute(f"PRAGMA user_version = {version}"); else: raise`
    3. `persistence_schema.py`: 修改 `_init_db()` async 版本，使用 `asyncio.to_thread` 包装迁移循环
    4. `persistence_schema.py`: 删除原有的 `_apply_schema_patch_batch_sync()` 方法（其逻辑已被 MIGRATIONS 循环替代）
  - **Acceptance Criteria**:
    - 全新安装 → `PRAGMA user_version` == MIGRATIONS 最大版本号
    - 已有 DB（user_version=3）→ 重启 → 仅执行版本 >3 的迁移
    - 模拟迁移失败（无效 DDL 在 MIGRATIONS 列表中）→ 插件启动被阻止，日志包含失败版本号和 DDL
    - 旧有 `_apply_schema_patch_batch_sync` 方法已移除
  - **Forbidden**: 不修改现有 `CREATE TABLE` DDL；不删除/修改已有 ALTER TABLE 语句的内容（仅重新组织）；不引入 Alembic 或 sqlalchemy-migrate
  - **Check Commands**: `pytest tests/ -v -k "persistence_schema or migration"` ；手工：删除 `astrmai.db` → 启动插件 → 检查 `PRAGMA user_version`
  - **Risk Notes**: 🟡 `user_version` 从 0 开始，若已有 DB 的 `user_version` 被外部工具修改需打印 warning
  - _Requirements: R6_

---

### Phase 4: 并发正确性

- [ ] 7. R7: ProactiveDispatcher 增加 per-chat 注入锁 — 修复 detach/restore 竞态窗口
  - **Goal**: `ProactiveDispatcher._dispatch_locked()` 获取 per-chat `asyncio.Lock` 后再执行 coordinator detach/inject/restore，防止并发注入冲突
  - **Files**:
    - ✏️ `astrmai/proactive/dispatcher.py` — `_dispatch_locked()` 增加 `async with injection_lock`
    - ✏️ `astrmai/conversation/attention/gate.py` — `AttentionGate` 新增 `_proactive_injection_lock` + `get_proactive_lock()` + `process_event()` 增加 coordinator None 检测
  - **Steps**:
    1. `gate.py`: `AttentionGate.__init__` 新增 `self._proactive_injection_lock: dict[str, asyncio.Lock] = {}`
    2. `gate.py`: `AttentionGate` 新增方法 `def get_proactive_lock(self, chat_id: str) -> asyncio.Lock:`（懒初始化 per-chat lock）
    3. `gate.py`: `AttentionGate.process_event()` 方法开头增加：`if getattr(self, "runtime_coordinator", None) is None: logger.warning(...); return {"action": "PROACTIVE_BLOCKED"}`
    4. `dispatcher.py:L301`: `_dispatch_locked()` 在 `original_runtime_coordinator = ...` 之前增加 `injection_lock = self.attention_gate.get_proactive_lock(intent.chat_id)` + `async with injection_lock:`
    5. `dispatcher.py:L302-318`: 将原有的 detach/inject/restore 逻辑整体缩进到 `async with` 块内
  - **Acceptance Criteria**:
    - `AttentionGate.get_proactive_lock("chat_1")` 对同一 chat_id 返回相同 Lock 对象
    - 并发 2 个 ProactiveMessageIntent 对同一 chat 被序列化执行（第二个等待第一个的 Lock 释放）
    - coordinator 在 injection 完成后正确恢复为 `original_runtime_coordinator`
    - `process_event()` 检测到 `runtime_coordinator is None` 时打印 warning 日志并返回 `PROACTIVE_BLOCKED`
  - **Forbidden**: 不修改 `inject_external_event()` 方法签名；不修改 `ProactiveMessageIntent` 数据结构；不修改 `ChatRuntimeCoordinator` 现有逻辑；不在 `process_event()` 的 coordinator 检测中阻塞等待
  - **Check Commands**: `pytest tests/ -v -k "dispatcher or proactive"` ； `python -c "from astrmai.conversation.attention.gate import AttentionGate; print('import OK')"`
  - **Risk Notes**: 🟢 Lock 字典条目数 ≈ 活跃 chat 数，内存占用可接受（<2MB for 10000 chats）
  - _Requirements: R7_

---

> **写入 2 完成。** 接下来写入 3 将填充 Phase 5（配置收尾 + 最终验证）+ Dependency Chain + Summary + 执行检查清单。

---

### Phase 5: 配置收尾 + 最终验证

- [ ] 8. R1 _conf_schema.json 配置项收尾
  - **Goal**: `_conf_schema.json` 中 `sys3` 分组和 `conversation` 分组新增本次 Spec 引入的配置项
  - **Files**:
    - ✏️ `_conf_schema.json` — `sys3.items` + `conversation.items`
  - **Steps**:
    1. `_conf_schema.json:L733-739`: 在 `sys3.items` 中 `enable_work_mode` 之后新增 `computer_agent_sandbox_enabled` 配置项（`type: "bool"`, `default: false`, `hint: "开启后 ComputerAgent 才能加载 Python/Shell 工具..."`）
    2. `_conf_schema.json`: 在 `conversation.items` 中 `enable_prefix_caching` 之后新增 `enable_token_estimator` 配置项（`type: "bool"`, `default: false`, `hint: "开启后上下文压缩基于 token 估算而非纯消息条数"`）
  - **Acceptance Criteria**:
    - AstrBot WebUI 配置页中 `sys3` 分组显示 `computer_agent_sandbox_enabled` 开关
    - AstrBot WebUI 配置页中 `conversation` 分组显示 `enable_token_estimator` 开关
    - 两个开关默认均为关闭状态
  - **Forbidden**: 不修改已有配置项的 `default` 值；不修改已有配置项的 `description` / `hint`
  - **Check Commands**: `python -c "import json; schema = json.load(open('_conf_schema.json')); assert 'computer_agent_sandbox_enabled' in str(schema['sys3']); assert 'enable_token_estimator' in str(schema['conversation'])"`
  - **Risk Notes**: 🟢 纯配置新增，零运行时影响
  - _Requirements: R1, R5_

- [ ] 9. 全量回归验证
  - **Goal**: 确认 7 项修复未引入回归，全部现有测试通过，新增测试覆盖关键路径
  - **Files**: 无新增文件（仅执行已有测试 + 验证）
  - **Steps**:
    1. 运行全量测试：`pytest tests/ -v --tb=short`
    2. 确认 ≥ 70 passed，0 failed
    3. 运行 LSP 诊断：对所有变更文件执行 `lsp_diagnostics`
    4. 手工验证 R1：`/work` 命令默认返回 DECLINE → 修改配置 → 重启 → `/work` 正常执行
    5. 手工验证 R2：`from astrmai.infrastructure.security import InputSanitizer, OutputGuard, TokenBucket` 导入成功
    6. 手工验证 R6：删除 `astrmai.db` → 启动插件 → 确认 DB 创建 + `PRAGMA user_version` 正确
  - **Acceptance Criteria**:
    - 全量测试 ≥ 70 passed
    - 全部变更文件 `lsp_diagnostics` 0 error
    - R1 DECLINE 消息友好可读
    - R6 DB 迁移幂等
  - **Forbidden**: 不跳过任何已有测试；不修改已有测试的断言
  - **Check Commands**: `pytest tests/ -v --tb=short 2>&1 | tail -5` ； `lsp_diagnostics` 对 config.py, computer_agent.py, router.py, security/*, gateway_policy.py, model_router.py, gateway_call.py, token_estimator.py, lane_manager.py, persistence_schema.py, dispatcher.py, gate.py
  - **Risk Notes**: 🟢 纯验证任务，无代码改动
  - _Requirements: R1–R7_

- [ ] 10. LSP 诊断清理 + 最终检查
  - **Goal**: 确认全部变更文件 LSP 诊断通过，提交前最终检查
  - **Files**: 所有变更文件（14 个）
  - **Steps**:
    1. `lsp_diagnostics` 对每个变更文件，确认 0 error
    2. `git diff --stat` 确认改动范围与设计文档一致
    3. 检查是否有遗漏的 `# TODO` 注释（R2 中 prompt_envelope.py 和 output_guard.py 应有迁移注释）
    4. 检查 `import asyncio` 是否已添加（R3 需要）
    5. 确认 `MIGRATIONS` 列表中版本号连续、无遗漏
  - **Acceptance Criteria**:
    - 全部变更文件 lsp_diagnostics 0 error
    - `git diff --stat` 显示的文件数与 Summary 表一致
    - 无遗漏的 import 语句
  - **Forbidden**: 不在此任务中做任何代码修改（仅诊断和验证）
  - **Check Commands**: `lsp_diagnostics` × 14 个文件；`git diff --stat`
  - **Risk Notes**: 🟢 纯验证任务
  - _Requirements: ALL_

---

## Dependency Chain（依赖链）

```
Task 1 (R1 config + agent)
    │
Task 2 (R2 security module)
    │
Task 3 (R3 timeout fatal fix)
    │
Task 4 (R4 cooldown unify)  ← 修改 gateway_policy.py 同一文件，依赖 Task 3 的改动
    │
Task 5 (R5 token estimator)
    │
Task 6 (R6 db migration)
    │
Task 7 (R7 dispatcher lock)
    │
Task 8 (R1/R5 _conf_schema 收尾)
    │
Task 9 (全量回归验证)
    │
Task 10 (LSP 清理)
```

| 严格串行原因 |
|---|
| Task 4 依赖 Task 3 ⇒ 两者修改同一文件 `gateway_policy.py`，Task 4 需要 Task 3 改完的 `_is_fatal_failure` 签名作为基础 |
| Task 8 依赖 Task 1 + Task 5 ⇒ `_conf_schema.json` 的 `computer_agent_sandbox_enabled` 和 `enable_token_estimator` 配置项需要在对应代码改动完成后才能验证 |
| Task 9/10 依赖全部前置任务 ⇒ 最终验证 |

## Summary（变更汇总）

| # | 文件 | 改动 | 行数估计 |
|---|------|------|:------:|
| 1 | `config.py` | `Sys3Settings` 新增 `computer_agent_sandbox_enabled` | +3 |
| 2 | `astrmai/workmode/subagents/computer_agent.py` | 模块变量 + 字段 + `get_tool_set` 配置判断 + DECLINE 文案 | +8/-2 |
| 3 | `astrmai/workmode/router.py` | `__init__` 读取 config 注入 `sandbox_enabled` | +3/-1 |
| 4 | `astrmai/infrastructure/security/__init__.py` | 替换为 re-export | +5/-1 |
| 5 | `astrmai/infrastructure/security/input_sanitizer.py` | **新建** | +20 |
| 6 | `astrmai/infrastructure/security/output_guard.py` | **新建** | +10 |
| 7 | `astrmai/infrastructure/security/rate_limiter.py` | **新建** | +35 |
| 8 | `astrmai/conversation/contracts/prompt_envelope.py` | 添加迁移注释 | +2 |
| 9 | `astrmai/infrastructure/gateway/output_guard.py` | 添加迁移注释 | +2 |
| 10 | `astrmai/infrastructure/gateway/gateway_policy.py` | `_classify_failure_kind` + `_is_fatal_failure` 新增 `error` 参数 + 移除裸 `"timeout"` + `_is_model_cooldown` 新增 + 冷却常量迁移 | +30/-5 |
| 11 | `astrmai/infrastructure/gateway/model_router.py` | 删除 `cooldown_until` + `get_ranked_models` 新增 `cooldown_checker` + `report_failure` 不再设置冷却 + 删除冷却常量 | +15/-25 |
| 12 | `astrmai/infrastructure/gateway/gateway_call.py` | `_is_fatal_failure` 调用点传递 `error=exc` | +2/-2 |
| 13 | `astrmai/infrastructure/context_economy/token_estimator.py` | **新建** | +25 |
| 14 | `astrmai/shared/constants/defaults.py` | `InfrastructureSettings` 新增 `token_estimator_enabled` | +2 |
| 15 | `astrmai/infrastructure/runtime/lane_manager.py` | `DEFAULT_POLICIES` sys2/dialog 增加 `summarize_threshold_tokens` + `_compact_history` token 估算分支 | +15 |
| 16 | `astrmai/infrastructure/persistence/persistence_schema.py` | `MIGRATIONS` 列表 + `_init_db_sync` 重构 + 删除 `_apply_schema_patch_batch_sync` | +40/-25 |
| 17 | `astrmai/proactive/dispatcher.py` | `_dispatch_locked` 增加 `async with injection_lock` | +3 |
| 18 | `astrmai/conversation/attention/gate.py` | `_proactive_injection_lock` + `get_proactive_lock()` + `process_event` coordinator None 检测 | +15 |
| 19 | `_conf_schema.json` | `sys3` + `conversation` 新增配置项 | +13 |
| **Total** | **19 个文件** | **+248 / -61 行** | |

## 执行检查清单

- [ ] Task 1–8 全部完成（代码改动 + 配置）
- [ ] 全量测试 `pytest tests/ -v --tb=short` ≥ 70 passed
- [ ] 全部变更文件 `lsp_diagnostics` 0 error
- [ ] R1: `/work` 默认 DECLINE → 开启配置 → 正常执行
- [ ] R2: `from astrmai.infrastructure.security import InputSanitizer, TokenBucket` 导入成功
- [ ] R3: `_is_fatal_failure(asyncio.TimeoutError)` → `False`
- [ ] R4: `ModelState` 无 `cooldown_until` 属性
- [ ] R5: `estimate_tokens("hello")` > 0
- [ ] R6: 全新安装 `PRAGMA user_version` 正确
- [ ] R7: 并发 ProactiveMessageIntent 序列化执行
- [ ] `git diff --stat` 与 Summary 表一致
- [ ] 无遗漏的 `import` 语句

---

> **任务文档完成。** 全部 10 个任务 + Dependency Chain + Summary + 执行检查清单已写入。可进入 Kiro Phase 4（交叉验证）或直接开始执行任务。
