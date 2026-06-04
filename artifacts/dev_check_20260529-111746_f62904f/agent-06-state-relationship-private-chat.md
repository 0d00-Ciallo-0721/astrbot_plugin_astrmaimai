# Agent 06

Agent ID:
`019e71b7-dabc-76b1-a793-2f6e995737fc`

状态：
已完成

发现：
- `[P1]` `mood` 更新仍然存在同一 `chat_id` 下的写后读竞争，最终值取决于分析完成顺序，不取决于消息顺序。[chat_state_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/chat_state_service.py:213) 先读 `snapshot_mood` 再 `await` 情绪分析，之后才在 [chat_state_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/chat_state_service.py:97) 里落盘绝对值；整个 read-analyze-write 不是原子区间。只读复现中，先发 `first`、后发 `second`，最终 `final_mood` 仍会被较慢返回的 `first` 覆盖成 `0.4`。
- `[P1]` `social_score` 和 `relationship_vector` 现在仍是分叉真值，外部只改 `social_score` 会在下次读取时被旧向量回滚。[state_profile_persistence.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/persistence/state_profile_persistence.py:65) 从列里读 `social_score`，同时从 `profile_metadata` 读 `relationship_vector`；[relationship_engine.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/relationship/relationship_engine.py:291) 优先从 `relationship_vector` 重建运行时状态；随后 [chat_state_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/chat_state_service.py:141) 又把 `profile.social_score` 覆盖成向量分数。而 [user_ui_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/user_ui_service.py:116) 允许 WebUI 单独修改 `social_score`。直接改 DB 把 `social_score` 从 `20` 改到 `80`，下次 `StateEngine.get_user_profile()` 读回来的仍然是 `20`。
- `[P1]` `user profile` 的缓存与 prompt 消费仍然不一致，外部持久化更新后私聊画像会继续吃旧缓存。[user_profile_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/user_profile_service.py:113) 命中缓存后不会回源刷新，[user_profile_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/user_profile_service.py:493) 生成 prompt bundle 直接读缓存，而 [context_engine.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/context_engine.py:395) 在私聊场景优先使用这个 bundle。只读复现里，持久化层把 `Alice/old` 改成 `Bob/new` 后，连续两次 `get_profile_prompt_bundle()` 仍返回 `Alice/old`。
- `[P2]` 私聊等待态仍然只按裸 `user_id` 建 session，跨 origin 的私聊会串线。[private_chat_manager.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/private_chat/private_chat_manager.py:25) 的 `_sessions` 以 `user_id` 为键，[private_chat_manager.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/private_chat/private_chat_manager.py:34) / [private_chat_manager.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/private_chat/private_chat_manager.py:47) 都把不同 `chat_id` 绑定到同一个 session，[private_chat_manager.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/state/private_chat/private_chat_manager.py:138) 还会在 fallback 时只取 `:FriendMessage:` 最后一段。

验证：
- 运行 `PYTHONPATH=C:\\Users\\zlj\\Desktop\\mai\\astrmai_plugin_refactored_final pytest tests/unit/state tests/regression/state`，结果 `20 passed`。
- 另外做了 3 个只读复现，分别确认了：`update_mood` 并发覆盖、`social_score`/`relationship_vector` 分叉回滚、以及 profile prompt 缓存陈旧。
