# Implementation Plan — AstrMai 审计后加固

> 本任务列表派生自同目录 `requirements.md`（18 条需求）与 `design.md`（18 个模块设计）。
> **执行原则**：任务**严格串行**，编号 T1 → T18，后续任务依赖前一任务完成。
> **状态规则**：所有任务初始 `- [ ]` 未完成。

---

## Overview

本任务列表将 18 条需求与 18 个模块设计翻译为 18 个**严格串行**的可执行任务，按 5 个 Phase 组织：

| Phase | Wave | 主题 | 任务 | 改动类型 |
|-------|------|------|:--:|------|
| Phase 1 | Wave 1 | 静默异常日志补全 | T1–T4 | 纯加日志 |
| Phase 2 | Wave 2 | 配置模型/模式同步 | T5–T7 | 模型字段 + 验证 |
| Phase 3 | Wave 3 | 时间源 DB 边界修复 | T8–T10 | 钳制 guard + 注释 |
| Phase 4 | Wave 4 | 无限集合清理 | T11–T14 | TTL/LRU 清理 |
| Phase 5 | Wave 5 | 测试基础设施 | T15–T18 | mock 同步 + 新建测试 + 回归 |

---

## Tasks

### Phase 1: Wave 1 — 静默异常日志补全 (T1–T4)

- [ ] **T1. Gateway 层 4 处异常日志补全**
  - **Goal**: 为 `gate.py` 中 4 处静默 `except Exception:` 补 `logger.warning(exc_info=True)`
  - **Files**: `astrmai/conversation/attention/gate.py` (写)
  - **Steps**:
    1. 读取 `gate.py` 确认 lines 155, 517, 522, 682 的当前代码
    2. 在 155 行 `except Exception: return False` → 加 `logger.warning(f"[AstrMai-gate] is_wakeup_signal failed for {event.unified_msg_origin}", exc_info=True)`
    3. 在 517 行 `except Exception: pass` → 加 `logger.warning("[AstrMai-gate] is_command check failed", exc_info=True)`
    4. 在 522 行 `except Exception: return True` → 加 `logger.warning("[AstrMai-gate] should_process_message failed, degrading to True", exc_info=True)`
    5. 在 682 行 `except Exception: chat_state = None` → 加 `logger.warning(f"[AstrMai-gate] state_engine.get failed for {chat_id}", exc_info=True)`
  - **Acceptance Criteria**:
    - 4 处 `except Exception:` 块均有 `logger.warning(..., exc_info=True)`
    - 原有降级返回值不变（`False`/`pass`/`True`/`None`）
    - `lsp_diagnostics` on `gate.py` 无新增 error
  - **Forbidden**: 不改变异常捕获类型；不改变降级逻辑
  - **Check Commands**: `python -c "from astrmai.conversation.attention.gate import AttentionGate"` 无 ImportError
  - **Risk Notes**: 🟢 纯加日志，零风险
  - _Requirements: R1, R2_

- [ ] **T2. Executor / ContextCompaction / VisionBinding 等 9 处日志补全**
  - **Goal**: 为 executor.py (3处)、context_compaction.py (4处)、vision_binding.py (2处) 补日志
  - **Files**: `astrmai/conversation/execution/executor.py`, `astrmai/conversation/attention/context_compaction.py`, `astrmai/conversation/attention/vision_binding.py` (写)
  - **Steps**:
    1. `executor.py:105` — sanitize 失败 → `logger.debug(f"[AstrMai-exec] sanitize_event failed: {exc}", exc_info=True)`
    2. `executor.py:250` — failure_kind 分类失败 → `logger.debug(f"[AstrMai-exec] classify_failure_kind failed: {exc}", exc_info=True)`
    3. `executor.py:467` — temp_file 清理失败 → `logger.debug(f"[AstrMai-exec] temp_file_cleanup failed: {exc}", exc_info=True)`
    4. `context_compaction.py:284` — snapshot bootstrap → `logger.debug(f"[AstrMai-ctx] bootstrap_snapshot failed for {chat_id}: {exc}", exc_info=True)`
    5. `context_compaction.py:504` — get_sender_id → `logger.debug(...)`
    6. `context_compaction.py:1214,1231` — evaluate skip → `logger.debug(...)`
    7. `vision_binding.py:14` — file_to_base64 → `logger.debug(...)`
    8. `vision_binding.py:26` — file_open → `logger.debug(...)`
  - **Acceptance Criteria**:
    - 9 处均补日志
    - 使用 `logger.debug`（非关键路径）
  - **Forbidden**: 不改为 `logger.exception`（避免堆栈刷屏）
  - **Check Commands**: `python -c "import astrmai.conversation.execution.executor"` 无异常
  - **Risk Notes**: 🟢 纯加日志
  - _Requirements: R1_

