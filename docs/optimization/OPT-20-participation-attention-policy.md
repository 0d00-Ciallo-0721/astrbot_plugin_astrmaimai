# OPT-20 群聊参与判定与短期迟滞

状态：**已实施（高置信 DROP 保持 shadow）** ｜ 优先级：P1 ｜ 依赖：OPT-17、OPT-18 ｜ 来源：AngelHeart 参与迟滞、MaiBot 参与评分、AstrMai 既有三态 prefilter

## 目标

- 扩展现有 `FORCE_PASS / DROP / NEED_JUDGE` 前置过滤，而不是新建第二套 Attention 状态机。
- 让明确唤醒和明显无关消息尽量不调用 LLM Judge。
- 对刚参与的话题/人物保留短期、可解释的自然承接能力。
- 话题或主要 actor 切换时立即使旧参与观察失效，避免机器人突然延续过时剧情。

## 基线证据

- `decision_router.py` 已有三态 `AttentionPrefilterDecision`，目前主要覆盖 strong wakeup、direct request、有限 active continuation 和空群事件。
- 当前 active continuation 依赖前一条可见事件为 Bot 且在 180 秒内，未绑定上一轮 committed target 和 topic epoch。
- 群聊大量消息最终 IGNORE/WAIT，但仍进入 Judge；同时过度扩大确定性 DROP 会伤害“然后呢”“我没有”等短承接。
- AngelHeart 与 MaiBot 的共同启示是：确定性层只处理高置信信号，灰区仍交 Judge。

## 设计

### ParticipationScore（可解释派生值）

只由结构信号计算，不读取人格和用户私密记忆。建议初始信号：

| 信号 | 方向 |
|---|---|
| @Bot、回复 Bot、poke Bot、唤醒词 | 强正 |
| 当前 actor 等于上一轮 committed target | 正 |
| 当前消息引用上一轮 source/commit | 强正 |
| topic epoch 相同且在短迟滞窗口 | 正 |
| 当前 actor 属活跃参与者 | 弱正 |
| 纯外部插件回执、Bot 自己回声、重复消息 | 强负 |
| 无文本且无媒体/互动 | 强负 |
| topic 已切换、actor 完全无关 | 负 |

输出：

- `FORCE_PASS`：明确直接请求或高置信承接。
- `DROP`：确定无关、回声、重复、无信息事件。
- `NEED_JUDGE`：其余灰区。

### ParticipationPhase

使用派生状态，不作为长期业务真源：

- `detached`
- `observing`
- `engaged`
- `cooling`

状态绑定 `chat_id + topic_epoch + actor set`，有短 TTL。任何 topic epoch 变化、强冲突目标或长时间无活动都使其失效。

## 实施步骤

1. **先加 shadow 观测**
   - 新 policy 计算结果但不改变现有行为。
   - 对比现有 prefilter/Judge 最终决策，建立混淆矩阵。
2. **批次强信号聚合**
   - owned batch 中任一事件明确 @/reply/poke Bot，批次不得因最后一条无关消息丢失强唤醒。
   - 强信号必须保留 source event ID。
3. **绑定 committed output**
   - active continuation 使用上一轮 `CommittedBotTurn.target`、source IDs 与 topic epoch。
   - 不再仅看“上一条 sender 是 bot”。
4. **实现可解释评分**
   - 评分器保持纯函数，返回信号列表、分数和决定。
   - 配置仅控制阈值/TTL，默认值写入 schema，中文说明用户可懂。
5. **短期迟滞**
   - engaged 后同 actor/同 topic 的短承接降低 Judge 门槛。
   - 不同 actor 加入共同话题时可进入 observing，不继承专属关系。
   - cooling 到期回 detached。
6. **观察失效**
   - topic epoch 变化。
   - 明确目标切换。
   - 上一轮 Bot 回复未 commit。
   - 会话离开/群成员变更/插件重载。
7. **分阶段 cutover**
   - 第一阶段仅启用 FORCE_PASS。
   - 第二阶段启用高置信 DROP。
   - NEED_JUDGE 始终保留。
8. **群 wait 关系**
   - ParticipationPolicy 只决定是否进入认知链，不直接修改 wait manager。
   - wait scope 修正在 OPT-22，通过独立特征测试控制。

## 测试矩阵

- batch 中前一条 @Bot、后一条图片或短文本。
- Bot 回复 A 后 A 说“我没有”，B 同时插话。
- topic 切换后同一用户发送短词。
- 外部插件 `[聊天记录]` 回执。
- Bot 自己文本被群友复读。
- peer poke 与 direct poke。
- 纯图片、At+图片、回复图片。

## 观测与指标

- `participation_score`
- `participation_signals`
- `participation_phase`
- `participation_phase_age_ms`
- `prefilter_shadow_action`
- `prefilter_judge_agreement`
- `judge_avoided`
- `strong_wakeup_override`
- `observation_invalidated_reason`

上线关注：

- 每 100 条群消息 Judge 调用数。
- direct wake 漏回复率。
- active continuation 承接率。
- false DROP 人工抽检率。

## 验收标准

- 所有 @Bot/reply Bot/poke Bot 固定样本 FORCE_PASS。
- 明确回声、重复和空事件 DROP，不调用 Judge。
- shadow 24 小时后，高置信 DROP 与 Judge IGNORE 一致率达到 98% 以上才允许 cutover。
- 高活跃群聊中短承接不因无关插话丢失 target。
- topic 切换后旧 participation phase 不再生效。

## 风险与回退

- DROP 误判风险最高，必须 shadow 后分开开关。
- ParticipationPhase 只保存在派生运行时，可从事件和 committed turn 重建，避免形成第二套不可恢复状态机。
- 回退关闭新 policy 后恢复 NEED_JUDGE，不删除观测字段。

## 完成记录

- 新增纯结构 `ParticipationPolicy`，输出分数、信号、阶段、阶段年龄和失效原因。
- owned batch 内的 @Bot、回复 Bot、戳 Bot 与其他 direct wake 信号会聚合 source event ID；明确唤醒维持确定性直通。
- 短承接优先绑定 `CommittedBotTurn.target_sender_id/source_event_ids/topic_epoch`，不再只依赖窗口里“上一条可见消息来自 Bot”。
- 不同 actor 只进入 observing，不继承也不擦除原 committed target 的短迟滞状态；topic epoch 变化和 TTL 到期会使旧观察失效。
- `participation_force_pass_enabled` 默认开启；`participation_drop_enabled` 默认关闭。外部回执等 DROP 候选先写 shadow 观测，空群事件保留原确定性 DROP。
- Trace/event extras 已记录 `participation_score`、`participation_signals`、`participation_phase`、`prefilter_shadow_action`、`prefilter_judge_agreement`、`judge_avoided`、强唤醒事件 ID 与失效原因。
- 兼容回退：DialogueStore 不可用时继续使用旧版 active bot continuation，避免旧部署行为回退。
- 回归测试：`tests/regression/architecture/test_participation_policy.py` 与 `tests/test_attention_gate_refactor.py`，共 `64 passed`。
- 待真实环境验收：累计至少 24 小时 shadow 样本，计算高置信 DROP 与 Judge IGNORE 的一致率；达到 98% 前不启用 DROP cutover。
