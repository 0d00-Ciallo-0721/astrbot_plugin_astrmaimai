# AstrMai 最终功能审查：12 份领域报告汇总

更新时间：2026-07-13

## 1. 当前范围

本文件只整理 Sub 01-12 已落盘的生产功能审查结果，不重新初始化项目，
不启动跨模块 Sub，不审查测试、安全策略、代码风格或重构机会。

所有数字均为领域报告的原始统计，尚未完成逐条源码复核与最终去重，
因此不能直接作为最终 Bug 数量。

## 2. 领域报告统计

| ID | 领域 | P0 | P1 | P2 | P3 | 原始合计 |
|---|---|---:|---:|---:|---:|---:|
| 01 | 入口、App、配置 | 0 | 3 | 2 | 0 | 5 |
| 02 | Conversation 入口与 Attention | 0 | 7 | 4 | 0 | 11 |
| 03 | Conversation 决策与规划 | 0 | 4 | 3 | 0 | 7 |
| 04 | Conversation 执行与 Presentation | 0 | 4 | 5 | 0 | 9 |
| 05 | Memory Retrieval / RAG | 0 | 3 | 3 | 0 | 6 |
| 06 | Memory Write / Governance | 0 | 4 | 8 | 1 | 13 |
| 07 | Gateway / Context Economy | 0 | 3 | 6 | 3 | 12 |
| 08 | Runtime / Persistence / Shared | 0 | 1 | 4 | 1 | 6 |
| 09 | State / Sessions | 0 | 2 | 5 | 2 | 9 |
| 10 | Learning / Proactive | 0 | 5 | 9 | 1 | 15 |
| 11 | Multimodal / Workmode | 0 | 1 | 4 | 0 | 5 |
| 12 | WebUI / Plugin Pages | 0 | 1 | 5 | 2 | 8 |
| **原始总计** |  | **0** | **38** | **58** | **10** | **106** |

原始分类为 105 条 confirmed、1 条 partial。01 中 partial 的 EventBus 队列残留
又被 08 独立报告为 confirmed，因此该根因本身需要在最终复核时按 confirmed 处理，
但不能计算两次。

## 3. 明确重复根因

以下项目从标题和调用链描述已经可以确认属于同一根因或同一修复点。
最终报告应合并，不应按领域报告数量重复计数。

| 根因簇 | 涉及原始发现 | 整理意见 |
|---|---|---|
| 普通私聊没有进入有效回复轮次 | `FFA-02-001`、`FF-01`、`FFA-09-002` | 三条合并为一个 P1 根因，分别描述入口、执行和状态侧症状 |
| EventBus stop 后保留旧队列 | `FFA-ENTRY-005`、08 的 EventBus P2 | 合并为一个重载生命周期缺陷 |
| Proactive 无文本/失败路径不执行完成结算 | `FFA-03-004`、`AM-LP-10-13` | 合并为一个 Planner/Executor 完成回调缺陷 |
| 每日 canonical memory decay 调用缺少参数 | `06-03`、`10-05` | 完全重复，保留一个 P1 |
| 用户画像即时 flush 调错持久化签名 | 08 的 profile flush P2、`FFA-09-008` | 合并为一个持久化调用缺陷；严重度需复核 P2/P3 分歧 |
| Wakeup cooldown 持久化参数错误 | 08 的 wakeup persistence P2、`10-06` | 合并为一个重启后主动回复冷却丢失缺陷 |

仅以上明确重复就至少消除 7 个重复计数。其余相似项仍需读取源码后判断，
暂不擅自压缩为同一 Bug。

## 4. 高度相关但不应直接合并

### 4.1 热配置传播不完整

涉及：`FFA-ENTRY-003`、`FFA-03-003`、`AMR-06`、`06-13`、08 的 lane
settings、`FFA-09-004`、`10-12`。

