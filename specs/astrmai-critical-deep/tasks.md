# Implementation Plan

> 本任务列表派生自同目录 `requirements.md` 与 `design.md`。
> **执行原则**：任务**严格串行**，编号 1 → N。
> **状态规则**：所有任务初始状态为 `- [ ]` 未完成。

## Overview

| Phase | 主题 | 任务 | 改动文件 | 行数 |
|-------|------|:--:|------|:--:|
| Phase 1 | 注入 + 乱码 | Tasks 1-2 | `prompt_refiner.py`, `self_lore_service.py` | +2/-1 |
| Phase 2 | 配置加固 | Tasks 3-5 | `config.py`, `defaults.py`, `_conf_schema.json` | +85/-5 |
| Phase 3 | 人设兜底 | Task 6 | `context_engine.py`, `_conf_schema.json` | +7 |
| Phase 4 | 最终验证 | Tasks 7-8 | — | — |

## Tasks

### Phase 1: 注入防御 + 乱码修复

- [ ] 1. C1: `sanitize_user_input` 接线 — 用户消息拼接前包裹 `<user_input>` 标签
  - **Goal**: Prompt Injection 防御链闭合——`sanitize_user_input` 被实际调用，用户消息在进入 prompt 前被 `<user_input>` 标签包裹
  - **Files**:
    - ✏️ `astrmai/conversation/planning/prompt_refiner.py` — L922-923
  - **Steps**:
    1. 定位 `prompt_refiner.py` L781：`focus_message_text = (prompt_envelope.focus_message_text or raw_user_text or prompt).strip()` — 确认此为焦点消息提取点，无需修改
    2. 定位 L922-923：`if focus_message_text:` → `sections.append(...)` — 拼接点
    3. 在 L922 的 `if` 块内、`sections.append` 之前增加：`safe_text = PromptEnvelope.sanitize_user_input(focus_message_text)`
    4. 将 L923 中的 `focus_message_text` 替换为 `safe_text`：`sections.append(f"---眼前正在对我说的---\n{await self._resolve_visual_memory(safe_text)}")`
    5. 确认 `PromptEnvelope` 已导入（搜索文件顶部的 import）
    6. 如果未导入，增加 `from ..contracts.prompt_envelope import PromptEnvelope`（或相对路径适配）
  - **Acceptance Criteria**:
    - `focus_message_text` 非空时被 `<user_input>\n...\n</user_input>` 包裹后进入 prompt
    - `focus_message_text` 为空时 `sections.append` 不执行（保持当前行为）
    - `sanitize_memory_content` 的现有调用（L883）不受影响
  - **Forbidden**: 不修改 `PromptEnvelope.sanitize_user_input()` 内部实现；不修改系统提示词规则（`context_engine.py:L556-557`）；不修改 `focus_message_text` 提取逻辑（L781）
  - **Check Commands**: `python -c "from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope; print(PromptEnvelope.sanitize_user_input('test'))"` ； `pytest tests/ -q -k "prompt_refiner" 2>&1 | tail -3`
  - **Risk Notes**: 🟡 标签包裹后下游关键词匹配可能受影响——但 `focus_message_text` 字段本身不变，仅拼接时包裹
  - _Requirements: C1_

