# Agent 06

Agent ID:
`019e6d48-8f8a-7090-8e8a-a0c0b3f440b9`

状态：
已完成

发现：
- `[P1] ChatState 的衰减/重置不是持久一致的。` [chat_state_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/chat_state_service.py:79), [mood_decay.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/mood/mood_decay.py:7), [orm_models.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/persistence/orm_models.py:75), [state_profile_persistence.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/persistence/state_profile_persistence.py:14), [lifecycle.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/lifecycle.py:121), [decay_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/decay_service.py:18)。`ChatState` 明明定义了 `last_reply_time` 和 `last_passive_decay_time`，但落库读回时完全丢失；同时日切重置和后台自然衰减只会标脏，不会像 user profile 那样被后台 flush。结果是重启后/后台衰减后状态不可预测。我本地只读复现实验里，保存前 `energy=0.4`、`mood=0.6`、`last_reply_time=10s 前`、`last_passive_decay_time=2h 前`，读回后再跑 `apply_natural_decay()`，`energy` 被错误恢复到 `0.5`，`mood` 仍停在 `0.6`，本该发生的被动情绪衰减直接丢了。

- `[P2] 私聊 wait 会在“同尾号”的群聊 heartbeat 上串线。` [private_chat_manager.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/private_chat/private_chat_manager.py:95), [private_chat_manager.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/private_chat/private_chat_manager.py:138), [chat_loop_kernel.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/loop/chat_loop_kernel.py:1292), [test_chat_loop_kernel_refactor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_chat_loop_kernel_refactor.py:367)。`get_session_info_by_chat_id()` 在没有显式映射时会把 `chat_id` 最后一个 `:` 后面的片段当成 `user_id`，而 heartbeat 路径在没有 `event` 时会直接按 `chat_id` 查私聊 session。对 QQ 这类“群号/用户号都可能是纯数字”的场景，这会把 `default:FriendMessage:12345` 的等待态误投到 `default:GroupMessage:12345`。我本地只读复现里，这两个 `chat_id` 查出来的是同一个私聊 session。现有测试只覆盖了 friend heartbeat 命中 private wait，没有覆盖这个碰撞分支。

- `[P2] 关系衰减现在有“双真相”，flush 后会把不一致的 `social_score` 和 `relationship_vector` 一起写回。` [chat_state_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/chat_state_service.py:134), [relationship_engine.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/relationship/relationship_engine.py:413), [decay_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/decay_service.py:21), [state_profile_persistence.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/persistence/state_profile_persistence.py:115)。`DecayService` 一边直接把 `profile.social_score` 做 `-1/+1`，一边又让 `relationship_engine.apply_global_decay()` 衰减运行时向量，但没有把衰减后的向量重新同步回 `profile.relationship_vector`，也没有用向量分数覆盖 `profile.social_score`。我本地只读复现里，`run_once()` 后三份值分别变成了 `profile.social_score=19.0`、运行时向量分数 `19.29`、`profile.relationship_vector["social_score"]=20.0`；随后 `flush_message_counters()` 真的会把 `19.0` 和 `20.0` 这对不一致值一起存盘。

补充：
- 没发现“私聊画像直接越权进入高优先级提示”的链路。私聊画像被放进的是 `soft_background`，不是 `system_prompt`、`focus_message` 或 `direct_context`。[context_engine.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/context_engine.py:145), [prompt_refiner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/prompt_refiner.py:956)
- 常规热路径里，情绪、关系、私聊画像本身没有直接互相覆盖：`mood` 只写 `ChatState`，关系主要写 `RelationshipVector/social_score`，私聊触达主要写 `UserProfile` footprint。这里真正不稳的是“后台/重启/回落到持久化读取”后的同步一致性。

假设：
- 第二条我默认“群号和用户号可能重号”是有效场景；如果上游协议能严格保证二者永不相等，这条风险可以降级，但当前代码本身没有做这种边界保护。
- `apply_profile_name`、`observe_user_activity` 等 profile 改动主要靠 15 秒批量 flush，而 `ContextEngine` 在 bundle 降级时会直接读 DB，所以 prompt 消费与持久化目前是“最终一致”而不是“严格一致”。[user_profile_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/user_profile_service.py:150), [user_profile_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/user_profile_service.py:188), [user_profile_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/user_profile_service.py:497), [context_engine.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/context_engine.py:399)

验证：
- 运行了 `PYTHONPATH=C:\\Users\\zlj\\Desktop\\mai\\astrmai_plugin_refactored_final pytest tests/unit/state tests/regression/state -q`，结果 `15 passed`。
- 另外做了 3 段只读临时脚本复现，上面 3 个问题都能稳定打出来，说明它们目前不在这批 state 测试覆盖里。
