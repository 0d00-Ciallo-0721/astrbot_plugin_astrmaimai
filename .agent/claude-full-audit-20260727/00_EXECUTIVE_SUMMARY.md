# 00 执行摘要 — AstrMai 全量只读审计（claude-full-audit-20260727）

> 审计对象: HEAD=4da2910（2026-07-27 vision timeout policy）
> 方法: 7 个领域并行只读审计（源码全读 + 585 条 trace 脚本重算 + 1.3MB 框架日志 / 364KB 诊断日志交叉）→ 主控对全部 P0/P1 及抽样 P2 共 **27 条逐行回读源码验证（零误报）** → 跨域合并 6 条重复 → 输出 68 条独立修复单元。
> 铁律遵守: 全程未修改任何源码/测试/配置/数据库；仅写入本目录与临时目录。
> **执行层入口: `docs/optimization/README.md`（16 个 OPT 工作流，本目录是其证据库）**

## 1. 总量与置信度

| 指标 | 值 |
|---|---|
| 原始发现 | 74 → 去重后 **68** |
| 严重度 | **P0×3 / P1×16 / P2×36 / P3×13** |
| 真实性 | VERIFIED×62 / LIKELY×5 / NEEDS_RUNTIME_EVIDENCE×1 |
| 分类 | 源码确定 BUG×38 / 运行时数据问题×10 / 设计优化×20 |
| 主控逐行复核 | 27 条（全部 P0/P1 + 抽样 P2），无一被推翻 |

## 2. 三个 P0（用户正在受害）

1. **ID-01 群聊在途回复被任意无关新消息击杀**。`gate.py:527-536` 在事件到达时记录活动，但 `astrmai_thread_signature` 要到 prompt 构建阶段（`legacy_compat.py:76/171`）才写入——活动记录的线程签名恒空，`chat_runtime_coordinator.py:447-477` 的整套线程隔离是死代码。实证：最活跃群 16h 内丢弃成品回复 7 条 > 实际发出 6 条，stale 原因清一色 `unknown_thread`。
2. **RT-01 后台任务继承已耗尽的 turn 预算（contextvar 泄漏）**。`event_bus.publish` 懒启动的 3 个常驻 worker 与 `memory_turn_pipeline` 的 per-chat worker 在某轮的 asyncio 上下文里创建，永久携带该轮 deadline；`gateway_call.py:283-289` 每次调用检查预算 → 6 分钟后后台 LLM 全部秒失败。实证：`turn_deadline_exhausted` 日志 71 条、instant 记忆兜底 17/17 全灭、278/539 judge attempt 账本丢失。两个独立代理（运行时/记忆）交叉命中同一根因。
3. **PL-01 主动行为链自 4d16a82 起整体死亡**。dispatcher 构造的合成事件只有 `message_str` 无 message 组件，`sensors.py:317-318` 只认组件文本 → 空消息过滤。wakeup/heartflow/签到跟发三类主动消息**从未发出过一条**（14/14 `skipped_sensor_filter`，两个观测窗 0 成功），且三层诊断全部误标为"planner 拒绝"（PL-02），peer poke 同源全灭（ID-03）。

## 3. 系统性主题（跨领域交叉验证后的结构判断）

1. **"写入膨胀、读取归零"的记忆失衡**：写入端治理（admission/审核/TTL）是扎实的，但读取端三重门（think 门 2.9% 注入率、深检索 50-92s 拖死 turn、后台抽取 100% 死亡）让用户告诉过 bot 的事实几乎读不回来；同时偏好互相覆盖（ML-03）与 FAISS 幽灵向量（ML-04）在静默累积不可逆的数据损耗。
2. **预算体系三态并存**：chat 路径 clamp 常错轮（RT-01）、tool 路径（主回复！）完全不受预算约束（RT-04）、vision 旁路无上限且三层重试相乘 5×3×7（RT-05，单图烧 360s 实锤）。420s 的 turn 7edddd 是三者叠加的完整事故样本。
3. **88% 的 LLM 调用花在最终不回复的消息上**：mood 串行前置且与 judge 内嵌情绪双重计算（RT-03，364 次 vs 67 次回复）、cognitive_loop 在 think1 全量放行（RT-06）、judge prompt 缓存敌对全价计费（RT-09）。"judge 同轮多次调用"并未修复，只是被分析脚本口径 bug 掩盖（RT-02，真实 max=10 次/150s）。
4. **人工校准链条对操作者三连失效**：编辑文本被静默丢弃（WU-01）、权重按增量漂移至 3.0（WU-02）、pending_human 计入徽标却不进队列（WU-03）；治理自愈通道结构性断裂——maintenance.run_once 无任何调度方（WU-04）。
5. **UI 承诺与运行时脱节**：9 个死配置键，含虚假内容安全开关（PL-04）与私聊合并死开关（PL-03，schema 挂错分节被 pydantic 静默丢弃）；越界配置直接拒载整个插件（PL-06）。
6. **观测层三处系统性失真**曾误导既往结论：judge 口径（RT-02）、`skipped by planner` 误标（PL-02）、`context_block_stats 511/585 缺失`是假警报而 `memory_funnel` 缺失是真缺口（TG-04）。

## 4. 简报疑点闭环（侦察阶段 5 个疑点全部定性）

| 疑点 | 结论 |
|---|---|
| ① 私聊 turn 55s、judge 前 14s 空档 | 合并窗+mood/judge/cognitive 五段串行（ID-09，架构性延迟非卡死） |
| ② sent_segment_count 0 vs 2 矛盾 | 纯 instrumentation：满发路径不写 metadata（ID-05），无真实丢段 |
| ③ 私聊 warm summary 说"群聊话题" | `group_dialogue_store.py:352` 硬编码话术（ID-04） |
| ④ judge ledger attempts=0 | RT-01 contextvar 泄漏的账本受害面 |
| ⑤ provider 全量 unknown | 能力解析把模型 ID 当 provider type 匹配（RT-08），cache_control/session 特性形同虚设 |

## 5. 与历史审计的关系

- 7-03 复审确认的 20 缺陷与 7-13 的 96 修复单元中，被抽查项均确认已修；本轮 68 条中 **KNOWN_OPEN 6 条、KNOWN_FIXED_REGRESSION 3 条**（如 ML-02 深检索超时为 4417ece 集中化后的回归、WU-03 为 R09-04 修复的副作用），其余 59 条为 NEW。
- 7-25 的 turn-ledger 分析结论两条被本轮推翻：judge 重复调用"已修复"（假象，RT-02）；"未检测到重复注入"（focus/raw_user 重复对 78 次实存，但量级小）。

## 6. 文件索引

| 文件 | 内容 |
|---|---|
| `01..07_*.md` | 七份领域报告（数据流实测 + 逐条证据） |
| `08_DEDUPLICATED_BACKLOG.md` | 68 条全字段 backlog + 合并记录 |
| `09_RECOMMENDED_EXECUTION_ROUNDS.md` | 7 轮执行计划（每轮 ≤10、含验收门槛） |
| `findings.json` | 结构化全量数据（meta + findings + merged_duplicates） |
| `config_consumption_matrix.md` | 配置项全量落地矩阵（197+12 键） |
| `drafts/` | 各领域代理原始草稿与共享简报 |

> **执行请走 `docs/optimization/`**：68 条已重组为 16 个 OPT 工作流（目标/基线证据/实施步骤/验收标准/风险回退），与 09 号文档的轮次规划一一对应。
