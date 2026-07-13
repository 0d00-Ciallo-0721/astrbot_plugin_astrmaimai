# AstrMai 群聊并发隔离与私聊连续轮次治理实施计划

## 0. 计划目标

本文档基于 `plan/P0_P3_CONVERSATION_CONCURRENCY_REQUIREMENTS.md`，将需求拆解为可执行开发计划。计划目标是指导后续代码落地，确保每一阶段都有明确的：

- 改动范围。
- 目标文件。
- 前置检查。
- 最小实现步骤。
- 测试用例。
- 验收命令。
- 回滚策略。
- 不应触碰的边界。

本计划仍然遵循最小改动原则：先建立发送端硬闸和 generation 底座，再逐步治理群聊 per-thread。P0 不提前重构全链路策略层，不取消模型请求，不改变现有聊天决策语义。

## 1. 总体分期

### 1.1 P0：最小并发防线

目标：

- 为有效对话消息创建 turn generation。
- ReplyService final 发送前检查 stale generation。
- ReplyService final 发送前执行 exactly-once send claim。
- 私聊等待期新消息同时推进 generation、进入 pending、唤醒 wait。
- 控制/测试/观测消息不进入正常对话状态机。

不做：

- GroupWait per-thread。
- 完整 thread resolver。
- ConversationModePolicy 抽象。
- 模型请求取消。
- 分布式 CAS。

### 1.2 P1：群聊多线程隔离

目标：

- 群聊 thread_id 从 `chat_id` 升级为真实 thread。
- GroupWait 从 per-chat 升级为 per-thread。
- GroupWait 恢复条件从弱匹配收紧为强匹配。
- 同群多用户/多话题可并行，互不误伤。

### 1.3 P2：观测、灰度、压测

目标：

- 给 generation、send claim、stale block、wait resume 增加 summary trace。
- debug trace 显式开关控制，默认不构造大 payload。
- 增加灰度开关与压测场景。
- 真实 QQ 验收脚本纳入观察项。

### 1.4 P3：长期架构收敛

目标：

- 收敛群聊/私聊策略分支。
- 设计持久化 outbox 和分布式 CAS。
- 管理页展示 active turns、wait states、blocked replies。

## 2. 开发总原则

### 2.1 只在必要层加硬闸

并发治理的第一刀必须落在发送端。Planner/Judge/Attention 可能有多条路径进入发送，只有 ReplyService 能统一兜底。

P0 关键路径：

```text
有效用户消息
  -> advance_generation
  -> event extras 绑定 TurnIdentity
  -> System2/Planner/ReplyService
  -> ReplyService 发送前 is_current_turn
  -> ReplyService 发送前 claim_send
  -> send segments
  -> commit_send
```

### 2.2 不重写现有文件

每个任务只改目标函数和相邻调用点。禁止为了并发治理重写完整模块。

### 2.3 缺失 turn 时保持兼容

主动唤醒、定时任务、历史测试、外部入口可能暂时没有 `TurnIdentity`。P0 中缺失 turn 时必须保持旧行为，只记录 debug，不崩溃、不阻断。

### 2.4 私聊和群聊只共享底座

共享：

- generation storage。
- send claim。
- stale check。
- trace。

不共享：

- GroupWait 恢复规则。
- PrivateChatManager pending/wait。
- 私聊强回复语义。

## 3. P0 实施计划

### 3.1 P0-0 前置审计

#### 目标

在动代码前确认当前发送路径、测试入口和相关文件。

#### 需要读取的文件

- `main.py`
- `astrmai/presentation/events/message_entry.py`
- `astrmai/presentation/dto/message_scope.py`
- `astrmai/app/plugin_facade.py`
- `astrmai/infrastructure/runtime/chat_runtime_coordinator.py`
- `astrmai/conversation/execution/reply_freshness.py`
- `astrmai/conversation/execution/reply_post_send.py`
- `astrmai/conversation/execution/reply_service.py` 或实际 ReplyService 拆分文件
- `astrmai/state/private_chat/private_chat_manager.py`
- `astrmai/conversation/attention/gate.py`
- `astrmai/conversation/execution/followup_manager.py`
- `tests/test_reply_service_refactor.py`
- `tests/test_system2_runner_refactor.py`
- `tests/unit/state/test_private_chat_manager_migrated.py`
- `tests/test_chat_loop_kernel_refactor.py`

