# 基线证据（2026-07-27 全量审计快照）

> 代码基线: HEAD=4da2910 · 运行时证据: `.agent/runtime-observability-c4aee57-20260726/`（585 traces / 16h / 15 会话）+ 1.3MB AstrBot 框架日志 + 364KB 诊断日志。所有 OPT 的"验收标准"以本文件的数字为对照基线。

## ⚠️ 口径变更切换点（2026-07-26 优化阶段落地后新增）

**本文件的数字是 c4aee57 基线口径。下述三处口径在优化阶段已经改变，复采后的新报表与本文件数字不可直接逐项比较**——差异可能来自口径而非行为改善。做前后对比时请先确认对照的是同一口径。

| # | 口径 | 旧（本文件基线） | 新（当前代码） | 影响的基线数字 | 变更处 |
|---|---|---|---|---|---|
| 1 | **judge 调用计数** | 按 `stage` 字符串匹配判定 judge 调用——匹配恒不中，judge 恒计为 0 | 按 `pool == "judge"` 判定 | 本文件所有"judge 调用次数/占比"为 **0 的行都是仪表假象**，真实值需用新脚本对同一份归档重跑 | RT-02 / OPT-11，`scripts/analyze_turn_ledger.py` |
| 2 | **发送段数（total）计数** | 满发路径从不写 `sent_segment_count`，恒为 0——与同一轮的 `reply_stats.segment_count` 自相矛盾 | 发送循环结束后无条件写入实际发送段数 | 任何按 `sent_segment_count` 聚合的「发送总量 / 满发率 / 截断率」，旧数据全部低估（满发轮计 0），新旧不可比 | ID-05 / OPT-11，`reply_artifact_builder.py` |
| 3 | **trace 序列化格式** | 单文件 `turn_trace_samples.json`（整文件读改写 + indent） | append-only `turn_trace_samples.jsonl`（同 turn 后写覆盖先写，周期性压实） | 采样文件路径与形态变了；`per-chat 上限`由"写入即刻生效"变为"压实后保证"，两次压实间样本数可短暂超出配置值 | WU-06 / OPT-11 + G8 |

**复采操作**：`scripts/analyze_turn_ledger.py` 双格式可读（`.jsonl` 与 legacy `.json` 均可），
所以**用当前脚本重跑 `.agent/runtime-observability-c4aee57-20260726/` 的历史归档**即可拿到
「同口径的旧基线」——这对口径 1 有效（judge 计数是脚本侧口径，重跑即可修正）。
口径 2 属于**写入端**变更，历史归档里根本没记过正确值，重跑也补不回来，只能从复采的新数据起算。
本文件正文数字对口径 1、2 未涉及的指标（漏斗状态分布、时延分位、记忆注入率等）仍然直接可用。历史归档转换用
`scripts/convert_turn_trace_to_jsonl.py`（已在 585 样本上实测，转换前后分析结果一致）。

## 代码与测试规模

- `astrmai/` 业务代码 **80,683 行**（13 个子包）；最大文件 `pfc_tools.py` 2991L、`chat_loop_kernel.py` 2336L、`v2_store.py` 2031L、`planner.py` 1911L。
- 测试 **1673 条可收集、0 错误**（`pytest --collect-only`，session-state.md 的 1142 已过期）；行覆盖率 72.9%（7-13 口径）。
- `_conf_schema.json` 58.9KB / 209 个叶子键；配置落地矩阵见 `../../.agent/claude-full-audit-20260727/config_consumption_matrix.md`（死键 9 项）。

## Turn 处理漏斗（585 traces / 16h）

| 状态 | 数量 | 占比 |
|---|---|---|
| skipped_ignore | 317 | 54.2% |
| skipped_sensor_filter | 102 | 17.4%（含 14 条主动候选 + 30 条 peer poke 全灭）|
| skipped_wait | 83 | 14.2% |
| executed（含 topic_confirmation） | 69 | 11.8% |
| stale_drop | 7 | 1.2%（全部在群 1062115731，该群 executed 仅 6）|
| skipped_repeater_echo | 7 | 1.2% |

## LLM 调用基线（llm_call_ledger 1022 次）