- [ ] **T3. Persona Summarizer 8 处异常日志补全**
  - **Goal**: 为 `persona_summarizer.py` 中 8 处 `except Exception:` 补 `logger.exception()` 含切片标识
  - **Files**: `astrmai/memory/persona/persona_summarizer.py` (写)
  - **Steps**:
    1. 定位 8 处 `except Exception:` 所在行（~457, 494, 526, 558, 590, 620, 651, 683）
    2. 每处加 `logger.exception(f"[AstrMai-persona] slice '{slice_name}' failed for {chat_id}")`
    3. 切片名与需求文档 R3 中定义的 8 个标识对应
  - **Acceptance Criteria**:
    - 8 处各含唯一切片标识字符串
    - 降级默认值不变
  - **Forbidden**: 不改变切片计算逻辑
  - **Check Commands**: `python -c "from astrmai.memory.persona.persona_summarizer import PersonaSummarizer"` 无异常
  - **Risk Notes**: 🟡 使用 `logger.exception()` 含堆栈 → minor 日志量增加
  - _Requirements: R1, R3_

- [ ] **T4. ChatState / Memory 管线 / 剩余 ~20 处日志补全**
  - **Goal**: 批量补全 chat_state_service (5处)、memory 管线 (~12处)、其余文件 (~3处)
  - **Files**: `astrmai/state/chat_state_service.py`, `astrmai/memory/services/memory_engine.py`, `astrmai/memory/services/memory_turn_pipeline.py`, `astrmai/memory/services/summarizer.py`, `astrmai/memory/services/topic_summarizer.py`, `astrmai/state/private_chat/private_chat_manager.py`, `astrmai/workmode/subagents/cron_agent.py`, `astrmai/infrastructure/persistence/database_profile_relation.py`, `astrmai/conversation/ingress/event_utils.py`, `astrmai/state/mood/mood_manager.py` (写)
  - **Steps**:
    1. `chat_state_service.py` 5 处 → `logger.warning(f"[AstrMai-state] op failed for {chat_id}: {exc}", exc_info=True)`
    2. `memory_engine.py` / `memory_turn_pipeline.py` / `summarizer.py` / `topic_summarizer.py` (~12 处) → `logger.exception(f"[AstrMai-mem] ...")`
    3. `private_chat_manager.py` 2 处 → `logger.warning(...)`
    4. `cron_agent.py:86` → `logger.exception(f"[AstrMai-cron] job {job_id} failed")`
    5. `database_profile_relation.py` 2 处 → `logger.warning(f"[AstrMai-profile] construction failed: {str(profile_data)[:200]}")`
    6. `event_utils.py:9` → `logger.debug(...)`
    7. `mood_manager.py:106` → `logger.debug(...)`
  - **Acceptance Criteria**:
    - ~20 处均补日志
    - 严重程度分级：state → warning, memory → exception, util → debug
  - **Forbidden**: 不改变降级逻辑
  - **Check Commands**: `python -c "import astrmai.state.chat_state_service; import astrmai.memory.services.memory_engine"` 无异常
  - **Risk Notes**: 🟡 `logger.exception()` 在 memory 管线可能刷日志
  - _Requirements: R1, R4_

---

### Phase 2: Wave 2 — 配置模型/模式同步 (T5–T7)

