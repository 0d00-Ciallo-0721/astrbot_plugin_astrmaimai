# 审查报告：astrmai/conversation/planning/
> task_id: r03-conversation-planning | 审查时间: 2026-06-06 16:30 UTC

## 执行摘要

本模块是 AstrBot 的 System 2 规划器（Planner），负责对话规划的主循环、认知决策（CognitiveLoop）、上下文工程（ContextEngine）、提示词精炼（PromptRefiner）、对话连续性追踪（ConversationContinuity）和执行器编排。共计 19 个 source 文件，核心代码约 3500 行。

**总体评级：B-（需关注）**

架构清晰，职责分离合理，错误处理和退避策略较为完善。但存在 **1 个严重缺陷**（prompt 精炼器中的乱码注入）、**若干中等风险**（对话连续性记录丢失、工具/冷却标签交叉管理薄弱）和多个持续重构期的遗留问题。

---

## 概述
- 审查文件数: 19
- 发现总数: 14
- 严重: 1 | 中等: 6 | 建议: 7

---

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | **prompt_refiner.py:105-113** | **`_render_runtime_guidance_cluster` 包含乱码（mojibake）中文**。三个 section 标题均出现编码损坏：`---???????---`（应为「内在驱动」）、`---褰撳墠鐘舵€佷笌绾︽潫---`（应为「当前状态与约束」）、`---鏈疆涓婁笅鏂囪В閲?--`（应为「本轮上下文解析」）。该方法在 `refine_prompt` L939 被调用，乱码文本会被注入到发送给 LLM 的 prompt 中，可能引起模型困惑或暴露编码问题。 |

---

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 2 | **conversation_continuity.py:174-178** | **wait/ignore/lightweight 轮次不会被记录到 `state.turns` 中**。`record()` 方法在轻量事件或 wait/ignore 回复需求时直接 `return item`，跳过了 `state.turns.append` 逻辑。文档注释声明「仅记录轮次，不更新状态」，但实际实现未记录轮次，导致 `summary()` 和 `recent()` 遗漏这些轮次，下游 agency feedback 和 cooling 机制可能失效。 |
| 3 | **planner.py:890-1319** | **`plan_and_execute` 方法长达 ~430 行**，单个方法承载了规划器主循环的所有逻辑：think level 决策、cognitive loop、prompt 构建、工具过滤、执行、后续发言判断等。极高的圈复杂度不利于单元测试和故障定位，任何一步的异常都可能导致整条链路静默降级。 |
| 4 | **cognitive_loop.py:58** | **`COMPLEXITY_HINTS` 包含单字符 `"查"`**。`gate_decision()` 中 `any(token in current_text ... for token in self.COMPLEXITY_HINTS)` 使用的是 Python `in` 子串匹配，任何包含汉字「查」的消息（如「查一下」「别查了」）都会触发 complexity 判定。过于宽泛，建议改用完整词汇或 word-boundary 匹配。 |
| 5 | **cognitive_loop.py:345-349** | **`gate_decision` 的 `?` 问号判定存在逻辑冗余**。`"?" in current_text` 检查的是原始文本（含空白），而 `meaningful = compact.replace("?", "")` 处理的是去空白后的文本。若消息为 `"?"` 或 `"？ "`，`compact` 为 `"？"`，`meaningful` 为空，`len(meaningful)` 为 0，不会触发。若消息为 `"a ? b"`，`compact` 为 `"a?b"`，`meaningful` 为 `"ab"`，`len=2` < 3 也不会触发。逻辑上正确但容易产生困惑，建议统一使用 `compact` 进行问号检测。 |
| 6 | **conversation_continuity.py:68-93** | **`_topic_similarity` 对短文本使用字符级 Jaccard 相似度**。当两个话题字符串都很短时（如「你好」vs「你好呀」），字符集 Jaccard 可能给出虚高的相似度分数（~0.8），导致不同话题被误合并。特别是在 `WEAK_TOPIC_SIMILARITY_THRESHOLD=0.45` 时，误判风险更高。 |
| 7 | **context_engine.py:238-243** | **`_resolve_visual_memory_refs` 在每个 picid 的循环内独立打开 DB session**。`with self.db.get_session() as session` 出现在 for 循环内部，对有多个图片消息的场景（如群聊发图序列），会产生多次数据库连接/关闭开销。建议在循环外统一获取 session。 |

