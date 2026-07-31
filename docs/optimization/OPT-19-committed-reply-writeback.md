# OPT-19 发送后提交与 Bot 输出真源

状态：**本地完成（含持久化 repair outbox）** ｜ 优先级：P0 ｜ 依赖：OPT-17、OPT-18 ｜ 来源：Group Chat Plus 发送后提交、MaiBot 唯一 Bot turn、AstrMai 当前草稿污染

## 目标

- 将“模型计划说什么”和“用户实际看见什么”彻底分离。
- 只有实际发送成功的内容才能写入群聊历史、原生 history、记忆、学习、连续性和 trace。
- 支持多段、部分发送、TTS、图片/表情、空文本、发送失败与重试的幂等提交。
- 建立唯一 `CommittedBotTurn`，成为后续上下文中的上一轮 Bot 真源。

## 基线证据

- `planner.py` 曾在 Executor 返回后使用完整模型草稿写 assistant segment；部分发送时 Executor 返回值仍可能与实际成功分段不一致，因此共享历史会看见未送达内容。
- `reply_artifact_builder.py` 在发送后才知道实际发出几段、哪些 outbound IDs，以及是否 partial send。
- `reply_service.py` 已使用 `artifact.persistable_text` 更新原生 history 和记忆，说明发送后提交已有一半基础，但群聊 store 仍提前写草稿。
- 当前可能出现：计划三段只发一段，但共享历史看见三段；发送失败但后续模型认为 Bot 已说过；TTS/附件实际输出与纯文本草稿不一致。

## 目标契约

`ReplyPlan`（可变、短命）：

```text
plan_id / turn_id
target
planned_text / planned_segments
planned_attachments
shape_policy
created_at
```

`CommittedBotTurn`（不可变、事实）：

```text
commit_id / turn_id / plan_id
chat_id / chat_kind
target
source_event_ids / topic_epoch
visible_text / persistable_text
sent_segments
sent_attachment_refs
outbound_message_ids
sent_at
partial_send
send_status
failure_reason
reply_hash
provenance
```

约束：

- `send_status=sent|partial` 才可进入可见历史。
- `failed|cancelled|stale` 只写诊断，不写对话历史和学习。
- 同一 `turn_id + plan_id` 重复 commit 幂等。
- `visible_text` 与实际发送保持一致；内部提示、工具 JSON、错误栈不得持久化为用户可见回复。

## 实施步骤

1. **Red 测试**
   - 全发送、部分发送、第一段失败、中途取消、重复回调、TTS only、文本+图片、空回复。
   - 断言 group store、native history、memory 和 learning 的可见文本完全一致。
2. **扩展 Reply Artifact**
   - 保留 `VisibleReplyArtifact` 作为发送计划/临时状态，或重命名为更清晰的 plan 类型。
   - 从 `_send_segments` 返回结构化 commit receipt，而不是仅修改 event extras。
   - 收集每段 outbound ID、发送时间和错误。
3. **移除发送前历史写入**
   - 删除/停用 Planner 中 `_record_planner_dialogue_segment` 的直接 append。
   - Planner 只记录 `reply_plan_created` trace。
   - 防回归测试扫描 Planner 不得调用 group store append assistant。
4. **统一 commit service**
   - 在发送成功后一次性协调：
     - `group_dialogue_store`
     - AstrBot native history
     - Memory ingestion
     - Learning/Evolution
     - Conversation continuity
     - Turn trace
   - 各消费者接收同一个 `CommittedBotTurn`，不得各自重新拼文本或目标。
5. **幂等与 repair**
   - 提交以 commit ID 去重。
   - 下游某个持久化失败时记录 outbox/repair 状态，不能再次向用户发送。
   - repair 重放只补数据，不触发 Reply。
6. **部分发送语义**
   - `sent_segments` 只包含成功段。
   - `persistable_text` 由成功段构建。
   - failed segment 仅进入诊断。