- [ ] 2. C5: SelfLoreService 乱码修复 — 三处 mojibake 替换为正确 UTF-8 中文
  - **Goal**: `self_lore_service.py` 中三处 fallback 字符串从乱码变为可读中文
  - **Files**:
    - ✏️ `astrmai/memory/persona/self_lore_service.py` — L45, L58, L59
  - **Steps**:
    1. L45：`return "锛堣瀹氬師鍏哥绾匡級"` → `return "（设定原典离线）"`
    2. L58：`return "锛堟綔鎰忚瘑鍘熷吀搴撲腑鏈彂鐜扮浉鍏充簨瀹烇級"` → `return "（潜意识原典库中未发现相关事实）"`
    3. L59：`f"[缁濆浜嬪疄]: {result.summary or result.content}"` → `f"[绝对事实]: {result.summary or result.content}"`
    4. 确认文件以 UTF-8 编码保存：`python -c "open('astrmai/memory/persona/self_lore_service.py', encoding='utf-8').read()"` 不抛异常
    5. 如果 UTF-8 读取失败，用 Python 检测并转换编码后保存
  - **Acceptance Criteria**:
    - L45 字符串为可读中文 `"（设定原典离线）"`
    - L58 字符串为可读中文 `"（潜意识原典库中未发现相关事实）"`
    - L59 前缀为可读中文 `"[绝对事实]"`
    - 文件编码为 UTF-8
  - **Forbidden**: 不修改 `recall_persona_lore()` 的检索逻辑；不修改文件其他部分
  - **Check Commands**: `python -c "from astrmai.memory.persona.self_lore_service import SelfLoreService; print('OK')"` ； `python -c "with open('astrmai/memory/persona/self_lore_service.py', encoding='utf-8') as f: content = f.read(); assert '设定原典离线' in content; print('UTF-8 OK')"`
  - **Risk Notes**: 🟡 如果原文件是 GBK/Latin-1 编码，直接替换字节可能产生新乱码 → 先确认编码再操作
  - _Requirements: C5_

### Phase 2: 配置系统加固

- [ ] 3. C2: Pydantic 范围约束 — 全部概率/比例/正整数/正浮点字段增加 `Field(ge=, le=)`
  - **Goal**: 17 个 Pydantic 模型中所有概率/比例/正整数/正浮点字段增加范围校验
  - **Files**:
    - ✏️ `config.py` — 全部 17 个模型
  - **Steps**:
    1. 概率 0–1 字段（16 个）：`base_frequency`, `follow_up_probability`, `throttle_probability`, `image_recognition_probability`, `wakeup_min_energy`, `wakeup_cost`, `min_reply_threshold`, `cost_per_reply`, `daily_recovery`, `decay_rate`, `unknown_decay`, `time_decay_rate`, `prune_threshold`, `deep_temporal_alpha`, `maintenance_hot_beta`, `maintenance_temporal_stale_hot_threshold` → `Field(ge=0.0, le=1.0, ...)`
    2. 百分比 0–100 字段（1 个）：`meme_probability` → `Field(ge=0, le=100, ...)`
    3. 正整数 ≥1 字段（~12 个）：`max_steps`, `timeout`, `bg_pool_size`, `llm_retries`, `max_concurrent_llm_calls`, `batch_size`, `mining_trigger`, `recall_top_k`, `segment_min_len`, `no_segment_max_len`, `review_batch_size`, `summary_threshold` 等 → `Field(ge=1, ...)`
    4. 正整数 ≥0 字段（~10 个）：`mining_window_sec`, `mining_cooldown_sec`, `cleanup_interval`, `dream_interval_min`, `silence_threshold`, `wakeup_cooldown`, `profiling_msg_threshold`, `review_min_count` 等 → `Field(ge=0, ...)`
    5. 正浮点 ≥0 字段（~8 个）：`typing_speed_factor`, `backoff_factor`, `api_timeout`, `debounce_window`, `hot_zone_ttl_seconds`, `warm_zone_ttl_seconds`, `stale_reply_max_age_sec`, `recovery_silence_min` 等 → `Field(ge=0.0, ...)`
    6. 在 `AstrMaiConfig.__init__` 中增加 try/except 捕获 `ValidationError`：log warning + 使用默认值
  - **Acceptance Criteria**:
    - `ReplyConfig(base_frequency=1.5)` → `ValidationError`
    - `ReplyConfig(base_frequency=0.5)` → 正常实例化
    - 已有有效配置（默认值）→ 正常实例化（向后兼容）
    - 越界配置 → 插件不崩溃 + warning 日志
  - **Forbidden**: 不修改任何 `Field` 的 `default` 值；不添加 `@model_validator` 跨字段验证；不修改字段名称或类型
  - **Check Commands**: `python -c "from config import ReplyConfig; try: ReplyConfig(base_frequency=1.5); print('FAIL'); except Exception: print('PASS')"` ； `python -c "from config import ReplyConfig; ReplyConfig(base_frequency=0.5); print('PASS')"`
  - **Risk Notes**: 🟡 越界配置实例化时需 catch 而非崩溃——在 `AstrMaiConfig.__init__` 中增加 try/except
  - _Requirements: C2_