#### 输出

不需要生成单独文档，但开发时必须确认：

- ReplyService final 发送的唯一入口或所有入口。
- 分段回复发送位置。
- 是否能获取 outbound message id。
- `event.set_extra/get_extra` 是否贯穿到发送端。
- 私聊 signal_new_message 当前调用点。

### 3.2 P0-1 新增 TurnIdentity

#### 推荐文件

新增：

- `astrmai/conversation/contracts/turn_identity.py`

备选：

- `astrmai/infrastructure/runtime/runtime_contracts.py`

推荐新增独立 contracts 文件，避免 runtime_contracts 继续膨胀。

#### 数据结构

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    mode: Literal["group", "private"]
    chat_id: str
    thread_id: str
    generation: int
    sender_id: str = ""
    input_message_ids: tuple[str, ...] = ()
    created_at: float = 0.0
```

#### 辅助方法

建议加轻量 helper：

```python
def build_p0_thread_id(mode: str, chat_id: str) -> str:
    return f"private:{chat_id}" if mode == "private" else chat_id

def turn_send_key(turn: TurnIdentity, response_kind: str = "final") -> str:
    return f"{turn.mode}:{turn.chat_id}:{turn.thread_id}:{turn.generation}:{response_kind}"
```

#### 注意

- 不在 P0 中引入完整 `ConversationModePolicy`。
- 不把 `TurnIdentity` 依赖反向导入到低层基础模块造成循环。
- 如果发生循环导入，发送端可通过 duck typing 读取属性，不强依赖类型。

#### 测试

新增或补充：

- `tests/unit/conversation/test_turn_identity.py`

用例：

- private thread id 构造。
- group thread id 构造。
- send key 构造稳定。

### 3.3 P0-2 RuntimeCoordinator 增加 generation 状态

#### 目标文件

- `astrmai/infrastructure/runtime/chat_runtime_coordinator.py`

#### 状态结构

在 `ChatRuntimeState` 中增加：

```python
turn_generations: Dict[str, int] = field(default_factory=dict)
send_claims: Dict[str, SendClaimState] = field(default_factory=dict)
```

或在 `ChatRuntimeCoordinator` 上集中维护：

```python
_turn_generations: Dict[tuple[str, str], int]
_send_claims: Dict[str, SendClaimState]
```

推荐优先放进 `ChatRuntimeState`，因为现有 `clear_runtime_state(chat_id)`、`prune_inactive()` 都是 chat 维度，更容易清理。

#### API

新增 async 方法：

```python
async def advance_generation(self, chat_id: str, thread_id: str) -> int
async def current_generation(self, chat_id: str, thread_id: str) -> int
async def is_current_turn(self, turn: Any) -> bool
```

#### 行为

- `advance_generation("", "")` 不应崩溃，但应使用空字符串归一化。
- 第一次 advance 返回 1。
- `current_generation` 不存在时返回 0。
- `is_current_turn` 缺少必要属性时返回 True 或 False 需要谨慎。

P0 建议：

```text
turn 缺失：调用方保持旧行为
turn 属性不完整：is_current_turn 返回 True，避免误杀
turn generation 非 int：返回 True 并记录 debug
```

这样不会因类型兼容问题破坏老路径。

#### 测试

新增：

- `tests/unit/infrastructure/test_chat_runtime_generation.py`

用例：

- 同 chat/thread 连续 advance 返回 1、2、3。
- 同 chat 不同 thread 独立。
- 不同 chat 独立。
- clear_runtime_state 清理 generation。
- concurrent advance 不重复、不倒退。

### 3.4 P0-3 RuntimeCoordinator 增加 send claim

#### 目标文件

- `astrmai/infrastructure/runtime/chat_runtime_coordinator.py`

#### 数据结构

```python
@dataclass
class SendClaimState:
    status: str
    created_at: float
    updated_at: float
    outbound_message_ids: list[str] = field(default_factory=list)
    error: str = ""
