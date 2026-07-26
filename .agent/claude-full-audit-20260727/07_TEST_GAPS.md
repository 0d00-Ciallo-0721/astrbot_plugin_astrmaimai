# 07 测试缺口审计 — 跨模块回归缺口

> 领域代理：07 测试缺口 · 2026-07-27 · 只读审计
> 目标：不重算覆盖率（codex-review 已给 72.9%），只找"用户可感知行为路径今天无测试守护、且最近提交仍在高频改动"的回归缺口。

## 0. 领域概述与基线核实

- **当前可收集测试：1673 条，收集 0 错误**（`python -m pytest --collect-only -q` → `1673 tests collected in 9.12s`）。`.agent/session-state.md:34` 声称的 "1142 passed" 是 2026-07-05 的过期快照（此后 10+ 提交新增了数百条测试），属文档漂移而非缺陷；codex-review 当时的 1037 也已过期。
- 测试热点（`git log -15 --stat -- tests/`）：最近 5 个提交集中改 `test_private_turn_coordinator.py`(+330/+67/+30/+26)、`test_attention_gate_refactor.py`(+68/+29/+35/+153/+151)、`test_turn_call_ledger_refactor.py`(+35/+162/+93)、`test_gateway_vision_refactor.py`、`test_memory_v2_services.py` —— 与源码热点（gate/private_turn_coordinator/planner/gateway）一致，说明"改哪补哪"的习惯存在，但补的是**新分支单元测试**，跨模块行为回归仍无人守护。
- 历史基线：`.agent/test-gap-audit-master.md`（2026-07-03，大量结论已被 codex-review 判为过期/误判）；`.agent/test-coverage-audit-codex-review.md`（真实覆盖率基线）。本报告发现均按两者去重。

## 1. 十个维度逐项结论

### 维度 1 群聊身份隔离 — ★ 最大缺口（TG-01）

**现有最接近的测试**：
- `tests/test_executor_refactor.py::test_finalize_reply_repairs_foreign_group_member_direct_address`（L177-213）等 3 条：终线 `GroupActorConsistencyGuard` 修复/放行/私聊直通。
- `tests/original_ported/test_prompt_refiner_focus_layout_ported.py::test_current_speaker_boundary_precedes_focus_message`（L87）、`test_final_speaker_lock_follows_runtime_guidance`（L125）：断言 prompt **布局**，但 `current_speaker_block` 是测试里手工捏造的。
- `tests/original_ported/test_planner_prompt_context_guards_ported.py::test_current_speaker_block_marks_group_weak_input_boundary`（L60）：`_build_current_speaker_block` 单测，仅单 sender 且 focus_context 与 event sender 一致。
- `tests/test_member_action_intent.py`（全文件仅 29 行 3 条）。

**为什么不够**：身份链路有三个独立来源，无任何测试断言它们一致：
1. speaker block 用 `focus_context.focus_sender_id`（`planner_prompt_context.py:155-156`）；
2. 关系/画像数据用 `event.get_sender_id()`（`planner.py:1224` → `planner_side_inputs.py:891-897` 的 `get_user_profile(user_id)`/`relationship_engine.get_or_create(user_id)`）；
3. gate 的 fast-wakeup/force-engage 路径直接 `sys2_process(event, [event])`（`gate.py:823`）派发原始事件而非 focus 事件。
终线 guard 只能修复"外人名+后缀称呼"（`group_actor_consistency.py:148-154` 的 `_ADDRESS_SUFFIXES` 11 个后缀），裸名直呼、关系数据串号完全不设防。零个测试构造"A、B 两个 sender 交替发言→断言 prompt 中 speaker block 的 QQ 号 == 关系块的主体 == 被回复者"。

**建议最小回归测试**：单文件集成测试，构造 gate→judge(stub REPLY)→planner._prepare_plan_context 链，窗口内 A(id=1,昵称甲)、B(id=2,昵称乙) 交替 3 条消息、focus 落在 B；断言 (a) prompt 含 "QQ: 2"，(b) `planner_side_inputs` 收到的 user_id == "2"，(c) actor guard 对 "甲哥哥" 回写 "你"。

### 维度 2 私聊连续输入 — coordinator 覆盖好，gate 消费侧组合分支缺（TG-07）

**现有**：`tests/unit/conversation/test_private_turn_coordinator.py`（593L，17 条）在 4da2910 +330 行后覆盖了：settle 窗口重置、pending batch 修订保留、批文本合并顺序、vision 成功/emoji/意外异常 fail-open、timeout_fallback 清 refs+占位符、require_analysis abort、部分成功、占位符去重、resolver 缺失（图/纯文本）、跨会话不阻塞、_record_outcome 脱敏。`tests/test_private_topic_continuity.py` 8 条覆盖话题续期/确认/跨聊隔离/过期。gate 消费：`test_attention_gate_refactor.py` 两条（群聊 abort 停在 dispatch 前、私聊 abort 停在 mood/judge/sys2 前）。

