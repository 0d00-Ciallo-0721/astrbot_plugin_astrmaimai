# AstrMai 群聊并发隔离与私聊连续轮次治理执行手册

## 0. 手册定位

本文档是 `P0_P3_CONVERSATION_CONCURRENCY_REQUIREMENTS.md` 与 `P0_P3_CONVERSATION_CONCURRENCY_IMPLEMENTATION_PLAN.md` 的执行版。它面向实际开发者或后续 Codex 会话，目标是让执行者可以按步骤开工、验证、排障和提交。

本文档回答：

- 开工前先看哪些文件。
- 每一步到底改哪里。
- 每一步先写哪些测试。
- 每一步运行哪些命令。
- 失败时如何定位。
- 哪些行为绝对不能误伤。
- 什么时候可以进入下一阶段。

本文档不重复论证方案必要性，只描述执行。

## 1. 执行前全局约束

### 1.1 本轮执行范围

除非明确另开任务，否则第一轮只执行 P0。

P0 只做：

- `TurnIdentity`。
- `ChatRuntimeCoordinator` generation。
- `ChatRuntimeCoordinator` send claim。
- 入口创建 turn。
- non conversational guard。
- ReplyService stale check。
- ReplyService final send claim。
- 私聊 pending/generation 对齐验证。
- P0 回归测试。

P0 不做：

- GroupWait per-thread。
- 完整 group thread resolver。
- ConversationModePolicy。
- 模型请求取消。
- WebUI。
- RAG/记忆链路调整。
- Gateway/lane 选择调整。

### 1.2 文件修改原则

只允许局部修改：

- 新增必要的小模块。
- 在现有函数中插入最小逻辑。
- 不重写完整文件。
- 不整理无关 import。
- 不顺手改编码乱码、注释、历史 TODO。
- 不触碰用户已有未提交变更。

### 1.3 兼容原则

P0 不能因为缺少 turn 让旧路径崩溃。

规则：

```text
有 TurnIdentity：执行 generation/send claim 保护
无 TurnIdentity：保持旧发送行为，只记录 debug
TurnIdentity 字段异常：保守保持旧行为，不误杀回复
```

### 1.4 用户体验原则

stale generation 不是错误，而是旧回复被正确丢弃。

因此：

- 不发送 fallback。
- 不提示用户“出错”。
- 不抛异常到主链路。
- 只记录 debug/trace。

## 2. 启动检查

### 2.1 必读文件

执行前按顺序读取：

```text
main.py
config.py
_conf_schema.json
astrmai/presentation/events/message_entry.py
astrmai/presentation/dto/message_scope.py
astrmai/app/plugin_facade.py
astrmai/app/runtime_facade_protocol.py
astrmai/infrastructure/runtime/chat_runtime_coordinator.py
astrmai/conversation/execution/reply_freshness.py
astrmai/conversation/execution/reply_post_send.py
astrmai/conversation/execution/followup_manager.py
astrmai/state/private_chat/private_chat_manager.py
astrmai/conversation/attention/gate.py
```

然后定位 ReplyService：

```text
rg -n "class Reply|def handle_reply|astrmai_reply_sent|plain_result|send_message" astrmai/conversation astrmai/app tests
```

必须确认：

- final reply 的实际发送函数。
- 分段回复在哪个函数循环发送。
- 是否可以拿到 outbound message id。
- `event.get_extra("astrmai_turn_identity")` 能否到达发送函数。

### 2.2 必读测试

执行前读取：

```text
tests/test_reply_service_refactor.py
tests/test_system2_runner_refactor.py
tests/test_chat_loop_kernel_refactor.py
tests/unit/state/test_private_chat_manager_migrated.py
tests/original_ported/test_attention_private_chat_ported.py
tests/unit/conversation/test_context_runtime_wiring.py
```

如果文件不存在，用 `rg` 找同类测试：

```text
rg -n "ReplyService|PrivateChatManager|ChatRuntimeCoordinator|message_entry|handle_global_message|FriendMessage" tests
```

### 2.3 基线命令

先运行最小基线：

```text
python -m pytest tests/unit/state/test_private_chat_manager_migrated.py -q
python -m pytest tests/test_reply_service_refactor.py -q
```

如果某个文件不存在，改跑 `rg` 找到的对应测试。

如果基线已有失败：

- 不先修无关失败。
- 记录失败测试名。
- 只要失败与本任务无关，后续验证中说明。

## 3. P0-T1 TurnIdentity helper

### 3.1 目标

