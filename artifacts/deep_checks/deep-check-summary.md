# AstrMai 模块深度检查总报告

检查时间：
2026-05-25

检查范围：
- 插件入口与装配层
- 对话主链路
- 注意力 / 上下文压缩链路
- 网关 / Provider / Runtime 观测
- Memory 主系统
- State / Relationship / Private Chat
- 主动行为 / Heartflow / 定时能力
- WebUI / 管理页 / 调试后端
- Presentation / 命令 / 事件接入
- 架构与边界专项

对应分报告：
- `artifacts/deep_checks/window-01-plugin-entry-and-bootstrap.md`
- `artifacts/deep_checks/window-02-conversation-mainline.md`
- `artifacts/deep_checks/window-03-attention-and-compaction.md`
- `artifacts/deep_checks/window-04-gateway-provider-runtime-observability.md`
- `artifacts/deep_checks/window-05-memory-system.md`
- `artifacts/deep_checks/window-06-state-relationship-private-chat.md`
- `artifacts/deep_checks/window-07-proactive-heartflow-cron.md`
- `artifacts/deep_checks/window-08-webui-admin-backend.md`
- `artifacts/deep_checks/window-09-presentation-and-ingress.md`
- `artifacts/deep_checks/window-10-architecture-boundaries.md`

---

## 一、总体结论

本轮未发现“整体无法运行”级别的单点致命故障，但发现了 4 类高优先级结构问题，已经跨模块影响行为正确性、观测可信度和后续重构成本：

1. 主链路存在真实语义丢失与收尾断链。
2. runtime / compat / webui 正在形成持续扩大的隐式耦合。
3. trace / cache / hash / session 观测口径不统一，已有“看起来有数据、实际上不可信”的风险。
4. memory、state、proactive 三条长期状态链仍残留明显双轨与竞态问题。

整体上，项目已经具备较完整测试骨架，但测试热点与真实高风险实现热点明显错位：不少通过的测试只能证明“字段存在”或“局部单元行为成立”，不能证明真实运行链闭环成立。

---

## 二、最高优先级发现

### P1：对话主链路有实际语义断链

1. `post_compaction_recovery_rounds` 在真实 planner 链路里写入过晚，导致“压缩后恢复轮次”无法在同轮 prompt 构造中生效。
   - 依据：`astrmai/conversation/planning/planner_prompt_context.py:370`、`astrmai/conversation/planning/prompt_refiner.py:472`、`astrmai/conversation/planning/planner.py:1195`
   - 来源：窗口 2

2. follow-up 第二条消息绕过 planner 收尾逻辑，不会进入 `dialogue_store`、`turn_trace_history` 和 continuity snapshot。
   - 依据：`astrmai/conversation/planning/planner.py:1218-1234`
   - 来源：窗口 2

3. attention 主链路把真实 `focus_reason` 覆盖成固定值 `"selected_focus_event"`，导致后续行为调优、直达判定和群聊过滤退化。
   - 依据：`astrmai/conversation/attention/focus_selector.py:47-82`、`astrmai/conversation/attention/gate.py:673-682`
   - 受影响下游：`behavior_tuning.py`、`think_level_policy.py`、`planner_side_inputs.py`
   - 来源：窗口 3

4. proactive synthetic event 会污染 `focus_message_text`、`raw_user_text`、`reply_prompt_focus_anchor`，把内部 guidance 当作“眼前消息”喂给主回复。
   - 依据：`astrmai/proactive/dispatcher.py:248-271`、`astrmai/conversation/attention/gate.py:504-516`、`astrmai/conversation/planning/planner_prompt_context.py:192-205`、`astrmai/conversation/planning/prompt_refiner.py:894-904`
   - 来源：窗口 7

### P1：入口与状态链存在真实竞态 / 覆盖问题

1. 私聊等待存在“先到消息丢失”竞态，用户在 bot arm wait 之前回复时，消息可能进 `pending_messages` 但永远不被消费。
   - 依据：`astrmai/state/private_chat/private_chat_manager.py:34`、`:47`、`astrmai/app/plugin_facade.py:257`
   - 来源：窗口 6

2. 关系四维状态会在每次读 profile 时被 `social_score` 反推覆盖，运行中累计出来的关系向量会被重建掉。
   - 依据：`astrmai/state/chat_state_service.py:133`、`astrmai/state/relationship/relationship_engine.py:244`
   - 来源：窗口 6

3. `dream` 仍只以旧 `MemoryEvent` 为种子，canonical-only 的即时记忆无法进入 dream consolidation。
   - 依据：`astrmai/memory/dream/dream_agent.py:372,404`、`astrmai/memory/services/instant_memory_gate.py:35,112`
   - 来源：窗口 5

