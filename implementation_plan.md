**结论先说**

当前 AstrMai 已经从“剧本续写机器人”进化到“有一定内在状态和动作选择的角色代理”，但它的主观能动性仍然偏弱。它现在更像：

> 一个会根据上下文、状态和工具条件做出局部反应的角色执行器。

还没有完全成为：

> 一个持续拥有偏好、动机、注意力、犹豫、主动性和自我连续性的“人”。

工程上不要追求“真实意识”，但可以追求**稳定的主观行为模型**：让它像一个有自己感受、目标、习惯和社交判断的人。

**当前架构认知**

现在主链路大致是：

```text
AttentionGate
  -> Judge 轻量闸门 WAIT/IGNORE/PASS
  -> Planner
      -> CognitiveLoop 可选隐藏决策
      -> ContextEngine 构造身份/状态/内在驱动
      -> PromptRefiner 构造眼前上下文
      -> 工具分层 chat/full/sys3
  -> Executor
      -> text mode 或 tool loop
```

这套架构已经比最早的“思考 JSON -> 回复文本”好很多：

- `ContextEngine` 有第一人称状态、内在驱动、输出契约。
- `PromptRefiner` 把当前消息、历史、记忆拆开，减少剧本感。
- `CognitiveLoop` 能做隐藏决策：`reply / wait / ignore / tool_call`。
- PFC 工具让角色可以“行动”：发表情、互动、点赞、戳人、查询记忆、转话题、撤回等。
- `ActionModifier` 用能量、关系、心情裁剪工具，已经有一点“状态影响行为”的味道。

但它的自由度目前主要是**局部自由**，不是**人格连续自由**。

**当前主观能动性评分**

我会这样评估：

| 维度 | 当前水平 | 说明 |
|---|---:|---|
| 注意力选择 | 中等偏高 | AttentionGate 已能合并窗口、选 focus、过滤弱事件 |
| 回复自由度 | 中等 | 能选择语气、记忆、工具，但多数时候仍围绕“回答当前消息” |
| 行动自由度 | 中等 | chat/full 工具分层后更像人，但仍依赖工具 loop 临场选择 |
| 内在动机 | 偏弱 | 有 `sys1_thought / goals_context`，但不是持续驱动力 |
| 情绪影响行为 | 中等 | mood/energy 影响 prompt 和工具裁剪，但还不影响“想做什么”的生成 |
| 自我连续性 | 偏弱 | 内心判断没有系统性写回长期自我状态 |
| 主动性 | 偏弱 | follow-up 偏随机，缺少“我想主动做某事”的稳定机制 |

一句话：

> 它现在有“状态”和“动作”，但还缺“欲望”和“自我连续”。

**最大结构问题**

1. **CognitiveLoop 没覆盖最常见主路径**

当前 `CognitiveLoop.should_run()` 会跳过 `CORE_ONLY / ALL`。但正常群聊经过 Gate 后，很多消息会带 `["ALL"]`。这意味着：  
最常见路径反而不经过真正的隐藏主脑决策。

结果是：

- Judge 只做 WAIT/IGNORE/PASS。
- Planner 继续构造 prompt。
- 最终工具 loop 再决定是否行动。

这让“主观能动性”发生得偏晚，像是执行阶段临场发挥，而不是角色先产生意图。

2. **思考不是发散，而是结构裁决**

`CognitiveLoop` 当前输出严格 JSON，这很稳，但人的思考不是直接生成表格。  
它更像：

```text
感到什么 -> 想靠近/回避/调侃/确认 -> 选择说话或行动
```

现在的结构更像：

```text
action = reply
memory_policy = light
style_policy = xxx
```

它能控制流程，但不太像“一个人在心里动了一下”。

3. **状态是描述，不是驱动**

`ContextEngine._build_state_block()` 会说“我现在心情偏低，精力多少”。  
但这些状态主要进入 prompt，只有工具裁剪少量使用。

也就是说，状态更多是**告诉模型怎么演**，而不是**真的改变决策系统**。

更人一点的系统应该让状态影响：

- 是否想说话
- 说多长
- 是否开玩笑
- 是否主动互动
- 是否查询记忆
- 是否回避某个话题
- 是否想靠近某个人

4. **工具行为缺少“意图前置”**

现在工具可用后，最终由 tool loop 决定调用什么。  
这给了模型自由，但自由有点散。

更好的是：主脑先决定本轮“动作姿态”：

```text
这轮我只是轻轻接话
这轮我想安慰
这轮我想调皮一下
这轮我需要查记忆
这轮我最好等一等
```

然后工具层只执行这个姿态允许的动作。

