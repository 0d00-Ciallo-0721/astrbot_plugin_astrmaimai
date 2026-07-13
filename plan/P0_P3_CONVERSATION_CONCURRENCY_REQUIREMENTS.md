# AstrMai 群聊并发隔离与私聊连续轮次治理需求文档

## 0. 文档目标

本文档定义 AstrMai 后续并发治理的需求边界、分阶段目标、验收标准与测试要求。目标不是重构整条对话链路，而是在尽量保持现有群聊/私聊产品语义不变的前提下，解决真实 QQ 环境中暴露出的以下问题：

- 旧模型回复迟到后仍然发送，污染新一轮对话。
- 同一轮 final 回复可能被多个异步入口重复发送。
- 群聊中同一 `chat_id` 下多用户、多话题、等待恢复状态互相干扰。
- 私聊中同一用户快速连续输入时，旧回复可能串到新消息之后。
- 测试/控制/观测消息进入正常对话链路，触发误唤醒、误推进状态或误写历史。

本文档按 P0-P3 分阶段描述。P0 必须是最小可执行闭环，优先封住最危险的线上问题；P1 再治理同群多线程；P2 增强观测、压测和灰度开关；P3 才考虑长期架构收敛。

## 1. 当前事实基础

### 1.1 入口链路

当前所有普通消息进入：

```text
main.py
  -> AstrMaiPlugin.on_global_message
  -> PluginFacade.on_global_message
  -> presentation/events/message_entry.py::handle_global_message
```

入口会依次执行：

- 消息去重。
- self message 过滤。
- poke 事件处理。
- 框架命令过滤。
- 权限检查。
- GroupReplyWait 处理。
- 用户活跃度追踪。
- 反思反馈消费。
- direct call 判断。
- `record_and_dispatch_attention()` 分发到 ChatLoop 或 AttentionGate。
- 根据 `ENGAGED` / direct call 抑制 AstrBot 默认 LLM。

### 1.2 群聊现状

群聊识别依赖 `event.get_group_id()`，`chat_id` 通常是：

```text
default:GroupMessage:<group_id>
```

当前已有的隔离：

- 不同群天然不同 `chat_id`。
- `ChatRuntimeCoordinator` 为每个 `chat_id` 提供 `sys2_lock`。
- `Executor` 有 per-chat `executor_lock` 与 pending 限制。
- `AttentionGate` 有 per-chat session 聚合和 debounce。
- `ChatLoopKernel` 有 per-chat loop state。
- `GroupReplyWaitManager` 有群聊等待恢复状态。

当前不足：

- `GroupReplyWaitManager` 目前以 `chat_id` 为主键，每个群同一时刻只有一个 wait state。
- 同群不同用户、不同话题没有强隔离。
- `sender_id == target_user_id` 在部分情况下会恢复旧 wait，容易被同用户后续普通消息误触发。
- 旧模型回复返回时，发送端缺少“是否仍属于当前轮”的硬校验。
- 多个异步入口可能对同一轮产生重复 final。

### 1.3 私聊现状

私聊识别通常是无 `group_id`，`chat_id` 通常是：

```text
default:FriendMessage:<user_id>
```

私聊特点：

- 私聊不进入 `GroupReplyWaitManager`。
- 私聊靠 `PrivateChatManager` 管理续聊等待。
- `PrivateChatManager` 维护 `pending_messages`、`new_message_event`、`is_bot_waiting`、`turn_count`、`last_message_time`。
- `Judge` 对 `FriendMessage` 有强回复兜底：WAIT/IGNORE 会被改成 REPLY。

当前不足：

- 同一私聊用户快速连续发消息时，旧模型回复可能迟到发送。
- 私聊等待期新消息必须唤醒 wait，但也必须让旧生成失效。
- 私聊 freshness 不能只用“超时多久”判断，更应该判断“是否仍属于当前 generation”。

### 1.4 真实 QQ 测试暴露的问题

真实测试中出现：

- 测试控制消息 `[AMTEST]`、`amt_probe bind` 被 AstrMai 入口观察到。
- GroupWait 看到测试 orchestrator 的状态消息后可能恢复或维持等待。
- 召回测试实际回复包含正确 token，但断言模板未展开导致假失败。
- 情感测试阶段收到上一轮召回的迟到回复，说明存在旧回复跨轮发送。
- 同一业务场景可能出现多条后续回复，说明发送端缺少 exactly-once final 闸门。