```

#### API

```python
async def claim_send(self, chat_id: str, send_key: str) -> bool
async def commit_send(self, chat_id: str, send_key: str, outbound_message_ids: list[str]) -> None
async def mark_send_failed(self, chat_id: str, send_key: str, error: Any) -> None
async def get_send_claim(self, chat_id: str, send_key: str) -> dict | None
```

API 是否传 `chat_id` 有两种选择：

1. `claim_send(send_key)`：简单，但清理时要扫描 key。
2. `claim_send(chat_id, send_key)`：清理方便。

推荐 P0 使用 `chat_id + send_key`，和现有 per-chat runtime 状态一致。

#### 行为

- `claim_send` 只在无记录时返回 True。
- 已有 `claimed`、`committed`、`failed` 都返回 False。
- `commit_send` 若 key 不存在，不抛错，只创建 committed 或记录 debug。
- `mark_send_failed` 若 key 不存在，不抛错。
- `clear_runtime_state(chat_id)` 清理 claims。
- `prune_inactive()` 清理 claims。

#### 测试

用例：

- 首次 claim True。
- 二次 claim False。
- commit 后 claim False。
- failed 后 P0 claim False。
- commit 记录 outbound ids。
- clear chat 后同 key 可重新 claim。

### 3.5 P0-4 有效消息判定和 TurnIdentity 创建

#### 目标文件

- `astrmai/presentation/events/message_entry.py`
- `astrmai/app/plugin_facade.py`
- 可能新增 `astrmai/conversation/ingress/turn_guard.py`

#### 推荐方式

为了避免 `message_entry.py` 直接操作 runtime 细节，在 `PluginFacade` 增加方法：

```python
async def prepare_conversation_turn(self, event, scope) -> None:
    ...
```

`message_entry.handle_global_message()` 在所有早期过滤通过后调用：

```python
await facade.prepare_conversation_turn(event, scope)
```

#### 调用位置

必须在：

- dedupe 之后。
- self message 之后。
- poke stop 之后。
- framework command 之后。
- permission guard 之后。
- non conversational guard 之后。
- `record_and_dispatch_attention()` 之前。

#### non conversational guard

在 `handle_global_message()` 早期加入：

```python
if event.get_extra("astrmai_non_conversational", False):
    debug_trace(...)
    return
```

位置建议：

- command guard 之后。
- permission guard 前后都可以，但更推荐 permission guard 之后，避免未授权消息借标记绕过入口审计。

如果测试插件会 stop event，AstrMai 这里直接 return 即可。

#### TurnIdentity 创建逻辑

```python
mode = "private" if scope.is_private_chat else "group"
thread_id = build_p0_thread_id(mode, scope.chat_id)
generation = await runtime_coordinator.advance_generation(scope.chat_id, thread_id)
turn = TurnIdentity(...)
event.set_extra("astrmai_turn_identity", turn)
```

message id 获取优先级：

- `event.message_obj.message_id`
- `event.message_id`
- raw message 中的 `message_id`
- 空字符串 fallback

#### 测试

新增或补充：

- `tests/unit/presentation/test_message_entry_turn_generation.py`

用例：

- 普通群消息创建 group turn。
- 普通私聊创建 private turn。
- framework command 不创建 turn。
- permission deny 不创建 turn。
- duplicate 不创建 turn。
- `astrmai_non_conversational=True` 不创建 turn、不 dispatch。

### 3.6 P0-5 私聊 pending 与 generation 对齐

#### 目标文件

- `astrmai/conversation/attention/gate.py`
- `astrmai/state/private_chat/private_chat_manager.py`
- `astrmai/app/plugin_facade.py`

#### 当前关键路径

`AttentionGate.process_event()` 中：

```python
if is_private and self.private_chat_manager and not is_strong_wakeup:
    await self.private_chat_manager.signal_new_message(sender_id, msg_str, chat_id=chat_id)
    return "PRIVATE_WAIT"