---

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 8 | **cognitive_loop.py:36** | **`SOFT_TIMEOUT_SECONDS = 2.5` 为硬编码魔法数值**。在多模型（如本地模型 vs API 模型）场景下，2.5 秒可能过于紧张或过于宽松。建议迁入配置或允许构造函数参数覆盖。 |
| 9 | **cognitive_loop.py:375-445** | **`_run_readonly_tool` 中 `"is offline"` 错误消息重复出现 5 次**（self_lore、user_profile、relationship、current_state、light_memory）。建议提取为统一错误模板或让各 service 返回标准错误码。 |
| 10 | **conversation_continuity.py:21** | **`MAX_TURNS_PER_CHAT = 12`** 在高频群聊场景中，12 条轮次可能在 2-3 分钟内填满，导致旧轮次被过早驱逐。建议根据对话密度动态调整或允许外部配置。 |
| 11 | **context_engine.py:314-318** | **`_pick_reply_style` 使用每小时变化的 MD5 哈希做伪随机选择**。`hashlib.md5(f"{chat_id}:{int(time.time() // 3600)}")` 的哈希值在整个小时窗口内对同一 chat_id 恒定，风格选择在小时边界突变。建议改用确定性轮转或基于对话状态的动态选择。 |
| 12 | **behavior_tuning.py:101-126** | **`_downgrade_pushback` 同时设置 `action_tier="none"` 但未清除 `action` 字段**。`apply()` 后续的 `_allow_pushback` 检查后，如果 pushback 被降级为 boundary，`action` 仍可能保持为 `"reply"`（CognitiveDecision 默认值），而 `action_tier="none"`。`reply`+`none` 的语义组合在下游可能引发歧义。建议联动修改 `action` 为 `"wait"` 或确认 `reply_need` 覆盖逻辑。 |
| 13 | **planner.py:634-648** | **`_resolve_cognitive_retrieve_keys` 在 cognitive decision 有 `retrieve_keys` 时直接返回，忽略 `current_keys`**。如果 cognitive_loop 返回空的 retrieve_keys 但 memory_policy 非 "none"，则回退到 `normalized_current`。这里缺少对 `current_keys` 中已有 `CORE_ONLY` 等特殊标记的保护，可能导致不必要的 RAG 记忆检索。 |
| 14 | **prompt_refiner.py:39-53** | **`SOFT_BACKGROUND_PRIORITY_ORDER` 和 `SOFT_BACKGROUND_TRIM_ORDER` 使用元组硬编码**。背景块优先级顺序和修剪顺序耦合在类常量中，新增背景块类型时需要同时更新两个常量且保持顺序对称。建议改用带有优先级数值的映射表。 |

---

## 亮点

1. **JSON 解析容错优秀**：`cognitive_loop.py` 的 `_safe_parse_json` + `_extract_braced_json` 实现了一个健壮的嵌套大括号解析器，能正确处理字符串字面量中的花括号和转义，远超简单的正则 `\{.*\}` 方案。这对此前已知的 JSON 解析回归问题提供了有力修复。

2. **Gate 去重与状态写入**：`gate_decision` 的分支覆盖完整（think_level=0/lightweight/fast_mode/core_only/judge_action/空消息/冷却/复杂度/平凡轮次），每个分支都有明确的 skip_reason 和 signal，且通过 `_write_gate_state` 同步到 `event` 和 `turn_context` 两个通道，便于后续链路追踪。

3. **对话连续性状态机设计清晰**：`ConversationContinuityStore.record()` 中的 topic/goal status 转换（new/continuing/redirected/guarded/observing）逻辑完整，考虑了软衰减、weak continuity、previous_closed 等边界情况，对 `WEAK_TOPIC_SIMILARITY_THRESHOLD` 的动态调整体现了细致的设计考量。