结论：业务能力可用，但并发隔离和发送端幂等需要补 P0 防线。

## 2. 总体设计原则

### 2.1 不改变现有聊天决策语义

P0 不改变以下行为：

- 群聊是否回复仍由现有 Attention/Judge/Planner 决策。
- 私聊仍保持强回复语义。
- 私聊 WAIT/IGNORE -> REPLY 兜底仍保留。
- GroupWait 主结构暂不重构。
- 不取消正在进行中的模型请求。
- 不引入完整 ConversationModePolicy 抽象层。

P0 只新增：

- turn generation。
- stale generation check。
- exactly-once send claim。
- 私聊 pending 与 generation 对齐。
- 非对话控制消息隔离标记。

### 2.2 群聊与私聊分开治理

群聊目标：

```text
chat_id + thread_id + generation + send lease
```

私聊目标：

```text
chat_id + private_session + generation + send lease
```

P0 中：

- 群聊 `thread_id` 可以暂时等于 `chat_id`。
- 私聊 `thread_id` 固定为 `private:<chat_id>`。

P1 中：

- 群聊再升级为真实 thread resolver。
- 私聊仍不接入 GroupWait。

### 2.3 发送端是最终硬闸

Planner/Judge/Attention 都可能只是中间层。最终必须在 ReplyService 发送前做硬校验：

- 当前 reply 是否仍属于当前 generation。
- 当前 generation 是否已经发送过 final。
- 若过期或重复，直接不发送，并记录可观测日志。

这条规则必须覆盖：

- 普通 System2 回复。
- fallback 回复。
- follow-up 回复。
- heartbeat 或 resume 触发的回复。
- 分段回复。

### 2.4 私聊可以慢，但不能错轮

私聊允许模型慢一些，只要期间没有新的有效用户消息。新的私聊用户消息到达后：

- 新消息必须进入 `pending_messages`。
- 必须唤醒 `new_message_event`。
- 必须推进 generation。
- 旧 generation 的模型结果发送前必须失败。

### 2.5 控制消息不能污染对话

测试/控制/观测消息不应：

- 推进 generation。
- 写入对话历史。
- 进入 Attention/Judge/Planner。
- 触发 GroupWait 恢复。
- 唤醒 PrivateChatManager。
- 影响 freshness。

推荐通用标记：

```text
event.extra["astrmai_non_conversational"] = True
```

AstrMai 主链路只识别通用标记，不应硬编码 `[AMTEST]`、`amt_probe` 等具体测试前缀。测试插件负责打标或在更早入口拦截。

## 3. 核心概念

### 3.1 TurnIdentity

P0 引入轻量 turn 标识，不上完整策略层。

建议结构：

```python
@dataclass(frozen=True)
class TurnIdentity:
    mode: Literal["group", "private"]
    chat_id: str
    thread_id: str
    generation: int
    sender_id: str | None = None
    input_message_ids: tuple[str, ...] = ()
    created_at: float = 0.0
```

要求：

- 可以作为 dataclass 放在新模块，或先作为轻量 dict/event extra 承载。
- P0 不强制修改所有函数签名，可通过 event extras 向下传递。
- 发送前必须能从 event 或 reply context 取回 turn。

事件 extra 建议：

```text
astrmai_turn_identity
astrmai_turn_mode
astrmai_turn_thread_id
astrmai_turn_generation
astrmai_turn_created_at
```

### 3.2 ThreadId

P0 规则：

```text
private: thread_id = "private:<chat_id>"
group:   thread_id = "<chat_id>"
```

P1 规则：

```text
group thread_id = root_message_id / reply_to / thread_signature / focus thread resolver
```

P0 不解决同群多线程并行，只先解决同群旧回复迟到和重复 final。

### 3.3 Generation

generation 是 `chat_id + thread_id` 维度的递增整数。

只有有效对话消息才推进 generation。有效消息必须至少满足：

- 去重通过。
- 非机器人自己发的消息。
- 非框架命令。
- 权限通过。
- 非 `astrmai_non_conversational`。
- 确实会进入对话链路。

不应推进 generation 的消息：

- self message。
- duplicate message。
- 框架命令。
- 被权限拒绝的消息。
- AMTEST/Probe/Trace/状态类控制消息。
- 只供其他插件消费的观测消息。
- group notice、membership notice 等非普通对话事件。