```

P0 已经在入口创建 generation，所以这里不一定要再次 advance。关键是确保：

- private wait 消息仍会通过入口创建 turn。
- signal_new_message 不被跳过。
- 如果 `AttentionGate` 某些路径绕过入口，需要补兜底。

#### 推荐实现策略

P0 不修改 `PrivateChatManager.signal_new_message()` 签名，避免扩散。只保证入口在调用 `record_and_dispatch_attention()` 前已经创建 turn。

可选增强：

```python
turn = event.get_extra("astrmai_turn_identity", None)
if turn:
    session.last_turn_generation = turn.generation
```

但这需要扩展 `PrivateSession`，P0 可不做，除非测试需要观测。

#### 测试

用例：

- 私聊 wait 中新消息会 advance_generation。
- 私聊 wait 中新消息仍调用 `signal_new_message`。
- `pending_messages` 包含消息。
- `new_message_event` 被 set。

### 3.7 P0-6 ReplyService stale check

#### 目标文件

需要先定位真实 ReplyService。可能涉及：

- `astrmai/conversation/execution/reply_service.py`
- `astrmai/conversation/execution/reply_post_send.py`
- `astrmai/conversation/execution/reply_freshness.py`

#### 插入点

必须在清洗文本、分段之后或之前？

推荐：

1. 完成文本清洗。
2. 计算 outbound policy。
3. 真正发送前检查 turn current。
4. claim final。
5. 发送所有 segment。

原因：

- 文本清洗成本小，不影响。
- policy 可能依赖 freshness。
- 发送前是最后硬闸。

#### 行为

```python
turn = event.get_extra("astrmai_turn_identity", None)
if turn and runtime_coordinator:
    if not await runtime_coordinator.is_current_turn(turn):
        log/debug_trace blocked stale_generation
        event.set_extra("astrmai_reply_blocked_reason", "stale_generation")
        return
```

缺失 turn：

- 保持旧行为。
- debug 记录 `turn_missing`.

#### 测试

用例：

- current turn 允许发送。
- stale turn 不发送。
- stale turn 不 fallback。
- 缺失 turn 保持旧行为。

### 3.8 P0-7 ReplyService send claim

#### 插入点

stale check 之后、发送 segment 之前。

#### 行为

```python
send_key = turn_send_key(turn, "final")
if not await runtime_coordinator.claim_send(turn.chat_id, send_key):
    log/debug_trace blocked send_claim_exists
    event.set_extra("astrmai_reply_blocked_reason", "send_claim_exists")
    return

try:
    outbound_ids = await send_segments(...)
except Exception as exc:
    await runtime_coordinator.mark_send_failed(turn.chat_id, send_key, exc)
    raise
else:
    await runtime_coordinator.commit_send(turn.chat_id, send_key, outbound_ids)
```

如果当前发送函数拿不到 outbound message ids：

- P0 可 commit 空列表。
- 后续 P1/P2 再从 adapter 返回值中补全。

#### 分段注意

不要在 segment loop 内 claim。claim 必须包住整组 final。

#### 测试

用例：

- 同一 turn 两次调用 handle_reply，只发送一次。
- 分段回复只 claim 一次。
- claim 失败不发送 fallback。
- 发送异常后 mark failed 被调用。

### 3.9 P0-8 Reply freshness 与 private overdue

#### 目标文件

- `astrmai/conversation/execution/reply_freshness.py`

#### 当前风险

`_allow_direct_reply_timeout()` 只判断 direct engagement 和 latest activity。私聊被视为 direct engagement，模型很慢时可能放行旧回复。

#### P0 调整

如果 event 有 turn：

- turn stale：不允许 overdue direct reply。
- turn current：可按旧规则继续判断。

伪逻辑：

```python
turn = event.get_extra("astrmai_turn_identity", None)
if turn and event.get_extra("is_private_chat", False):
    if runtime_coordinator and not await runtime_coordinator.is_current_turn(turn):
        return False
