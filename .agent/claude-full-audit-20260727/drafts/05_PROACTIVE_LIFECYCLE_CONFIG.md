# 领域 05：主动行为、生命周期与配置 — 审计报告

> 审计代理：05 | 日期 2026-07-26 | 只读审计。运行时证据：`.agent/runtime-observability-c4aee57-20260726/`（16h，585 traces，1.3MB 框架日志 + 364KB 诊断日志）。配置矩阵全量表见同目录 `config_consumption_matrix.md`。

## 1. 领域概述

覆盖六块：① `_conf_schema.json`(197 叶子键) ↔ `config.py`(205 pydantic 字段) ↔ 业务消费点全量比对；② 配置热更新与回滚（webui `plugin_api.apply_config` → `PluginFacade.apply_hot_config`）；③ 越界配置容错（pydantic 校验层 + 启动失败路径 + `openai/deepseek-v4-pro` 溯源）；④ 主动行为链（wakeup/heartflow/signin/poke → `ProactiveDispatcher` → 合成事件 → attention 主链）；⑤ 后台任务去重节流（proactive 调度循环、dream、evolution backlog、memory maintenance、cron）；⑥ 生命周期（bootstrap/lifecycle/runtime_context 启动顺序、卸载任务回收）。

## 2. 数据流/调用链实测

### 2.1 主动行为链（实测）

```
ProactiveTask._loop (5/10/15s 自适应轮询, proactive_task.py L799)
  → _run_chat_heartbeat_pass → ChatLoopKernel.describe_due_selection → kernel.tick(chat)
    → dispatch bridge PROACTIVE_WAKEUP → WakeupService.run_for_chat (wakeup_service.py L160)
      build_signal: 静默>silence_threshold(120min) && energy>0.6 && 过了 next_wakeup_timestamp
      → ProactiveDispatcher.dispatch (dispatcher.py L274)
        _safety_check: quiet_hours/chat_active/wait_targets/executor_pending/noise/cooldown/energy
        → AttentionGate.inject_external_event (gate.py L999) 构造 _SyntheticExternalEvent
          → kernel.tick(trigger=external) → gate.process_event
            → L1137 _passes_sensor_filters → sensors.should_process_message
              ✗ 合成事件 message_obj=None → clean_text=="" && has_payload==False → return False (sensors.py L317)
            → status=skipped_sensor_filter, 0 LLM calls  ←←← 链路 100% 死在这里
```

运行时实证：观测期 14 次 wakeup 候选（9 个群，间隔均 ≥8h，kernel 冷却生效），trace 里 14/14 `status=skipped_sensor_filter`、`llm_call_ledger=[]`、`proactive.is_proactive=False`；两个观测窗（20bb585 与 c4aee57）`proactive wakeup sent via main chain` 均为 0 次。日志却打出 14 条 `[Life] proactive wakeup skipped by planner`（wakeup_service.py L183 对 reply_sent=False 的统一误标）。

### 2.2 配置热更路径（实测代码）

- 插件自有 WebUI：`settings_ui_service.apply_config` → `plugin_api.apply_config`（L455）先 `AstrMaiConfig(**config_data)` 校验，**失败则不触碰运行时**（返回 error，旧配置继续生效——这条路径是原子的）→ `facade.apply_hot_config`（plugin_facade.py L185，threading.RLock 串行）→ 整体换 `runtime.config` + `rebuild_infrastructure_settings` + 17 个根组件 `refresh_config` 级联（state_engine→mood/energy/relationship；planner→context_engine/prompt_refiner/cognitive_loop/executor 等；proactive_task→dispatcher/wakeup/signin/decay/diary/dream/heartflow）。失败时逐组件回滚旧配置（L253-273），回滚异常仅记 error 不中断。
- persona/sys3 结构性变化：`apply_hot_config` 直接 return False（L205-216），`_requires_reload`（plugin_api.py L420）把 provider./vision./sys3./life./persona. 前缀标记为需重启（其中 `memory.embedding_models` 前缀是死键，实际键在 provider. 下）。
- AstrBot 侧配置保存：走插件整体重载（terminate→重实例化）。在飞 turn 由 `ChatRuntimeCoordinator.shutdown`（chat_runtime_coordinator.py L401-418）cancel + gather，回复直接丢弃；`GroupDialogueStore` 纯内存（group_dialogue_store.py L53-59）→ 热区/温区/压缩链全部丢失，重载后短期上下文失忆。

### 2.3 越界配置路径（运行时实证）