提供一个轻量、稳定、可测试的 turn 标识对象和 key 构造 helper。

### 3.2 文件

新增：

```text
astrmai/conversation/contracts/turn_identity.py
```

如 `contracts` 目录已有 `__init__.py` 或导出模式，按现有模式补导出；若没有必要，不额外导出。

### 3.3 实现内容

最小内容：

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


def build_p0_thread_id(mode: str, chat_id: str) -> str:
    normalized_mode = str(mode or "").strip().lower()
    normalized_chat_id = str(chat_id or "").strip()
    if normalized_mode == "private":
        return f"private:{normalized_chat_id}"
    return normalized_chat_id


def build_turn_send_key(turn: TurnIdentity, response_kind: str = "final") -> str:
    kind = str(response_kind or "final").strip() or "final"
    return f"{turn.mode}:{turn.chat_id}:{turn.thread_id}:{int(turn.generation)}:{kind}"
```

### 3.4 测试

新增：

```text
tests/unit/conversation/test_turn_identity.py
```

覆盖：

- private thread id。
- group thread id。
- empty chat id 不崩溃。
- send key 稳定。
- response_kind 空值 fallback 为 final。

### 3.5 验证

```text
python -m pytest tests/unit/conversation/test_turn_identity.py -q
```

### 3.6 失败处理

如果导入路径失败：

- 检查 `astrmai/conversation/contracts/__init__.py` 是否有懒加载机制。
- 优先直接从具体模块导入。
- 不为了导入方便改全局 `__init__` 大结构。

## 4. P0-T2 RuntimeCoordinator generation

### 4.1 目标

在 `ChatRuntimeCoordinator` 中提供 per chat/thread generation，并发安全。

### 4.2 文件

修改：

```text
astrmai/infrastructure/runtime/chat_runtime_coordinator.py
```

新增测试：

```text
tests/unit/infrastructure/test_chat_runtime_generation.py
```

### 4.3 实现位置

在 `ChatRuntimeState` dataclass 中增加字段：

```python
turn_generations: Dict[str, int] = field(default_factory=dict)
```

key 使用 `thread_id` 字符串。因为 state 已经按 `chat_id` 分桶，不需要 tuple key。

### 4.4 新增方法

加在 `update_wait_targets/get_wait_targets` 附近或 runtime state 方法区域：

```python
async def advance_generation(self, chat_id: str, thread_id: str) -> int:
    ...

async def current_generation(self, chat_id: str, thread_id: str) -> int:
    ...

async def is_current_turn(self, turn: Any) -> bool:
    ...
```

### 4.5 行为细则

`advance_generation`：

- 归一化 `chat_id/thread_id` 为字符串。
- `thread_id` 为空时使用 `chat_id`。
- 在 `_lock` 内创建 state。
- 当前值 +1 后返回。

`current_generation`：

- 不存在返回 0。
- 不修改状态。

`is_current_turn`：

- `turn is None`：返回 True。
- 缺少 `chat_id/thread_id/generation`：返回 True。
- generation 无法转 int：返回 True。
- 正常情况下比较当前 generation。

这样做是为了 P0 兼容老路径，避免误杀。

### 4.6 清理逻辑

`clear_runtime_state(chat_id)` 已 pop 整个 state，自动清理。

`prune_inactive()` 删除 state，自动清理。

如果选择 coordinator 级 dict，则必须手动清理；推荐放入 `ChatRuntimeState`。

### 4.7 测试用例

`tests/unit/infrastructure/test_chat_runtime_generation.py`：

- `test_advance_generation_increments_per_thread`
- `test_generation_is_isolated_by_thread`
- `test_generation_is_isolated_by_chat`
- `test_current_generation_defaults_to_zero`
- `test_is_current_turn_accepts_matching_generation`
- `test_is_current_turn_rejects_stale_generation`
- `test_is_current_turn_is_compatible_with_missing_turn`
- `test_clear_runtime_state_resets_generation`
- `test_concurrent_advance_generation_is_monotonic`

### 4.8 验证

```text
python -m pytest tests/unit/infrastructure/test_chat_runtime_generation.py -q
```

### 4.9 失败处理

如果并发测试偶发失败：

- 确认所有读写都在 `_lock` 内。
- 不使用无锁 read-modify-write。
- 不把 `asyncio.Lock` 放在测试外复用跨 loop 对象。

## 5. P0-T3 RuntimeCoordinator send claim

### 5.1 目标

为同一 turn 的 final 回复提供 exactly-once claim。

### 5.2 文件

继续修改：

```text
astrmai/infrastructure/runtime/chat_runtime_coordinator.py
```

测试仍放：

```text
tests/unit/infrastructure/test_chat_runtime_generation.py
```

或拆：

```text
tests/unit/infrastructure/test_chat_runtime_send_claim.py
```

### 5.3 数据结构

新增 dataclass：

```python
@dataclass
class SendClaimState:
    status: str
    created_at: float
    updated_at: float
    outbound_message_ids: List[str] = field(default_factory=list)
    error: str = ""
