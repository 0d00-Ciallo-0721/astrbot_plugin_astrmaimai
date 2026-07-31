# OPT-24 上下文架构回放、迁移、观测与灰度

状态：**本地代码与自动化验收完成，待生产灰度** ｜ 优先级：P0 ｜ 依赖：OPT-17～OPT-23 ｜ 来源：四份报告的共同验收缺口与 AstrMai 线上事故复盘

## 目标

- 将历史真实事故转成稳定、可重复的回放测试，避免靠人工聊天碰运气。
- 对新旧事件、目标、上下文、回复提交和主动调度提供 shadow/cutover 机制。
- 让每次异常都能回答：看到了哪些事件、目标是谁、为什么参与、实际发了什么、写入了什么。
- 在生产数据不被破坏的前提下完成数据库迁移、灰度和回退演练。

## 基线证据

- 既有线上事故主要依靠用户聊天截图、容器日志和事后数据库查询拼接，缺少能在本地稳定重现的端到端回放入口。
- Turn Trace 已能记录 LLM、上下文块和回复统计，但还不能将 canonical event、TurnTarget、发送提交、actor filter 和主动 generation 串成一条证据链。
- 历史部署曾因备份目录留在 `plugins/` 下被 AstrBot 当作插件误加载，说明发布结构本身也必须进入验收。
- OPT-17～OPT-23 同时涉及 schema、读写路径和行为 cutover；没有分阶段 shadow 与独立开关时，任一异常都难以快速回退。

## 回放样本集

至少固化以下场景，使用匿名稳定 ID：

1. **高频人物黏连**：用户 A 连续聊天后用户 B @Bot，回复必须指向 B。
2. **冒犯与道歉**：同一用户前后态度连续，其他人复读不改变对象。
3. **公共称号游戏**：群共享规则可延续，但专属关系不扩散。
4. **短承接**：“不对”“我没有”“然后呢”绑定正确上一轮 committed target。
5. **空酱身份**：同一昵称在共享时间线中有来源，不会片刻后说不认识。
6. **发送部分失败**：只提交成功段。
7. **新消息取消主动任务**：模型生成中有用户消息，旧主动回复不得发送。
8. **reply/at/quote 冲突**：目标证据顺序稳定。
9. **peer poke/direct poke**：actor 与 target 正确。
10. **图片占位**：视觉未完成也保留媒体事件和人物归属。

样本不得包含真实 API Key、Cookie、私密路径或未经脱敏的个人数据。

## 实施步骤

1. **回放 Harness**
   - 输入 canonical event JSONL 与可控时钟。
   - 运行 Normalizer → Timeline → Focus/Target → Participation → Context → Reply mock → Commit。
   - 支持固定模型输出 fixture，测试结构而非供应商随机性。
2. **契约测试**
   - ConversationEvent schema。
   - TurnTarget immutable。
   - CommittedBotTurn 幂等。
   - context block trust boundary。
   - actor whitelist。
   - proactive generation。
3. **Shadow 模式**
   - 新旧 event window、target、prefilter、memory filter 同时计算。
   - 用户行为仍由旧路径控制。
   - trace 记录差异，不记录原文。
4. **逐项 Cutover**
   - canonical read。
   - target read。
   - committed history。
   - renderer。
   - participation FORCE_PASS。
   - memory actor filter。
   - participation DROP。
   - proactive due。
   每项有独立开关和回退，不使用一个总实验开关掩盖差异。
5. **数据迁移**
   - 生产库先备份。
   - 迁移脚本 dry-run 输出预计行数和冲突。
   - 迁移可重复执行。
   - 老数据无法补 actor/target 时保留 unknown，不通过 LLM 猜。
6. **性能基线**
   - 规范化、target、renderer、commit 各自计时。
   - Judge 调用数、上下文字符数、memory 候选数与回复 P50/P95。
   - 新增结构不能在消息热路径整文件重写。
7. **线上灰度**
   - 先单个低风险群，再活跃群，再私聊。
   - 每阶段至少 24 小时或达到样本量。
   - 发现人物错绑或草稿污染立即回退对应 cutover。