### P1：架构边界已经出现明显反向依赖和隐藏式总管

1. `PluginFacade` 已不再是薄门面，而是继续吸收 lifecycle、诊断、WebUI 激活、Sys2/Sys3 入口与后备执行逻辑。
   - 依据：`astrmai/app/plugin_facade.py:15`、`:228`
   - 来源：窗口 1

2. `legacy_compat` 和 `export_legacy_attrs()` 仍是生产主链的一部分，而不是临时桥。
   - 依据：`astrmai/infrastructure/compat/legacy_compat.py:15-168`、`astrmai/app/runtime_context.py:383`
   - 来源：窗口 1、窗口 10

3. `infrastructure` 层存在反向 import 上层对话/渲染对象。
   - 依据：`astrmai/infrastructure/runtime/lane_history.py:10`、`lane_transcript.py:8`、`runtime_contracts.py:13-30`、`gateway_result.py:8-9`
   - 来源：窗口 10

---

## 三、高风险但偏结构性的发现

### 1. trace / cache / hash 口径不统一

1. `prefix_hash`、`semantic_system_hash`、`stable_prefix_hash`、`effective_prefix_hash` 在同一请求链中混用，gateway trace、lane/economy trace、turn trace 不能直接对齐。
   - 依据：`astrmai/conversation/planning/context_engine.py:205`、`astrmai/infrastructure/context_economy/center.py:438`、`astrmai/infrastructure/gateway/gateway_lane.py:439,491`
   - 来源：窗口 4

2. `cache_ready` / `cache_ready_reasons` 观测依赖的请求级字段没有完整回流，现有 cache 观测天然失真。
   - 依据：`astrmai/infrastructure/gateway/gateway_result.py:53`、`astrmai/infrastructure/gateway/gateway_call.py:211`、`astrmai/infrastructure/gateway/gateway_lane.py:436`
   - 来源：窗口 4

3. `cache_affinity_enabled` / `cached_usage_supported` 主要靠测试或脚本伪造，真实主链没有稳定 writer。
   - 依据：`astrmai/conversation/planning/planner.py:328`
   - 来源：窗口 4

### 2. Planner / TurnContext / WebUI 服务对象持续膨胀

1. `Planner` 同时处理 planning、budget、tool 装配、prompt 组装、执行、trace、continuity、follow-up，已经明显过胖。
   - 依据：`astrmai/conversation/planning/planner.py:886`
   - 来源：窗口 2

2. `TurnContext` 已从合同对象演变成“trace + runtime bus”，多个行为层反向读取它作为决策输入。
   - 依据：`astrmai/conversation/planning/prompt_refiner.py:391`、`astrmai/memory/services/memory_injection_service.py:45`
   - 来源：窗口 2

3. `AdminUiService`、`MemoryUiService`、`PluginRuntimeContext` 等文件已经接近 God Object。
   - 依据：`astrmai/webui/backend/services/admin_ui_service.py:37-80,757,1199-1213`、`astrmai/webui/backend/services/memory_ui_service.py:201-350,801-867`、`astrmai/app/runtime_context.py:12-380,386-436`
   - 来源：窗口 8、窗口 10

### 3. runtime / webui / hot config 行为存在“部分新、部分旧”

1. WebUI 热配置只替换 `runtime.config/raw_config`，但大量核心服务在 bootstrap 时已经固化持有旧 `config` 引用。
   - 依据：`astrmai/webui/backend/adapters/plugin_api.py:171`、`astrmai/infrastructure/gateway/model_gateway.py:28`、`astrmai/infrastructure/runtime/lane_manager.py:55`、`astrmai/conversation/attention/gate.py:59`
   - 来源：窗口 1

2. FastAPI `/api` 对 runtime 绑定依赖全局 `ACTIVE_FACADE`，独立启动或初始化顺序变化时会静默降级。
   - 依据：`astrmai/webui/backend/server.py:20`、`astrmai/app/plugin_facade.py:20-24`、`astrmai/webui/plugin_pages.py:568`
   - 来源：窗口 8

### 4. ingress 入口规则未完全统一

1. `poke` 分支绕过了统一权限/入口合同，直接进入 attention。
   - 依据：`astrmai/presentation/events/message_entry.py:25,77`、`astrmai/conversation/ingress/poke_handler.py:14`、`astrmai/conversation/ingress/sensors.py:308,473`
   - 来源：窗口 9

2. external result sniff 只靠 `astrmai_is_self_reply` 排除自身回复，且不复用统一 permission/scope 校验。
   - 依据：`main.py:115`、`astrmai/conversation/ingress/external_result_bridge.py:31`
   - 来源：窗口 9

3. media-only dedupe 只用长度指纹，存在误杀不同附件消息的风险。
   - 依据：`astrmai/conversation/ingress/dedupe.py:12`
   - 来源：窗口 9

