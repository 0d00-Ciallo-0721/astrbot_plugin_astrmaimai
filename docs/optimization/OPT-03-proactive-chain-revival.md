# OPT-03 主动行为链复活（wakeup / peer poke / 签到）

状态：代码完成（待线上首条主动消息验收） ｜ 优先级：P0 ｜ 依赖：无 ｜ 覆盖发现：PL-01(P0，已吸收 ID-02)、ID-03(P2)、ID-06(P2)、ID-10(P3)、PL-02(P2) ｜ 整个主动行为子系统自 4d16a82（7-18）起从未对外发出过一条消息。

## 目标

- wakeup/heartflow/群签到跟发的主动消息真正发出；群友互戳（peer poke）事件能进入判决而不是 100% 被过滤。
- 修复被 PL-01 掩盖的二级缺陷：主动消息的 prompt 不再对着幽灵用户"主动开口候选"说话。
- 诊断层如实归因：主动候选被哪一层拦截，日志/trace/WebUI 三处一致可见。
- 基线 → 目标：主动消息发出 0 条/16h（14/14 候选死于 sensor）→ 候选能走到 judge，由 judge 决定发或不发；peer poke 30/30 被滤 → 进入 playbook 频控+judge。

## 基线证据

- `dispatcher.py:327-351` 构造的合成事件只有 `message_str`，**无 message 组件**（`gate.py:36-39` `_SyntheticExternalEvent.message_obj=None`，注释"reserved for future use"）；`sensors.py:296` 的 `clean_text` 只从组件拼接 → `sensors.py:317-318` 空消息过滤 `return False` → `gate.py:1137` 记 `skipped_sensor_filter`。
- trace：14/14 `sender=astrmai_proactive_candidate` 的 turn 全部 `skipped_sensor_filter` 且 `llm_call_ledger=[]`；两个观测窗 grep `proactive wakeup sent via main chain` 均为 0。
- ID-03 同源：peer poke 叙事文本只在 `message_str`，`Poke` 组件不计入 `has_payload`（`sensors.py:643-646` 仅 bot 目标才设 `is_virtual_poke`）→ 30/30 被滤，PokePlaybook 的围观/加入剧本是死代码。
- ID-06 二级缺陷：`planner_prompt_context.py:148-180` 的"当前发言人归因锁"会把合成 sender（`主动开口候选`/`astrmai_proactive_candidate`）当真人锁定第二人称——**PL-01 一修它就会显形**，必须同批。
- PL-02 诊断误标：`wakeup_service.py:181-184` 对 `reply_sent=False` 一律打 "skipped by planner"（实际 0 次进过 planner）；trace `proactive.*` 只在 planner 阶段填充，pre-planner 终结路径恒空。
- ID-10：`sensors.py:500-504` poke target 不可解析时兜底成"戳 bot"→ 无端回戳 + 好感误结算；`_resolve_name` 会把群名片签名当显示名。

## 实施步骤

1. 放行合成事件：`sensors.should_process_message` 开头对 `astrmai_is_proactive_event` / `astrmai_interaction_kind` 非空的事件豁免组件文本检查（频控与合适性判断仍交给 judge/playbook）；或 dispatcher 构造 event_data 时补 `Plain(candidate_text)` 组件——**二选一，推荐前者**（改动面小、对真实消息零影响）。
2. **同一提交内**修 ID-06：`planner_prompt_context._build_current_speaker_block` 对 proactive 事件返回空串（归因锁随之消失）；`prompt_refiner.py:153-158` 同步核对。
3. ID-03：peer poke 放行后核对 `planner_prompt_context.py:525-529` 的互动叙事路径可达。
4. ID-10：poke target 不可解析时标记 `target_unknown`，跳过回戳与好感结算；`_resolve_name` 过滤超长/含空格的名片文本。
5. PL-02：`gate._finalize_pre_planner_turn` 补写 trace `proactive.*` 上下文（含 `blocked_reason=sensor_filtered` 等）；wakeup 日志按 blocked_reason 分流措辞。
6. 集成测试：本地构造 `ProactiveMessageIntent` 走 `dispatcher.dispatch`，断言 turn 进入 judge 阶段而非 `skipped_sensor_filter`；构造 B 戳 C 的 OneBot notice，断言进入 judge；构造无 target 的 poke，断言不回戳不结算。

## 验收标准

- 集成测试全绿（现缺 inject_external_event→sensors→judge 贯通测试，现有测试 mock 掉了 attention_gate，接缝零覆盖——本 OPT 必须补上）。
- 部署后观察：trace 出现 `proactive.is_proactive=true` 且 dispatch_status 非空的 turn；日志出现 `proactive wakeup sent via main chain`（或明确的 judge 拒绝记录）；主动消息 prompt 与回复中无"主动开口候选"字样、无无端第二人称。
- peer poke：互戳事件出现 judge 调用记录；bot 回应频率受 playbook 频控约束（不刷屏）。

## 风险与回退

- **低风险**：豁免只影响带 proactive/interaction 标记的合成事件，真实消息路径零改动；放行后 judge 仍可 IGNORE，最坏情形是多一些 judge 调用（注意 OPT-08 会同步削减 judge 成本）。
- 频控自检：wakeup 候选本身 8h/群节流（已验证健康），放行不会造成消息风暴。
- 回退：单提交 revert 即回到"全部静默"现状。

## 完成记录

**2026-07-26 代码侧完成**（线上首条主动消息验收待部署后执行）：

- 改动文件（4 个）：
  - `sensors.py::should_process_message`：`astrmai_is_proactive_event` / `astrmai_interaction_kind` 标记事件豁免组件文本检查（放在 `is_virtual_poke` 早退之后，走既有先例；PL-01+ID-03 同一插入点覆盖）。
  - `planner_prompt_context.py::_build_current_speaker_block`：proactive 事件返回空串（ID-06）；`_render_final_speaker_lock` 解析空块后自然消失，无需另改。
  - `sensors.py::process_poke_event`：目标不可解析时标记 `astrmai_interaction_target_unknown` 并整体跳过（不再伪装"戳 bot"→无端回戳+好感误结算）；`_resolve_name` 弃用超长(>20字符)/含换行的签名样名片（ID-10）。
  - `gate.py::_finalize_pre_planner_turn`：proactive 事件在 pre-planner 终结时填充 `turn_context.proactive` 快照（source/intent_id/reason/dispatch_status/blocked_reason），trace 死因可见（PL-02）；`wakeup_service.py` 撤掉武断的 "skipped by planner" 文案。
- 新增回归测试：`tests/regression/proactive/test_proactive_chain_revival.py` 7 条。**stash 红验证**：去除修复后恰好 5 个守护用例变红（传感器放行×2、归因锁×1、poke 未知目标×1、trace 快照×1），2 个负向对照（纯空消息仍被滤、真人归因锁保留）保持绿；恢复后 7/7。
- 受影响区域回归：tests/regression/proactive + tests/unit/proactive + 互动叙事 ported 共 28 passed。
- 待部署验收：trace 出现 `proactive.is_proactive=true` 且 dispatch_status 非空的 turn；日志出现 `proactive wakeup sent via main chain` 或明确的 judge 拒绝记录；主动消息 prompt/回复无"主动开口候选"字样；peer poke 进入 judge 且受 playbook 频控；synthetic 事件深链路（perception→dialogue store→judge→planner）在真实环境跑通一条完整链（代码侧已确认 perception/window 兼容 message_obj=None，深层各站以线上首条消息为准）。