```

在 `ChatRuntimeState` 增加：

```python
send_claims: Dict[str, SendClaimState] = field(default_factory=dict)
```

### 5.4 新增方法

```python
async def claim_send(self, chat_id: str, send_key: str) -> bool:
    ...

async def commit_send(self, chat_id: str, send_key: str, outbound_message_ids: list[str]) -> None:
    ...

async def mark_send_failed(self, chat_id: str, send_key: str, error: Any) -> None:
    ...

async def get_send_claim(self, chat_id: str, send_key: str) -> dict | None:
    ...
```

### 5.5 行为细则

`claim_send`：

- send_key 为空返回 False。
- 已存在任何状态返回 False。
- 不存在则创建 `claimed` 并返回 True。

`commit_send`：

- key 不存在时创建 `committed`，避免 commit 路径崩溃。
- key 存在时状态改为 `committed`。
- outbound ids 去重、转字符串。

`mark_send_failed`：

- key 不存在时创建 `failed`。
- key 存在时改为 `failed`。
- error 截断到安全长度，比如 300 字符。

`get_send_claim`：

- 返回普通 dict，不暴露内部 dataclass 可变对象。

### 5.6 测试用例

- `test_claim_send_first_time_succeeds`
- `test_claim_send_second_time_fails`
- `test_claim_send_after_commit_fails`
- `test_claim_send_after_failed_fails_in_p0`
- `test_commit_send_records_outbound_ids`
- `test_mark_send_failed_records_error`
- `test_clear_runtime_state_resets_send_claims`

### 5.7 验证

```text
python -m pytest tests/unit/infrastructure/test_chat_runtime_generation.py -q
```

或：

```text
python -m pytest tests/unit/infrastructure/test_chat_runtime_send_claim.py -q
```

## 6. P0-T4 入口 TurnIdentity 创建

### 6.1 目标

只有真正进入对话链路的有效消息才推进 generation，并把 turn 写入 event extras。

### 6.2 文件

修改：

```text
astrmai/app/plugin_facade.py
astrmai/app/runtime_facade_protocol.py
astrmai/presentation/events/message_entry.py
```

新增测试：

```text
tests/unit/presentation/test_message_entry_turn_generation.py
```

### 6.3 Facade 方法

在 `PluginFacade` 增加：

```python
async def prepare_conversation_turn(self, event, scope) -> None:
    ...
```

在 `RuntimeFacadeProtocol` 增加声明，避免类型测试失败。

方法内部：

- 获取 `runtime_coordinator`。
- mode = private/group。
- thread_id = `build_p0_thread_id(mode, scope.chat_id)`。
- generation = `await runtime_coordinator.advance_generation(...)`。
- message_id = helper 提取。
- created_at = `time.time()`。
- 创建 `TurnIdentity`。
- 写入 event extras。

### 6.4 message_entry 调用位置

在 `handle_global_message()` 中，必须放在：

```text
dedupe/self/poke/command/permission/non_conversational 之后
record_and_dispatch_attention 之前
```

推荐顺序：

1. dedupe。
2. self。
3. poke。
4. command。
5. permission。
6. non conversational guard。
7. group_reply_wait。
8. prepare_conversation_turn。
9. activity tracking。
10. feedback/direct/attention。

是否在 GroupWait 前创建 turn？

P0 推荐在 GroupWait 后创建 turn，原因：

- GroupWait 的 `RESUME` 也可能进入对话链路，但 P0 先避免扩大影响。
- 如果 GroupWait 处理过程中决定 stop/expire，避免无意义 generation。

但如果后续发现 `RESUME_WAIT` 回复也需要 turn，可在 P1 调整。

### 6.5 non conversational guard

在 permission 后加：

```python
try:
    if bool(event.get_extra("astrmai_non_conversational", False)):
        debug_trace(event, "ingress.stop", reason="non_conversational")
        return
except Exception:
    logger.exception(...)
