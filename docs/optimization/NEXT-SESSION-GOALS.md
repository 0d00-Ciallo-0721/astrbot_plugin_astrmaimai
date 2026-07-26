# 接续目标清单（2026-07-26 交接）

> 上一轮成果：全量只读审计（68 条发现）→ 16 个 OPT 工作流 → 主线 13 个 OPT 代码完成、8 个提交落地。
> 本文件是**唯一接续入口**：新会话读完本文即可继续，不需要回溯对话历史。

## 0. 现状快照

| 项 | 值 |
|---|---|
| HEAD | `78bfa65`（main，**领先 origin/main 13 个提交，尚未 push**） |
| 全量回归 | `1770 passed, 1 skipped`（排除绝对路径环境检查 + signin 时间窗 flaky） |
| 工作区 | 干净（仅 3 项未跟踪的运行时观测原始数据，刻意不入库） |
| 线上状态 | **所有修复均未部署**，无任何线上验证数据 |
| 证据层 | `.agent/claude-full-audit-20260727/findings.json`（68 条全字段） |
| 执行层 | `docs/optimization/README.md`（16 个 OPT 状态表） |

## 1. 目标顺序总览

```
G0  push 授权（用户决策）
     │
     ├── 分支 A（已部署）→ G1 部署复采验收 → G2 数据驱动项（RT-11/RT-02/OPT-14）
     │
     └── 分支 B（未部署，可立即做）→ G3 → G4 → G5 → G6 → G7
```

**不依赖运行环境的（G3-G7）可以随时推进，与部署并行不冲突。**

---

## G0 · push 授权（用户决策，5 分钟）

13 个提交待推送（本轮 8 个 + 会话前遗留 5 个：`4da2910`/`c4aee57`/`20bb585`/`858b8b3`/`f09cf65`）。
`git push` 需用户明确授权，不得自行执行。

---

## G1 · 部署复采验收（最高价值，依赖部署）

**前置**：代码部署到服务器并运行 ≥24h。

**动作**：拉取新 `turn_trace_samples.json` + 同窗 Docker 日志到 `.agent/runtime-observability-<hash>-<date>/`，跑
`PYTHONIOENCODING=utf-8 python scripts/analyze_turn_ledger.py <file>`，逐项对照 `baseline-audit-20260727.md`。

**验收矩阵**（基线 → 目标）：

| OPT | 指标 | 基线 | 目标 |
|---|---|---|---|
| 01 | stale 原因中 `unknown_thread` 占比 | 100%（7/7） | ≈0，且出现 `same_thread`/`other_thread_ignored` |
| 01 | 最活跃群 executed vs stale_drop | 6 vs 7 | stale_drop 显著低于 executed |
| 02 | `instant llm backfill degraded` 日志 | 17 条 | 0 |
| 02 | executed 轮 judge ledger `attempts>=1` | 48% | >95% |
| 03 | 主动消息发出数 | 0 | >0（trace `proactive.is_proactive=true`） |
| 06 | 记忆注入率 | 2.9% | 15-30%（观察 token 成本） |
| 06 | `memory.injection` p95 | 92s | <10s，无深检索 stale_drop |
| 07 | `exhausted=true` 后仍执行主回复的 turn | 存在（420s 事故） | 0 |
| 08 | mood 池调用数 vs executed 轮 | 364 vs 67 | ≈1:1 |
| 08 | judge 池 cached input | 0-25% | 显著 >0 |
| 08 | **judge_outcomes 分布漂移** | — | **<5pp（超出即回退 prompt 重排）** |
| 09 | trace `provider` 字段 unknown 占比 | 100% | ≈0 |
| 11 | executed 轮 `memory_funnel` 缺失 | 64/67 | 0 |

**异常回退开关**（全部已就位，改配置即可）：
`attention.mood_post_judge_enabled` / `private_skip_judge_enabled` / `cognitive_loop_min_think_level`（设 1）/ `memory.think1_semantic_intent_enabled`。

**验收通过后**：观察一周维护日志报告，确认清理范围无误再开 `memory.maintenance_purge_enabled=true`（OPT-05）。

---

## G2 · 数据驱动项（依赖 G1 的线上数据）

| 项 | 内容 | 取证依据 |
|---|---|---|
| **RT-11** 信号量拆分 | 关键路径与后台调用共用全局信号量(3)，skipped 轮 judge elapsed 51.7s vs attempt 数秒 | 已埋 `semaphore_wait_ms`（ledger metadata），看分布决定是否拆分/优先队列 |
| **RT-02 附带** judge 焦点冷却 | 同一 focus 连续 IGNORE 后仍被反复判决（实测 max 10 次/150s） | 中风险（影响群聊唤醒灵敏度），需先看 OPT-08 降本后的真实 judge 调用量 |
| **OPT-14 / PL-10** `_terminated` 闩锁 | 同实例 terminate→initialize 是否静默拒启 | **必须实测**：AstrBot 面板禁用→启用插件（不重启进程），看是否出现 `runtime startup rejected reason=terminated` |
| **ML-08** 污染核查 | dream 晋升事实是否已有幻觉污染 | `SELECT * FROM canonical_memories WHERE source='dream_audit_pipeline' ORDER BY create_time DESC LIMIT 20;` 检查 evidence_turns 是否同 turn 重复 |

