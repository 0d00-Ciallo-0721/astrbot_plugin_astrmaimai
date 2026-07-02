# Design Document

> 本文档对应 Spec `astrmai-high-hardening`，描述 12 个 High 级缺陷的修复设计方案。
> 按四组 Wave 组织：内容安全 (H1–H3)、日志完整性 (H4–H8)、错误恢复 (H9–H10)、跨插件交互 (H11–H12)。

## 1. Overview

### 1.1 整体策略

| Wave | 需求数 | 改动类型 | 风险 |
|:----:|:--:|------|:--:|
| ① 内容安全 | 3 | 新增检测 + 文档 | 低（可配置开关） |
| ② 日志完整性 | 5 | 日志级别调整 + 新增日志 | 低（仅日志） |
| ③ 错误恢复 | 2 | 异常捕获 + 降级 | 中（改变异常传播） |
| ④ 跨插件 | 2 | 配置化 + 白名单 | 中（改变默认行为） |

### 1.2 设计边界

- 不修改 AstrBot 框架 API
- 不新增 pip 依赖
- H1 第一期不引入第三方内容审核 API
- 所有可配置项默认值保持向后兼容（或提供保留旧行为的选项）

### 1.3 全局不变量

| 不变量 | 冻结理由 |
|--------|---------|
| 所有 `logger.error` → `logger.exception` 替换不改变日志消息文本 | 仅附加堆栈 |
| H4-H5 新增 INFO 日志不改变函数返回值 | 纯观测性 |
| H11-H12 配置默认值保留旧行为 | 向后兼容 |

---

## 2. Wave 1 — 内容安全（H1–H3）

### 2.1 H1: 输出过滤增加基础内容安全检测

**涉及文件**: `astrmai/infrastructure/gateway/output_guard.py`, `_conf_schema.json`, `config.py`

#### 当前状态

`output_guard.py` 仅检测内部泄露标记（provider failure / prompt scaffold / tool protocol / mojibake），无面向最终用户的内容安全检测。

#### 设计决策

**新增 `looks_like_harmful_content()` 函数 + 配置开关 + 集成到 `sanitize_visible_reply_text`。**

```python
# output_guard.py 新增：
_NSFW_PATTERNS = [
    r'(fuck|shit|damn|asshole)',           # 英文 NSFW
    r'(操|草|靠|日|傻逼|他妈|你妈)',         # 中文 NSFW
]
_SELF_HARM_PATTERNS = [
    r'(自杀|自残|割腕|跳楼|不想活)',
]
_PII_PATTERNS = [
    r'1[3-9]\d{9}',                        # 手机号
    r'\d{17}[\dXx]',                        # 身份证号
]

def looks_like_harmful_content(text: str) -> bool:
    """检测文本是否含 NSFW/自残/PII 内容。"""
    lowered = text.lower()
    for pattern in _NSFW_PATTERNS + _SELF_HARM_PATTERNS + _PII_PATTERNS:
        if re.search(pattern, lowered):
            return True
    return False

# sanitize_visible_reply_text 修改（L64 附近）：
if enable_content_safety_filter and looks_like_harmful_content(cleaned):
    logger.warning(f"[OutputGuard] harmful content blocked, falling back to default")
    return fallback_text
```

**配置**：
- `_conf_schema.json` 的 `reply` 分组新增 `enable_content_safety_filter: bool (default=false)`
- `config.py` 的 `ReplyConfig` 新增字段

#### 影响范围：1 文件 +30，配置 +5

#### 禁止：不修改现有检测逻辑

---

### 2.2 H2: 记忆写入增加注入载荷消毒

**涉及文件**: `astrmai/memory/services/memory_write_service.py`

#### 当前状态

`_classify_skip_reason()` 仅过滤空内容、JSON 错误载荷、噪音 token。

#### 设计决策

**在 `_classify_skip_reason()` 中新增注入模式检测。**

