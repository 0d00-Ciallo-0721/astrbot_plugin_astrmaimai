# Round 08：配置、状态与持久化一致性

数量：9。依赖：Round 01 身份统一、Round 06-07 Memory。

完成标准：热配置传播到 live derived state；持久化签名正确；重载不重放旧事件；state lock 与 decay 结果一致。

## R08-01 / P2：Memory pipeline/maintenance 热配置继续使用旧对象
- 原始 ID：`AM-MEM-06-13`（Dream 部分归 `R09-06`）；验证级别：B。
- 主文件：`astrmai/app/plugin_facade.py`, `astrmai/memory/services/memory_engine.py`, `memory_turn_pipeline.py`。
- 修复边界：刷新 pipeline、summarizer、instant gate、maintenance、tool service 的 config/derived fields；DreamScheduler 不在本项重复修改。
- 回归目标：summary threshold/maintenance 参数下一轮生效；失败回滚恢复所有 memory children。

## R08-02 / P2：画像每消息即时 flush 使用不存在的方法形状
- 原始 ID：Assignment 08 profile flush, `FFA-09-008`；验证级别：A。
- 主文件：`astrmai/state/user_profile_service.py`, `astrmai/infrastructure/persistence/state_profile_persistence.py`。
- 修复边界：统一一参数 profile persistence contract，成功后清 dirty；保留周期 flush 作为容错而非正常路径。
- 回归目标：普通消息即时写入且无 warning；立即崩溃/重载前数据已落盘。

## R08-03 / P1：wakeup cooldown 用单参数调用双参数 `save_chat_state`
- 原始 ID：Assignment 08 wakeup persistence, `AM-LP-10-06`；验证级别：A。
- 主文件：`astrmai/proactive/wakeup_service.py`, `astrmai/infrastructure/persistence/state_profile_persistence.py`。
- 修复边界：能量、reply metadata、next_wakeup_timestamp 以同一 state 可靠保存；completion 不吞确定性签名错误。
- 回归目标：成功 wakeup 后重启仍处于 cooldown；失败发送不扣能量/写 cooldown。

## R08-04 / P2：EventBus stop 保留旧队列并跨 runtime generation 重放
- 原始 ID：`FFA-ENTRY-005`, Assignment 08 EventBus；验证级别：A。
- 主文件：`astrmai/infrastructure/runtime/event_bus.py`, `astrmai/app/bootstrap.py`, `astrmai/app/lifecycle.py`。
- 修复边界：stop 后 drain/replace queue 与 subscriber generation；明确丢弃旧 payload，不让新 runtime 消费。
- 回归目标：带 pending event 重载后旧事件不触发新 subscriber，下一新事件正常处理。

## R08-05 / P3：LaneManager hot refresh 不替换 frozen settings snapshot
- 原始 ID：Assignment 08 lane settings Finding；验证级别：B。
- 主文件：`astrmai/infrastructure/runtime/lane_manager.py`, `astrmai/app/runtime_context.py`。
- 修复边界：refresh 原子重建 lane settings，昵称/debug 和 history sanitation 使用同一版本。
- 回归目标：热更后下一 transcript 使用新昵称/debug；回滚恢复旧 snapshot。

## R08-06 / P2：State hot reload 不刷新 ChatStateService、timeout 和 emotion mapping
- 原始 ID：`FFA-09-004`；验证级别：B。
- 主文件：`astrmai/state/chat_state_service.py`, `private_chat/private_chat_manager.py`, `mood/mood_manager.py`, `astrmai/app/plugin_facade.py`。
- 修复边界：更新 nested service config 并重算 timeout/mapping；不能清空 live session/state。
- 回归目标：energy/mood/private timeout 下一次调用使用新值；连续 refresh 幂等。

## R08-07 / P2：group departure 删除仍有 waiter 引用的 per-chat lock
- 原始 ID：`FFA-09-005`；验证级别：B。
- 主文件：`astrmai/state/chat_state_service.py`, `astrmai/app/plugin_facade.py`。
- 修复边界：锁身份在 holder/waiter drain 前不可替换；cleanup 用 generation/tombstone 防止旧操作复活 state。
- 回归目标：L1 有 holder+waiter 时 group leave 后不创建并行 L2，旧操作不能重建状态。

## R08-08 / P2：relationship maintenance 双重 decay 且只持久化第一次
- 原始 ID：`FFA-09-006`；验证级别：B。
- 主文件：`astrmai/proactive/decay_service.py`, `astrmai/state/relationship/relationship_engine.py`, `astrmai/state/user_profile_service.py`。
- 修复边界：选择一个 canonical decay owner，统一持久化 vector/social score；维护不得伪造 last_seen。
- 回归目标：每周期只衰减一次，runtime/profile/DB 值相同，重启前后结果一致。

## R08-09 / P2：NaN mood 被 clamp 为最大正值并持久化
- 原始 ID：`FFA-09-007`；验证级别：B。
- 主文件：`astrmai/state/mood/mood_manager.py`, `astrmai/state/chat_state_service.py`。
- 修复边界：float 后先 `isfinite`，无效值走 current/local fallback，绝不进入 CAS save。
- 回归目标：NaN/Inf/-Inf 都不改变 mood；有限边界值正常 clamp。