```

不要发送任何文本。

### 6.6 测试用例

构造 fake facade：

- `prepare_conversation_turn` 记录调用。
- `record_and_dispatch_attention` 记录是否调用。

测试：

- 普通群消息调用 prepare。
- 普通私聊调用 prepare。
- `astrmai_non_conversational=True` 不调用 prepare、不 dispatch。
- command 不调用 prepare。
- permission deny 不调用 prepare。
- duplicate 不调用 prepare。

### 6.7 验证

```text
python -m pytest tests/unit/presentation/test_message_entry_turn_generation.py -q
python -m pytest tests/test_message_entry_refactor.py -q
```

如果 `tests/test_message_entry_refactor.py` 不存在，用现有 message_entry 测试替代。

## 7. P0-T5 ReplyService stale check

### 7.1 目标

旧 generation 的回复不发送。

### 7.2 定位发送入口

先执行：

```text
rg -n "class Reply|handle_reply|_check_reply_freshness|astrmai_reply_sent|send_message|plain_result" astrmai/conversation/execution tests/test_reply_service_refactor.py
```

必须找到：

- 最终文本进入发送的函数。
- 分段 loop。
- 设置 `astrmai_reply_sent=True` 的位置。

### 7.3 插入 helper

可以在 ReplyService 类中增加私有方法：

```python
async def _should_block_stale_turn(self, event, chat_id: str) -> tuple[bool, str]:
    ...
```

行为：

- runtime_coordinator 不存在：不阻断。
- turn 不存在：不阻断。
- turn 属性不完整：不阻断。
- not current：阻断，reason `stale_generation`。

### 7.4 插入位置

在真正发送前：

```python
blocked, reason = await self._should_block_stale_turn(event, chat_id)
if blocked:
    event.set_extra("astrmai_reply_blocked_reason", reason)
    return
```

不得：

- 设置 `astrmai_reply_sent=True`。
- arm private wait。
- register group wait。
- 发送 fallback。

### 7.5 测试用例

在 ReplyService 测试中 mock runtime_coordinator：

- current 返回 True -> 发送。
- current 返回 False -> 不发送。
- turn missing -> 发送。
- runtime_coordinator missing -> 发送。

断言：

- stale 时 send 方法未调用。
- stale 时 `astrmai_reply_sent` 不为 True。
- stale 时 blocked reason 写入 extra。

### 7.6 验证

```text
python -m pytest tests/test_reply_service_refactor.py -q
```

## 8. P0-T6 ReplyService send claim

### 8.1 目标

同一 turn generation 的 final 只能发送一次。

### 8.2 插入 helper

在 ReplyService 增加：

```python
async def _claim_final_send(self, event, chat_id: str) -> tuple[bool, str]:
    ...
```

返回：

```text
(True, send_key)   可以发送
(False, reason)    不发送
```

### 8.3 行为

- 无 runtime_coordinator：允许发送。
- 无 turn：允许发送。
- 有 turn：构造 send_key。
- claim 成功：允许发送，并把 send_key 存 event extra。
- claim 失败：不发送，reason `send_claim_exists`。

event extras：

```text
astrmai_reply_send_key
astrmai_reply_blocked_reason
```

### 8.4 commit/failed

发送成功后：

```python
await runtime_coordinator.commit_send(chat_id, send_key, outbound_ids)
```

发送异常：

```python
await runtime_coordinator.mark_send_failed(chat_id, send_key, exc)
raise
```

如果没有 outbound ids：

- commit 空列表。
- 不阻断。

### 8.5 分段回复要求

claim 必须在 segment loop 前。

伪结构：

```python
claim_ok, send_key = await self._claim_final_send(...)
if not claim_ok:
    return

outbound_ids = []
try:
    for segment in segments:
        sent = await send(segment)
        outbound_ids.append(extract_id(sent))
except Exception as exc:
    mark_failed(...)
    raise
