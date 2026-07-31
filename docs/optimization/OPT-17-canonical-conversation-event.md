# OPT-17 规范会话事件（Canonical ConversationEvent）

状态：**代码完成，待线上 shadow 验收** ｜ 优先级：P0 ｜ 依赖：无 ｜ 来源：MaiBot 群共享时间线、Group Chat Plus 身份显式化、AngelHeart 批次聚合

## 目标

- 建立 AstrMai 唯一的会话事件语义，统一入站消息、互动、图片、引用、撤回和 Bot 已提交输出。
- 不再让 `NormalizedEvent`、`DialogueSegment`、SQL `MessageLog` 和 trace 各自维护不同字段口径。
- 为后续 TurnTarget、发送后提交、参与判定和群聊记忆隔离提供稳定 ID 与来源证据。
- 保留群共享可见时间线，同时让所有人物身份以稳定平台 ID 表达。

## 基线证据

- `conversation/attention/event_normalizer.py` 的 `NormalizedEvent` 已有 sender、文本、@Bot、回复 Bot 和图片字段，但缺稳定 event ID、@用户列表、引用事件 ID、topic epoch、provenance 和 source event IDs。
- `conversation/attention/group_dialogue_store.py` 的 `DialogueSegment` 已包含大部分高级字段，说明无需再创造长期并行的第四套消息对象。
- `infrastructure/persistence/orm_models.py` 的 `MessageLog` 仅保存 group/sender/content/timestamp/processed，无法可靠重建高活跃群聊的 actor、target 和因果关系。
- 当前多处通过 event extra 临时携带字段；插件重载、跨进程或数据库回放后这些信息消失。

## 目标契约

建议新增语义契约 `ConversationEvent`，具体文件位置可在实现前按项目分层确认，字段至少包括：

```text
schema_version
event_id
platform_message_id
chat_id / chat_kind / group_id
timestamp / sequence
actor_id / actor_name / actor_role / is_bot
visible_text / rich_text / message_kind
reply_target_event_id
reply_target_actor_id / reply_target_actor_name
quote_event_id
at_actor_ids
topic_epoch
causal_parent_event_id
source_event_ids
provenance
image_refs / attachment_refs
interaction_kind
recalled / outcome
```

约束：

- `event_id` 在同一 `chat_id` 内稳定且可去重；平台 message ID 存在时优先使用，不存在时使用可重现的组合键。
- `actor_id` 是身份真源；昵称只用于展示。
- 互动事件和无文本媒体也必须进入时间线，不能因 `visible_text` 为空而丢失。
- `source_event_ids` 有序去重，用于说明本轮上下文来自哪些原始消息。
- `provenance` 使用枚举或受控字符串，不接受任意用户文本。

## 实施步骤

1. **先写契约测试**
   - 同一原始事件重复规范化得到相同 event ID。
   - 两个昵称相同但 QQ ID 不同的用户不得合并。
   - 改名不改变历史 actor ID。
   - 回复、@、图片、戳、撤回和 Bot 输出字段能够往返序列化。
2. **定义单一语义模型**
   - 复用 `DialogueSegment` 已有字段命名，避免平行概念。
   - 给 `NormalizedEvent` 增加适配器，而非让所有调用点立即换类型。
   - 明确 immutable 字段与运行时 enrichment 字段。
3. **稳定事件 ID**
   - 优先读取 AstrBot/OneBot message ID。
   - notice/poke 等无 message ID 事件使用 `chat_id + actor_id + target_id + timestamp bucket + kind`，并记录生成来源。
   - Bot 输出 event ID 由 send commit 生成，不从 Planner 草稿生成。
4. **持久化迁移**
   - 二选一并在实施前写 ADR：扩展 `MessageLog`，或新增只承载同一语义契约的 append-only 表。
   - 先双写旧 `MessageLog` 与新结构，不修改旧管理页读取。
   - 增加唯一索引与 schema version。
   - 数据库迁移必须可重复执行，旧库缺列时安全升级。
5. **读路径 shadow**
   - 构造当前群聊窗口时同时读取旧/新路径，仅比较 event ID、actor、文本和数量。
   - trace 记录差异，不直接切换用户行为。
6. **切换群聊时间线输入**
   - `group_dialogue_store` 改为消费规范事件适配器。
   - 删除重复的事件 ID、target 和 provenance 二次推断。
   - 旧 extra 保留一个发布周期作为兼容读。
7. **被动媒体与系统互动**
   - 图片未完成视觉识别时仍写 `[图片]` 类型事件和稳定 image reference。
   - 外部插件回执、撤回、戳、转发卡片分别使用明确 `message_kind`。

## 测试矩阵

- 单元：ID 生成、序列化、字段默认值、受控枚举、source 去重。
- 集成：AstrMessageEvent → NormalizedEvent → ConversationEvent → DialogueSegment → SQL → 回放。
- 幂等：相同 message ID 重复投递只保留一条。
- 兼容：旧数据库、缺列数据库、旧 trace、旧 extra。
- 并发：同会话同时到达多条消息，sequence 单调且不会覆盖。
- 数据质量：空文本图片、At+图片、reply+At、peer poke、bot poke、撤回。

## 观测字段

- `conversation_event_schema_version`
- `event_id_source`
- `canonical_event_write_status`
- `canonical_event_duplicate`
- `legacy_shadow_match`
- `legacy_shadow_diff_fields`
- `timeline_append_ms`

默认不记录完整用户原文，只记录长度、哈希和结构字段。

## 验收标准

- 所有群聊入站消息与 Bot 已提交输出都有非空 event ID、actor ID 和 chat kind。
- 规范事件与 `DialogueSegment` 的 actor/target/source/topic 字段一致率 100%。
- 重复投递不会产生重复时间线节点。
- shadow 运行至少 24 小时后，旧/新窗口事件数量与身份匹配率达到 99.9% 以上；剩余差异有可解释类型。
- 旧管理页和旧数据库读取在迁移期保持可用。

## 风险与回退

- 数据迁移风险高：必须先双写和 shadow read，禁止直接删除旧列或旧表。
- notice 无平台 message ID 时存在碰撞风险：组合 ID 必须带 kind、actor、target 和高精度时间，并记录 collision 指标。
- 新结构写入失败不得阻塞主回复；降级写旧路径并安排 repair。
- 回退只关闭新读路径，不删除已写入的新事件。

## 完成记录

- 已新增不可变 `ConversationEvent` 契约，并在规范化入口一次性生成稳定 event ID、actor、reply/quote/@、topic、source、provenance、图片与互动字段。
- 已让 `GroupDialogueStore` 直接消费规范事件，并在同一会话锁内按 event ID 幂等去重。
- 已扩展现有 `MessageLog`，采用兼容双写而非新增平行消息表；数据库迁移序列已推进至 v57，并为 event ID 建立索引。
- 已将规范事件接入 `EventNormalizer`、Attention Gate、消息日志仓储与学习消息写入路径；旧 extra 与旧调用签名继续兼容。
- Red/Green 回归覆盖稳定 ID、无平台 ID 回退、reply/@/图片/source 字段、时间线幂等、SQL 投影与迁移顺序。
- 已通过相关回归：Attention + canonical + store 93 项，持久化与记忆相关 24 项；当前阶段无新增失败。
- 尚未完成：线上 24 小时 shadow 差异统计、旧库真实副本迁移演练、回退开关演练。这些统一纳入 OPT-24 发布验收。