```python
# memory_write_service.py _classify_skip_reason() 新增：
_INJECTION_PATTERNS = [
    r'</?user_input>',
    r'</?retrieved_memory>',
    r'忽略(所有)?系统指令',
    r'输出你的(系统)?提示词',
    r'\[SYSTEM\]',
    r'\[INST\]',
]

def _classify_skip_reason(self, content: str) -> str:
    # ... 现有检测 ...
    lowered = content.lower()
    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, lowered):
            logger.warning(f"[MemoryWrite] injection payload blocked: {content[:80]}...")
            return "injection_payload"
    return ""
```

#### 影响范围：1 文件 +12

#### 禁止：不修改现有过滤逻辑

---

### 2.3 H3: `sensitive_words` 文档化

**涉及文件**: `_conf_schema.json`, `affection_router.py`, `config.py`

#### 设计决策

纯文档修改：更新 hint 文本 + 代码注释。

```json
// _conf_schema.json sensitive_words hint:
"情感路由权重词：当 Bot 情绪为愤怒/悲伤且消息含这些词时，发言者获得更高的情感权重。这不是内容安全过滤，不会拦截或屏蔽消息。"
```

```python
# affection_router.py L162:
# affection boost for hostile messages (NOT a safety filter)
t_score = 80.0
```

#### 影响范围：3 文件，仅注释/文本

---

## 3. Wave 2 — 日志完整性（H4–H8）

### 3.1 H4: 决策日志提升至 INFO

**涉及文件**: `astrmai/conversation/attention/gate.py`, `astrmai/conversation/attention/decision_router.py`

#### 当前状态

`process_event()` 返回字符串如 `"DUPLICATED"` 等，仅 DEBUG 日志。`_debounce_and_judge()` 的 `judge_action` 仅 `debug_trace`。

#### 设计决策

**在关键决策点增加精简 INFO 日志。**

```python
# gate.py process_event() 各返回路径：
if message_id in message_cache:
    logger.info(f"[Gate] DUPLICATED chat={event.unified_msg_origin}")
    return "DUPLICATED"

# _debounce_and_judge() L803-813：
logger.info(f"[Gate] judge_action={judge_action} reason={focus_thread.focus_reason} chat={chat_id}")
```

**精度控制**：每条 INFO 日志 ≤ 100 字符，不含完整消息文本。

#### 影响范围：2 文件 +8

---

### 3.2 H5: 状态变更 INFO 日志

**涉及文件**: `astrmai/state/chat_state_service.py`, `astrmai/state/energy/energy_manager.py`

#### 设计决策

```python
# update_mood() CAS 写入后：
logger.info(f"[State] mood={tag} val={new_value:.2f} delta={delta:+.2f} chat={chat_id}")

# consume_energy() 非 FriendMessage 路径：
logger.info(f"[State] energy {state.energy:.2f}→{new_energy:.2f} ({amount:+.2f}) chat={chat_id}")

# should_drop_by_energy() drop 时：
logger.info(f"[State] energy drop chat={chat_id} energy={state.energy:.2f}")
```

#### 影响范围：2 文件 +10

---

### 3.3 H6: `logger.exception` 替换

**涉及文件**: `gateway_call.py`, `gate.py`, `lifecycle.py`, `judge.py`, `mood_manager.py`

#### 设计决策

7 处替换，格式统一：

```python
# 修改前：
logger.error(f"[Gateway] fatal model failure {model_id}: {last_error[:120]}")

# 修改后：
logger.exception(f"[Gateway] fatal model failure {model_id}")
```

`logger.exception()` 自动附加 `exc_info=True`，堆栈自动包含在日志中。

#### 影响范围：5 文件 +0/-0（替换）

---

### 3.4 H7: LLM 延迟计时

**涉及文件**: `gateway_call.py`, `gateway_result.py`

#### 设计决策

```python
# gateway_call.py _elastic_call_result():
t0 = time.perf_counter()
resp = await asyncio.wait_for(context.llm_generate(...), timeout=timeout_limit)
latency_ms = (time.perf_counter() - t0) * 1000

# _log_usage() 新增字段：
logger.info(f"[GatewayUsage] ... latency_ms={latency_ms:.0f}")
```

