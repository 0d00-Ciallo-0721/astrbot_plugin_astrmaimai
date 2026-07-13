# AstrMai 最终功能审计：Gateway 与 Context Economy

## 审计结论

本报告基于当前工作树（包含未提交的生产代码变更）审计 `astrmai/infrastructure/gateway/` 与 `astrmai/infrastructure/context_economy/`。仅为证明真实生产可达性读取相邻的入口、执行器、视觉、lane persistence、compaction 与 WebUI 指标消费路径；未读取或运行任何测试，未审计测试覆盖率、代码风格、重复、死代码、重构机会、认证、授权或安全策略，且将 `astrmai/infrastructure/security/` 视为不透明依赖。

共确认 **12 项可达功能缺陷**：**P1 3 项、P2 6 项、P3 3 项**。未发现 P0 缺陷。

## Finding 07-01：正常回答只要提到通用错误术语就会被当成供应商失败文本并耗尽模型池

- **ID / 严重级别**：AM-GW-07-01 / **P1**
- **文件:行**：`astrmai/infrastructure/gateway/output_guard.py:31`；`astrmai/infrastructure/gateway/output_guard.py:180`；`astrmai/infrastructure/gateway/output_guard.py:278`；`astrmai/infrastructure/gateway/gateway_call.py:313`；`astrmai/conversation/execution/executor.py:567`
- **触发条件**：用户询问 API 配额、限流、HTTP 状态或响应格式等主题，模型给出包含 `quota`、`rate limit`、`permission denied`、`status code`、`response:`、`request_id` 等任一普通术语的正常答案。
- **真实调用链**：普通消息进入生产对话链 → `Executor.execute()` → `_run_text_mode()` → `GlobalModelGateway.chat_in_lane_result()` → `_elastic_call_result()` → `validate_visible_output_text()` → `looks_like_provider_failure_text()` → 抛出 `ValueError("provider_failure_text")` → Executor 切换模型并最终进入 fatal fallback。
- **实际行为**：`looks_like_provider_failure_text()` 对完整回复做无上下文的子串匹配；正常技术回答会被拒绝、记为模型失败并在每个候选模型上重试。只要各模型都自然复述问题术语，整个池必然耗尽。只读探针已复现三条正常文本分别因 `quota`、`Response:` 和 `status code 200` 被判为 `provider_failure_text`。
- **期望行为**：只拒绝具有供应商错误信封/错误结构证据的返回；正常回答中讨论错误概念或状态码时必须可见发送。
- **生产影响**：核心聊天对一整类常见技术问题稳定无响应或返回错误降级文案，同时健康分被错误扣减并可能触发后续错误路由。
- **现有守卫为何失效**：标记匹配没有要求错误码、字段组合、整行格式或供应商响应结构；后续 `sanitize_visible_reply_text()` 复用同一判定，无法恢复被误杀的正文。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 07-02：网关 15 秒 API 超时覆盖了 120 秒工具超时，合法长工具任务必然被提前取消

- **ID / 严重级别**：AM-GW-07-02 / **P1**
- **文件:行**：`astrmai/infrastructure/gateway/gateway_lane.py:492`；`astrmai/infrastructure/gateway/gateway_lane.py:501`；`astrmai/infrastructure/gateway/gateway_lane.py:505`；`astrmai/app/plugin_facade.py:654`；`astrmai/app/plugin_facade.py:666`；`config.py:204`
- **触发条件**：`/work`、正常工具模式或 Sys3 子代理执行一个耗时超过 `infrastructure.api_timeout`（默认 15 秒）但没有超过 `sys3.tool_timeout`（默认调用点为 120 秒）的工具，或多步 ReAct 总耗时超过 15 秒。
- **真实调用链**：`PluginFacade.enter_sys3_direct()` / `Executor._run_tool_mode()` / `BaseSubAgent.call()` → `tool_chat_in_lane_result(timeout=120)` → `context.tool_loop_agent(tool_call_timeout=120)` → 外层 `asyncio.wait_for(..., timeout=self._api_timeout())`。
- **实际行为**：`tool_call_timeout=120` 只传入内层 Agent，但整个 Agent coroutine 被默认 15 秒的 `wait_for` 包裹；合法工具仍在自身预算内时，外层已经取消整轮。随后网关或 Executor 可能换模型重新执行任务。
- **期望行为**：总 Agent 预算必须与工具预算和 `max_steps` 协调，至少不能短于允许的单次工具超时；普通 LLM API 超时不应直接成为整个 ReAct 工作流的硬上限。
- **生产影响**：文件处理、浏览器、远程 API、定时任务等稍长工具稳定失败；若外部工具已产生不可回滚副作用，模型切换会重复执行同一操作。
- **现有守卫为何失效**：调用方正确传入的 `tool_call_timeout` 无法延长外层 `wait_for`；异常处理只切换模型，没有区分“工具仍在合法预算内”与真正超时。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 07-03：成功后的 lane 持久化/trace 失败会反向判定模型失败，并可重复执行已完成工具

