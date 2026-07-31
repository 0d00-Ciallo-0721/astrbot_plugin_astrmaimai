# 下一会话入口：群聊上下文架构优化

状态：**本地目标完成，下一步为生产灰度验收** ｜ 主路线图：[`CONTEXT-ARCHITECTURE-ROADMAP-20260731.md`](CONTEXT-ARCHITECTURE-ROADMAP-20260731.md)

> G0～G8 的代码、迁移、回放和自动化测试已完成。当前文件保留为实施证据与上线检查表；下一阶段不得重新实现这些契约，而应按 OPT-24 的灰度顺序部署、采集指标和验证回退。

## 开始前必读

1. 本文件。
2. 总路线图。
3. 即将实施的对应 `OPT-17`～`OPT-24` 文档。
4. `C:/Users/zlj/.codex/skills/astrbot-plugin-dev/SKILL.md`。

仓库源码和测试是事实源；四份外部报告仅用于解释设计来源，不能替代当前代码核验。

## 有序目标

### G0：冻结基线

- 记录 `git status`、当前提交和现有未提交改动。
- 运行群聊 attention、planner、reply、runtime、proactive、memory 相关测试。
- 固化已知事故回放样本，不使用生产数据库做写操作。

成功标准：能够区分已有失败与本阶段新增回归。

### G1：实施 OPT-17

- 建立单一 `ConversationEvent` 语义契约。
- 为入站、互动、图片、引用、Bot 输出定义统一 ID 和字段。
- 兼容 `NormalizedEvent`、`DialogueSegment`、`MessageLog` 与 trace。
- 完成持久化双写、shadow read 和去重测试。

成功标准：同一原始消息在所有层具有相同 event ID、actor 和目标字段。

### G2：实施 OPT-18

- 引入结构化且不可变的 `TurnTarget`。
- Focus 阶段确定目标；Judge、Planner、Tool、Reply、Trace 只消费。
- 建立冲突解决规则和目标证据链。

成功标准：高活跃群聊回放中不会因最近发言者变化而改写目标。

### G3：实施 OPT-19

- 将 Planner 的 Bot 历史写入从发送前移到发送成功后。
- 引入 `CommittedBotTurn` 或等价提交回执。
- 对部分发送、TTS、空文本、附件、发送失败和重复回调做幂等测试。

成功标准：模型草稿永不成为历史事实，后续系统只看实际已发送内容。

### G4：实施 OPT-22

- 统一 `MessageRenderer`，显示 actor ID、target、reply/quote 和媒体占位。
- 对 derived context 建立不可信边界和转义。
- 调查并实现基于 AstrBot 公共 API 的外部插件上下文桥。
- 用特征测试确认并修正 group wait 的 chat/thread 作用域。

成功标准：模型能稳定区分“谁说了什么、对谁说、引用什么”，且用户文本无法伪造系统块。

### G5：实施 OPT-20

- 扩展现有三态 prefilter，不创建第二套 Attention。
- 聚合批次强信号，增加可解释 ParticipationScore 和短期参与迟滞。
- 先 shadow，再在指标达标后启用 DROP/FORCE_PASS。

成功标准：Judge 调用下降，直接唤醒与活跃承接漏判不增加。

### G6：实施 OPT-23

- 群共享时间线继续共享。
- 画像、关系、记忆候选按 actor whitelist 隔离。
- 中期摘要保存参与者、未决问题、上一轮 Bot 目标和 topic epoch。
- 过期话题进入确认流程，而非无条件继承。

成功标准：共同剧情可延续，但专属关系和负面态度不跨用户传播。

### G7：实施 OPT-21

- 区分最近真实用户活动与最近 Bot 可见回复。
- 持久化 `next_proactive_due_at` 和未应答次数。
- 用户新消息递增 generation，主动任务发送前再次核验。
- 正确传递 private/group chat kind。

成功标准：主动消息不会在用户刚发言后抢答，不会把私聊伪装成 group。

### G8：实施 OPT-24

- 建立回放 harness、结构契约测试、迁移测试和灰度开关。
- 对人物错绑、上下文断裂、回复草稿污染、主动抢答、群聊等待作用域建立固定回归。
- 部署后按真实群聊矩阵复采 trace。

成功标准：所有 cutover 条件有自动化证据，任一阶段可独立回退。

## 工作纪律

- 严格按 `Plan → Execute → Verify` 执行，每个 G 目标先做 Red 测试。
- 不一次跨越两个 P0 数据契约；G1、G2、G3 独立提交。
- 不把 nickname 当 identity，不用 prompt 补丁替代结构化字段。
- 不在 Planner 草稿阶段写长期事实。
- 不破坏别的插件事件传播，不使用 `event.stop_event()` 拦截非 AstrMai 事件。
- 迁移阶段保留旧读路径和对比 trace，稳定后再移除兼容代码。
- 每完成一项，更新对应 OPT 的“完成记录”和本索引状态。

## 建议验证命令

先查项目实际测试名，再运行最小相关集；全量命令保持：

```powershell
python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths"
python -m compileall -q astrmai main.py config.py
git diff --check
```

本地收口结果（2026-07-31）：

- 全量测试：`1984 passed, 1 skipped, 1 deselected`。
- `reply_commit_outbox` 迁移版本：v72 建表、v73 建重试索引。
- 跨重启 repair 回归：只补做 pending/failed 消费者，不重放已成功消费者，也不触发发送。
- 尚待外部环境：生产数据库备份与 dry-run、容器重启、各 cutover 开关启停、24 小时 shadow 和真实群聊人工抽检。

行为灰度必须额外覆盖：

- 20 条多人高活跃群聊。
- 10 条 direct @ / 回复 Bot / 戳 Bot。
- 10 条短承接语。
- 5 条引用或 @第三人消息。
- 5 条发送失败或部分发送模拟。
- 5 条主动任务被新消息取消。
- 5 条群共享剧情与用户专属关系同时存在的场景。