**4da2910 新增 411 行中无测试的分支**：
- `_prepare_event` 的 **resolve 超时**分支（`private_turn_coordinator.py:403-418`，`outcome="resolve_timeout"`）——所有测试的 resolver 都瞬时返回；
- `prepare_batch` 在 **resolver=None 且多事件带图**时的逐事件 `_apply_failed_policy` 聚合（L233-246）；
- gate 侧 **屏障期间新消息到达→池回填→re-merge 续跑**（`gate.py:1311-1315`）与 **abort 后池非空→continue**（`gate.py:1331-1336`）——这是"用户补充消息后旧任务停止/合并"的核心可感知行为，现有 `test_debounce_worker_drain_loop_keeps_late_arrivals` 只测了群聊无屏障场景。

**建议**：一条测试用慢 `prepare_batch`（await Event）+ 屏障期间注入第二条消息，断言第二轮批次含两条消息且第一轮不发失败通知；一条测试 abort 分支后池非空续跑。

### 维度 3 单轮调用预算 — 机制单测有，端到端保证零守护（TG-03）

**现有**：`test_turn_call_ledger_refactor.py::test_turn_budget_clamps_noncritical_timeout_and_keeps_reply_reserve`（L212-223，唯一 budget 测试）；`test_reply_freshness_budget_ported.py` 3 条（стale 分类/预算缩放）。

**为什么不够**（三个执法点全部无测试）：
- 接线点：`message_entry.py:145-156` `_configure_turn_budget` 从 `config.timing.turn_total_budget_sec` 读值且 **异常整体吞掉只留 debug 日志**——若字段改名/config 结构变化，预算静默失效、所有 clamp 变 no-op（`remaining_turn_budget` 返回 None → `clamp` 原样放行）。`turn_total_budget_sec` 在 tests/ 中 **0 次出现**。
- 网关执法点：`gateway_call.py:283-289` `effective_timeout <= 0 → raise TimeoutError("turn_deadline_exhausted")` 无测试（tests 中无 `turn_deadline_exhausted`）。
- judge 执法点：`decision_router.py:115-128` `judge_budget_exhausted → PASS` 降级无测试（peer_poke 超时分支有测试，预算耗尽分支没有）。
- 无"慢模型→turn 总时长 ≤ 预算"的复合测试。运行时证据：`remaining_ms p05 = 0`、budget exhausted 1 次、单次 gateway.chat max 122.8s、私聊 turn 55.1s——耗尽在生产真实发生，行为却无锚定。

**建议**：fake clock + 慢 `llm_generate`（sleep 可控），configure budget=5s/reserve=2s，跑 dialog 调用→断言实际 wait_for timeout ≤3s 且 ledger `budget.exhausted` 状态正确；另一条直接删掉 `config.timing` 断言 `_configure_turn_budget` 后 clamp 仍有默认 360s 兜底（守护静默失效路径）。

### 维度 4 Provider 失败降级 — 失败矩阵缺 not-found 轴（TG-02）

**现有**：`test_gateway_context_passthrough_refactor.py` 17 条覆盖：cooldown 跨模型、全冷却 override（关键/非关键）、side-effect 后禁重试、空响应/超时分类重试、fallback 池标注、并发预算；`test_gateway_vision_refactor.py` 覆盖 vision 池内轮换/健康序/冷却跳过。`test_gateway_policy_refactor.py` 仅 1 条（3 个输出类分类）。

**为什么不够**：`gateway_policy.py:169-191` `_is_fatal_failure` 关键字表含 429/403/quota/timeout 等，**不含 "没有找到"/"not found"/"provider"**；`_classify_failure_kind`（L147-167）也无 not-found 类。后果：配置漂移导致的缺失 provider（AstrBot 抛"没有找到 ID 为 openai/deepseek-v4-pro 的提供商"）被判非致命 → 同一个永远不存在的模型被重试 `max_retries+1` 次、每次夹 backoff sleep（`gateway_call.py:340-377`），才轮到下一模型。运行时证据：model_attempts 层 ProviderNotFoundError 3 次、star.context WARN 4 条——该路径在生产被真实踩中。timeout×首模型/5xx×fallback 有测试，**not-found×任意位置 0 测试**；也没有"首模型 not-found→立刻切下一模型不浪费重试"的行为锚定。

**建议**：参数化矩阵测试 `(timeout|5xx|not-found) × (primary|fallback)`，llm_generate 按模型 id 抛对应异常，断言每种组合的 attempt 次数、是否 backoff、最终模型。not-found 应断言 attempt==1（当前代码会失败——这同时是行为缺陷，归 gateway 域修）。

