# 01 运行时与模型调用 — 领域审计报告

> 审计代理: 01_runtime_and_model_calls | 日期: 2026-07-26 | 代码基线: HEAD=4da2910
> 运行时证据: `.agent/runtime-observability-c4aee57-20260726/`（585 traces, 1022 LLM calls, 16h; 代码版本 c4aee57）
> 分析脚本: scratchpad `analyze_rt.py` → `rt_analysis_out.json`（摘要见附录）

## 1. 领域概述

本报告回答六个问题：单轮调用构成、延迟预算统一性、阶段间空档、Provider 失败放大、按复杂度跳过、缓存前缀稳定性。核心结论：

1. **遥测双轨制是本领域大多数怪象的总根因**：`turn_call_ledger` 同时支持"显式 event 参数"和"asyncio contextvar"两条记账通道，`begin/finish_llm_call` 走 event、`record_llm_attempt`/`clamp_timeout_to_turn_budget` 在 `gateway_call.py` 里恒传 `None` 走 contextvar。contextvar 只在 `main.py:224 turn_telemetry_scope(event)` 进入，之后随 `asyncio.create_task` 被后台任务永久继承。凡是"处理 B 轮消息的代码跑在 A 轮 spawn 的 task 里"的场景（attention session worker、memory per-chat worker），attempts 丢失、预算 clamp 用错轮次——已在生产环境造成 **instant memory backfill 100% 失败**（17/17, `turn_deadline_exhausted` 日志 71 条）。
2. **judge 并没有"修好"**：ledger 统计 `judge_calls_per_turn p50/p95=0` 是 `scripts/analyze_turn_ledger.py` 的度量 bug（按 stage 名匹配 "judge"，而真实 judge 条目是 `stage="gateway.chat", pool="judge"`）。真实值 p50=1 / p95=2 / **max=10**（单 trace 150 秒内 10 次 judge），7-25 报告的"同轮多次 judge"仍然存在。
3. **mood 是最大的隐性成本**：364 次 mood LLM 调用 vs 67 次实际回复，串行发生在 judge 之前的关键路径上（群聊在 ingress 内联，attention.dispatch p50 4.4s ≈ mood 延迟），而 judge 的 JSON 输出本身就带 mood_tag/mood_delta。
4. **预算体系半失效**：`gateway.chat` 名义受 turn 预算约束但经常 clamp 错轮次；`gateway.tool`（dialog 主回复、agent 工具环）完全不 clamp（trace 7edddd 预算耗尽后 dialog 照常成功）；executor 的 vision 旁路无 timeout_override 无总 deadline。
5. **重试乘法确认**：一次失败图片 = 框架层 5 × 网关层 3 × 视觉池 7 模型 ≈ 单图上限百余次请求。turn 7edddd 用 420.5s 才回复（vision 302.9s + 43 条 Gemini request_retry 全部落在 15:25–15:29 同一窗口）。
6. **缓存分层**：dialog 池 87.7% input cached、executed 轮 cache_hit 67/67——很好；judge 池 539 次 × p50 1977 字符动态 prompt、仅 222 字符稳定 system，指令 rubric 全在动态区且排在历史之后，0 前缀复用（7-25 已指出，未修）。

## 2. 单轮调用链实测（Q1）

### 2.1 代码路径

- **judge**: `decision_router.evaluate()` → `judge.evaluate()` → `gateway.chat_in_lane_result(lane=sys1/judge, event=focus_event, timeout_override=timing.attention_judge_timeout_sec, max_retries=0, max_models=1, reserve_for_reply=True)`（judge.py L467-485）。`gateway_tasks.call_judge_task`（stage="attention.judge"）**无人调用**，是死路径——所以 ledger 里永远没有 `attention.judge` stage。
- **mood**: `decision_router.evaluate() L101` / `gate._apply_primary_mood_update()`（gate.py L858-873，群聊 ingress L1080 内联、私聊 worker L1355） → `chat_state_service.update_mood` → `mood_manager.analyze_mood` → `chat_in_lane_result(lane=sys1/mood, **不传 event**)`，30s 超时（timing.mood_analysis_timeout_sec）。
- **dialog**: executor → `tool_chat_in_lane_result`（stage="gateway.tool"）。
- **cognitive_loop / goal / jargon / query_rewrite / memory / compaction / vision**: 全部经 `call_data_process_task`/`call_vision_task`/compaction_providers，event=None，靠 contextvar 记账。

### 2.2 实测构成（585 traces）

