# OPT-15 学习与 Dream 治理（毒丸 / 幻觉晋升 / 演示残留）

状态：未开始 ｜ 优先级：P2 ｜ 依赖：无（ML-08 动手前需 DB 取证） ｜ 覆盖发现：ML-07(P2)、ML-08(P2/LIKELY)、ML-09(P2)、ML-11(P3) ｜ 写入端治理在 320663f/f09cf65 后是扎实的（历史 06 号审计 7 个问题全部确认已修），剩余风险集中在 Dream 产物与挖掘韧性。

## 目标

- Dream 不再往真实会话写运维噪声记忆；LLM 合并叙事经 admission 治理。
- 梦境幻觉不能凭单次响应内的重复项晋升为 confidence=1.0 权威事实（可覆盖用户亲述）。
- 坏批次不再永久卡死单群学习；挖掘链路存活状态可观测。
- 清理演示场景硬编码（任意数字+“服务器”即产出资产 claim）。

## 基线证据

- **ML-07**：`dream_scheduler.py:233-237` 每轮把 `[dream_maintenance] 完成 N 次维护动作` 写入真实 session（importance=0.65 active，可被检索注入 prompt）；`dream_agent.py:226-231` merge 走 `add_memory(source=legacy_add_memory)`，不在 `memory_admission_service.py:22` 的治理名单。
- **ML-08**（LIKELY）：`promotion_engine.py:105-106,147-149` 不去重 detected_facts，同一事实在单次 LLM 响应重复 3 次即满足证据阈值；confidence 缺省 0.9/signal 缺省 high 恰好过滤线上方；晋升写入硬编码 confidence=1.0 且 source 在 authority EAV 白名单——可 supersede 用户亲述。**取证 SQL**：`SELECT * FROM canonical_memories WHERE source='dream_audit_pipeline' ORDER BY create_time DESC LIMIT 20;` 检查 metadata.evidence_turns 是否同 turn 重复。
- **ML-09**：`evolution_manager.py:604-614,809-812` fail-closed 不标 processed（f09cf65 防丢数据，方向正确）但 backlog 固定取同一头部批次、无失败计数/跳过——坏批每 30min 原样重试永久卡死该群学习；成功路径不打日志，新窗口 16.6h 零挖掘日志无法区分"没跑"与"没失败"（旧日志实证群 1075910254 连续 3 次失败）。存活定论 SQL：`SELECT key,value FROM memory_v2_meta WHERE key LIKE 'learning_mining_ledger:%';`
- **ML-11**：`claim_rules.py:9-12` `SERVER_COUNT_PATTERN=re.compile(r"(\d+)")` + 火锅/芒果词表进了生产 claim 规则与检索重排——聊 MC 服务器人数会被记成用户资产。

## 实施步骤

1. ML-07：maintenance 摘要改写 `__dream_diary__`（或仅存 meta）；dream merge 的 source 加入 admission 治理名单。断言：跑一次 dream 后检索该会话无 `[dream_maintenance]` 候选。
2. ML-08：先跑取证 SQL 定级；修复：`_iter_detected_facts` 按 `(key, evidence.turn_id)` 去重；confidence 取证据实际置信度上限而非硬编码 1.0。单测：同响应 3 份重复 → 不晋升；3 个不同 turn → 晋升。
3. ML-09：`_backlog_failure_until` 增加按群失败计数，>=3 次跳过头部 `min_mining_context` 条并标记 processed（跳过量以 overlap 尺寸为界）；`run_backlog_mining_once` 成功时打 INFO 摘要。单测：必失败 enricher stub，第 4 次重试后头部批被跳过且 backlog 下降。
4. ML-11：server/anxiety 规则收紧为句式匹配（“我有/我的+数量词+服务器”）或移除；词表迁配置。负例测试：“服务器 100 人在线”不产出 server_count claim。

## 验收标准

- 四项单测绿 + 全量 pytest 绿。
- 部署后：dream 噪声记忆停止增长（SQL 计数持平）；挖掘日志出现成功 INFO；毒丸场景注入测试通过；DB 采样确认 dream 晋升事实的 evidence_turns 无同 turn 重复。

## 风险与回退

- ML-08 若取证显示线上已有污染事实，需追加一次性清理脚本（单独评审，不在本 OPT 自动执行）。
- ML-09 低风险（跳过量有界）；ML-07/11 低风险。
- 各项独立提交可单独 revert。

## 完成记录

（完成后填写：取证 SQL 结果、dream 记忆计数趋势、毒丸测试输出）