- **ID / 严重级别**：AM-GW-07-03 / **P1**
- **文件:行**：`astrmai/infrastructure/gateway/gateway_lane.py:96`；`astrmai/infrastructure/gateway/gateway_lane.py:114`；`astrmai/infrastructure/gateway/gateway_lane.py:141`；`astrmai/infrastructure/gateway/gateway_lane.py:350`；`astrmai/infrastructure/gateway/gateway_lane.py:589`；`astrmai/infrastructure/gateway/gateway_lane.py:629`；`astrmai/infrastructure/gateway/gateway_lane.py:654`
- **触发条件**：模型已成功返回可用文本或工具已完成后，conversation manager 更新、lane history 写入、event extra 写入或 trace 追加任一失败。
- **真实调用链**：普通聊天为 `Executor._run_text_mode()` → `chat_in_lane_result()` → `_elastic_call_result()` 成功 → `_finalize_success_artifacts()` → `LaneManager.append_visible_reply_artifact()`；工具模式为 `Executor._run_tool_mode()` / `/work` → `tool_chat_in_lane_result()` → 工具完成并 `router.report_success()` → `_finalize_success_artifacts()` → 异常落入同一模型调用的 `except`。
- **实际行为**：普通聊天中 finalizer 异常代替已构造的成功 `LLMCallResult` 向上抛出，Executor 会换模型重新生成。工具模式更严重：同一次调用先执行 `report_success()`，finalizer 失败后又执行 `report_failure()`，然后切换模型并可能重新调用已产生副作用的工具。
- **期望行为**：成功后的 usage、economy、history 与 trace 写入应作为可降级副作用隔离；失败可以记录诊断，但不得撤销模型成功或重新执行工具。
- **生产影响**：瞬时持久层故障会导致用户收不到已经生成的回复；工具任务可能重复创建提醒、重复发送、重复写文件或重复调用外部服务，且路由健康统计同时记成功和失败。
- **现有守卫为何失效**：`gateway_call._record_success_artifacts()` 对日志、economy 和 benchmark 已有异常隔离，但 lane 专用 `_finalize_success_artifacts()` 没有同等边界；工具路径的大范围 `try/except` 把后处理异常误归类为模型/工具失败。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 07-04：所有 tool-loop 调用绕过全局 LLM 并发上限

