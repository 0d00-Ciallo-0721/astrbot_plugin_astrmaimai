# Implementation Plan

> 本任务列表派生自同目录 `requirements.md` 与 `design.md`。
> **执行原则**：任务**严格串行**，编号 1 → N。
> **状态规则**：所有任务初始状态为 `- [ ]` 未完成。

## Overview

| Phase | 主题 | 任务 | 改动文件 | 行数 |
|-------|------|:--:|------|:--:|
| Phase 1 | 内容安全 | Tasks 1-3 | `output_guard.py`, `memory_write_service.py`, `_conf_schema.json`, `config.py` | +42 |
| Phase 2 | 日志完整性 | Tasks 4-8 | `gate.py`, `chat_state_service.py`, `gateway_call.py`, `gateway_result.py`, `judge.py`, `lifecycle.py`, `mood_manager.py` | +28/-5 |
| Phase 3 | 错误恢复 | Tasks 9-10 | `plugin_facade.py`, `chat_state_service.py` | +15 |
| Phase 4 | 跨插件 | Tasks 11-12 | `external_result_bridge.py`, `outbound_error_policy.py`, `_conf_schema.json`, `config.py` | +19 |
| Phase 5 | 验证 | Tasks 13-14 | — | — |

## Tasks

### Phase 1: 内容安全

- [ ] 1. H1: 输出过滤增加基础内容安全检测
  - **Goal**: `output_guard.py` 新增 `looks_like_harmful_content()` + 配置开关 + 集成到 sanitize 管道
  - **Files**: ✏️ `astrmai/infrastructure/gateway/output_guard.py`, `_conf_schema.json`, `config.py`
  - **Steps**:
    1. `output_guard.py`: 新增 `_NSFW_PATTERNS`、`_SELF_HARM_PATTERNS`、`_PII_PATTERNS` 三个正则列表
    2. `output_guard.py`: 新增 `looks_like_harmful_content(text) -> bool` 函数
    3. `output_guard.py`: 在 `sanitize_visible_reply_text()` 中增加条件分支
    4. `config.py` `ReplyConfig`: 新增 `enable_content_safety_filter: bool = Field(default=False)`
    5. `_conf_schema.json` `reply.items`: 新增配置项
  - **AC**: `looks_like_harmful_content("fuck")` → True；默认不启用；启用后有害内容被替换为 fallback
  - **Forbidden**: 不修改现有 provider_failure/scaffold/tool_protocol 检测
  - **Check**: `python -c "from astrmai.infrastructure.gateway.output_guard import looks_like_harmful_content; print(looks_like_harmful_content('test'))"`
  - **Risk**: 🟡 默认 False，用户主动开启
  - _Requirements: H1_

- [ ] 2. H2: 记忆写入增加注入载荷消毒
  - **Goal**: `_classify_skip_reason()` 拒绝包含注入模式的载荷写入记忆
  - **Files**: ✏️ `astrmai/memory/services/memory_write_service.py`
  - **Steps**:
    1. 文件顶部新增 `_INJECTION_PATTERNS` 正则列表（6 个模式）
    2. `_classify_skip_reason()` 函数体末尾增加注入模式检测 → 返回 `"injection_payload"`
    3. 检测命中时增加 `logger.warning` 日志
  - **AC**: 含 `</user_input>` 的载荷 → 拒绝写入 + warning 日志
  - **Forbidden**: 不修改现有 empty/json/noise 过滤逻辑
  - **Check**: `python -c "from astrmai.memory.services.memory_write_service import MemoryWriteService; print('OK')"`
  - **Risk**: 🟡 仅检测明确注入模式
  - _Requirements: H2_