### 3.4 SendClaim

send claim 是发送端 exactly-once 闸门。

建议 key：

```text
<mode>:<chat_id>:<thread_id>:<generation>:<response_kind>
```

P0 只要求 `response_kind="final"`。

状态建议：

```text
claimed
committed
failed
```

语义：

- `claim_send()` 成功后才允许发送。
- 同 key 第二次 claim 必须失败。
- 分段回复属于同一个 final，不能每段 claim 一次。
- `commit_send()` 记录已发出的 outbound message ids。
- `mark_send_failed()` 记录失败，P0 可不自动重试。

## 4. P0 需求：最小并发防线

### 4.1 P0 目标

P0 必须解决：

- 旧 generation 回复迟到后仍发送。
- 同 generation final 重复发送。
- 私聊等待期新消息只唤醒 wait 但不推进 generation。
- 控制消息推进 generation 或进入对话链路。

P0 不解决：

- 同群多 thread 并存。
- GroupWait per-thread。
- 模型请求 cancel。
- 全链路 policy 抽象。
- 跨进程/多实例分布式 CAS。

### 4.2 P0-1 RuntimeCoordinator 增加 generation API

建议在 `ChatRuntimeCoordinator` 上新增 async API，以复用现有 `asyncio.Lock`：

```python
async def advance_generation(self, chat_id: str, thread_id: str) -> int: ...
async def current_generation(self, chat_id: str, thread_id: str) -> int: ...
async def is_current_turn(self, turn: TurnIdentity) -> bool: ...
```

内部状态建议：

```python
turn_generations: dict[tuple[str, str], int]
```

要求：

- 缺省 generation 为 0。
- 第一次有效消息 advance 后返回 1。
- 所有访问都受 coordinator 锁保护。
- `clear_runtime_state(chat_id)` 必须清理该 chat 下相关 generations 与 send claims。
- `prune_inactive()` 可按 chat 清理相关 generation/send state。

### 4.3 P0-2 RuntimeCoordinator 增加 send claim API

建议新增：

```python
async def claim_send(self, send_key: str) -> bool: ...
async def commit_send(self, send_key: str, outbound_message_ids: list[str]) -> None: ...
async def mark_send_failed(self, send_key: str, error: Exception | str) -> None: ...
async def get_send_claim(self, send_key: str) -> dict | None: ...
```

内部状态建议：

```python
send_claims: dict[str, SendClaimState]
```

`SendClaimState` 至少包含：

- `status`
- `created_at`
- `updated_at`
- `outbound_message_ids`
- `error`

要求：

- 已 claimed/committed 的 key 再 claim 返回 False。
- failed 的 key P0 默认也返回 False，避免自动重发导致重复；如需重试，P1 再设计。
- `commit_send` 幂等，可重复写相同 ids。
- `mark_send_failed` 不抛出异常影响主链路。

### 4.4 P0-3 有效消息创建 TurnIdentity

建议位置：`message_entry.handle_global_message()` 通过所有早期过滤后、进入 `record_and_dispatch_attention()` 前。

必须在以下之后：

- dedupe。
- self message。
- poke stop。
- framework command stop。
- permission guard。
- non conversational guard。

必须在以下之前：

- `record_and_dispatch_attention()`。
- `AttentionGate.process_event()`。
- `ChatLoopKernel.tick()`。
- `System2Runner.run()`。

原因：后续所有模型任务都要绑定本次 turn。

伪流程：

```text
scope = MessageScope.from_event(event)
...
if should_enter_conversation:
    mode = "private" if scope.is_private_chat else "group"
    thread_id = resolve_p0_thread_id(scope)
    generation = await runtime_coordinator.advance_generation(scope.chat_id, thread_id)
    turn = TurnIdentity(...)
    event.set_extra("astrmai_turn_identity", turn)
```

### 4.5 P0-4 non conversational 消息隔离

入口应先识别：

```text
event.get_extra("astrmai_non_conversational", False)
```

如果为 True：

- 不推进 generation。
- 不进入 `record_and_dispatch_attention()`。
- 不触发 GroupWait。
- 不触发 PrivateChatManager。
- 不写入 evolution/message log。
- 不更新 relationship/mood。
- 不触发默认 LLM。

是否 `event.stop_event()` 取决于该消息是否还要留给其他插件。P0 建议：

