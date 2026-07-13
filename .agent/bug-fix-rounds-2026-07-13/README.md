# AstrMai 功能缺陷修复批次索引

生成日期：2026-07-13

基线：`.agent/final-functional-audit/` 下 12 份领域报告

范围：生产功能与模块协作；不包含测试覆盖率、安全、代码风格和纯重构建议

## 结论

- 原始发现：106 条。
- 明确重复计数：10 条。
- 独立修复单元：96 个。
- 排除为误判：0 条。
- 批次：11 轮，每轮 8-9 个，均不超过 10 个。
- 生产代码：本目录生成过程未修改任何生产代码。

这里的“未排除误判”不表示所有问题都同等严重。保留条件是：报告提供了真实生产入口、触发条件、调用链、实际行为和源码锚点；主线程又完成了重复簇源码核对、路径有效性检查和跨报告归因。条件型问题仍保留，但必须在对应批次先补最小复现，再进入实现。

## 执行顺序

| 轮次 | 类别 | 数量 | 主要目标 |
|---|---|---:|---|
| 01 | 会话入口、身份与线程隔离 | 9 | 先恢复私聊、群聊身份和线程等待正确性 |
| 02 | 入口生命周期、外部结果与错误终止 | 9 | 清理跨插件结果、后台任务和 fallback 终止语义 |
| 03 | 决策、规划与实际发送状态 | 9 | 让 Planner 只提交真实执行/发送结果 |
| 04 | 回复、Sys3 与 Gateway 边界 | 9 | 修复 follow-up、工具分派和 tool-loop 基础可靠性 |
| 05 | Gateway、Compaction 与 Lane 并发 | 9 | 隔离成功后副作用、超时、统计和历史并发 |
| 06 | 记忆检索、RAG 与升级迁移 | 9 | 修复召回排序、候选池、迁移和主体身份 |
| 07 | 记忆治理、Persona 与 Dream | 9 | 修复写入门控、治理协议、后台任务和乱码数据 |
| 08 | 配置、状态与持久化一致性 | 9 | 修复热更新、状态锁、持久化签名和重载隔离 |
| 09 | 学习、反思与人工审核 | 8 | 恢复 mining，保证反思和审核不重放、不丢失 |
| 10 | 主动服务、Cron 与生命周期清理 | 8 | 修复 cooldown、日记、Dream、签到和恢复幂等性 |
| 11 | WebUI 与运行时数据契约 | 8 | 修复页面与真实运行缓存、API、分页和字段契约 |

## 执行规则

1. 严格按轮次推进；同一轮先补最小失败测试，再做局部修复。
2. 每个修复 ID 独立提交或在同一根因下合并提交；不得把相邻但不同根因顺手重构。
3. 每轮完成后运行该轮相关测试、`python -m compileall` 和 `git diff --check`。
4. 修复热配置类问题时，必须同时验证成功应用、重复应用幂等和失败回滚。
5. 修复并发类问题时，必须验证旧任务、当前任务、取消、重载和发送失败路径。
6. 原始报告 ID 到修复 ID 的去重映射见 `00_DEDUP_AND_VALIDATION.md`。

## 文件

- `00_DEDUP_AND_VALIDATION.md`
- `ROUND_01_CONVERSATION_IDENTITY.md`
- `ROUND_02_INGRESS_EXTERNAL_ERRORS.md`
- `ROUND_03_PLANNING_SEND_STATE.md`
- `ROUND_04_REPLY_SYS3_GATEWAY.md`
- `ROUND_05_GATEWAY_COMPACTION_LANES.md`
- `ROUND_06_MEMORY_RETRIEVAL_MIGRATION.md`
- `ROUND_07_MEMORY_GOVERNANCE_PERSONA_DREAM.md`
- `ROUND_08_CONFIG_STATE_PERSISTENCE.md`
- `ROUND_09_LEARNING_REVIEW.md`
- `ROUND_10_PROACTIVE_CRON_LIFECYCLE.md`
- `ROUND_11_WEBUI_RUNTIME_CONTRACTS.md`