`AstrMaiConfig(**{"infra":{"api_timeout":-5}})` → ValidationError（连带 timing 别名 2 个错误）；`bg_pool_size=0`、`turn_total_budget_sec=999999`、`meme_probability="abc"` 全部抛错。main.py L65 `self.config = AstrMaiConfig(**self.raw_config)` 在 `__init__` 内**无 try/except** → 插件加载直接失败。schema 只有 25 个键声明 minimum/maximum（timing 20 + private_chat.topic_* 4 + min_memory_confidence），其余约 90 个数值键 pydantic 有 ge/le 但 UI 层无约束提示。没有任何"裁剪+告警"层。

### 2.4 `openai/deepseek-v4-pro` 溯源

4 次 `star.context:403 没有找到 ID 为 openai/deepseek-v4-pro 的提供商` 全部出现在图片消息到达后 0.3-0.7s 内，且与 `attention.compaction.v2` ledger 条目同 turn 强相关。代码路径：`compaction_providers._resolve_provider_candidates`（L24-39）把 `conversation.compaction_provider_id` 配置值与 `context.get_current_chat_provider_id(chat_id)`（AstrBot 会话级 provider 偏好）拼成候选列表，逐个 `context.llm_generate(chat_provider_id=...)`；AstrBot `get_provider_by_id`（框架 context.py L315-320）对查不到的 ID 打 WARN 并返回 None。该 ID 不在插件仓库源码中——来源是服务器侧配置残留（compaction_provider_id 填了模型串，或会话 provider 偏好指向已改名的 provider）。schema 对 `compaction_provider_id` 的描述是"压缩摘要使用的**模型标识**"（_conf_schema.json L?，见矩阵），而代码要求的是 **AstrBot provider ID**——文案直接诱导用户填出这种坏值；插件对该值无启动期存在性校验，每次压缩都浪费一次失败尝试。另注：`gateway_result._provider_capabilities(model_id)`（gateway_call.py L389）也把完整 `provider/model` 串传给 `get_provider_by_id`，这是全部 GatewayUsage 日志 `provider=unknown` 的根因（能力推断永远落到 unknown 家族，还好 capability 兜底为字符串推断）。

### 2.5 生命周期（诊断日志 04:00:11-04:00:15 实测）

启动 4.3s 完成，顺序：bootstrap(全部同步构造) → initialize_memory（skeleton 同步，FAISS 惰性——04:02:38 才 "hybrid memory engine ready"，懒加载正常）→ persona core（缓存命中即刻 ready；若失败则**无限重试阻塞后续所有服务启动**，backoff 15s→300s，lifecycle.py L120-156）→ commands → expression governance → proactive → visual → background(evolution backlog/memory GC/db sync) → workmode guard。`on_program_start` 双入口（plugin_initialize + astrbot_loaded 相隔 0.85s）由 in_progress 去重正确合并为一次启动。Sys3 禁用时 CronHeartbeatGuard 不构造（bootstrap.py L272-292），cron 降级 WARN 只是子代理模块 import 时打的一次性噪音。

卸载：`terminate()` → coordinator.shutdown（cancel 在飞 turn）→ memory_pipeline.stop → private chat 持久化 → proactive.stop（cancel loop+bg tasks 并 gather）→ governance.stop → evolution.stop_background_tasks → persona.stop → cron.stop → `collect_background_tasks(5 个 owner)` cancel + 8s wait → EventBus.stop → persistence.dispose。任务回收面覆盖 lifecycle/attention_gate/evolution/governance/proactive 五个 owner 的 `_background_tasks`/`_session_tasks`；kernel 自身只发 fire-and-forget 观测任务（短命）。遗留风险见 PL-10（`_terminated` 永久闩锁）与 8s 超时后未退出任务仅告警。

## 3. 逐条发现

### PL-01（P0/P1，VERIFIED）主动开口全链路死于传感器过滤：合成事件无 message_obj，wakeup/heartflow/signin 三类主动消息从未发出过

- `gate.py L36-39`：`_SyntheticExternalEvent` 只有 `message_str`，`self.message_obj = self._data.get("message_obj")  # reserved for future use` → None。
- `sensors.py L205`：`should_process_message` 仅从 `event.message_obj.message` 的 Comp.Plain 组件提取 `clean_text_parts`，从不读 `message_str`；L317：`if not clean_text and not has_payload: return False`。
- `gate.py L1137-1144`：过滤后 `_complete_proactive_candidate(reason="sensor_filtered")` → turn 终结为 `skipped_sensor_filter`。
- 运行时：14/14 wakeup 候选 trace `skipped_sensor_filter` + 0 LLM 调用；两个观测窗 0 次成功发送；heartflow 可见候选（manager.py L866）与群签到跟发（group_signin_service.py L113）走同一 dispatcher→inject 路径，同样必死。
- 用户后果：`life.enable_proactive` 开着、能量/静默/安静时段全套机制在跑，但用户永远收不到任何主动消息；每 8h/群白白构造一次候选。历史审计 10-06/10-13（wakeup 完成回调 bug）因链路根本到不了 planner 而全部失效。known_status=NEW（历史报告均假设事件能进 planner）。
- 最小修复：`inject_external_event` 或 `should_process_message` 对 `astrmai_is_proactive_event` 豁免组件文本检查（也可给合成事件补一个 Plain 组件的 message_obj）。