- [ ] **T5. config.py 补 4 个缺失字段**
  - **Goal**: 在 Pydantic 配置模型中补 `enable_token_estimator`、`review_runner_interval_sec`、`review_runner_min_interval_sec`、`auto_recall_probability`
  - **Files**: `config.py` (写)
  - **Steps**:
    1. `ConversationConfig` 加 `enable_token_estimator: bool = False`
    2. `EvolutionConfig` 加 `review_runner_interval_sec: int = 60`
    3. `EvolutionConfig` 加 `review_runner_min_interval_sec: int = 45`
    4. `MemoryConfig` 加 `auto_recall_probability: float = 0.0`
  - **Acceptance Criteria**:
    - 4 字段均可通过 `AstrMaiConfig(**data)` 构造并读取
    - 默认值与 `_conf_schema.json` 一致
  - **Forbidden**: 不移除 `defaults.py` 中的 `getattr` 回退路径
  - **Check Commands**: `python -c "from config import AstrMaiConfig; c = AstrMaiConfig(); assert c.conversation.enable_token_estimator == False; assert c.evolution.review_runner_interval_sec == 60"`
  - **Risk Notes**: 🟢 纯加字段，不影响现有逻辑
  - _Requirements: R5, R6, R7_

- [ ] **T6. _conf_schema.json 补 3 个缺失字段定义**
  - **Goal**: 为 `review_runner_interval_sec`、`review_runner_min_interval_sec`、`auto_recall_probability` 补 schema 定义
  - **Files**: `_conf_schema.json` (写)
  - **Steps**:
    1. 在 `evolution.items` 下加 `review_runner_interval_sec` 定义（type: int, default: 60, hint）
    2. 在 `evolution.items` 下加 `review_runner_min_interval_sec` 定义（type: int, default: 45, hint）
    3. 在 `memory.items` 下加 `auto_recall_probability` 定义（type: float, default: 0.0, hint）
    4. `enable_token_estimator` 已存在 → 跳过
  - **Acceptance Criteria**:
    - JSON 结构合法（`python -m json.tool _conf_schema.json` 无报错）
    - 3 个新字段的 `default` 值与 `config.py` 一致
  - **Forbidden**: 不修改已有字段的定义
  - **Check Commands**: `python -c "import json; json.load(open('_conf_schema.json'))"` 无异常
  - **Risk Notes**: 🟢 JSON 纯数据
  - _Requirements: R6, R7_

- [ ] **T7. 全局对齐验证 + 差异报告**
  - **Goal**: 遍历 `_conf_schema.json` 所有字段与 `config.py` 模型字段，生成差异报告
  - **Files**: 无代码修改（仅产出验证报告）
  - **Steps**:
    1. `grep` 提取 `_conf_schema.json` 中所有 `items` 下的字段名
    2. `grep` 提取 `config.py` 中各 Pydantic 模型的所有 `: ` 字段定义
    3. diff → 输出差异清单（Markdown 表格）
    4. 确认 4 个缺失字段已在 T5/T6 补齐
  - **Acceptance Criteria**:
    - 差异报告显示 0 差异（T5/T6 完成后）
    - 报告归档到 `specs/astrmai-post-audit-hardening/`
  - **Forbidden**: 不做代码修改
  - **Check Commands**: 手动 diff
  - **Risk Notes**: 🟢 纯验证
  - _Requirements: R8_

---
---

### Phase 3: Wave 3 — 时间源 DB 边界修复 (T8–T10)

- [ ] **T8. DB 查询截止时间保护 — 5 站点 max-guard 钳制**
  - **Goal**: 在 5 个 DB 查询截止时间计算站点加 `max(0, delta)` 或 NTP 回拨检测
  - **Files**: `astrmai/infrastructure/persistence/database_service.py`, `astrmai/memory/services/memory_retrieval_service.py`, `astrmai/memory/services/session_memory_summarizer.py`, `astrmai/memory/services/v2_store.py` (写)
  - **Steps**:
    1. `database_service.py:146` — 加 `now = time.time(); cutoff = now - max_age; if cutoff > now: logger.warning(...); cutoff = 0.0`
    2. `memory_retrieval_service.py:353` — `time.time() - item.created_at` → `max(0.0, time.time() - item.created_at)`
    3. `session_memory_summarizer.py:43` — 同 pattern 1
    4. `v2_store.py:1086` — 同 pattern 1
    5. `v2_store.py:1135` — 同 pattern 1
  - **Acceptance Criteria**:
    - 5 站点均有钳制逻辑
    - `lsp_diagnostics` 无新增 error
  - **Forbidden**: 不替换 `time.time()` → `time.monotonic()`；不修改 DB 查询 WHERE 条件
  - **Check Commands**: `grep -n "max(0.*now\|cutoff.*clamp\|clock skew" database_service.py memory_retrieval_service.py session_memory_summarizer.py v2_store.py`
  - **Risk Notes**: 🟡 钳制为 0.0 可能查全量数据（性能风险），但数据完整性优先
  - _Requirements: R9_

