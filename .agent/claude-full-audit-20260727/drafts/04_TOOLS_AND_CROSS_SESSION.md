# 04 工具系统与跨会话执行 — 领域审计报告

> 审计代理：Domain 04。基线代码 c4aee57；运行时证据 `.agent/runtime-observability-c4aee57-20260726/`（585 traces / 16h / 15 会话）。全程只读。

## 1. 领域概述

工具链五层：**披露**（`tool_disclosure.py` 静态包定义 + `planner_side_inputs._build_execution_tools` 应用）→ **意图解析**（`tool_intent_resolution.py` + `member_action_intent.py`，产出 clarify/required）→ **执行**（`executor._run_tool_mode` → `gateway_lane.tool_chat_in_lane_result` → AstrBot `tool_loop_agent`，max_steps=5）→ **副作用提交**（工具内直发 或 `astrmai_pending_actions` 延迟到 `qq_action_dispatcher.commit`，随可见回复提交）→ **观测**（`astrmai_tool_execution_trace`/`tool_lifecycle_trace` → planner `_remember_turn_trace` 入 trace）。

工具全集 35 个（`tool_contracts.TOOL_CAPABILITIES`），首层 core 包 6 个（wait_and_listen / omni_perception_query / self_lore_query / cross_chat_memory_query / persona_fact_check_tool / bot_capability_lookup），二段可开放包限 identity/relationship/artifact/memory_governance 四个只读包（`SECOND_PASS_ALLOWED_PACKAGES`）。

## 2. 数据流/调用链实测（trace + 日志交叉）

### 2.1 披露与实际使用（585 traces）

- disclosure_enabled 70 轮（=全部执行轮），tier chat:56 / task:14；包分布 core:70, fun:7, artifact:5, identity:1, relationship:1；**62 轮只披露 core 6 件套**。
- `gateway.tool` 69 次调用全部 success，p50 8.3s / p95 18s / max 27.8s，max_steps=5、tool_count 与 filtered_tools 一致（观测无缺口）。
- **65/68 工具轮模型 0 次工具调用**（tool_execution_trace 空是真没调用，不是没记录——3 个真调用的轮次记录完整）。即工具模式主要作为"带工具的主回复"，实际工具使用率 4.4%。
- **disclosure_expanded_packages 全 585 轮为 0 次**；bot_capability_lookup 16h 内从未被模型调用 → 二段展开机制实际死路（TL-01）。
- invocation_mode：auto 584 / required 1 / clarify 0。唯一 required 轮（"你是小锦…你是谁" → self_lore family）完整走通 enforcement：planned:required → missing → required_tool_retry → tool_completed → satisfied（正面验证 ddc73ea/3a38e12 链路），代价 = 3×gateway.chat + 2×gateway.tool ≈ 60s+。
- perception.image_count 全部 585 轮恒 0，包括 vision ledger 有 9 次尝试、模型实调 vision 工具的图片轮（TL-07，观测断裂）。

### 2.2 模糊语义路径（Q2 结论）

规划层对模糊意图是**先澄清、不强选**：`resolve_explicit_tool_intents` 对 private/at/memory_correction/meme 等家族做槽位守卫；缺槽 → `astrmai_tool_clarification_needed` + planner 注入"这轮不要声称已经执行，先自然追问"（planner.py L468-484）；若无任何 ready 家族 → `invocation_mode=clarify` 且**返回空工具集**（planner_side_inputs L1170-1173），走纯文本澄清回复。槽位齐 → `build_explicit_invocation_plans` → required 注入 + `prepare_explicit_tool_fallbacks` 确定性预排队（自戳/emoji/群签/撤回/meme 五类不依赖模型选择）。模型拒不调用 required 工具时 executor 单次强制重试（限定 retry_tools + enforcement prompt），再失败则发诚实澄清文本（`_required_tool_missing_reply`），不伪造执行。member_action ddc73ea 修复生效：purpose ∈ {discuss_member, unclear} 时 construct_at_event 被移除（L1031-1032），且 construct_at_event 本体强制 NapCat 当前群校验。