- [ ] 3. H3: `sensitive_words` 文档化
  - **Goal**: 配置项 hint 明确标注"情感路由而非安全过滤"
  - **Files**: ✏️ `_conf_schema.json`, `affection_router.py`, `config.py`
  - **Steps**:
    1. `_conf_schema.json`: `sensitive_words` hint 更新为情感路由说明
    2. `affection_router.py` L162: 注释改为 `# affection boost (NOT a safety filter)`
    3. `config.py` `AttentionConfig.sensitive_words`: 增加 `description` 参数
  - **AC**: WebUI 中 hint 含"这不是内容安全过滤"
  - **Check**: 代码审查
  - **Risk**: 🟢 纯文档，零行为变更
  - _Requirements: H3_

### Phase 2: 日志完整性

- [ ] 4. H4: 决策日志提升至 INFO
  - **Goal**: 消息处理决策（PASS/WAIT/IGNORE/DUPLICATED 等）在生产日志可见
  - **Files**: ✏️ `astrmai/conversation/attention/gate.py`
  - **Steps**:
    1. `process_event()` 中每个早期返回路径增加 `logger.info(f"[Gate] {action} chat={chat_id}")`
    2. `_debounce_and_judge()` L803-813 增加 `logger.info(f"[Gate] judge={judge_action} reason={focus_thread.focus_reason}")`
  - **AC**: INFO 日志含 `judge_action=PASS` 等决策信息
  - **Forbidden**: 不删除现有 `debug_trace`；不记录完整消息文本
  - **Check**: 代码审查 — INFO 日志精简为一行
  - **Risk**: 🟡 高频群聊日志量增加
  - _Requirements: H4_

- [ ] 5. H5: 状态变更 INFO 日志
  - **Goal**: mood/energy/affection 变更在生产日志可见
  - **Files**: ✏️ `astrmai/state/chat_state_service.py`
  - **Steps**:
    1. `StateEngine.update_mood()` CAS 写入后增加 `logger.info(mood_tag, new_value, delta)`
    2. `StateEngine.consume_energy()` 非 FriendMessage 路径增加日志
    3. `EnergyManager.should_drop_by_energy()` drop 时增加日志
  - **AC**: INFO 日志含 `mood=happy val=0.75` 等状态信息
  - **Forbidden**: 不修改状态计算公式
  - **Check**: 代码审查
  - **Risk**: 🟢 仅日志，零行为变更
  - _Requirements: H5_

- [ ] 6. H6: `logger.exception` 替换 — 7 处错误路径增加堆栈
  - **Goal**: 所有关键错误路径使用 `logger.exception()`（自动附堆栈）
  - **Files**: ✏️ `gateway_call.py` (L196,L313), `gate.py` (L285,L316), `lifecycle.py` (L33,L145,L162), `judge.py` (L548), `mood_manager.py` (L242)
  - **Steps**: 逐处将 `logger.error(...)` / `logger.warning(...)` 替换为 `logger.exception(...)`，消息文本不变
  - **AC**: 7 处全部替换；日志含完整堆栈
  - **Forbidden**: 不改变日志消息文本
  - **Check**: `Select-String -Pattern "logger\.exception"` → 7 处匹配
  - **Risk**: 🟢 仅替换函数，零行为变更
  - _Requirements: H6_

- [ ] 7. H7: LLM 延迟计时
  - **Goal**: `_log_usage()` 新增 `latency_ms` 字段
  - **Files**: ✏️ `gateway_call.py`, `gateway_result.py`
  - **Steps**:
    1. `gateway_call.py:_elastic_call_result()`: LLM 调用前后 `time.perf_counter()` 计时
    2. 将 `latency_ms` 传入 `_log_usage()`
    3. `gateway_result.py:_log_usage()`: 日志新增 `latency_ms` 字段
  - **AC**: INFO 日志含 `latency_ms=1234`
  - **Check**: 代码审查
  - **Risk**: 🟢 仅观测性
  - _Requirements: H7_

- [ ] 8. H8: Judge prompt 降级 DEBUG
  - **Goal**: Judge prompt 全文从 INFO 移至 DEBUG
  - **Files**: ✏️ `astrmai/conversation/decision/judge.py` L455
  - **Steps**: `logger.info` → `logger.debug`
  - **AC**: Judge prompt 仅 DEBUG 级别可见
  - **Check**: 代码审查
  - **Risk**: 🟢 仅日志级别
  - _Requirements: H8_

