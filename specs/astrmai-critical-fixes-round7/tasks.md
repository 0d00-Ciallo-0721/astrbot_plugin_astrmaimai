# Implementation Plan — AstrMai 第二轮审查阻断级修复

> 本任务列表派生自同目录 `requirements.md`（5 条需求）与 `design.md`（5 个模块设计）。
> **执行原则**：Wave 1 内任务可并行，Wave 2 依赖 Wave 1。
> **状态规则**：所有任务初始 `- [ ]` 未完成。

---

## Overview

| Phase | Wave | 主题 | 任务 | 改动类型 |
|-------|------|------|:--:|------|
| Phase 1 | Wave 1 | 聊天功能修复 | T1–T4 | 局部 Bugfix |
| Phase 2 | Wave 2 | 热重载修复 | T5–T9 | 加 `refresh_config()` |
| Phase 3 | — | 回归验证 | T10 | 测试 |

---

## Tasks

### Phase 1: Wave 1 — 聊天功能修复 (T1–T4) — 可并行

- [ ] **T1. 修复 `_system2_entry` yield → await**
  - **Goal**: 将 `yield main_event.plain_result(fallback)` 替换为 `await self.runtime.reply_engine.handle_reply(main_event, fallback, chat_id)`
  - **Files**: `astrmai/app/plugin_facade.py` (写)
  - **Steps**:
    1. 读 `plugin_facade.py:481-486`
    2. 将 `yield main_event.plain_result(fallback)` 替换为 `await self.runtime.reply_engine.handle_reply(main_event, fallback, chat_id)`
    3. 确认函数中无其他 `yield` 语句
  - **Acceptance Criteria**:
    - `grep "yield" plugin_facade.py` 在 `_system2_entry` 方法中返回 0
    - `python -c "import ast; ast.parse(open('astrmai/app/plugin_facade.py').read()); print('ok')"` 无异常
  - **Forbidden**: 不修改 `system2_runner` 分支 (line 428-429)；不修改 `finally` 块
  - **Check Commands**: `python -c "import ast; ast.parse(open('astrmai/app/plugin_facade.py', encoding='utf-8').read()); print('ok')"`
  - **Risk Notes**: 🔴 影响面大 — 修复后 System2 路径恢复，需验证 LLM 调用正常
  - _Requirements: R1_

- [ ] **T2. 修复 `gateway_policy.py` cooldowns NameError**
  - **Goal**: 在 `_cleanup_model_cooldowns` 开头添加 `cooldowns = getattr(self, "_model_cooldowns", {})`
  - **Files**: `astrmai/infrastructure/gateway/gateway_policy.py` (写)
  - **Steps**:
    1. 读 `gateway_policy.py:15-19`
    2. 在 `now = monotonic()` 之后、`for` 循环之前插入 `cooldowns = getattr(self, "_model_cooldowns", {})`
  - **Acceptance Criteria**:
    - `grep "cooldowns = getattr" gateway_policy.py` 有结果
    - `python -c "from astrmai.infrastructure.gateway.gateway_policy import GatewayPolicyMixin; print('ok')"` 无异常
  - **Forbidden**: 不修改其他方法
  - **Check Commands**: `python -c "import ast; ast.parse(open('astrmai/infrastructure/gateway/gateway_policy.py', encoding='utf-8').read()); print('ok')"`
  - **Risk Notes**: 🟢 +1 行，零风险
  - _Requirements: R2_

- [ ] **T3. SubAgent 接入 Gateway**
  - **Goal**: 在 `BaseAgent.call()` 中优先走 Gateway，回退裸 provider
  - **Files**: `astrmai/workmode/subagents/base_agent.py` (写)
  - **Steps**:
    1. 读 `base_agent.py:55-100`
    2. 在获取 `ctx` 和 `event` 后插入 Gateway 优先路径（参考 design.md §3.3.2）
    3. 添加 `from ...infrastructure.runtime.lane_manager import LaneKey` 导入
    4. 保留现有 `ctx.tool_loop_agent()` 作为回退分支
  - **Acceptance Criteria**:
    - Gateway 路径包含 `gateway.tool_chat_in_lane_result(...)`
    - 回退路径保留 `ctx.tool_loop_agent(...)`
    - `python -c "from astrmai.workmode.subagents.base_agent import BaseAgent; print('ok')"`
  - **Forbidden**: 不修改返回值格式；不修改子类
  - **Check Commands**: `python -c "import ast; ast.parse(open('astrmai/workmode/subagents/base_agent.py', encoding='utf-8').read()); print('ok')"`
  - **Risk Notes**: 🟡 Gateway 上下文穿透需验证 — 已用 `getattr(ctx, "gateway", None)` 防御
  - _Requirements: R3_

