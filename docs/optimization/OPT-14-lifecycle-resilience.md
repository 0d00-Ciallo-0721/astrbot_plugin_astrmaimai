# OPT-14 生命周期与重载韧性

状态：未开始（本轮未动，取证条件不具备） ｜ 优先级：P2 ｜ 依赖：无 ｜ 覆盖发现：PL-09(P2)、PL-10(P3/NEEDS_RUNTIME_EVIDENCE) ｜ 热更新路径本身设计良好（校验前置、组件级联刷新、失败回滚），弱点在重载的副作用。

## 目标

- AstrBot 面板改配置触发插件重载后，bot 不再"突然接不上话"：群对话热区/温区与压缩摘要链跨重载存活（或至少有明确的降级日志）。
- 排除 `_terminated` 永久闩锁风险：同实例 terminate→initialize 场景不会静默拒绝启动。

## 基线证据

- **PL-09**：`group_dialogue_store.py:53-59` 纯内存（dict + asyncio.Lock）无持久化；`chat_runtime_coordinator.py:401-418` terminate 时 cancel 在飞 turn + `_states.clear()`。私聊 pending 有持久化先例（`_persist_pending_sessions`），群对话没有。每次配置保存 = 所有群失忆 + 在飞回复被掐。
- **PL-10**：`lifecycle.py:53-56,307-310` terminate() 置 `_terminated=True` 后无任何复位路径，on_program_start 第一行即拒。是否实际触发取决于 AstrBot enable/disable 是否重建实例——观测期无重载样本，**需实测取证**。

## 实施步骤

1. PL-10 取证先行：AstrBot 面板禁用再启用插件（不重启进程），看日志是否出现 `runtime startup rejected reason=terminated`。若复用实例 → 修复：on_program_start 允许 _terminated 状态下重置标志重启（或 facade 在 initialize 时重建 LifecycleManager）；若每次重建实例 → 降级为文档说明，关闭该项。
2. PL-09：terminate 时把 dialogue_store 热/温区快照写入 cache 目录（对齐 `dream_scheduler_state.json` 的既有做法），启动时按 TTL 恢复；处理 schema 演进（版本号 + 不兼容即弃用快照）。
3. 集成测试：重载前后对同群提问上一分钟话题，断言上下文连续（或降级日志明确）。

## 验收标准

- 改配置触发重载后，对同群提问上一分钟话题 bot 能接上；恢复失败时有一行明确的 WARN 而非静默失忆。
- PL-10 有实测结论记录在案（修复或关闭）。
- 全量 pytest 绿 + 重载连续性集成测试绿。

## 风险与回退

- PL-09 中风险：快照恢复需处理 TTL 过期与 schema 演进；恢复逻辑出错的最坏情形 = 回到现状（失忆），用版本号硬门槛保底。
- 回退：快照写入与恢复各自独立提交，revert 即回纯内存现状。

## 完成记录

本轮未实施。PL-10 需 AstrBot 面板禁用→启用实测取证（NEEDS_RUNTIME_EVIDENCE），PL-09 的对话快照持久化需设计 TTL/schema 演进策略，两者都不适合在无运行环境的批次里推进。建议与下次真实环境验收同批做。
