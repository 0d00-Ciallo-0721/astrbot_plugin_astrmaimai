# OPT-22 上下文渲染边界与外部插件桥

状态：**已实施** ｜ 优先级：P1 ｜ 依赖：OPT-17、OPT-18 ｜ 来源：Group Chat Plus 公共/自有窗口、AngelHeart prompt boundary、AstrBot 插件互操作要求

## 目标

- 用一个 actor-aware renderer 生成 Judge、Planner、Tool 与 trace 需要的会话文本。
- 明确区分公共可见历史、当前 owned batch、系统规则和派生资料。
- 所有用户内容、记忆、摘要和外部插件内容都放入不可信资料边界，不能伪装系统指令。
- 通过 AstrBot 公共 API 保留其他插件合法的 LLM request 增强，不使用 monkey patch。
- 用测试定性并修正 group wait 的 chat/thread 作用域。

## 基线证据

- `conversation/planning/message_renderer.py` 当前主要输出 `昵称: 文本`，可选 QQ ID，但未统一 target、reply/quote、topic、provenance 和媒体占位。
- 不同 prompt 构建点仍可能各自拼 recent transcript、lane history、last assistant 和 focus，形成重复或归属差异。
- `main.py` 有 `on_llm_request` hook，但 AstrMai programmatic gateway 调用是否经过第三方插件 hook 需基于 AstrBot 公共 API 验证。
- `plugin_facade.handle_group_reply_wait` 中 `group_thread_wait_enabled=True` 时读取 chat 级 wait info，False 时反而传 thread ID，命名与实现疑似反转。

## 上下文分区

1. **Trusted System Policy**
   - AstrMai 固定行为、安全和人格边界。
   - 只能由代码与可信配置生成。
2. **Persona Context**
   - 核心摘要、风格、按需八维切片。
   - 标记来源和版本，不含用户可伪造标签。
3. **Shared Visible Timeline**
   - 群内所有可见规范事件，含 actor ID、消息类型和时间。
4. **Owned Turn Batch**
   - 本轮触发 Attention 的 source event IDs。
5. **Derived Untrusted Context**
   - 记忆、摘要、视觉转述、网页结果、外部插件补充。
6. **Turn Instruction**
   - 结构化 TurnTarget、动作约束和输出形态。

每个区块有：

```text
block_type
source
provenance
trusted=false/true
source_event_ids
content_hash
char_count
```

## 实施步骤

1. **Renderer 契约测试**
   - actor ID、Bot ID、target、reply/quote、@列表、媒体和互动可见。
   - 用户文本包含 `<system>`、`[系统指令]` 或伪分隔符时安全转义。
2. **统一 MessageRenderer**
   - 输入 `ConversationEvent`，输出结构化文本或消息对象。
   - Judge/Planner/Tool 不再分别以昵称拼字符串。
   - 昵称保留可读性，ID 保证身份。
3. **公共窗口与 owned batch**
   - shared timeline 用于了解群聊。
   - owned batch 明确本轮为什么被触发。
   - 同一 event ID 不在两个区块重复正文；owned batch 可引用 shared event。
4. **上下文去重**
   - current focus、recent transcript、lane history、last assistant 按 event/commit ID 去重。
   - 上一轮 Bot 只来自 committed turn。
   - context block stats 记录去重前后大小。
5. **不可信边界**
   - 记忆、摘要、视觉、网页与外部插件结果统一包装。
   - derived block 不能改变 tool schema、system policy 或 TurnTarget。
   - 明确提示模型：资料可能错误，仅作事实候选。
6. **外部插件桥调查**
   - 先读 AstrBot skill 和当前版本源码，确认 programmatic LLM 调用的公开扩展点。
   - 用最小测试插件验证第三方 `on_llm_request` 是否收到 AstrMai 调用。
   - 若不经过，新增官方兼容桥：显式收集公开扩展结果、标记插件 ID/provenance/权限，再注入 derived block。
   - 禁止遍历私有 handler 并手工调用，禁止 monkey patch event bus。
7. **group wait 作用域**
   - 先写特征测试证明配置语义。
   - `group_thread_wait_enabled=True` 应按 thread ID 隔离；False 才使用 chat 级。
   - 修改后覆盖两个并发 thread 的 RESUME/OBSERVED/EXPIRED。
8. **预算**
   - 每个区块有独立字符预算与截断原因。
   - 先保身份、目标、未决问题，再保媒体描述和闲聊。

## 外部插件桥安全规则

- 仅接受已启用插件与公开扩展点返回。
- 每条内容记录 `plugin_id`、capability、生成时间和 source。
- 插件内容默认不可信，不得覆盖 Persona、Target、Tool schema。
- 插件失败不阻塞 AstrMai 主回复。
- AstrMai 判断是其他插件命令时直接 return，不停止事件传播。

## 测试矩阵

- 用户伪造 system/assistant 标签。
- 同名用户、改名用户、昵称带冒号与换行。
- reply+at+image 组合。
- shared/owned 重复事件。
- 其他插件注入成功、失败、超时、返回空。
- 两个群聊 thread 同时 wait。
- 超长群聊窗口按优先级截断。

## 观测字段

- `context_blocks`
- `shared_event_count`
- `owned_event_count`
- `deduplicated_event_count`
- `untrusted_block_count`
- `external_context_sources`
- `external_bridge_status`
- `context_chars_before/after`
- `group_wait_scope`

## 验收标准

- 模型看到的每条群聊消息都能区分 actor ID、Bot、target 和来源。
- 同一 event/commit 正文只注入一次。
- prompt injection 固定样本不能越过不可信边界。
- 外部插件合法增强可达，失败不阻塞；无 monkey patch。
- group thread wait 两个线程互不误恢复。
- 最终上下文字符数不高于基线，且身份信息完整。

## 风险与回退

- 外部插件桥必须先做 API 能力验证；无法确认公共 API 时只保留调查结果，不实现私有调用。
- renderer 切换可先 shadow 输出哈希/长度对比。
- group wait 修复可能改变现有并发行为，单独提交并提供配置回退。

## 完成记录

- 新增 `ContextBlock` / `ContextPackage` 契约，记录来源、provenance、可信级别、event IDs、哈希、字符数和截断原因。
- `MessageRenderer` 统一按规范事件输出 actor ID、消息类型、目标、回复引用、@、媒体和互动；shared timeline 与 owned batch 按 event ID 去重，owned batch 只引用正文。
- Planner 构建规范上下文包，加入当前发言人、提及对象和结构化 TurnTarget 指令，并把区块统计写入 turn ledger。
- 用户、群聊、记忆和外部插件派生文本经过边界标签或安全转义；最终发言人锁中的昵称和 ID 也按单行安全值处理。
- 外部结果仅通过 AstrBot `on_decorating_result` 公共事件入口接收，标记 `external_plugin` provenance；不手工调用私有 handler、不 monkey patch，也不把外部插件回复登记为 AstrMai committed reply。
- 修正 `group_thread_wait_enabled` 作用域反转：开启时按 thread 隔离，关闭时按 chat 共享。
- 回归验证：
  - `tests/regression/architecture/test_context_rendering_boundary.py`
  - `tests/unit/app/test_plugin_facade_turn_prepare.py`
  - `tests/test_external_result_bridge_refactor.py`
  - `tests/original_ported/test_prompt_refiner_focus_layout_ported.py`
  - `tests/test_attention_gate_refactor.py`
  - `tests/regression/conversation/test_persona_addressing_scope.py`
- 相关回归结果：`100 passed`。线上字符统计与外部插件实机互操作留待 OPT-24 灰度验收。