```

最终发送端仍会 stale check，这里是提前减少错误 policy。

#### 测试

用例：

- 私聊 current turn overdue 可放行。
- 私聊 stale turn overdue 不放行。
- 无 turn 时保持旧行为。

### 3.10 P0-9 控制消息打标协作

#### AstrMai 主插件

只识别：

```text
astrmai_non_conversational
```

不硬编码具体测试前缀。

#### 测试插件/Probe/Orchestrator

后续需要在测试插件中：

- 对 `amt_probe bind` 控制消息打标或在插件内 stop。
- 对 `[AMTEST]` 状态消息打标或避免被本机 AstrMai 接收处理。

此项可以在 AstrMai P0 后单独补测试插件，不阻塞主插件 P0。

## 4. P0 开发小闭环顺序

### 4.1 Commit 1：TurnIdentity + RuntimeCoordinator generation/send claim

改动：

- 新增 TurnIdentity。
- RuntimeCoordinator 新增 generation API。
- RuntimeCoordinator 新增 send claim API。
- 单元测试覆盖 coordinator。

验证：

```text
python -m pytest tests/unit/infrastructure/test_chat_runtime_generation.py -q
python -m pytest tests/unit/conversation/test_turn_identity.py -q
```

成功标准：

- generation 并发安全。
- send claim exactly-once。
- clear/prune 清理正确。

### 4.2 Commit 2：入口 TurnIdentity 创建 + non conversational guard

改动：

- `PluginFacade.prepare_conversation_turn()`。
- `message_entry.handle_global_message()` 调用。
- non conversational guard。
- presentation 单元测试。

验证：

```text
python -m pytest tests/unit/presentation/test_message_entry_turn_generation.py -q
python -m pytest tests/test_message_entry_refactor.py -q
```

如实际文件名不同，以现有 message_entry 测试为准。

成功标准：

- 有效消息创建 turn。
- 控制消息不创建 turn。
- 旧入口行为不破坏。

### 4.3 Commit 3：ReplyService stale check + send claim

改动：

- ReplyService 发送前 current generation 校验。
- ReplyService 发送前 final claim。
- 分段回复共享 claim。
- 发送失败 mark failed。

验证：

```text
python -m pytest tests/test_reply_service_refactor.py -q
```

成功标准：

- stale turn 不发送。
- duplicate final 不发送。
- missing turn 兼容旧行为。
- 分段不被错误截断。

### 4.4 Commit 4：私聊 pending/generation 对齐

改动：

- 确认私聊 wait 消息经过入口 generation。
- 如必要，在 PrivateChatManager 或 AttentionGate 中记录 turn 信息。
- 补私聊 wait 测试。

验证：

```text
python -m pytest tests/unit/state/test_private_chat_manager_migrated.py -q
python -m pytest tests/original_ported/test_attention_private_chat_ported.py -q
python -m pytest tests/test_system2_runner_refactor.py -q
```

成功标准：

- 私聊 wait 被唤醒。
- pending message 保留。
- generation 推进。
- 私聊强回复不被破坏。

### 4.5 Commit 5：P0 并发回归整合

改动：

- 新增并发回归测试文件。
- 覆盖 stale reply、duplicate final、control message。

推荐文件：

- `tests/regression/test_conversation_turn_generation_p0.py`

验证：

```text
python -m pytest tests/regression/test_conversation_turn_generation_p0.py -q
python -m pytest tests/test_system2_runner_refactor.py tests/test_reply_service_refactor.py -q
python -m compileall astrmai main.py config.py
git diff --check
```

成功标准：

- P0 场景全部通过。
- compileall 通过。
- diff check 通过。

## 5. P1 实施计划

### 5.1 P1-0 前置观测

P1 开始前必须先基于 P0 日志观察：

- 群聊 stale_generation 发生频率。
- stale 是否主要来自同 thread，还是来自不同用户/话题。
- GroupWait 误恢复频率。
- 是否存在用户反馈“@bot 后被别人消息打断”。

只有确认 P0 保守策略造成明显体验损耗，才进入 P1。

### 5.2 P1-1 Group thread resolver

#### 推荐文件

新增：

- `astrmai/conversation/threading/group_thread_resolver.py`

#### 输入

- event。
- MessageScope。
- existing event extras。
- reply metadata。
- focus thread context。
- GroupWait state。

#### 输出

```python
@dataclass(frozen=True, slots=True)
class GroupThreadResolution:
    thread_id: str
    source: str
    confidence: float
    root_message_id: str = ""
    reply_to_message_id: str = ""
    thread_signature: str = ""