7. **非文本输出**
   - TTS/Record：保存可读转写或明确 `[语音]` 占位与 asset reference。
   - 表情包/图片：保存 `[表情包]`/`[图片]` 与 asset ID。
   - 其他插件接管发送时，只有拿到官方发送结果才 commit；否则标记 external/unknown，不猜测成功。
8. **上一轮 Bot 读取**
   - Continuity、active continuation 和 ContextEngine 只读最后一个 committed turn。
   - 不再从 planner draft、event extra 和 native history 多处竞争。

## 测试矩阵

- 单段、多段、双换行强制分段。
- QQ 平台单段失败与中途失败。
- 相同 commit 回调两次。
- 发送后 memory 写失败再 repair。
- 新消息在发送途中到达导致 stale/cancel。
- TTS 成功、TTS 失败静默、meme 附件。
- 容器重启后 outbox 恢复。

## 观测字段

- `reply_plan_id`
- `reply_commit_id`
- `reply_commit_status`
- `planned_segment_count`
- `sent_segment_count`
- `outbound_message_ids`
- `partial_send`
- `commit_consumer_status`
- `commit_repair_scheduled`
- `draft_history_write_attempt`（必须为 0）

## 验收标准

- 所有历史和记忆消费者看到的 Bot 文本与实际成功发送文本一致。
- 模拟发送失败后，下一轮上下文不含失败草稿。
- 部分发送时只提交成功分段。
- 同一 commit 重放不会重复 MessageLog、记忆或学习记录。
- 最后一轮 Bot 连续性只来自 `CommittedBotTurn`。

## 风险与回退

- 这是高风险写路径变更，应分两阶段：先双记 trace 对比，再切断 Planner 写入。
- commit service 失败不得让已经成功发送的消息丢失事实；必须进入 repair outbox。
- 回退时可恢复旧 history 消费，但禁止恢复“发送前把草稿当已发送”的行为。

## 完成记录

- 新增不可变 `ReplyPlan`、`ReplySendReceipt`、`CommittedBotTurn`，`turn_id + plan_id` 生成稳定 commit ID。
- `ReplyService` 在发送后依据真实 `sent_segment_count` 构造 receipt；失败、过期、取消只写诊断，不产生可见 commit。
- 新增 `ReplyCommitService`，统一向 group store、native history、memory、learning 提交同一份事实；成功消费者不重复执行，失败消费者仅在重放时补数据，不会再次发送。
- `GroupDialogueStore.append_committed_bot_turn()` 使用 commit ID 幂等写入，上一轮 Bot turn、来源事件、目标和立场均来自同一事实对象。
- Planner 已移除所有直接 assistant history append；社交候选和压缩调度只消费 `astrmai_committed_bot_turn`。
- Executor 返回真实已提交文本，部分发送不再把未送达草稿传给后续连续性、agency 和表达链。
- 已记录 `reply_plan_id`、`reply_commit_id/status`、计划/实发段数、outbound IDs、consumer status、repair 标记及 `draft_history_write_attempt=0`。
- 回归覆盖：全发送、首段失败、中途失败、部分提交、重复 commit、消费者失败后补偿、Planner 无草稿写入、群聊因果连续性。
- 新增 SQLite `reply_commit_outbox`：v72 建表、v73 建到期重试索引；保存 committed reply、纯数据 repair context 和每个消费者状态。
- 插件生命周期启动 repair worker；重启后只重放 `pending/failed` 的 group history、native history、memory、learning 消费者，`committed/skipped` 消费者不会重复执行，repair 链路没有发送能力。
- 回归覆盖持久化 round-trip、跨重启恢复、不重放成功消费者、迁移幂等和旧库升级。
- 相关验证：`90 passed`；持久化 outbox 专项与相邻链路 `63 passed`；最终全量结果见 OPT-24。
- 尚待生产 planned/committed 差异报表、真实容器重启恢复和 outbox 积压告警验收。