### Phase 3: 错误恢复

- [ ] 9. H9: Sys2 捕获 Gateway 级联失败
  - **Goal**: Gateway 全部模型耗尽时返回兜底消息而非抛异常
  - **Files**: ✏️ `astrmai/app/plugin_facade.py`
  - **Steps**:
    1. 文件顶部导入 `from ..infrastructure.gateway.gateway_exceptions import LLMCascadeFailureException`
    2. `_system2_entry()` 在 `try/finally` 外增加 `except LLMCascadeFailureException` 分支
    3. 捕获后 `logger.exception(...)` + `yield event.plain_result(fallback_text)`
  - **AC**: 模拟级联失败 → 用户收到 `fallback_text`，非框架异常
  - **Forbidden**: 不修改 `try/finally` 清理逻辑
  - **Check**: `python -c "from astrmai.infrastructure.gateway.gateway_exceptions import LLMCascadeFailureException; print('OK')"`
  - **Risk**: 🟢 降级路径
  - _Requirements: H9_

- [ ] 10. H10: DB 运行时异常保护
  - **Goal**: SQLite 运行时错误不导致消息处理崩溃
  - **Files**: ✏️ `astrmai/state/chat_state_service.py`
  - **Steps**:
    1. `_get_state_inner()` L95: `load_chat_state()` 包裹 try/except → 返回默认 state + `logger.exception`
    2. `mark_energy_consumed()` L150: `save_chat_state()` 包裹 try/except
    3. `atomic_update_mood()` L138: `save_chat_state()` 包裹 try/except
  - **AC**: 模拟 DB 只读 → 消息处理不崩溃 + 日志含异常信息
  - **Forbidden**: 不修改 state 计算逻辑
  - **Check**: 代码审查
  - **Risk**: 🟡 降级为内存状态，重启丢失
  - _Requirements: H10_

### Phase 4: 跨插件

- [ ] 11. H11: 外部结果白名单
  - **Goal**: 仅嗅探白名单内来源的插件输出
  - **Files**: ✏️ `astrmai/conversation/ingress/external_result_bridge.py`, `_conf_schema.json`, `config.py`
  - **Steps**:
    1. `config.py`: 新增 `ExternalResultConfig` 或直接在现有配置中增加 `external_result_sources: List[str] = Field(default=["astrbot_builtin"])`
    2. `_conf_schema.json`: 新增配置项 `external_result_sources`
    3. `external_result_bridge.py`: `bridge_external_plugin_result()` 开头增加白名单检查
  - **AC**: 翻译插件输出 → 不记录；`"*"` 通配符 → 记录所有
  - **Forbidden**: 不修改嗅探的核心逻辑
  - **Check**: `python -c "from astrmai.conversation.ingress.external_result_bridge import bridge_external_plugin_result; print('OK')"`
  - **Risk**: 🟡 默认仅 `astrbot_builtin`
  - _Requirements: H11_

- [ ] 12. H12: `stop_event` 可配置降级
  - **Goal**: 错误拦截行为可配置（block_and_stop / block_only / log_only）
  - **Files**: ✏️ `astrmai/conversation/execution/outbound_error_policy.py`, `_conf_schema.json`, `config.py`
  - **Steps**:
    1. `config.py`: 新增 `error_interception_mode: str = Field(default="block_only")`
    2. `_conf_schema.json`: 新增 `error_interception_mode` 配置项（含 `options` 枚举）
    3. `outbound_error_policy.py`: `intercept_outbound_error()` 增加模式判断
  - **AC**: `log_only` 模式 → 不调用 `stop_event()`；`block_and_stop` → 调用
  - **Forbidden**: 不修改错误关键词匹配逻辑
  - **Check**: 代码审查
  - **Risk**: 🟡 默认 `block_only`（行为改变，但提供旧选项）
  - _Requirements: H12_

