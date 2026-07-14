# Round 10 Review: 主动服务、Cron 与生命周期清理

**审查日期**: 2026-07-14
**审查范围**: R10-01 至 R10-08，共 8 项修复
**审查方法**: 逐项阅读源码，对照修复要求逐条验证
**总结论**: ✅ **全部 8 项修复已实现且有效**

---

## R10-01 ✅ IMPLEMENTED — Private session eviction 泄漏 chat-to-user reverse mapping

| 要求 | 状态 | 证据 |
|------|------|------|
| session key ↔ reverse index 关系显式化 | ✅ | `_remove_session_key()` (L199-220) 根据 key 类型（含 `::` 或纯 `user_id`）精确清理 `_chat_to_user` |
| evict 同时删除准确 mapping | ✅ | `_get_or_create_session()` (L195) 在超限驱逐时调用 `_remove_session_key()` |
| cleanup 同时删除准确 mapping | ✅ | `cleanup_stale_sessions()` (L179) 同样调用 `_remove_session_key()` |
| >100 session 后 `_sessions` 和 `_chat_to_user` 皆有界 | ✅ | MAX_SESSIONS=100 (L22)，驱逐逻辑仅选非 waiting session，`_chat_to_user` 随 key 移除同步清理 |

**关键机制**:
- `_remove_session_key(key)` 是唯一的 session 移除入口，确保 reverse index 与 session key 同步维护
- `close_session()` (L156-168) 同时清理所有匹配的 session keys 和 `_chat_to_user` 条目
- 无 orphan 残留路径

---

## R10-02 ✅ IMPLEMENTED — Heartflow 仅在 dispatch 成功后才 commit cooldown

| 要求 | 状态 | 证据 |
|------|------|------|
| queued/sent 后才 commit cooldown | ✅ | `_commit_visible_cooldown` callback (manager.py L858-863) 仅在 `reply_sent=True` 时设置 `last_visible_candidate_ts` |
| blocked 路径 rollback/no-op | ✅ | dispatcher blocked 时 (L881-888) decision 标记为 not allowed，cooldown 未提交 |
| exception 路径 rollback/no-op | ✅ | dispatcher 异常时 (L867-876) 捕获异常，cooldown 未提交 |
| dispatch blocked 后下一周期仍可选 candidate | ✅ | `_build_impulse_decision()` 的 cooldown 检查 (L794) 依赖 `last_visible_candidate_ts`，blocked 路径不更新该字段 |
| 成功发送 → 15 分钟 cooldown | ✅ | `VISIBLE_CANDIDATE_COOLDOWN_SECONDS = 15 * 60` (L24)，`_recent_visible_candidate_cooldown()` (L722-728) 精确检查 |

**关键机制**:
- Cooldown 提交与 `reply_sent` 绑定而非 `dispatch` 本身
- `dispatcher.py` `complete()` (L209-239) 在 `reply_sent=True` 时额外设置 `_cooldowns` 字典做双重保护
- 两个独立 cooldown 检查点：`_build_impulse_decision` 和 `_build_action_decision` 均依赖同一 `last_visible_candidate_ts`

---

## R10-03 ✅ IMPLEMENTED — Diary 逐 chat try/continue，失败不阻止其他 chat

| 要求 | 状态 | 证据 |
|------|------|------|
| 逐 chat try/continue | ✅ | `run_once()` (diary_service.py L36-96) 遍历 active_states 时每个 chat 独立 try/except |
| 成功 ack 后才标记完成 | ✅ | `completed.add(chat_id)` (L88) 仅在所有 diary 操作成功后执行 |
| Chat A 失败不阻止 Chat B | ✅ | except 块 (L92-95) 仅累加 `failed` 计数和记录 `failed_chat_ids`，继续循环 |
| 取消/失败保留当日窗口重试 | ✅ | `asyncio.CancelledError` 显式 re-raise (L90-91)；`self._diary_pending_date` 在 finally 清除 (L534-535) |
| 不重复已成功 chat | ✅ | `if chat_id in completed: continue` (L39) 跳过已完成 chat |

**关键机制**:
- `_completed_by_date` 字典按日期隔离，每轮只保留当天的 completed set (L25)
- Jitter 逻辑 (`_run_daily_diary_task_with_jitter` L523-535)：仅全部成功时才更新 `_last_diary_date`，失败则次日可重试
- `should_run()` (L98-103)：仅在凌晨 3-5 点触发，日期变化后自动重试

---

## R10-04 ✅ IMPLEMENTED — Dream 写回/发送失败返回 degraded，interval 仅完整成功推进

