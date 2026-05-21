# P10 状态栏语义说明

本说明用于锁定 6 个聊天状态栏的产品语义与实现边界。

## energy

- `energy` 表示公共社交/出场体力，不是私聊陪伴体力。
- 群聊、主动唤醒、scheduler/proactive 链路会消耗 `energy`。
- 私聊 `FriendMessage` 不消耗 `energy`，这是刻意设计，不是 bug。

## mood

- `mood` 是持久化的聊天情绪状态，范围 `[-1, 1]`。
- 主更新时机是消息进入主聊天链路、Judge 之前的文本情绪分析。
- Judge 中的 `mood_delta` 只作为微调项。
- post-send 的 mood settlement 只负责轻量回弹/收尾，不承担主建模职责。

## social_score

- `social_score` 是关系向量的聚合分数，不是单独拍脑袋生成的标签。
- 更新时机在回复发送后结算。
- 更新时需要结合本轮真实用户消息文本，避免所有互动退化成 `NORMAL_CHAT`。
- 它影响亲密工具、hostile 工具和低 trust 限制。

## think_level

- `think_level` 是单轮脑力预算，不是持久情绪。
- 它控制是否进入更深记忆、readonly tools、以及群聊环境消息的轻量跳过。

## social_intent

- `social_intent` 是这一轮的社交意图主控变量。
- 它决定这轮更偏回答、安慰、观察、回忆、立边界还是反击。
- 它直接限制工具族和动作层级。

## stance

- `stance` 是这一轮的姿态 bias，不是主决策器。
- `guarded/cool` 会对过度活跃、轻佻、侵入式工具做二级约束。
- `warm` 只允许更自然地承接情绪，不会越权绕过 `social_intent`、cooldown、`energy` 或 `social_score`。
