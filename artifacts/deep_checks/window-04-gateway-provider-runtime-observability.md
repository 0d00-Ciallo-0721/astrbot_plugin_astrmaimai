# 窗口 4：网关 / Provider / Runtime 观测

模块：
网关 / Provider / Runtime 观测（`astrmai/infrastructure/gateway/*`、`astrmai/infrastructure/runtime/*`、`astrmai/infrastructure/context_economy/*`）

职责：
负责 lane/session/cache 相关 request 组织、gateway result 观测、trace/hash 记录、reverse session 处理，以及 context economy 模板与指标支撑。

关键文件：
- `astrmai/infrastructure/gateway/gateway_lane.py`
- `astrmai/infrastructure/gateway/gateway_result.py`
- `astrmai/infrastructure/gateway/gateway_call.py`
- `astrmai/infrastructure/gateway/model_gateway.py`
- `astrmai/infrastructure/runtime/lane_manager.py`
- `astrmai/infrastructure/runtime/lane_storage.py`
- `astrmai/infrastructure/runtime/reverse_session.py`
- `astrmai/infrastructure/context_economy/center.py`
- `astrmai/infrastructure/context_economy/prompt_templates.py`
- `main.py`

现有测试：
- `tests/test_gateway_context_passthrough_refactor.py`
- `tests/test_main_reverse_session_hook_refactor.py`
- `tests/test_main_reply_request_trace_refactor.py`
- `tests/test_reverse_session_refactor.py`
- `tests/test_context_economy_refactor.py`
- `tests/test_prompt_metrics_compare_refactor.py`
- 实跑：`python -m pytest tests/test_gateway_context_passthrough_refactor.py tests/test_main_reverse_session_hook_refactor.py tests/test_main_reply_request_trace_refactor.py tests/test_reverse_session_refactor.py tests/test_context_economy_refactor.py tests/test_prompt_metrics_compare_refactor.py -q`
- 结果：`31 passed`

主要发现：
1. `prefix_hash` / `semantic_system_hash` / `stable_prefix_hash` 口径不统一，同一请求内就会混用。
   - 依据：`astrmai/conversation/planning/context_engine.py:205` 把 `prefix_hash` 和 `semantic_system_hash` 都设成 `frozen_prefix` 的 MD5。
   - 进一步依据：`astrmai/infrastructure/context_economy/center.py:440` 的 `stable_prefix_hash` 是 template/schema/persona + stable prefix 的 SHA1；`gateway_lane.py:439` 使用 `effective_prefix_hash`，`gateway_lane.py:491` 又写 `stable_prefix_hash`。
2. gateway 侧的 `cache_ready` / `cache_ready_reasons` 观测失真。
   - 依据：`astrmai/infrastructure/gateway/gateway_result.py:53` 的 `_build_cache_observation()` 依赖 `request_cache_control`、`request_session_id`、`prefix_stable`、`provider_visible_hash_stable`、`cache_affinity_enabled`、`cached_usage_supported`。
   - 进一步依据：`astrmai/infrastructure/gateway/gateway_call.py:211` 和 `astrmai/infrastructure/gateway/gateway_lane.py:436` 的真实 `_log_usage()` 调用并没有把这些请求级字段完整并回去。
3. `cache_affinity_enabled` / `cached_usage_supported` 没有接进真实主链。
   - 依据：`astrmai/conversation/planning/planner.py:328` 读取这两个字段，但在生产链里找不到稳定 writer；`gateway_lane.py:28` 记录 request trace 时也没写。
4. lane/session/cache affinity 对 `model_id` 的消费不完整。
   - 依据：`astrmai/infrastructure/runtime/lane_manager.py:109` 的 `_rotation_reason()` 完全不比较 `model_id`。
   - 进一步依据：`astrmai/infrastructure/runtime/lane_storage.py:36`、`:93` 会一路保存 `model_id`；`lane_manager.py:141` 的远端 session key 仍只按 `provider_family:lane_umo` 取值。
5. reverse session hack 仍然侵入主链，provider-visible hash 依赖 `main.py` 事后补丁。
   - 依据：`main.py:77` 的 `on_llm_request` 直接改 `request.system_prompt` 并手工补 `astrmai_request_trace`。
   - 进一步依据：`astrmai/conversation/planning/planner.py:295` 之后再把这个 out-of-band hash 拼回 turn trace。

未实现/不完整项：
1. 缺少端到端测试把 `gateway.chat_in_lane_result -> on_llm_request hook -> planner._update_turn_trace_runtime` 串起来，证明 `gateway_system_hash / provider_visible_system_hash / post_hook_system_hash` 口径一致。
2. 缺少“模型切换后 lane 是否应 rotate / 远端 session 是否应换代”的自动化保护。

高风险点：
1. gateway trace、lane/economy trace、turn trace 当前不能直接对齐同一个 prefix/hash 语义，后续排查 cache 和 prompt 稳定性时容易出现假信号。
2. `prompt_templates.py` 模板与注册集中在单文件，现有测试更偏 policy/metrics，没有验证模板是否仍被真实调用链消费，维护风险高。

建议下一步：
1. 先统一 hash 口径和 cache 观测写入源，明确哪个字段代表 provider-visible、哪个字段代表 semantic/stable prefix，并补一条端到端 trace 一致性测试。
2. 再为 lane rotate / session 换代补测试，并评估 `reverse session` 是否能从 `main.py` hook 回收进 gateway 内部闭环。
