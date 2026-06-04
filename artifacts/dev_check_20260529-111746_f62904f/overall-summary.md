# 修复后复检总报告

生成时间：
`2026-05-29`

基线提交：
`f62904f`

来源文件：
- `agent-01-plugin-entry-and-bootstrap.md`
- `agent-02-conversation-mainline.md`
- `agent-03-attention-and-compaction.md`
- `agent-04-gateway-provider-runtime.md`
- `agent-05-memory-system.md`
- `agent-06-state-relationship-private-chat.md`
- `agent-07-proactive-heartflow-cron.md`
- `agent-08-webui-admin-backend.md`
- `agent-09-presentation-and-ingress.md`
- `agent-10-architecture-boundaries.md`

---

## 一、总体结论

这轮复检说明：你前一轮修复已经消掉了一批旧问题，但系统还保留了几类“真正会影响运行结果”的残余缺陷。

当前最突出的不是单一模块坏掉，而是 5 条横向风险链还没有收口：

1. 对话主链和注意力链还有“主线焦点错位 / 收尾断裂”问题。
2. 主动行为链和状态链还有“竞态 / 回写口径不一致”问题。
3. memory 主系统仍保留明显的 legacy/canonical 双轨债务。
4. gateway/runtime 观测字段仍混用，session/cache 语义还没完全对齐。
5. WebUI、presentation、compat 仍直接穿透 runtime / persistence 内部细节。

整体判断：
- 现在最优先的是修“真实行为错误”。
- 第二优先的是修“跨层观测口径不一致”。
- 最后再收“架构边界与 compat 技术债”。

---

## 二、本轮未复现的问题

这轮有几类旧问题没有再次坐实，说明前序修复已经产生效果：

1. 入口层 `main.py` 职责继续膨胀，本轮未复现。
2. `LEGACY_RUNTIME_ATTRS` 继续扩面，本轮未复现新的扩张点，但桥仍然存在。
3. 插件页“旧 facade 悬挂绑定”本轮未复现，现有幂等测试仍成立。
4. 显式的 `infrastructure -> presentation/webui`、`conversation -> presentation` 反向 import 违规，本轮未复现。
5. `presentation` 直接 import persistence internals，本轮未复现。
6. “heartflow 只是状态堆积、不真正影响行为”，本轮未复现。
7. “主动 synthetic 文本重新写回主链 anchor”的旧问题，在本轮 `proactive` 检查范围内未发现新增证据。

这些都不代表架构已经健康，只代表“旧问题没有再次被当前复检坐实”。

---

## 三、P1 级残留问题

### 1. 对话主链仍有真实行为断裂

1. Tool 模式异常通道仍未统一。
   - 文件：`astrmai/conversation/execution/executor.py`
   - 现象：tool 模式首模抛普通异常时，不会像 text 路径那样切第二模型，而是直接进入 fatal fallback。
   - 风险：有副作用工具可能在错误路径下被过早终止，且模型 fallback 策略不一致。

2. `skipped_wait/ignore` 这类无回复分支仍未走共享收尾。
   - 文件：`astrmai/conversation/planning/planner.py`
   - 现象：跳过分支的 turn trace 缺少 `compaction_status`、`recent_transcript_used`、`reply_prompt_focus_anchor`。
   - 风险：无回复 turn 的 trace/runtime 信息不完整，后续调试和策略依赖会失真。

### 2. 注意力主线仍可能被错误抢走

1. `near_context_followup` 触发词过宽。
   - 文件：`astrmai/conversation/attention/gate.py`、`astrmai/conversation/attention/focus_selector.py`
   - 现象：无关群聊里的“这个 / 那个 / 不可以”这类泛指代句也会拿到很高焦点评分。
   - 风险：群聊主线会被无关 follow-up 抢走，回复对象偏移。

2. warm 层仍会“捞回”过旧 assistant 回合。
   - 文件：`astrmai/conversation/attention/group_dialogue_store.py`
   - 现象：assistant 已经掉出最近尾部后，warm summary/quotes 仍可能把它描述成“最近刚回应过”。
   - 风险：`social/recent transcript` 语义漂移，planner 误判上下文新鲜度。

### 3. state 持久一致性仍有明确缺陷

