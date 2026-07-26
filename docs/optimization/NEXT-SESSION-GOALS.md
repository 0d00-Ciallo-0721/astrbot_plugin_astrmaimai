# 接续目标清单（2026-07-26 交接 · 纯代码与测试推进）

> 上一轮成果：全量只读审计（68 条发现）→ 16 个 OPT 工作流 → 13 个 OPT 代码完成、9 个提交落地。
> 本文件是**唯一接续入口**：新会话读完本文即可继续，不需要回溯对话历史。
> **本计划不含部署环节**——所有目标都靠代码实现 + 自动化测试收敛；线上观测类验收由用户另行安排。

## 0. 现状快照

| 项 | 值 |
|---|---|
| HEAD | `b46ce20`（main，领先 origin/main 14 个提交，未 push；push 需用户授权） |
| 全量回归 | `1770 passed, 1 skipped`（排除两项：绝对路径环境检查、signin 时间窗 flaky） |
| 工作区 | 干净（仅 3 项未跟踪的运行时观测原始数据，刻意不入库） |
| 证据层 | `.agent/claude-full-audit-20260727/findings.json`（68 条全字段，含证据行号与最小修复边界） |
| 执行层 | `docs/optimization/README.md`（16 个 OPT 状态表） |
| 基线数字 | `docs/optimization/baseline-audit-20260727.md`（写验收标准时引用，不必再采集） |

## 1. 目标顺序（G1→G9，全部可离线完成）

| 目标 | 内容 | 规模 | 价值 |
|---|---|---|---|
| **G1** | signin 时间窗 flaky（注入可测时钟） | 1-2h | 解锁"零 ignore 全绿回归" |
| **G2** | TG-07 vision barrier 并发交织测试 | 半天 | 护住 OPT-07 刚改的热区 |
| **G3** | ID-08 撤回 tombstone | 半天 | 用户可感知（隐私） |
| **G4** | OPT-14 完整（PL-10 闩锁 + PL-09 重载快照） | 1 天 | 重载失忆是高频体感问题 |
| **G5** | TL-01 后半（识别信号直接并包工具） | 2-4h | 补齐二段披露死路 |
| **G6** | RT-02 附带：judge 焦点冷却 | 半天 | 同 focus 最多判 10 次的浪费 |
| **G7** | RT-11 信号量拆分（关键路径优先） | 半天 | 首响延迟 |
| **G8** | WU-06 trace 存储结构迁移 | 1 天 | 聊天路径隐藏税 |
| **G9** | 收尾：trace 契约测试 + ML-08 核查脚本 + 文档终稿 | 半天 | 防回归与交付完整性 |

---

## G1 · signin 时间窗 flaky ★ 第一个做

**问题**：`astrmai/proactive/group_signin_service.py:39` 用 `int(time.localtime(now_ts).tm_hour) == cls.SIGN_HOUR`（8 点）判定，`tests/test_group_signin_service_refactor.py` 3 个用例未注入时钟，只在真实签到时段能过。

**做法**：给判定路径注入可测时钟（`now_ts` 参数贯通，或提供可覆盖的 `_now()`），测试显式传签到时刻与非签到时刻两组。

**验收**：`PYTHONIOENCODING=utf-8 python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths"` **无需任何 `--ignore` 即全绿**（这条从此成为标准回归命令）。

---

## G2 · TG-07 vision barrier 并发交织测试

**为什么**：4da2910 新增、OPT-07 又改过（burst deadline 跨迭代持久化），三条 gate 侧分支至今零测试。

**三条分支**：
1. 慢 `prepare_batch` 期间注入新消息 → 第二轮批次含旧+新且不重复发送（`gate.py` 排水循环回填分支）
2. abort 分支后池非空 → 失败通知只发一次且新消息继续处理
3. 慢 resolver → `outcome=resolve_timeout` 且 downstream 符合 policy

**技术要点**：需 `asyncio.Event` 级同步 harness（现有 fixture 的 `prepare_batch` stub 瞬时返回，永远命中不到回填分支）；禁用 sleep 竞态写法。顺带断言 OPT-07 的 burst deadline 跨迭代不重置。

---

## G3 · ID-08 撤回 tombstone

**问题**：用户撤回的消息仍留在对话上下文，bot 后续可能原文复述。16h 日志 ≥5 条 `group_recall` notice，插件侧零处理。

**做法**：`presentation/events/message_entry.py` 的 notice 分类新增 recall 路由 → `group_dialogue_store` 按 event_id 打 tombstone（内容替换 `[已撤回]`、保留 speaker 与时序）。**只改展示层内容，不动原始事件存储。**

**验收**：单测——store 内已有消息，收到对应 recall notice 后热区渲染出 `[已撤回]` 且原文不再出现在 warm/focus 任何预览字段。

---

## G4 · OPT-14 完整（重载韧性）

**PL-10 `_terminated` 永久闩锁**：`app/lifecycle.py:53-56,307-310` 置位后无复位路径，`on_program_start` 首行即拒。原判定为 NEEDS_RUNTIME_EVIDENCE，但**代码层可直接做成幂等安全**：允许 `_terminated` 状态下重新初始化（或 facade 在 initialize 时重建 LifecycleManager）。单测：同实例 terminate→initialize 后消息仍被处理。