### PL-02（P2，VERIFIED）主动链三层诊断全部误标，运营者无法发现 PL-01

- `wakeup_service.py L181-183`：completion(reply_sent=False) 一律打 `proactive wakeup skipped by planner`——planner 从未参与。
- `dispatcher.complete`（dispatcher.py L253）status 只有 sent/queued/skipped，不含 sensor_filtered 原因（`_complete_proactive_candidate` 写入的 blocked_reason 在 dict 分支可用，但日志与 status 不透出）。
- trace `proactive.*` 仅在 planner `_apply_proactive_context`（planner.py L1100-1114）填充；pre-planner 终结的 turn 永远 `is_proactive=False` → 585 条 trace proactive 字段"全空"的直接原因。`_finalize_pre_planner_turn` 未调用 proactive 上下文填充。
- 修复边界：gate 的 pre-planner finalize 路径补 `_apply_proactive_context` 等价逻辑 + wakeup 日志按 blocked_reason 分流。

### PL-03（P1，VERIFIED）UI"合并私聊连续输入"开关是死键：timing.turn_merge_enabled 被 pydantic 静默丢弃

- schema `_conf_schema.json L1095-1100` 暴露 `timing.turn_merge_enabled`；`config.py` TimingConfig（L306-325）无此字段，`LEGACY_TIMING_NAMESPACE_FIELDS`（L17-27）也未收录 → pydantic extra=ignore 丢弃。
- 业务读 `private_chat.turn_merge_enabled`（private_turn_coordinator.py L129-130），恒为默认 True。
- 运行时实证：`AstrMaiConfig(**{"timing":{"turn_merge_enabled":False}})` → `cfg.private_chat.turn_merge_enabled == True`。
- 后果：用户关闭合并无效；UI 显示关、行为开。修复：LEGACY_TIMING_NAMESPACE_FIELDS 增加 `("turn_merge_enabled","private_chat","turn_merge_enabled")` 或 TimingConfig 增加字段并同步。

### PL-04（P1，VERIFIED）"启用基础内容安全过滤（NSFW/自残/PII 检测）"是虚假开关，无任何实现

- schema L433-438 与 `config.py` L179 `reply.enable_content_safety_filter` 存在；全仓库（astrmai/ + main.py）无任何消费点，也不存在任何 NSFW/self-harm/PII 检测代码（仅 output_guard 做 provider 失败文本识别、context_compaction 的"safety"是压缩安全性）。
- 后果：运营者以为开启了安全过滤，实际内容不经任何检测直接外发——安全预期落空比普通死配置严重。修复：实现或从 schema 移除并注明。

### PL-05（P2，VERIFIED）另 7 个死配置键（用户改了没效果）

`attention.debounce_window`（防抖硬编码 window_buffer.py L17-24）、`attention.max_message_length`（无引用）、`attention.repeater_threshold`（gate.py L928 硬编码 >=2）、`attention.throttle_probability`/`throttle_min_entropy`（限流改为能量驱动 gate.py L913-920 + energy_manager.should_drop_by_energy）、`evolution.enable_relationship_engine`（chat_state_service.py L271 无条件实例化）、`mood.unknown_decay`（无引用）。全量矩阵及判死依据见 `config_consumption_matrix.md` ①。known_status=NEW（历史报告未做过 schema 全量比对）。

### PL-06（P2，VERIFIED）越界配置=插件整体拒载：无裁剪/告警层，且 ~90 个数值键 UI 无范围提示

- main.py L62-65 `__init__` 中 `AstrMaiConfig(**self.raw_config)` 无 try/except；pydantic ge/le 违例（负超时、0 并发、概率>1、字符串数字以外的类型错误）直接 ValidationError → AstrBot 标记插件加载失败，整个 AstrMai 下线。
- schema 仅 25 键有 minimum/maximum；`persona.component_max_retries`、`reply.meme_probability`、`attention.bg_pool_size` 等约 90 个键 pydantic 有界但 schema 无界（清单：constraint_check 脚本输出，见附录）。
- 对比：插件自有 WebUI 的 apply_config 校验失败会优雅返回 error 并保留旧配置——同一份配置两条路径行为不一致。
- 修复边界：main.py `__init__` 包一层 try/except：校验失败时逐字段回退默认并 logger.error 汇总（裁剪+告警），或至少给 schema 补齐 min/max。

