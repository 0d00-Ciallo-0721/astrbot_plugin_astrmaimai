# OPT-14 生命周期与重载韧性

状态：**已完成**（G4，2026-07-26） ｜ 优先级：P2 ｜ 依赖：无 ｜ 覆盖发现：PL-09(P2)、PL-10(P3/NEEDS_RUNTIME_EVIDENCE) ｜ 热更新路径本身设计良好（校验前置、组件级联刷新、失败回滚），弱点在重载的副作用。

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

**2026-07-26（G4）代码侧完成**：

### PL-10 终止闩锁 —— 按启动来源区分，而非一刀切复位

原计划是"允许 `_terminated` 状态下重新初始化"。实施时发现既有测试
`test_terminated_lifecycle_cannot_be_restarted_by_late_hook` 守护的是**另一个真实场景**：
shutdown 期间迟到的框架 hook 不得复活插件。两者存在真实张力，一刀切复位会破坏后者。

**解法**：把 `main.py` 早已区分的启动来源（`plugin_initialize` / `astrbot_loaded`）
贯通到生命周期层——`main → facade.on_program_start(source=) → startup_hooks → lifecycle`
四层各带默认值保持向后兼容。`_LATCH_RESET_SOURCES = {"plugin_initialize"}`：

- 面板禁用→启用（复用同一实例）→ 复位闩锁并正常启动（PL-10 修复目标）
- 迟到的 `astrbot_loaded` / 无来源 → 维持拒启 + WARN（既有保护不变）

结果：两条既有测试**无需修改**即保持绿，新旧语义共存。

### PL-09 对话上下文快照

**策略三要素**（对齐 `dream_scheduler_state.json` 先例：原子写 tmp+replace、`asyncio.to_thread`、容错吞异常）：

- **TTL**：只导出/恢复 `warm_zone_ttl_seconds` 内的 segment，落盘与恢复**两侧各校验一次**
  （快照静置期间可能过期）；陈旧上下文宁可不要。
- **schema 版本门槛**：`SNAPSHOT_SCHEMA_VERSION = 1`，不匹配整份弃用 + WARN，**不做半解析**
  （避免旧结构恢复出畸形 segment 污染上下文）；文件损坏同样安全返回 0。
- **写入时机**：`terminate` 钩子，且**排在 `coordinator.shutdown()` 之前**——顺序关键，
  否则运行态已被清空。恢复在 `_complete_startup` 开头。
- 容量护栏：最多 64 个 chat × 每 chat 40 条 segment，防止快照文件无界膨胀。
- 开关：`conversation.dialogue_store_persist_enabled`（默认开；关闭时 bootstrap 不传
  `snapshot_dir`，store 内部所有快照方法直接 no-op）。schema + pydantic 双侧登记。

**测试**：`tests/regression/architecture/test_lifecycle_resilience.py` 13 条
（闩锁 3 / 快照 6 / 装配 3 + 与 G3 协同的墓碑跨重载存活 1），**红验证 10 红 → 13 绿**。

**过程记录**：新写的 `restore_snapshot` 里用了 `str(chat_id or "")`，被既有 R11 守卫
`test_resolve_chat_key_exists` 当场抓住（该模式会把 None 强制成空串、让所有 None-chat 共享
同一线程）——改用 `_resolve_chat_key` 统一口径。另有 3 处测试桩因 `source` 新参数需同步更新。

全量回归 **1805 passed, 1 skipped**。

### 交付审计补充（2026-07-26）

在发布候选审计中继续验证了“同一个插件实例被禁用后再启用”的完整服务树，而不只验证
生命周期管理器本身：

- `ChatRuntimeCoordinator.reopen()` 清除关闭闩锁与残留会话状态，允许新消息重新进入。
- `PersonaSummarizer.reopen()` 恢复后台人格任务接收能力。
- `VisualCortex.start()` 可在旧 worker 已结束时重建 worker；`stop()` 会排空尚未处理的图片任务，
  防止禁用前图片在重新启用后迟到注入新会话。
- `CronHeartbeatGuard.start()` 恢复运行标志，避免同实例重启后心跳永久静默。
- 生命周期重启时重新绑定学习系统的 EventBus 订阅，避免 terminate 中解除订阅后无法恢复。

新增回归覆盖同实例 `terminate → initialize`、视觉 worker 重启、视觉待处理队列丢弃、
persona/coordinator/cron 服务复开和学习订阅重绑。交付审计时全量结果为
`1853 passed, 1 skipped, 1 deselected`。