- [ ] **T4. 恢复远程图片 URL 下载**
  - **Goal**: 用 `aiohttp` 异步下载远程图片 URL 并转 base64
  - **Files**: `astrmai/conversation/attention/vision_binding.py` (写)
  - **Steps**:
    1. 读 `vision_binding.py:30-36`
    2. 将 `extract_image_base64_from_url` 函数体重写为 design.md §3.4.2 中的实现
    3. 添加 `import base64` 和 `import aiohttp`（如未导入）
  - **Acceptance Criteria**:
    - 函数开头检查 URL 协议（仅 `http://`/`https://`）
    - 包含 10s 超时 + 10MB 上限
    - 错误路径返回 `""`
  - **Forbidden**: 不修改 `extract_image_base64_from_file`
  - **Check Commands**: `python -c "import ast; ast.parse(open('astrmai/conversation/attention/vision_binding.py', encoding='utf-8').read()); print('ok')"`
  - **Risk Notes**: 🟡 `aiohttp` 是 AstrBot 传递依赖，需确认可用
  - _Requirements: R4_

---

### Phase 2: Wave 2 — 热重载修复 (T5–T9) — 依赖 Phase 1

- [ ] **T5. GlobalModelGateway 加 `refresh_config()`**
  - **Goal**: 重建 `self.settings` 和更新 `self.config`
  - **Files**: `astrmai/infrastructure/gateway/model_gateway.py` (写)
  - **Steps**:
    1. 在 `GlobalModelGateway` 类中添加 `refresh_config(self, config)` 方法
    2. `self.config = config`
    3. `self.settings = build_infrastructure_settings(config)`（需要导入 `build_infrastructure_settings`）
  - **Acceptance Criteria**: `hasattr(gateway, "refresh_config")` 为 True
  - **Forbidden**: 不修改 `__init__`
  - **Check Commands**: `python -c "from astrmai.infrastructure.gateway.model_gateway import GlobalModelGateway; print(hasattr(GlobalModelGateway, 'refresh_config'))"`
  - **Risk Notes**: 🟢 纯赋值
  - _Requirements: R5_

- [ ] **T6. StateEngine 加 `refresh_config()` 并传播到子组件**
  - **Goal**: 更新 `self.config` + `mood_manager.config` + `energy_manager.config` + `relationship_engine.config`
  - **Files**: `astrmai/state/chat_state_service.py` (写)
  - **Steps**:
    1. 在 `StateEngine`（或 `ChatStateService`）类中添加 `refresh_config(self, config)` 方法
    2. `self.config = config`
    3. 传播到 `self.mood_manager.config`、`self.energy_manager.config`、`self.relationship_engine.config`（用 `hasattr` 防御）
  - **Acceptance Criteria**: 子组件的 config 与 `self.config` 同一对象引用
  - **Forbidden**: 不触发 state 持久化
  - **Check Commands**: `python -c "exec(open('astrmai/state/chat_state_service.py').read().split('class StateEngine')[1].split('class')[0]); print('ok')"`
  - **Risk Notes**: 🟢 纯赋值
  - _Requirements: R5_

- [ ] **T7. 其余 8 个组件加 `refresh_config()`**
  - **Goal**: 给以下组件各加一个 `refresh_config(self, config): self.config = config`
  - **Files**: 
    - `astrmai/infrastructure/runtime/lane_manager.py` (`LaneManager`)
    - `astrmai/conversation/ingress/sensors.py` (`PreFilters`)
    - `astrmai/state/energy/frequency_controller.py` (`FrequencyController`)
    - `astrmai/state/private_chat/private_chat_manager.py` (`PrivateChatManager`)
    - `astrmai/conversation/attention/gate.py` (`AttentionGate`)
    - `astrmai/learning/evolution_manager.py` (`EvolutionManager`)
    - `astrmai/memory/services/memory_engine.py` (`MemoryEngine`)
    - `astrmai/conversation/decision/judge.py` (`Judge` — 更新 `self.gateway` 的 config 引用，如 gateway 自身已刷新则无需额外操作)
  - **Steps**:
    1. 每个文件：找到类定义，添加 `refresh_config(self, config): self.config = config`
    2. 如果类没有 `self.config` 属性（如 `MemoryEngine` 用 `self.gateway`），适配到实际属性名
  - **Acceptance Criteria**: 每个组件的 `hasattr(comp, "refresh_config")` 为 True
  - **Forbidden**: 不触发任何 IO 或状态变更
  - **Check Commands**: 逐个 `grep "def refresh_config"` 确认
  - **Risk Notes**: 🟢 纯赋值，每个 +1~2 行
  - _Requirements: R5_