5. **没有行动后的反思闭环**

人会在说完后形成一点残留：

- 刚才那样说好像太冷了
- 对方喜欢这种玩笑
- 我刚刚已经发过表情了，别再发
- 这个人今天心情不好，我下次轻一点

当前系统有 evolution/expression，但缺少一个明确的“本轮自我反思记录”。

这会导致人格表现靠 prompt 和记忆，而不是靠持续的自我修正。

**我建议的框架更新**

我建议把“思考 → 动作”升级为：

```text
感知 Sense
  -> 评估 Appraise
  -> 意图 Intend
  -> 表达/行动 Act
  -> 反思 Reflect
```

不是要大重写，而是逐步补层。

**第一阶段：把 CognitiveLoop 改成真正主脑**

建议让 CognitiveLoop 覆盖更多普通主路径，不再简单跳过 `ALL`。

它不需要每次都重 LLM：

- lightweight / poke / fast 仍跳过。
- 普通短消息可走轻量规则。
- 中等复杂度和有社交动作空间的消息走 CognitiveLoop。

CognitiveDecision 可以扩展为：

```text
reply_need: reply | wait | ignore
social_intent: comfort | tease | answer | observe | join | disengage
action_tier: none | chat | full | sys3
memory_policy: none | light | deep
style_policy: short | playful | soft | serious
risk_flags: []
inner_state_summary: 一句内部状态摘要
```

重点不是字段多，而是让“我想干什么”先于“我要怎么说”。

**第二阶段：新增 Appraisal 层**

在人类行为里，主观能动性来自评价：

- 这句话和我有关吗？
- 对方是在求助、玩闹、挑衅、撒娇、沉默？
- 我想靠近还是保持距离？
- 现在适合调皮还是认真？
- 这个动作会不会打扰别人？

建议在 Planner 前加一个轻量 `Appraisal`：

```text
valence: 正/负/中性
arousal: 强/弱
social_distance: 亲近/普通/疏离
risk: 低/中/高
user_need: 求助/陪伴/玩笑/信息/无明确需求
bot_impulse: 回应/等待/互动/查询/转移
```

这可以先用规则 + 少量 LLM，不必一步到位。

**第三阶段：把 State 从“展示状态”变成“驱动状态”**

当前 mood/energy 像背景板。建议变成决策输入：

- energy 低：更短、更少 full 工具、更少主动 follow-up。
- mood 低：少发表情，更多冷淡/简短，但不失控。
- curiosity 高：更可能追问或查记忆。
- affection 高：更可能点赞、轻互动。
- caution 高：少 at、少 poke、少转私聊。

也就是把状态映射到行为倾向，而不是只写进 prompt。

**第四阶段：建立行动后反思**

每轮结束后生成一个很短的内部记录，不给用户看：

```text
本轮我选择了：轻松接话 + 表情
原因：对方语气开心，关系安全
下次注意：刚发过表情，短时间不要重复
```

这可以写入短期运行态，不一定马上进长期记忆。  
它会显著增强“我是同一个人”的感觉。

**第五阶段：工具调用改成“意图约束工具”**

现在是“给工具，模型自己选”。  
建议变成：

```text
CognitiveLoop 决定 action_tier 和 allowed_action_family
Planner 暴露对应工具
Executor 只允许这些工具
```

例如：

- `social_intent=tease` -> meme/reaction/poke
- `social_intent=comfort` -> no poke, maybe like/reaction
- `social_intent=answer` -> no meme unless气氛合适
- `social_intent=withdraw` -> only full + withdraw

这会让工具行为更像“人有意图地行动”，而不是模型看到工具就尝试。

**最值得优先做的三件事**

1. **让普通主路径也经过轻量 CognitiveLoop**

这是主观能动性的核心。  
否则最常见的聊天仍然是 prompt 直达 Executor。

2. **新增 social_intent / action_tier**

让 bot 先决定“这一轮我要以什么姿态存在”。  
这比单纯 `reply/wait/tool_call` 更像人。

3. **增加 post-action reflection**

不需要复杂，先做短期缓存即可。  
这会让角色变得有连续性，而不是每轮重新扮演一次。

**最终判断**

当前架构已经解决了“剧本化”和“工具不可达”的一大截问题。  
但如果目标是“像一个有主观思想的人”，下一步不能只继续改 prompt。

真正需要补的是：

```text
状态 -> 动机 -> 意图 -> 行动 -> 反思
```

现在系统有状态、有行动、有部分思考，但“动机”和“反思”还不够。  
一旦补上这两层，角色会明显从“会聊天的 Bot”变成“有自己倾向和节奏的人”。