### 维度 5 记忆闭环 — 写→检→注 有集成测试，修订腿断裂（TG-05）

**现有**：`tests/integration/test_memory_write_retrieve_inject.py` 5 条（instant gate 写入 canonical、偏好写→检、排序、弱查询空返回、检索→prompt bundle 渲染）——真 SQLite 全链，质量高。`test_memory_v2_services.py::test_review_pending_jargon_...`（L786-826）有 store 级 `update_memory(status)` 翻转→tool 检索反映。projector 单测 4 条（重建去重/pending/修复/维护周期）。

**为什么不够**："WebUI 修订后检索立即反映"无任何测试：`memory_ui_service.update_canonical`（L295-352）修订 content 后调 `projector.project(memory_id)` 重投影，但 WebUI 层测试全部 `index_projector = None` 或 mock store（`test_webui_backend_refactor.py:516-552` 的 `_Engine.index_projector = None`）；集成测试没有修订腿。若 update→project 的 wiring 断了（例如 projector 获取路径变化、project 对已投影 id 提前返回旧文档），旧内容会继续被注入 prompt——用户在 WebUI 改了记忆但 bot 仍说旧事实，且三层测试（WebUI mock、store 单测、写检注集成）全部绿灯。

**建议**：在现有集成测试文件加一条：write("喜欢火锅")→retrieve 命中→通过真实 `MemoryUiService(update_canonical)`+真实 projector 修订为"讨厌火锅，喜欢烧烤"→再 retrieve("烧烤")命中新内容且 retrieve("火锅") 的 top1 不再是旧文案→`injection.build_bundle` 渲染含新内容。

### 维度 6 工具连续调用 — 基本覆盖，留一个中断口子（无独立 finding，并入报告）

`tool_loop_agent` 本体在 AstrBot host 侧，插件可测面已覆盖：side-effect 后禁级联重试（`test_tool_loop_does_not_retry_after_recorded_side_effect`）、TERMINAL_YIELD 透传、超时分类重试、并发预算共享、required-tool 缺失→澄清话术（`test_tool_mode_missing_required_tool_sends_clarification_without_alert`、`test_fatal_fallback_converts_required_tool_error_to_clarification`）、multi_tool_max_steps 配置（`test_chat_tool_tier_uses_configured_multi_tool_max_steps`）。剩余口子：**side-effect 已发生（如已点赞/已戳）→ 级联终止抛 `LLMCascadeFailureException` 后用户是否收到任何可见回复**没有端到端断言——建议一条 executor 级测试：tool trace 已含 success 项+gateway 抛级联异常→断言 outbound_error_policy 产生降级话术而非静默。P3 级，不单列 finding。

### 维度 7 配置热更新/非法配置 — 覆盖良好（无 finding）

`test_config_standalone_refactor.py` 12 条：默认值、legacy timing 迁移、中心 timing 覆盖旧位、vision policy 别名+非法值归一（L76-81 直接测了 `"unknown"→超时后忽略`）、schema 可解析性。`tests/integration/test_hot_config_consistency.py` 6 条：热更新刷新组件、幂等、**失败全组件回滚**、work_mode 需运行栈、persona 改动要求重启。非法数值（负/零 timeout）在消费端有 `max(0.1, ...)` 钳制（`private_turn_coordinator.py:78-124`）。此维度是测试组织的正面样板。

### 维度 8 WebUI 契约 — 断言深度=服务层+注册表镜像，无前后端自动对齐（TG-06）

**现有断言深度实测**：`test_webui_backend_refactor.py` 大多数用真 aiosqlite 建表调 service 方法（比 mock 深），部分用 mock runtime；`test_plugin_pages_admin_refactor.py::test_native_admin_api_registers_core_routes`（L18-88）把后端注册表和一份**手工维护的路径清单**对照；真正“走 route handler”的只有 1 条（L90-103 persona/slices handler 直调）。**前端 `pages/admin/app.js` 有 75 个去重后的 `api.get/post` 路径**（经 `pluginEndpoint`，L352-357），没有任何测试解析它们并验证每条都能命中后端注册表。round11 的防御是字符串 pin（`test_round11_runtime_contracts.py:236-245` 直接 assertIn JS 片段），极易随重构腐化。历史证据：`.agent/final-functional-audit/12_webui_plugin_pages.md` 人工抓到 ≥4 例 FE/BE 契约漂移（双层 .data 解包、persona cache 路径不一致、review 字段名 expression vs text、legacy list 不含 canonical）——这类 bug 已反复发生，自动守护至今缺位。