### PL-07（P2，机制 VERIFIED / 值来源 LIKELY）compaction_provider_id 文案误导 + 无存在性校验 → 每次压缩浪费一次失败尝试并刷 WARN

- schema 称"压缩摘要使用的模型标识"，代码 `compaction_providers.py L24-39, L225-231` 按 **AstrBot provider ID** 使用（`llm_generate(chat_provider_id=...)`）。
- 运行时：4 次 `star.context:403 没有找到 ID 为 openai/deepseek-v4-pro` 与 compaction turn 强相关（13:30:00/17:22:29/18:25:18/20:23:51，均紧跟图片消息触发的压缩评估）；该串不在仓库源码中，为服务器配置残留。
- 关联：`gateway_call.py L389` 把完整 `provider/model` 串传 `_provider_capabilities` → `get_provider_by_id(全串)` 永远失败 → **全部 GatewayUsage 日志 provider=unknown**（主控疑点⑤的根因；能力推断降级为字符串匹配）。
- 修复：schema 文案改为"AstrBot 提供商 ID"；启动/热更时校验 provider 存在性并 WARN 一次而非每次压缩；`_provider_capabilities` 传 provider 前缀（`_provider_id()` 已有现成实现 gateway_policy.py L16-18）。

### PL-08（P2，VERIFIED）Instant memory backfill 继承已耗尽的 turn 预算：约 25% 已执行 turn 的即时记忆静默丢失

- 日志 17 次三连击：`[Gateway] model code2/... timeout (1/3): turn_deadline_exhausted` ×2 + `[InstantMemoryGate] instant llm backfill degraded: 所有模型均失败: turn_deadline_exhausted`，全部出现在 executed turn 收尾后（06:50:14/06:50:43/06:55:51/06:56:10...），执行 turn 共 69 个 → ~25% 命中。
- `instant_memory_gate.py L246-278` 经 `gateway.call_data_process_task` 走 turn 预算钳制；回复已发出后的后台记忆写不应共享前台 deadline（0ms 预算 → 两个模型瞬时"超时"，纯日志噪音+记忆漏写）。
- 修复边界：instant backfill 的 gateway 调用脱离 turn telemetry scope 或给 `reserve_for_reply=False` 的独立最小预算。与领域 04（memory）重叠，主控去重。

### PL-09（P2，VERIFIED）插件重载即上下文失忆：GroupDialogueStore/压缩链纯内存，AstrBot 侧任何配置保存都清空

- `group_dialogue_store.py L53-59` 无持久化；AstrBot 配置保存→插件重载→热区/温区/摘要链清零 + `runtime_coordinator.shutdown` cancel 在飞 turn（回复丢弃）+ `_states.clear()`。
- 用户后果：每次调参后 bot 对几分钟内的群聊上下文失忆、正在生成的回复凭空消失。缓解：文档告知或 dialogue store 快照持久化（terminate 时 `_persist_pending_sessions` 已有私聊先例）。DESIGN_IMPROVEMENT。

### PL-10（P3，LIKELY）PluginLifecycleManager._terminated 永久闩锁：同实例 terminate→initialize 复活被拒

- `lifecycle.py L54-56, L309`：terminate 置 `_terminated=True`，`on_program_start` 直接 `runtime startup rejected reason=terminated`，无解除路径。AstrBot 若在禁用→启用/热重载场景复用同一 Star 实例（不重新 `__init__`），插件将静默拒绝启动直到进程重启。需要框架行为实证（当前日志未出现该 reject 行）。

### PL-11（P3，VERIFIED）agent.max_steps 静默钳制到 ≥5，UI 允许 1-4 但无效

- `executor.py L529-531`：`max_steps = max(5, config_max_steps)`；schema/pydantic 允许 ≥1。UI 行为不一致的小项。

### PL-12（P2，VERIFIED，跨域）mood LLM 分析按消息触发而非按回复：580 次 mood 调用 vs 67 次 executed

- 16h 内 `pool=mood` 580 次（与消息量 1:1），发生在 attention ingress（gate.py L1080 `_apply_primary_mood_update`）——先于 judge 的 ignore 判决；317 个 skipped_ignore turn 也各付一次 mood 调用。成本大头（580/1022 ≈ 57% 的 LLM 调用）。属于注意力域的设计权衡，此处从后台资源争抢角度记录，主控与领域 02/03 去重。