| pool | 次数 | 说明 |
|---|---|---|
| judge | 539 | 其中 **521 次发生在最终 skipped_ignore/wait/stale 的轮次**（409+108+4），只有 18 次在 executed 轮 |
| mood | 364 | executed 轮只占 62 次；~302 次花在被忽略的消息上 |
| dialog | 72 | 67 executed 轮每轮 1 次（2 轮 2 次） |
| cognitive_loop | 22 | p50 ~15s；think_level=1 也会跑 |
| vision 10 / memory 7 / goal 4 / compaction 3 / query_rewrite 1 | | |

**judge 重复**：`judge_calls_per_turn` 真实分布 p50=1 / p95=2 / max=10；40 turns >1。样本 `f487f997baa2`（群 666466106）：150 秒内同一 focus event 被连续 10 次 judge（每次 4.6-20.3s，全部 attempts=0），最终 ignore。机制：gate.py `_debounce_and_judge` 的 while 循环每批把被 IGNORE 的 focus event 放回 attention window（L1509-1510），下一批 `_merge_attention_window` 后焦点选择再次选中同一事件 → `begin_llm_call(focus_event)` 把新 judge 条目追加到同一轮 ledger。每"批"judge 一次是设计使然，但同一 focus 反复当选说明焦点选择对已忽略事件无衰减。

**分析器度量 bug**（`scripts/analyze_turn_ledger.py` L160-162）:
```python
if stage == "attention.judge" or "judge" in stage:
    judge_count += 1
```
judge 真实条目 stage 恒为 "gateway.chat"，pool 才是 "judge"。planner.py L928-937 的 `_count_judge_calls` 用 pool 判断（正确），同一提交(20bb585)里两处口径不一致，导致 ledger_analysis.json 的 `judge_calls_per_turn=0` 给出"已修复"的假信号。

## 3. 遥测双轨与预算错位（Q2 核心，含 Q4 一部分）

### 3.1 机制

`turn_call_ledger.py`：`current_turn_telemetry(event=None)` → `_CURRENT_TELEMETRY.get()`。唯一 setter `turn_telemetry_scope` 只在 `main.py:224` 进入。`asyncio.create_task` 在创建时拷贝当前 context ⇒ 长命任务永久携带"出生轮"的 telemetry。

`gateway_call.py::_elastic_call_result`：
```python
effective_timeout = clamp_timeout_to_turn_budget(None, timeout_limit, reserve_for_reply=...)   # L283
if effective_timeout <= 0.0: raise asyncio.TimeoutError("turn_deadline_exhausted")            # L288-289
...
record_llm_attempt(None, ledger_call_id, ...)                                                  # L317/349/418/466/500
```
event 恒为 None ⇒ clamp 和 attempt 落在 contextvar 轮上；而外层 `chat_in_lane_result` 的 begin/finish 用显式 event 落在正确轮上。

### 3.2 三个生产后果

**(a) instant memory backfill 永久死亡（VERIFIED, P1）**
`memory_turn_pipeline.on_turn_committed` L170-177 在**该 chat 第一条 committed turn 的处理上下文里** lazily `asyncio.create_task(self._chat_worker(...))`——per-chat 常驻 worker 永久继承第一轮的 telemetry。第一轮开始 360s 后，worker 里所有 `call_data_process_task` → clamp → 0 → 立即 `turn_deadline_exhausted`。16h 内 backfill 尝试 17 次全部失败（`instant llm backfill degraded: 所有模型均失败: turn_deadline_exhausted` ×17；`turn_deadline_exhausted` 总计 71 条：gateway_call 46 + gateway_tasks 6 + instant_memory_gate 17 + coordinator/mood 2）。运营者感知：即时记忆 LLM 兜底通道形同虚设，且每次还白付 begin/finish 开销与日志噪音。

**(b) attempts 丢失/串轮（VERIFIED, P2 观测性）**
539 条 judge 条目里 278 条 `model_attempts=[]`（主控疑点④）：judge 跑在 session worker（gate.py L455 `asyncio.create_task(self._debounce_and_judge(...))`）里时，contextvar 指向 spawn 轮，`record_llm_attempt(None, call_id)` 在错误的 calls 列表里找不到 call_id → 静默丢弃。同理 turn `99ebb0c5e1ce` cognitive_loop_ran=true 但自己的 ledger 里没有 cognitive_loop 条目（记到了邻轮），表面上是"judge 结束→context_build 9-10s 空档"，实际是被错记的 cognitive_loop LLM 调用。