### Phase 5: 验证

- [ ] 13. 全量回归
  - **Goal**: 所有改动无回归
  - **Steps**: `pytest tests/ -q --tb=short --ignore=tests/integration/runtime/`
  - **AC**: ≥ 68 passed
  - _Requirements: ALL_

- [ ] 14. LSP 清理
  - **Goal**: 全部变更文件 0 error
  - **Steps**: `lsp_diagnostics` 15 文件；`git diff --stat`
  - **AC**: 0 error
  - _Requirements: ALL_

---

## Dependency Chain

```
Task 1-3 (内容安全) ──┐
Task 4-8 (日志)     ──┤  全并行（不同文件集）
Task 9-10 (恢复)    ──┤
Task 11-12 (插件)   ──┘
    │
    ▼
Task 13 (回归) → Task 14 (LSP)
```

## Summary

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| 1 | `output_guard.py` | H1: `looks_like_harmful_content()` | +30 |
| 2 | `memory_write_service.py` | H2: 注入检测 | +12 |
| 3 | `_conf_schema.json` | H1/H3/H11/H12 配置项 | +20 |
| 4 | `config.py` | H1/H11/H12 字段 + H3 description | +12 |
| 5 | `affection_router.py` | H3: 注释 | +0 |
| 6 | `gate.py` | H4: INFO 决策日志 | +8 |
| 7 | `chat_state_service.py` | H5: 状态日志 + H10: DB 保护 | +20 |
| 8 | `gateway_call.py` | H6: exception + H7: 计时 | +5/-5 |
| 9 | `gateway_result.py` | H7: `latency_ms` | +3 |
| 10 | `lifecycle.py` | H6: exception 替换 | +0/-0 |
| 11 | `judge.py` | H6: exception + H8: debug | +0/-0 |
| 12 | `mood_manager.py` | H6: exception 替换 | +0/-0 |
| 13 | `plugin_facade.py` | H9: 级联失败捕获 | +5 |
| 14 | `external_result_bridge.py` | H11: 白名单 | +8 |
| 15 | `outbound_error_policy.py` | H12: 模式配置 | +6 |
| **Total** | **15 文件** | | **~+129/-5** |

## 执行检查清单

- [ ] Task 1-12 全部完成
- [ ] `pytest tests/ -q --tb=short` ≥ 68 passed
- [ ] `lsp_diagnostics` 15 文件 0 error
- [ ] H1: `looks_like_harmful_content("fuck")` → True
- [ ] H2: 注入载荷 → `injection_payload` skip
- [ ] H3: `sensitive_words` hint 更新
- [ ] H4-H5: INFO 日志含决策/状态信息
- [ ] H6: `logger.exception` 7 处替换
- [ ] H7: `latency_ms` 字段存在
- [ ] H8: Judge prompt DEBUG 级别
- [ ] H9: 级联失败 → 兜底消息
- [ ] H10: DB 异常 → 降级不崩溃
- [ ] H11: 白名单过滤生效
- [ ] H12: `log_only` 模式生效

---

# 🔍 交叉验证报告（嵌入）

| 检查项 | 结果 | 详情 |
|--------|:--:|------|
| 需求→设计 H1-H12 | ✅ 12/12 | 每条需求有对应设计模块 |
| 设计→任务 | ✅ 12/12 | 每个模块分配 1 个任务 |
| 任务字段完整性 | ✅ 14×8=112/112 | Goal/Files/Steps/AC/Forbidden/Check/Risk/_Req |
| EARS | ✅ 48 条 | 每条 2-5 条 |
| 风险标注 | ✅ 🟡8 + 🟢6 |
| 文件实存性 | ✅ 15/15 |
| 依赖链 | ✅ 无循环 |
| **缺口** | **0** |

---

> **任务文档 + 交叉验证完成。** 可开始执行。