- [ ] 4. C3a: 互斥配置检测 — `AstrMaiConfig.__init__` 增加 work_mode/vision 与空模型池的互斥 warning
  - **Goal**: 互斥配置在启动时产生 warning 日志
  - **Files**:
    - ✏️ `config.py` — `AstrMaiConfig.__init__`（L233）
  - **Steps**:
    1. `AstrMaiConfig.__init__` 在 `super().__init__(...)` 后增加互斥检测：
       - `if getattr(self.sys3, "enable_work_mode", False) and not self.provider.agent_models: logger.warning(...)`
       - `if getattr(self.vision, "enable_vision", True) and not self.provider.vision_models: logger.warning(...)`
    2. 确认 `logger` 已导入（`from astrbot.api import logger`）
  - **Acceptance Criteria**:
    - `AstrMaiConfig(sys3={enable_work_mode:True}, provider={agent_models:[]})` → warning 日志含 "agent_models is empty"
    - `AstrMaiConfig(sys3={enable_work_mode:False})` → 无 warning
  - **Forbidden**: 不硬阻断插件启动（仅 warning）；不添加 `@model_validator`
  - **Check Commands**: `python -c "from config import AstrMaiConfig; import logging; logging.basicConfig(level=logging.WARNING); c = AstrMaiConfig(sys3={'enable_work_mode':True}, provider={'agent_models':[]})"`
  - **Risk Notes**: 🟢 纯日志增加，零运行影响
  - _Requirements: C3_

- [ ] 5. C3b: `or` 陷阱修复 + `enable_proactive` 补齐
  - **Goal**: `defaults.py` 中 `0 or default` 陷阱修复 + `LifeConfig` 补齐 `enable_proactive` 字段
  - **Files**:
    - ✏️ `astrmai/shared/constants/defaults.py` — L68-73
    - ✏️ `config.py` — `LifeConfig` 类
    - ✏️ `_conf_schema.json` — `life.items`
  - **Steps**:
    1. `defaults.py`：新增模块级辅助函数 `def _val(raw, default): return default if raw is None else raw`
    2. `defaults.py` L68-73：5 处 `or default` 替换为 `_val(value, default)`：
       - `max_concurrent_llm_calls`、`backoff_factor`、`api_timeout`、`rate_limit_model_cooldown_sec`、`quota_model_cooldown_sec`
    3. `config.py` `LifeConfig` 新增字段：`enable_proactive: bool = Field(default=True, description="是否启用主动发言功能")`
    4. `_conf_schema.json` `life.items` 新增 `enable_proactive` 配置项（`type: bool`, `default: true`）
  - **Acceptance Criteria**:
    - `_val(0, 3)` → `0`（`0` 不被覆盖为默认值）
    - `_val(None, 3)` → `3`（`None` 使用默认值）
    - `LifeConfig.enable_proactive` 存在且默认为 `True`
    - `_conf_schema.json` 中 `life` 分组含 `enable_proactive` 开关
  - **Forbidden**: 不删除 `_normalize_legacy_memory_namespace` 逻辑
  - **Check Commands**: `python -c "from config import LifeConfig; l = LifeConfig(); assert l.enable_proactive == True"` ； `python -c "from astrmai.shared.constants.defaults import _val; assert _val(0, 3) == 0; assert _val(None, 3) == 3"`
  - **Risk Notes**: 🟢 `enable_proactive` 默认 `True` 向后兼容（旧代码 `getattr(life, "enable_proactive", True)` 默认也是 `True`）
  - _Requirements: C3_