**建议最小测试**：正则抽取 app.js 中 `api.get("...")/api.post("...")`/模板字面量路径（`${var}` 归一为 `{param}`），注册 `register_astrmai_admin_pages` 到 fake context 收集后端 path 集合（`{chat_id}`/`<chat_id>` 双格式归一），断言前端每条路径都存在于后端集合。纯静态、零网络、<50 行。

### 维度 9 Trace/观测自洽 — memory_funnel 是真缺口，context_block_stats 是误报（TG-04）

**用脚本核实 585 条 recent traces**（`scratchpad/trace_fields_out.txt`）：
- `context_block_stats`：executed 67/67 **全量存在**；缺失的 511 条全部是 skipped_* / topic_confirmation 状态（根本不进 context build）——**主控简报里"511/585 缺失"的疑点应澄清为预期行为，不是回归**。
- `memory_funnel`：executed 67 条中仅 3 条存在（+stale_drop 2）。executed 的 memory.policy 分布 none=50 / light=17 —— 即 17 条 light 里 14 条也没有 funnel。根因：funnel 只在 `MemoryInjectionService.build_bundle` 内部写（`memory_injection_service.py:182-188` `remember_funnel`），而 `prompt_refiner.py:646-697` 有 **7 条 early-return 跳过路径**（lightweight/near_context_priority/empty_query/think_level_0/think_level_1_no_intent/fast_mode/service_unavailable）在调 build_bundle 之前就 return，只写 decision.skip_reason 不写 funnel。`scripts/analyze_turn_ledger.py:223` 把无 funnel 计入 missing → 运营者在报告里看到 580/585 缺失，无法区分"合理跳过"和"仪表坏了"。
- c4aee57（"complete turn trace observability"）的测试只锚定 context stages 顺序、judge 计数、turn_id 去重（`test_turn_trace_store_v2_refactor.py` +31L），**没有任何"executed trace 必含哪些字段"的 schema 完整性契约测试**——这正是这次 64/67 缺失能溜进生产的原因。
- 主控疑点②（reply.send metadata sent_segment_count=0 vs reply_stats=2）与④（judge attempts:0 但 success）属观测域代理范围，此处仅确认无测试锚定（related）。

**建议**：契约测试 `executed trace ⊇ {llm_call_ledger≠[], stage_ledger≠[], context_block_stats≠[], memory_funnel≠{}}`；配套让 prompt_refiner 的 early-return 也调 `remember_funnel({status:"skipped", skip_reason:...})`（修复归属 conversation/memory 域）。

### 维度 10 测试基建质量 — 健康，一处文档漂移（TG-08）

- **收集**：1673 条，0 collection error（session-state 声称 1142 已过期，非 P1）。
- **manual 脚本未腐化**：8 个脚本 AST 语法全过；所有 `from astrmai...` 顶层符号在装 astrbot stubs 后全部可解析（`scratchpad/manual_api_check.txt`：TOTAL broken 0）；且每个大 manual 脚本都有被收集的 wrapper 回归（`test_group_trace_audit_refactor.py`、`test_main_reply_cache_replay_live_refactor.py` 5 条、`test_prompt_metrics_compare_refactor.py` 5 条）守护其内部函数。
- **helpers**：`scheduler_webui_fixture.py`(1142L)/`state_bar_audit.py`(1101L) 分别有 `test_scheduler_fixture_refactor.py`/`test_state_bar_audit_refactor.py` 专测。conftest 无全局魔法，测试自装 stubs（`tests/helpers/astrbot_stubs.py`），隔离性好。
- 唯一动作项：更新 `.agent/session-state.md` 的测试计数与入口命令（P3 文档项）。

## 2. 领域级测试缺口总结

跨模块回归缺口按风险排序：**身份隔离 E2E（TG-01） > provider not-found 矩阵（TG-02） > turn budget 端到端（TG-03） > trace 字段完整性契约（TG-04） > 记忆修订闭环（TG-05） > WebUI 路径对齐（TG-06） > 私聊屏障×续跑组合（TG-07）**。共性模式：每次提交都补了"新函数的单元测试"（4da2910 +468 测试行是好习惯），但**没有人写"跨两个模块的行为不变式"测试**——身份一致、预算总量、修订可见性、字段完整性全是这一类。

## 3. 附录：分析脚本输出摘要

- `pytest --collect-only -q`：1673 collected / 9.12s / 0 errors（tail 存 scratchpad/collect_tail.txt）。
- trace 字段完整性（585 recent）：memory_funnel 缺 580/585（executed 内缺 64/67）；context_block_stats 缺 511/585 但 executed 内 67/67 全有；llm_call_ledger 缺 42/585（多为 sensor_filter）；budget/decision_observation 0 缺失。
- manual API 检查：8 文件 0 broken import。
