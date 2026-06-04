# AstrMai 10 子智能体总开发检查汇总报告

生成时间：
`2026-05-28`

基线提交：
`f62904f`

对应分报告：
- `agent-01-plugin-entry-and-bootstrap/report.md`
- `agent-02-conversation-mainline/report.md`
- `agent-03-attention-and-compaction/report.md`
- `agent-04-gateway-provider-runtime/report.md`
- `agent-05-memory-system/report.md`
- `agent-06-state-relationship-private-chat/report.md`
- `agent-07-proactive-heartflow-cron/report.md`
- `agent-08-webui-admin-backend/report.md`
- `agent-09-presentation-and-ingress/report.md`
- `agent-10-architecture-boundaries/report.md`

---

## 一、总体结论

本轮 10 个子智能体已经全部完成检查。当前项目没有暴露出“启动即崩”的单点致命问题，但存在 5 类已经会影响真实运行行为或后续维护成本的结构性风险：

1. 主对话链、注意力链和主动行为链之间，仍有多个“真实行为语义”错位点。
2. runtime / compat / WebUI 的边界继续扩张，热配置、管理页和运行时对象之间存在明显漂移风险。
3. trace / hash / cache / session 观测口径不统一，已经影响排障可信度。
4. memory / state / proactive 这三条长期状态链仍保留双轨、竞态和持久化不一致问题。
5. 测试数量不少，但很多只守住局部 happy path，没有守住真实运行闭环。

整体判断：
- 现在最值得优先修的是“真实行为错误”而不是“代码风格不理想”。
- 第二优先级是“观测字段口径不统一”，因为它已经会误导后续排障。
- 最后才是“compat 收口、架构边界测试补强、God Object 收缩”。

---

## 二、P1 级问题

### 1. 主动行为主链有真实退化

1. `PROACTIVE_WAKEUP` 的 kernel-mediated 真路径当前基本失效。
   - 依据：`astrmai/proactive/proactive_task.py:472` 与 `astrmai/proactive/wakeup_service.py:116`
   - 结果：调度层已经决定唤醒，但子服务会把空 signal 当作 `ineligible` 返回，形成静默退化。
   - 来源：窗口 7

2. proactive synthetic event 会污染 runtime activity 时间锚与活跃计数。
   - 依据：`astrmai/proactive/dispatcher.py:255`、`astrmai/conversation/attention/gate.py:257,393`、`astrmai/infrastructure/runtime/chat_runtime_coordinator.py:70`
   - 结果：主动唤醒会被算成“用户刚刚活跃”，反向影响 wakeup、heartflow、freshness 判定。
   - 来源：窗口 7

3. Heartflow visible cooldown 在真实路径里会被写两次。
   - 依据：`astrmai/proactive/heartflow/manager.py:805`、`astrmai/proactive/proactive_task.py:522`
   - 结果：重复副作用和冷却时间冲突风险。
   - 来源：窗口 7

### 2. 注意力与上下文拼装会把主线焦点拉偏

1. focus 选择会卡在更早的 direct turn，后续自然追问抢不回主线。
   - 依据：`astrmai/conversation/attention/focus_selector.py:14-43`、`astrmai/conversation/attention/thread_builder.py:19,62`
   - 结果：bot 已经回答后，用户自然 follow-up 仍可能围绕旧 direct turn 打转。
   - 来源：窗口 3

2. warm/recent 交接不稳定，最新追问可能被 warm 丢掉，而 recent 又不补位。
   - 依据：`astrmai/conversation/attention/group_dialogue_store.py:232-245,417-427,499-560`、`astrmai/conversation/planning/planner_prompt_context.py:288-321`
   - 结果：prompt 锚点回退到旧 direct turn，`social/recent transcript` 语义不稳。
   - 来源：窗口 3

### 3. memory / state 里有真实持久一致性问题

1. `ChatState` 的衰减与重置不是持久一致的。
   - 依据：`astrmai/state/chat_state_service.py:79`、`astrmai/state/mood/mood_decay.py:7`、`astrmai/infrastructure/persistence/state_profile_persistence.py:14`
   - 结果：重启后、后台衰减后，energy/mood 与时间锚会出现错误恢复。
   - 来源：窗口 6