- 如果已经明确是 AstrMai 测试/控制消息，测试插件自身应 stop。
- AstrMai 主链路看到标记后直接 return，不主动生成回复。

### 4.6 P0-5 ReplyService 发送前 stale check

所有 final reply 发送前必须：

1. 取出 `TurnIdentity`。
2. 若不存在 turn，则保持旧行为，但记录 debug，避免一次性破坏历史路径。
3. 若存在 turn，调用 `is_current_turn(turn)`。
4. 若不是当前 turn，直接不发送。

阻断时记录：

```text
blocked_reason=stale_generation
chat_id
thread_id
turn_generation
current_generation
message_id/input ids
```

要求：

- 不向用户发送 fallback。
- 不抛出异常。
- 不标记为系统错误。
- 可记录 observability trace。

### 4.7 P0-6 ReplyService final send claim

stale check 通过后，再 claim：

```text
send_key = <mode>:<chat_id>:<thread_id>:<generation>:final
```

若 claim 失败：

- 不发送。
- 记录 `blocked_reason=send_claim_exists`。
- 不发送 fallback。
- 不抛异常。

若 claim 成功：

- 发送所有 segment。
- 记录 outbound message ids。
- 全部发送完成后 commit。
- 发送失败时 mark failed。

分段规则：

- 同一 final 的所有分段共享一个 send claim。
- 不允许每个 segment 单独 claim。
- 如果发送过程中 freshness 变化，P0 可不在每段前重复检查；更严格版本可在每段前检查当前 generation，但要避免半段已发造成体验割裂。推荐 P0 在 claim 前检查一次，P1 再讨论 per-segment 中断。

### 4.8 P0-7 私聊 pending 与 generation 对齐

私聊有效消息进入时：

- 推进 private thread generation。
- 创建 TurnIdentity。
- 如果处于 private wait，仍调用 `PrivateChatManager.signal_new_message()`。
- `signal_new_message()` 必须 append pending message。
- 如果 `is_bot_waiting=True`，必须 set event。

要求：

- 不得因为 generation 机制跳过 `signal_new_message()`。
- 不得因为处于 private wait 而不推进 generation。
- 不得把私聊消息送进 GroupWait。
- 当前新 generation 仍保持私聊强回复语义。

### 4.9 P0-8 私聊 freshness 调整

现有 `_allow_direct_reply_timeout()` 中，私聊被视为 direct engagement。P0 增加 generation 后，私聊 overdue reply 放行必须满足：

- turn 存在且仍 current。
- 没有更新 generation。
- final 未发送。

如果 turn 不存在，先保持旧行为，避免一次性破坏历史兼容。

### 4.10 P0-9 群聊保守 generation

P0 群聊 `thread_id=chat_id`，因此同群任何新的有效对话消息都会让旧群聊 generation 过期。

优点：

- 可以阻止旧群回复迟到串入新群聊上下文。
- 可以阻止同群重复 final。

代价：

- 用户 A 的慢回复可能被用户 B 的新消息打断。
- 同群多话题并行不能保留。

这是 P0 可接受的保守行为，P1 用 per-thread 改善。

## 5. P0 验收标准

P0 完成后必须满足：

- 私聊用户快速连续输入，旧 generation 回复不发送。
- 私聊等待期用户继续输入，`pending_messages` 被写入，wait 被唤醒，generation 被推进。
- 私聊 WAIT/IGNORE 仍会强制 REPLY。
- 同一 turn 的 final 只发送一次。
- 分段回复不会每段生成独立 final claim。
- 控制消息不推进 generation。
- 控制消息不进入 Attention/Judge/Planner。
- 群聊中后到有效消息会让旧 generation 回复过期。
- 无 turn 的历史路径仍可运行，不因缺失 turn 崩溃。

## 6. P0 测试需求

### 6.1 私聊旧回复迟到

场景：

```text
用户私聊：问题 A
模型开始生成，阻塞
用户私聊：补充 B
generation +1
旧模型返回
```

期望：

- A 的旧回复不发送。
- 日志包含 `stale_generation`。
- B 的新 turn 可以继续生成回复。

### 6.2 私聊等待期续聊

场景：

```text
bot 发送回复后进入 private_wait
用户继续发消息
```

期望：

- `PrivateChatManager.pending_messages` 增加。
- `new_message_event` 被 set。
- generation +1。
- private wait 被唤醒。
- 旧 final 不重复发送。