4. **Prefix Caching 架构完善**：ContextEngine 的 prompt 缓存分层清晰（frozen_prefix 含 persona/style/system_rules；semi_stable 含冷摘要/状态/行为规则等），MD5 hash 比对 + change reason 追踪为 provider 侧 prefix caching 提供了完整的可行性判断。

5. **ThinkLevelPolicy 多信号融合**：`think_level_policy.py` 综合考虑了 heartflow 状态、对话连续性、工具意图关键词、消息长度、群聊 vs 私聊、直接 vs 间接触发等多维信号，决策逻辑有层次感，非 trivial 的启发式框架。

---

## 已知修复回归检查

| 已知问题 | 检查结果 | 备注 |
|---------|---------|------|
| **JSON 解析** | ✅ 已修复 | `_extract_braced_json` 正确处理嵌套花括号和字符串字面量 |
| **Gate 去重** | ✅ 无回归 | `gate_decision` 分支互斥，无重复判断路径 |
| **Prompt 缓存** | ✅ 功能完整 | MD5 hash + meta 追踪，首次/变更/稳定三种状态区分清晰 |
| **COMPLEXITY_HINTS 问号处理** | ⚠️ 逻辑正确但令人困惑 | 原始文本和去空白文本混用，建议统一使用 `compact` |

---

## 测试覆盖评估

| 模块 | 覆盖评估 | 建议 |
|------|---------|------|
| `CognitiveLoop.gate_decision` | 🟡 边界覆盖不全 | 缺少对 cooldown + complexity 交织场景（如冷却中的长复杂消息）的测试；COMPLEXITY_HINTS 中 `"查"` 的假阳性未覆盖 |
| `CognitiveLoop._safe_parse_json` | 🟢 覆盖较好 | `_extract_braced_json` 的字符串内花括号处理有防御性逻辑 |
| `ConversationContinuityStore.record` | 🔴 关键路径无测试 | wait/ignore/lightweight 不记录轮次的行为无单元测试验证，与注释矛盾 |
| `ContextEngine.build_prompt` | 🟡 集成覆盖不足 | frozen_prefix hash 变化时的 prefix_stable 切换未覆盖；`_compress_cold_summary` 的边界（空输入/超短/超长）未见测试 |
| `PromptRefiner.refine_prompt` | 🔴 存在明显 bug | 乱码字符串表明该路径在 CI 中未被执行或未使用非 ASCII locale 测试 |
| `BehaviorTuningPolicy` | 🟡 部分覆盖 | pushback downgrade 分支有测试覆盖，但 group_ambient_short_wait + uncertain_observe 组合场景未覆盖 |
| `ThinkLevelPolicy.decide` | 🟡 分支众多但未穷举 | heartflow 信号 + group_non_direct 的组合决策树缺少正交测试 |

---

## 总结

`astrmai/conversation/planning/` 模块整体架构扎实，职责划分清晰，错误处理和降级策略（timeout/catch-all/degraded log）较为完备。`CognitiveLoop`、`ContextEngine`、`ConversationContinuityStore` 三个核心组件均有超出平均水平的设计质量。

**最大的风险点**是 `PromptRefiner._render_runtime_guidance_cluster` 中的乱码注入——这会直接毒化发送给 LLM 的 prompt 质量。此外，`ConversationContinuityStore.record()` 关于 lightweight/wait/ignore 轮次的记录策略与注释矛盾，可能导致下游 cooling 和 agency feedback 信号丢失。

修复这些问题的优先级：
1. **P0**：修复 prompt_refiner.py 中的乱码（重写为正确的 UTF-8 中文）
2. **P1**：确认 conversation_continuity.py 中 lightweight 轮次是否需要实际记录到 turns 中（按注释意图修复或更新注释）
3. **P2**：拆分 planner.py 的 `plan_and_execute` 方法；收紧 `COMPLEXITY_HINTS` 中 `"查"` 的匹配范围
4. **P3**：提取魔法数值为配置项；统一工具错误消息；增加多信号组合场景的单元测试

**总体评级：B-（需关注）**