| 维度 | 基线值 | 备注 |
|---|---|---|
| 调用构成 | gateway.chat 950 / gateway.tool 69 / compaction 3 | judge 池 539 次、mood 池 364 次 vs 回复仅 67 |
| 非回复消耗 | **~88% 调用花在最终不回复的消息上** | OPT-08 主指标 |
| judge/turn | p50=1 / p95=2 / **max=10**（150s 内 10 连判） | analyze 脚本口径 bug 曾报 0（OPT-11） |
| 延迟 | gateway.chat p50 7.4s / p95 20s / max 122.8s | attention.dispatch p50 4.4s≈mood 串行 |
| 私聊首响 | executed reply_age **p50=44.3s** / max 357s | 五段串行（OPT-08/ID-09） |
| memory.injection | p50 29.8s / max 92s（深检索 3 次中 2 次拖到 stale_drop） | OPT-06 主指标 |
| 失败 | 模型尝试层 timeout 13 / error 12 / ProviderNotFoundError 3 | `turn_deadline_exhausted` 日志 71 条（OPT-02） |
| 预算 | exhausted 1 次（turn 7edddd 总长 420s）；remaining_ms p05=0 | tool/vision 不受预算约束（OPT-07） |
| 缓存 | dialog 池 cached 87.7%；judge 池 0-25%（system 仅 222 字符） | OPT-08/RT-09 |
| provider 归因 | **unknown 1005/1005** | 能力解析全失败（OPT-09/RT-08） |

## 记忆系统基线

- 注入率 **2.9%**（执行轮 2/69；私聊 19 条回复 0 注入）；skip 主因 think_level_0×52、think1 无关键词×13。
- 即时记忆 LLM 兜底 **17/17 全部失败**（contextvar 预算泄漏）；`memory_funnel` 在 executed 轮 64/67 缺失（观测缺口）。
- FAISS 删除 API 从未被调用（幽灵向量单调累积）；偏好类 dedup_key 不含 value（新喜好覆盖旧喜好）。
- `MemoryMaintenanceService.run_once`（索引修复+积压清理）**无任何调度方**；黑话/表达待审积压只增不减。

## 主动行为基线

- wakeup/heartflow/签到主动消息 **发出 0 条**（14/14 候选死于传感器过滤，跨两个观测窗）；peer poke 30/30 被过滤。
- 日志/trace/dispatcher 三层诊断全部误标为 "skipped by planner"。

## WebUI/校准基线

- 编辑通过丢弃修订文本、权重按 `weight-1.0` 增量漂移（上限 3.0）、pending_human 不进人工队列、`/learning/cooldowns` 恒空（属性名自初始提交即错）。
- trace 样本库每条消息全文件重写：15MB 实测 parse 0.21s + dumps 0.46s，与 WebUI 45s 轮询共锁。

## 日志异常指纹（astrbot_since_c4aee57.log，178 条 WARN）

| 指纹 | 次数 | 归属 |
|---|---|---|
| Gateway model timeout (1/3)（code2/code3 flash） | 40 | OPT-07/08 |
| [Gemini] request_retry 2/5→5/5（vision 502 重试风暴，15:25-15:29 同窗） | 43 | OPT-07/RT-05 |
| instant llm backfill degraded: turn_deadline_exhausted | 17 | OPT-02 |
| ContextEconomy cache-priority workload 警告 | 12 | OPT-08 |
| star.context 没有找到 ID 为 openai/deepseek-v4-pro 的提供商 | 4 | OPT-09/RT-07 |
| deep query rewrite degraded | 3 | OPT-06/ML-02 |
| executor tool model failed（实为 stale 误分类） | 3 | OPT-01/TL-05 |

## 复采对照方法

部署任一行为类 OPT 后：服务器拉取新 `turn_trace_samples.json` + 同窗 Docker 日志 → `PYTHONIOENCODING=utf-8 python scripts/analyze_turn_ledger.py <file>` → 与本文件对应行比对。注意：analyze 脚本的 judge 口径在 OPT-11 修正前会低报 judge/turn（用 `pool=='judge'` 重算）。