- **ID / 严重级别**：AM-GW-07-04 / **P2**
- **文件:行**：`astrmai/infrastructure/gateway/model_gateway.py:38`；`astrmai/infrastructure/gateway/gateway_call.py:179`；`astrmai/infrastructure/gateway/gateway_lane.py:486`；`astrmai/infrastructure/gateway/gateway_lane.py:492`
- **触发条件**：多个群聊、`/work`、正常工具模式或多个 SubAgent 同时启动 tool loop，活跃数量超过 `max_concurrent_llm_calls`。
- **真实调用链**：各生产工具入口 → `tool_chat_in_lane_result()` → 直接 `context.tool_loop_agent()`；对照普通 `call_data_process_task()` / `chat_in_lane_result()` → `_elastic_call_result()` → `async with self._global_semaphore`。
- **实际行为**：全局信号量只包裹 `_elastic_call_result()`；tool-loop 分支从未获取它，因此配置为 3 也可同时启动任意数量的 Agent LLM 请求及其后续步骤。
- **期望行为**：同一 Gateway 实例发起的普通生成和 tool-loop 都应共享全局并发预算。
- **生产影响**：工具流量高峰可突破运维设置，放大 429、供应商拥塞和内存/任务压力，并使普通聊天被工具请求挤占。
- **现有守卫为何失效**：`get_agent_models()` 只做模型冷却筛选，不限制并发；AstrBot 内层 `tool_loop_agent()` 没有接入本插件的 `_global_semaphore`。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 07-05：tool-loop 完全忽略配置的模型重试与退避，超时还被记成 unknown

- **ID / 严重级别**：AM-GW-07-05 / **P2**
- **文件:行**：`astrmai/infrastructure/gateway/gateway_lane.py:486`；`astrmai/infrastructure/gateway/gateway_lane.py:492`；`astrmai/infrastructure/gateway/gateway_lane.py:654`；`astrmai/infrastructure/gateway/gateway_lane.py:656`；`astrmai/infrastructure/gateway/gateway_lane.py:683`；`astrmai/infrastructure/gateway/gateway_call.py:212`；`astrmai/infrastructure/gateway/model_gateway.py:75`
- **触发条件**：tool-loop 的当前模型发生一次可恢复网络错误或 `asyncio.TimeoutError`，尤其是只配置一个 agent 模型时。
- **真实调用链**：`Executor._run_tool_mode()` / `/work` / SubAgent → `tool_chat_in_lane_result()` → 每个 `model_id` 只调用一次 `tool_loop_agent()` → 异常后直接 `continue` 到下一模型或抛出 cascade failure。
- **实际行为**：该路径从不读取 `_max_retries()` 或 `_backoff_factor()`，与普通 `_elastic_call_result()` 的每模型 `max_retries + 1` 次尝试不一致。并且异常分类只传 `str(exc)`，没有把异常对象传给 `_classify_failure_kind()`；`TimeoutError` 的字符串通常为空，因此被记为 `unknown` 而非 `timeout`，也不会形成正确超时诊断。
- **期望行为**：tool-loop 应遵守同一网关重试/退避配置，并用异常类型进行失败分类；可恢复瞬时错误不应立即耗尽单模型池。
- **生产影响**：工具模式明显比文本模式更脆弱，单次抖动即可令任务失败；超时统计与 trace 错误，运维无法区分供应商超时和未知失败。
- **现有守卫为何失效**：外层 Executor 的多模型循环不是同模型重试；单模型部署没有任何替代候选，且字符串关键字分类无法识别空消息的 `TimeoutError`。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 07-06：视觉池绕过健康排序，业务无效结果还会先记成功并写入 lane 历史

