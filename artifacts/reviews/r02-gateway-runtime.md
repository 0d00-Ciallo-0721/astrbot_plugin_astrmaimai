# 审查报告：astrmai/infrastructure/gateway/ + astrmai/infrastructure/runtime/
> task_id: r12 | 审查时间: 2025-07-17T10:00:00Z

## 概述
- 审查文件数: 21（gateway 11 文件 + runtime 10 文件）
- 发现总数: 15
- 严重: 2 | 中等: 8 | 建议: 5

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `gateway_lane.py:217-262` | **chat_in_lane_result 双重冷却过滤导致 trace 日志与执行不一致。** 方法内部先调用 `_filter_cooldown_attempt_queue()` 获得 `skipped_cooldown_models`/`cooldown_overridden`，随后调用 `_elastic_call_result()` 时传的是原始 `models` 参数而非过滤后的 `attempt_queue`。`_elastic_call_result` 会再次执行 `_filter_cooldown_attempt_queue`，但 trace stage 使用的 `skipped_cooldown_models` 取自第一次（过期）的快照。若两次过滤间有模型解除冷却，trace 会错误地将该模型标记为"被跳过"。 |
| 2 | `gateway_lane.py:48-51` | **_reuse_or_hash 对空字符串失效——"空值持久化"缺陷。** 当 `existing_payload.get(key)` 返回 `""`（空字符串）时，`"" is not None` 求值为 `True`，因此 `_reuse_or_hash` 返回空字符串而非对新内容计算哈希。一旦某次调用写入空哈希（例如初始值为空），后续所有调用无论内容如何变化，该字段（`gateway_system_hash`、`gateway_prompt_hash` 等）永远保持空值，trace 数据永久失能。 |

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 3 | `model_router.py:55-60` + `gateway_policy.py:47-51` | **ModelRouter 与 GatewayPolicyMixin 双冷却系统不一致。** `ModelRouter` 管理自己的 `cooldown_until`（影响 `get_ranked_models` 排序），`GatewayPolicyMixin` 另有独立 `_model_cooldowns` 字典（影响 `_filter_cooldown_attempt_queue` 跳过逻辑）。两者在 `_elastic_call_result` 中均被写入（`report_failure` + `_open_model_cooldown`），但冷却时长策略不同：前者使用自适应 `BASE_COOLDOWN_SEC * consecutive_failures`，后者使用固定 `rate_limit_model_cooldown_sec`/`quota_model_cooldown_sec`。模型可能在路由器层面已解除冷却，但仍被 `_filter_cooldown_attempt_queue` 跳过，或反之。建议统一冷却判定出口。 |
| 4 | `gateway_lane.py:48-63` | **_reuse_or_hash 设计语义不清晰——"首次调用快照" vs "当前内容指纹"。** 该函数强制复用已有哈希值，使得 `_record_event_request_trace` 的 hash 字段（`gateway_system_hash` 等）反映的是首次调用时的内容，而非当前调用。若后续 prompt/system_prompt 发生变化，trace 中 hash 字段仍为旧值，可能误导调试者。应在文档中明确说明这一设计意图，或改为始终以当前内容计算哈希。 |
| 5 | `lane_manager.py:123-126` | **_rotation_reason 在 `_meta_lock` 外读取 `meta` 内字段，模式脆弱。** 虽然 CPython 中 dict 赋值是原子的且写操作整体替换整个 dict，`meta` 持有旧引用保证了快照一致性，但此模式严重依赖 Python 实现细节（非 PEP 保证），且代码注释虽长仍无法消除潜在的维护风险。若将来有人原地修改 dict（如 `meta[key]=value`），将引入 TOCTOU 竞争。建议锁内在 `meta` 退出锁范围前完成所有判读。 |
| 6 | `lane_manager.py:161-167` | **`_cleanup_remote_sessions` 在无锁状态下迭代并删除 `_remote_sessions`。** `get_remote_session_id` 在惰性清理路径调用 `_cleanup_remote_sessions(now)`，该方法构造 `expired` 列表后逐条 `del self._remote_sessions[k]`。虽然自身迭代安全（列表推导），但与 `expire_remote_sessions_for_lane` 和其他地方的 `.items()` / `.keys()` 迭代并发执行时可能触发 `RuntimeError: dictionary changed size during iteration`。建议加锁或将清理逻辑移入 `async with self._lock` 范围。 |
| 7 | `model_router.py:122-145` | **`_sticky_primary` LRU 在并发路径下无保护。** `_resolve_sticky_primary` 被 `get_ranked_models` 调用（而 `get_ranked_models` 又被 `_build_attempt_queue` 从多个协程调用），对 `OrderedDict` 的 `get`/`move_to_end`/`popitem`/`__setitem__` 均无锁保护。CPython GIL 对单字节码操作提供一定原子性，但 `move_to_end` 是多步操作（删除+插入），协程切换可能导致 `_sticky_primary` 处于不一致状态。建议添加 `asyncio.Lock`。 |
| 8 | `gateway_lane.py:344-359` | **`tool_chat_in_lane_result` 循环中未调用 `_filter_cooldown_attempt_queue` 重新评估冷却状态。** `tool_chat_in_lane_result` 在循环开始时构建 `attempt_queue` 并做一次冷却过滤，但后续每次模型失败后仅通过 `_open_model_cooldown` 写入冷却，同一次 `attempt_queue` 遍历中后续模型不会因新冷却而被跳过（因为队列已在循环前固定）。这与 `_elastic_call_result` 的行为一致，但若 `attempt_queue` 很大，新进入冷却的模型仍会被尝试，浪费一次调用。建议在循环顶部添加快速冷却重检。 |
| 9 | `gateway_result.py:79-97` | **`_build_cache_observation` 的 `cache_ready_reasons` 基于 `debug_meta` 而非真实请求状态。** 例如 `"explicit_cache_hint"` 理由使用 `debug_meta.get("request_cache_control", "")`——此字段由调用方在 `_log_usage` 前手动填充，若调用方忘记填充或填充错误，`cache_ready_reasons` 将与实际发送的请求不匹配。`gateway_call.py:215` 和 `gateway_lane.py:478` 两处均正确填充了 `request_cache_control`，但该模式依赖调用方的一致性，缺乏防御性校验。 |
| 10 | `lane_manager.py:67-69` | **`_remote_sessions_ttl` 设为 3600 秒但惰性清理周期为 300 秒，过期条目最多可存活 3300 秒。** 若一条 session 在第 1 秒写入、在第 3601 秒过期，但下次惰性清理在第 300+ 秒才触发，实际 cleanup 发生在第 3600+300=3900 秒，导致 session 比预期多活 300 秒。在 session 密集的场景下，过期映射可能占用内存时间远超设计值。建议将 TTL 与清理周期解耦，或改用定时清理。 |

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 11 | `gateway_policy.py:57-66` | **`_filter_cooldown_attempt_queue` 在全部模型冷却时返回最早解冻模型，但其 `attempt_queue` 已丢失原始排序信息。** 当 `available` 为空且 `skipped` 非空时，返回 `[earliest_model_id], skipped, True`。但 `earliest_model_id` 从 `skipped` 列表选出，而 `skipped` 顺序是模型在原始 `attempt_queue` 中出现顺序——最早解冻的模型可能不是最优健康分模型。建议在返回前对全部冷却模型按解冻时间排序后取最早的一个，而非从 `skipped` 中 `min`。 |
| 12 | `lane_storage.py:109-124` | **`ensure_lane` 在旋转时对 `dialog` lane 调用 `save_lane_history`（写 rolling summary），但在旋转后再次写 `_runtime_meta`。** 第二次写（`async with self._meta_lock` 块）会覆盖第一次写（`save_lane_history` 内部写）的 `_runtime_meta`，丢失第一次写设置的 `lane_rotated=False` 等字段。目前两次写入内容一致（仅 rotated 字段从 True→False），但维护时容易引入差异。建议仅在旋转路径尾部统一写一次 `_runtime_meta`。 |
| 13 | `gateway_lane.py:72-75` | **`_record_event_request_trace` 中 `request_session_id` 始终设为当前请求的 session_id，`request_cache_control` 始终设为当前请求的缓存控制值，但 hash 字段则是"首次即固定"。** 这种"半粘性"行为（部分字段复用、部分字段覆盖）可能让后续调试者困惑：为什么 trace 中的 hash 不随当前请求变化，但 session_id 和 cache_control 却变化？建议统一策略或添加注释说明。 |
| 14 | `model_router.py:167-175` | **`report_success` 会清除冷却（`cooldown_until = 0.0`），但仅基于 router 内冷却状态，不清理 `GatewayPolicyMixin._model_cooldowns`。** 若一个模型在 GatewayPolicyMixin 侧的冷却中，但 ModelRouter 侧已无冷却（或在 router 冷却中被成功调用），router 侧的清除不会传播到 policy 侧。该模型仍需等待 policy 侧冷却到期才能被 `_filter_cooldown_attempt_queue` 放行。建议在 `report_success` 成功时通过回调或共享事件清理双端冷却。 |
| 15 | `gateway_call.py:166-171` | **`_elastic_call_result` 在重试循环中对 `is_fatal` 的模型调用 `break`，但该模型仍在 `attempt_queue` 中，如果它并非最后一个模型，后续正常模型将不会被执行。** 虽然 fatal 错误（如 429）建议立即跳过当前模型，但 `break` 仅跳出 `for attempt in range(max_retries + 1)` 循环，外层 `for model_id in attempt_queue` 会继续下一个模型——这是正确的，但 `break` 和 `continue` 易于混淆，建议添加行内注释明确 `break` 跳出的是内层重试循环。 |