### Phase 3: 人设兜底

- [ ] 6. C4: 空人设兜底 — `context_engine.py` 增加 `DEFAULT_PERSONA_PROMPT` + `_conf_schema.json` hint 更新
  - **Goal**: Bot 在无人设配置时使用内置兜底人设而非裸系统提示词
  - **Files**:
    - ✏️ `astrmai/conversation/planning/context_engine.py` — 模块级常量 + `_load_persona_payload()`
    - ✏️ `_conf_schema.json` — `persona_id` hint
  - **Steps**:
    1. `context_engine.py` 文件顶部（import 之后、class 之前）新增常量：
       ```python
       DEFAULT_PERSONA_PROMPT = (
           "你是一个友好、自然、乐于助人的聊天助手。"
           "你喜欢用轻松的语气与人交流，偶尔带点幽默感。"
           "你善于倾听，会在合适的时机给出有价值的建议。"
       )
       ```
    2. `context_engine.py` `_load_persona_payload()` L309 之后增加兜底：
       ```python
       if not raw_prompt:
           raw_prompt = DEFAULT_PERSONA_PROMPT
           logger.warning("[AstrMai] No persona configured — using built-in default persona.")
       ```
    3. `_conf_schema.json` `persona_id` 的 hint 更新为：`"留空时使用 AstrBot 默认人设或内置兜底人设；填写后所有会话共用该人设。"`
  - **Acceptance Criteria**:
    - 未配置人设 → Bot 使用兜底人设（`DEFAULT_PERSONA_PROMPT` 被拼入 system prompt）
    - 已配置人设 → Bot 使用配置人设（兜底逻辑不触发）
    - warning 日志含 "using built-in default persona"
  - **Forbidden**: 不改变 `persona_id` 为空时的 per-session 人设行为；不修改 `PersonaSummarizer.get_summary()` 缓存逻辑
  - **Check Commands**: `python -c "from astrmai.conversation.planning.context_engine import DEFAULT_PERSONA_PROMPT; assert len(DEFAULT_PERSONA_PROMPT) > 10; print('OK')"`
  - **Risk Notes**: 🟢 兜底人设温和中性，不影响已有配置用户
  - _Requirements: C4_

### Phase 4: 验证

- [ ] 7. 全量回归验证
  - **Goal**: 确认 5 项修复未引入回归
  - **Steps**:
    1. `pytest tests/ -q --tb=short` → ≥ 68 passed
    2. 逐一验证 C1–C5 的 Check Commands
    3. 手工验证 C1：构造 Prompt Injection 消息 → 最终 prompt 含 `<user_input>` 标签
    4. 手工验证 C5：打开 `self_lore_service.py` 确认三处中文可读
  - **Acceptance Criteria**: ≥ 68 passed；0 新增 failure
  - **Forbidden**: 不跳过任何已有测试
  - **Check Commands**: `pytest tests/ -q --tb=short`
  - **Risk Notes**: 🟢 纯验证
  - _Requirements: C1–C5_

- [ ] 8. LSP 诊断 + 最终检查
  - **Goal**: 全部变更文件 LSP 0 error
  - **Steps**:
    1. `lsp_diagnostics` 对 7 个变更文件
    2. `git diff --stat` 确认改动范围与 Summary 一致
    3. 确认 C5 文件编码为 UTF-8
  - **Acceptance Criteria**: 0 lsp error；git diff 与 Summary 一致
  - **Forbidden**: 不在此任务中做代码修改
  - **Check Commands**: `lsp_diagnostics` × 7；`git diff --stat`
  - **Risk Notes**: 🟢 纯验证
  - _Requirements: ALL_

---

## Dependency Chain

```
Task 1 (C1 sanitize) ──┐
                        ├──► Task 2 (C5 乱码) ──► Task 3 (C2 范围约束)
                        │                              │
                        │                     Task 4 (C3a 互斥检测) ──┘
                        │                              │
                        │                     Task 5 (C3b or陷阱+enable_proactive)
                        │                              │
                        └──────────────────────────────┼──► Task 6 (C4 兜底人设)
                                                       │
                                                       ▼
                                               Task 7 (回归验证)
                                                       │
                                                       ▼
                                               Task 8 (LSP 清理)
```

