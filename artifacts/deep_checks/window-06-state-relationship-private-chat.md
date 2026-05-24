# 窗口 6：State / Relationship / Private Chat

模块：
State / Relationship / Private Chat（`astrmai/state/*`）

职责：
负责用户画像、关系状态、情绪状态、私聊等待与 chat state 的读取、更新和运行时消费。

关键文件：
- `astrmai/state/chat_state_service.py`
- `astrmai/state/private_chat/private_chat_manager.py`
- `astrmai/state/relationship/relationship_engine.py`
- `astrmai/state/user_profile_service.py`
- `astrmai/state/mood/mood_manager.py`
- `astrmai/infrastructure/persistence/orm_models.py`
- `astrmai/infrastructure/persistence/state_profile_persistence.py`

现有测试：
- `tests/unit/state/*`
- `tests/regression/state/*`
- `tests/test_persona_context_refactor.py`
- `tests/original_ported/test_prompt_refiner_focus_layout_ported.py`
- 实跑：`python -m pytest tests/unit/state tests/regression/state -q` -> `10 passed`
- 实跑：`python -m pytest tests/test_persona_context_refactor.py tests/original_ported/test_prompt_refiner_focus_layout_ported.py -q` -> `32 passed`

主要发现：
1. `[高]` 关系四维状态会在每次读 profile 时被重建，关系状态被 `social_score` 反向覆盖。
   - 依据：`astrmai/state/chat_state_service.py:133` 每次 `get_user_profile()` 都会调用 `relationship_engine.load_from_profile()`。
   - 进一步依据：`astrmai/state/relationship/relationship_engine.py:244`、`astrmai/infrastructure/persistence/orm_models.py:79`、`astrmai/infrastructure/persistence/state_profile_persistence.py:43` 所在持久化模型都没有 `relationship_vector` 字段，只能反复用单一 `social_score` 推回四维关系向量。
   - 影响：`astrmai/conversation/planning/planning_input_loader.py:383`、`astrmai/conversation/planning/cognitive_loop.py:625` 都直接消费该向量。
2. `[高]` 私聊等待存在“先到消息丢失”竞态，属于写后读不一致。
   - 依据：`astrmai/state/private_chat/private_chat_manager.py:34` 的 `signal_new_message()` 只有在 `is_bot_waiting=True` 时才 `set()` 事件。
   - 进一步依据：`astrmai/state/private_chat/private_chat_manager.py:47` 的 `wait_for_new_message()` 进入等待前又会 `clear()`；主链中的 `astrmai/app/plugin_facade.py:257`、`astrmai/conversation/execution/followup_manager.py:51` 也没有消费 `pending_messages`。
3. `[中]` 情绪更新把“分析快照”和“提交基线”混用了，静默衰减场景下结果不可预测。
   - 依据：`astrmai/state/chat_state_service.py:195` 用当前 `snapshot_mood` 做分析，再把 `new_value - snapshot_mood` 当 delta。
   - 进一步依据：`astrmai/state/chat_state_service.py:96` 的 `atomic_update_mood()` 提交前会先 `apply_natural_decay()`；`astrmai/state/mood/mood_decay.py:7` 是衰减逻辑入口。

未实现/不完整项：
1. 未发现“private chat 信息越权进入 system 高优先级”的明确缺陷，但现有测试只覆盖它仍在 `final_prompt` 背景理解段，不在 `final_system_prompt`。
2. `user profile` 落盘时机不统一，异常退出前的数据保真没有测试覆盖。
   - 依据：`astrmai/app/lifecycle.py:121` 的 15 秒批量 flush。
3. 现有 state 测试没有覆盖上面 3 个竞态 / 持久化问题。

高风险点：
1. 关系向量被 `social_score` 重建会直接污染 planning / cognitive loop 的实时判断，而不是仅在重启后丢状态。
2. 私聊回复先到后等待的竞态会让真实用户回复被静默错过，直接影响 follow-up 行为。

建议下一步：
1. 先补 `relationship_vector` 持久化或等价快照测试，证明读取 profile 不会覆盖运行时累计关系。
2. 再补 private chat “先到消息”竞态测试和 mood decay 提交基线测试，之后再决定修正等待协议还是改为显式消费 `pending_messages`。