- [ ] **T8. 重写 `apply_hot_config` 遍历刷新**
  - **Goal**: 替换当前的硬编码刷新为遍历所有组件的通用模式
  - **Files**: `astrmai/app/plugin_facade.py` (写)
  - **Steps**:
    1. 读 `plugin_facade.py:80-95`
    2. 按 design.md §4.1.2 重写，遍历 `components` 列表，每个调用 `refresh_config`（try/except 防御）
  - **Acceptance Criteria**:
    - `grep "refresh_config" plugin_facade.py` 返回 ≥ 10
    - 每个调用在 try/except 内
  - **Forbidden**: 不改变返回值 `True`
  - **Check Commands**: `python -c "import ast; ast.parse(open('astrmai/app/plugin_facade.py', encoding='utf-8').read()); print('ok')"`
  - **Risk Notes**: 🟡 遍历调用，单个组件失败不影响其他
  - _Requirements: R5_

- [ ] **T9. 验证热重载完整链**
  - **Goal**: 确认所有组件都有 `refresh_config` 且在 `apply_hot_config` 中被调用
  - **Files**: 无代码修改（验证任务）
  - **Steps**:
    1. `grep -c "refresh_config"` 比对组件数量
    2. 人工确认 `apply_hot_config` 中的列表覆盖 design.md §4.1.1 中列出的 13 个组件
  - **Acceptance Criteria**: 列表包含全部 13 个组件名称
  - **Forbidden**: 不写代码
  - **Check Commands**: 人工对比
  - **Risk Notes**: 🟢 纯验证
  - _Requirements: R5_

---

### Phase 3: 回归验证 (T10)

- [ ] **T10. 全量回归 + LSP 诊断**
  - **Goal**: 确认修复不破坏现有测试，语法全部通过
  - **Files**: 无修改
  - **Steps**:
    1. `python -c "import astrmai"` 或等效批量 AST 解析
    2. `pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
    3. `lsp_diagnostics` 对全部变更文件
  - **Acceptance Criteria**:
    - 0 新增 SyntaxError
    - passed ≥ 810（不引入新失败）
    - 全部变更文件 lsp_diagnostics 无 error
  - **Forbidden**: 不做额外代码修改
  - **Check Commands**: `pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py 2>&1 | tail -3`
  - **Risk Notes**: 🟢 纯验证
  - _Requirements: R1–R5_

---

## Dependency Chain

```
T1 ─┬─→ T5 → T6 → T7 → T8 → T9
T2 ─┤       (Phase 2: 热重载，依赖 T1 完成)
T3 ─┤
T4 ─┘
(Phase 1: 4 任务可并行)
                              ↓
                             T10 (回归验证)
```

---

## Summary（变更汇总）

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| T1 | `plugin_facade.py` | `yield` → `await` | ±1 |
| T2 | `gateway_policy.py` | +1 行 | +1 |
| T3 | `base_agent.py` | Gateway 优先路径 | +15 / -3 |
| T4 | `vision_binding.py` | 函数体重写 | +20 / -3 |
| T5 | `model_gateway.py` | +`refresh_config()` | +8 |
| T6 | `chat_state_service.py` | +`refresh_config()` for StateEngine | +8 |
| T7 | 8 个组件文件 | 各 +`refresh_config()` | +1 × 8 |
| T8 | `plugin_facade.py` | 重写 `apply_hot_config` | +20 / -5 |
| T9 | — | 验证 | — |
| T10 | — | 回归测试 | — |
| **Total** | **~15 文件** | | **~+85 / -15** |

---

## 执行检查清单

- [ ] T1-T4 全部完成
- [ ] `grep "yield" plugin_facade.py` 在 `_system2_entry` 中 0 匹配
- [ ] `grep "cooldowns = getattr" gateway_policy.py` 有结果
- [ ] `grep "gateway.tool_chat_in_lane" base_agent.py` 有结果
- [ ] `grep "aiohttp" vision_binding.py` 有结果
- [ ] T5-T8 全部完成
- [ ] `grep -c "def refresh_config"` ≥ 10
- [ ] T10 全量回归 passed ≥ 810
- [ ] 0 新增 SyntaxError