- **ID / 严重级别**：AM-GW-07-06 / **P2**
- **文件:行**：`astrmai/infrastructure/gateway/gateway_tasks.py:61`；`astrmai/infrastructure/gateway/gateway_tasks.py:69`；`astrmai/infrastructure/gateway/gateway_tasks.py:83`；`astrmai/infrastructure/gateway/gateway_tasks.py:90`；`astrmai/infrastructure/gateway/gateway_tasks.py:136`；`astrmai/infrastructure/gateway/gateway_call.py:291`；`astrmai/infrastructure/gateway/gateway_lane.py:350`
- **触发条件**：`vision_models[0]` 反复返回可解析 JSON，但缺少非空 `description`、含无效 `emotion_tags`，或发生不会打开 rate/quota cooldown 的普通失败。
- **真实调用链**：`VisualCortex.process_image_async()` 或 `Executor` 视觉旁路 → `call_vision_task()` → 直接按配置顺序遍历 `vision_models` → 对单个 `[model_id]` 调 `chat_in_lane_result()` / `_elastic_call_result()` → JSON 解析成功后先 `router.report_success()` 和追加 lane artifact → 返回 wrapper 后才 `_normalize_vision_failure_reason()`。
- **实际行为**：视觉 wrapper 用原始配置列表调用 `_filter_cooldown_attempt_queue()`，未调用 `router.get_ranked_models("vision", ...)`，所以健康分无法改变下次视觉调用顺序。业务无效 JSON 已在内层记成功并写入历史；wrapper 既不 `report_failure()`，`empty_description` 等理由也不会打开 cooldown，然后才尝试下一模型。
- **期望行为**：视觉候选应按健康状态排序；只有通过视觉结果契约校验后才能记成功和持久化，业务无效结果应作为该模型失败反馈给路由器。
- **生产影响**：坏模型长期占据每次图片分析的第一尝试，增加延迟与费用；无效 assistant JSON 污染后续视觉 lane 上下文，并使健康统计反向奖励坏模型。
- **现有守卫为何失效**：通用 JSON 校验只证明语法可解析，不验证视觉字段；视觉专用校验位于成功记账与持久化之后，且 cooldown 分类只识别少数供应商错误词。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 07-07：compaction 供应商调用没有任何超时，单次挂起会永久占住该会话的压缩槽位

- **ID / 严重级别**：AM-GW-07-07 / **P2**
- **文件:行**：`astrmai/conversation/attention/compaction_providers.py:161`；`astrmai/conversation/attention/compaction_providers.py:173`；`astrmai/conversation/attention/compaction_providers.py:239`；`astrmai/conversation/attention/compaction_providers.py:251`；`astrmai/conversation/attention/context_compaction.py:310`；`astrmai/conversation/attention/context_compaction.py:321`；`astrmai/conversation/attention/context_compaction.py:1385`
- **触发条件**：配置的 compaction provider 或当前聊天 provider 的 `llm_generate()` 建连后不返回也不抛异常。
- **真实调用链**：用户/助手消息追加 → `schedule_compaction_evaluation()` 创建并登记 `_pending_tasks[chat_id]` → `maybe_compact()` → `_build_summary_with_provider_v2()` → 直接 `await context.llm_generate()`。
- **实际行为**：compaction 绕过 Gateway 的 `asyncio.wait_for(api_timeout)`，自身也没有超时。挂起任务一直保留在 `_pending_tasks`；后续该 chat 的每次调度都因 `evaluation_already_scheduled` 返回，永远不会进入本地 `_build_summary_v2()` fallback。
- **期望行为**：供应商摘要调用应受有限超时控制；超时后立即尝试下一 provider，最终使用已有本地摘要 fallback，并释放 pending slot。
- **生产影响**：单次供应商挂起即可永久停止该会话的上下文压缩，旧段持续增长，压缩状态与上下文预算逐渐失真，直到插件重启或任务被外部取消。
- **现有守卫为何失效**：`except Exception` 只能处理已抛出的错误，不能处理永不完成的 await；本地 fallback 位于该 await 返回之后，无法被到达。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 07-08：compaction 绕过网关结果校验，会把供应商错误正文持久化为冷摘要

- **ID / 严重级别**：AM-GW-07-08 / **P2**
- **文件:行**：`astrmai/conversation/attention/compaction_providers.py:173`；`astrmai/conversation/attention/compaction_providers.py:193`；`astrmai/conversation/attention/compaction_providers.py:195`；`astrmai/conversation/attention/compaction_providers.py:251`；`astrmai/conversation/attention/compaction_providers.py:271`；`astrmai/conversation/attention/compaction_providers.py:273`；`astrmai/conversation/attention/context_compaction.py:1416`；`astrmai/conversation/attention/context_compaction.py:1439`
- **触发条件**：Provider 没有抛异常，而是在 `completion_text` 中返回非空的限流、权限、usage metadata、安全过滤或其他错误说明。
- **真实调用链**：compaction 调度 → `_build_summary_with_provider_v2()` → 直接 `context.llm_generate()` → 只做 `str(...).strip()` 非空判断 → `_structure_from_summary_text()` → `_merge_cold_structure()` → `dialogue_store.set_cold_summary()`。
- **实际行为**：该链路不调用 Gateway 的 `validate_visible_output_text()` 或等价的 provider-failure 判定；任何非空错误正文都被当作成功摘要并记录 economy success，随后解析为 topic 并持久化。v1 路径同样只做 `_clip_summary()`。
- **期望行为**：compaction 应复用网关的结构化失败判定，拒绝供应商错误正文并尝试下一 provider/本地 fallback。
- **生产影响**：错误码、配额提示或供应商元数据进入长期冷摘要，之后持续注入规划上下文并挤占有限摘要预算，直到后续压缩将其覆盖。
- **现有守卫为何失效**：异常捕获假设供应商失败一定抛异常；`clipped`/`rendered` 仅验证非空和长度，不验证语义契约或错误信封。
- **分类**：confirmed（已确认）
- **置信度**：0.99