**(c) judge 兜底 REPLY 风险（LIKELY, P2）**
繁忙群聊里 session worker 可跨多轮存活；spawn 轮 270s（360-90 reserve）后，worker 内 judge 的 gateway 调用会全部立即 turn_deadline_exhausted → cascade → `judge.py L551-555` 捕获异常后 `plan.action = "REPLY"` 兜底 ⇒ 停判即回。本窗口日志无 "Judge LLM failed"（0 条），未实际发生，但机制与 (a) 完全同链路。

### 3.3 预算覆盖矩阵（Q2 答案）

| 调用点 | 超时参数 | 是否受 turn 预算 | 备注 |
|---|---|---|---|
| gateway.chat（judge/mood/task/…） | timeout_override 或 infra.api_timeout=15 | "受"，但经常 clamp 错轮 | §3.1 |
| **gateway.tool（dialog 主回复、executor 工具环）** | `_tool_loop_total_timeout = max(api_timeout, tool_timeout)` | **完全不受** | gateway_lane.py L182-185、L730-744 无 clamp；trace 7edddd 预算 0 后 dialog 仍成功 8.3s |
| judge 外层 | attention.judge_timeout（默认3.0，服务器≥15）+ `clamp(focus_event)` | 受（正确 event） | decision_router.py L111-119，硬 deadline 会真 cancel（L45-57） |
| mood | mood_analysis_timeout_sec=30 | 仅经由 (错轮)clamp | 不传 event |
| cognitive_loop | cognitive_loop_timeout_sec=2.5（服务器实测≥35） | 仅 contextvar | wait_for 真 cancel |
| query_rewrite | query_rewrite_timeout_sec=8 + `clamp(None)` + 硬 deadline task.cancel | 受（多数在轮内） | **7-25 的 79s 问题已实修**（memory_retrieval_service.py L790-869），16h 仅 3 次 deadline 降级 |
| react 检索 step | 仅 `_generate_question` 有 wait_for(15s)；`_react_step` L185 **无超时** | 否 | 3 轮 × 每轮 1 LLM + 工具，memory.injection max 92s |
| vision（coordinator 屏障） | 4da2910: min(image_analysis_timeout, barrier剩余) wait_for + 180s 总额 | 每次迭代重新起算 | §5 |
| **vision（executor 旁路）** | executor.py L682 **不传 timeout_override**，无总 deadline | 仅 contextvar | 全池 × 3 attempts 扫描 |
| compaction | compaction provider timeout（4417ece 集中化） | 否（后台） | |

`asyncio.wait_for` 超时会真正 cancel 底层任务（gateway_call L298-306、decision_router L51-57、query_rewrite L842-847），**没有发现"超时后继续后台烧钱"的路径**；被取消调用在 ledger 记为 cancelled（3 条）。风险不在"不取消"，在"该 clamp 的 clamp 错轮、不该 clamp 的（工具环）不 clamp"。

## 4. 阶段间空档定量（Q3）

全部 585 traces 时间线重建（llm_call_ledger + stage_ledger 的 started_at/finished_at，trace `created_at` 是捕获时刻而非轮开始——本身是个观测陷阱）：

- **私聊 dispatch 结束 → 首个 LLM（mood）**：稳定 10.0-10.5s（6/6 样本）= `private_turn_coordinator.wait_for_input_stability`（L220-229，`input_settle_sec`，默认 1.5s、服务器配置≈10s）。**设计上的等待**，但注意 gate.py L1311-1315 合并循环里每有新消息就重新等一轮 settle（样本 99ebb 出现 31.3s ≈ 3 轮）。
- **turn_start→judge ≈14.1s（主控样本）** = settle(≈10s) + mood LLM(3-8s) 串行。不是神秘空档，是两段**未按此名目记账**的真实工作。
- **judge 结束→context_build ≈9-10s** = cognitive_loop LLM（think_level=1 也运行，见 §6），常被记到邻轮 ledger 造成"空档"假象。
- **群聊 attention.dispatch stage p50 4.4s / max 40s** = ingress 内联 mood 调用本身（gate.py L1080）；c7d6148cbf8b：mood 39.7s 后该消息被 skipped_ignore。
- **63 次 attention.dispatch abandoned 是仪表伪影**：skip 路径在 dispatch stage 内部调用 `_finalize_pre_planner_turn` → `finalize_turn_telemetry` 把仍 pending 的 dispatch 标 abandoned 并立即快照（planner.py L752-753），随后 observe_stage 正常退出又把活动条目改回 success——但快照已经带走 abandoned。**不存在真实的 63 次挂起**。
- **信号量排队**：`max_concurrent_llm_calls=3`（config.py L259）对所有 `_elastic_call_result`+`tool_chat` 生效，ledger elapsed 包含排队（begin 在 acquire 前）：judge 条目 elapsed 18.3s / attempt 14.6s；skipped 轮 judge 条目 30-51.7s。ambient judge/mood 与主回复 dialog 争抢同 3 个槽位 → 队头阻塞真实回复。