### 2.3 连续工具调用与失败面（Q3 结论）

多步工具结果**统一在框架 tool_loop 内消费，只输出最终文本**；用户看不到中间步骤。`[SYSTEM_WAIT_SIGNAL]` → 静默（skipped_wait）；`[TERMINAL_YIELD]:` → 1:1 复读直出。中间某个 pfc 工具失败返回错误字符串（循环继续，模型可自适应）；整轮失败按 executor 模型级联换模型**整轮重跑**（前轮工具结果丢弃）；全池耗尽 → 发 fallback_text"（陷入了短暂的沉默...）"+ 管理员私聊告警。日志中 3 次 `executor:1081 tool model failed` 实为**过期回复被误分类为模型失败**（TL-05），非真模型故障。

### 2.4 跨会话（Q4 结论）

- `space_transition_action`：真实 `astr_ctx.send_message` 直发好友私聊；relay 模式自动加"{发起人}让我转告你："前缀（来源标注✓）；发送失败/目标非好友/接口异常均返回"发送失败：…消息未发送"要求如实转告（错误回传契约✓）；发送前 turn freshness 校验防重载期发送。
- Handoff 闭环：`CrossSessionHandoffStore`（内存，TTL 30min，每收件人 4 条，观察 3 轮）→ 目标会话 planner `_apply_private_jump_context` 注入【发起人 name+QQ】【跨会话摘要】【已发送消息】+ 三方消歧指令（发起人/机器人/收件人不混淆）。persona 一致性：目标会话用同一 persona 正常规划，注入块只补充语境——一致性由外层保证。`cross_session_reply_lookup`/`qq_recent_contact_lookup` 返回带 sender id 的消息摘要（来源可辨）。
- workmode/Sys3（服务器 disabled）：启用契约 = cognitive `tool_call`/`sys3` → judge_action=TOOL_CALL（`sys3_router is None` 时强制降级 REPLY，planner.py L1742-1751）→ 8 内置查询工具 + 子代理工具；子代理嵌套独立 tool_loop（Cron 8 步 / Computer 15 步 120s），经 gateway sys3 lane 享受路由/冷却；工具不可用时 `[SUBAGENT_DECLINE]` 诚实降级文案（计算机代理还需 `computer_agent_sandbox_enabled`）。`cron_guard/heartbeat.py` 快照复活带 `_pending_snapshot_swaps` 防重复复活 + 失败补偿删除，设计闭环，无发现。
- 唯一结构性弱点：handoff 仅内存 + 注入块 360 字符截断（TL-09）。

### 2.5 工具结果进上下文与缓存（Q5 结论）

工具结果只存在于 tool_loop 会话内；lane 历史仅追加 `raw_user_text + 最终可见回复`（`gateway_lane._finalize_success_artifacts` L122-137 只在 artifact_text 路径 append_visible_reply_artifact）→ **工具中间消息不进 lane 历史、不污染缓存前缀**（设计正确）。截断防线齐全：视觉描述 600 字符、列表工具 5-12 条封顶、topic_thread 每块 240、cognitive readonly observation 1200、跨会话注入 360。`copy.copy(event)` 共享 `_extras` 字典（AstrBot `set_extra` 原地写 `self._extras[key]`），`_sync_execution_event_trace` 实为冗余但无害。

## 3. 逐条发现

### TL-01 (P2) 二段披露展开机制 16h/585 轮 0 次触发，触发链路实践中不可达