## Finding 07-09：Provider 能力由可自定义 ID 的子串猜测，误命中会给不支持的适配器发送专用参数

- **ID / 严重级别**：AM-GW-07-09 / **P2**
- **文件:行**：`astrmai/infrastructure/gateway/provider_capabilities.py:12`；`astrmai/infrastructure/gateway/provider_capabilities.py:15`；`astrmai/infrastructure/gateway/provider_capabilities.py:36`；`astrmai/infrastructure/gateway/gateway_lane.py:167`；`astrmai/infrastructure/gateway/gateway_lane.py:171`；`astrmai/infrastructure/gateway/gateway_lane.py:178`
- **触发条件**：AstrBot 中用户自定义的 provider ID 含 `claude`/`anthropic`/`dify`/`coze` 等词，但实际适配器不是对应类型；或真实对应适配器使用不含这些词的普通 ID（如 `provider-1`）。
- **真实调用链**：管理员从 provider selector 配置 ID → Gateway lane 调用 → `_lane_request_kwargs(actual_model)` → `infer_provider_capabilities(provider_id)` → 根据猜测添加 `cache_control` 或 `session_id` → `context.llm_generate()` / `tool_loop_agent()`。
- **实际行为**：能力判断完全依赖 ID 字符串，没有读取 AstrBot provider 实例/适配器元数据。误命中会发送适配器不接受的专用参数并导致主模型调用失败；漏命中则静默禁用可用的 remote session/cache hint。只读探针确认 `provider-1` 被固定判为 `unknown`。
- **期望行为**：能力应由实际 provider 类型、显式能力声明或受控配置决定；自定义显示/实例 ID 不应改变调用协议。
- **生产影响**：合法自定义 provider 在 lane chat、tool-loop 和 compaction 上不可用或失去会话/缓存能力，可能持续切到 fallback，单模型部署则直接耗尽。
- **现有守卫为何失效**：fallback 只能在另有候选模型时掩盖误判，无法纠正当前 provider 的协议；`unknown` 分支把漏识别当成永久不支持。
- **分类**：confirmed（已确认）
- **置信度**：0.97

## Finding 07-10：同一主模型池内的轮询模型被错误统计为 fallback

