# 表达风格与黑话注入链路审计（2026-08-12）

状态：**只读审计完成，尚未实施修复** ｜ 范围：表达风格学习、全局黑话、被动注入、模型主动查询、Bot 主动发言

## 审计目标

本次审计回答以下问题：

1. 已审核的表达风格和黑话能否在普通聊天中自动进入模型上下文。
2. Bot 能否在自动注入不足时主动查询相关信息。
3. Bot 主动发言是否复用同一套学习注入链路。
4. 从“数据被选中”到“模型实际看到”之间是否存在丢失或观测误差。

本文中的“主动”需要区分两种语义：

- **模型主动查询**：模型在工具循环中主动调用工具查询黑话、记忆或表达习惯。
- **Bot 主动发言**：定时、日程或主动调度生成候选事件，再进入正常 Attention / Planner / Reply 链。

## 总体结论

当前链路的“选择与检索阶段”比较清晰，但“最终提示词可见性”存在断层：

- 普通非快速回复可以被动选择当前群的表达风格，并精确匹配当前消息中的全局黑话。
- 快速回复、强唤醒短消息和近距离承接模式会把整个软背景预算设为 `0`，导致已选中的表达风格和黑话在最终提示词中消失。
- 模型可以通过 `omni_perception_query` 主动查询全局黑话，但工具可用性仍受 tier 和工具家族过滤限制。
- 表达风格没有独立主动查询工具，也没有二阶段披露包；自动注入丢失后不存在恢复路径。
- Bot 主动发言复用正常 Planner，表达风格原则上可用；但黑话匹配优先读取合成的主动候选文本，可能漏掉近期真实群消息中的黑话。
- 当前 trace 在 InputLoader 选中内容时就记为 `injected=True`，没有反映 PromptRefiner 后续裁剪，因此可能出现“日志显示已注入、模型实际没看到”的假阳性。

## 当前被动注入链路

### 表达风格

```text
当前会话近期消息
→ PlanningInputLoader._load_expression_habits()
→ ExpressionSelector.select_with_trace()
→ 查询当前群 shared_scope 下 approved + active 的 expression_pattern
→ 根据语境与冷却策略选择候选
→ 写入 stable_expression_habits
→ ContextEngine 包装为“语言习惯参考”
→ 进入 soft_background_sections.stable_expression
→ PromptRefiner 按预算决定是否保留
→ Dialog 模型
```

源码证据：

- `astrmai/conversation/planning/planning_input_loader.py:296`
- `astrmai/conversation/planning/expression_policy.py:372`
- `astrmai/conversation/planning/expression_policy.py:403`
- `astrmai/conversation/planning/expression_policy.py:697`
- `astrmai/conversation/planning/context_engine.py:136`
- `astrmai/conversation/planning/prompt_refiner.py:183`

表达风格的作用域设计正确：证据来源可以来自不同群友，但注入按当前群 `shared_scope=chat_id` 隔离，不会默认把甲群风格注入乙群。只有人工批准且状态为 `active` 的候选会参与回复。

### 全局黑话

```text
当前消息正文
→ PlanningInputLoader._load_jargon_explanation()
→ JargonRetrievalPolicy.search(session_id="", exact_only=True)
→ 查询 global_jargon 中 active 的词条
→ 仅保留当前消息确实出现的词或别名
→ 写入 stable_jargon_explanation
→ ContextEngine 包装为“全局黑话参考”
→ 进入 soft_background_sections.stable_jargon
→ PromptRefiner 按预算决定是否保留
→ Dialog 模型
```

源码证据：

- `astrmai/conversation/planning/planning_input_loader.py:372`
- `astrmai/conversation/planning/planning_input_loader.py:405`
- `astrmai/memory/services/jargon_retrieval_policy.py:9`
- `astrmai/memory/services/jargon_retrieval_policy.py:91`
- `astrmai/conversation/planning/context_engine.py:145`
- `astrmai/conversation/planning/prompt_refiner.py:183`

黑话采用全局统一词典，自动路由只看当前消息的精确命中。这能避免旧话题中的黑话无条件污染新回复，是正确的基本边界。

## 当前主动获取链路

### 黑话主动查询