### 6.3 私聊强回复保留

场景：

```text
FriendMessage
Judge 返回 WAIT 或 IGNORE
```

期望：

- 仍被改成 REPLY。
- 当前 generation 允许发送。

### 6.4 群聊重复 final

场景：

```text
同一 turn 被 message tick 和 heartbeat/resume 同时触发发送
```

期望：

- 第一次 claim 成功。
- 第二次 claim 失败。
- 只发送一组 final。
- 日志包含 `send_claim_exists`。

### 6.5 控制消息不推进 generation

场景：

```text
event.extra["astrmai_non_conversational"] = True
message="[AMTEST] ..."
```

期望：

- generation 不变。
- 不进入 Attention。
- 不写入 history/evolution。
- 不触发 GroupWait/PrivateChatManager。

### 6.6 群聊旧回复过期

场景：

```text
群里用户 A @bot
模型慢
群里出现新的有效对话消息
旧模型返回
```

期望：

- P0 中旧回复不发送。
- 日志包含 `stale_generation`。

### 6.7 分段回复共享 claim

场景：

```text
同一 final 被切成 2-3 段
```

期望：

- 只 claim 一次。
- commit 记录多个 outbound ids。
- 不因第二段 claim 失败导致回复截断。

## 7. P1 需求：群聊多线程隔离

### 7.1 P1 目标

P1 解决 P0 的保守代价：

- 同群用户 A 的慢回复不应被用户 B 无关消息无条件打断。
- 同群多话题应能拥有不同 wait state。
- GroupWait 恢复必须绑定具体线程、目标和机器人发出的消息。

### 7.2 GroupWait per-thread

当前结构：

```python
_states: Dict[str, GroupReplyWaitState]
```

目标结构：

```python
_states: Dict[str, Dict[str, GroupReplyWaitState]]
# chat_id -> thread_id -> wait_state
```

每个 wait state 至少绑定：

- `chat_id`
- `thread_id`
- `target_user_id`
- `target_name`
- `source_user_id`
- `thread_signature`
- `root_event_identity`
- `outbound_message_ids`
- `turn_generation`
- `created_at`
- `expires_at`
- `remaining_messages`

### 7.3 群聊 thread resolver

P1 引入群聊 thread_id 解析。优先级建议：

1. 用户回复 bot 的具体 outbound message id。
2. 用户回复某条 root message。
3. event 中已有 `thread_signature`。
4. FocusThread 识别出的主线 signature。
5. direct @ bot 且无上下文时使用新 root thread。
6. fallback 使用 `chat_id`。

要求：

- thread resolver 必须可测试。
- 不能在 P1 中破坏 P0 的 generation/send claim。
- 解析失败时必须降级到 P0 行为。

### 7.4 GroupWait 恢复条件收紧

P1 后，恢复 wait 必须满足至少一个强条件：

- reply_to 命中 bot 之前发出的 outbound message id。
- root message id 命中 wait state。
- thread_signature 匹配。
- 明确 @ bot 且 sender 是 target user。
- 明确 direct wakeup 且 thread resolver 命中同 thread。

不应单独作为恢复条件：

```text
sender_id == state.target_user_id
```

同一用户普通新消息应进入新的 attention/judge，而不是强制恢复旧 wait。

### 7.5 同群多 wait state 策略

同一群允许多个 wait state，但必须有上限。

建议：

- 每群最多 active wait state：3-5。
- 超过上限时清理最旧或最低优先级 wait。
- 过期 wait 在读写时清理。
- bot 离群时清理该群所有 wait。

### 7.6 群聊 generation 升级

P1 中 generation 从：

```text
chat_id + chat_id
```

升级为：

```text
chat_id + thread_id
```

效果：

- 同群不同 thread 的新消息不会让彼此 generation 过期。
- 同 thread 新消息仍会让旧回复过期。
- send claim key 继续复用 P0 格式。

## 8. P1 验收标准

P1 完成后必须满足：

- 同群两个用户分别 @bot，可以形成不同 thread。
- thread A 的新消息不会让 thread B 的当前回复过期。
- thread A 的新消息会让 thread A 的旧回复过期。
- GroupWait 可以同时维护多个 thread wait。
- 普通同 target 用户消息不再无条件恢复旧 wait。
- reply_to bot message 可以准确恢复对应 wait。
- 过期 wait 被清理，不无限增长。
- P0 的私聊行为不受影响。