- [ ] **T9. 聊天链路时间比较保护 — 3 站点 max-guard**
  - **Goal**: 在 judge.py、cognitive_loop.py、reply_freshness.py 加 `max(0.0, ...)` 钳制
  - **Files**: `astrmai/conversation/decision/judge.py`, `astrmai/conversation/planning/cognitive_loop.py`, `astrmai/conversation/execution/reply_freshness.py` (写)
  - **Steps**:
    1. `judge.py:191` — `delta = now - timestamp; if delta < 0: logger.warning(...); delta = 0`
    2. `cognitive_loop.py:682` — `idle_seconds = max(0.0, time.time() - last_reply_time)`
    3. `reply_freshness.py:55` — `reply_age = max(0.0, time.time() - event_ts)`
  - **Acceptance Criteria**:
    - 3 站点均有非负 delta 保证
  - **Forbidden**: 不替换 `time.time()`；不改变消息过滤逻辑
  - **Check Commands**: `python -c "import astrmai.conversation.decision.judge"` 无异常
  - **Risk Notes**: 🟢 纯钳制
  - _Requirements: R10_

- [ ] **T10. 状态存储时间源注释标注 — 7 站点**
  - **Goal**: 在 7 个 `time.time()` 与 DB/外部时间戳混用的站点添加 `# ponytail:` 注释
  - **Files**: `astrmai/state/relationship/relationship_engine.py`, `astrmai/state/mood/mood_decay.py`, `astrmai/state/chat_state_service.py`, `astrmai/state/user_profile_service.py`, `astrmai/memory/dream/promotion_engine.py`, `astrmai/memory/retrieval/hybrid_retriever.py`, `astrmai/memory/services/memory_scoring.py` (写)
  - **Steps**:
    1. 在每个 `time.time()` 调用前一行加 `# ponytail: wall-clock, mixed with DB values — do NOT replace with monotonic`
    2. 确认注释加在正确位置（与需求文档 R11 的站点清单对齐）
  - **Acceptance Criteria**:
    - `grep -rc "ponytail: wall-clock"` 返回 ≥ 7
    - 不影响代码执行
  - **Forbidden**: 不修改任何运行时代码
  - **Check Commands**: `grep -rn "ponytail: wall-clock" astrmai/state/ astrmai/memory/`
  - **Risk Notes**: 🟢 纯注释
  - _Requirements: R11_

---

### Phase 4: Wave 4 — 无限集合清理 (T11–T14)

- [ ] **T11. gate._proactive_injection_lock 同步清理**
  - **Goal**: 在 `_prune_stale_focus_pools()` 中同步 pop `_proactive_injection_lock`
  - **Files**: `astrmai/conversation/attention/gate.py` (写)
  - **Steps**:
    1. 定位 `_prune_stale_focus_pools()` 方法（Phase 4 已添加）
    2. 在 `self.focus_pools.pop(cid, None)` 后加 `self._proactive_injection_lock.pop(cid, None)`
  - **Acceptance Criteria**:
    - `grep "_proactive_injection_lock.pop" gate.py` 有结果
    - `lsp_diagnostics` on gate.py 无新增 error
  - **Forbidden**: 不改变 `_prune_stale_focus_pools` 的 TTL 和调度间隔
  - **Check Commands**: 同上 grep
  - **Risk Notes**: 🟢 +1 行
  - _Requirements: R12_