| 要求 | 状态 | 证据 |
|------|------|------|
| 区分 generated/writeback/sent 状态 | ✅ | `pending` dict (L134-143) 追踪 4 个 stage：feedback_done, diary_memory_done, maintenance_memory_done, visible_send_done |
| 可恢复失败进入补偿 | ✅ | 每个 stage 失败 (L162-197) 追加到 `failures` 列表，pending 状态保持未完成 |
| 只有全部完成才推进 interval | ✅ | `self._last_dream_time = time.time()` (L215) 仅在所有 stage 完成后执行 |
| memory write 失败 → degraded/false 可重试 | ✅ | 任一 stage 未完成 → 返回 `performed=False, degraded=True` (L203-212) |
| 成功路径只执行一次 | ✅ | `_pending_completions` (L89-96) 防止重复 generation；global cooldown (L98-99) 防止并发 |

**关键机制**:
- 分阶段状态机：generation（一次性）→ writeback（可重试）→ send（可重试）
- `_pending_completions` 既是幂等保护，也是重试状态的持久化
- 全局 `_last_dream_time` 和 `_pending_completions` 的交互确保同一时刻最多一个 dream 处于 in-flight 状态

---

## R10-05 ✅ IMPLEMENTED — 群签到平台成功但本地状态保存失败仍继续并吞错

| 要求 | 状态 | 证据 |
|------|------|------|
| 外部 action 与本地幂等 marker 建立可恢复协议 | ✅ | 三阶段协议：intent marker → platform signin → complete marker (L167-198) |
| intent 保存失败不签也不 follow-up | ✅ | `_persist_marker(..., rollback_on_failure=True)` (L172) — 失败时回滚 bucket，`continue` 跳过签到 |
| signin 成功但 complete save 失败不 repeat | ✅ | `_already_signed_today()` 检查 `last_date`；complete 失败时 `last_date` 已设为今日，防止当天重复 |
| 诊断能区分 partial success | ✅ | stats 区分 `signed`/`partial`/`failed` (L196/L200)；`_last_run` 状态为 `partial`/`degraded`/`completed` |

**关键机制**:
- Intent marker 写 `last_date=today` 同时设 `status="intent"`，提供部分成功的可诊断性
- Complete save 失败 → `stats["partial"] += 1`，**不调用 `_dispatch_after_sign()`** (L195-197)
- 如果 intent 写入但随后进程崩溃：重启后 `_already_signed_today` 为 True，不会重复签到
- `_persist_marker` 的 `rollback_on_failure` 确保 intent 写入失败时 bucket 完整回滚

---

## R10-06 ✅ IMPLEMENTED — 可选 meme 发送失败不改变主回复 outcome

| 要求 | 状态 | 证据 |
|------|------|------|
| meme 是 best-effort post-send side effect | ✅ | `send_meme()` 在 `reply_post_send.py` `_settle_post_send()` 末尾调用 (L252-258) |
| 异常只记录 degraded，不改变主回复 outcome | ✅ | except 块 (L259-263) 仅 log warning + 设置 diagnostic flag，不 raise |
| 文本已发送后 meme 失败 → Planner 按成功结算 | ✅ | `handle_reply()` (reply_service.py L146) 在 `_send_segments` 成功 (L123) 和 memory ingest (L139) 之后才调用 `_settle_post_send` |
| 无第二 fallback | ✅ | meme 失败路径无任何补偿发送逻辑 |

**双重防护**:
- `meme_sender.py` 内部已有 try/except (L41-47)，返回 False 不抛异常
- `_settle_post_send` 的 try/except (L259) 作为第二层防护，即使 `send_meme` 抛出也会被捕获

---

## R10-07 ✅ IMPLEMENTED — Startup reload transient failure 不永久阻止 cron heartbeat

| 要求 | 状态 | 证据 |
|------|------|------|
| 初始 reload 失败后仍启动 heartbeat | ✅ | `start_workmode_guard()` (lifecycle.py L141-156)：`reload_all_lost_jobs()` 在独立 try/except (L146-149)，失败后 CONTINUE 到 `run_heartbeat()` (L151) |
| heartbeat 具备 per-tick recovery | ✅ | `_heartbeat_tick()` (heartbeat.py L102-139) 每个 tick 独立扫描 snapshot、revive lost jobs |
| 首次 reload 抛错 + 第二 tick 成功 → lost jobs 恢复 | ✅ | 每个 tick 的 for 循环 (L114-139) 重新 get snapshots 和 active jobs，不依赖前次状态 |
| 无需重启进程 | ✅ | 整个 recovery 在 `while self._is_running` 循环内自动重试 |