- 展开唯一入口 = 模型调用 `bot_capability_lookup(needed_package=...)`（pfc_tools.py L2374-2382 写 `astrmai_requested_tool_packages`）→ executor 在**第一轮工具对话完整结束后**检查并**整轮重跑**（executor.py L966-989，prompt 前缀加 [SYSTEM TOOL DISCLOSURE]，成本双倍）。
- 现实：65/68 轮模型 0 工具调用，capability 工具从未被调；62 轮只有 core 6 件套，需要 identity/relationship 事实的问题（如 trace 1785069575"@小良 妃爱的什么时候有样品"披露了 identity 包是靠关键词，非模型自检）只能靠关键词命中，否则模型直接臆答。planner guidance（planner.py L518-524 chat tier）从不提示"工具不够可以调 bot_capability_lookup 请求开放"。
- 后果：付了 446 行披露机制 + 每轮 second_pass 计算的复杂度，换到 0 次实际展开；被裁工具的可达性完全退化为关键词表。

### TL-02 (P2) social_intent 家族过滤会清空披露层刚为图片/引用加的 artifact 工具（连 core 查询与 wait 也一并剥除）

- 链路：`has_image` → disclosure 加 artifact 包（tool_disclosure.py L386-391）；但 `social_intent ∈ {comfort, tease}` → `_families_for_social_intent` 只允许 {reaction,qq_reaction,like(,meme)}（planner_side_inputs L699-717），且 L1005 仅在 `intent_families is None` 时才把披露家族并入 allowed → `_filter_tools_by_families`（L1051）把 artifact/query/wait 全部滤掉。
- 实测：trace 1785050973（图片消息，vision 转写成功那次）`pkgs=['core','artifact','fun']` 但 `filtered_tools=['qq_custom_face_send_tool','proactive_meme','message_reaction_action','message_emoji_like_action']`——artifact 与 core 6 件套全灭。
- 后果：tease/comfort（7/70 轮）叠加 vision 转写失败（同窗口 vision timeout 40 次、fallback 2 次、一轮 9 连错）时，模型既无图片转写又无查图工具，只能臆测图片内容。

### TL-03 (P2) sanitized execution event 剥离消息组件，vision/artifact 工具"当前消息"路径必然假阴性

- `_build_sanitized_execution_event`（executor.py L99-118）把 `message_obj.message` 整体替换为 `[Plain(safe_text)]`；工具循环内 `_get_current_event` 拿到的正是该事件 → `vision_message_analyze_tool`/`qq_message_artifact_lookup`/`qq_forward_message_lookup` 的 message_id 留空分支读不到任何 image/forward 段。
- 日志实锤（astrbot_since_c4aee57.log L245-256, 06:12:40）：第一次调用返回"当前没有发现可分析的图片或表情包片段"（图明明存在），模型被迫经 artifact_lookup 拿 message_id 再走 NapCat `get_msg` 兜圈，最终因 VisualCortex 未完成只能回"图好像还在加载中……妃爱看不清啦"（trace 1785017592，4 次工具调用、gateway.tool 21.5s，全轮 30s+）。
- 后果：图片轮工具路径首步被系统性误导；若模型不追查（常见）会直接断言"你没发图"。叠加根因：vision 工具本身只读 `_visual_records_from_event` 缓存，无法触发/等待实时分析。

### TL-04 (P1, LIKELY) gateway 层 side-effect 中止保护被 executor 模型级联绕过，跨会话私聊消息存在重复真发风险

- gateway 保护：`_tool_side_effect_count`（gateway_lane.py L187-193，注意**纯查询工具执行也计数**）> 之前值且本次失败 → `abort_after_side_effect` break，不再级联（L995-1000, L1014-1015）。
- 但 executor `_run_tool_mode` 的 except（L1065-1082）不感知该语义：捕获 `LLMCascadeFailureException` 后照常 `continue` 换下一模型**整轮重跑**。新模型看不到上一轮已执行的工具（对话从零开始）。
- `space_transition_action` 是即时真发（非延迟提交），去重仅 `(target_id, outbound_message)` 精确文本匹配（pfc_tools.py L2734-2742）——换模型后措辞几乎必然不同 → 好友收到两条私聊。延迟类动作（poke/emoji/签到）靠 pending_actions 结构去重 + dispatcher `_executed_keys`，风险低，但失败尝试排入的动作仍会随 fallback 文本"（陷入了短暂的沉默...）"被 `qq_action_dispatcher.commit`（reply_service.py L184-191）提交——动作与最终文本语义脱节。
- 16h 窗口 space_transition 未发生（native/cross-session lifecycle 0 条），故 LIKELY；但触发前提（工具 side-effect 后本轮失败，如 output_guard 拒收最终文本）在日志中同窗即有同构事件（TL-05 的 3 次失败换模型）。