`omni_perception_query` 位于渐进式披露的全局 `core` 工具包：

```text
Planner 构建完整候选工具集
→ ToolDisclosurePlanner 默认加入 core
→ core 披露 omni_perception_query
→ 工具家族过滤
→ 模型调用 OmniPerceptionTool
→ MemoryToolService.omni_query()
→ 并行查询记忆、画像、节点、反思和全局黑话
→ 工具结果返回模型
```

源码证据：

- `astrmai/conversation/planning/tool_disclosure.py:10`
- `astrmai/conversation/planning/tool_disclosure.py:13`
- `astrmai/conversation/planning/planner_side_inputs.py:555`
- `astrmai/conversation/planning/planner_side_inputs.py:1019`
- `astrmai/conversation/planning/tools/pfc_tools.py:789`
- `astrmai/memory/services/memory_tool_service.py:142`

因此，黑话主动查询不是纸面能力，正常渐进披露路径会从完整候选工具集中选择真实工具实例。

但该能力不是无条件可用：

- `requested_tier=none` 时不会提供任何工具。
- 社交意图或允许家族可能把 `query` 家族过滤掉。
- `bot_capability_lookup` 是受保护保底工具，`omni_perception_query` 没有同等级保护。
- `OmniPerceptionTool` 的本地降级实现中 `_fetch_jargon()` 直接返回 `None`；正常注入的 `MemoryToolService` 不可用时，工具会失去黑话查询能力。

### 表达风格主动查询

当前不存在以下能力：

- 查询当前群已批准表达风格的独立工具。
- 表达风格专属工具包。
- 表达风格二阶段渐进披露。
- 自动注入被裁剪后的主动恢复路径。

因此表达风格目前是**纯被动注入能力**。模型不能主动确认“这个群通常怎么说话”，也不能在自动注入为空时补查。

## Bot 主动发言链路

主动调度不会使用独立回复生成器，而是构造 `astrmai_is_proactive_event=True` 的合成事件，再通过 `AttentionGate.inject_external_event()` 回到正常 Planner：

```text
主动调度器
→ 构造 [主动开口候选] 合成事件
→ AttentionGate.inject_external_event()
→ 正常 Planner
→ PlanningInputLoader
→ ContextEngine / PromptRefiner
→ 正常 Dialog / Reply
```

源码证据：

- `astrmai/proactive/dispatcher.py:385`
- `astrmai/proactive/dispatcher.py:397`
- `astrmai/proactive/dispatcher.py:430`

这意味着主动发言与被动回复原则上能够共享表达和黑话注入，不会形成第二套不可控生成链。

但黑话自动匹配存在特殊缺口：`_load_jargon_explanation()` 优先读取 `event.message_str`。主动事件中的该字段是“主动开口候选 + 候选指引”，不是最近一条真实用户消息。近期群聊里刚出现、但没有被候选指引复述的黑话，主动发言无法精确命中。

表达风格读取近期会话文本并按当前群选择，所以主动发言的表达注入相对完整；最终仍受软背景预算裁剪影响。

## 风险发现

### P0：快速模式完全丢弃表达与黑话

`PromptRefiner` 当前配置：

```python
SOFT_BACKGROUND_FAST_MODE_BUDGET = 0
SOFT_BACKGROUND_NEAR_CONTEXT_BUDGET = 0
```

表达风格和黑话都位于 `soft_background`，预算为 `0` 时会整体跳过。常见受影响场景包括：

- 简单强 `@` 唤醒。
- `CORE_ONLY` 快速回复。
- think level 0 的短消息。
- 近距离上下文承接模式。
- 部分轻量互动事件。

这不是检索失败，而是检索成功后在最终提示词阶段被删除。

源码证据：

- `astrmai/conversation/planning/prompt_refiner.py:20`
- `astrmai/conversation/planning/prompt_refiner.py:22`
- `astrmai/conversation/planning/prompt_refiner.py:23`
- `astrmai/conversation/planning/prompt_refiner.py:178`
- `astrmai/conversation/attention/gate.py:1067`

### P1：普通模式仍可能优先裁掉学习内容

普通模式的软背景总预算只有 `420` 字符。当前裁剪顺序为：