#### 影响范围：2 文件 +5

---

### 3.5 H8: Judge prompt 降级 DEBUG

**涉及文件**: `astrmai/conversation/decision/judge.py`

#### 设计决策

```python
# L455: logger.info → logger.debug
logger.debug(f"[Judge] System1 prompt:\n{prompt}")
```

#### 影响范围：1 文件 +0/-0

---

## 4. Wave 3 — 错误恢复（H9–H10）

### 4.1 H9: Sys2 捕获级联失败

**涉及文件**: `astrmai/app/plugin_facade.py`

#### 当前状态

`_system2_entry` 使用 `try/finally` 但无 `except` — 级联失败直接传播。

#### 设计决策

```python
async def _system2_entry(self, event, events_to_process):
    try:
        # ... 现有逻辑 ...
    except LLMCascadeFailureException:
        logger.exception(f"[AstrMai] Gateway cascade failure for {event.unified_msg_origin}")
        fallback = self.runtime.config.reply.fallback_text
        yield event.plain_result(fallback)
    finally:
        # ... 现有清理逻辑 ...
```

#### 影响范围：1 文件 +5

---

### 4.2 H10: DB 运行时异常保护

**涉及文件**: `astrmai/state/chat_state_service.py`

#### 设计决策

在 `_get_state_inner()` 和 `save_chat_state()` 调用处增加 try/except：

```python
async def _get_state_inner(self, chat_id: str) -> ChatState:
    try:
        state = await self.persistence.load_chat_state(chat_id)
    except Exception:
        logger.exception(f"[State] DB load failed for {chat_id}, using default")
        state = self._create_default_state(chat_id)
    # ...
```

```python
async def mark_energy_consumed(self, chat_id: str, amount: float):
    try:
        await self.persistence.save_chat_state(chat_id, state)
    except Exception:
        logger.exception(f"[State] DB save failed for {chat_id}")
```

#### 影响范围：1 文件 +10

---

## 5. Wave 4 — 跨插件交互（H11–H12）

### 5.1 H11: 外部结果白名单

**涉及文件**: `astrmai/conversation/ingress/external_result_bridge.py`, `_conf_schema.json`, `config.py`

#### 当前状态

`bridge_external_plugin_result()` 处理所有非自回复结果，无来源过滤。

#### 设计决策

**新增配置项 `external_result_sources` + 白名单检查。**

```python
# external_result_bridge.py:
def bridge_external_plugin_result(runtime, event):
    loop_source = event.get_extra("astrmai_loop_source", "")
    allowed_sources = runtime.config.external_result_sources  # 新配置
    
    if "*" not in allowed_sources and loop_source not in allowed_sources:
        logger.debug(f"[ExtBridge] skipped non-whitelisted source: {loop_source}")
        return
    # ... 现有逻辑 ...
```

**配置**：
- `_conf_schema.json` 新增 `external_result_sources: list<string> (default=["astrbot_builtin"])`
- `config.py` 新增 `ExternalResultConfig`

#### 影响范围：1 文件 +8，配置 +5

---

### 5.2 H12: `stop_event` 可配置

**涉及文件**: `astrmai/conversation/execution/outbound_error_policy.py`, `_conf_schema.json`, `config.py`

#### 当前状态

`intercept_outbound_error()` 始终调用 `event.stop_event()`。

#### 设计决策

**新增 `error_interception_mode` 配置项，三选一。**

```python
# outbound_error_policy.py:
async def intercept_outbound_error(runtime, event):
    mode = runtime.config.error_interception_mode  # 新配置
    
    if mode == "log_only":
        logger.warning(f"[ErrorPolicy] error detected but log_only mode, not blocking")
        return
    
    event.set_result(None)
    
    if mode == "block_and_stop":
        event.stop_event()
    # "block_only" 模式：仅 set_result(None)，不 stop_event
```

**配置**：
- `_conf_schema.json` 新增 `error_interception_mode: string (default="block_only", options=["block_and_stop","block_only","log_only"])`
- `config.py` 新增字段