- [ ] **T12. chat_state_service._chat_locks LRU 上限 + 清理**
  - **Goal**: 加 `MAX_CHAT_LOCKS = 500` 上限，超限时 FIFO 清理至 300
  - **Files**: `astrmai/state/chat_state_service.py` (写)
  - **Steps**:
    1. 在 `__init__` 处加 `self._last_lock_prune: float = 0.0`
    2. 在 `_get_lock()` 开头加清理逻辑（参考 design.md §6.2.2）
    3. 加 `# ponytail: FIFO eviction when >500, keep 300` 注释
  - **Acceptance Criteria**:
    - `len(self._chat_locks)` 永不超过 500
    - 清理前将当前访问的 `chat_id` 移到 dict 末尾（LRU 近似）
  - **Forbidden**: 不修改 `_get_lock()` 的返回值类型（仍是 `asyncio.Lock`）
  - **Check Commands**: 人工压测：发送 >500 个不同 chat_id 后检查 `len(_chat_locks)`
  - **Risk Notes**: 🟡 FIFO 可能误删活跃 chat 的锁 → 使用 move-to-end 策略缓解
  - _Requirements: R13_

- [ ] **T13. memory_engine._disabled_cognitive_feedback_keys TTL 重构**
  - **Goal**: 将 `set[tuple]` 重构为 `dict[str, float]`，加 7 天 TTL 惰性清理
  - **Files**: `astrmai/memory/services/memory_engine.py` (写)
  - **Steps**:
    1. Line 86: `set[tuple[...]]` → `dict[str, float]`
    2. 加 `DISABLE_TTL_SEC = 7 * 86400`
    3. 新增 `_cognitive_feedback_key_str(signal) -> str` 方法
    4. 修改 `disable_cognitive_feedback()`：存储 `time.time()` + 惰性清理
    5. 修改成员检查 `key in dict`
  - **Acceptance Criteria**:
    - 成员检查仍为 O(1)
    - 8 天后旧条目被清理
    - `lsp_diagnostics` 无新增 error
  - **Forbidden**: 不改变 `_cognitive_feedback_key()` 的逻辑（仅改为返回 str）
  - **Check Commands**: `python -c "from astrmai.memory.services.memory_engine import MemoryEngine"` 无异常
  - **Risk Notes**: 🟡 内部数据结构变更 → 需确保所有引用点同步更新
  - _Requirements: R14_

- [ ] **T14. private_chat_manager._chat_to_user 同步清理**
  - **Goal**: `cleanup_stale_sessions()` 和 `close_session()` 中同步清理 `_chat_to_user`
  - **Files**: `astrmai/state/private_chat/private_chat_manager.py` (写)
  - **Steps**:
    1. 在 `cleanup_stale_sessions()` 末尾加：`for cid in closed_chat_ids: self._chat_to_user.pop(cid, None)`
    2. 在 `close_session()` 中加反向查找并 pop
  - **Acceptance Criteria**:
    - 关闭的 chat_id 在 `_chat_to_user` 中不存在
    - `lsp_diagnostics` 无新增 error
  - **Forbidden**: 不改变会话关闭的业务逻辑
  - **Check Commands**: `python -c "from astrmai.state.private_chat.private_chat_manager import PrivateChatManager"` 无异常
  - **Risk Notes**: 🟢 +4 行
  - _Requirements: R15_

---

### Phase 5: Wave 5 — 测试基础设施 (T15–T18)

- [ ] **T15. 测试 mock 同步 time.time → time.monotonic**
  - **Goal**: 将被测代码已改为 `monotonic()` 的测试中的 `@patch("time.time")` 同步更新
  - **Files**: `tests/unit/conversation/test_group_dialogue_store_and_compaction.py`, `tests/test_proactive_scheduler_refactor.py`, 等 ~5 文件 (写)
  - **Steps**:
    1. `grep -rn "time\.time\|monotonic" tests/` → 列出全部时间 mock
    2. 逐文件确认被测代码是否已改为 `monotonic()`
    3. 对已改的：`@patch("time.time")` → `@patch("time.monotonic")`，`mock_time` → `mock_monotonic`
    4. 对未改的（DB 边界）：保留 `@patch("time.time")`
  - **Acceptance Criteria**:
    - 测试失败数从 59 降至 ≤ 10（排除预存 SyntaxError）
    - 被 mock 的函数名与被测代码一致
  - **Forbidden**: 不修改测试的业务逻辑；不改变断言条件
  - **Check Commands**: `pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py` 失败数 ≤ 10
  - **Risk Notes**: 🟡 需人工判断哪些测试需更新 vs 保留
  - _Requirements: R16_

