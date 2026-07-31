# OPT-18 单一 TurnTarget 与人物归属

状态：**代码完成，待线上事故集灰度** ｜ 优先级：P0 ｜ 依赖：OPT-17 ｜ 来源：MaiBot 目标绑定、Group Chat Plus actor/target 显式化、既有人物错绑事故

## 目标

- 每轮在 Focus 阶段只确定一次“机器人正在回应谁/什么”，下游不得按昵称、最近发言者或记忆内容重新猜测。
- 将当前发言者、回复对象、@对象、引用对象、话题参与者和 Bot 自身明确区分。
- 为群聊画像、关系、记忆和语气注入提供稳定 actor whitelist。
- 保留多人共同话题，不退化为“一人一个完全隔离 lane”。

## 基线证据

- `FocusThreadContext` 只有 focus/root event、sender、reason 和 thread signature，没有结构化目标。
- Planner 的 `_record_planner_dialogue_segment` 会从 focus 和 `DialogHistoryPolicy` 再次推断目标。
- 多人高活跃群聊中曾出现：A 发送“色色”，Bot 却回复另一人的 poke；用户 6 呼叫 Bot，Bot 仍称呼前一个高频用户。
- 当前 nickname 可进入 prompt，但 nickname 不是稳定身份，改名、同名和引用都可能造成误归属。

## 目标契约

建议定义不可变 `TurnTarget`：

```text
target_kind: actor | bot | message | topic | none
target_actor_id / target_actor_name
target_event_id
topic_epoch
source_event_ids
evidence: direct_at | reply | quote | interaction | focus | continuity | proactive | operator
confidence
resolved_by
created_at
```

同时定义当轮 `ActorSet`：

```text
current_actor_id
explicit_target_actor_ids
at_actor_ids
quoted_actor_ids
recent_topic_actor_ids
bot_id
```

规则：

- ID 决定身份，名称仅展示。
- `TurnTarget` 在 Focus 完成后冻结。
- 下游可降低 confidence 或拒绝执行，但不能静默改成另一个人。
- 跨会话工具必须显式创建新的目标上下文，不能复用当前群聊 target。

## 目标解析优先级

1. 明确回复/引用某条 Bot 或用户消息。
2. 明确 @Bot 并由当前 actor 发起请求。
3. poke/互动中的协议 user_id 与 target_id。
4. 当前批次中最后一个未被更强信号覆盖的 direct request。
5. 活跃 Bot 连续对话，且 actor 与上一轮已提交目标一致。
6. 参与者感知 topic continuity。
7. 无法确定时 `target_kind=none`，交 Judge/Planner 询问，不把最近高频用户当默认目标。

## 实施步骤

1. **事故回放先红**
   - 用户 6 在萤后 @Bot，目标必须是 6。
   - 小欣冒犯 Bot 后再次道歉，态度与历史必须仍绑定小欣。
   - A/B 同名、改名、引用第三人和 @多人。
   - peer poke 的 actor 与 target 分离。
2. **扩展 Focus 合同**
   - `FocusThreadContext` 增加 `turn_target` 与 `actor_set`。
   - 兼容旧字段 `focus_sender_id/name`，但由 `TurnTarget` 派生。
   - `TurnContext` 和 trace 只引用同一对象的序列化结果。
3. **单点解析器**
   - 在 attention/focus 层建立唯一 resolver。
   - 输入规范事件窗口、owned batch、上一轮 committed bot turn 和 topic snapshot。
   - 输出 target + evidence + confidence，不输出自然语言猜测。
4. **删除下游重推断**
   - Judge prompt 使用 target。
   - Planner、ContextEngine、工具上下文、Reply、历史提交和 Memory query 全部读取 target。
   - 对仍读取“最近 sender/name”的调用点加源码契约测试，逐步清零。
5. **actor whitelist 基础**
   - 白名单只含当前 actor、明确目标、@/quote actor 和同一 topic 的有限近期参与者。
   - whitelist 不代表关系等价，只代表本轮允许出现其数据。
6. **目标冲突**
   - 回复 A 的消息同时 @B：target_kind=message，A 是 primary，B 在 explicit target set。
   - 多人 @：不猜唯一关系对象，Planner 看到多目标结构。
   - 目标不在当前群成员列表时保留 ID，但标记 membership 未确认。
7. **显示与诊断**
   - 管理页 trace 显示 target ID/name、evidence、source IDs 和 actor set。
   - 用户可见回复不暴露内部 confidence。

## 测试矩阵

- 解析优先级参数化测试。
- 高频用户插队、无关图片插入、其他插件回执插入。
- 用户改名、同名、昵称含特殊字符。
- reply/quote/at/poke 组合。
- 群共享 topic + 不同用户各自关系。
- 私聊固定 current actor。
- 主动消息目标和跨会话工具目标。

## 观测字段

- `turn_target_kind`
- `turn_target_actor_id`
- `turn_target_event_id`
- `turn_target_evidence`
- `turn_target_confidence`
- `target_conflict_count`
- `actor_whitelist_size`
- `target_rederived_attempt`（目标应逐步降为 0）

## 验收标准

- 固定人物错绑回放集 100% 通过。
- target 从 Focus 到 Reply Commit 的 ID 与 evidence 不变。
- 无明确目标时不会自动绑定最近聊天最多的人。
- 模型 prompt 中每条历史消息可追溯 actor ID，Bot 与用户身份不混淆。
- direct @、reply、poke 的目标解析不依赖 LLM。

## 风险与回退

- 过度冻结可能影响模型在用户纠正目标时改口：纠正应生成新一轮 target，而不是修改旧轮。
- 多目标消息需要 Planner 支持；切换前保留旧 focus sender 兼容字段。
- actor whitelist 初期只观测不抑制记忆，避免误杀共享事实；抑制在 OPT-23 完成。

## 完成记录

- 已新增冻结 `TurnTarget`、`ActorSet` 及单点 `turn_target_resolver`，ID 为身份真源，名称只用于展示。
- `FocusThreadContext`、legacy bridge、`TurnContext.attention`、trace、Planner prompt、Planning input 与发送前人物一致性检查均读取同一目标对象。
- compatibility `focus_sender_id/name` 由已解析目标派生；当前 actor 另由 `ActorSet.current_actor_id` 保留，避免“目标对象”和“消息发送者”混成一人。
- 已删除 Planner 记录路径对 `DialogHistoryPolicy.current_sender_id` 的优先重猜；兼容回退仅在旧调用未携带 target 时生效。
- 事故回放 5 项覆盖：高频用户残留、reply+@ 冲突、回复 Bot、同名不同 ID、冻结目标；相关组合回归共 98 项通过。
- OPT-19 已完成：从 Focus 到 `CommittedBotTurn` 保持同一 target，Planner 草稿不再写入共享时间线。
- OPT-23 已完成：群聊人物记忆在 rerank/compress 前按 actor whitelist 执行隔离，并保留抑制证据。
- OPT-24 已完成本地事故回放与 trace 契约；尚待生产误绑定率、`target_rederived_attempt` 和多目标灰度统计。
