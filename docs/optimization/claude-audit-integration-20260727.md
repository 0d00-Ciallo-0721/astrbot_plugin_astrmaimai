# Claude 全量审计的核实与融合记录（2026-07-27）

本目录 16 个 OPT 全部来源于 `.agent/claude-full-audit-20260727/` 的只读审计。本文件记录审计方法、主控核实结论与去重决策，供后续质疑任何一条发现时回溯证据链。

## 方法

1. **侦察**（主控）：项目地图、585 traces 结构解析、日志 WARN 指纹统计、历史审计（7-03 复审 76 条 / 7-13 修复 96 单元）读入作为去重基线，产出共享简报。
2. **7 领域并行只读审计**（子代理，各自全读领域源码 + 对 trace/日志写一次性分析脚本）：运行时与模型调用、会话身份与并发、记忆与学习、工具与跨会话、主动/生命周期/配置、WebUI 数据契约、测试缺口。产出 74 条原始发现（每条含证据行号/最小修复边界/验证命令）。
3. **主控逐行复核**：对全部 P0/P1 及抽样 P2 共 **27 条**回读引用源码行核实——**零误报**；其中 2 条在复核中获得加重证据（executor `fatal` 分类只写日志不终止级联；`is_stale_reply_reason` 冒号前缀永不匹配的机制确证）。
4. **跨域去重**：6 条重复合并（见下）；严重度按"用户明显受害/数据损坏且高频=P0"统一校准（ID-01、RT-01 上调）。
5. **重组为 16 个 OPT 工作流**（本目录），68 条修复单元全覆盖、无遗漏无重复（映射清单见各 OPT 文档头部）。

## 关键交叉验证

- **RT-01 = ML-01 = PL-08**：运行时代理与记忆代理从不同入口（`event_bus.py:209` 懒启动 worker / `memory_turn_pipeline.py:174` per-chat worker）独立命中同一 contextvar 泄漏根因，日志 71 条 `turn_deadline_exhausted` 与 17/17 instant backfill 全灭互相咬合 → 合并为 OPT-02 的单一根因修复。
- **PL-01 = ID-02**：身份代理与配置代理分别从 sensors 过滤条件（`sensors.py:317-318`）与合成事件构造（`dispatcher.py:327-351` 无 message 组件）两端取证，14/14 候选 `skipped_sensor_filter` + 两观测窗 0 条主动消息实锤 → OPT-03。
- **judge "已修复"是假象**：7-25 分析称 judge 重复调用已解决，本轮证实是 `analyze_turn_ledger.py:160` 按 stage 匹配（judge 实际记账在 `pool` 字段）导致恒 0；脚本重算真实 p50=1/p95=2/max=10 → OPT-08 降本 + OPT-11 口径修正。
- **两条简报疑点被证伪**（防止误修）："WebUI 读 v1 trace 字段"不成立（前端所读字段 v2 全存在，真问题是 v2 新字段零渲染）；"context_block_stats 511/585 缺失"是假警报（executed 内 67/67 全量，缺失全在 skipped 轮，属预期）。

## 去重决策（74 → 68）

| 被合并 | 并入 | 理由 |
|---|---|---|
| ML-01 | RT-01 | 同一 contextvar 根因（event_bus 侧证据） |
| PL-08 | RT-01 | 同一根因的 instant backfill 受害面 |
| ID-02 | PL-01 | 同一传感器绞杀根因（PL-01 证据面更全） |
| PL-12 | RT-03 | 同一 mood 全量触发（57% 调用占比佐证并入） |
| RT-12 | ML-02 | 同一深检索无预算（react step 层与 deep json 层合并为双层修复） |
| PL-07 | RT-07 | compaction 误配主体并入；provider=unknown 姊妹面归 RT-08 |

## 置信度分层与动手前置条件

- **VERIFIED×62**：代码逻辑闭环或 trace/日志直接命中，可直接进入实施。
- **LIKELY×5**（TL-04、TL-06、RT-11、ML-08、ID-07）：机制成立但缺一环实证，各 OPT 文档已写明取证方法（如 RT-11 需先加 `semaphore_wait_ms` 埋点、ML-08 需先跑 DB 采样 SQL）。
- **NEEDS_RUNTIME_EVIDENCE×1**（PL-10）：需 AstrBot 面板禁用→启用实测是否复用实例。
- 与历史审计关系：KNOWN_OPEN 6 条、KNOWN_FIXED_REGRESSION 3 条（ML-02 为 4417ece 超时集中化后的回归、WU-03 为 R09-04 的副作用、RT-05 为 4da2910 部分修复后的残留缺口），其余 59 条 NEW。

## 证据文件索引

| 位置 | 内容 |
|---|---|
| `../../.agent/claude-full-audit-20260727/findings.json` | 68 条全字段结构化数据（唯一真源） |
| `../../.agent/claude-full-audit-20260727/00_EXECUTIVE_SUMMARY.md` | 执行摘要 |
| `../../.agent/claude-full-audit-20260727/01..07_*.md` | 七份领域报告（含分析脚本输出） |
| `../../.agent/claude-full-audit-20260727/08/09_*.md` | 全字段 backlog 与 7 轮次规划（与 16 OPT 等价的另一种切分） |
| `../../.agent/runtime-observability-c4aee57-20260726/` | 原始 trace/日志证据 |