---

## 四、测试与验证缺口

### 1. 通过了很多测试，但关键闭环没有被证明

1. 没有一条端到端测试证明 `gateway -> on_llm_request hook -> planner turn trace` 的 hash / trace 口径一致。
   - 来源：窗口 4

2. 没有测试证明 follow-up 第二条消息也会进入完整 trace / continuity / dialogue store。
   - 来源：窗口 2

3. 没有测试证明 post-compaction recovery 在真实 planner 链路中同轮生效。
   - 来源：窗口 2

4. 没有测试覆盖 `focus_reason` 透传到 `behavior_tuning / think_level_policy / planner_side_inputs` 的闭环。
   - 来源：窗口 3

### 2. 高风险路径与现有测试热点明显错位

1. WebUI 热点改动在 `admin_ui_service.py`、`memory_ui_service.py`、`dashboard.js`、`plugin_pages.py`，但 `tests/unit/webui/*` 基本只覆盖 `UserUiService`。
   - 来源：窗口 8

2. proactive / cron guard 中存在真实测试入口不稳定问题，`tests/test_cron_guard_refactor.py` 当前连 import 都不稳定。
   - 来源：窗口 7

3. state 测试没有覆盖关系向量重建、私聊先到消息竞态、mood 提交基线混用。
   - 来源：窗口 6

4. memory 测试没有覆盖 canonical-only memory 到 dream、以及重启后仅靠 canonical store 回读 recent / cognitive feedback。
   - 来源：窗口 5

5. ingress / presentation 缺少负例集成测试：未授权 poke、非白名单 external result、自身 `/mai` 输出不应被 sniff、纯媒体消息 dedupe。
   - 来源：窗口 9

---

## 五、共性问题归纳

本轮 10 个窗口的发现可以归纳成 5 个共性模式：

1. 兼容桥长期化。
   - 典型：`legacy_compat.py`、`export_legacy_attrs()`、WebUI 直接拿 runtime

2. 局部重构成功，但主链闭环未完全收口。
   - 典型：follow-up 第二条消息、post-compaction recovery、reverse session hash 回填

3. 观测字段多而不统一，容易形成伪可观测性。
   - 典型：prefix/hash/cache/session trace 多口径并存

4. 多个子系统仍保留新旧双轨。
   - 典型：memory v2/canonical vs `MemoryEvent` / `documents`，runtime 新配置 vs 旧实例持有配置

5. 测试存在，但很多只守住局部单元，不守住真实运行链。
   - 典型：WebUI、proactive、ingress、state 竞态、gateway trace

---

## 六、建议修复顺序

### 第一批：先修行为正确性

1. 修 planner 主链断链。
   - `post_compaction_recovery_rounds` 同轮生效
   - follow-up 第二条消息补齐 trace / continuity / dialogue_store
   - `executed` 状态分类修正

2. 修 attention / proactive 的焦点污染问题。
   - `focus_reason` 透传
   - synthetic proactive guidance 不再进入 `focus_message_text/raw_user_text/reply_prompt_focus_anchor`

3. 修 private chat 等待竞态与关系向量重建问题。

### 第二批：再修观测可信度

1. 统一 hash / cache / trace 字段口径。
2. 让 lane/session/model rotate 规则真正进入主链。
3. 收回 reverse session 的 out-of-band 回填路径。

### 第三批：最后做边界收缩

1. 收缩 `legacy_compat.py`、`LEGACY_RUNTIME_ATTRS`、`PluginFacade`
2. 禁止 `infrastructure -> conversation.planning/presentation/webui` 反向 import
3. 给 WebUI 建立 facade / DTO 边界，去掉 service 对 runtime 私有状态的直接访问

---

## 七、建议优先补的测试

1. planner 端到端：
   - post-compaction recovery 同轮生效
   - follow-up 第二条消息进入 trace / continuity / dialogue store

2. gateway / trace 端到端：
   - `gateway -> on_llm_request -> planner` 的 hash 一致性
   - lane rotate / session 换代

3. ingress / presentation 负例集成：
   - 未授权 poke
   - 非白名单 external result
   - `/mai` 输出不应被 sniff
   - 纯媒体消息 dedupe

4. state / memory / proactive 竞态：
   - private chat 先到消息
   - relationship vector 持久化
   - canonical-only memory -> dream
   - wakeup / heartflow cooldown 只有在实际可见发送后才生效

---

## 八、残余说明

1. 本轮是只读检查，未修改业务代码。
2. 大部分窗口都完成了最小相关测试验证。
3. `cron_guard` 存在一个明确的测试桩导入问题，已在分报告中单独记录，不属于业务逻辑已被证伪，但意味着该验证入口当前不可靠。

