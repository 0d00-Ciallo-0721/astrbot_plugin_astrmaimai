# Round 10：主动服务、Cron 与生命周期清理

数量：8。依赖：Round 03 completion、Round 08 persistence、Round 09 review。

完成标准：只有真实排队/发送成功才消耗 cooldown；后台任务失败可恢复；Cron revival 不重复创建；辅助索引可清理。

## R10-01 / P3：private session eviction 泄漏 chat-to-user reverse mapping
- 原始 ID：`FFA-09-009`；验证级别：B。
- 主文件：`astrmai/state/private_chat/private_chat_manager.py`。
- 修复边界：session key 与 reverse index 关系显式化；evict/cleanup 同时删除准确 mapping。
- 回归目标：超过 100 session 后 `_sessions` 和 `_chat_to_user` 都有界且无 orphan。

## R10-02 / P2：Heartflow 在 dispatch 前提交 visible candidate cooldown
- 原始 ID：`AM-LP-10-07`；验证级别：B。
- 主文件：`astrmai/proactive/heartflow/manager.py`, `astrmai/proactive/dispatcher.py`。
- 修复边界：queued 或最好 sent 后再 commit cooldown；blocked/exception 必须 rollback/no-op。
- 回归目标：dispatch blocked 后下一周期仍可选 candidate；成功发送才进入 15 分钟 cooldown。

## R10-03 / P2：Diary 在 jitter/处理前标记当天完成，单 chat 失败中止全批
- 原始 ID：`AM-LP-10-08`；验证级别：B。
- 主文件：`astrmai/proactive/proactive_task.py`, `diary_service.py`。
- 修复边界：逐 chat try/continue 和成功 ack；取消/失败保留当日窗口重试，不重复已成功 chat。
- 回归目标：A 失败不阻止 B；jitter 中 shutdown 后重启可继续；成功项不重复。

## R10-04 / P2：Dream 写回/发送失败仍返回 performed 并消耗全局间隔
- 原始 ID：`AM-LP-10-10`；验证级别：B。
- 主文件：`astrmai/proactive/dream_scheduler.py`。
- 修复边界：区分 generated/writeback/sent 状态；可恢复失败进入补偿，只有定义的完成条件推进 interval。
- 回归目标：memory write 或 visible send 失败时结果 degraded/false 且可重试，成功路径只执行一次。

## R10-05 / P2：群签到平台成功但本地状态保存失败仍继续并吞错
- 原始 ID：`AM-LP-10-11`；验证级别：B。
- 主文件：`astrmai/proactive/group_signin_service.py`。
- 修复边界：外部 action 与本地幂等 marker 建立可恢复协议；持久化失败不能报告完整成功或重复 follow-up。
- 回归目标：save 失败+重启不会重复签到/主动消息，诊断能区分 partial success。

## R10-06 / P2：可选 meme 发送失败把已成功主回复改成 executor failure
- 原始 ID：Assignment 11 Finding 3；验证级别：B。
- 主文件：`astrmai/conversation/execution/reply_post_send.py`, `astrmai/multimodal/meme/meme_sender.py`, `reply_service.py`。
- 修复边界：meme 是 best-effort post-send side effect，异常只记录 degraded，不改变主回复 outcome。
- 回归目标：文本已发送后 meme 文件/adapter 抛错，Planner 仍按成功结算且无第二 fallback。

## R10-07 / P2：startup reload transient failure 永久阻止 cron heartbeat 启动
- 原始 ID：Assignment 11 Finding 4；验证级别：B。
- 主文件：`astrmai/app/lifecycle.py`, `astrmai/workmode/cron_guard/heartbeat.py`。
- 修复边界：初始 reload 失败后仍启动具备 per-tick recovery 的 heartbeat，或安排受控重试。
- 回归目标：首次 reload 抛错、第二 tick 成功时 lost jobs 被恢复，无需重启进程。

## R10-08 / P2：Cron revival 先创建 host job，再非原子替换 snapshot identity
- 原始 ID：Assignment 11 Finding 5；验证级别：B。
- 主文件：`astrmai/workmode/cron_guard/heartbeat.py`, `astrmai/infrastructure/persistence/database_cron.py`。
- 修复边界：snapshot swap 事务化并为新 host job 提供失败补偿/幂等 marker；不能每 tick 重建。
- 回归目标：deactivate/save 任一步失败后最多一个 host job，恢复后 snapshot 指向真实 job。
