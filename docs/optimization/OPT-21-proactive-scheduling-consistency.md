# OPT-21 主动调度一致性（双水位、持久 due、generation 取消）

状态：**已完成（代码与本地回归）** ｜ 优先级：P1 ｜ 依赖：OPT-17、OPT-18、OPT-19 ｜ 来源：Proactive Chat 调度模型、AstrMai 既有 runtime generation

## 目标

- 区分“最后一条真实用户活动”和“最后一次 Bot 可见回复”，避免 Bot 用自己的发言延长或缩短沉默时间。
- 将下次主动触发时间持久化，容器重启后不密集补发或永久丢失。
- 任意新用户消息到达时，取消尚未发送的旧主动任务。
- 主动候选继续走正常 Attention → Planner → Executor → Reply Commit，不建立独立人格回复链。
- 正确区分私聊和群聊，限制连续未获回应的主动消息次数。

## 基线证据

- `wakeup_service.py` 以 `state.last_reply_time` 计算沉默，不能判断该时间来自用户还是 Bot。
- `run_once()` 扫描活跃状态，没有持久化 due 索引，重启后的调度语义依赖内存。
- `build_wakeup_intent` 元数据使用 `group_id=chat_id`，私聊可能被描述成群聊。
- `chat_runtime_coordinator.py` 已有 generation、active task 和 send claim，可复用为取消屏障。
- 当前主动链已能经 dispatcher 进入正常回复流程，这一点必须保留。

## 状态契约

每个会话持久化：

```text
chat_id / chat_kind
last_real_user_activity_at
last_committed_bot_reply_at
next_proactive_due_at
proactive_generation
unanswered_proactive_count
last_proactive_commit_id
last_proactive_cancel_reason
updated_at
```

定义：

- `last_real_user_activity_at`：非 Bot、非回声、非外部伪消息的真实用户事件。
- `last_committed_bot_reply_at`：OPT-19 的 committed send 时间。
- `unanswered_proactive_count`：主动消息后没有真实用户回应的连续次数。
- `proactive_generation`：任何新用户活动或手动取消时递增。

## 实施步骤

1. **时间可注入测试**
   - 使用 fake clock，禁止依赖真实 sleep。
   - 覆盖重启、时区、静默区间、due 过期、用户新消息竞争。
2. **持久化迁移**
   - 增加调度状态表或扩展现有 state，建立 `next_proactive_due_at` 索引。
   - 旧数据迁移时根据现有 last activity 安全推导；无法推导则延后一个完整周期，不立即补发。
3. **双水位更新**
   - 入站 canonical user event 更新用户水位。
   - `CommittedBotTurn` 更新 Bot 水位。
   - Planner 草稿、失败发送、外部回声不更新。
4. **generation 取消**
   - 创建主动任务时捕获 generation。
   - 模型调用前、模型调用后、发送 claim 前分别核验。
   - 用户新消息使旧任务标记 cancelled，不向用户发送，也不 commit。
5. **due 查询**
   - 调度器按 `next_proactive_due_at <= now` 查询有限批次。
   - 单会话 claim 防止多个 worker 重复执行。
   - 执行完成或取消后原子计算下一次 due。
6. **chat kind**
   - private intent 不写 group_id。
   - group intent 携带真实 group ID。
   - 工具与 Reply 根据 chat kind 使用正确平台 API。
7. **未应答上限**
   - 达到配置上限后延长冷却或停止主动消息。
   - 用户任何真实回应归零。
8. **上下文快照**
   - 主动候选记录创建时 topic snapshot，但执行时重新读取最新 canonical timeline。
   - 若 topic epoch 已变化，旧候选失效或重新 Judge。
9. **失败策略**
   - provider/工具/发送失败只记 trace，默认静默。
   - 不向群里发送 executor alert。
   - 失败按退避设置下一次 due。

## 配置页面

配置项全部使用易懂中文：

- 启用主动聊天
- 私聊主动聊天
- 群聊主动聊天
- 最短沉默时间
- 最长随机等待时间
- 无回应时最多主动联系次数
- 主动任务失败后的重试间隔
- 安静时段

内部 generation、watermark 不暴露为用户配置。

## 测试矩阵

- 到期前用户发言。
- 模型生成中用户发言。
- 发送 claim 后、真正发送前用户发言。
- 容器在 due 前/后重启。
- 私聊与群聊同 ID 数字碰撞。
- 连续主动两次无人回应。
- 主动回复发送失败、部分发送。
- 多 worker/插件重载重复 run_once。

## 观测字段

- `last_real_user_activity_at`
- `last_committed_bot_reply_at`
- `next_proactive_due_at`
- `proactive_generation`
- `captured_generation`
- `proactive_cancel_reason`
- `unanswered_proactive_count`
- `due_claim_status`
- `proactive_commit_id`

## 验收标准

- 用户发消息后，旧主动任务 100% 在发送前取消。
- 重启不会立即向全部会话补发。
- 私聊主动消息使用 FriendMessage 路由，群聊使用 GroupMessage。
- 达到未应答上限后停止连续打扰。
- 主动回复进入同一 CommittedBotTurn 写回链。
- 主动失败不会把内部错误发给普通用户。

## 风险与回退

- 时间迁移容易造成集中触发，首次上线给 due 增加稳定随机抖动。
- generation 检查必须与 send claim 同步，不能只在任务开始检查一次。
- 可通过关闭主动总开关回退；持久状态保留，不删除。

## 完成记录

- ChatState 已增加双水位、持久 due、generation、未应答次数、commit/cancel 与 claim 字段，并通过增量迁移与旧字段回填保持兼容。
- 调度器同时扫描内存活跃会话和数据库到期会话；SQLite 条件更新实现单会话原子 claim/settle，插件重启后仍可恢复到期任务。
- 真实用户事件递增 generation、清空未应答次数并取消旧 claim；主动任务在模型前、模型后、每段文本发送前和 TTS 发送前复检 generation。
- 私聊 intent 使用 FriendMessage 语义且不写 group_id；群聊 intent 保留真实 group_id；主动失败静默并按配置退避。
- 只有成功提交的可见回复更新 Bot 水位；主动 commit 才增加未应答次数，发送失败与草稿不更新。
- 中文配置已增加私聊/群聊开关、未应答上限、失败重试和 claim 租约；`_conf_schema.json` 已通过 JSON 解析。
- 回归：`tests/regression/proactive/test_proactive_scheduling_consistency.py` 与 reply/attention/proactive 相关测试合计 145 项通过（最终全量结果见 OPT-24 完成记录）。
- 尚需部署后灰度统计主动取消率、到期任务恢复率和真实 private/group 路由；不属于本地代码完成阻塞项。