- **ID / 严重级别**：AM-GW-07-10 / **P3**
- **文件:行**：`astrmai/infrastructure/context_economy/center.py:156`；`astrmai/infrastructure/context_economy/center.py:364`；`astrmai/infrastructure/gateway/gateway_lane.py:340`；`astrmai/infrastructure/gateway/gateway_lane.py:371`；`astrmai/infrastructure/gateway/gateway_lane.py:615`；`astrmai/infrastructure/gateway/model_router.py:119`
- **触发条件**：Judge、Mood 或任一 lane 调用传入两个以上同池模型，ModelRouter 因 round-robin/健康分选择列表中非第一个模型并成功；没有使用真正的 `fallback_models`。
- **真实调用链**：例如 `Judge.judge()` → `chat_in_lane_result(models=task_models, use_fallback=False)` → `_elastic_call_result()` 从主池选中第二个模型 → lane 层重建 `WorkloadTrace` → `ContextEconomyCenter.record_trace()`。
- **实际行为**：通用调用层正确以“是否属于 `primary_models`”判断 fallback，但 lane 层覆盖为 `actual_model != workload_policy.primary_model`；而 `primary_model` 永远是配置列表第一个元素。因此主池内正常轮询到第二个模型也增加 `fallback_count`，降低 primary hit rate。
- **期望行为**：fallback 只表示落到独立 fallback 池；主池任一模型成功都应计为 primary-pool hit，另行记录具体模型即可。
- **生产影响**：Context Economy WebUI 与 benchmark 的 fallback/primary 指标持续失真，健康路由越积极，表面 fallback 率反而越高，误导运维调整模型池。
- **现有守卫为何失效**：`WorkloadPolicy` 只保存单个首选模型，没有保存主池成员集合；lane 层丢弃了 `LLMCallResult` 在通用调用阶段已经得到的真实池归属。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 07-11：缓存 usage 声明“支持证据”却不读取对应缓存 token 字段

- **ID / 严重级别**：AM-GW-07-11 / **P3**
- **文件:行**：`astrmai/infrastructure/gateway/gateway_result.py:52`；`astrmai/infrastructure/gateway/gateway_result.py:55`；`astrmai/infrastructure/gateway/gateway_result.py:57`；`astrmai/infrastructure/gateway/gateway_result.py:69`；`astrmai/infrastructure/gateway/gateway_lane.py:62`；`astrmai/conversation/planning/planner.py:311`；`astrmai/conversation/planning/planner.py:336`
- **触发条件**：供应商 usage 通过代码已认可的 `cache_read_input_tokens` 或 `prompt_tokens_details` 暴露缓存读取量，而不是顶层 `input_cached` / `cached_tokens`。
- **真实调用链**：任一成功 Gateway 调用 → `_extract_usage(response)` → lane request trace → Planner `_sync_turn_context()` → continuity cache metrics / WebUI 诊断。
- **实际行为**：`_has_usage_field()` 把 `cache_read_input_tokens` 与 `prompt_tokens_details` 视为缓存证据支持，但 `_read_usage_field()` 只读取 `input_cached` 和 `cached_tokens`，也不展开 `prompt_tokens_details`。结果是 `cached_usage_supported=True`、`input_cached=0`、`cache_hit=False` 的自相矛盾状态。
- **期望行为**：既然接受这些字段作为支持证据，就应解析其缓存 token 数；不支持解析时也不应声称可判定 cache hit。
- **生产影响**：真实缓存命中被系统性记为未命中，usage 日志、continuity、cache hit rate 与调优判断失真。
- **现有守卫为何失效**：字段存在性与数值提取使用了不同字段集合；后续所有消费者只信任已归一化的 `input_cached`，无法从原始 usage 补救。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 07-12：tool-loop 成功调用从不写 Context Economy benchmark sample

- **ID / 严重级别**：AM-GW-07-12 / **P3**
- **文件:行**：`astrmai/app/bootstrap.py:197`；`astrmai/app/bootstrap.py:198`；`astrmai/infrastructure/gateway/gateway_call.py:113`；`astrmai/infrastructure/gateway/gateway_call.py:148`；`astrmai/infrastructure/gateway/gateway_lane.py:513`；`astrmai/infrastructure/gateway/gateway_lane.py:589`；`astrmai/infrastructure/gateway/gateway_lane.py:629`
- **触发条件**：任意普通工具模式、`/work` 或 Sys3 SubAgent 成功完成 tool loop。
- **真实调用链**：bootstrap 为 Gateway 注入 `ContextEconomyBenchmarkSampleStore` → 普通生成成功走 `_record_success_artifacts()` → `_record_benchmark_sample()`；工具生成成功则在 `tool_chat_in_lane_result()` 中手动执行 usage log、trace 和 finalizer 后直接返回。
- **实际行为**：两个 tool-loop 成功分支都没有调用 `_record_benchmark_sample()`，所以已启用的持久 benchmark 数据集中系统性缺失 `CHAT_TOOLS` / Sys3 样本；失败也只留 event trace。
- **期望行为**：所有通过同一 Gateway 完成的 workload 都应按同一 success accounting 契约写 benchmark sample，或明确标记不可采样，而不是静默漏项。
- **生产影响**：离线 Context Economy 基准报告无法反映工具工作负载的 token、缓存、模型命中和 fallback 表现，调优结果偏向普通聊天/结构化任务。
- **现有守卫为何失效**：tool-loop 复制了普通成功路径中的部分记账步骤，但 benchmark 调用只封装在 `_record_success_artifacts()`，lane tool 分支没有复用该入口，也没有独立补写。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## 已核验的生产路径