### 后台任务去重与节流体检（无新发现，健康度记录）

| 任务 | 去重 | 节流 | 失败退避 | 结论 |
|------|------|------|---------|------|
| ProactiveTask._loop | 单任务 + `_on_loop_done` 崩溃 5s 自动重启（防复活已处理） | 5/10/15s 自适应轮询 + kernel due selection + HEARTBEAT_MAX_BATCH=32 + maintenance budget | 循环级 try/except | 健康 |
| wakeup | kernel set_cooldown(8h) 在 dispatch 时即设置（观测 14 次间隔均 ≥8h） | silence/energy/quiet_hours/noise | 拒绝无额外退避但被 kernel 冷却兜住 | 健康（但见 PL-01） |
| DreamScheduler | `_pending_completions` 全局单飞 + 会话级 backoff + 状态原子持久化(tmp+replace) | 全局 30min 间隔 + 时间窗 + min_events | 分阶段完成状态机，失败只重试未完成阶段 | 健康；观测期 0 次 dream（16h 无 dream pool 调用，疑 min_memory_events 未达标，NEEDS_RUNTIME_EVIDENCE） |
| Evolution backlog | `_mining_tasks` already_mining 跳过 + per-group `_mining_locks` | scan ≥60s（默认 900s）+ group_limit 2/轮 | `_backlog_failure_until` 冷却 1800s | 健康；本窗口 0 条 mining failed WARN（历史 WARN 大头已被 f09cf65 治理，jargon_enricher 8 次全 completed）|
| Memory maintenance | kernel 单 tick 串行 | per-session cooldown | 指数退避 300s→3600s（memory_turn_pipeline.py L333） | 健康 |
| Cron guard | Sys3 禁用时不构造；启用时 reload_all_lost_jobs + 单 heartbeat 任务 | — | — | 服务器上未启用，仅 import 期一次性降级 WARN |
| 前台争抢 | 全部后台 LLM 走 `_global_semaphore`（max_concurrent_llm_calls=3）与前台共享 | — | — | 共享池设计，后台可挤占前台并发额度；观测期未见饱和证据 |

### 生命周期附加观察（不单列 finding）

- persona core 无限重试会阻塞 proactive/visual/background/workmode 启动（attention 主链已可用），操盘者只能靠 `persona core initialization failed; retrying` WARN 感知——可接受但值得知晓。
- `_run_reflection_tasks`（proactive_task.py L510）生产代码零调用，仅测试引用——死代码（反思已迁移 ExpressionGovernanceRunner）。
- 关闭时 8s 等待后仍未退出的任务仅告警不强杀（`{n} background tasks did not exit gracefully`），观测日志未出现该行，实际回收干净。

## 4. 领域级测试缺口

1. **schema↔config 一致性守卫缺失**：无测试断言"schema 每个叶子键都能被 AstrMaiConfig 接受且字段存在"，PL-03 这类静默丢弃可以用 10 行参数化测试全量拦截（现有 tests/test_config_standalone_refactor.py 只测 timing 别名等局部）。
2. **合成事件端到端测试缺失**：tests/unit/proactive/ 覆盖了 wakeup/dispatcher 单元，但没有"inject_external_event → sensors → judge"贯通测试，PL-01 恰好死在两个模块的接缝上。
3. **越界配置加载测试缺失**：无"坏配置 → 插件应降级而非拒载"的合同测试。
4. **热更回滚路径**：`_apply_hot_config_locked` 的回滚分支无测试（组件 refresh_config 抛错时旧配置恢复的断言）。

## 5. 附录：分析脚本输出摘要

- `scratchpad/config_matrix.py` → schema 叶子 197 / pydantic 字段 205 / schema-only 4（3 个嵌套 affection_weights 正常 + turn_merge_enabled 死键）/ config-only 12（全 legacy 别名）/ 直接默认值不一致 0 / 零命中 11（人工复核后 9 死 + 2 legacy-sync 误报…实为 3 个 legacy-sync）/ getattr fallback 漂移 28 处（leaf 碰撞剔除后 11 处有效）。
- `scratchpad/constraint_check.py` → ~90 个键 pydantic 有界而 schema 无 min/max；负超时/0 池/超预算/类型错全部 ValidationError。
- `scratchpad/proactive_trace.py` → 585 唯一 trace；perception 含 proactive 的 14 条全部 skipped_sensor_filter/0 calls；wakeup 日志 14 条全 "skipped by planner"；pool 分布 mood 580 / judge 574 / memory 76 / dialog 72 / cognitive_loop 32 / profile 12 / jargon 9 / goal 7 / task 4 / vision 1 / diary 1 / dream 0。