**设计等待 vs 无谓串行结论**：settle 窗口(10s)与 judge 门控是设计；无谓的是 (1) mood 串行且先于 judge、(2) cognitive_loop 在默认 think_level=1 上串行 8-16s、(3) 三槽信号量把旁路调用和主回复混在一个队列。

## 5. Provider 失败与重试放大（Q4）

**turn 7edddd6eb3d7 全景**（私聊 1608783003，图片消息，状态 executed，总耗时 **420.5s**，budget.exhausted=true）：
```
mood 8.4s → judge 18.3s → cognitive 16.2s
→ vision gemini-3-flash-preview   3 attempts (25.5+32.3+27.3s)  ledger 109.1s error
→ vision gemini-3.1-flash-lite    2 attempts, cancelled @71.0s   (coordinator wait_for)
→ vision gemini-3.1-flash-image   3 attempts (26.3+47.0+40.2s)  ledger 122.8s error
→ 剩余 6 个 vision 模型 0.5ms 全部 turn_deadline_exhausted（预算真耗尽）
→ mood/cognitive/memory 全部 deadline error
→ gateway.tool dialog 8.3s 成功（不受预算）→ 回复发出 @420s
```
同窗（15:25-15:29）AstrBot 框架 `[Gemini] request_retry` 43 条（2/5→5/5, 502 Bad Gateway）全部属于这一事件 ⇒ **乘法确认：框架 5 × 网关 (llm_retries=2 →3 次) × 池内模型（vision 7 个，coordinator 每图再 ×2）**。40 条真实模型 timeout(1/3) 日志中 46 条 timeout 行有 46-40=…（46 行里大部分是 turn_deadline_exhausted 伪超时，真实 provider 超时约 40）。

- 超时不开冷却：`_elastic_call_result` 的 TimeoutError 分支只 `report_failure(is_fatal=False)` 不 `_open_model_cooldown`（gateway_call.py L307-339），持续超时的模型每轮照常吃满 3×timeout；fatal（429/403/quota）才进 GatewayPolicy 冷却（120s/1800s）+ ModelRouter 30s cooldown_until，**同池不会重复消耗已 blocked 模型**（attempt_plan + blocked_models，lane L718-720）。
- 该事件发生于 c4aee57 运行期；**4da2910 的 vision timeout policy 已部分修复**（coordinator per-call wait_for + 180s barrier 总额），残留缺口：① gate 合并循环每次迭代 `prepare_batch` 重新起算 180s deadline；② executor.py L682 旁路无 timeout_override / 无总额；③ wait_for 切片内框架 5 连重试仍不可控（只能靠切片长度兜住）。
- **ProviderNotFoundError ×3** 定位：`conversation.compaction_provider_id` 配置为已不存在的 `openai/deepseek-v4-pro`（config.py L218 默认空，服务器填了旧值）。compaction_providers `_resolve_provider_candidates` L24-28 把它排第一位 → 每次压缩第一次尝试必失败（trace 3 条 attention.compaction.v2 attempts、star.context:403 警告 4 条），随后回落当前 provider 成功。另框架层 default_provider_id 漂移（google_gemin/gemini-3-flash-preview → kimi-k2.5）属 AstrBot 配置，插件池独立。
- `provider: "unknown"` 全量（1005/1005，主控疑点⑤）：`resolve_provider_capabilities`（provider_capabilities.py L107-121）拿不到 provider 对象时把**模型 ID 当 provider type** 匹配 → unknown。连带 `supports_cache_control/supports_remote_session=False`，cache_control hint 与 provider session 特性全被静默关闭（dialog 87.7% 缓存是 provider 侧隐式缓存的功劳）。

## 6. 按复杂度跳过的覆盖面（Q5）

trace 证实 think_level 分布 0:89 / 1:481 / 2:3 / 3:12；已被门控的：memory_feedback、goal_update、slang_context、jargon_explanation（side_inputs.timings 里 `skipped_reason: think_level_1`）、memory 注入（`think_level_1_no_memory_intent`）、readonly tools（<3 禁用）。**未被门控的高频成本**：