## 亮点

1. **GatewayLaneMixin 与 GatewayCallMixin 职责边界清晰。** `GatewayCallMixin` 封装无状态的 LLM 调用管线（重试、冷却、健康评分上报），`GatewayLaneMixin` 在其之上叠加 lane 生命周期管理（旋转检测、session 亲和性、trace 日志）。`model_router.py` 的 `ModelRouter` 将调度策略独立为无状态路由器，便于单测和替换。

2. **`_rotation_reason` 锁外快照读取的注释详尽**，虽模式脆弱但团队已充分理解其并发语义并留下文档说明。

3. **模型冷加载与惰性清理设计务实：** `_remote_sessions` 的惰性清理（每 300s 扫描一次）避免了在高频调用路径上引入额外锁开销，平衡了性能与内存安全。

4. **`GatewayResultMixin._build_cache_observation` 的日志聚合设计良好**，将缓存就绪理由、命中证据等观测信息结构化输出，便于后续 dashboard 消费。

## 总结

`gateway/` 和 `runtime/` 两个模块整体架构清晰，关注点分离合理。`GatewayCallMixin` 和 `GatewayLaneMixin` 的职责边界分明，`ModelRouter` 的独立设计值得肯定。

**最关键的发现**是两个冷却系统（`ModelRouter.cooldown_until` vs `GatewayPolicyMixin._model_cooldowns`）并行运行但互不感知，可能产生不一致的行为 — 模型在一边解冻却在另一边被阻塞，或反向。建议将冷却判定统一到一个出口，或将 `_filter_cooldown_attempt_queue` 改为同时检查两边的冷却状态。

**其次重要**的是 `chat_in_lane_result` 的双重冷却过滤导致 trace 日志可能记录过时状态的问题。虽然不影响执行正确性（实际执行由 `_elastic_call_result` 内部第二次过滤决定），但 trace 日志作为调试依据，其可信度会被削弱。

**第三**是 `_reuse_or_hash` 的空字符串 bug — 该问题会导致 hash 字段永久空置，削弱 trace 的可用性。修复成本极低（将 `if existing is not None` 改为 `if existing` 即可），建议优先处理。