这些共享“顶层 config 已替换，但存活子组件或派生字段仍使用旧值”的系统性模式，
但覆盖 Planner、Memory、Dream、Lane、State、Learning 等不同实例。最终修复可能需要
统一刷新协议，也需要逐组件补齐，不能简单当成一条或七条处理。

### 4.2 群线程等待身份不一致

涉及：`FFA-ENTRY-001`、`FFA-02-008`、`FFA-09-003`。

其中既包含 thread key 注册/查找不一致，也包含回落到 chat-wide wait 的聚合行为。
需要复核是否为一个上游 identity 根因加一个独立调用错误。

### 4.3 外部插件结果进入错误会话链

涉及：`FFA-02-010`、`FF-06`，并与 `FF-07` 的处理顺序相关。

它们分别描述 self-message 过滤、scope 丢失和 result-sniffer 时序，可能是连续链上的
多个独立错误，不能只凭症状相似合并。

### 4.4 显式 fallback 与 AstrBot 默认 LLM 重复处理

涉及：`FFA-ENTRY-004`、`FF-09`。

一个描述 suppress-default 条件，一个描述 fallback yield 后未 stop event。它们可能指向
同一个事件终止根因，也可能是两层均缺保护，需要源码复核。

### 4.5 Stale reply 与历史记录不一致

涉及：`FFA-02-005`、08 的 generation eviction、`FF-03`、`FF-05`。

这些覆盖任务取消、generation 驱逐、发送前 freshness 和分段中途失效。属于同一功能域，
但触发窗口与修复位置不同，暂按多个候选根因保留。

### 4.6 编码损坏文本进入模型或记忆

涉及：`06-11`、`06-12`、`10-15`。

均为 mojibake，但分别进入 Dream 风格、即时记忆、画像/昵称 Prompt。可以统一执行编码
清理，但数据流和生产影响独立。

## 5. P1 风险簇

当前没有任何 Sub 报告 P0，但以下 P1 簇可能直接造成用户可感知故障，应优先人工复核：

1. **私聊不可用**：普通私聊被缓存却不触发 System2。
2. **群聊并发与身份**：raw group ID、UMO、thread ID、generation 和 send claim 不一致。
3. **回复状态不真实**：发送失败后 claim 被占用；旧回复未发送却写入历史。
4. **记忆正确性**：群成员 sender ID 丢失导致事实互相覆盖；BM25 分数塌缩；迁移后 FTS 空索引。
5. **网关结果正确性**：正常文本被误判 provider error；成功后的 trace/lane 失败反向判模型失败；全局超时截断合法工具任务。
6. **持久化并发**：同 lane 并发成功调用以整段历史覆盖，后完成者擦除先完成者。
7. **学习链路不可达或重复消费**：正常聊天不触发 mining；两个调度器并发消费同一 reflection 队列。
8. **Sys3 普通聊天工具不可用**：light tool 以 AstrMessageEvent 调用需要 ContextWrapper 的 SubAgent。
9. **WebUI 与运行态分裂**：画像修改绕过 live cache，后续可能被旧缓存覆盖。

## 6. 当前判断

- 12 个领域均已完成生产代码功能审查。
- 106 是原始发现数，不是最终确认 Bug 数。
- 至少存在 6 个明确重复根因簇，已知可消除至少 7 个重复计数。
- 热配置、群线程、外部结果、fallback、stale reply 五组需要主线程进一步读取源码裁决。
- 在去重与源码复核前，不应直接按 38 个 P1 制定修复任务。
- 第 13 个 Sub 已按用户要求取消，不再消耗项目初始化额度。

## 7. 后续整理顺序

1. 主线程先复核 9 个 P1 风险簇。
2. 再裁决明确重复和“相关但不应直接合并”的项目。
3. 把确认根因按修复文件和依赖顺序分 Wave。
4. 最后生成 `FINAL_REPORT.md`，保留原始 finding ID 到最终 root cause 的映射。

本阶段未修改任何生产代码。