## 9. P1 测试需求

### 9.1 同群双线程并发

场景：

```text
用户 A @bot 问 A1
用户 B @bot 问 B1
两个模型请求交错返回
```

期望：

- A/B 各自 thread generation 独立。
- A 回复不被 B 消息打断。
- B 回复不被 A 消息打断。

### 9.2 同线程 latest-wins

场景：

```text
用户 A @bot 问 A1
用户 A 追加 A2
A1 旧模型返回
```

期望：

- A1 旧回复不发送。
- A2 新回复可以发送。

### 9.3 GroupWait 强恢复

场景：

```text
bot 回复用户 A 并等待
用户 A reply_to bot 回复
```

期望：

- 命中对应 wait state。
- `RESUME_WAIT` 只恢复该 thread。

### 9.4 GroupWait 弱恢复禁止

场景：

```text
bot 回复用户 A 并等待
用户 A 未 reply、未 @，在群里普通说另一句话
```

期望：

- 不强制恢复旧 wait。
- 进入正常 Attention/Judge。

### 9.5 Wait state 上限

场景：

```text
同一群制造超过上限的 wait states
```

期望：

- 旧 wait 被清理或拒绝。
- 状态字典不无限增长。

## 10. P2 需求：观测、灰度与压测

### 10.1 P2 目标

P2 不引入核心语义变化，重点增强可观测性和灰度安全：

- turn generation trace。
- send claim trace。
- stale reply 统计。
- wait resume 统计。
- 控制消息隔离统计。
- 性能与压力测试。
- 灰度开关。

### 10.2 配置/灰度开关

建议内部开关：

```text
conversation_generation_enabled
reply_send_claim_enabled
group_thread_wait_enabled
non_conversational_guard_enabled
```

默认策略建议：

- P0 合入后 generation/send claim 默认开启。
- P1 group_thread_wait 可灰度开启。
- debug trace 默认关闭。

### 10.3 Summary Trace

默认 trace 不记录正文，只记录：

- `chat_id`
- `mode`
- `thread_id`
- `generation`
- `action`
- `blocked_reason`
- `send_key hash`
- `claim_status`
- `wait_scope`
- `wait_resume_reason`

不得默认记录：

- 用户完整消息正文。
- assistant 完整回复正文。
- recent context。
- memory content。

### 10.4 Debug Trace

debug trace 可记录更完整信息，但必须显式开启。

debug trace 可包含：

- raw query preview。
- event message ids。
- thread resolver 输入/输出。
- stale check 前后状态。
- send claim 状态详情。
- wait state 详情。

debug trace 仍应限制长度，避免日志爆炸。

### 10.5 压测场景

P2 至少覆盖：

- 单群 10 用户快速 @bot。
- 单群 3 thread 交错回复。
- 单私聊用户 5 条连续输入。
- 100 条 non conversational 控制消息不推进 generation。
- 模型 90 秒返回时旧 generation 不发送。
- 分段回复并发发送不重复 claim。

## 11. P2 验收标准

- 可以从日志判断某条回复为何被阻断。
- 可以统计 stale_generation 次数。
- 可以统计 send_claim_exists 次数。
- 默认 summary trace 不泄露正文。
- debug trace 关闭时不构造大 payload。
- 压测下 generation/send claim 状态不无限增长。
- P1 灰度开关关闭时回到 P0 行为。

## 12. P3 需求：长期架构收敛

### 12.1 ConversationModePolicy

P3 可考虑引入策略层：

```python
class ConversationModePolicy(Protocol):
    def build_thread_id(self, event) -> str: ...
    def should_resume_wait(self, event, state) -> bool: ...
    def should_force_reply(self, event, plan) -> bool: ...
    def on_new_message(self, event) -> TurnIdentity: ...
    def can_send_reply(self, turn) -> bool: ...
```

实现：

- `GroupConversationPolicy`
- `PrivateConversationPolicy`

目的：

- 收敛 scattered `FriendMessage` 判断。
- 把群聊/私聊差异显式化。
- 降低后续维护成本。

### 12.2 持久化/分布式 CAS

如果未来 AstrMai 支持多进程或多实例，应将 generation/send claim 从内存迁移到：

- SQLite/DB CAS。
- Redis。
- AstrBot KV + compare-and-set 封装。

P0-P2 不要求。

