# OPT-08 模型调用成本削减（mood / cognitive / judge 缓存 / 私聊串行）

状态：代码完成（待调用量/延迟复采 + judge A/B） ｜ 优先级：P1 ｜ 依赖：量化验收建议先做 OPT-11 的 RT-02 口径修正 ｜ 覆盖发现：RT-03(P1，已吸收 PL-12)、RT-06(P2)、RT-09(P2)、RT-11(P2/LIKELY)、ID-09(P2) ｜ 基线：88% 的 LLM 调用花在最终不回复的消息上；私聊首响 p50 44.3s。

## 完成记录

**2026-07-26 代码侧完成**（全部行为变更配置可回退）：

- **RT-03 mood 后置**（新配置 `attention.mood_post_judge_enabled` 默认开）：ingress 的无条件 mood LLM 改为——强唤醒（必回复）保持即时感知；ambient 消息在 judge_action 确定为回复类后才 **fire-and-forget** 情绪感知（WAIT/IGNORE 不付调用，且不再阻塞关键路径）；decision_router 的判决前 mood 块同步受该开关控制。私聊排水循环的 mood 由串行 await 改为 pending 暂存 + 判决后触发（连带 ID-09 的并行化收益）。**语义权衡记录**：被忽略消息不再更新情绪（judge 内嵌 mood_delta 仍在）；judge 读到的情绪数值滞后一条消息。
- **RT-06 认知循环门槛**（新配置 `attention.cognitive_loop_min_think_level` 默认 2）：think1 不再无条件放行，落到既有"长句(≥12字)/复杂度信号"判定；设 1 可回退旧行为。
- **RT-09 judge 前缀缓存**：固定 rubric（决策流/人格维度 Key/情绪标签/JSON schema/mood_delta 说明）逐字迁入 `JUDGE_STABLE_PREFIX`（system 可缓存），user prompt 只留动态段并按"半稳定在前（persona/keyword/actions）、易变在后（history/message）"排序；action 取值约束保留在 user prompt 的可用动作段。
- **ID-09 私聊跳过 judge**（新配置 `attention.private_skip_judge_enabled` 默认开）：合并窗+settle 已承担等待职能，16h 实测 judge 在私聊 18/18 全 REPLY。
- **RT-11 取证埋点**：`gateway_call` 记录 `semaphore_wait_ms` 进 ledger metadata；信号量拆分待线上数据定论后另行实施（LIKELY 项不先动刀）。
- 测试：新增 `tests/regression/conversation/test_llm_cost_reduction.py` 10 条（router 前置 mood 开/关对照、gate 配置管道、认知门 4 态、judge prompt 结构快照+源码扫描）；既有测试 4 处按新契约更新（3 处显式关闭后置开关锁旧路径语义、1 处 think1 平凡消息改断言 False + 补 think2 放行）。受影响套件 214 passed。
- 待部署验收：mood 池调用数 ≈ executed 轮数（基线 364 vs 67）；attention.dispatch p50 <1s（基线 4.4s）；cognitive_loop 调用量降至 think≥2 量级；judge 池 cached input 显著 >0 且 **judge_outcomes 分布 A/B 漂移 <5pp**（reply/ignore/wait 比例）；私聊 reply_age p50 从 44.3s 回落；semaphore_wait_ms 分布决定是否拆信号量。

## 目标

- mood 调用数从 364 次/16h（57% 总调用）降到 ≈executed 轮数；attention.dispatch p50 从 4.4s 降到 <1s。
- cognitive_loop 不再对 82% 的 think1 消息全量放行（p50 15s 的意图分类只换来 reply/comfort 平凡结论）。
- judge prompt 缓存命中从 0-25% 提到与 dialog 同量级（87.7%）。
- 私聊首响 reply_age p50 从 44.3s 显著回落（五段串行 → 并行/裁剪）。

## 基线证据

- **RT-03**：`gate.py:1079-1080` 在注意力入口主线 `await _apply_primary_mood_update`（mood LLM 3-40s），判定前无条件执行；`judge.py:459-462,520-532` 判决 JSON 本就输出 mood_tag/mood_delta 并已应用——同一文本两次情绪计算。364 次 mood vs 67 次回复；c7d6148 样本 mood 39.7s 后该消息被 ignore。
- **RT-06**：`cognitive_loop.py:192-194` think>=1 即放行（memory/goal/slang 的门槛都是 >=2）；池 22 次 p50≈15s max 34.5s；是私聊"judge→context_build 9-10s 空档"的真身（且常被记到邻轮 ledger 制造观测假象）。
- **RT-09**：`judge.py:419-463` 把固定动作表/维度 key/JSON schema/mood 说明放在动态段之后的 user prompt，system 仅 222 字符；539 次调用累计 1.07M prompt 字符几乎全价。7-25 已列 P1，至今未动（KNOWN_OPEN）。
- **RT-11**（LIKELY）：`gateway_call.py:193-194` 全局信号量(3) 不分关键路径；skipped 轮 judge ledger elapsed 51.7/32.7/30.3s 而 attempt 仅数秒（排队证据），缺每槽等待直方图故列 LIKELY。
- **ID-09**：私聊 worker 内联串行 settle(1.5s+)→mood(3-8s)→judge(5-17s)→cognitive(8-15s)→tools(5-9s)→send；16h 内私聊 judge 18/18 全 REPLY，turn_merge+settle 已承担"等他说完"职能，judge 在私聊是纯延迟。样本"呜呜呜"总 64.4s。