1. mood 更新仍存在同一 `chat_id` 下的写后读竞争。
   - 文件：`astrmai/state/chat_state_service.py`
   - 风险：最终 mood 取决于分析返回顺序，而不是消息顺序。

2. `social_score` 与 `relationship_vector` 仍是分叉真值。
   - 文件：`astrmai/infrastructure/persistence/state_profile_persistence.py`、`astrmai/state/relationship/relationship_engine.py`、`astrmai/state/chat_state_service.py`
   - 风险：外部只改 `social_score` 会在下次读取时被旧向量回滚。

3. `user profile` 缓存与 prompt 消费仍不一致。
   - 文件：`astrmai/state/user_profile_service.py`、`astrmai/conversation/planning/context_engine.py`
   - 风险：持久化层已经更新，私聊 prompt 仍继续吃旧缓存。

### 4. proactive / dream / cron 仍有真实竞态

1. proactive dispatcher 存在“成功发送后又被旧 decision 回写覆盖”的状态错误。
   - 文件：`astrmai/proactive/dispatcher.py`、`astrmai/proactive/wakeup_service.py`
   - 风险：主动消息明明已成功发出，但 trace/history 仍显示 `queued` / `reply_sent=False`。

2. `dream_scheduler` 的全局冷却仍有并发窗口。
   - 文件：`astrmai/proactive/dream_scheduler.py`
   - 风险：并发 session 可绕过 `throttle_scope="global"`，导致重复 dream 写回。

3. cron guard 的 `job_id` 归一化仍不一致。
   - 文件：`astrmai/workmode/cron_guard/heartbeat.py`
   - 风险：`1` 和 `"1"` 会被误判成不同 job，导致重复 revive。

### 5. memory 主系统仍有主轨边界错误

1. Dream 通用检索仍会串到 `feedback/tool_only` 层。
   - 文件：`astrmai/memory/dream/dream_agent.py`、`astrmai/memory/services/memory_engine.py`、`astrmai/memory/services/v2_store.py`
   - 风险：内部反馈提示会被当成长期记忆素材参与 dream 推理。

2. 长期记忆主链仍在双写 legacy `MemoryEvent`。
   - 文件：`astrmai/memory/services/session_memory_summarizer.py`、`astrmai/memory/dream/dream_agent.py`
   - 风险：canonical 更新后 legacy 不同步，Dream fallback 仍可能读到旧版本。

---

## 四、P2 级残留问题

### 1. contracts / prompt layering 仍未彻底收口

1. `TurnContext` 仍承担大量运行时语义。
   - 文件：`astrmai/conversation/contracts/turn_context.py`
   - 风险：planner / refiner / loader 仍通过共享可变运行时包耦合。

2. `ContextEngine` 仍反向写 `planner_runtime_instruction_block`。
   - 文件：`astrmai/conversation/planning/context_engine.py`
   - 风险：系统层规则继续混入动态侧输入预算。

### 2. compaction / gateway 观测口径仍不统一

1. `prefix_hash` 在 turn trace 与 gateway/lane/context-economy trace 中仍是不同语义。
   - 文件：`context_engine.py`、`planner.py`、`gateway_lane.py`、`center.py`

2. `cache_affinity_enabled` 仍把 “enabled” 和 “ready” 混在一起。
   - 文件：`gateway_lane.py`、`planner.py`

3. 远端 provider session 仍不会跟随 `prefix_hash` 触发的 lane 旋转一起切断。
   - 文件：`lane_storage.py`、`lane_manager.py`

4. reverse-session hack 仍没有退回 gateway/provider 边界内。
   - 文件：`main.py`、`executor.py`、`reverse_session.py`

### 3. memory trace 仍不够可解释

1. auto injection 的 `selected_ids` 仍按“预选中”而不是“实际注入”记账。
   - 文件：`memory_injection_service.py`、`memory_context_builder.py`、`memory_tool_service.py`

2. light/jargon 路径的 retrieval trace 仍缺 `retrieved_count/search_steps`。
   - 文件：`memory_retrieval_service.py`、`memory_injection_service.py`、`react_retriever.py`

### 4. WebUI / ingress / compat 仍直接碰内部实现

1. 管理后端仍能直接热改核心 runtime。
   - 文件：`settings_ui_service.py`、`plugin_api.py`

