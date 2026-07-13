# Round 09：学习、反思与人工审核

数量：8。依赖：Round 06-08 的 Memory/Persistence。

完成标准：正常聊天可触发 mining；反思 batch 有唯一所有者和幂等提交；人工审核只发送一次且失败可重试。

## R09-01 / P1：普通用户/Bot 日志从不触发 mining
- 原始 ID：`AM-LP-10-01`；验证级别：B。
- 主文件：`astrmai/learning/evolution_manager.py`, `astrmai/presentation/events/message_entry.py`。
- 修复边界：消费 recorder trigger 结果并调度 per-session mining；复用现有锁，避免每消息重复任务。
- 回归目标：达到阈值后只启动一次 mining，成功后日志处理状态更新。

## R09-02 / P1：两个 lifecycle scheduler 并发消费同一 reflector queue
- 原始 ID：`AM-LP-10-02`；验证级别：B。
- 主文件：`astrmai/app/bootstrap.py`, `astrmai/app/lifecycle.py`, `astrmai/proactive/proactive_task.py`, `astrmai/learning/review/expression_governance_runner.py`, `reflector.py`。
- 修复边界：只保留一个消费者或实现原子 claim-by-ID/ack；不能按当前队首长度删除。
- 回归目标：两个调度入口重叠时同一 item 只处理一次，后续 batch 不被误删。

## R09-03 / P2：reflect batch 部分写入失败导致已成功 delta 重复应用
- 原始 ID：`AM-LP-10-03`；验证级别：B。
- 主文件：`astrmai/learning/review/reflector.py`。
- 修复边界：事务提交或逐项幂等 ack，仅重试失败项；与 R09-02 的 ownership 协议兼容。
- 回归目标：第二项失败重试时第一项 weight 不再变化。

## R09-04 / P1：`pending_human` 继续自动审核并反复发送同一问题
- 原始 ID：`AM-LP-10-04`；验证级别：B。
- 主文件：`astrmai/learning/review/expression_auto_check_task.py`, `reflect_tracker.py`, `astrmai/proactive/review_dispatcher.py`。
- 修复边界：pending_human 从自动 reviewable 集合移除；sent/decision 状态稳定，显式重新排队才重发。
- 回归目标：多轮 governance 期间同一请求只发送一次，人工处理后关闭。

## R09-05 / P2：jargon 先标 active 再 project，投影失败后永不重试
- 原始 ID：`AM-LP-10-09`；验证级别：B。
- 主文件：`astrmai/learning/review/jargon_auto_check_task.py`, `astrmai/memory/services/v2_store.py`。
- 修复边界：使用 pending_projection 状态/补偿队列，或投影成功后再提交 active；保证幂等。
- 回归目标：project 瞬时失败后下一轮可恢复，active 记录必有对应 projection。

## R09-06 / P2：Learning governance 与 DreamScheduler 热配置仍使用旧派生值
- 原始 ID：`AM-LP-10-12`（Memory pipeline 部分归 `R08-01`）；验证级别：B。
- 主文件：`astrmai/learning/evolution_manager.py`, `expression_governance_runner.py`, `astrmai/proactive/proactive_task.py`, `dream_scheduler.py`。
- 修复边界：刷新 runner/auto-check/miner/recorder 与 `_dream_interval`/`dream_visible`；失败回滚全链。
- 回归目标：审核间隔、batch、mining window、Dream 可见性/周期热更后立即一致。

## R09-07 / P2：人工反馈在解析和持久化前就从 pending 队列 pop
- 原始 ID：`AM-LP-10-14`；验证级别：B。
- 主文件：`astrmai/learning/review/reflect_tracker.py`, `astrmai/presentation/events/message_entry.py`。
- 修复边界：parse+persist 成功后 ack；失败保留 request/feedback 或明确 nack/requeue。
- 回归目标：LLM parse/DB update 失败后原人工决定仍可重试，不进入普通聊天误处理。

## R09-08 / P3：画像/昵称模板默认语义字符串 mojibake
- 原始 ID：`AM-LP-10-15`；验证级别：B。
- 主文件：`astrmai/learning/profiling/profile_generator.py`, `nickname_generator.py`。
- 修复边界：修正默认 payload，优先结构化空值；不批量改已有画像数据。
- 回归目标：缺画像/标签/记忆点时 Prompt Registry 输入可读且稳定。
