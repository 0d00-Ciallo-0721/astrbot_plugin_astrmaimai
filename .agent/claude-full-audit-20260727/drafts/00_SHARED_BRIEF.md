# AstrMai 全量只读审计 — 子代理共享简报

> 生成时间: 2026-07-26。主控在派发领域审计代理前完成的侦察摘要。所有子代理开工前必读本文件。

## 1. 铁律（违反即报废）

1. **只读审计**。禁止修改任何源码、测试、配置、数据库、Trace 文件。你唯一允许写入的位置是 `.agent/claude-full-audit-20260727/drafts/`（你的领域报告 + findings JSON 片段）和系统 scratchpad 目录（临时分析脚本）。
2. **禁止只依据文件名、注释、docstring 下结论**——必须读到实际代码行为。每个发现必须引用真实行号和代码原文。
3. **代码风格、纯安全问题不在本轮范围**。只关注功能正确性、架构、延迟/成本、数据质量、用户可感知行为。
4. **不要机械列出低价值问题**。每个发现必须能回答"用户/运营者会感知到什么后果"。宁可 8 条扎实，不要 30 条水货。
5. 分析 Trace/日志用 Python 时注意 Windows 控制台是 GBK：加 `PYTHONIOENCODING=utf-8` 或把结果写文件再读。项目根目录运行 `python` 可用。

## 2. 项目地图

- 入口: `main.py` → `astrmai/app/plugin_facade.py` (801L) → `bootstrap.py` (555L) / `lifecycle.py` (484L) / `runtime_context.py` (505L)
- `astrmai/conversation/` — 消息→注意→判决→规划→执行主链路
  - ingress: `sensors.py` (681L), `command_guard.py`
  - attention: `gate.py` (1565L), `private_turn_coordinator.py` (734L), `decision_router.py`, `thread_builder.py`, `focus_selector.py`, `group_dialogue_store.py` (680L), `context_compaction.py` (1839L), `compaction_providers.py` (467L)
  - decision: `judge.py` (558L)
  - planning: `planner.py` (1911L), `planner_side_inputs.py` (1756L), `prompt_refiner.py` (1112L), `cognitive_loop.py` (745L), `context_engine.py` (779L), `conversation_continuity.py` (565L), `planning_input_loader.py` (492L), `planner_prompt_context.py` (565L), `think_level_policy.py` (397L), `expression_policy.py` (698L), `tool_disclosure.py` (446L), `tool_intent_resolution.py` (250L), `tools/pfc_tools.py` (2991L), `agency_runtime.py`, `member_action_intent.py`
  - execution: `executor.py` (1299L), `reply_service.py`, `reply_artifact_builder.py` (672L), `reply_freshness.py` (290L), `reply_post_send.py`, `qq_action_dispatcher.py`, `group_actor_consistency.py`
  - loop: `chat_loop_kernel.py` (2336L)
  - contracts: `turn_context.py` (653L), `prompt_envelope.py`
- `astrmai/infrastructure/`
  - gateway: `gateway_call.py` (569L), `gateway_lane.py` (1025L), `gateway_policy.py`, `gateway_tasks.py` (494L), `model_router.py` (253L), `gateway_result.py`, `output_guard.py`
  - runtime: `turn_call_ledger.py` (697L), `turn_trace_store.py`, `chat_runtime_coordinator.py` (477L), `lane_storage.py`, `observability.py`, `event_bus.py`
  - persistence: `persistence_schema.py`, `database_*.py`, `orm_models.py`, `state_profile_persistence.py`
  - context_economy: `center.py` (473L), `prompt_templates.py` (662L)
  - compat: `legacy_compat.py`