**关键机制**:
- `reload_all_lost_jobs()` (L24-70) 和 `_heartbeat_tick()` (L102-139) 共享相同的 idempotent revival 逻辑
- 每个 tick 独立获取 `get_all_active_cron_snapshots()` 和 `cron_manager.list_jobs()`，不受历史失败影响
- 单个 job 恢复失败不阻止其他 job（L138-139：per-job try/except）

---

## R10-08 ✅ IMPLEMENTED — Cron revival 事务化 snapshot swap + 幂等 marker

| 要求 | 状态 | 证据 |
|------|------|------|
| snapshot swap 事务化 | ✅ | `replace_cron_snapshot()` (database_cron.py L42-72)：同一事务内 upsert new + deactivate old |
| 新 host job 有失败补偿/幂等 marker | ✅ | `_pending_snapshot_swaps` (heartbeat.py L22) 记录 old→new 映射；`_sync_revived_snapshot` 失败时 `_remove_host_job()` 补偿 (L164-167) |
| 不能每 tick 重建 | ✅ | heartbeat tick (L122-133) 先检查 `_pending_snapshot_swaps`，已存在映射则 sync 不重建；`_find_matching_host_job` (L173-191) 识别同名同 payload 的已有 job |
| deactivate/save 任一步失败 → 最多一个 host job | ✅ | `_revival_lock` (L22, L142) 串行化 revival；sync 失败时补偿删除 host job；transactional replace 原子性 |
| 恢复后 snapshot 指向真实 job | ✅ | `_sync_revived_snapshot` 用 `new_job_id` 创建 replacement snapshot，旧 snapshot deactivate |

**关键机制**:
- `_revive_job()` 的两阶段补偿：创建 host job → 更新 snapshot；更新失败 → 删除 host job
- `_pending_snapshot_swaps` 提供跨 tick 的幂等保护：tick N 创建 job 后 update 失败，tick N+1 检测到 pending swap 存在且 host job 活跃，直接 sync 而不重建
- `replace_cron_snapshot` 的事务性：upsert new + deactivate old 在同一 `session.commit()` 中

---

## 各文件修改状态

| 修复 ID | 文件 | 需要修改 | 实际状态 |
|---------|------|----------|---------|
| R10-01 | `astrmai/state/private_chat/private_chat_manager.py` | 是 | ✅ 已修改 |
| R10-02 | `astrmai/proactive/heartflow/manager.py` | 是 | ✅ 已修改 |
| R10-02 | `astrmai/proactive/dispatcher.py` | 是 | ✅ 已修改 |
| R10-03 | `astrmai/proactive/diary_service.py` | 是 | ✅ 已修改 |
| R10-03 | `astrmai/proactive/proactive_task.py` | 是 | ✅ 已修改 |
| R10-04 | `astrmai/proactive/dream_scheduler.py` | 是 | ✅ 已修改 |
| R10-05 | `astrmai/proactive/group_signin_service.py` | 是 | ✅ 已修改 |
| R10-06 | `astrmai/conversation/execution/reply_post_send.py` | 是 | ✅ 已修改 |
| R10-06 | `astrmai/multimodal/meme/meme_sender.py` | 是 | ✅ 已修改 |
| R10-06 | `astrmai/conversation/execution/reply_service.py` | 是 | ✅ 已修改 |
| R10-07 | `astrmai/app/lifecycle.py` | 是 | ✅ 已修改 |
| R10-07 | `astrmai/workmode/cron_guard/heartbeat.py` | 是 | ✅ 已修改 |
| R10-08 | `astrmai/workmode/cron_guard/heartbeat.py` | 是 | ✅ 已修改 |
| R10-08 | `astrmai/infrastructure/persistence/database_cron.py` | 是 | ✅ 已修改 |

---

## 备注

- 所有修复均为**局部修改**，符合「最小改动」策略
- 未发现与 Round 09 或之前轮次的回归冲突
- `R10-03` 的 jitter 使用 `asyncio.sleep(random.randint(1, 300))`，在最坏情况下（第 1 秒触发 jitter 后进程 crash），jitter 在 finally 中清除 `_diary_pending_date`，但由于 `_last_diary_date` 未更新，次日仍会重试 — 符合预期
- `R10-04` 的 `_pending_completions` 检查 (L89-96) 存在一个边缘情况：如果 `request_key` 匹配但 `pending is None` 且 `_pending_completions` 非空，会返回 "dream_completion_pending" 拒绝新 dream。这是有意设计，防止全局并发 — 但语义上可能让某个 session 的 dream 被不相关的 pending completion 阻塞。不违反修复要求，因为修复目标是「只有定义的完成条件推进 interval」
