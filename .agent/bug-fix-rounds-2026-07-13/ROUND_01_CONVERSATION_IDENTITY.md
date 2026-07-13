# Round 01：会话入口、身份与线程隔离

数量：9。优先级：最高。依赖：无。

完成标准：私聊普通消息可进入回复链；群聊全链统一使用 UMO；同群不同线程互不覆盖；旧 System2 不能发送。

## R01-01 / P1：普通私聊被永久缓存而不进入 System2

- 原始 ID：`FFA-02-001`, `FF-01`, `FFA-09-002`；验证级别：A。
- 主文件：`astrmai/conversation/attention/gate.py`, `astrmai/state/private_chat/private_chat_manager.py`, `astrmai/conversation/execution/followup_manager.py`。
- 修复边界：`signal_new_message()` 必须区分“已有 waiter 被恢复”和“无 waiter 的新消息”；无 waiter 时走正常 attention，恢复时保留原事件并消费 pending。
- 回归目标：首条 DM、强唤醒后的普通跟进、wait 超时后新消息均只回复一次。

## R01-02 / P1：群聊 raw group ID 导致跨 origin 混池和 mood/energy 双状态

- 原始 ID：`FFA-02-002`, `FFA-09-001`；验证级别：A。
- 主文件：`astrmai/conversation/attention/perception.py` 及 State/Judge 调用点。
- 修复边界：Perception、Attention、Judge、State、cleanup 统一使用 collision-resistant UMO；raw group ID 只作平台元数据。
- 回归目标：两个 adapter 使用相同群号时上下文不串线；同一群只产生一个 ChatState，扣能量后下一轮 Judge 可见。

## R01-03 / P1：立即 engage 同时直派 System2 并把事件留在 accumulation pool

- 原始 ID：`FFA-02-003`；验证级别：B。
- 主文件：`astrmai/conversation/attention/gate.py`。
- 修复边界：立即处理必须原子取得事件所有权，不覆盖无关 pending，也不能被 debounce 再消费。
- 回归目标：force engage、fast wakeup 与已有 debounce 并发时，每个事件恰好执行一次。

## R01-04 / P1：历史 direct wakeup 在 180 秒窗口内持续抢占新消息焦点

- 原始 ID：`FFA-02-004`；验证级别：B。
- 主文件：`astrmai/conversation/attention/focus_selector.py`, `astrmai/conversation/attention/window_buffer.py`, `astrmai/conversation/attention/gate.py`。
- 修复边界：已消费 direct 事件不能在后续 batch 继续获得 direct 巨额权重；保留历史仅作上下文。
- 回归目标：旧 @ 消息处理完成后，新普通消息可成为 focus，且旧消息仍可作为历史上下文。

## R01-05 / P1：generation 前进不取消 stale System2 工作

- 原始 ID：`FFA-02-005`；验证级别：B。
- 主文件：`astrmai/conversation/attention/gate.py`, `astrmai/infrastructure/runtime/chat_runtime_coordinator.py`, `astrmai/conversation/execution/system2_runner.py`。
- 修复边界：新 generation 必须取消或主动失效旧任务，且不能让旧任务长期占有 per-chat admission lock。
- 回归目标：慢旧轮次与快新轮次并发时只允许新轮次进入发送和状态提交。

## R01-06 / P1：poke 与 proactive force-engage 没有 TurnIdentity

- 原始 ID：`FFA-02-006`；验证级别：B。
- 主文件：`astrmai/proactive/dispatcher.py`, `astrmai/conversation/attention/gate.py`, `astrmai/conversation/contracts/turn_identity.py`。
- 修复边界：所有可见 synthetic turn 在进入 System2 前绑定 generation、thread 和 send identity。
- 回归目标：synthetic turn 运行期间出现更新用户消息时，旧主动回复被 freshness 可靠撤销。

## R01-07 / P1：detached System2 异常只记录日志，不进入用户 fallback

- 原始 ID：`FFA-02-007`；验证级别：B。
- 主文件：`astrmai/conversation/attention/gate.py`, `astrmai/conversation/execution/system2_runner.py`, `astrmai/presentation/events/message_entry.py`。
- 修复边界：后台任务异常要回传到该 turn 的统一完成/降级协议，避免 fire-and-forget 静默失败。
- 回归目标：System2 在调度后抛错时，用户得到一次 fallback，proactive callback 也完成释放。

## R01-08 / P2：threaded group wait 注册与查找使用不同 thread identity

- 原始 ID：`FFA-02-008`, `FFA-09-003`；验证级别：A。
- 主文件：`astrmai/state/group_wait/group_reply_wait_manager.py`, `astrmai/conversation/threading/group_thread_resolver.py`, `astrmai/app/plugin_facade.py`。
- 修复边界：turn 绑定、reply 注册、plain/@ 恢复、Reply ID 恢复和取消必须使用同一稳定 thread key。
- 回归目标：同群两个 thread 分别等待时，plain、@、Reply 只恢复各自 thread，不遗留 stale wait。

## R01-09 / P1：ChatLoopKernel 把多 thread wait 压回一个 chat-wide wait

- 原始 ID：`FFA-ENTRY-001`；验证级别：A。
- 主文件：`astrmai/app/plugin_facade.py`, `astrmai/conversation/loop/chat_loop_kernel.py`, `astrmai/conversation/loop/state_store.py`。
- 修复边界：kernel wait API/state 必须携带 thread ID，或明确以 manager 为唯一权威，不能双份状态互相清理。
- 回归目标：thread A resume/expire 不改变 thread B 的 wait、heartbeat 和诊断状态。