## 实施步骤

1. RT-03：mood 独立 LLM 调用改为三选一（按改动量递增）：(a) 移到 `judge_action∈{REPLY,TOOL_CALL}` 之后（最少改动）；(b) 与 judge 并行 gather；(c) 复用 judge 返回的 mood_tag/mood_delta 彻底去掉独立调用。**推荐先 (a) 灰度，确认 WAIT/IGNORE 消息不更新情绪可接受后推进 (c)**（judge delta 仍在，衰减/关键词反应时序需确认）。
2. RT-06：`gate_decision` 门槛提为 >=2，或 level1 仅在含复杂度信号时放行；备选与 context_build 并行 gather。确认 planner 默认流在缺 cognitive 输出时足够（memory_policy/intent 有默认值）。
3. RT-09：把固定段全部并入 `JUDGE_STABLE_PREFIX`（system），动态段只留 mood 数值/历史/消息且置尾。**A/B 比较 judge_outcomes 分布**（prompt 重排可能轻微影响判决），加 prompt 结构快照测试。
4. RT-11：先埋点 `semaphore_wait_ms` 进 ledger metadata（取证），确认排队量后拆信号量（critical_path 独立配额或优先队列，总并发不超 provider 限额）。
5. ID-09：私聊分支 mood 改 fire-and-forget 或与 judge 并行；私聊默认 `should_skip_judge=True`（可配置开关保底）；发送后记账移出关键路径统计。
6. 每步部署后跑 trace 复采对比（OPT-11 修正口径后的 analyze 脚本）。

## 验收标准

- 复采指标：mood 池调用数 ≈ executed 轮数；attention.dispatch p50 <1s；cognitive_loop 池调用数降至 think>=2 轮量级；judge 池 `usage_input_cached/usage_input_tokens` 显著 >0；私聊 executed 关键路径 LLM 调用数 4-5 → 2-3，reply_age p50 显著回落。
- judge_outcomes 分布 A/B 无显著漂移（reply/ignore/wait 比例变化 <5pp）；回复质量抽查无退化。
- 全量 pytest 绿 + 新增行为测试（同一 event 至多一次情绪 LLM；think1 平凡消息跳过 cognitive；judge prompt 固定段全在 stable prefix）。

## 风险与回退

- **中风险集中在时序语义**：mood 后置改变 WAIT/IGNORE 消息的情绪更新行为；私聊跳过 judge 需确认无其他职责（16h 样本内无）。全部做成配置开关，异常即回切。
- RT-09 prompt 重排有判决分布漂移风险——A/B 门槛不过就回退重排幅度。
- 每项独立提交可单独 revert。

### G7 补充（2026-07-26）：RT-11 信号量拆分（从 LIKELY 转为已实施）

OPT-08 只埋了 `semaphore_wait_ms` 取证点；本次补齐拆分实现。

**设计要点：不放大总并发**。原全局信号量的目的是 429 保护，直接调大是错的。改为
"总闸不变 + 后台加一层子限流器"：

- `model_gateway`：新增 `_background_semaphore = Semaphore(max - reserved)`；
  `reserved` 由 `infra.critical_path_reserved_slots`（默认 1）控制，热更新时一并重建。
- `gateway_call._concurrency_slot(critical_path)`：关键路径直取全局槽；后台**先取子槽
  再取全局槽**——顺序关键，反序会让后台攥着全局槽等子槽，反而把关键路径堵死。
- 边界收敛：`reserved=0` 不创建子信号量（完全等同旧行为）；`reserved>=max` 自动收敛到
  `max-1`（后台至少 1 个槽）；`max=1` 时无法预留。
- 配置：`infra.critical_path_reserved_slots`（schema + pydantic 双侧，纯中文 hint）。

**测试**：`tests/regression/architecture/test_gateway_concurrency_priority.py` 9 条
（槽位算术 5 / 并发行为 4），含**429 红线断言**（关键路径+后台同时在飞数不得超总上限）。
红验证 **8 红 1 绿**（13.7s 快速失败）。

**过程改进**：首版红验证时测试**挂死**而非失败——实现缺失时后台任务抛异常，等待信号量的
主协程永远等下去。已给所有等待加 `asyncio.wait_for(timeout=2.0)`：挂死的测试比失败的测试
更糟，回归时会拖死整个 CI。

全量回归 **1829 passed, 1 skipped**。