- [ ] **T16. safe_create_task 单元测试（新建）**
  - **Goal**: 新建测试覆盖 `safe_create_task()` 的正常/异常/返回值
  - **Files**: `tests/unit/shared/test_safe_create_task.py` (新建)
  - **Steps**:
    1. 创建 `tests/unit/shared/` 目录（`__init__.py` 如需要）
    2. 实现 `TestSafeCreateTask` 类含 3 个测试方法（参考 design.md §7.2.1）
    - `test_normal_completion_no_error_log`
    - `test_exception_triggers_error_log`
    - `test_returns_task_object`
  - **Acceptance Criteria**:
    - 3/3 passed
    - `logger.error` 在异常场景被调用 1 次
    - `logger.error` 在正常场景不被调用
  - **Forbidden**: 不修改 `safe_create_task()` 的实现
  - **Check Commands**: `pytest tests/unit/shared/test_safe_create_task.py -v`
  - **Risk Notes**: 🟢 纯新增
  - _Requirements: R17_

- [ ] **T17. Hook 异常韧性测试（新建）**
  - **Goal**: 新建测试覆盖 `main.py` 中 3 个 Hook 的异常不传播行为
  - **Files**: `tests/unit/test_hook_error_resilience.py` (新建)
  - **Steps**:
    1. 实现 `test_inject_reverse_session_handles_internal_error`
    2. 实现 `test_sniff_external_results_handles_error`
    3. 实现 `test_intercept_errors_handles_error`
    4. 修正 `tests/test_main_reverse_session_hook_refactor.py` 的导入路径（当前因相对导入失败）
  - **Acceptance Criteria**:
    - 3 个测试全部通过
    - Hook 内部异常不传播到框架（不抛出未捕获异常）
    - `test_main_reverse_session_hook_refactor.py` 能正常执行（不再因导入路径报错）
  - **Forbidden**: 不修改 `main.py` 的 Hook 逻辑
  - **Check Commands**: `pytest tests/unit/test_hook_error_resilience.py -v`
  - **Risk Notes**: 🟡 Hook 测试依赖 AstrBot mock → 可能需要额外 mock `AstrMessageEvent`
  - _Requirements: R18_

- [ ] **T18. 全量回归验证**
  - **Goal**: 运行完整测试套件，确认回归基线
  - **Files**: 无代码修改
  - **Steps**:
    1. `pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
    2. 确认 ≥ 836 passed（Phase 5 前基线）
    3. 确认无新增语法错误
    4. `python -c "import astrmai"` 无异常
    5. `lsp_diagnostics` 对全部变更文件无 error
  - **Acceptance Criteria**:
    - passed ≥ 836
    - 0 新增 SyntaxError
    - 全部变更文件 `lsp_diagnostics` 无 error
  - **Forbidden**: 不做额外代码修改（仅验证）
  - **Check Commands**: `pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py 2>&1 | tail -5`
  - **Risk Notes**: 🟢 纯验证
  - _Requirements: R1–R18_

---
---

## Dependency Chain

```
T1 ──→ T2 ──→ T3 ──→ T4          (Phase 1: 异常日志)
 │                        │
 │    ┌───────────────────┘
 ▼    ▼
T5 ──→ T6 ──→ T7                  (Phase 2: 配置同步)
 │                       
 ├────────────────────┐
 ▼                    ▼
T8 ──→ T9 ──→ T10              (Phase 3: 时间源修复)
 │                       
 ▼
T11 ──→ T12 ──→ T13 ──→ T14    (Phase 4: 集合清理)
 │                       
 ▼