2. Dream 维护仍是 legacy-first 双轨，不是 canonical/v2 主轨。
   - 依据：`astrmai/memory/dream/dream_agent.py:222,473`
   - 结果：只要旧 `MemoryEvent` 还在，dream 就不会稳定建立在 v2 主存上。
   - 来源：窗口 5

3. Memory injection 仍可能把“指令型记忆文本”直接抬升进最终提示词。
   - 依据：`astrmai/memory/services/memory_write_service.py:15`、`astrmai/memory/services/memory_context_builder.py:30`、`astrmai/conversation/planning/prompt_refiner.py:946`
   - 结果：用户历史原话如果带指令性措辞，可能被当成高权重上下文再次注入。
   - 来源：窗口 5

---

## 三、P2 级问题

### 1. 对话主链和 contracts 仍有收尾与分层问题

1. executor 的 native vision tool-mode 失败回退仍混着两条异常通道，存在重复执行有副作用工具的风险。
   - 来源：窗口 2

2. planner 的“无回复”收尾链路已经分叉，同样都是“不发回复”，后续 continuity / agency / turn_count 更新口径不一致。
   - 来源：窗口 2

3. `turn_context` 仍在承担运行时控制语义，不只是 contracts 层的观测快照。
   - 来源：窗口 2

4. `ContextEngine` 还在反向写 `planner_runtime_instruction_block`，导致系统层规则继续混进动态侧输入预算。
   - 来源：窗口 2

### 2. Gateway / runtime 观测已经发生口径错位

1. `prefix_hash` 在 turn trace、gateway trace、context economy trace 中代表不同语义。
   - 依据：`context_engine.py:207`、`planner.py:236`、`gateway_lane.py:86`、`center.py:228`、`models.py:87`
   - 来源：窗口 4

2. 主回复 `chat` lane 没有和 `tool` lane 同粒度的 gateway stage trace。
   - 依据：`gateway_call.py:113`、`gateway_lane.py:237,538,662`
   - 来源：窗口 4

3. reverse-session hack 仍然侵入主链，而且 `provider_visible_system_hash` / `post_hook_system_hash` 绑定在 `main.py` 全局 hook 上。
   - 来源：窗口 4

4. lane/session/cache affinity 是真实落地的，但当前观测命名高估了“可缓存性”。
   - 来源：窗口 4

### 3. State / relationship / private wait 仍有竞态与双真相

1. 私聊 wait 会在“同尾号”群聊 heartbeat 上串线。
   - 依据：`astrmai/state/private_chat/private_chat_manager.py:95,138`、`chat_loop_kernel.py:1292`
   - 来源：窗口 6

2. 关系衰减存在“双真相”：`social_score`、运行时向量分数、`relationship_vector["social_score"]` 可能同时不一致。
   - 依据：`chat_state_service.py:134`、`relationship_engine.py:413`、`decay_service.py:21`
   - 来源：窗口 6

### 4. WebUI / ingress / compat 仍直接碰内部细节

1. 独立 `persona` 页已经和当前后端契约脱节。
   - 依据：`astrmai/webui/frontend/js/pages/persona.js:18`、`astrmai/webui/backend/services/persona_ui_service.py:59`
   - 来源：窗口 8

2. `AdminUiService` 仍直接穿透 runtime 私有缓存，而且 `clear_heartflow_cooldowns` 与 `clear_chat_runtime` 语义不一致。
   - 来源：窗口 8

3. `result_sniffer` 与 `error_interceptor` 边界有穿透风险，ghost sentinel / error text 可能先被当成外部插件结果注入 attention。
   - 来源：窗口 9

4. `poke` 入口并不统一，notice-only poke 可能根本进不到 raw payload fallback。
   - 来源：窗口 9

5. 命令守卫存在过宽风险，运行时可能退化为“任何 `/...` 都当 framework command 拦掉”。
   - 来源：窗口 9

### 5. 入口层 / WebUI / compat 继续扩大技术债