| 严格串行原因 |
|---|
| Task 3/4/5 修改同一文件 `config.py`，建议串行避免冲突 |
| Task 6 修改 `context_engine.py` + `_conf_schema.json`，独立于 Task 1-5 |
| Task 1-2 修改不同文件（`prompt_refiner.py` vs `self_lore_service.py`），可并行 |

## Summary（变更汇总）

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| 1 | `astrmai/conversation/planning/prompt_refiner.py` | C1: `focus_message_text` 包裹 `<user_input>` | +2/-1 |
| 2 | `astrmai/memory/persona/self_lore_service.py` | C5: L45/L58/L59 字符串替换 | +0/-0 |
| 3 | `config.py` | C2: 17 模型 × 3-5 字段增加 `ge/le` + C3a: `__init__` 互斥检测 + C3b: `LifeConfig.enable_proactive` | +70 |
| 4 | `astrmai/shared/constants/defaults.py` | C3b: `_val()` + 5 处 `or` 陷阱修复 | +8/-5 |
| 5 | `_conf_schema.json` | C3b: `enable_proactive` + C4: `persona_id` hint | +7 |
| 6 | `astrmai/conversation/planning/context_engine.py` | C4: `DEFAULT_PERSONA_PROMPT` + 兜底逻辑 | +7 |
| **Total** | **6 文件** | | **~+94 / -6** |

## 执行检查清单

- [ ] Task 1–6 全部完成（代码改动 + 配置）
- [ ] C1: `focus_message_text` 被 `<user_input>` 标签包裹
- [ ] C2: `ReplyConfig(base_frequency=1.5)` → `ValidationError`
- [ ] C2: 越界配置 → 插件不崩溃（catch 在 `__init__`）
- [ ] C3a: `work_mode=True` + `agent_models=[]` → warning 日志
- [ ] C3b: `_val(0, 3)` → `0`（不覆盖）
- [ ] C3b: `LifeConfig.enable_proactive` 存在
- [ ] C4: 未配置人设 → 使用兜底人设 + warning 日志
- [ ] C5: `self_lore_service.py` 三处中文可读 + UTF-8 编码
- [ ] 全量测试 `pytest tests/ -q --tb=short` ≥ 68 passed
- [ ] `lsp_diagnostics` 全部变更文件 0 error
- [ ] `git diff --stat` 与 Summary 表一致

---

# 🔍 交叉验证报告（嵌入）

| 检查项 | 结果 | 详情 |
|--------|:--:|------|
| 需求→设计 C1 | ✅ | R1 → §3.1 |
| 需求→设计 C2 | ✅ | R2 → §4.1 |
| 需求→设计 C3 | ✅ | R3 → §4.2 |
| 需求→设计 C4 | ✅ | R4 → §5.1 |
| 需求→设计 C5 | ✅ | R5 → §5.2 |
| 设计→任务 C1 | ✅ | §3.1 → Task 1 |
| 设计→任务 C2 | ✅ | §4.1 → Task 3 |
| 设计→任务 C3 | ✅ | §4.2 → Task 4+5 |
| 设计→任务 C4 | ✅ | §5.1 → Task 6 |
| 设计→任务 C5 | ✅ | §5.2 → Task 2 |
| 任务字段完整性 | ✅ | 8×8=64/64 |
| EARS 覆盖 | ✅ | 24 条（每条 4-5 条） |
| 风险标注 | ✅ | 🟡4 + 🟢4 |
| 验证命令 | ✅ | 8/8 |
| 文件实存性 | ✅ | 6/6 现有 |
| 依赖链 | ✅ | 无循环/孤儿 |
| **缺口** | **0** | |

---

> **任务文档 + 交叉验证完成。** 全部三阶段 Spec 产出完毕，可开始执行。