- `astrmai/memory/` — 记忆 v2: `services/v2_store.py` (2031L), `memory_engine.py` (1265L), `memory_retrieval_service.py` (1001L), `memory_injection_service.py`, `memory_write_service.py`, `memory_admission_service.py`, `memory_claim_service.py`, `memory_maintenance_service.py` (429L), `instant_memory_gate.py` (367L), `session_memory_summarizer.py`, `topic_summarizer.py` (414L), `memory_index_projector.py`, `memory_migration_service.py` (634L), `memory_turn_pipeline.py` (515L), `memory_tool_service.py`, `memory_observer.py`, `cognitive_feedback.py`, `expression_pattern_service.py` (560L), `memory_query_builder.py` (399L); retrieval/: `react_retriever.py` (419L 含 query_rewrite), `embedding.py`; persona/: `persona_summarizer.py` (1338L); dream/: `dream_agent.py` (560L)
- `astrmai/learning/` — `evolution_manager.py` (855L), mining/ (jargon_*, expression_*), review/ (reflector, jargon_auto_check_task, reflect_tracker)
- `astrmai/state/` — `user_profile_service.py` (644L), relationship/`relationship_engine.py` (620L), `chat_state_service.py` (600L), group_wait/`group_reply_wait_manager.py` (483L), private_chat/, mood/
- `astrmai/proactive/` — heartflow/`manager.py` (1175L), `proactive_task.py` (896L), `dream_scheduler.py`, `dispatcher.py`, `wakeup_service.py`
- `astrmai/webui/` — backend/routes/*, backend/services/* (`memory_ui_service.py` 1325L, `admin_ui_service.py` 1136L), `plugin_pages.py` (786L), adapters/`plugin_api.py`; 前端 `pages/admin/` (app.js/index.html/style.css)
- `astrmai/workmode/` — sys3 subagents/, cron_guard/heartbeat.py（服务器上 Sys3 已禁用，日志证实 chat-only mode）
- `astrmai/multimodal/` — `visual_cortex.py`, `napcat_image_resolver.py`
- `astrmai/presentation/events/message_entry.py` (346L)
- 配置: `config.py` (29.5KB), `_conf_schema.json` (58.9KB)
- 测试: `tests/` 277 个文件；分析脚本 `scripts/analyze_turn_ledger.py`

## 3. 运行时证据（必用）

### 3.1 数据文件

| 路径 | 内容 |
|------|------|
| `.agent/runtime-observability-c4aee57-20260726/turn_trace_samples_server.json` | 14.3MB，schema v2，`{version, capture_started_at, by_chat: {chat_id: [trace...]}, recent: [...]}`。15 会话 392+ traces（recent 585），16h 覆盖，llm_call_ledger 覆盖 94% |
| `.agent/runtime-observability-c4aee57-20260726/astrbot_since_c4aee57.log` | AstrBot 框架日志 1.3MB（最新代码 c4aee57 之后） |
| `.agent/runtime-observability-c4aee57-20260726/astrmai_diagnostics.log` | 插件诊断日志 364KB |
| `.agent/runtime-observability-c4aee57-20260726/ledger_analysis.json/.md` | 已有脚本产出的账本统计 |
| `.agent/runtime-observability-20260726/` | 前一天的快照（旧代码 20bb585 时期，可对比） |
| `.agent/turn_trace_samples_server.json` | 更早快照 (8.6MB, schema v1 时期) |
| `.agent/turn-ledger-server-analysis-20260725.md` | 7-25 的人工分析（judge 重复调用、query_rewrite 79s 等结论，部分可能已被 20bb585/c4aee57 修复——需要你验证新旧） |

### 3.2 Trace v2 单条结构（关键字段）

`created_at, status, chat_id, reply_sent, reply_preview, perception{sender_id, sender_name, text_preview, image_count, is_private, is_direct_wakeup, is_at_bot, is_reply_to_bot, is_strong_wakeup}, attention{judge_action, retrieve_keys, is_fast_mode, focus_preview, warm_*_preview, recent_transcript_*}, cognitive{action, think_level, think_reason, cognitive_loop_ran}, continuity{...缓存前缀 hash/长度/块分布, usage_input_tokens, usage_input_cached, cache_hit...}, conversation_compression{...}, memory{policy, injected, skip_reason}, expression_patterns, follow_up, side_inputs.timings[], proactive, tools{disclosure_*, filter_steps[]}, turn_id, thread_id, generation, turn_total_elapsed_ms, llm_call_ledger[{call_id, sequence, stage, family, pool, status, elapsed_ms, system_chars, prompt_chars, model_attempts[], model, output_chars, error_kind}], context_block_stats[{stage, blocks{...chars/hash/duplicate_of}}], stage_ledger[{stage, status, elapsed_ms, metadata}], reply_stats{segment_*, send_status, freshness_state, reply_age_sec}, budget{total_budget_sec, remaining_ms, exhausted}, memory_funnel, decision_observation{...}, tool_execution_trace[], tool_lifecycle_trace[]`

### 3.3 主控已核实的统计（来自 ledger_analysis.json + 日志 grep，可直接引用，也应抽样复核）

- 585 traces：executed 67+2，skipped_ignore 317，sensor_filter 102，skipped_wait 83（judge_wait 50 + ambient_short_wait 33），stale_drop 7，repeater_echo 7
- LLM 1022 次调用：gateway.chat 950 / gateway.tool 69 / attention.compaction.v2 3；成功 1005，error 11，abandoned 3，cancelled 3；模型尝试层面 timeout 13 + error 12 + ProviderNotFoundError 3
- 延迟：gateway.chat p50 7.4s / p95 20s / max 122.8s；attention.dispatch p50 4.4s / p95 14.6s / max 40s，且有 63 次 abandoned；memory.injection 仅 6 样本但 p50 29.8s / max 92s；planner.context_build p95 16.9s；planner.prompt_refine max 71.9s（离群）；reply.send p50 1.4s
- calls_per_turn p50=2 p95=4；judge_calls_per_turn p50/p95 = 0（注意：judge_outcomes 却有 420 条 ignore/reply/wait——judge 调用是否没进 ledger？需核实 judge 走哪条代码路径、是否绕过 turn_call_ledger）
- query_rewrite timeout 3 次；budget exhausted 1 次；remaining_ms p05 = 0
- context: transmitted p50 9814 chars；source 层 duplicate_block_count 78（focus_message duplicate_of raw_user_text 是最常见对）
- 日志 WARN 178 条：模型超时 40（code3/deepseek-v4-flash 20 + code2/deepseek-v4-flash 20，均 "timeout (1/3)"）；[Gemini] request_retry 风暴 43 条（2/5→5/5）；instant_memory_gate backfill degraded 17；context_economy cache-priority workload 12；star.context "没有找到 ID 为 openai/deepseek-v4-pro 的提供商" 4；gateway_tasks implicit global scope family 4；deep query rewrite degraded 3；vision gemini-3-flash-preview failed→fallback 2；executor tool model failed 2
- AstrBot core: `default_provider_id=google_gemin/gemini-3-flash-preview` 不存在 → fallback `moonshot/kimi-k2.5`（框架层配置漂移，插件模型池独立于此）
- Sys3 (Work) 服务器上 disabled，Computer/Cron 子代理降级 WARN
- 服务器模型池: Judge=code2/deepseek-v4-flash, Agent=code2/deepseek-v4-pro, dialog=code3/deepseek-v4-pro（trace 所见）
- 主控在样本 trace 里已看到的疑点（请核实归属领域）: ① 私聊 turn 总时长 55.1s，其中 turn_start→judge 调用开始有 14.1s 空档、judge 结束→context_build 又 9.1s 空档；② stage_ledger reply.send metadata `sent_segment_count: 0` 与 reply_stats `sent_segment_count: 2` 矛盾（instrumentation bug）；③ 私聊 trace 的 warm_summary 模板文案写"延续刚才的**群聊**话题"（私聊语境错误）；④ judge ledger 条目 `attempts: 0, model_attempts: []` 但 status=success（计数缺口）；⑤ `provider: "unknown"`、`request_provider_family: "unknown"` 全量如此

### 3.4 历史审计（去重基线）

`.agent/final-76-bug-reaudit.md`（76 bug 复审）、`.agent/bug-fix-rounds-2026-07-13/`（8 轮修复 ROUND_01~08 + 00_DEDUP）、`.agent/final-functional-audit/`（10 份分模块功能审计）、`.agent/round3~8-*.md`、`.agent/test-gap-audit-master.md`、`.agent/test-coverage-audit-codex-review.md`（真实覆盖率 72.9%）、`.agent/test-catalog-complete.md`（1168 测试目录，496KB 慎读，用 Grep 按需查）。
**要求**：你的每个发现都要判断 `known_status`：NEW（历史报告没提过）/ KNOWN_OPEN（提过没修）/ KNOWN_FIXED_REGRESSION（说修了但代码或 Trace 显示又坏了）/ 不要报告已确认修复且现状良好的旧问题。用 Grep 在上述文件里搜关键词做对照，不需要通读全部历史报告。

## 4. 产出契约

每个代理产出两个文件到 `.agent/claude-full-audit-20260727/drafts/`：

1. `NN_<领域名>.md` — 完整领域报告（结构：领域概述 → 数据流/调用链实测 → 逐条发现（含证据代码块）→ 领域级测试缺口 → 附录：分析脚本输出摘要）
2. `findings_NN.json` — 结构化发现数组，每条：

```json
{
  "id": "<前缀>-01",
  "domain": "<领域slug>",
  "title": "一句话标题",
  "severity": "P0|P1|P2|P3",
  "veracity": "VERIFIED|LIKELY|NEEDS_RUNTIME_EVIDENCE",
  "category": "BUG_CODE_CERTAIN|RUNTIME_DATA_ISSUE|DESIGN_IMPROVEMENT",
  "user_impact": "用户/运营者可感知后果",
  "root_cause": "根因",
  "evidence": [{"file": "astrmai/xx.py", "lines": "L120-L145", "quote": "关键代码原文(<=6行)"}],
  "runtime_evidence": "trace/log 数字佐证，无则空串",
  "related_tests": ["tests/test_x.py::test_y"],
  "test_gaps": "缺哪类测试",
  "duplicate_of": null,
  "related_findings": ["RT-02"],
  "known_status": "NEW|KNOWN_OPEN|KNOWN_FIXED_REGRESSION",
  "fix_boundary": "最小修复边界（文件+函数级）",
  "regression_risk": "低/中/高 + 一句话",
  "verification": "验证命令或方法",
  "ready_for_dev": true
}
```

严重级别定义：P0=用户明显受害或数据损坏且高频；P1=用户可感知的错误行为/显著延迟或成本浪费；P2=边缘条件下错误或明确的优化机会；P3=打磨项。
真实性定义：VERIFIED=代码逻辑闭环可证或 Trace/日志直接命中；LIKELY=代码强烈暗示但缺一环；NEEDS_RUNTIME_EVIDENCE=需要线上补采数据才能定论。

3. **返回给主控的最终文本**：只返回紧凑清单——每条发现一行 `ID | severity | veracity | title | file:line`，加 3-5 句领域整体结论。不要在返回文本里贴大段代码。

## 5. 建议方法

- 先 `git log -15 --oneline -- <你的领域目录>` 了解最近变更热点，重点提交可 `git show <hash> --stat`
- 用 Read 全文阅读你领域的核心文件（大文件分段读完），小文件顺带
- 对 Trace/日志写一次性 Python 分析脚本放 scratchpad，输出写文件
- 交叉：源码断言 ↔ Trace 字段 ↔ 日志行，三者能对上的才标 VERIFIED
- 你领域内部先自行去重；跨领域重复不用担心，主控统一处理，但在 related_findings 里标注你怀疑与其他领域重叠的点