1. 热配置存在“配置态 / 运行态漂移”风险。
   - 依据：`PluginApiAdapter.apply_config()` 与 `PluginRuntimeContext._refresh_live_config_refs()`
   - 来源：窗口 1

2. `terminate()` 清理对状态位不对称，停机后 runtime diagnostics 仍可能残留“已启动”位。
   - 来源：窗口 1

3. `legacy_compat.py`、`LEGACY_RUNTIME_ATTRS`、`ACTIVE_FACADE`、`PluginApiAdapter.get_runtime()` 都已长期化。
   - 来源：窗口 1、窗口 10

---

## 四、P3 与结构性风险

1. `webui/backend/services` 仍直接碰 persistence 和 runtime 内部细节，现有架构测试只约束 namespace，不约束 SQL / runtime 钻取。
2. 存在明确的 `conversation -> presentation` 反向依赖，但当前架构测试没有覆盖。
3. God Object 持续膨胀：
   - `AdminUiService`：1132 行 / 76 方法
   - `MemoryUiService`：843 行 / 49 方法
   - `MemoryEngine`：861 行 / 45 方法
   - `PluginRuntimeContext`：447 行 / 53 方法
4. `prompt_templates` 注册表偏大，但当前未发现明确“注册后无人调用”的脱节证据。

---

## 五、测试缺口

### 1. 最关键的真实闭环未被覆盖

1. proactive 真正的 wakeup bridge 路径没有被稳定回归覆盖。
2. `old direct -> bot answer -> natural followup` 这条注意力主线竞争路径没有测试。
3. `get_trace_status()` 的 `state` 与 `eligibility` 一致性没有测试。
4. memory injection 的“指令型记忆文本不应污染最终回复”没有端到端回归。
5. `result_sniffer` 与 `error_interceptor` 联动顺序没有集成验证。

### 2. 测试本身也有基线漂移

1. `tests/test_heartflow_refactor.py` 有 6 处仍按旧签名调用 `_build_impulse_decision(...)`。
2. `test_compaction_provider_kwargs_use_dedicated_lane_and_reuse_session` 对 `session_id` 结尾的断言已经漂移。
3. `tests/test_cron_guard_refactor.py` 只覆盖 happy path，没有覆盖循环与 stop/cleanup。

---

## 六、建议修复顺序

### 第一批：先修真实行为错误

1. 窗口 7：主动行为 / Heartflow / cron
   - 先修 wakeup bridge、runtime-activity 污染、double cooldown

2. 窗口 3：注意力 / 上下文压缩
   - 再修 direct turn 抢占主线、warm/recent 交接、compaction state 混义

3. 窗口 5：Memory
   - 处理 dream legacy-first 与 memory injection 提示词污染

4. 窗口 6：State
   - 处理 ChatState 持久一致性、private wait 串线、relationship 双真相

### 第二批：修观测可信度

5. 窗口 4：Gateway / runtime
   - 统一 `prefix_hash` / stage trace / reverse hook 口径

6. 窗口 2：Conversation 主链
   - 修收尾链路分叉、executor fallback 混道、turn_context/runtime 边界

### 第三批：最后做边界收口

7. 窗口 8：WebUI
   - 先修 persona 页契约漂移和 clear runtime 语义不一致

8. 窗口 9：Presentation / ingress
   - 收紧 sniff / error / poke / command guard

9. 窗口 1：插件入口与装配层
   - 收口热配置、terminate 状态位、插件页重复注册

10. 窗口 10：架构边界
   - 最后补架构回归测试，推动 compat 收缩

---

## 七、总建议

如果你后面按窗口顺序发修复任务，最重要的执行原则只有 4 条：

1. 每个窗口必须先读本总报告和自己的 `report.md`，再回到真实代码核对。
2. 报告只是线索，不能机械照抄，若前面窗口已经修掉，必须明确说明“报告已漂移”。
3. 每个窗口只做最小闭环修复，并跑最小相关验证。
4. 涉及共享核心文件时，后发窗口必须先读当前实现再动手，避免按旧报告误修。