### 12.3 Outbox 模型

长期可引入 Outbox：

- pending outbound。
- claimed outbound。
- committed outbound。
- failed outbound。
- retry policy。

用途：

- 精准恢复发送失败。
- 避免分段半成功状态不可见。
- 支持管理页审计。

### 12.4 管理页展示

未来 WebUI 可展示：

- active turns。
- active wait states。
- stale blocked replies。
- send claim records。
- group thread states。
- private session pending messages。

P0-P2 不做 UI。

## 13. 影响面分析

### 13.1 可能涉及模块

P0 可能涉及：

- `astrmai/infrastructure/runtime/chat_runtime_coordinator.py`
- `astrmai/presentation/events/message_entry.py`
- `astrmai/presentation/dto/message_scope.py`
- `astrmai/conversation/execution/reply_service` 相关文件
- `astrmai/conversation/execution/reply_freshness.py`
- `astrmai/state/private_chat/private_chat_manager.py`
- `astrmai/app/plugin_facade.py`
- 对应 tests

P1 可能涉及：

- `astrmai/state/group_wait/group_reply_wait_manager.py`
- `astrmai/conversation/attention/focus_thread` 相关模块
- `astrmai/conversation/loop/chat_loop_kernel.py`
- `astrmai/infrastructure/runtime/chat_runtime_coordinator.py`
- tests

### 13.2 不应修改的内容

P0 不应修改：

- RAG 检索语义。
- 记忆写入 schema。
- 模型配置。
- Gateway lane 选择。
- GroupWait 主结构。
- sys3/workmode。
- WebUI。

## 14. 风险与缓解

### 14.1 群聊 P0 过度保守

风险：

- 同群任何有效消息都会让旧回复过期。
- 可能减少慢回复发送率。

缓解：

- P0 接受该保守策略。
- P1 用 thread_id 细化。
- 日志记录 stale_generation 便于观察。

### 14.2 缺失 turn 的旧路径

风险：

- 某些主动任务、定时任务或外部入口没有 turn。

缓解：

- P0 中缺失 turn 时保持旧行为。
- 只记录 debug。
- P2 再补主动任务 turn 策略。

### 14.3 send claim 阻断合法重试

风险：

- 发送失败后 claim 已占用，自动重试无法发送。

缓解：

- P0 默认保守，不自动重试。
- `mark_send_failed` 记录状态。
- P3 Outbox 再设计安全重试。

### 14.4 控制消息标记不完整

风险：

- 外部测试插件未设置 `astrmai_non_conversational`，控制消息仍进入链路。

缓解：

- 测试插件必须补打标。
- P2 增加可配置控制消息前缀仅用于测试环境。
- 主链路不硬编码业务前缀。

## 15. 开发执行建议

### 15.1 P0 推荐顺序

1. 增加 TurnIdentity 或轻量 turn extras。
2. RuntimeCoordinator 增加 generation API 和测试。
3. RuntimeCoordinator 增加 send claim API 和测试。
4. message_entry 在有效消息后创建 turn。
5. ReplyService 发送前 stale check。
6. ReplyService final send claim。
7. 私聊 signal_new_message 与 generation 对齐。
8. 补 P0 并发回归测试。
9. 跑相关测试。
10. 跑全量 pytest 与 compileall。

### 15.2 P1 推荐顺序

1. 增加 group thread resolver。
2. GroupWait 状态改为 per-thread。
3. GroupWait 恢复条件收紧。
4. generation 维度切换为 group thread。
5. 补同群多线程测试。
6. 灰度开关验证。

### 15.3 P2 推荐顺序

1. 增加 summary trace。
2. 增加 debug trace。
3. 增加指标统计。
4. 增加压力测试。
5. 增加灰度配置与文档。

## 16. 最终成功标准

整体完成后，AstrMai 在真实 QQ 环境中应满足：

- 同一私聊用户连续发消息，不会收到旧问题的迟到回复。
- 私聊等待期继续发消息不会断续聊。
- 私聊仍保持强回应体验。
- 同一轮 final 不重复发送。
- 群聊中旧回复不会污染新一轮有效对话。
- P1 后同群不同用户/话题可以并行隔离。
- GroupWait 不再被同用户普通消息无条件误恢复。
- 测试/控制消息不会进入正常对话状态机。
- 所有阻断都有可审计日志或 trace。
- 默认 trace 不泄露用户正文。