- Gateway 初始化、配置热刷新、模型池读取、全局并发器、fallback 与 cooldown。
- 普通文本、JSON task、Judge、Mood、Vision、Persona、Proactive、Dream、Memory 与 lane chat 调用。
- 正常工具模式、`/work` 直连、Sys3 SubAgent 与 terminal/wait protocol 返回。
- usage/result 构造、模型健康成功/失败记账、lane history artifact、event trace、Context Economy trace 与 benchmark sample。
- Prompt template registry 的全部模板、template envelope、稳定前缀/动态 payload、lane identity、scope、rotation 与 metrics。
- compaction provider 选择、provider kwargs、v1/v2 summary 返回、本地 fallback 与 cold-summary 持久化契约。

## 已审阅文件

### Assignment 07 全部目标文件

- `astrmai/infrastructure/gateway/__init__.py`
- `astrmai/infrastructure/gateway/gateway_call.py`
- `astrmai/infrastructure/gateway/gateway_exceptions.py`
- `astrmai/infrastructure/gateway/gateway_lane.py`
- `astrmai/infrastructure/gateway/gateway_policy.py`
- `astrmai/infrastructure/gateway/gateway_result.py`
- `astrmai/infrastructure/gateway/gateway_tasks.py`
- `astrmai/infrastructure/gateway/model_gateway.py`
- `astrmai/infrastructure/gateway/model_router.py`
- `astrmai/infrastructure/gateway/output_guard.py`
- `astrmai/infrastructure/gateway/provider_capabilities.py`
- `astrmai/infrastructure/context_economy/__init__.py`
- `astrmai/infrastructure/context_economy/center.py`
- `astrmai/infrastructure/context_economy/models.py`
- `astrmai/infrastructure/context_economy/prompt_templates.py`
- `astrmai/infrastructure/context_economy/token_estimator.py`

### 为确认生产可达性读取的相邻路径

- `astrmai/app/bootstrap.py`、`astrmai/app/plugin_facade.py`
- `astrmai/conversation/execution/executor.py`
- `astrmai/conversation/decision/judge.py`
- `astrmai/conversation/planning/planner.py`
- `astrmai/conversation/attention/compaction_providers.py`
- `astrmai/conversation/attention/context_compaction.py`
- `astrmai/infrastructure/runtime/lane_manager.py`
- `astrmai/infrastructure/runtime/lane_storage.py`
- `astrmai/infrastructure/runtime/lane_history.py`
- `astrmai/infrastructure/runtime/runtime_contracts.py`
- `astrmai/infrastructure/runtime/trace_runtime.py`
- `astrmai/multimodal/visual_cortex.py`
- `astrmai/state/mood/mood_manager.py`
- `astrmai/workmode/subagents/base_agent.py`
- 相关 Memory、Persona、Dream、Proactive 生产调用点与 Context Economy WebUI 指标消费服务。

## 验证说明

- 已逐文件审阅 Assignment 07 的全部当前生产代码，并用生产调用者反向证明每项 finding 可达。
- 使用 `python -B` 只读探针验证 output guard 会把三条正常技术回答判为 `provider_failure_text`，并验证普通自定义 provider ID 会被能力推断为 `unknown`；`-B` 禁止生成 bytecode。
- 按任务约束未读取、未运行任何测试，也未修改任何生产代码、配置、状态文件或其他审计报告。