### TL-05 (P2) `is_stale_reply_reason` 漏配 `_same_thread/_unknown_thread` 变体，过期回复被误当模型失败重试

- 生产方 `chat_runtime_coordinator.evaluate_reply_freshness`（L469-477）产出 `superseded_by_newer_activity_same_thread:actor:Xs` / `..._unknown_thread:...`；消费方 `is_stale_reply_reason`（reply_freshness.py L50-58）只匹配 `"superseded_by_newer_activity:"`（带冒号原始格式，L209 另一产地）→ startswith 失配。
- executor `_finalize_reply` L798-804 走 else 分支 `raise RuntimeError(blocked_reason)` → `_run_tool_mode` except → WARN "tool model failed, trying next"（日志 3 次实锤：L3017/L3074/L5331，reason 均为 `superseded_by_newer_activity_unknown_thread:…`）→ 下一模型迭代靠 pre-model freshness 检查（executor:762）救回 stale_drop。
- 后果：误导性 WARN（运营会当成模型故障）、执行状态绕道、salvage 窗口内可能换模型完整重生成一轮（双倍 dialog 成本）。同文件内 L229 用的是不带冒号前缀（能匹配全部变体），同一文件两套判据。

### TL-06 (P2, LIKELY) “听说/据说/有人说/不确定”等日常词直接触发 unverified_report 显式家族 → 升级 task tier 并强制 required 工具

- `GENERAL_EXPLICIT_TOOL_KEYWORDS["unverified_report"] = ("听说","据说","有人说","未确认","不确定")`（planner_side_inputs L150）→ `_explicit_tool_families` 命中 → `_has_tool_intent=True`（task tier、16 工具上限、RAG 注入关闭逻辑连带）→ `resolve_explicit_tool_intents` 的 `_simple_slot_resolution` required_tokens 同为"听说…" → 必然 ready_required → `invocation_mode=required` 强制 `unverified_report_record_tool`，模型不调用则 enforcement 重试 + 最终发澄清文案"我还没能确认这次要执行的具体信息…"。
- 任何含"听说"的闲聊（"我听说你会唱歌"）都会走此链。16h 窗口 0 次命中（观察群闲聊未含触发词），故 LIKELY；一旦命中，用户看到的是答非所问的"操作确认"文案或被写入一条无意义的未核实报告。MEMORY_GOVERNANCE_KEYWORDS 同词表（tool_disclosure.py L288-298）还会同步开 memory_governance 包，放大暴露面。

### TL-07 (P2) perception.image_count 全量恒 0，图片轮在 trace 中不可辨识

- 585/585 轮 `perception.image_count=0`，包括 vision ledger 9 次尝试、vision 工具实调、回复内容明确讨论图片的轮次（1785017592/1785050973/1785051067）。
- 本域直接受害：审计要求的"image_count>0 轮次的 filtered_tools 对照"无法执行，只能改用 vision ledger/工具执行反推；运营侧无法量化图片轮的工具披露正确率。归属 observability 域修复（perception 采集点未接 direct_image_refs/组件计数），此处立据。

### TL-08 (P3) FAMILY_TO_PACKAGES["quote_reply"] 是死配置；引用场景永远拿不到 quote_reply_action 非显式路径

