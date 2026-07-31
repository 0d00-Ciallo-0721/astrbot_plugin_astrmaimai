# OPT-23 群共享连续性与人物记忆白名单

状态：**已完成（待真实群聊灰度）** ｜ 优先级：P0 ｜ 依赖：OPT-17、OPT-18、OPT-19、OPT-22 ｜ 来源：MaiBot 参与者摘要、四份报告共同结论、群聊 OOC/人物错绑事故

## 目标

- 保留群共享聊天历史，让共同剧情、公共事实和多人互动可以连续。
- 用户画像、关系、称呼、情绪态度和长期记忆按稳定 actor ID 隔离。
- 中期摘要必须记录参与者和未决问题，不能只保留无主语的故事文本。
- 话题有效期内自然承接；超过有效期先确认，不把旧剧情硬套到新消息。
- 解决“知道群里发生了什么”和“知道现在是谁在说话”不能同时成立的问题。

## 基线证据

- 当前群聊能够保留公共故事，但发生过将萤的上下文套给 6、把小欣的冒犯归因给飞飞宝、几天前布丁称号链持续影响新话题等问题。
- 完全“一人一个 lane”会破坏共同聊天，用户已明确不接受。
- 当前 memory/relationship 注入未建立严格 actor whitelist；摘要可能丢失发言者和目标。
- `DialogueSegment` 已有 topic epoch、source IDs 和 target 字段，可作为连续性基础。

## 连续性模型

### 三层状态

1. **共享事件层**
   - 群内公开可见事件，所有参与者共享。
2. **话题层**
   - `topic_epoch`、参与者、关键事实、未决问题、最近 committed Bot turn。
3. **人物层**
   - 每个 actor 的画像、关系、偏好、情绪、边界和长期记忆。

共享事件可以被所有人看见，但人物层数据只有进入当轮 actor whitelist 后才能注入。

### Actor Whitelist

当轮允许人物数据的 ID：

- current actor
- primary TurnTarget
- 明确 @/quote/reply 的 actor
- 当前 topic snapshot 中最近、有限、且有 source evidence 的参与者

默认不允许：

- 仅因历史发言频率高而加入。
- 仅因昵称相似而加入。
- 仅因向量记忆语义相似而加入。
- actorless 的恋爱/亲属/敌意关系候选进入群聊 prompt。

## 中期摘要结构

```text
topic_epoch
topic_label
started_at / last_activity_at
participant_ids + display_names
shared_facts
per_actor_stance
unresolved_questions
last_committed_bot_turn_id
last_bot_target_actor_id
source_event_ids
confidence
expires_at
```

摘要是派生资料，不覆盖原始 canonical timeline。

## 实施步骤

1. **事故夹具**
   - 空酱前后身份失忆。
   - 小欣冒犯 → 追问 → 道歉，态度连续且对象不变。
   - 萤/6 切换。
   - 布丁称号是群共享游戏，但专属称呼不自动扩散给其他人。
   - 多人共同包饺子，后加入者能看懂公共剧情。
2. **统一 continuity 输入**
   - 只读 canonical shared timeline 和 committed bot turn。
   - 不读 Planner 草稿和重复 native history。
3. **建立 topic snapshot**
   - topic epoch 由明确切换、长期静默或冲突语义产生。
   - 默认有效期 10～20 分钟可配置；超过 30 分钟的旧话题必须进入确认路径。
   - 时间阈值不是唯一条件，明确“换个话题”立即切换。
4. **参与者感知摘要**
   - 摘要生成 prompt 强制输出 actor IDs、事实归属、未决问题和 source IDs。
   - 缺 actor 的关系陈述标记 invalid，不持久化。
   - 摘要更新采用 merge，不丢失尚未解决的问题。
5. **记忆候选过滤**
   - retrieval candidate 带 subject/actor IDs、scope、source chat/topic。
   - actor whitelist 在 rerank/compress 前应用，避免错误候选浪费模型预算。
   - 群公共事实允许 `scope=group_shared`，但不得包含专属关系。
6. **关系与称呼边界**
   - Persona 中“用户/哥哥/恋人”先解析为角色关系锚点，不自动映射当前任何群友。
   - 只有明确绑定的 actor ID 可获得专属称呼；其余按姓名/中性社交称呼。
   - 关系纠正写入对应 actor，不写群全局。
7. **态度连续性**
   - 冒犯、道歉、拒绝等短期 stance 绑定 actor + topic epoch。
   - 其他用户复读 Bot 文本不改变 stance 对象。
   - topic 到期后 stance 降权，不永久惩罚。
8. **read-your-write**
   - 刚提交的 per-actor fact 即使索引尚未投影，也可从 canonical SQL 短期读取。
   - 与现有 memory projection repair 协同，不新建重复记忆库。
9. **管理与观测**
   - trace 显示 whitelist、被抑制候选、topic snapshot 和 last committed target。
   - WebUI 可只读查看摘要证据；人工编辑另立需求，避免本工作流扩大。

## 测试矩阵

- 3～10 人高活跃群聊，消息交错。
- 同一公共话题中不同用户各自关系。
- 用户改名/同名。
- topic 5 分钟、20 分钟、31 分钟后承接。
- 摘要生成失败时回退原始近期事件。
- 记忆候选 subject 缺失、actor 错误、group_shared。
- 向量召回错误人物但 whitelist 正确抑制。
- Bot 上一轮 partial send。

## 观测字段

- `topic_epoch`
- `topic_age_ms`
- `topic_confirmation_required`
- `topic_participant_ids`
- `actor_whitelist`
- `memory_candidates_before_actor_filter`
- `memory_candidates_suppressed`
- `summary_source_event_ids`
- `last_committed_target_actor_id`
- `stance_actor_id`

## 验收标准

- 群共享剧情仍可被不同参与者承接。
- 专属称呼、恋爱关系、负面态度和画像不跨 actor ID 扩散。
- 固定事故回放全部通过。
- actorless 关系候选在群聊注入率为 0。
- 超过 30 分钟的模糊旧话题先确认，不直接延续。
- 摘要失败时不阻断回复，且不会伪造参与者。

## 风险与回退

- whitelist 过窄会削弱多人共同话题；先 shadow 记录 suppression，再启用硬过滤。
- 摘要不能取代原始事件，始终保留近期 canonical timeline。
- 人格专属关系迁移要兼容已有人工角色切片，不能修改用户原始人格文件。

## 完成记录

- 新增 `ActorMemoryScope`，只接受当前 actor、结构化目标、明确 @/quote/reply、有限话题参与者和已消歧实体，不接受昵称相似或发言频率推断。
- 记忆候选在 deep rerank/compress 前完成 actor 白名单过滤；actorless 人物画像、关系和情绪记忆在群聊中被隔离，公共群事实仍可进入。
- `MemoryInjectionDecision`、memory funnel 和 turn trace 记录白名单、抑制 ID、抑制数量与过滤前候选数；候选全部被隔离时仍保留证据。
- `GroupContextSnapshot` 已携带 topic epoch、参与者、原始事件 ID、未解析 actor 和上一轮已提交目标。
- 固定回归覆盖人物错绑、actorless 专属关系、公共群事实、deep 链过滤顺序、稳定证据来源与零结果观测。
- 自动化验证：`tests/regression/memory/test_group_actor_memory_whitelist.py` 与 `tests/regression/conversation/test_group_causal_continuity.py` 共 14 条通过。
- 待部署后补充：真实群聊候选抑制率、摘要质量抽检和 OOC 灰度数据。