---

## G3 · signin 时间窗 flaky（1-2 小时，无依赖）★ 建议第一个做

**问题**：`astrmai/proactive/group_signin_service.py:39` 按 `int(local.tm_hour) == cls.SIGN_HOUR`（8 点）判定，测试未注入时钟 → `tests/test_group_signin_service_refactor.py` 3 个用例只在签到时段能通过。

**影响**：每次全量回归都要 `--ignore` 这个文件，掩盖真实回归风险。

**做法**：给 `_is_sign_hour`（或调用方）注入可测时钟（`now_ts` 参数 / 可覆盖的 `_now()`），测试显式传签到时刻。

**验收**：`python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths"` 无需任何 ignore 即全绿。

---

## G4 · TG-07 vision barrier 并发交织测试（半天，无依赖）

**为什么现在做**：OPT-07 刚改过这个区域（burst deadline 跨迭代持久化），而 4da2910 新增的 gate 侧三条分支至今零测试。

**三条分支**（`gate.py` 排水循环 + `private_turn_coordinator`）：
1. 慢 `prepare_batch` 期间注入新消息 → 第二轮批次含旧+新且不重复发送
2. abort 分支后池非空 → 失败通知只发一次且新消息继续处理
3. 慢 resolver → `outcome=resolve_timeout` 且 downstream 符合 policy

**技术要点**：需 `asyncio.Event` 级同步 harness（现有 gate fixture 的 `prepare_batch` stub 都是瞬时返回，永远命中不到回填分支）；避免 sleep 竞态。

---

## G5 · ID-08 撤回 tombstone（半天，无依赖）

用户撤回的消息仍留在对话上下文，bot 后续可能原文复述（隐私/尴尬）。16h 日志 ≥5 条 `group_recall` notice，插件侧零处理。

**做法**：`message_entry` notice 分类新增 recall 路由 → `group_dialogue_store` 按 event_id 打 tombstone（内容替换 `[已撤回]`、保留 speaker）。**只改展示层内容，不动原始事件存储。**

**验收**：群里发消息→撤回→@bot 询问，bot 不引用原文。

---

## G6 · TL-01 后半：识别信号直接并包（2-4 小时，建议看 G1 数据后做）

OPT-12 只做了 guidance 提示（告诉模型可以调 `bot_capability_lookup` 自检）。后半是"识别到 identity/relationship 疑问信号但关键词未命中时**直接并包**对应工具"，弱化对模型自检的依赖。

**判断依据**：G1 复采看 `disclosure_expanded_packages` 是否由 0 转正——如果 guidance 提示已经生效，这半可以不做。

---

## G7 · WU-06 trace 存储结构迁移（1 天，无依赖但需谨慎）

已做短期缓解（去 indent）。结构迁移目标：整文件 JSON 读写 → append-only JSONL 分片或 SQLite 表。

**难点**：读取端必须同步迁移——`webui` 的 `recent()`（cognition 页 45s 轮询）+ `scripts/analyze_turn_ledger.py`。建议写入端与读取端分两个提交，并保留一次性转换脚本。

**验收**：max_global 填满后单条 append 耗时较基线（0.7s@15MB）降一个量级；消息 p95 延迟无回归。

---

## G8 · OPT-14 / PL-09 重载上下文持久化（半天，设计决策优先）

每次在 AstrBot 面板改配置触发重载 → 所有群丢失热区/温区对话与压缩摘要链（`GroupDialogueStore` 纯内存），在飞回复被 cancel。

**先定策略再动手**：快照 TTL、schema 版本号门槛（不兼容即弃用快照）、写入时机（terminate 钩子）。参照既有先例 `dream_scheduler_state.json` 与私聊 `_persist_pending_sessions`。

---

## 2. 工作纪律（沿用，勿破坏）

1. **先补测试再改代码**；红验证（stash 掉修复看测试变红）是本轮质量保证的核心手段。
2. 每个 OPT 完成后跑全量回归 + 更新对应文档"完成记录"（含改动清单、红验证数据、部署验收清单）+ 同步 README 状态表。
3. **完成一块提交一块**，按互不相交文件集切分保证可单独 revert；`git add/commit/push` 需用户明确授权。
4. 既有测试若锁定的是缺陷行为，更新它并在注释写明"旧断言锁定的正是 XX 缺陷"。
5. Windows 控制台 GBK：分析脚本一律带 `PYTHONIOENCODING=utf-8`。
6. 不顺手修范围外问题；`.agent/final-76-bug-reaudit.md` 判定为"设计行为/不可达"的项不翻案。

## 3. 一句话优先级

**部署验收（G1）> signin flaky（G3）> vision barrier 测试（G4）> 撤回 tombstone（G5）> 其余按数据决定。**