- `FAMILY_TO_PACKAGES["quote_reply"]=("artifact","native_action")`（tool_disclosure.py L99）但 quote_reply ∈ `PRECISION_ONLY_FAMILIES`（L132-149），plan() L375-379 把 precision 家族从包映射剔除 → 该映射永不生效；`TOOL_PACKAGES["artifact"]` 亦不含 quote_reply_action。has_reply 只加 artifact 查询包（L391）。16h 内 quote_reply_action 0 次披露、0 次执行。作为防滥用设计可辩护，但配置项与实际语义不一致，易误导后续维护（以为 artifact 包含引用回复能力）。

### TL-09 (P3) 跨会话 handoff 仅内存驻留 + 注入块 360 字符截断，三方消歧指令位于截断尾部

- `CrossSessionHandoffStore` 无持久化（bootstrap.py L74 每次实例化全新），插件重载/重启即丢失待衔接语境（TTL 本 30min）；目标好友稍后回复时 bot 冷启动应答，"传话人"人设断裂。
- `_apply_private_jump_context` 组装的 sys_inject（发起人+摘要+已发送消息+“不要把三者混为一人”消歧指令）整体 `_truncate_runtime_instruction_text(…, 360)`（planner_side_inputs L1306-1309），摘要/消息取满 90 字符时总长 ≈340-380，消歧指令（块尾 ~80 字符）最先被截；`planner_runtime_instruction_block` 还有 480 全局钳制叠加。

## 4. 领域级测试缺口

- `tests/test_tool_disclosure_refactor.py` 只覆盖包选择纯函数；**无** "social_intent 家族过滤 × 披露包" 集成测试（TL-02 场景：has_image + tease 应保留 vision/artifact 工具的期望行为无契约）。
- `tests/test_executor_refactor.py::test_tool_mode_can_expand_readonly_disclosure_package_once` 覆盖展开机制单元行为，但无"模型从不触发"的产品级信号（建议加 trace 断言/遥测告警而非单测）。
- **无** executor 级联 × side-effect 测试：模拟第一模型执行 space_transition 后抛错，断言第二模型不得重发（TL-04 的回归锚点）。
- **无** `is_stale_reply_reason` 与 `chat_runtime_coordinator` reason 格式的契约测试（两产地一消费者，字符串漂移无守卫）。
- **无** sanitized execution event 下 vision/artifact 工具当前消息路径的测试（TL-03：应断言剥段后工具仍能经 message_id 或原始事件访问图片段）。
- `tests/test_tool_invocation_contracts.py` 覆盖 required 构建，但无"听说"类日常词误触发的负样本集（TL-06）。

## 5. 附录：分析脚本输出摘要

脚本：scratchpad/analyze_tools.py + analyze_vision_turns.py（一次性，未入库）。

```
total unique traces: 585; executed 67 + executed_topic_confirmation 2
disclosure_enabled: 70 (chat 56 / task 14); packages core:70 artifact:5 fun:7 identity:1 relationship:1
second_pass published: 70/70; disclosure_expanded: 0/585
invocation_modes: auto 584 / required 1 / clarify 0; required_tools: {self_lore_query:1}
filtered_tools size: 6→56轮, 11→4轮, 5/4/3/9/10→各1-3轮; quote_reply_action/space_transition/poke/at 披露 0 次(除 identity 轮的 construct 无)
gateway.chat 950; gateway.tool 69 全 success; p50 8305ms p95 18043ms max 27800ms; max_steps=5 全部;
tool 调用轮 68，其中 tool_execution_trace 非空仅 3 轮（vision×2+artifact+topic / persona+self_lore(required) / omni）
image_count>0 轮：0（instrumentation 恒 0）；is_reply_to_bot 轮：0
双 gateway.tool 轮：1（required enforcement 重试）
native/cross-session 工具 lifecycle 条目：0
log: executor:1081 "tool model failed" ×3，reason 全为 superseded_by_newer_activity_unknown_thread；
     tool_loop_agent_runner 06:12:40 vision 工具首调返回"没有发现可分析的图片"（sanitize 剥段实锤）
```