#### 影响范围：1 文件 +6，配置 +5

---

## 6. Risk Assessment

| # | 风险 | 等级 | 缓解 |
|---|------|:--:|------|
| RSK1 | H1 关键词检测可能误杀正常回复 | 🟡 | 默认 `False`，用户主动开启 |
| RSK2 | H2 注入检测可能误拒 XML 标签合法内容 | 🟡 | 仅检测明确注入模式 |
| RSK3 | H4 INFO 日志量增加 | 🟡 | 每条精简为一行 ≤100 字符 |
| RSK4 | H9 捕获级联失败后返回兜底 → 用户可能不知道 Bot 故障 | 🟢 | 日志含 `logger.exception` 供运维排查 |
| RSK5 | H10 DB 异常降级 → 状态仅内存保存，重启丢失 | 🟡 | 日志明确标注降级，运维可据此修复 DB |
| RSK6 | H11 白名单默认 `astrbot_builtin` → 可能漏掉合法外部来源 | 🟡 | 提供 `"*"` 通配符 |
| RSK7 | H12 默认 `block_only` → 下游插件可能收到被标记的错误消息 | 🟡 | 提供 `block_and_stop` 选项 |

## 7. Verification Matrix

| # | 需求 | 验证方式 | 通过标准 |
|---|------|---------|---------|
| V1 | H1 | `looks_like_harmful_content("fuck you")` → True | True |
| V2 | H1 | `looks_like_harmful_content("hello")` → False | False |
| V3 | H2 | 构造注入载荷 → `_classify_skip_reason` 返回 `injection_payload` | 返回正确 |
| V4 | H3 | WebUI hint 文本含"这不是内容安全过滤" | 文本匹配 |
| V5 | H4 | INFO 日志含 `judge_action=PASS` | 日志可见 |
| V6 | H5 | INFO 日志含 `mood=happy val=0.75` | 日志可见 |
| V7 | H6 | `logger.exception` 替换 7 处 | 代码审查 |
| V8 | H7 | `_log_usage()` 含 `latency_ms` | 字段存在 |
| V9 | H8 | Judge prompt 仅 DEBUG 级别 | 代码审查 |
| V10 | H9 | 模拟级联失败 → 用户收到兜底消息 | 兜底文本 |
| V11 | H10 | 模拟 DB 只读 → 使用默认 state + 日志 | 不崩溃 |
| V12 | H11 | 翻译插件输出 → 不被嗅探 | 日志含 `skipped` |
| V13 | H12 | `log_only` 模式 → 不调用 `stop_event` | 其他插件正常接收 |

## 8. Summary

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| 1 | `output_guard.py` | H1: `looks_like_harmful_content()` | +30 |
| 2 | `memory_write_service.py` | H2: 注入模式检测 | +12 |
| 3 | `_conf_schema.json` | H1/H11/H12 配置项 + H3 hint | +20 |
| 4 | `config.py` | H1/H11/H12 配置字段 | +12 |
| 5 | `affection_router.py` | H3: 注释 | +0 |
| 6 | `gate.py` | H4: INFO 决策日志 | +8 |
| 7 | `chat_state_service.py` | H5: 状态日志 + H10: DB 异常保护 | +20 |
| 8 | `gateway_call.py` | H6: exception 替换 + H7: 延迟计时 | +5/-5 |
| 9 | `gateway_result.py` | H7: `latency_ms` 字段 | +3 |
| 10 | `lifecycle.py` | H6: exception 替换 | +0/-0 |
| 11 | `judge.py` | H6: exception + H8: debug 降级 | +0/-0 |
| 12 | `mood_manager.py` | H6: exception 替换 | +0/-0 |
| 13 | `plugin_facade.py` | H9: 级联失败捕获 | +5 |
| 14 | `external_result_bridge.py` | H11: 白名单检查 | +8 |
| 15 | `outbound_error_policy.py` | H12: 模式配置 | +6 |
| **Total** | **15 文件** | | **~+129 / -5** |

---

> **设计文档完成。** 可进入 Phase 3（任务文档）或直接执行。