T15 ──→ T16 ──→ T17 ──→ T18    (Phase 5: 测试 + 回归)
```

**并行机会**：
- T5 和 T8 可在 T4 完成后并行（配置不碰运行时，时间源不碰配置）
- T16 和 T17 可在 T15 完成后并行（两个独立测试文件）

---

## Summary（变更汇总）

| # | 文件 | 改动 | 行数估计 |
|---|------|------|:------:|
| **Phase 1** | | | |
| 1 | `gate.py` | +4 logger.warning | +4 |
| 2 | `executor.py` | +3 logger.debug | +3 |
| 3 | `context_compaction.py` | +4 logger.debug | +4 |
| 4 | `vision_binding.py` | +2 logger.debug | +2 |
| 5 | `persona_summarizer.py` | +8 logger.exception | +8 |
| 6 | `chat_state_service.py` | +5 logger.warning | +5 |
| 7 | `memory_engine.py` (T4) | +3 logger.exception | +3 |
| 8 | 其余 ~8 文件 | +20 logger | +20 |
| **Phase 2** | | | |
| 9 | `config.py` | +4 字段 | +4 |
| 10 | `_conf_schema.json` | +3 字段定义 | +15 |
| 11 | 对齐报告 (T7) | 新建 .md | — |
| **Phase 3** | | | |
| 12 | `database_service.py` | +3 guard | +3 |
| 13 | `memory_retrieval_service.py` | `max(0,...)` | ±1 |
| 14 | `session_memory_summarizer.py` | +3 guard | +3 |
| 15 | `v2_store.py` | +6 guard (×2) | +6 |
| 16 | `judge.py` | +4 guard | +4 |
| 17 | `cognitive_loop.py` | `max(0,...)` | ±1 |
| 18 | `reply_freshness.py` | `max(0,...)` | ±1 |
| 19 | 7 个状态文件 | +7 注释 | +7 |
| **Phase 4** | | | |
| 20 | `gate.py` (T11) | +1 pop | +1 |
| 21 | `chat_state_service.py` (T12) | +10 LRU | +10 |
| 22 | `memory_engine.py` (T13) | refactor TTL | +15 / -5 |
| 23 | `private_chat_manager.py` | +4 pop | +4 |
| **Phase 5** | | | |
| 24 | ~5 测试文件 | mock 同步 | +25 / -25 |
| 25 | `tests/unit/shared/test_safe_create_task.py` | 新建 | +40 |
| 26 | `tests/unit/test_hook_error_resilience.py` | 新建 | +60 |
| **Total** | **~35 源文件 + ~7 测试文件** | | **~+245 / -35** |

---

## 执行检查清单

- [ ] **Phase 1** — 全量日志补全完成
- [ ] `grep -c "logger.exception\|logger.warning"` 变更前后 > 48
- [ ] `python -c "import astrmai"` 无异常
- [ ] **Phase 2** — 配置同步完成
- [ ] `python -c "from config import AstrMaiConfig; c = AstrMaiConfig(); assert c.conversation.enable_token_estimator == False"`
- [ ] 对齐报告 0 差异
- [ ] **Phase 3** — 时间源修复完成
- [ ] `grep -c "max(0.*\|clock skew\|ponytail: wall-clock"` ≥ 15
- [ ] **Phase 4** — 集合清理完成
- [ ] `grep -c "pop.*None.*#\|TTL\|LRU\|_last_lock_prune"` ≥ 4
- [ ] **Phase 5** — 测试通过
- [ ] `pytest tests/ -q` ≥ 836 passed
- [ ] `pytest tests/unit/shared/test_safe_create_task.py -v` 3/3
- [ ] `pytest tests/unit/test_hook_error_resilience.py -v` 3/3
- [ ] **最终检查**
- [ ] 全部变更文件 `lsp_diagnostics` 无 error
- [ ] `python -c "import astrmai"` 无异常
- [ ] 无新增 `SyntaxError`
- [ ] git diff 确认无意外修改

---
_（tasks.md — 写入 3/3 完成。18 个任务 × 8 字段，Spec Phase 3 结束。）_