```text
stable_jargon
→ stable_slang
→ stable_expression
→ stable_private_chat
→ stable_behavior_rules
→ stable_state
→ cold_summary
```

因此黑话最先被移除，表达风格第三个被移除。即使不是快速模式，软背景内容较多时仍可能出现“选择成功但模型不可见”。

### P1：主动能力不对称

- 黑话：有自动精确注入，也有 `omni_perception_query` 主动查询。
- 表达风格：只有自动注入，没有主动查询和二阶段恢复。

这会使表达学习对 PromptRefiner 预算高度敏感。一旦自动注入为空或被裁剪，模型没有其他方式获得相关信息。

### P1：主动事件的黑话匹配对象错误

主动事件使用合成候选文本做精确匹配，真实群聊焦点消息没有优先参与黑话路由。结果是 Bot 主动加入包含新黑话的话题时，可能不知道该词已经存在于全局词典。

### P2：注入 trace 存在假阳性

`PlanningInputLoader` 在选择到内容时就记录：

```text
decision.injected = True
```

但 PromptRefiner 可能随后因为快速模式、近距离模式或软背景预算将内容删除。当前 trace 没有形成完整生命周期：

```text
selected
→ rendered
→ trimmed
→ model_visible
```

因此不能只依赖 `expression_patterns.injected` 或 `jargon.injected` 判断模型是否真正看见学习内容。

### P2：黑话精确匹配仍有短词碰撞风险

自动路由使用规范化后的子串匹配。对高质量、人工批准的黑话通常可接受，但两字常用词可能嵌在普通长词中形成误命中。当前主要依靠人工审核和 `active` 状态控制风险，没有独立的词边界或歧义规则。

## 能力矩阵

| 场景 | 表达风格 | 全局黑话 |
|---|---|---|
| 普通非快速回复 | 自动按当前群选择；可能被预算裁剪 | 自动按当前消息精确匹配；可能被预算裁剪 |
| 快速/强唤醒回复 | 已选择内容最终不可见 | 已匹配内容最终不可见 |
| 近距离上下文承接 | 已选择内容最终不可见 | 已匹配内容最终不可见 |
| Bot 主动发言 | 通常能按当前群选择 | 可能因合成事件文本漏匹配 |
| 模型主动查询 | 不支持 | 通过 `omni_perception_query` 支持，但受 tier/家族过滤影响 |
| 待审或拒绝数据 | 不注入，符合预期 | 不注入，符合预期 |

## 验证结果

本次运行以下相关测试：

```text
python -m pytest \
  tests/test_planning_input_loader_refactor.py \
  tests/test_tool_disclosure_refactor.py \
  tests/test_planner_side_inputs_refactor.py -q
```

结果：`55 passed in 1.93s`。

测试证明 InputLoader、表达选择、黑话路由和工具披露组件按现有规则运行。现有快速模式测试还明确断言软背景应被全部跳过，因此本次发现属于跨模块策略冲突，而不是单个函数异常。

## 当前测试缺口

尚未覆盖以下端到端场景：

1. 快速模式选择到表达/黑话后，最终提示词是否可见。
2. Bot 主动事件能否使用近期真实群消息匹配黑话。
3. Bot 主动事件的表达风格是否实际进入最终 Dialog prompt。
4. `omni_perception_query` 被社交意图家族过滤后的二阶段恢复。
5. 表达风格主动查询能力。
6. trace 中 selected、rendered、trimmed、model_visible 的一致性。

## 后续修复边界

后续开发应保持现有学习存储和审核模型不变，优先修复注入末端：

1. 为快速模式提供独立的小额 `stable_expression` / `stable_jargon` 预算，不恢复整个软背景。
2. 将表达和黑话从普通软背景裁剪竞争中拆成可控的学习提示块，或至少调整保留优先级。
3. 主动事件的黑话匹配改用真实 focus/latest user text，合成候选文本只作为补充。
4. 增加当前群表达风格只读查询工具，或把该能力接入现有 `omni_perception_query`。
5. trace 同时记录选择结果和最终 PromptRefiner 渲染结果，消除假阳性。
6. 补齐快速回复、主动发言、普通回复和主动工具查询四条端到端回归测试。