8. **管理页诊断**
   - Turn Trace 展示事件源、target、participation、context blocks、commit 和 memory filter。
   - 只显示必要文本摘要或 hash，避免泄露完整聊天。
9. **发布包**
   - 发布构建验证新增源码、迁移和 schema 文件存在。
   - 排除 tests/.agent/本地数据。
   - 容器内关键文件 SHA256 与本地提交一致。

## Trace 契约

每个 executed 或 skipped turn 至少包含：

```text
turn_id
input_event_ids
canonical_event_status
turn_target
actor_whitelist
participation_decision
judge_decision
context_block_stats
reply_plan
reply_commit
memory_actor_filter
proactive_observation
status / elapsed
```

skipped 轮允许无 reply，但必须有 skip reason。

## 灰度门槛

| 指标 | Cutover 门槛 |
|---|---|
| canonical shadow actor/event 匹配 | ≥99.9% |
| target 固定事故集 | 100% |
| draft history write | 0 |
| direct wake 漏回复 | 不高于基线 |
| actorless 关系注入 | 0 |
| committed/history 文本一致 | 100% |
| proactive stale send | 0 |
| trace JSONL 解析错误 | 0 |

## 验收标准

- 十类历史事故全部进入自动回放，并能在故意回滚对应修复时稳定变红。
- 新旧路径 shadow 差异可按 turn ID、event ID 和 actor ID 定位，不依赖完整原文。
- 每个 cutover 开关均完成一次启用、禁用和容器重启后的状态验证。
- 数据迁移 dry-run、正式迁移和重复执行结果一致，旧管理页在兼容期可读。
- 生产灰度达到表中门槛，且人工抽检未发现人物错绑、草稿污染和主动抢答。
- 发布包不包含测试、`.agent`、生产数据或插件目录内备份，容器启动无 AstrMai Traceback。

## 验收命令

实现时按真实测试文件名细化，至少执行：

```powershell
python -m pytest -q tests/regression/conversation
python -m pytest -q tests/regression/architecture
python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths"
python -m compileall -q astrmai main.py config.py
git diff --check
```

发布后：

- 拉取新版本 JSONL 与容器日志。
- 按 turn ID 去重。
- 生成 target、participation、commit 和 actor filter 专项报告。
- 对随机样本做人工复核，不仅看聚合数字。

## 风险与回退

- 结构迁移和行为 cutover 不能同一提交。
- 线上开关默认从 shadow 开始。
- 回退不得删除新事件或 commit 数据，只切回旧读取。
- 发现数据不一致时优先停止学习/记忆写入，回复仍可降级走近期 canonical timeline。
- 备份目录不得放在 AstrBot `plugins/` 下，避免被误加载。

## 完成记录

- 已固化 10 类匿名事故样本至 `tests/fixtures/context_architecture_incidents.jsonl`，并新增 `astrmai/conversation/replay/` 回放 harness；模型输出使用 fixture，不依赖供应商随机性。
- 已建立 schema、TurnTarget、CommittedBotTurn、可信边界、actor whitelist、participation、proactive generation 和回复提交持久化恢复的架构回归测试。
- 数据库迁移推进至 v73；`architecture_migration_audit.py` 可审计表、列、索引和版本，覆盖旧库重复升级与缺失对象检测。
- Turn Trace 已串联 canonical event、target、participation、context package、reply plan/commit、memory actor filter 与 proactive observation，并保持 skipped turn 的原因可解释。
- 发布候选契约验证新增源码、迁移、fixture 与脚本进入发布包，同时排除 tests、`.agent`、缓存和生产数据。
- `reply_commit_outbox` 已完成跨重启补偿回归：成功消费者不会重放，失败消费者可恢复，补偿过程不会再次发送用户消息。
- 本地自动化验收：`python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths"` 得到 `1984 passed, 1 skipped, 1 deselected`；相关架构组合测试 `45 passed`；持久化提交链组合测试 `63 passed`。
- 本地未执行服务器部署、生产数据库正式迁移、24 小时 shadow、cutover 开关启停、真实容器回退演练和线上人工抽检；这些属于下一阶段外部环境验收，不标记为完成。