commit(...)
```

### 8.6 测试用例

- 同一 event/turn 调两次 `handle_reply`，第二次不发送。
- 两个不同 generation 都可发送。
- 分段回复只调用一次 `claim_send`。
- claim 失败不 fallback。
- 发送异常 mark failed。

### 8.7 验证

```text
python -m pytest tests/test_reply_service_refactor.py -q
```

## 9. P0-T7 私聊 pending/generation 验证

### 9.1 目标

确保私聊等待期消息不会因 generation 机制断掉。

### 9.2 文件

主要测试现有路径，尽量不改源码：

```text
astrmai/conversation/attention/gate.py
astrmai/state/private_chat/private_chat_manager.py
```

如果发现入口创建 turn 后 `PRIVATE_WAIT` 路径跳过，则再最小补。

### 9.3 测试策略

构造私聊 event：

- `unified_msg_origin="default:FriendMessage:user-1"`。
- `group_id=""`。
- `sender_id="user-1"`。
- event extra 已有 `astrmai_turn_identity`。

mock PrivateChatManager：

- 记录 `signal_new_message(sender_id, msg, chat_id)`。

断言：

- `AttentionGate.process_event()` 返回 `PRIVATE_WAIT`。
- `signal_new_message` 被调用。
- 如果用真实 manager，pending_messages 增加。

### 9.4 generation 对齐测试

在 message_entry 层测：

- 私聊 wait 消息不是 command/duplicate/control。
- `prepare_conversation_turn` 被调用。
- 之后 attention 返回 `PRIVATE_WAIT`。

### 9.5 验证

```text
python -m pytest tests/unit/state/test_private_chat_manager_migrated.py -q
python -m pytest tests/original_ported/test_attention_private_chat_ported.py -q
python -m pytest tests/test_system2_runner_refactor.py -q
```

## 10. P0-T8 freshness 私聊过期规则

### 10.1 目标

私聊可以慢，但 stale generation 不可借 direct timeout 放行。

### 10.2 文件

```text
astrmai/conversation/execution/reply_freshness.py
```

### 10.3 实现

在 `_allow_direct_reply_timeout()` 里：

- 如果 event 有 `astrmai_turn_identity` 且 runtime_coordinator 可用：
  - `is_current_turn=False` 返回 False。
  - current 则继续旧逻辑。
- 无 turn 保持旧逻辑。

### 10.4 测试

在 reply freshness 或 reply service 测试中覆盖：

- private stale turn overdue 不放行。
- private current turn overdue 放行。
- no turn private overdue 保持旧行为。

### 10.5 验证

```text
python -m pytest tests/test_reply_service_refactor.py -q
```

## 11. P0-T9 回归测试文件

### 11.1 文件

新增：

```text
tests/regression/test_conversation_turn_generation_p0.py
```

### 11.2 必测场景

1. 私聊旧回复迟到。
2. 私聊等待期续聊。
3. 私聊 WAIT/IGNORE 仍强制 REPLY。
4. 群聊重复 final。
5. 控制消息不推进 generation。
6. 群聊旧回复过期。
7. 分段回复共享 claim。

### 11.3 测试风格

优先 mock 外部边界：

- 不调真实 LLM。
- 不连 QQ/NapCat。
- 不写真实 DB。
- 使用 fake event/fake runtime/fake sender。

### 11.4 验证

```text
python -m pytest tests/regression/test_conversation_turn_generation_p0.py -q
```

## 12. P0 总体验证

### 12.1 最小验证

```text
python -m pytest tests/unit/conversation/test_turn_identity.py -q
python -m pytest tests/unit/infrastructure/test_chat_runtime_generation.py -q
python -m pytest tests/unit/presentation/test_message_entry_turn_generation.py -q
python -m pytest tests/regression/test_conversation_turn_generation_p0.py -q
```

### 12.2 相关验证

```text
python -m pytest tests/test_reply_service_refactor.py -q
python -m pytest tests/test_system2_runner_refactor.py -q
python -m pytest tests/unit/state/test_private_chat_manager_migrated.py -q
python -m pytest tests/original_ported/test_attention_private_chat_ported.py -q
```

### 12.3 构建验证

```text
python -m compileall astrmai main.py config.py
git diff --check
```

### 12.4 全量验证

```text
python -m pytest -q
```

如果全量耗时过长，可先跑相关验证，但最终合入前建议全量。

## 13. P0 失败排查指南

### 13.1 正常回复突然不发送

检查：

- event 是否有 turn。
- current_generation 是否被控制消息推进。
- `astrmai_non_conversational` 是否误标。
- thread_id 是否不一致。
- claim_send 是否提前被调用。

命令：

```text
rg -n "advance_generation|astrmai_turn_identity|claim_send|stale_generation|send_claim_exists" astrmai tests
```

### 13.2 分段回复只发第一段

检查：

- 是否在 segment loop 内 claim。
- 是否每段都重新做 stale check。
- 发送失败是否 mark failed 后 raise。

修复方向：

- claim 移到 loop 外。
- P0 不做 per-segment 中断。

### 13.3 私聊等待断掉

检查：

- `AttentionGate` 私聊路径是否仍调用 `signal_new_message`。
- non conversational guard 是否误拦私聊。
- `PrivateChatManager.is_bot_waiting` 是否被错误重置。
- generation 是否推进但 pending 没写。

### 13.4 群聊过度误杀

P0 预期会保守误杀部分同群慢回复。如果过多：

- 确认是否为不同用户/话题导致。
- 记录为 P1 per-thread 的输入。
- 不在 P0 临时加入复杂 thread 规则。

### 13.5 控制消息仍污染链路

检查：

- 测试插件是否设置 `astrmai_non_conversational`。
- AstrMai guard 是否在 `record_and_dispatch_attention` 前。
- GroupWait 是否在 guard 前被调用。

如果 GroupWait 仍被控制消息触发，需要把 guard 移到 GroupWait 前，或测试插件自身 stop。

## 14. P1 执行入口

P1 必须在 P0 稳定后执行。

进入条件：

- P0 全部测试通过。
- 真实 QQ 灰度至少跑过 smoke。
- stale_generation 日志显示 P0 保守策略影响群聊体验。
- 用户确认需要同群多线程并行。

P1 第一任务不是改 GroupWait，而是先写 GroupThreadResolver 单测。

## 15. P1 执行步骤摘要

### 15.1 P1-T1 GroupThreadResolver

新增：

```text
astrmai/conversation/threading/group_thread_resolver.py
tests/unit/conversation/test_group_thread_resolver.py
```

先单测 resolver，不接入主链路。

### 15.2 P1-T2 GroupWait 内部状态升级

修改：

```text
astrmai/state/group_wait/group_reply_wait_manager.py
```

先保持旧 API 兼容，内部换成 `chat_id -> thread_id -> state`。

### 15.3 P1-T3 恢复条件收紧

先写失败测试：

- 同 target 普通消息不恢复。
- reply_to bot message 恢复。

再改 `handle_incoming_message`。

### 15.4 P1-T4 主链路接入 thread_id

入口 group turn 创建改为：

```text
thread_id = group_thread_resolver.resolve(event).thread_id
```

resolver 异常 fallback 到 `chat_id`。

### 15.5 P1 验证

```text
python -m pytest tests/unit/conversation/test_group_thread_resolver.py -q
python -m pytest tests/unit/state/test_group_reply_wait_threaded.py -q
python -m pytest tests/regression/test_group_thread_concurrency_p1.py -q
python -m pytest tests/regression/test_conversation_turn_generation_p0.py -q
python -m compileall astrmai main.py config.py
git diff --check
```

## 16. P2 执行入口

P2 在 P0/P1 行为稳定后执行，重点不是改语义，而是可观测和灰度。

进入条件：

- P0/P1 相关测试稳定。
- 需要线上观察 stale/claim/wait 频率。
- 需要真实 QQ 压测脚本辅助判断。

## 17. P2 执行步骤摘要

### 17.1 Summary trace

记录：

- turn_created。
- generation_advanced。
- reply_blocked。
- send_claimed。
- wait_resumed。

不记录正文。

### 17.2 Debug trace

加开关，关闭时不构造 payload。

### 17.3 压测

新增：

```text
tests/regression/test_conversation_concurrency_pressure.py
```

### 17.4 QQ 验收脚本

在 Probe/Orchestrator 文档或插件中补：

- 快速私聊。
- 群聊交错 @。
- 控制消息隔离。
- 模型迟到。

## 18. 提交建议

P0 推荐拆 5 个提交：

```text
test/runtime: cover turn generation and send claim
feat/runtime: add turn identity generation and send claim
feat/ingress: bind conversation turn at message entry
fix/reply: block stale replies and duplicate finals
test/regression: cover P0 conversation concurrency
```

如果用户要求单 commit，可以最终 squash，但开发过程仍按小闭环验证。

## 19. 最终执行完成定义

P0 完成必须同时满足：

- 新增/修改测试通过。
- 相关旧测试通过。
- compileall 通过。
- git diff --check 通过。
- 文档中的 P0 验收场景有测试覆盖。
- final 回复 stale 时不会发送 fallback。
- 私聊强回复和 private wait 未被破坏。

P1 完成必须同时满足：

- GroupWait per-thread 测试通过。
- 同群双线程并发测试通过。
- P0 私聊测试仍通过。
- 可关闭灰度回到 P0。

P2 完成必须同时满足：

- trace 不泄露正文。
- debug off 不构造大 payload。
- pressure tests 通过。
- 真实 QQ 灰度记录可审查。