2. WebUI 调试/观测服务仍大量依赖 runtime 私有字段。
   - 文件：`admin_ui_service.py`、`memory_ui_service.py`、`review_ui_service.py`、`persona_ui_service.py`

3. `routes/service/schema` 在 learning/proactive/chat-runtime 线上仍混层。
   - 文件：`schemas.py`、`proactive_routes.py`、`learning_routes.py`、`chats_routes.py`

4. 命令入口仍绕过统一权限闸门。
   - 文件：`main.py`、`permission_guard.py`、`plugin_facade.py`

5. dedupe 仍在 poke 归一化前执行，空事件仍统一塌缩成 `obj_empty`。
   - 文件：`message_entry.py`、`dedupe.py`、`poke_handler.py`

6. 外部插件结果嗅探仍有 fallback 分叉。
   - 文件：`external_result_bridge.py`、`command_guard.py`、`plugin_facade.py`

### 5. 架构边界仍未真正收口

1. `presentation` 仍直接操纵 concrete runtime。
2. WebUI 仍直接碰 persistence 与 runtime internals。
3. `legacy_compat.py`、`LEGACY_RUNTIME_ATTRS`、`ACTIVE_FACADE` 仍是被测试与代码共同固化的长期公共面。
4. `AdminUiService`、`MemoryUiService`、`PluginRuntimeContext` 仍在继续膨胀。

---

## 五、测试缺口

### 1. 直接缺失的关键回归

1. tool 模式首模抛普通异常时是否仍能切第二模型
2. `skipped_wait/ignore` 分支的 trace/runtime 完整性
3. `near_context_followup` 假阳性场景
4. warm 层“捞回过旧 assistant”场景
5. mood 并发更新竞争
6. `social_score` 外部修改后不应被旧向量回滚
7. profile cache 与 prompt bundle 一致性
8. dream 检索不应带入 `feedback/tool_only`
9. injection 截断后 `selected_ids` 只统计真实渲染项
10. proactive dispatcher callback 回写链
11. dream scheduler 并发全局冷却
12. cron guard 的 `job_id` 类型归一化
13. 未授权 `/mai` / `/work` 命令路径
14. dedupe + poke 联动路径
15. external result sniff fallback 分叉

### 2. 测试基础设施缺口

1. WebUI 相关测试运行仍依赖手工设置 `PYTHONPATH`
2. 架构回归套件直接 `pytest tests/regression/architecture -q` 仍可能因为导入路径不稳而失败
3. 多处测试仍存在 `after_nonebot_init was never awaited` warning，说明异步初始化/清理链路的测试洁净度还不完整

---

## 六、建议修复顺序

### 第一批：先修真实运行行为错误

1. 窗口 9：命令权限绕过、dedupe/poke 顺序、external bridge fallback
2. 窗口 7：dispatcher 回写错误、dream 并发冷却、cron guard job_id 归一化
3. 窗口 3：near-context 假阳性、warm/recent 语义漂移
4. 窗口 6：mood 并发覆盖、social_score / relationship_vector 双真相、profile cache 陈旧
5. 窗口 5：dream 串入 feedback/tool_only、canonical/legacy 双写

### 第二批：修观测与 contracts

6. 窗口 4：session affinity 与 `prefix_hash/cache_affinity` 口径统一
7. 窗口 2：tool 异常通道、skipped 分支收尾、TurnContext/runtime 包收口

### 第三批：最后收架构边界

8. 窗口 8：settings 热改 runtime、WebUI 私有字段依赖、schema 契约
9. 窗口 1：life.enable_proactive 热配置漂移、terminate 异常安全、APPLY_STATUS 全局污染
10. 窗口 10：把当前“白名单容忍”改成真正的边界回归约束

---

## 七、总判断

这轮复检说明：系统已经比上轮更稳定，但还没有进入“只剩技术债清理”的阶段。当前仍有多条会直接影响真实行为的 `P1` 问题，尤其集中在：

- 命令入口权限
- proactive 状态回写
- attention 主线判定
- state 并发与持久一致性
- memory 主轨边界

这些问题修完后，再收 gateway 观测口径和 WebUI / compat 边界，会更稳。  