```

#### P1 解析优先级

1. reply_to outbound message id。
2. reply_to root message id。
3. explicit thread_signature。
4. focus thread signature。
5. direct @ bot new root。
6. fallback chat_id。

#### 测试

新增：

- `tests/unit/conversation/test_group_thread_resolver.py`

覆盖各优先级和 fallback。

### 5.3 P1-2 GroupWait per-thread 状态

#### 目标文件

- `astrmai/state/group_wait/group_reply_wait_manager.py`

#### 改动策略

分两步：

1. 内部数据结构升级，外部 API 尽量保持兼容。
2. 新增 thread-aware API。

兼容 API：

- `register_from_reply_event(event)` 仍可用。
- `handle_incoming_message(event)` 仍返回原字符串。
- `get_wait_info(chat_id)` 旧调用返回最相关或最新 wait。

新增 API：

```python
get_wait_info(chat_id, thread_id="")
cancel_wait(chat_id, thread_id="", reason="")
list_waits(chat_id)
```

#### 状态结构

```python
_states: dict[str, dict[str, GroupReplyWaitState]]
```

#### 迁移要求

- 不做数据持久迁移，运行时内存升级即可。
- 如果发现旧 state 格式，降级兼容。

### 5.4 P1-3 GroupWait 恢复条件收紧

#### 旧弱条件

```text
sender_id == state.target_user_id
```

#### 新强条件

满足任一：

- reply_to 命中 outbound message id。
- root message id 命中。
- thread_signature 匹配。
- 明确 @ bot 且 sender 是 target，且 resolver 命中 thread。
- direct wakeup 且 resolver 命中 thread。

#### 不允许

- 仅同 sender 普通消息恢复旧 wait。
- 控制消息恢复 wait。
- 其他用户消息恢复 wait。

### 5.5 P1-4 generation 切换到 group thread

入口创建 group turn 时：

- 调用 group thread resolver。
- `thread_id = resolution.thread_id`。
- advance `chat_id + thread_id` generation。

fallback：

- resolver 异常时 `thread_id=chat_id`，保持 P0。

### 5.6 P1 测试矩阵

新增：

- `tests/regression/test_group_thread_concurrency_p1.py`
- `tests/unit/state/test_group_reply_wait_threaded.py`

场景：

- 同群 A/B 双线程并发。
- 同 thread latest-wins。
- reply_to 恢复对应 wait。
- 同 target 普通消息不恢复 wait。
- wait 上限。
- resolver 异常降级。
- P0 私聊测试仍通过。

### 5.7 P1 验证命令

```text
python -m pytest tests/unit/conversation/test_group_thread_resolver.py -q
python -m pytest tests/unit/state/test_group_reply_wait_threaded.py -q
python -m pytest tests/regression/test_group_thread_concurrency_p1.py -q
python -m pytest tests/test_chat_loop_kernel_refactor.py tests/test_system2_runner_refactor.py -q
python -m compileall astrmai main.py config.py
git diff --check
```

## 6. P2 实施计划

### 6.1 Summary trace

#### 目标

默认开启轻量 summary，不含正文。

#### 记录点

- Turn 创建。
- Generation advance。
- Stale block。
- Send claim success/fail。
- GroupWait resume/ignore。
- Private wait wake。

#### 字段

```text
chat_id
mode
thread_id
generation
action
blocked_reason
claim_status
wait_scope
resolver_source
```

#### 禁止

- 用户完整文本。
- assistant 完整文本。
- memory 正文。
- recent context。

### 6.2 Debug trace

#### 开关

新增或复用内部 debug 配置：

```text
conversation_concurrency_debug_trace_enabled
```

#### 行为

- 关闭时不构造大 payload。
- 开启时限制 preview 长度。
- 避免持久化敏感正文，除非本地测试明确开启。

### 6.3 灰度开关

建议配置：

```text
conversation_generation_enabled = true
reply_send_claim_enabled = true
group_thread_wait_enabled = false
non_conversational_guard_enabled = true
```

P0 默认开启 generation/send claim，因为它是安全硬闸。P1 per-thread 可灰度。

### 6.4 压测与真实 QQ 验收

压测文件：

- `tests/regression/test_conversation_concurrency_pressure.py`

真实 QQ 验收：

- 使用 Probe + Orchestrator。
- 场景加入：
  - 快速连续私聊。
  - 群聊 A/B 交错 @bot。
  - AMTEST 状态消息隔离。
  - 90 秒模型迟到。

### 6.5 P2 验证命令

```text
python -m pytest tests/regression/test_conversation_concurrency_pressure.py -q
python -m pytest tests/regression/test_conversation_turn_generation_p0.py tests/regression/test_group_thread_concurrency_p1.py -q
python -m pytest -q
python -m compileall astrmai main.py config.py
git diff --check
```

## 7. P3 实施计划

### 7.1 ConversationModePolicy 收敛

P3 才引入：

- `GroupConversationPolicy`
- `PrivateConversationPolicy`

迁移目标：

- 收敛 scattered `FriendMessage` 判断。
- 将 `thread_id` 构造、强回复、wait resume、send eligibility 显式化。

不在 P0/P1 做，避免抽象先行。

### 7.2 Outbox

设计持久化 outbound：

- `pending`
- `claimed`
- `committed`
- `failed`
- `retry_suppressed`

可用于：

- 发送失败审计。
- 分段半成功恢复。
- WebUI 展示。

### 7.3 分布式 CAS

如果未来多实例部署：

- generation CAS。
- send claim CAS。
- wait state CAS。

候选：

- SQLite transaction。
- Redis。
- AstrBot KV compare-and-set 封装。

### 7.4 WebUI 展示

展示内容：

- active turns。
- active group waits。
- private pending sessions。
- stale blocked replies。
- send claim records。

## 8. 测试总矩阵

### 8.1 Unit

- TurnIdentity helper。
- RuntimeCoordinator generation。
- RuntimeCoordinator send claim。
- Message entry turn creation。
- PrivateChatManager wait/pending。
- GroupThreadResolver。
- GroupReplyWaitManager threaded state。

### 8.2 Regression

- 私聊旧回复迟到。
- 私聊等待期续聊。
- 私聊强回复保留。
- 群聊重复 final。
- 控制消息不推进 generation。
- 群聊旧回复过期。
- 分段回复共享 claim。
- 同群双线程并发。
- GroupWait 弱恢复禁止。

### 8.3 Integration

- `tests/integration/test_message_to_reply_pipeline.py`
- `tests/integration/test_memory_write_retrieve_inject.py`
- `tests/integration/test_hot_config_consistency.py`

并发治理不应破坏：

- 正常消息到回复链路。
- 记忆注入链路。
- 热配置刷新。

### 8.4 Manual QQ

最低真实场景：

```text
1. smoke run：基础回复、记忆写入、记忆召回、情感回复。
2. 私聊连续输入：A -> B -> 只看 B 或合并后回复。
3. 群聊连续消息：A @bot 后 B 插话，P0 旧 A 可被阻断；P1 A/B 分离。
4. 控制消息：[AMTEST] 不触发 AstrMai 回复。
5. 90 秒迟到：旧 generation 不发送。
```

## 9. 验收关口

### 9.1 P0 合入关口

必须通过：

```text
python -m pytest tests/unit/infrastructure/test_chat_runtime_generation.py -q
python -m pytest tests/unit/presentation/test_message_entry_turn_generation.py -q
python -m pytest tests/regression/test_conversation_turn_generation_p0.py -q
python -m pytest tests/test_reply_service_refactor.py tests/test_system2_runner_refactor.py -q
python -m compileall astrmai main.py config.py
git diff --check
```

建议通过：

```text
python -m pytest -q
```

### 9.2 P1 合入关口

必须通过：

```text
python -m pytest tests/unit/conversation/test_group_thread_resolver.py -q
python -m pytest tests/unit/state/test_group_reply_wait_threaded.py -q
python -m pytest tests/regression/test_group_thread_concurrency_p1.py -q
python -m pytest tests/regression/test_conversation_turn_generation_p0.py -q
python -m compileall astrmai main.py config.py
git diff --check
```

### 9.3 P2 合入关口

必须通过：

```text
python -m pytest tests/regression/test_conversation_concurrency_pressure.py -q
python -m pytest -q
python -m compileall astrmai main.py config.py
git diff --check
```

并完成一次真实 QQ 灰度观察。

## 10. 回滚策略

### 10.1 P0 回滚

如果 P0 上线后出现误杀回复：

1. 优先关闭 `conversation_generation_enabled`。
2. 若重复回复仍严重，保留 `reply_send_claim_enabled`。
3. 如果 send claim 误伤分段，临时关闭 send claim。
4. 保留 non conversational guard，除非它误阻断正常消息。

### 10.2 P1 回滚

如果 GroupWait per-thread 出现误恢复：

1. 关闭 `group_thread_wait_enabled`。
2. 回到 P0 `thread_id=chat_id`。
3. 保留 P0 generation/send claim。

### 10.3 P2 回滚

如果 trace 或压测逻辑影响性能：

1. 关闭 debug trace。
2. 降低 summary trace 采样。
3. 移除压力测试中的昂贵路径，不影响主功能。

## 11. 开发注意事项

### 11.1 不要把 generation 当作业务轮次历史

generation 是并发控制版本号，不是用户可见的第几轮对话。不要写入长期记忆，不要展示给普通用户。

### 11.2 不要让 send claim 阻断非 final 内部动作

P0 只保护 final reply。工具调用、内部状态更新、memory write 不使用 final send claim。

### 11.3 不要在 generation stale 时发送 fallback

stale 表示旧回复不应出现，不是系统失败。发送 fallback 反而会制造新噪音。

### 11.4 不要把控制消息前缀硬编码进主链路

主链路只识别 `astrmai_non_conversational`。测试插件负责标记。

### 11.5 不要在 P0 做 per-segment 中断

P0 只在 final 发送前检查一次，避免半段已发导致体验割裂。

### 11.6 不要把私聊接入 GroupWait

私聊继续使用 PrivateChatManager。群聊 wait 的任何升级都不应影响私聊。

## 12. 推荐任务拆分清单

### P0 Task List

```text
P0-T1  TurnIdentity helper
P0-T2  RuntimeCoordinator generation API
P0-T3  RuntimeCoordinator send claim API
P0-T4  message_entry turn creation
P0-T5  non conversational guard
P0-T6  ReplyService stale generation check
P0-T7  ReplyService final send claim
P0-T8  private wait generation/pending alignment tests
P0-T9  P0 regression tests
P0-T10 full verification
```

### P1 Task List

```text
P1-T1  GroupThreadResolver
P1-T2  GroupReplyWait state map upgrade
P1-T3  GroupWait strong resume predicates
P1-T4  group generation uses thread_id
P1-T5  wait state limit/cleanup
P1-T6  P1 regression tests
P1-T7  gray switch validation
```

### P2 Task List

```text
P2-T1  summary trace
P2-T2  debug trace guarded payload
P2-T3  metrics counters
P2-T4  pressure tests
P2-T5  real QQ acceptance script update
P2-T6  documentation update
```

### P3 Task List

```text
P3-T1  ConversationModePolicy design
P3-T2  Outbox schema design
P3-T3  distributed CAS design
P3-T4  WebUI diagnostics design
```

## 13. 最终交付标准

P0 交付后应能回答：

- 这条回复为什么被阻断？
- 这条 final 是否已经发送过？
- 当前 event 属于哪个 generation？
- 私聊等待期新消息是否推进 generation？
- 控制消息是否被隔离？

P1 交付后应能回答：

- 群里这条消息属于哪个 thread？
- GroupWait 为什么恢复或不恢复？
- 同群 A/B 两个话题是否互不打断？

P2 交付后应能回答：

- stale_generation 的线上频率是多少？
- send_claim_exists 的线上频率是多少？
- debug trace 关闭时是否没有大 payload？
- 真实 QQ 灰度是否无重复 final、无明显误杀？