**PL-09 重载上下文失忆**：`GroupDialogueStore` 纯内存，面板改配置触发重载即丢全部群热/温区与压缩摘要链。
**先定策略再动手**：快照 TTL、schema 版本号门槛（不兼容即弃用）、写入时机（terminate 钩子）、落盘位置对齐既有先例 `dream_scheduler_state.json` 与私聊 `_persist_pending_sessions`。
单测：写入→模拟重载→恢复后热区连续；schema 版本不符时安全弃用并 WARN。

---

## G5 · TL-01 后半：识别信号直接并包

OPT-12 只做了 guidance 提示（告知模型可调 `bot_capability_lookup` 自检）。后半是"识别到 identity/relationship 疑问信号但关键词未命中时**直接并包**对应只读工具"，弱化对模型自检的依赖。

**做法**：`planner_side_inputs._build_execution_tools` 复用 `QueryIntentClassifier`（OPT-06 已在 prompt_refiner 引入同款判定），identity/relationship 意图直接并入对应包。全部只读工具，风险有限。
**验收**：单测——"你还记得我是谁吗"类问句在关键词未命中时 filtered_tools 含 identity 查询工具。

---

## G6 · RT-02 附带：judge 焦点冷却

**问题**：`gate.py:1509-1510` 把 IGNORE 的 focus event 放回 window，下一批仍可能选中同一事件重复判决（实测同 focus 150s 内判 10 次，judge 池 539 次调用中 521 次花在最终被忽略的消息上）。

**做法（保守）**：对连续被 IGNORE 的 focus 做降权或短冷却，**做成配置开关**（默认开、可关），避免影响群聊唤醒灵敏度。
**验收**：单测——同一 focus 连续两次 IGNORE 后，第三批不再选中它；有新消息或强唤醒时立即恢复。

---

## G7 · RT-11 信号量拆分

**问题**：`gateway_call.py:194` 关键路径与后台调用共用全局信号量(3)，skipped 轮 judge ledger elapsed 51.7s 而 attempt 仅数秒（排队证据）。OPT-08 已埋 `semaphore_wait_ms` 观测点。

**做法**：`model_gateway` 拆分信号量（critical_path 独立配额或优先队列），**总并发不超 provider 限额**（原设计目的是 429 保护，不能破坏）。
**验收**：并发压力单测——关键路径调用在后台调用占满槽位时仍能及时获得配额；`semaphore_wait_ms` 在关键路径上趋近 0。

---

## G8 · WU-06 trace 存储结构迁移

已做短期缓解（去 indent）。目标：整文件 JSON 读写 → append-only JSONL 分片或 SQLite 表。

**难点**：读取端必须同步迁移——`webui` 的 `recent()`（cognition 页 45s 轮询）与 `scripts/analyze_turn_ledger.py`。
**做法**：写入端与读取端分两个提交；提供一次性格式转换脚本；保留对旧格式文件的读取兼容（`.agent/runtime-observability-*` 里的历史快照仍要能被分析脚本读）。
**验收**：压测单测——填满 max_global 后单条 append 耗时较基线（0.7s@15MB）降一个量级；分析脚本对新旧两种格式都能解析。

---

## G9 · 收尾

1. **trace 契约测试**（OPT-11 步骤 2 遗留）：executed trace 必含 `llm_call_ledger`/`stage_ledger`/`context_block_stats`/`memory_funnel` 非空——本轮修了 funnel 但没加锚定测试。
2. **ML-08 核查脚本**：写一次性 SQL 核查脚本（检查 `source='dream_audit_pipeline'` 的 evidence_turns 是否同 turn 重复），**只提交脚本不执行**，供用户在有库的环境自行采样。
3. **文档终稿**：各 OPT 完成记录补齐、README 状态表全部转"已完成/已关闭"、`baseline-audit-20260727.md` 标注"口径变更切换点"（judge 计数口径、total 口径、trace 序列化格式在本轮均已变化，新旧报表数字不可直接比较）。
4. **最终全量回归** + 提交（提交需用户授权）。

---

## 2. 工作纪律（沿用，勿破坏）

1. **先补测试再改代码**；**红验证**（stash 掉修复看测试精确变红）是本轮质量保证的核心手段，每个目标都要做。
2. 每个目标完成后：跑全量回归 → 更新对应 OPT 文档"完成记录"（改动清单 + 红验证数据 + 决策记录）→ 同步 README 状态表。
3. **完成一块提交一块**，按互不相交文件集切分保证可单独 revert；`git add/commit/push` 需用户明确授权。
4. 既有测试若锁定的是缺陷行为，更新它并在注释写明"旧断言锁定的正是 XX 缺陷"（本轮已有 11 处先例）。
5. 行为变更一律带配置开关，默认值取保守侧；开关同时加进 `_conf_schema.json`（纯中文 hint，有 `test_schema_display_text_is_readable_chinese` 守卫）与 pydantic（有 `SchemaPydanticContractTests` 守卫）。
6. Windows 控制台 GBK：分析脚本一律带 `PYTHONIOENCODING=utf-8`。
7. 不顺手修范围外问题；`.agent/final-76-bug-reaudit.md` 判定为"设计行为/不可达"的项不翻案。

## 3. 已知不在本计划内

- **线上观测类验收**（注入率、mood 调用量、judge A/B 漂移等 13 项指标）：需真实运行数据，由用户自行安排；验收矩阵保留在各 OPT 文档"完成记录"末尾备查。
- **`memory.maintenance_purge_enabled`**：默认关闭，需真实库观察维护日志后由用户手动开启。