1. **mood**：任何 think level、任何 judge 结局都跑（364 次；仅 micro-utterance 与 primary_mood_applied 跳过）。judge JSON 已含 `mood_tag/mood_delta`（judge.py L459-462）且 judge 会 `atomic_update_mood`——独立 mood 池调用与 judge 内嵌 mood 是**双重情绪计算**，前者可在 judge 必然运行的轮次直接省略，或降级为关键词启发（mood_manager 已有 `_fallback_analyze_local`）。
2. **cognitive_loop**：`think_level >= 1 即运行`（cognitive_loop.py L193-194），而 1 是默认级（82%）。对普通寒暄多付 8-16s 串行 LLM。建议 gate 提到 ≥2，或与 planner context_build 并行。
3. **judge 本身**：521/539 次花在最终被忽略/等待的消息上——ambient 设计使然，但同一 focus 被 10 连判说明可加"同 focus 连续 IGNORE n 次后进入静默期"的短路。
4. compaction 触发正常（16h 仅 3 次，事件驱动）；fast_mode/CORE_ONLY（87 次 fast_mode）正常跳过整个 System2。

## 7. 缓存前缀稳定性（Q6）

- **dialog 池（executed 67 轮）**：cache_hit 67/67，input cached 479,744/547,161 = **87.7%**（均值 86.3%）；prefix_stable=True 61/67，6 次 first_seen。system_chars 恒 8830。健康。
- **judge 池**：system 恒 222 字符（JUDGE_STABLE_PREFIX），但 p50 1977 字符的动态 prompt 里嵌着 ~1.4K 字符固定 rubric（可用动作表、人格维度 key 列表、JSON schema、mood 标签说明，judge.py L419-463），且排在 history/mood 之后 ⇒ 前缀命中率 0-25%（7-25 实测）无改善。539 次 × 1977 ≈ 1.07M chars/16h 几乎全价。修法：rubric 全部并入 system prefix，动态段（mood 值、历史、消息）置尾。
- **mood 池**：system 恒 574；prompt p50 218，本身小。
- **prefix_changed_reason 标注 bug**：context_engine.py L229-230 稳定时置 reason=""，planner.py L263 `or "unavailable_in_trace"` 把空串覆写成 "unavailable_in_trace" ⇒ 61 个稳定轮全被标成"trace 不可用"，趋势分析失真。
- semi_stable 块变动频率无法从本批 trace 直接测（continuity 只带长度/哈希快照），建议在 context_engine 把 `semi_stable_blocks` 哈希逐轮 diff 计数后再评估。

## 8. 领域级测试缺口

1. 无测试断言"gateway 调用的 attempts/clamp 与 begin/finish 落在同一轮"——`tests/test_turn_call_ledger_refactor.py` 全部在 `turn_telemetry_scope` 内同步调用，覆盖不到跨 task 继承场景（复现：在 scope 内 create_task 一个 worker，scope 结束后经 worker 调 `_elastic_call_result`，断言 attempt 不丢失且 clamp 用当前轮）。
2. `scripts/analyze_turn_ledger.py` 的 judge 口径无守护测试（tests/test_analyze_turn_ledger_refactor.py 未喂 pool="judge"+stage="gateway.chat" 的条目）。
3. 无"tool 环占用 turn 预算"的测试（tool_chat 在预算 0 时应至少记录 budget_ignored 标记）。
4. vision 合并循环多次迭代的累计 deadline 无测试（test_private_turn_coordinator.py 只测单次 prepare_batch）。
5. mood-判定重复计算无回归护栏（可断言"同一 event 至多一次 mood LLM 且 judge 轮次内跳过独立 mood 调用"——若采纳 RT-04 修复）。

## 附录 A：脚本输出摘要

- pool 计数：judge 539 / mood 364 / dialog 72 / cognitive_loop 22 / vision 10 / memory 7 / goal 4 / compaction 3 / query_rewrite 1；stage 只有 gateway.chat 950 / gateway.tool 69 / attention.compaction.v2 3。
- judge/turn: p50 1, p95 2, max 10; >1 的 40 turns; judge attempts 缺失 278/539。
- 私聊 6 样本 dispatch_end→mood_start: 10.0/10.0/10.4/10.5/31.3(3×settle)/–；judge_end→next_llm p95 17.5s。
- 30s+ 调用 13 个：vision 122.8/114.7/109.1/71.0，judge 51.7/32.7/30.3/30.2（skipped 轮），memory 44.5/34.4，mood 39.7/31.8，cognitive 34.5。
- budget: remaining_ms p05=0（由 7edddd 等极端轮贡献），exhausted 1；attempt 状态: success 727 / timeout 13 / error 12。
- 日志：turn_deadline_exhausted 71（gateway_call 46 + gateway_tasks 6 + instant_memory_gate 17 + 其他 2）；[Gemini] request_retry 43 条全部位于 15:25-15:29；backfill degraded 17/17；CognitiveLoop timeout 0；Judge LLM failed 0。
