# 08 去重后的统一优化 Backlog

> 原始发现 74 条 → 跨域合并 6 条 → **68 条独立修复单元**。
> 严重度: P0×3 / P1×16 / P2×36 / P3×13；真实性: VERIFIED×62 / LIKELY×5 / NEEDS_RUNTIME_EVIDENCE×1。
> `✔主控` = 主控已逐行回读源码复核。分类: BUG=源码确定缺陷, RUNTIME=线上数据暴露问题, DESIGN=设计优化建议。

## 合并记录（6 条重复根因）

| 被合并 ID | 并入 | 合并理由 |
|---|---|---|
| RT-12 | **ML-02** | 同一深检索无预算约束（两层修复合并） |
| ID-02 | **PL-01** | 同一传感器绞杀合成事件根因（PL-01 证据面更全） |
| ML-01 | **RT-01** | 同一 contextvar turn-deadline 泄漏根因（event_bus 懒启动侧证据） |
| PL-07 | **RT-07** | compaction 误配并入 RT-07；provider=unknown 姊妹面并入 RT-08 |
| PL-08 | **RT-01** | 同一根因的 instant backfill 受害面（25% executed turn 记忆丢失） |
| PL-12 | **RT-03** | 同一 mood 全量触发问题（57% 调用占比佐证并入） |

## P0（3 条）

| ID | 标题 | 真实性 | 分类 | 新旧 | 证据 | 轮次 | 复核 |
|---|---|---|---|---|---|---|---|
| **ID-01** | 群聊在途回复被任意无关新消息击杀：freshness 线程隔离因 latest_activity_thread_signature 恒空而整体失效 | VERIFIED | BUG | NEW | `astrmai/conversation/attention/gate.py:L527-L536, L1205`; `astrmai/infrastructure/runtime/chat_runtime_coordinator.py:L447-L477`; `astrmai/conversation/execution/executor.py:L444-L449, L759-L762` | R1 | ✔主控 |
| **PL-01** | 主动开口全链路死于传感器过滤：合成事件 message_obj=None，wakeup/heartflow/signin 主动消息从未发出过 | VERIFIED | BUG | NEW | `astrmai/conversation/attention/gate.py:L36-L39`; `astrmai/conversation/ingress/sensors.py:L205,L317-L318`; `astrmai/conversation/attention/gate.py:L1137-L1144` | R1 | ✔主控 |
| **RT-01** | 跨 task 继承的 contextvar 遥测导致预算 clamp 用错轮：per-chat memory worker 里 instant backfill 100% 失败 turn_deadline_exhausted | VERIFIED | BUG | NEW | `astrmai/infrastructure/gateway/gateway_call.py:L283-L289`; `astrmai/memory/services/memory_turn_pipeline.py:L170-L177`; `astrmai/infrastructure/runtime/turn_call_ledger.py:L215-L232` | R1 | ✔主控 |

## P1（16 条）

| ID | 标题 | 真实性 | 分类 | 新旧 | 证据 | 轮次 | 复核 |
|---|---|---|---|---|---|---|---|
| **TG-02** | Provider 失败矩阵缺 not-found 轴：_is_fatal_failure 无 not-found 关键字且零测试，缺失 provider 被空转重试 | VERIFIED | BUG | NEW | `astrmai/infrastructure/gateway/gateway_policy.py:L169-191`; `astrmai/infrastructure/gateway/gateway_call.py:L340-377` | R1 | ✔主控 |
| **ML-03** | 偏好类权威事实 dedup_key 只到 attribute 级（{uid}:preference:like），新偏好 supersede 旧偏好导致旧事实丢失 | VERIFIED | BUG | NEW | `astrmai/memory/services/memory_claim_service.py:L69-L86`; `astrmai/memory/services/instant_memory_gate.py:L153`; `astrmai/memory/services/v2_store.py:L900-L913` | R2 | ✔主控 |
| **ML-04** | 索引清理绕过 FaissVecDB.delete，删除/替换后嵌入向量永不回收——向量召回被幽灵 id 逐渐挤占 | VERIFIED | BUG | NEW | `astrmai/memory/services/memory_index_projector.py:L105-L110`; `site-packages/astrbot/core/db/vec_db/faiss_impl/vec_db.py:L168-L180`; `site-packages/astrbot/core/db/vec_db/faiss_impl/vec_db.py:L125-L146` | R2 | ✔主控 |
| **WU-01** | 表达审核"编辑通过/编辑驳回"静默丢弃人工修改后的表达文本（replacement 不随 approve/reject 落库） | VERIFIED | BUG | NEW | `pages/admin/app.js:L728-L730`; `astrmai/learning/review/review_service.py:L96-L113`; `astrmai/webui/backend/services/review_ui_service.py:L226-L239` | R2 | ✔主控 |
| **WU-02** | 审核权重输入被当作"相对 1.0 的增量"应用，每次编辑通过都使权重漂移（clamp 3.0） | VERIFIED | BUG | NEW | `astrmai/webui/backend/services/review_ui_service.py:L217-L224`; `astrmai/memory/services/expression_pattern_service.py:L316`; `pages/admin/app.js:L721` | R2 | ✔主控 |
| **WU-03** | pending_human 表达候选计入"待审核"徽标但不出现在人工待审队列（auto-check 升级人工的候选不可见） | VERIFIED | BUG | KNOWN_FIXED_REGRESSION | `astrmai/memory/services/expression_pattern_service.py:L209-L221`; `astrmai/learning/review/expression_auto_check_task.py:L119-L126`; `astrmai/webui/backend/services/runtime_memory_stats.py:L60-L63` | R2 | ✔主控 |
| **WU-04** | MemoryMaintenanceService.run_once（索引一致性修复+黑话/表达积压过期清理）无任何调度器，唯一入口是前端从不调用的 WebUI 端点 | VERIFIED | BUG | NEW | `astrmai/webui/backend/services/memory_ui_service.py:L615-L620`; `astrmai/proactive/proactive_task.py:L787`; `astrmai/memory/services/memory_engine.py:L313-L328` | R2 | ✔主控 |
| **ML-02** | think>=3 深检索串行 rewrite(8s必超时)+rerank+guidance 两次无超时 LLM，注入耗时 50~92s，3 次深检索 2 次以 stale_drop 丢弃回复 | VERIFIED | BUG | KNOWN_FIXED_REGRESSION | `astrmai/memory/services/memory_retrieval_service.py:L906-L916`; `astrmai/memory/services/memory_retrieval_service.py:L403-L404`; `astrmai/memory/services/memory_retrieval_service.py:L428-L433` | R3 |  |
| **RT-03** | mood LLM 串行前置于 judge 且与 judge 内嵌 mood 双重计算：364 次调用中约 302 次花在最终不回复的消息上，构成群聊 ingress p50 4.4s 延迟 | VERIFIED | DESIGN | NEW | `astrmai/conversation/attention/gate.py:L1079-L1080`; `astrmai/state/chat_state_service.py:L378-L379`; `astrmai/conversation/decision/judge.py:L459-L462, L520-L532` | R3 | ✔主控 |
| **RT-05** | 视觉链路重试乘法（框架5×网关3×池7模型）+ executor 旁路无超时 + 合并循环重置屏障 deadline：单图可烧掉整个 360s 轮预算 | VERIFIED | BUG | KNOWN_FIXED_REGRESSION | `astrmai/conversation/execution/executor.py:L682-L688`; `astrmai/conversation/attention/private_turn_coordinator.py:L401-L402`; `astrmai/conversation/attention/gate.py:L1310-L1315` | R3 | ✔主控 |
| **TL-04** | gateway 层 side-effect 中止保护被 executor 模型级联绕过：space_transition 可能向好友重复真发私聊，失败尝试排队的动作随 fallback 提交 | LIKELY | BUG | NEW | `astrmai/infrastructure/gateway/gateway_lane.py:L995-L1000`; `astrmai/conversation/execution/executor.py:L1065-L1082`; `astrmai/conversation/planning/tools/pfc_tools.py:L2736-L2742` | R3 | ✔主控 |
| **PL-03** | UI '合并私聊连续输入' 开关是死键：timing.turn_merge_enabled 被 pydantic 静默丢弃，无法关闭合并 | VERIFIED | BUG | NEW | `_conf_schema.json:L1095-L1100`; `config.py:L17-L27`; `astrmai/conversation/attention/private_turn_coordinator.py:L129-L130` | R4 | ✔主控 |
| **PL-04** | '启用基础内容安全过滤（NSFW/自残/PII 检测）' 是虚假开关：全仓库不存在任何实现 | VERIFIED | BUG | NEW | `config.py:L179`; `_conf_schema.json:L433-L438` | R4 | ✔主控 |
| **TG-01** | 群聊身份隔离无端到端回归：speaker block、关系数据、终线 guard 三个身份来源各自单测，无一测试断言三者指向同一 sender | VERIFIED | DESIGN | NEW | `astrmai/conversation/planning/planner.py:L1224`; `astrmai/conversation/planning/planner_prompt_context.py:L155-156`; `astrmai/conversation/planning/planner_side_inputs.py:L891-897` | R5 |  |
| **TG-03** | Turn 总预算端到端零守护：配置接线、网关耗尽分支、judge 耗尽降级三个执法点均无测试，接线失败会静默让预算失效 | VERIFIED | DESIGN | NEW | `astrmai/presentation/events/message_entry.py:L145-156`; `astrmai/infrastructure/gateway/gateway_call.py:L283-289`; `astrmai/conversation/attention/decision_router.py:L123-128` | R5 | ✔主控 |
| **ML-05** | think 门 + 窄关键词门使记忆注入率仅 2.9%（私聊 0/19）：正常聊天读不到已写入的记忆 | VERIFIED | DESIGN | KNOWN_OPEN | `astrmai/conversation/planning/prompt_refiner.py:L678-L684`; `astrmai/memory/services/memory_injection_service.py:L22-L33` | R7 | ✔主控 |

## P2（36 条）

| ID | 标题 | 真实性 | 分类 | 新旧 | 证据 | 轮次 | 复核 |
|---|---|---|---|---|---|---|---|
| **ID-03** | Peer poke（群友互戳）虚拟事件 100% 被过滤，整套 peer 互动剧本为死代码 | VERIFIED | BUG | NEW | `astrmai/conversation/ingress/sensors.py:L643-L646`; `astrmai/conversation/ingress/sensors.py:L317-L318`; `astrmai/conversation/planning/planner_prompt_context.py:L525-L529` | R1 |  |
| **ID-06** | Proactive 事件的'当前发言人归因锁'指向幽灵用户 astrmai_proactive_candidate（被 ID-02 掩盖的二级缺陷） | VERIFIED | BUG | NEW | `astrmai/conversation/planning/planner_prompt_context.py:L148-L180, L463-L467`; `astrmai/conversation/planning/prompt_refiner.py:L153-L158`; `astrmai/proactive/dispatcher.py:L330-L331` | R1 |  |
| **TL-05** | is_stale_reply_reason 漏配 superseded_by_newer_activity_same/_unknown_thread 变体，过期回复被误判为模型失败并触发换模型重试 | VERIFIED | BUG | NEW | `astrmai/conversation/execution/reply_freshness.py:L50-L58`; `astrmai/infrastructure/runtime/chat_runtime_coordinator.py:L469-L477`; `astrmai/conversation/execution/executor.py:L798-L804` | R1 | ✔主控 |
| **WU-08** | /learning/cooldowns 永远返回空对象：读取的属性名 _recent_patterns 从未存在（真实为 _recent_pattern_keys） | VERIFIED | BUG | NEW | `astrmai/webui/backend/services/admin_ui_service.py:L1054-L1063`; `astrmai/conversation/planning/expression_policy.py:L383` | R1 | ✔主控 |
| **ML-10** | 会话摘要主路径（pipeline buffer）说话人解析失败——参与者全部 unknown，群记忆无法归属到人 | VERIFIED | BUG | NEW | `astrmai/memory/services/memory_turn_pipeline.py:L136-L139`; `astrmai/memory/services/session_memory_summarizer.py:L364-L370`; `astrmai/memory/services/v2_store.py:L1032-L1040` | R2 | ✔主控 |
| **WU-05** | 表达审核通过/驳回不同步召回索引投影（jargon/canonical 路径都同步，唯独 expression 缺失） | VERIFIED | BUG | NEW | `astrmai/memory/services/expression_pattern_service.py:L369-L380`; `astrmai/webui/backend/services/memory_ui_service.py:L1185-L1189`; `astrmai/memory/services/memory_index_projector.py:L136-L170` | R2 |  |
| **WU-07** | 黑话"驳回并删除"硬删除抹掉 rejected 墓碑，挖掘器会把同一噪声词重新捞回待审队列 | VERIFIED | BUG | NEW | `astrmai/webui/backend/services/memory_ui_service.py:L1319-L1322`; `astrmai/memory/services/v2_store.py:L1561-L1569`; `astrmai/learning/mining/jargon_miner.py:L57-L63` | R2 |  |
| **WU-10** | 黑话/表达关键字搜索只过滤当前页：服务端 query 过滤发生在 LIMIT/OFFSET 之后，total 用未过滤总数 | VERIFIED | BUG | NEW | `astrmai/webui/backend/services/memory_ui_service.py:L657-L668`; `pages/admin/app.js:L1520-L1521` | R2 |  |
| **ID-09** | 私聊回复中位延迟 44s：settle→mood→judge→cognitive→tools 五段串行，且私聊 judge 16h 内 0 次非 REPLY 纯属延迟 | VERIFIED | DESIGN | NEW | `astrmai/conversation/attention/gate.py:L1293-L1296, L1354-L1359, L1443-L1449`; `astrmai/conversation/attention/private_turn_coordinator.py:L220-L229` | R3 |  |
| **ML-06** | 发送后内联 claim 抽取 LLM 在 turn 任务内同步执行（实测 5.2~44.5s×7），拖长 turn 与 per-chat 后续处理 | VERIFIED | RUNTIME | NEW | `astrmai/conversation/execution/reply_service.py:L198-L203`; `astrmai/memory/services/memory_claim_service.py:L146-L153` | R3 |  |
| **RT-04** | gateway.tool（dialog 主回复/工具环）完全不受 turn 预算约束，与 gateway.chat 的预算语义不一致 | VERIFIED | BUG | NEW | `astrmai/infrastructure/gateway/gateway_lane.py:L182-L185`; `astrmai/infrastructure/gateway/gateway_lane.py:L729-L744` | R3 | ✔主控 |
| **RT-06** | cognitive_loop 在默认 think_level=1 上仍串行运行（8-35s LLM），think 分级未覆盖此高频成本 | VERIFIED | DESIGN | NEW | `astrmai/conversation/planning/cognitive_loop.py:L192-L194`; `astrmai/conversation/planning/cognitive_loop.py:L222-L225` | R3 |  |
| **RT-09** | judge prompt 缓存敌对结构未修：539 次调用 × p50 1977 字符动态段内嵌 1.4K 固定 rubric，前缀命中 0-25% | VERIFIED | DESIGN | KNOWN_OPEN | `astrmai/conversation/decision/judge.py:L419-L463` | R3 |  |
| **RT-11** | 全局 LLM 信号量(3) 把 ambient judge/mood 与主回复混排，skipped 轮 judge 条目出现 30-51.7s 排队 | LIKELY | DESIGN | NEW | `astrmai/infrastructure/gateway/gateway_call.py:L193-L194`; `astrmai/infrastructure/gateway/model_gateway.py:L38` | R3 |  |
| **PL-02** | 主动链三层诊断全误标：日志称 'skipped by planner'、dispatcher status=skipped 无原因、trace proactive 字段恒空 | VERIFIED | RUNTIME | NEW | `astrmai/proactive/wakeup_service.py:L181-L184`; `astrmai/conversation/planning/planner.py:L1100-L1106` | R4 |  |
| **PL-05** | 另 7 个死配置键：debounce_window/max_message_length/repeater_threshold/throttle_probability/throttle_min_entropy/enable_relationship_engine/unknown_decay | VERIFIED | BUG | NEW | `astrmai/conversation/attention/window_buffer.py:L17-L24`; `astrmai/conversation/attention/gate.py:L926-L929`; `astrmai/state/chat_state_service.py:L271` | R4 |  |
| **PL-06** | 越界配置=插件整体拒载：AstrMaiConfig 校验异常未捕获，且约 90 个数值键 UI 无范围约束提示 | VERIFIED | BUG | NEW | `main.py:L62-L65`; `astrmai/webui/backend/adapters/plugin_api.py:L458-L471` | R4 |  |
| **PL-09** | 插件重载即短期上下文失忆：GroupDialogueStore/压缩链纯内存，AstrBot 侧任何配置保存都清零并掐掉在飞 turn | VERIFIED | DESIGN | NEW | `astrmai/conversation/attention/group_dialogue_store.py:L53-L59`; `astrmai/infrastructure/runtime/chat_runtime_coordinator.py:L401-L418` | R4 |  |
| **RT-08** | provider 能力解析全量失败（provider=unknown 1005/1005）：cache_control/provider session 特性被静默关闭，观测字段失真 | VERIFIED | BUG | NEW | `astrmai/infrastructure/gateway/provider_capabilities.py:L107-L121`; `astrmai/infrastructure/gateway/provider_capabilities.py:L54-L59` | R4 |  |
| **ID-05** | stage_ledger reply.send 的 sent_segment_count 恒为 0（满发路径从不写 metadata）——确认为 instrumentation bug，无真实丢段 | VERIFIED | RUNTIME | NEW | `astrmai/conversation/execution/reply_artifact_builder.py:L544, L601-L604, L634-L637`; `astrmai/conversation/execution/reply_service.py:L153, L181` | R5 | ✔主控 |
| **RT-02** | analyze_turn_ledger.py judge 口径错误（按 stage 匹配），judge_calls_per_turn=0 掩盖了仍存在的同轮多次 judge（真实 p50=1/p95=2/max=10） | VERIFIED | RUNTIME | KNOWN_OPEN | `scripts/analyze_turn_ledger.py:L160-L162`; `astrmai/infrastructure/gateway/gateway_lane.py:L399-L404`; `astrmai/conversation/attention/gate.py:L1509-L1510` | R5 | ✔主控 |
| **TG-04** | trace v2 memory_funnel 在 executed turns 中 64/67 缺失（prompt_refiner 7 条 early-return 不写 funnel），且无字段完整性契约测试；context_block_stats 的 511/585 缺失系误报 | VERIFIED | RUNTIME | NEW | `astrmai/conversation/planning/prompt_refiner.py:L646-697`; `astrmai/memory/services/memory_injection_service.py:L182-188`; `scripts/analyze_turn_ledger.py:L196,L223` | R5 | ✔主控 |
| **TG-05** | 记忆闭环缺'修订'腿：WebUI update_canonical 修订内容→projector 重投影→检索/注入反映新内容 无任何测试（WebUI 测试全部 projector=None 或 mock store） | VERIFIED | DESIGN | NEW | `astrmai/webui/backend/services/memory_ui_service.py:L338-346`; `tests/test_webui_backend_refactor.py:L524-527` | R5 |  |
| **TG-06** | WebUI 前后端契约无自动对齐校验：前端 app.js 75 个 api 路径 vs 后端注册表，测试只有手工镜像清单+JS 字符串 pin，历史已有 ≥4 例 FE/BE 漂移 bug | VERIFIED | DESIGN | KNOWN_OPEN | `pages/admin/app.js:L350-358`; `tests/test_plugin_pages_admin_refactor.py:L29-33` | R5 |  |
| **TG-07** | 4da2910 私聊 vision barrier 的 gate 消费侧组合分支无测试：屏障期间新消息 re-merge 续跑、abort 后池非空续跑、resolve 超时 outcome | VERIFIED | DESIGN | NEW | `astrmai/conversation/attention/gate.py:L1311-1315`; `astrmai/conversation/attention/gate.py:L1331-1336`; `astrmai/conversation/attention/private_turn_coordinator.py:L403-418` | R5 |  |
| **WU-06** | TurnTrace 样本库每条消息全文件读改写（15MB 实测，封顶约 42MB），与 WebUI 45s 轮询读共用一把锁 | VERIFIED | RUNTIME | NEW | `astrmai/infrastructure/runtime/turn_trace_store.py:L94-L115`; `astrmai/conversation/attention/gate.py:L981-L992`; `astrmai/conversation/planning/planner.py:L895-L897` | R5 |  |
| **ID-04** | 私聊 prompt 被硬编码'群聊/群友/群里'话术污染（warm/cold 摘要模板不分场景） | VERIFIED | BUG | NEW | `astrmai/conversation/attention/group_dialogue_store.py:L348-L355`; `astrmai/conversation/attention/group_dialogue_store.py:L363-L380`; `astrmai/conversation/attention/context_compaction.py:L1594-L1600` | R6 | ✔主控 |
| **TL-01** | 二段披露展开机制 585 轮/16h 零触发：唯一入口是模型主动调 bot_capability_lookup 且需整轮重跑，实践中不可达 | VERIFIED | DESIGN | NEW | `astrmai/conversation/planning/tools/pfc_tools.py:L2374-L2382`; `astrmai/conversation/execution/executor.py:L966-L989` | R6 |  |
| **TL-02** | social_intent(tease/comfort) 家族过滤清空披露层为图片/引用消息加的 artifact 工具，连 core 查询与 wait_and_listen 一并剥除 | VERIFIED | BUG | NEW | `astrmai/conversation/planning/planner_side_inputs.py:L699-L710`; `astrmai/conversation/planning/planner_side_inputs.py:L1005-L1007` | R6 |  |
| **TL-03** | sanitized execution event 将消息组件替换为 Plain 占位，vision/artifact 工具的『当前消息』路径必然假阴性 | VERIFIED | BUG | NEW | `astrmai/conversation/execution/executor.py:L106-L114`; `astrmai/conversation/planning/tools/pfc_tools.py:L2053-L2061` | R6 | ✔主控 |
| **TL-06** | 『听说/据说/有人说/不确定』日常词直接构成 unverified_report 显式工具意图：升级 task tier 并强制 required 工具 | LIKELY | BUG | NEW | `astrmai/conversation/planning/planner_side_inputs.py:L150`; `astrmai/conversation/planning/tool_intent_resolution.py:L207-L215`; `astrmai/conversation/planning/planner_side_inputs.py:L1143-L1151` | R6 |  |
| **TL-07** | perception.image_count 在全部 585 traces 恒 0，图片轮在观测层不可辨识 | VERIFIED | RUNTIME | NEW | `astrmai/conversation/planning/planner_side_inputs.py:L959-L962` | R6 |  |
| **ML-07** | Dream 每轮往真实会话写『[dream_maintenance] 完成 N 次维护动作』运维噪声记忆；LLM 合并叙事不经 admission 治理直接 active | VERIFIED | RUNTIME | NEW | `astrmai/proactive/dream_scheduler.py:L233-L237`; `astrmai/memory/dream/dream_agent.py:L226-L231`; `astrmai/memory/services/memory_admission_service.py:L22` | R7 |  |
| **ML-08** | Dream 事实晋升的 3 证据阈值可被单次 LLM 响应内重复项满足，写出 confidence=1.0 权威事实并可覆盖用户亲述事实 | LIKELY | BUG | NEW | `astrmai/memory/dream/promotion_engine.py:L105-L106`; `astrmai/memory/dream/promotion_engine.py:L147-L149`; `astrmai/memory/dream/fact_contract.py:L21-L26` | R7 |  |
| **ML-09** | 挖掘 fail-closed 无毒丸跳过：坏批次每 30min 原样重试可永久卡死单群学习；新窗口 16.6h 零挖掘日志，链路是否存活不可观测 | VERIFIED | DESIGN | NEW | `astrmai/learning/evolution_manager.py:L604-L614`; `astrmai/learning/evolution_manager.py:L809-L812` | R7 |  |
| **WU-09** | 空数据三义性：前端把错误回退缓存成 180 秒"新鲜空数据"；runtime_bound:false 与真无数据渲染完全相同 | VERIFIED | BUG | NEW | `pages/admin/app.js:L159-L165`; `pages/admin/app.js:L440-L452`; `astrmai/webui/backend/services/memory_ui_service.py:L275-L276` | R7 |  |

## P3（13 条）

| ID | 标题 | 真实性 | 分类 | 新旧 | 证据 | 轮次 | 复核 |
|---|---|---|---|---|---|---|---|
| **RT-07** | compaction_provider_id 配置指向不存在的 openai/deepseek-v4-pro：每次压缩首个尝试必失败 | VERIFIED | RUNTIME | NEW | `astrmai/conversation/attention/compaction_providers.py:L24-L28`; `astrmai/conversation/attention/context_compaction.py:L215` | R1 | ✔主控 |
| **PL-10** | PluginLifecycleManager._terminated 永久闩锁：同实例 terminate 后 on_program_start 永拒，无解除路径 | NEEDS_RUNTIME_EVIDENCE | BUG | NEW | `astrmai/app/lifecycle.py:L53-L56,L307-L310` | R4 |  |
| **PL-11** | agent.max_steps 被 executor 静默钳制到 >=5：schema/pydantic 允许 1-4 但无效 | VERIFIED | BUG | NEW | `astrmai/conversation/execution/executor.py:L529-L531` | R4 |  |
| **RT-10** | 观测字段小缺陷簇：prefix_changed_reason 稳定轮被标 unavailable_in_trace、63 次 attention.dispatch abandoned 为快照顺序伪影、trace created_at 是捕获时刻 | VERIFIED | RUNTIME | NEW | `astrmai/conversation/planning/planner.py:L263`; `astrmai/conversation/planning/context_engine.py:L229-L230`; `astrmai/conversation/planning/planner.py:L745-L753` | R5 |  |
| **ID-08** | 撤回(recall)通知零处理：被撤回消息原文继续留在对话上下文中可被 bot 引用 | VERIFIED | DESIGN | NEW | `astrmai/presentation/events/message_entry.py:L89-L97, L160-L176`; `astrmai/conversation/attention/group_dialogue_store.py:L92-L132` | R6 |  |
| **ID-10** | poke 目标解析兜底把'戳别人(目标缺失)'误记为'戳 bot'：无端回戳+好感度误结算 | VERIFIED | BUG | NEW | `astrmai/conversation/ingress/sensors.py:L500-L504`; `astrmai/conversation/ingress/sensors.py:L605-L616` | R6 |  |
| **TL-08** | FAMILY_TO_PACKAGES['quote_reply'] 是死配置：quote_reply 属 PRECISION_ONLY，包映射永不生效，引用场景无自主 quote 能力为纯关键词依赖 | VERIFIED | DESIGN | NEW | `astrmai/conversation/planning/tool_disclosure.py:L99, L132-L149`; `astrmai/conversation/planning/tool_disclosure.py:L375-L379` | R6 |  |
| **TL-09** | 跨会话 handoff 仅内存驻留且注入块 360 字符截断，三方消歧指令位于截断尾部 | VERIFIED | DESIGN | NEW | `astrmai/infrastructure/runtime/cross_session_handoff_store.py:L36-L43`; `astrmai/conversation/planning/planner_side_inputs.py:L1288-L1309` | R6 |  |
| **ID-07** | group_wait 残留不对称：reply: 键位等待无法被目标的普通跟进复活，unique-target 兜底因 has_explicit_thread 恒真成死分支 | LIKELY | BUG | KNOWN_OPEN | `astrmai/state/group_wait/group_reply_wait_manager.py:L53-L62, L329-L337`; `astrmai/presentation/events/message_entry.py:L282-L285` | R7 |  |
| **ML-11** | 演示场景启发式硬编码进生产：server_count=(\d+) 任意数字、火锅/芒果/蓝色词表进入 claim 规则与检索重排 | VERIFIED | DESIGN | NEW | `astrmai/memory/services/claim_rules.py:L9-L12`; `astrmai/memory/services/memory_retrieval_service.py:L253-L254` | R7 |  |
| **TG-08** | 测试基建健康核实：收集 1673 条 0 错误、manual 脚本未腐化；session-state.md 测试计数(1142)过期 | VERIFIED | DESIGN | NEW | `.agent/session-state.md:L34` | R7 |  |
| **WU-11** | trace v2 新字段（llm_call_ledger/stage_ledger/reply_stats/budget/memory_funnel）已随 API 返回但管理页零呈现；工具披露表"工具"列恒为"-" | VERIFIED | DESIGN | NEW | `pages/admin/app.js:L1262-L1278`; `astrmai/conversation/planning/planner.py:L755-L770`; `pages/admin/app.js:L1294` | R7 |  |
| **WU-12** | 计数口径与删除反馈错位集合：表达 total 含已删行、"黑话全量"实为已通过、legacy 事件删除返回 readonly 却 toast 已删除、Dashboard 待审仅统计表达 | VERIFIED | BUG | NEW | `astrmai/webui/backend/services/runtime_memory_stats.py:L61-L64`; `pages/admin/app.js:L1501`; `astrmai/webui/backend/services/memory_ui_service.py:L928-L933` | R7 |  |

## 逐条详情（用户可感知后果 / 根因 / 最小修复边界 / 验证）

### ID-01 [P0/VERIFIED] 群聊在途回复被任意无关新消息击杀：freshness 线程隔离因 latest_activity_thread_signature 恒空而整体失效

- **用户可感知后果**: 活跃群里 @bot/点名直接提问后，bot 已生成的回复被静默丢弃且无补偿：16h 内最活跃群 executed 6 条 vs stale_drop 7 条，其中 4 条是被别人（甚至是 bot 自己都忽略的被动图片）的新消息在 11-30s 内误杀；LLM 成本照付。
- **根因**: gate._record_event_activity 在 process_event 早期调用 mark_activity，此时 astrmai_thread_signature 尚未生成（focus 构建后才由 emit_legacy_focus_thread_extras 写入），latest_activity_thread_signature 恒为空 → evaluate_reply_freshness 的 different_known_thread 永假 → allow_parallel_threads 永不触发，所有新活动都判成 unknown_thread stale；executor 侧调用还漏传 allow_parallel_threads。
- **证据**: `astrmai/conversation/attention/gate.py:L527-L536, L1205` — `await self.runtime_coordinator.mark_activity(     chat_id, now, activity_sender_id, ...,     event.get_extra("astrmai_thread_signature", None) ...)`
- **证据**: `astrmai/infrastructure/runtime/chat_runtime_coordinator.py:L447-L477` — `different_known_thread = bool(thread_signature and latest_signature and thread_signature != latest_signature) if allow_parallel_threads and different_known_thre`
- **证据**: `astrmai/conversation/execution/executor.py:L444-L449, L759-L762` — `return await self.runtime_coordinator.evaluate_reply_freshness(     chat_id, focus_timestamp, max_age_seconds=max_age_seconds,     thread_signature=thread_signa`
- **证据**: `astrmai/conversation/execution/reply_freshness.py:L221-L228` — `allow_parallel_threads=not bool(event.get_extra("is_private_chat", False)),`
- **运行时佐证**: traces: stale_drop 7/585 全在群 1062115731（该群 executed 仅 6），4 例 stale_category=newer_activity_unknown_thread（age 11.5/18.3/23.2/29.8s，max_age 450s）；log 15:23:22 'stop expired tool execution: superseded_by_newer_activity_unknown_thread:师清漪:6.7s'，击杀者为随后被 IGNORED_IMAGE 的被动图片；受害消息含 '为什么加了妃妃还要等同意' '我是你的哥哥吗'。
- **相关测试**: `tests/original_ported/test_reply_freshness_budget_ported.py`, `tests/integration/test_message_to_reply_pipeline.py`
- **测试缺口**: 缺集成断言：ingress 时刻 mark_activity 携带的线程标识非空；缺 '他人新消息不杀 A 的在途回复' 的端到端用例。
- **最小修复边界**: gate.py::_record_event_activity 改传 resolve_group_thread(event,chat_id).thread_id；chat_runtime_coordinator.evaluate_reply_freshness 改按 turn thread_id 比较；executor.py::_evaluate_execution_freshness 补 allow_parallel_threads；executor 预检路径补记 stale_category（3/7 样本字段全空）并停止把 freshness 中止记为 'tool model failed'（executor.py L1081）。
- **回归风险**: 中——放开跨线程并行后同群并发回复增多，依赖 send_claim/actor_consistency 兜底，需灰度观察。
- **建议验证**: 构造群聊：A @bot 提问，5s 后 B 发无关消息；断言 A 的回复仍发送且 trace 无 newer_activity_unknown_thread；再跑 python scripts/analyze_turn_ledger.py 比对 stale_drop 率。
- **适合立即开发**: 是 | **执行轮次**: R1
- **关联发现**: ID-05, ID-07

### PL-01 [P0/VERIFIED] 主动开口全链路死于传感器过滤：合成事件 message_obj=None，wakeup/heartflow/signin 主动消息从未发出过

- **用户可感知后果**: life.enable_proactive 开启、能量/静默/安静时段机制全在跑，但用户永远收不到任何主动消息（wakeup、heartflow 可见候选、群签到跟发三类全灭）；每 8h/群空转一次候选构造。
- **根因**: _SyntheticExternalEvent 只带 message_str（message_obj=None，gate.py L39 'reserved for future use'）；sensors.should_process_message 仅从 message_obj.message 的 Plain 组件提取 clean_text，从不读 message_str → clean_text 空且无 payload → sensors.py L317 return False → gate.py L1137 判 skipped_sensor_filter，事件到不了 judge/planner。
- **证据**: `astrmai/conversation/attention/gate.py:L36-L39` — `self.message_str = str(self._data.get("message_str", ...) or "") ... self.message_obj = self._data.get("message_obj")  # reserved for future use`
- **证据**: `astrmai/conversation/ingress/sensors.py:L205,L317-L318` — `if event.message_obj and event.message_obj.message: ... if not clean_text and not has_payload:     return False`
- **证据**: `astrmai/conversation/attention/gate.py:L1137-L1144` — `if not sensor_checked and not await self._passes_sensor_filters(event, msg_str):     await self._complete_proactive_candidate(event, reason="sensor_filtered")  `
- **证据**: `astrmai/proactive/dispatcher.py:L327-L351` — `event_data = {"message_str": candidate_text, ... "sender_id": "astrmai_proactive_candidate", ...}  # 无 message_obj`
- **运行时佐证**: trace: 14/14 sender=astrmai_proactive_candidate 的 turn status=skipped_sensor_filter 且 llm_call_ledger=[]；两个观测窗（20bb585/c4aee57）grep 'proactive wakeup sent via main chain' 均为 0；wakeup 候选 14 次跨 9 群、同群间隔 ≥8h。
- **合并说明**: 吸收 ID-02（sensors.py:317-318 空组件过滤 + dispatcher.py:327-351 合成事件无 message 组件的独立取证）。ID-03（peer poke 同源被滤）、ID-06（幽灵用户归因锁）、PL-02（三层诊断误标）为同批必修伴随项。
- **相关测试**: `tests/unit/proactive/test_proactive_gap_coverage.py`
- **测试缺口**: 缺 inject_external_event→sensors→judge 的贯通集成测试；现有测试 mock 掉 attention_gate，接缝无覆盖。
- **最小修复边界**: gate.AttentionGate._passes_sensor_filters 或 sensors.should_process_message 对 astrmai_is_proactive_event 豁免组件文本检查；或 dispatcher 构造 event_data 时补 Plain 组件 message_obj。
- **回归风险**: 低——豁免只影响带 proactive 标记的合成事件，真实消息路径不变。
- **建议验证**: 本地构造 ProactiveMessageIntent 走 dispatcher.dispatch，断言 turn status 进入 judge 阶段而非 skipped_sensor_filter；线上观察 'proactive wakeup sent via main chain' 出现。
- **适合立即开发**: 是 | **执行轮次**: R1
- **关联发现**: PL-02

### RT-01 [P0/VERIFIED] 跨 task 继承的 contextvar 遥测导致预算 clamp 用错轮：per-chat memory worker 里 instant backfill 100% 失败 turn_deadline_exhausted

- **用户可感知后果**: 即时记忆 LLM 兜底通道在任何聊天开始 6 分钟后永久失效（16h 内 17/17 次失败），用户新透露的事实无法进即时记忆；同机制还会让常驻 worker 里其它 gateway.chat 调用（mood/cognitive/judge）在陈旧上下文中集体秒失败（日志 turn_deadline_exhausted 共 71 条）。
- **根因**: turn_call_ledger 双轨记账：gateway_call.py 的 clamp_timeout_to_turn_budget/record_llm_attempt 恒传 event=None 走 _CURRENT_TELEMETRY contextvar；contextvar 随 asyncio.create_task 拷贝，memory_turn_pipeline.on_turn_committed 在某 chat 第一轮的处理上下文里 lazily 创建常驻 _chat_worker，worker 永久携带第一轮 telemetry，360s 后 remaining=0 → 每次调用先验地 raise TimeoutError('turn_deadline_exhausted')。
- **证据**: `astrmai/infrastructure/gateway/gateway_call.py:L283-L289` — `effective_timeout = clamp_timeout_to_turn_budget(     None,     timeout_limit,     reserve_for_reply=reserve_for_reply, ) if effective_timeout <= 0.0:     raise`
- **证据**: `astrmai/memory/services/memory_turn_pipeline.py:L170-L177` — `queue = self._worker_queues.get(turn.chat_id) if queue is None:     queue = asyncio.Queue()     self._worker_queues[turn.chat_id] = queue     task = asyncio.cre`
- **证据**: `astrmai/infrastructure/runtime/turn_call_ledger.py:L215-L232` — `def current_turn_telemetry(event: Any = None):     if event is not None: ...     return _CURRENT_TELEMETRY.get()`
- **运行时佐证**: astrbot_since_c4aee57.log: 'turn_deadline_exhausted' 71 条（gateway_call 46 / gateway_tasks 6 / instant_memory_gate 17）；'instant llm backfill degraded: 所有模型均失败: turn_deadline_exhausted' 17 条且无任何成功 backfill；成对 code2+code3 同毫秒瞬时 timeout(1/3)。
- **合并说明**: 吸收 ML-01（event_bus.publish 懒启动 3 worker 继承 turn context，event_bus.py:209-216）与 PL-08（instant_memory_gate.py:246 继承已耗尽预算，17 次日志实锤）。合并后受害面：LLM 记忆兜底抽取 100% 失败、278/539 judge attempt 账本丢失、budget clamp 错轮。
- **相关测试**: `tests/test_turn_call_ledger_refactor.py`
- **测试缺口**: 缺少跨 asyncio.create_task 继承场景的测试：在 scope 内 spawn 常驻 worker，scope 过期后经 worker 调 _elastic_call_result，断言不被陈旧 deadline 拦截。
- **最小修复边界**: turn_call_ledger.clamp_timeout_to_turn_budget/record_llm_attempt 增加显式 event 透传（chat_in_lane_result→_elastic_call_result 传 event）；或后台 worker 创建时用 contextvars.copy_context() 前先清空 _CURRENT_TELEMETRY（提供 detach_turn_telemetry() helper，在 memory_turn_pipeline._chat_worker、gate._spawn_session_worker 入口调用）。
- **回归风险**: 中：改动触及所有 gateway 调用的预算路径，需保证轮内调用仍被正确 clamp。
- **建议验证**: 复跑 scripts/analyze_turn_ledger.py 看 instant backfill 成功率>0；grep 日志 turn_deadline_exhausted 应仅出现于真实预算耗尽轮。
- **适合立即开发**: 是 | **执行轮次**: R1
- **关联发现**: RT-02, RT-08

### TG-02 [P1/VERIFIED] Provider 失败矩阵缺 not-found 轴：_is_fatal_failure 无 not-found 关键字且零测试，缺失 provider 被空转重试

- **用户可感知后果**: 配置漂移导致模型池里出现不存在的 provider 时（生产已发生：openai/deepseek-v4-pro），网关把'没有找到 ID 为 … 的提供商'当作可重试错误，对永远不可能成功的模型重试 max_retries+1 次并夹 backoff 睡眠，用户为每次命中该模型的调用多等数秒；无任何测试锚定 not-found 应立即切下一模型。
- **根因**: gateway_policy._is_fatal_failure（L169-191）关键字表只有 429/403/quota/timeout 等，不含 '没有找到'/'not found'/'provider'；_classify_failure_kind（L147-167）归为 UNKNOWN。测试矩阵只覆盖 timeout/空响应/cooldown，not-found × primary/fallback 组合 0 测试。
- **证据**: `astrmai/infrastructure/gateway/gateway_policy.py:L169-191` — `fatal_keywords = ("429", "ratelimit", ... "408", "504",)  # 无 not-found 类关键字`
- **证据**: `astrmai/infrastructure/gateway/gateway_call.py:L340-377` — `is_fatal = self._is_fatal_failure(last_error, error=exc) ... if attempt < max_retries: ... await asyncio.sleep(remaining)`
- **运行时佐证**: ledger_analysis: model_attempts 层 ProviderNotFoundError 3 次；astrbot_since_c4aee57.log: star.context '没有找到 ID 为 openai/deepseek-v4-pro 的提供商' WARN 4 条
- **相关测试**: `tests/test_gateway_policy_refactor.py::test_classify_failure_kind_covers_new_output_categories`, `tests/test_gateway_context_passthrough_refactor.py::test_tool_loop_retries_empty_timeout_and_classifies_it`
- **测试缺口**: 缺参数化失败矩阵测试 (timeout|5xx|not-found) × (primary|fallback)，断言各组合的 attempt 次数、backoff 行为与最终选中模型；not-found 应断言单次尝试即切换。
- **最小修复边界**: gateway_policy.py::_is_fatal_failure 加 not-found 关键字（或独立 FailureKind.PROVIDER_NOT_FOUND）+ tests/test_gateway_policy_refactor.py 矩阵测试
- **回归风险**: 中：改 fatal 判定影响所有失败路径，需矩阵测试先行锚定现状再改
- **建议验证**: python -m pytest tests/test_gateway_policy_refactor.py tests/test_gateway_context_passthrough_refactor.py -q
- **适合立即开发**: 是 | **执行轮次**: R1
- **关联发现**: 疑与 gateway/infrastructure 域代理的 provider 路由发现重叠：代码行为本身（not-found 非致命）归该域修，本条聚焦测试守护

### ML-03 [P1/VERIFIED] 偏好类权威事实 dedup_key 只到 attribute 级（{uid}:preference:like），新偏好 supersede 旧偏好导致旧事实丢失

- **用户可感知后果**: 用户先说『我喜欢咖啡』后说『我喜欢猫』，咖啡偏好被标 superseded 并移出索引——bot 只记得最后一个喜好，此前所有 like/dislike 逐个被吞，用户感知为『你上次还说记得我喜欢X』失忆。
- **根因**: claim 抽取把所有喜好归为 attribute=like/dislike（多值集合被建模成单值 EAV）；authority 路径 mark_superseded_by_key 按 dedup_key={subject}:{entity}:{attribute} 把旧记录全部 supersede，write_service 随后 cleanup_deleted 移出索引。
- **证据**: `astrmai/memory/services/memory_claim_service.py:L69-L86` — `attribute="dislike" if verb in {"不喜欢", "讨厌", "不吃"} else "like",`
- **证据**: `astrmai/memory/services/instant_memory_gate.py:L153` — `dedup_key=f"{primary_claim.subject_id}:{primary_claim.entity}:{primary_claim.attribute}",`
- **证据**: `astrmai/memory/services/v2_store.py:L900-L913` — `if old_create_time < float(created_at or 0.0):     ...SET status = ?, superseded_by = ?...     covered_old_ids.append(old_id)`
- **证据**: `astrmai/memory/services/memory_write_service.py:L134-L135` — `if self.index_projector and superseded_old_ids:     await self.index_projector.cleanup_deleted(superseded_old_ids)`
- **运行时佐证**: 本地无 DB；建议采样：SELECT dedup_key, COUNT(*) n, SUM(status='superseded') dead FROM canonical_memories WHERE dedup_key LIKE '%:preference:%' GROUP BY dedup_key HAVING n>1;
- **相关测试**: `tests/unit/memory/test_memory_conflict_resolution.py`
- **测试缺口**: 缺『两条不同 value 的 like 先后写入后均可检索』的多值语义测试
- **最小修复边界**: memory_claim_service 偏好类 dedup_key 追加 value 归一片段，或 MemoryConflictResolver 对 like/dislike 禁用 authority_override（走普通 dedup 写入）
- **回归风险**: 中——需确认同 value 重复表述仍能去重（用 value 归一化解决）
- **建议验证**: 单测：instant gate 连续处理『我喜欢咖啡』『我喜欢猫』，断言 store 中两条 active 偏好
- **适合立即开发**: 是 | **执行轮次**: R2
- **关联发现**: ML-04

### ML-04 [P1/VERIFIED] 索引清理绕过 FaissVecDB.delete，删除/替换后嵌入向量永不回收——向量召回被幽灵 id 逐渐挤占

- **用户可感知后果**: 每次事实覆盖、软删、stale 标记、质量隔离都会留下幽灵向量；FAISS top-k 名额被已删条目占据后静默丢弃，向量召回率随运行时间单调下降，长期运行后语义检索趋于空转，且嵌入索引文件只增不减。
- **根因**: MemoryIndexProjector.cleanup_deleted/_clear_projected_documents 用裸 SQL 删 documents 与 memories_fts 行；AstrBot FaissVecDB.delete 才会同步 embedding_storage.delete([int_id])，该 API 从未被调用。
- **证据**: `astrmai/memory/services/memory_index_projector.py:L105-L110` — `deleted += await self.engine._execute_documents_write(     "DELETE FROM documents WHERE json_extract(metadata, '$.canonical_id') = ?",     (memory_id,), ...) aw`
- **证据**: `site-packages/astrbot/core/db/vec_db/faiss_impl/vec_db.py:L168-L180` — `async def delete(self, doc_id: str):     ...     await self.document_storage.delete_document_by_doc_id(doc_id)     await self.embedding_storage.delete([int_id])`
- **证据**: `site-packages/astrbot/core/db/vec_db/faiss_impl/vec_db.py:L125-L146` — `scores, indices = await self.embedding_storage.search(...) fetched_docs = await self.document_storage.get_documents(..., ids=indices[0]) ...pos = idx_pos.get(in`
- **运行时佐证**: 本地无 DB；建议采样：比较 embedding 存储行数与 documents 行数差值（表名见 astrbot faiss_impl/sqlite_init.sql），差值即幽灵向量数。
- **测试缺口**: 无任何 embedding 层删除一致性测试；需『写入→cleanup_deleted→faiss 检索不返回该文档且嵌入行被删』的集成测试
- **最小修复边界**: MemoryIndexProjector.cleanup_deleted：查出 documents.doc_id 后改调 engine.faiss_db.delete(doc_id)（或补调 embedding_storage.delete），rebuild 路径同步修改
- **回归风险**: 中——需处理 faiss 未初始化时的降级（保留现 SQL 路径作 fallback）
- **建议验证**: 集成测试或线上跑一次 supersede 后查 embedding 行数减少；maintenance run_once 的 index_repair 报告不再出现 orphan 增长
- **适合立即开发**: 是 | **执行轮次**: R2
- **关联发现**: ML-03

### WU-01 [P1/VERIFIED] 表达审核"编辑通过/编辑驳回"静默丢弃人工修改后的表达文本（replacement 不随 approve/reject 落库）

- **用户可感知后果**: 运营者在弹窗里修正表达文本后点"保存并通过"，toast 显示成功，但库里仍是 AI 原始文本；人工校准（60f70e1 核心卖点）对文本字段实际不生效，且无任何报错。
- **根因**: ReviewUiService.submit_review 主路径把 replacement 传给 facade，但 ExpressionReviewService.submit_review 只在 decision∈{revision_needed,revised,replace} 分支携带 replacement_expression/apply_replacement，前端只发 approve/reject；随后的 extra_update 补丁字典也不含 expression。只有降级路径（facade 失败时）才带 apply_replacement。
- **证据**: `pages/admin/app.js:L728-L730` — `if (action === "approve") await api.post(`/reviews/${segment(itemId)}/submit`, { ...payload, action: "approve" });`
- **证据**: `astrmai/learning/review/review_service.py:L96-L113` — `if normalized == "approved":     kwargs.update({"checked": True, "rejected": False, "review_status": "approved", "review_suggestion": ""})`
- **证据**: `astrmai/webui/backend/services/review_ui_service.py:L226-L239` — `extra_update = {key: value for key, value in {"situation": situation, "style": style, "shared_scope": shared_scope, "review_reason": reason, "review_suggestion"`
- **相关测试**: `tests/unit/webui/test_webui_gap_coverage.py::test_review_submit_forwards_manual_calibration_fields`
- **测试缺口**: 现有测试用 submit_review 返回 {status:deferred} 的桩只覆盖降级路径；缺"facade 成功 + replacement 落库"的主路径断言。
- **最小修复边界**: astrmai/webui/backend/services/review_ui_service.py::submit_review（将 replacement/apply_replacement 并入 extra_update 或在主路径调用 service.update_review）；或 learning/review/review_service.py::submit_review 的 approved 分支接受 replacement_expression。
- **回归风险**: 低——只影响 WebUI 提交链，approve 不带 replacement 时行为不变。
- **建议验证**: 单测：facade.submit_expression_review 返回含 id 的 dict，service.update_review 桩记录 kwargs，断言 replacement_expression==编辑文本且 apply_replacement=True；或线上编辑通过后 GET /memories/canonical/{id} 比对 content。
- **适合立即开发**: 是 | **执行轮次**: R2
- **关联发现**: WU-02

### WU-02 [P1/VERIFIED] 审核权重输入被当作"相对 1.0 的增量"应用，每次编辑通过都使权重漂移（clamp 3.0）

- **用户可感知后果**: 弹窗预填当前权重（如 2.0），用户不改动直接保存并通过 → 权重变 3.0；想把 0.5 调到 1.5 实际落成 1.0。表达选择按 weight 降序排序，人工校准反而污染排序。
- **根因**: review_ui_service.py L223 `weight_delta=float(weight) - 1.0` 假定当前权重恒为 1.0，而 ExpressionPatternService.update_review 按 current+delta 应用；同文件降级路径 L260-264 用 weight-current（正确），两路径不一致。
- **证据**: `astrmai/webui/backend/services/review_ui_service.py:L217-L224` — `result = await self.plugin_api.submit_review(..., weight_delta=float(weight) - 1.0 if weight is not None else 0.0,)`
- **证据**: `astrmai/memory/services/expression_pattern_service.py:L316` — `metadata["weight"] = max(0.0, min(3.0, self._safe_float(metadata.get("weight"), ...) + float(weight_delta or 0.0)))`
- **证据**: `pages/admin/app.js:L721` — `{ name: "weight", label: "权重", default: item.weight ?? 1.0, type: "number", cast: "float" },`
- **相关测试**: `tests/unit/webui/test_webui_gap_coverage.py::test_review_submit_forwards_manual_calibration_fields`
- **测试缺口**: 缺"编辑通过后权重等于输入绝对值"的断言（现有测试 weight=1.2 且 current=1.2 恰好 delta 归零，掩盖问题）。
- **最小修复边界**: review_ui_service.py::submit_review——先 get_pattern 取当前权重再算 delta（与降级路径同法），或给 facade 链路增加绝对权重参数。
- **回归风险**: 低——纯 WebUI 提交链计算。
- **建议验证**: 单测：current weight=2.0，提交 weight=2.0，断言最终 weight==2.0（现行代码会得 3.0）。
- **适合立即开发**: 是 | **执行轮次**: R2
- **关联发现**: WU-01

### WU-03 [P1/VERIFIED] pending_human 表达候选计入"待审核"徽标但不出现在人工待审队列（auto-check 升级人工的候选不可见）

- **用户可感知后果**: Dashboard/审核页显示"表达待审核 N"，打开"表达待审"tab 却为空或少于 N；被 AI 审核判定"需要人工定夺"(pending_human) 的候选恰恰进不了人工队列，只能在无过滤器的"表达全量"里人肉翻找——升级人工的审核流程实质死锁。
- **根因**: 徽标口径 canonical_kind_review_stats: pending=count(status=review_pending)（含 pending_human）；队列口径 ExpressionPatternService.list_reviewable_patterns 只保留 review_status∈{pending,revision_needed}。R09-04 修复把 pending_human 移出自动审核集合时，未注意该 helper 同时是 WebUI 人工队列（facade list_pending_expression_reviews）的数据源；legacy 分支（statuses 含 pending_human）语义未被移植。
- **证据**: `astrmai/memory/services/expression_pattern_service.py:L209-L221` — `statuses=["review_pending"],)         return [item for item in rows if str(item.review_status or "").strip().lower() in {"pending", "revision_needed"}]`
- **证据**: `astrmai/learning/review/expression_auto_check_task.py:L119-L126` — `kwargs.update({"checked": False, "rejected": False, "review_status": "pending_human", "review_suggestion": replacement or None,})`
- **证据**: `astrmai/webui/backend/services/runtime_memory_stats.py:L60-L63` — `pending = await _count_canonical(store, kind=kind, status="review_pending")`
- **相关测试**: `tests/regression/review/test_review_service_migrated.py`, `tests/regression/learning/test_round9_learning_review.py::test_pending_human_pattern_is_not_auto_reviewed`
- **测试缺口**: regression 测试只锁 legacy 分支包含 pending_human；缺 canonical 路径"人工队列包含 pending_human 且 auto-check 跳过它"的成对断言。
- **最小修复边界**: learning/review/review_service.py::list_pending_reviews——canonical 分支改为独立查询（statuses=review_pending，review_status∈{pending,revision_needed,pending_human}），不复用 list_reviewable_patterns；auto-check 继续用 list_reviewable_patterns。
- **回归风险**: 中——需确认 auto-check 不因此重新吃进 pending_human（其 L59 已有显式跳过，双保险）。
- **建议验证**: 构造 review_status=pending_human 的 expression_pattern，GET /astrmai/admin/reviews/pending 应包含它；expression_auto_check_task.run_once 不应处理它。
- **适合立即开发**: 是 | **执行轮次**: R2

### WU-04 [P1/VERIFIED] MemoryMaintenanceService.run_once（索引一致性修复+黑话/表达积压过期清理）无任何调度器，唯一入口是前端从不调用的 WebUI 端点

- **用户可感知后果**: 黑话 review_pending/pending_human 14 天过期清理、表达 pending 21 天/rejected 14 天清理、运行期索引一致性修复全部从不执行：待审队列随挖掘无限增长只能人手清；WU-05 的投影缺口在重启前不自愈；记忆质量面板"索引异常"数字持续增长且无解释。
- **根因**: run_once 全仓唯一调用方是 memory_ui_service.run_maintenance → POST /memories/maintenance/run，而 app.js 从不调用该端点（契约 diff 证实）；调度侧只挂了 DecayService.run_once → apply_daily_decay（无修复/清理）；索引修复只在 memory_engine faiss 初始化时执行一次（_index_consistency_repaired 进程级标志）。
- **证据**: `astrmai/webui/backend/services/memory_ui_service.py:L615-L620` — `async def run_maintenance(self, policy: dict | None = None) -> dict: ... await maintenance.run_once(policy=policy or {})`
- **证据**: `astrmai/proactive/proactive_task.py:L787` — `await self.decay_service.run_once()`
- **证据**: `astrmai/memory/services/memory_engine.py:L313-L328` — `if not self._index_consistency_repaired:     report = await self.index_projector.check_consistency() ... self._index_consistency_repaired = True`
- **运行时佐证**: contract_diff.py：POST /memories/maintenance/run 与 /memories/diagnostics/index/repair 均在"后端注册、前端从不调用"清单中。
- **测试缺口**: 缺"maintenance run_once 有调度方"的装配断言；缺积压清理端到端测试（写入 15 天前 pending 黑话 → 例行任务后消失）。
- **最小修复边界**: astrmai/proactive/proactive_task.py（或 dream_scheduler）——在日常低峰任务里调用 memory_engine.maintenance_service.run_once()；或至少在管理页记忆质量面板加"执行维护"按钮接通现有端点。
- **回归风险**: 中——run_once 含物理删除（purge），首次接通调度前应先在真实库上 dry-run 核对 protected_* 保护参数。
- **建议验证**: grep -rn "run_once" astrmai | grep maintenance 确认调用方；接通后观察 /memories/quality/overview 的 index 异常数归零、review_pending 超期条目减少。
- **适合立即开发**: 是 | **执行轮次**: R2
- **关联发现**: WU-05, WU-07

### ML-02 [P1/VERIFIED] think>=3 深检索串行 rewrite(8s必超时)+rerank+guidance 两次无超时 LLM，注入耗时 50~92s，3 次深检索 2 次以 stale_drop 丢弃回复

- **用户可感知后果**: 用户发出触发深度记忆的消息后，bot 卡 100~180s 然后一言不发（回复被 freshness 判过期丢弃）；16.6h 内仅有的 3 次深检索 2 次如此收场，深度记忆功能实际不可用。
- **根因**: retrieve_deep 的 _rerank_candidates/_compress_guidance 经 _call_deep_json 调 call_data_process_task 时不传 lane/timeout/预算钳制，各吃默认 API 超时且对 2~3 条候选也照跑；query_rewrite 的 8s 硬限在 deepseek-v4-flash p50≈7.4s 的池上几乎必超时，白烧 8s。
- **证据**: `astrmai/memory/services/memory_retrieval_service.py:L906-L916` — `async def _call_deep_json(self, prompt: str) -> dict:     ...     response = await gateway.call_data_process_task(prompt=prompt, is_json=True)`
- **证据**: `astrmai/memory/services/memory_retrieval_service.py:L403-L404` — `if query.policy == "deep" or (query.think_level is not None and query.think_level >= 3):     return await self.retrieve_deep(query)`
- **证据**: `astrmai/memory/services/memory_retrieval_service.py:L428-L433` — `reranked = await self._rerank_candidates(query, temporal_top) candidates = list(reranked) + list(temporal_tail) guidance = await self._compress_guidance(query, `
- **运行时佐证**: trace 1d01319296b6: memory.injection=50179ms(其中 query_rewrite_trace timeout 8000ms), status=stale_drop, reply_sent=False, turn 总时长 104.6s；trace dfdd1f5d89aa: injection abandoned@92167ms + 重试 71896ms, stale_drop, 总时长 179.8s；第三次深检索 491174a56687 rewrite 被预算钳到 1.6s 超时后 no_result。日志 memory_retrieval_service:856 deep query rewrite degraded ×3。
- **合并说明**: 吸收 RT-12（react_retriever.py:185 逐步无超时）。修复需双层：react step 循环包 wait_for + _call_deep_json 接 turn 预算 clamp。
- **相关测试**: `tests/unit/memory/test_memory_query_optimization.py`
- **测试缺口**: 缺 deep 路径总时延契约测试（mock 慢 LLM 断言不超过 turn 预算）与候选数≤top_k 时跳过 rerank 的测试
- **最小修复边界**: memory_retrieval_service._call_deep_json 加 timeout_override=clamp_timeout_to_turn_budget(reserve_for_reply=True) 与 lane_key；_rerank_candidates 在 len(candidates)<=query.top_k 时直接返回；query_rewrite 池不可用时跳过而非等满 8s
- **回归风险**: 低——降级路径已有（异常即回退），只是把无界等待变有界
- **建议验证**: 构造 think3 消息实测 memory.injection stage 耗时 <10s 且 turn 不再 stale_drop；单测断言 _call_deep_json 传入了 timeout_override
- **适合立即开发**: 是 | **执行轮次**: R3
- **关联发现**: ML-01, ML-05

### RT-03 [P1/VERIFIED] mood LLM 串行前置于 judge 且与 judge 内嵌 mood 双重计算：364 次调用中约 302 次花在最终不回复的消息上，构成群聊 ingress p50 4.4s 延迟

- **用户可感知后果**: 每条过滤后的消息（含最终被忽略的）都先付一次 3-40s 的 mood LLM；群聊在 attention.dispatch 内联执行（p50 4.4s / max 40s 即 mood 延迟），私聊在 settle 后串行，直接推高首响延迟；成本上 364 次 mood vs 67 次回复，而 judge JSON 输出本就含 mood_tag/mood_delta 并已调用 atomic_update_mood——同一文本两次情绪计算。
- **根因**: gate._apply_primary_mood_update 在 ingress/worker 内 await state_engine.update_mood（LLM，mood_analysis_timeout_sec=30 默认），判定前无条件执行（仅 micro-utterance 跳过）；judge.py 又在判决结果里输出并应用 mood_delta（primary_mood_applied 只把 judge 的 delta 缩放 0.25，不省调用）。
- **证据**: `astrmai/conversation/attention/gate.py:L1079-L1080` — `if not defer_private_context and not defer_direct_vision_context:     await self._apply_primary_mood_update(event, chat_id, msg_str)`
- **证据**: `astrmai/state/chat_state_service.py:L378-L379` — `# Phase 2: LLM analysis (no lock — parallel-safe, no blocking)  tag, new_value = await self._resolve_mood_analysis(chat_id, text, snapshot_mood)`
- **证据**: `astrmai/conversation/decision/judge.py:L459-L462, L520-L532` — `"mood_tag": "happy/sad/angry/neutral/curious/surprise", "mood_delta": 0.0 ... effective_mood_delta *= self.PRIMARY_MOOD_MICROADJUST_SCALE`
- **运行时佐证**: trace: mood pool 364 次（executed 轮仅 62 次）；stage attention.dispatch p50 4.4s/p95 14.6s/max 40s；c7d6148cbf8b mood 39.7s 后该消息 skipped_ignore；私聊时间线 mood 3.5-8.4s 全部位于 judge 之前。
- **合并说明**: 吸收 PL-12（mood 580 次/16h = 全部 LLM 调用 57% 的量化佐证）。
- **测试缺口**: 无"同一 event 至多一次情绪 LLM"与"低价值消息跳过 mood"的行为测试。
- **最小修复边界**: gate._apply_primary_mood_update / decision_router.evaluate：将独立 mood LLM 调用改为 (a) 复用 judge 返回的 mood_tag/mood_delta，或 (b) 与 judge 并行 gather，或 (c) think_level/判定后置——最少改动是把 mood 调用移到 judge_action∈{REPLY,TOOL_CALL} 之后。
- **回归风险**: 中：情绪衰减/关键词反应依赖 mood 值的时序，需要确认 WAIT/IGNORE 消息不更新情绪是否可接受（judge delta 仍在）。
- **建议验证**: 回放 trace 统计 mood 池调用数应降至≈executed 轮数；attention.dispatch p50 应降到 <1s。
- **适合立即开发**: 是 | **执行轮次**: R3
- **关联发现**: RT-02, RT-06

### RT-05 [P1/VERIFIED] 视觉链路重试乘法（框架5×网关3×池7模型）+ executor 旁路无超时 + 合并循环重置屏障 deadline：单图可烧掉整个 360s 轮预算

- **用户可感知后果**: 用户发一张（provider 侧 502 的）图片等了 7 分钟才收到回复；期间 43 次框架级 Gemini 重试、~8 次网关模型尝试共 302.9s，随后同轮 mood/cognitive/memory 全部因预算耗尽失败。
- **根因**: 三层重试相乘：AstrBot request_retry(5) 在每次网关 attempt 内部、gateway llm_retries=2(→3 attempts)/模型、vision 池 7 模型 + coordinator 每图 retries=2。4da2910 的屏障策略（per-call wait_for + 180s 总额）只覆盖 coordinator 路径，且 gate 合并循环每次迭代 prepare_batch 重新起算 180s；executor.py 视觉旁路完全不传 timeout_override 也无总额。
- **证据**: `astrmai/conversation/execution/executor.py:L682-L688` — `result_dict = await self.gateway.call_vision_task(     image_data=temp_file_path,     prompt=VISION_USER_PROMPT,     system_prompt=VISION_SYSTEM_PROMPT,     lan`
- **证据**: `astrmai/conversation/attention/private_turn_coordinator.py:L401-L402` — `started_at = time.monotonic() deadline = deadline or (started_at + self._vision_total_timeout())`
- **证据**: `astrmai/conversation/attention/gate.py:L1310-L1315` — `vision_outcome = await self.private_turn_coordinator.prepare_batch(batch_events, chat_id) async with session.lock:     if session.accumulation_pool:         ses`
- **运行时佐证**: turn 7edddd6eb3d7 三条 vision ledger 109.1s/71.0s(cancelled)/122.8s（各 2-3 attempts 25-47s）+6 条 deadline 秒败；[Gemini] request_retry 43 条全部落在 15:25-15:29 同窗；budget exhausted=1 即此轮。事件发生于 c4aee57，4da2910 已部分修复但上述三缺口在 HEAD 仍在。
- **相关测试**: `tests/test_gateway_vision_refactor.py`, `tests/unit/conversation/test_private_turn_coordinator.py`
- **测试缺口**: 无合并循环多迭代累计视觉耗时测试；无 executor 旁路超时测试；无框架重试与网关重试叠加的上限测试。
- **最小修复边界**: ①executor._analyze_direct_images 传 timeout_override 并套用 vision_barrier_total 类似的总额；②coordinator 屏障 deadline 存到 session/事件级别，合并迭代不重置；③call_vision_task 对同池顺序尝试加轮预算 clamp（vision 目前是唯一绕过 _filter 后逐模型全遍历的任务）。
- **回归风险**: 中：过紧的总额会让慢 provider 下图片全部失败，需保留 timeout_fallback 策略语义。
- **建议验证**: 模拟 502 provider 重放图片消息，断言总视觉耗时≤配置总额且轮长≤budget。
- **适合立即开发**: 是 | **执行轮次**: R3
- **关联发现**: RT-04, RT-01

### TL-04 [P1/LIKELY] gateway 层 side-effect 中止保护被 executor 模型级联绕过：space_transition 可能向好友重复真发私聊，失败尝试排队的动作随 fallback 提交

- **用户可感知后果**: 工具已真实执行副作用（发私聊/戳/表情）后本轮失败时，换模型整轮重跑可导致好友收到两条措辞不同的转告私聊；或用户收到兜底文本『（陷入了短暂的沉默...）』的同时机器人仍戳人/发表情，动作与文本语义脱节。
- **根因**: gateway_lane 在 side_effect_recorded 时 break 级联（正确），但 executor._run_tool_mode 的 except 不识别该语义照常换下一模型重跑全新对话；space_transition 去重键为 (target_id, 精确文本)，跨模型措辞不同必失配；pending_actions 在失败尝试后仍留在共享 _extras，最终随任意成功发送（含 fatal fallback）被 qq_action_dispatcher.commit 提交。
- **证据**: `astrmai/infrastructure/gateway/gateway_lane.py:L995-L1000` — `if side_effect_recorded:     abort_after_side_effect = True     logger.error(f"[Gateway] tool_loop failure after recorded side effect; retry disabled for {model`
- **证据**: `astrmai/conversation/execution/executor.py:L1065-L1082` — `except Exception as exc:     last_error = str(exc)     ...     logger.warning(f"[{chat_id}] tool model {provider_id} failed, trying next: {exc}")     continue`
- **证据**: `astrmai/conversation/planning/tools/pfc_tools.py:L2736-L2742` — `if any(     str(item.get("target_id") or "") == target_id     and str(item.get("message") or "") == outbound_message     for item in sends ...):     return f"消息`
- **运行时佐证**: 16h 窗口 space_transition 未发生（lifecycle 0 条）故无实例；但同构触发条件（工具轮失败→换模型）日志 3 次（executor:1081）。另 _tool_side_effect_count 把纯查询工具也计入（gateway_lane L187-193），一次查询后失败即禁 gateway 级重试，把级联压力全部推给 executor 层。
- **相关测试**: `tests/test_executor_refactor.py`
- **测试缺口**: 缺『第一模型执行副作用工具后抛错，第二模型不得重放副作用』的级联测试；缺 fallback 发送时 pending_actions 是否应清空的契约测试。
- **最小修复边界**: executor._run_tool_mode except 分支：检查 _tool_side_effect_count(event)（或 LLMCascadeFailureException 增加 side_effect 标志）> 进入循环前基线时停止级联、改走 _handle_required_tool_missing/诚实降级；_handle_fatal_fallback 前清空或标记 pending_actions。
- **回归风险**: 中：过度收紧会让副作用后的可恢复失败（如输出格式）直接放弃回复；建议仅禁重跑、保留基于已得工具结果的文本重写路径。
- **建议验证**: 单测模拟：ToolSet 内工具 call 时向 event 写 cross_session_sends 后 gateway 抛 LLMCascadeFailureException，断言第二模型未被调用或未再触发 send_message。
- **适合立即开发**: 是 | **执行轮次**: R3
- **关联发现**: TL-05

### PL-03 [P1/VERIFIED] UI '合并私聊连续输入' 开关是死键：timing.turn_merge_enabled 被 pydantic 静默丢弃，无法关闭合并

- **用户可感知后果**: 用户在配置页关闭'合并私聊连续输入'无任何效果（UI 显示关、行为开），私聊输入聚合行为不可配置。
- **根因**: schema 把开关放在 timing 分节（L1095），但 TimingConfig 无 turn_merge_enabled 字段且 LEGACY_TIMING_NAMESPACE_FIELDS 未收录该键 → pydantic extra=ignore 丢弃；业务读 private_chat.turn_merge_enabled 恒为默认 True。
- **证据**: `_conf_schema.json:L1095-L1100` — `"turn_merge_enabled": {"description": "合并私聊连续输入", "type": "bool", "default": true, ...}`
- **证据**: `config.py:L17-L27` — `LEGACY_TIMING_NAMESPACE_FIELDS = (     ("model_request_timeout_sec", "infra", "api_timeout"),     ...  # 无 turn_merge_enabled`
- **证据**: `astrmai/conversation/attention/private_turn_coordinator.py:L129-L130` — `def turn_merge_enabled(self) -> bool:     return bool(getattr(self._private_config(), "turn_merge_enabled", True))`
- **运行时佐证**: 本地实测 AstrMaiConfig(**{'timing': {'turn_merge_enabled': False}}) 后 cfg.private_chat.turn_merge_enabled == True。
- **相关测试**: `tests/test_config_standalone_refactor.py`
- **测试缺口**: 缺 'schema 每个叶子键都被 pydantic 接受且映射到有效字段' 的参数化一致性测试。
- **最小修复边界**: config.py LEGACY_TIMING_NAMESPACE_FIELDS 增加 ('turn_merge_enabled','private_chat','turn_merge_enabled') 并在 TimingConfig 增加该字段（bool, default True）。
- **回归风险**: 低——单字段映射补齐。
- **建议验证**: AstrMaiConfig(**{'timing':{'turn_merge_enabled':False}}).private_chat.turn_merge_enabled 应为 False。
- **适合立即开发**: 是 | **执行轮次**: R4

### PL-04 [P1/VERIFIED] '启用基础内容安全过滤（NSFW/自残/PII 检测）' 是虚假开关：全仓库不存在任何实现

- **用户可感知后果**: 运营者开启后以为有内容安全兜底，实际所有输出不经任何检测直接外发——安全预期落空，比普通死配置更危险。
- **根因**: reply.enable_content_safety_filter 在 schema（L433）与 config.py（L179）都存在，但 astrmai/ + main.py 无任何消费点，也不存在 NSFW/self-harm/PII 检测代码（output_guard 只识别 provider 失败文本）。
- **证据**: `config.py:L179` — `enable_content_safety_filter: bool = Field(default=False, description="启用基础内容安全过滤（NSFW/自残/PII 检测）")`
- **证据**: `_conf_schema.json:L433-L438` — `"enable_content_safety_filter": {..."default": false...}`
- **运行时佐证**: grep -riE 'nsfw|self.?harm|content_safety' astrmai/ 仅命中 context_compaction 的压缩安全分析（无关），无过滤实现。
- **测试缺口**: 缺 '每个 bool 功能开关至少有一个行为差异测试' 的守卫。
- **最小修复边界**: 二选一：在 reply_service/output_guard 实现最小过滤并接开关；或从 schema+config.py 删除该键并在变更说明标注。
- **回归风险**: 低（删除路径）/ 中（实现路径需误杀率评估）。
- **建议验证**: 开启开关后发送含测试敏感词的回复，断言被拦截或改写。
- **适合立即开发**: 是 | **执行轮次**: R4
- **关联发现**: PL-05

### TG-01 [P1/VERIFIED] 群聊身份隔离无端到端回归：speaker block、关系数据、终线 guard 三个身份来源各自单测，无一测试断言三者指向同一 sender

- **用户可感知后果**: 两个用户交替发言时，bot 可能用 A 的称呼/关系语气回复 B（称呼串号）。该 bug 类历史上真实发生过（专门为此写了 GroupActorConsistencyGuard），但今天没有任何测试守护完整链路：guard 只能修复'外人名+11种后缀称呼'，裸名直呼与关系数据串号完全无测试，回归后无人发现。
- **根因**: 身份取值分散且无一致性契约：speaker block 用 focus_context.focus_sender_id（planner_prompt_context.py:155-156），关系/画像用 event.get_sender_id()（planner.py:1224 → planner_side_inputs.py:891-897），gate fast-wakeup 路径直接派发原始 event（gate.py:823）。现有测试全部单组件、单 sender 或手工捏造 speaker block。
- **证据**: `astrmai/conversation/planning/planner.py:L1224` — `user_id = event.get_sender_id()`
- **证据**: `astrmai/conversation/planning/planner_prompt_context.py:L155-156` — `sender_id = str(getattr(focus_context, "focus_sender_id", "") or "").strip() or cls._safe_event_sender_id(focus_event)`
- **证据**: `astrmai/conversation/planning/planner_side_inputs.py:L891-897` — `profile = await self.state_engine.get_user_profile(str(user_id)) ... relationship_vec = self.state_engine.relationship_engine.get_or_create(str(user_id))`
- **证据**: `astrmai/conversation/execution/group_actor_consistency.py:L148-154` — `for suffix in _ADDRESS_SUFFIXES: tokens.append(f"{name}{suffix}")`
- **相关测试**: `tests/test_executor_refactor.py::test_finalize_reply_repairs_foreign_group_member_direct_address`, `tests/original_ported/test_prompt_refiner_focus_layout_ported.py::test_current_speaker_boundary_precedes_focus_message`, `tests/original_ported/test_planner_prompt_context_guards_ported.py::test_current_speaker_block_marks_group_weak_input_boundary`, `tests/test_member_action_intent.py`
- **测试缺口**: 缺'双 sender 交替发言 → gate 选 focus → planner 组 prompt'的端到端测试，断言 prompt 中 speaker block 的 QQ == side_inputs 加载画像的 user_id == 被回复者；缺裸名直呼场景的 guard 行为锚定。
- **最小修复边界**: 新增 tests/regression/conversation/test_group_identity_isolation_e2e.py（gate._debounce_and_judge + planner._prepare_plan_context + executor._finalize_reply 三段拼装，stub judge/gateway）
- **回归风险**: 高：gate.py/planner.py 最近 5 个提交全部在改，任何 focus 选择或事件派发重构都可能悄悄破坏身份一致性
- **建议验证**: python -m pytest tests/test_executor_refactor.py -k actor -q（现状）；新测试落地后跑该文件
- **适合立即开发**: 是 | **执行轮次**: R5

### TG-03 [P1/VERIFIED] Turn 总预算端到端零守护：配置接线、网关耗尽分支、judge 耗尽降级三个执法点均无测试，接线失败会静默让预算失效

- **用户可感知后果**: 预算体系是 20bb585 引入的核心延迟护栏（防止单轮 120s+ 的调用把用户晾着）。message_entry._configure_turn_budget 把异常整体吞掉：若 config.timing 字段改名或结构变化，预算从此不再配置，clamp 全部变 no-op（remaining 返回 None 直接放行），所有慢调用回到无上限状态，且测试全绿。生产 remaining_ms p05=0 说明预算确实被顶到耗尽边界。
- **根因**: turn_total_budget_sec 在 tests/ 中 0 次出现；gateway_call.py:288-289 的 turn_deadline_exhausted 抛出分支、decision_router.py:123-128 的 judge_budget_exhausted→PASS 降级分支均无测试；唯一 budget 测试只验证 clamp 纯函数。
- **证据**: `astrmai/presentation/events/message_entry.py:L145-156` — `def _configure_turn_budget(facade, event): try: ... configure_turn_budget(event, total_budget_sec=float(getattr(timing, "turn_total_budget_sec", 360.0) or 360.0`
- **证据**: `astrmai/infrastructure/gateway/gateway_call.py:L283-289` — `effective_timeout = clamp_timeout_to_turn_budget(...); if effective_timeout <= 0.0: raise asyncio.TimeoutError("turn_deadline_exhausted")`
- **证据**: `astrmai/conversation/attention/decision_router.py:L123-128` — `if judge_timeout <= 0.0: ... return AttentionDecision(action="PASS", raw_action="PASS", reason="judge_budget_exhausted")`
- **运行时佐证**: ledger_analysis: budget exhausted 1 次；remaining_ms p05 = 0；gateway.chat max 122.8s；样本私聊 turn 55.1s
- **相关测试**: `tests/test_turn_call_ledger_refactor.py::test_turn_budget_clamps_noncritical_timeout_and_keeps_reply_reserve`, `tests/original_ported/test_reply_freshness_budget_ported.py`
- **测试缺口**: 缺三条：① message_entry 层断言事件进入后 snapshot['budget'].total_budget_sec 来自 config.timing；② 慢模型（可控 sleep）下 dialog 调用实际超时被 clamp 到剩余预算且 exhausted 标记正确；③ judge 在预算=0 时走 judge_budget_exhausted 降级。
- **最小修复边界**: 新增 tests/test_turn_budget_e2e_refactor.py（message_entry._configure_turn_budget + gateway_call + decision_router 三点）
- **回归风险**: 高：budget 相关代码在 20bb585/c4aee57 连续两提交改动，仍是活跃开发区
- **建议验证**: python -m pytest tests/test_turn_call_ledger_refactor.py -q
- **适合立即开发**: 是 | **执行轮次**: R5
- **关联发现**: 与 runtime/观测域的 budget 相关发现可能互补

### ML-05 [P1/VERIFIED] think 门 + 窄关键词门使记忆注入率仅 2.9%（私聊 0/19）：正常聊天读不到已写入的记忆

- **用户可感知后果**: 执行轮 72% 为 think0 直接跳过记忆，think1 需说出『记得/之前/上次/回忆/想起』才检索；用户问『我叫什么名字』『我喜欢吃什么』得不到已存事实，私聊 19 条回复 0 注入。写入端持续膨胀（摘要/挖掘照跑烧 LLM），读取端近乎关闭——记忆系统投入产出严重失衡。
- **根因**: prompt_refiner 前置门：think<=0 → policy=none；think==1 且未命中 MEMORY_INTENT_KEYWORDS（10 个词）→ 跳过。私聊常规轮被判 cooldown_simple_turn(think0)/direct_normal_turn(think1)，identity/preference 提问不含触发词。
- **证据**: `astrmai/conversation/planning/prompt_refiner.py:L678-L684` — `if think_level is not None and think_level <= 0:     decision.policy = "none"     decision.skip_reason = "think_level_0"     return decision, "" if think_level `
- **证据**: `astrmai/memory/services/memory_injection_service.py:L22-L33` — `MEMORY_INTENT_KEYWORDS = {"记得","刚才","之前","上次","回忆","想起","remember","last time","earlier","before"}`
- **运行时佐证**: 585 traces: injected 4（执行轮 2/69=2.9%）；skip: think_level_0×52、think_level_1_no_memory_intent×13；私聊 1481314186 十四条回复全跳过；旧快照 9/92=9.8% 同构。memory_funnel 5/585 属前置门设计（funnel 只在 build_bundle 执行时写），非采集缺陷。
- **相关测试**: `tests/unit/memory/test_memory_gap_coverage.py`
- **测试缺口**: 缺『identity/preference 类问句在 think1 下应触发检索』的策略测试
- **最小修复边界**: prompt_refiner think1 门放宽：复用 MemoryQueryBuilder.QueryIntentClassifier，identity/preference/location 意图也放行；可选为 think0 私聊提供 FTS-only 轻量注入
- **回归风险**: 中——放宽后注入次数上升，需监控 token 成本与 near_context 冲突
- **建议验证**: 回放私聊『我叫什么名字』应产生 memory.injected=true；统计一周 trace 注入率回升到目标区间
- **适合立即开发**: 是 | **执行轮次**: R7
- **关联发现**: ML-01, ML-02

### ID-03 [P2/VERIFIED] Peer poke（群友互戳）虚拟事件 100% 被过滤，整套 peer 互动剧本为死代码

- **用户可感知后果**: 群友之间互戳时 bot 永远无反应，PokePlaybook 的围观/加入互动人设完全不可见（16h 30 次互戳全被丢弃）；戳 bot 本身的回戳正常。
- **根因**: process_poke_event 仅在 target_is_bot 时设置 is_virtual_poke=True；peer poke 的叙事文本只写入 message_str，message_obj 中只有 Poke 组件（不计入 has_payload）→ should_process_message 步骤 4 拒绝。
- **证据**: `astrmai/conversation/ingress/sensors.py:L643-L646` — `event.message_str = virtual_text event.set_extra("is_virtual_poke", target_is_bot) event.set_extra("astrmai_interaction_kind", "poke" if target_is_bot else "pee`
- **证据**: `astrmai/conversation/ingress/sensors.py:L317-L318` — `if not clean_text and not has_payload:     return False`
- **证据**: `astrmai/conversation/planning/planner_prompt_context.py:L525-L529` — `elif interaction_kind == "peer_poke":  # 不可达`
- **运行时佐证**: traces: 30/30 含 '戳了 X 一下，这是群友之间的轻互动' 的 turn 全部 skipped_sensor_filter；log '捕获互动事件' 31 条中 30 条 peer；bot 目标 poke 1 例成功回戳（15:22:53 已回戳反击）。
- **相关测试**: `tests/original_ported/test_attention_interaction_narrative_ported.py`
- **测试缺口**: 缺 peer_poke 事件通过 process_event→sensor 的放行测试。
- **最小修复边界**: sensors.py::should_process_message 对 astrmai_interaction_kind 非空事件放行（频控继续交给 playbook 的 peer_join_allowed + judge）。
- **回归风险**: 低——放行后 judge 仍可 IGNORE，最多增加少量 judge 调用。
- **建议验证**: 模拟 B 戳 C 的 OneBot notice，断言 trace 进入 judge 而非 sensor_filter。
- **适合立即开发**: 是 | **执行轮次**: R1
- **关联发现**: ID-02, ID-10

### ID-06 [P2/VERIFIED] Proactive 事件的'当前发言人归因锁'指向幽灵用户 astrmai_proactive_candidate（被 ID-02 掩盖的二级缺陷）

- **用户可感知后果**: 一旦 ID-02 修复，每条主动消息的 prompt 都会强制'第二人称必须指向 主动开口候选（QQ astrmai_proactive_candidate）'——bot 主动发言会对着不存在的人说'你'，或把内部占位名说出口。
- **根因**: _build_current_speaker_block 与 _render_final_speaker_lock 不区分 proactive 合成事件，直接采用合成 sender（sender_id=astrmai_proactive_candidate, sender_name=主动开口候选）。
- **证据**: `astrmai/conversation/planning/planner_prompt_context.py:L148-L180, L463-L467` — `lines = ["本轮正在回应的对象只看这一位：", f"- QQ: {sender_id or 'unknown'}", f"- 昵称: {sender_name or '群友'}", ...]`
- **证据**: `astrmai/conversation/planning/prompt_refiner.py:L153-L158` — `"---最终发言人归因锁---" f"当前唯一对话对象是 {display_name}（QQ {display_id}）。" "回复中的第二人称、昵称和关系称呼必须指向这一位；"`
- **证据**: `astrmai/proactive/dispatcher.py:L330-L331` — `"sender_id": "astrmai_proactive_candidate", "sender_name": "主动开口候选",`
- **运行时佐证**: 当前无运行时样本（ID-02 使事件到不了 planner）；代码链闭环可证。
- **测试缺口**: 缺 proactive 事件 prompt 不含 speaker block/归因锁的断言。
- **最小修复边界**: planner_prompt_context._build_current_speaker_block 对 astrmai_is_proactive_event 返回空串（归因锁随之消失）；必须与 ID-02 同批修复。
- **回归风险**: 低。
- **建议验证**: 修复 ID-02 后触发主动开口，检查 prompt/trace 无 '主动开口候选' 字样且回复不含无端第二人称。
- **适合立即开发**: 是 | **执行轮次**: R1
- **关联发现**: ID-02

### TL-05 [P2/VERIFIED] is_stale_reply_reason 漏配 superseded_by_newer_activity_same/_unknown_thread 变体，过期回复被误判为模型失败并触发换模型重试

- **用户可感知后果**: 运营看到误导性 WARN『tool model failed, trying next』误判模型故障；salvage 窗口内可能换模型完整重生成一轮（双倍 dialog 成本、回复更迟）；执行状态需靠下一次 pre-model freshness 检查兜底才落 stale_drop。
- **根因**: chat_runtime_coordinator 产出 reason 前缀为 superseded_by_newer_activity_same_thread:/…_unknown_thread:，而 is_stale_reply_reason 只 startswith 'superseded_by_newer_activity:'（reply_freshness L209 自家格式）；同文件 L229 又用不带冒号的宽前缀——同一 reason 三处两套判据。
- **证据**: `astrmai/conversation/execution/reply_freshness.py:L50-L58` — `return str(reason or "").startswith((     "reply_age_exceeded:",     "superseded_by_newer_activity:",     "stale_",     "expired", ))`
- **证据**: `astrmai/infrastructure/runtime/chat_runtime_coordinator.py:L469-L477` — `reason_kind = (     "superseded_by_newer_activity_same_thread"     if same_thread     else "superseded_by_newer_activity_unknown_thread" ) ... return FreshnessS`
- **证据**: `astrmai/conversation/execution/executor.py:L798-L804` — `if is_stale_reply_reason(blocked_reason):     event.set_extra("astrmai_execution_status", "stale_drop") ... else:     event.set_extra("astrmai_execution_status"`
- **运行时佐证**: astrbot_since_c4aee57.log L3017/L3074/L5331：三次 executor:1081 WARN，error 均为 superseded_by_newer_activity_unknown_thread:<actor>:…；随后 executor:762 pre-model 检查以同 reason 落 stale_drop。
- **相关测试**: `tests/original_ported/test_reply_freshness_budget_ported.py`
- **测试缺口**: 缺 producer(chat_runtime_coordinator reason 格式) 与 consumer(is_stale_reply_reason) 的契约测试。
- **最小修复边界**: reply_freshness.is_stale_reply_reason：把前缀改为 'superseded_by_newer_activity'（去冒号）即可同时覆盖三种格式。
- **回归风险**: 低：仅放宽字符串匹配，语义方向一致。
- **建议验证**: 单测 is_stale_reply_reason('superseded_by_newer_activity_unknown_thread:a:5.0s') is True；回放日志确认不再出现该 reason 的 model failed WARN。
- **适合立即开发**: 是 | **执行轮次**: R1
- **关联发现**: TL-04

### WU-08 [P2/VERIFIED] /learning/cooldowns 永远返回空对象：读取的属性名 _recent_patterns 从未存在（真实为 _recent_pattern_keys）

- **用户可感知后果**: "表达冷却"诊断面板永远显示空 {}，且 runtime_bound=true 让它看起来是"当前没有冷却"——排查表达重复/冷却问题时这个观测口提供的是假信息。
- **根因**: AdminUiService/LearningService.expression_cooldowns 都取 _as_dict(selector).get("_recent_patterns", {})，而 ExpressionSelector 的属性自初始提交就叫 _recent_pattern_keys（git log -S 证实 _recent_patterns 从未存在）。
- **证据**: `astrmai/webui/backend/services/admin_ui_service.py:L1054-L1063` — `"recent_patterns": self._as_dict(selector).get("_recent_patterns", {}) if selector else {},`
- **证据**: `astrmai/conversation/planning/expression_policy.py:L383` — `self._recent_pattern_keys: dict[str, List[tuple[str, str]]] = {}`
- **测试缺口**: 缺"selector 有冷却记录时端点返回非空"的断言。
- **最小修复边界**: admin_ui_service.py::expression_cooldowns 与 learningservice.py::expression_cooldowns——读 _recent_pattern_keys（tuple key 需序列化为字符串）。
- **回归风险**: 低。
- **建议验证**: 对 selector._recent_pattern_keys 注入一条记录后 GET /learning/cooldowns，断言 recent_patterns 非空。
- **适合立即开发**: 是 | **执行轮次**: R1
- **关联发现**: WU-09

### ML-10 [P2/VERIFIED] 会话摘要主路径（pipeline buffer）说话人解析失败——参与者全部 unknown，群记忆无法归属到人

- **用户可感知后果**: 定时摘要产生的群记忆 speaker_ids/participants 为空或 unknown，『查某人说过什么』只能靠正文碰巧带名字的 FTS 模糊匹配；结构化的 sender_id 列在检索层也不支持过滤，人物归属链路整体缺失。
- **根因**: record_turn 写 buffer 行格式为『用户/旁白：{sender}: {text}』（memory_turn_pipeline.py:137），而 _build_topic_messages 只解析『[time] sender: content』格式（session_memory_summarizer.py:364），不匹配即 sender=unknown；v2_store.search 亦无 sender_id 过滤参数。
- **证据**: `astrmai/memory/services/memory_turn_pipeline.py:L136-L139` — `if turn.user_text:     session_data["buffer"].append(f"用户/旁白：{turn.user_text}") if turn.assistant_text:     session_data["buffer"].append(f"Bot：{turn.assistant_`
- **证据**: `astrmai/memory/services/session_memory_summarizer.py:L364-L370` — `match = re.match(r"^\[(?P<time>[^\]]+)\]\s*(?P<sender>[^:]+):\s*(?P<content>.*)$", line) ...else:     sender = "unknown"`
- **证据**: `astrmai/memory/services/v2_store.py:L1032-L1040` — `elif session_id:     where.append("(session_id = ? OR session_id = '')") ...if persona_id: ...  # 无 sender_id 过滤分支`
- **运行时佐证**: 代码路径闭环；DB 佐证：SELECT COUNT(*) FROM canonical_memories WHERE source='memory_summary' AND (metadata NOT LIKE '%speaker_ids%' OR metadata LIKE '%"speaker_ids": []%');
- **相关测试**: `tests/unit/memory/test_memory_v2_services.py`
- **测试缺口**: 缺 _build_topic_messages 对 pipeline buffer 行格式的解析测试
- **最小修复边界**: memory_turn_pipeline.record_turn 存结构化 dict（sender_id/sender_name/text）替代拼接字符串；或 _build_topic_messages 增加『用户/旁白：sender: text』解析分支
- **回归风险**: 低
- **建议验证**: 单测：record_turn 两轮后 run_maintenance，断言 topic_messages sender 非 unknown 且摘要 metadata.speaker_ids 非空
- **适合立即开发**: 是 | **执行轮次**: R2
- **关联发现**: ML-05

### WU-05 [P2/VERIFIED] 表达审核通过/驳回不同步召回索引投影（jargon/canonical 路径都同步，唯独 expression 缺失）

- **用户可感知后果**: 审批通过的表达在向量召回路径缺位直至重启（结合 WU-04 运行期无修复）；每次审批使记忆质量面板 missing/inactive projection 计数增长，操作者看到"索引异常"却无从解释；驳回已通过表达后向量索引残留 inactive 投影（检索期被 canonical 水合过滤兜住，不泄露，但一致性面板持续报警）。
- **根因**: ExpressionPatternService.update_review 经 store.update_memory 改 status/visibility（只同步 canonical_fts），从不调用 index_projector.project/cleanup_deleted；对照 MemoryUiService.update_jargon L1185-1189 与 update_canonical L339-346 均有投影同步。
- **证据**: `astrmai/memory/services/expression_pattern_service.py:L369-L380` — `updated = await self.store.update_memory(str(pattern_id), content=expression, summary=summary, metadata=metadata, status=status, visibility=visibility,)        `
- **证据**: `astrmai/webui/backend/services/memory_ui_service.py:L1185-L1189` — `if changed and projector:     if next_status == "active":         await projector.project(str(jargon_id))     else:         await projector.cleanup_deleted([str`
- **证据**: `astrmai/memory/services/memory_index_projector.py:L136-L170` — `report["missing_projection_ids"] = sorted(projectable_ids - projected_ids)`
- **相关测试**: `tests/test_webui_backend_refactor.py::test_runtime_jargon_delete_cleans_projection`
- **测试缺口**: 缺"expression approve 后 check_consistency 无 missing"断言（jargon 有对应测试，expression 没有）。
- **最小修复边界**: expression_pattern_service.py::update_review / ReviewUiService 层——审批状态变化后按 jargon 同款调用 projector.project/cleanup_deleted（service 需可访问 projector，或在 ReviewUiService 完成）。
- **回归风险**: 低——project 幂等（先 delete 再 add）。
- **建议验证**: 审批一条表达后调用 GET /memories/diagnostics/index，missing_projection_ids 不应包含该 id。
- **适合立即开发**: 是 | **执行轮次**: R2
- **关联发现**: WU-04

### WU-07 [P2/VERIFIED] 黑话"驳回并删除"硬删除抹掉 rejected 墓碑，挖掘器会把同一噪声词重新捞回待审队列

- **用户可感知后果**: 运营者驳回一条噪声黑话后，下一轮挖掘同群语料会再次提出同一词（静态噪声规则未覆盖的 LLM 提取项），重新出现在黑话待审——人工清理变成打地鼠，且 UI 只承诺"不可恢复"没提示"会回流"。
- **根因**: reject_jargon → delete_jargon → v2_store.hard_delete 物理删除 canonical 行+dedup 别名+FTS+legacy 行；而 JargonMiner.mine 的 existing_terms 去重集合恰恰依赖 statuses 含 "rejected" 的行作为墓碑（jargon_miner.py L57-63）。维护路径本给 rejected 保留 7 天 grace 再 purge（墓碑语义被认可），UI 硬删跳过了它。
- **证据**: `astrmai/webui/backend/services/memory_ui_service.py:L1319-L1322` — `async def reject_jargon(self, jargon_id: str, data: dict | None = None) -> dict[str, object]:         result = await self.delete_jargon(str(jargon_id))`
- **证据**: `astrmai/memory/services/v2_store.py:L1561-L1569` — `await db.execute("DELETE FROM canonical_fts WHERE memory_id = ?", (clean_id,))                 await db.execute("DELETE FROM memory_dedup_aliases WHERE canonica`
- **证据**: `astrmai/learning/mining/jargon_miner.py:L57-L63` — `rows = await self.memory_engine.v2_store.list_candidates(session_id=group_id, kinds=["jargon"], statuses=["active", "review_pending", "rejected", "stale"], limi`
- **相关测试**: `tests/test_webui_backend_refactor.py::test_jargon_cleanup_preview_and_apply_physically_delete_selected_items`
- **测试缺口**: 缺"驳回后同词不再进入候选"的挖掘端到端测试。
- **最小修复边界**: memory_ui_service.py::reject_jargon——改为置 status=rejected（软墓碑，交给维护 purge 7 天 grace）而非直接 hard_delete；或 hard_delete 时把词写入独立 rejected_terms 表供 miner 去重。
- **回归风险**: 中——需同步调整噪声预检"物理删除"文案与 jargon_cleanup 语义；rejected 墓碑本身依赖 WU-04 的 purge 恢复调度后才会过期。
- **建议验证**: 驳回一条黑话后对同群跑 run_expression_backfill/挖掘，断言 existing_terms 仍包含该词、候选不重现。
- **适合立即开发**: 是 | **执行轮次**: R2
- **关联发现**: WU-04

### WU-10 [P2/VERIFIED] 黑话/表达关键字搜索只过滤当前页：服务端 query 过滤发生在 LIMIT/OFFSET 之后，total 用未过滤总数

- **用户可感知后果**: 搜索一个确实存在的词，若命中不在当前页则显示"当前分类暂无数据"，同时分页器显示"共 N 条"(未过滤总数)并允许翻页——搜索功能对多页数据基本不可用。
- **根因**: memory_ui_service.list_jargon 先分页查询再 _filter_jargon_rows(query)，total 取未过滤 count；前端表达全量 tab 的关键字过滤只作用于已取回的 25 条（app.js L1520-1521），无服务端 keyword 参数传递（/reviews 支持 keyword 但前端不传）。
- **证据**: `astrmai/webui/backend/services/memory_ui_service.py:L657-L668` — `result = await store.list_canonical(kind="jargon", status=status, session_id=group_id, limit=page_limit, offset=page_offset,)             items = [self._canonic`
- **证据**: `pages/admin/app.js:L1520-L1521` — `const keyword = String(state.cache.reviews.filters.keyword || "").trim().toLowerCase();   const activeItems = asItems(activePage).filter((item) => !keyword || j`
- **测试缺口**: 缺"跨页搜索命中"测试（第 2 页存在匹配时第 1 页搜索应能召回）。
- **最小修复边界**: memory_ui_service.py::list_jargon——query 下推为 SQL LIKE/FTS（过滤后再分页并返回过滤后 total）；app.js loadReviews 表达 tab 把 keyword 作为 /reviews?keyword= 传给后端（后端已实现）。
- **回归风险**: 低。
- **建议验证**: 造 30+ 条黑话使匹配项落在第 2 页，搜索后第 1 页应显示命中且 total=匹配数。
- **适合立即开发**: 是 | **执行轮次**: R2

### ID-09 [P2/VERIFIED] 私聊回复中位延迟 44s：settle→mood→judge→cognitive→tools 五段串行，且私聊 judge 16h 内 0 次非 REPLY 纯属延迟

- **用户可感知后果**: 私聊用户发一句'呜呜呜'要等 ~52s 才收到回复（executed 私聊 reply_age p50=44.3s / max=357.4s）；主控疑点①的 14s 空档=合并等待窗+mood LLM 串行+排队，9s 空档=cognitive_loop LLM，均非卡死缺陷但用户可感知。
- **根因**: 私聊 worker 内联串行：wait_for_input_stability(1.5s+) → _apply_primary_mood_update(mood LLM 3-8s，await) → judge LLM(5-17s) → cognitive chat_dialog(8-15s) → context_build → chat_tools(5-9s) → send；judge 在私聊场景 16h 内 18/18 turn 全部 REPLY/确认，turn_merge+settle 已承担'等他说完'职能，judge 为冗余关键路径。
- **证据**: `astrmai/conversation/attention/gate.py:L1293-L1296, L1354-L1359, L1443-L1449` — `await self.private_turn_coordinator.wait_for_input_stability(session) ... await self._apply_primary_mood_update(mood_event, chat_id, "\n".join(mood_texts)) ... `
- **证据**: `astrmai/conversation/attention/private_turn_coordinator.py:L220-L229` — `async def wait_for_input_stability(self, session):     delay = self.settle_seconds()  # 默认 1.5s`
- **运行时佐证**: 时间线重建（created_at=完成时刻）：'呜呜呜' turn 总 64.4s：t0 dispatch → +11.0s mood(3.5s) → +14.5s judge(13.5s) → +28.7s cognitive(15.0s) → +45.2s chat_tools(6.3s) → +51.5s send，尾部 ~12s 发送后记账；18 个私聊 turn 含 14 次 judge LLM，0 次 IGNORE/WAIT；全局 mood 364 次 / judge 539 次调用。
- **相关测试**: `tests/conversation/test_private_turn_coordinator.py`, `tests/original_ported/test_attention_private_chat_ported.py`
- **测试缺口**: 缺私聊端到端延迟预算断言（如 dispatch→send 关键路径 LLM 次数上限）。
- **最小修复边界**: gate._debounce_and_judge 私聊分支：mood 更新改 fire-and-forget 或与 judge 并行；私聊默认 should_skip_judge=True（保留可配置开关）；发送后记账移出 turn 关键路径统计。
- **回归风险**: 中——跳过私聊 judge 需确认 judge 在私聊无其他职责（样本内无）；mood 并行需确认 judge prompt 不依赖本轮 mood 结果。
- **建议验证**: 对比修改前后私聊 trace reply_age p50；断言私聊 executed turn 关键路径 LLM 调用数从 4-5 降至 2-3。
- **适合立即开发**: 是 | **执行轮次**: R3
- **关联发现**: ID-01

### ML-06 [P2/VERIFIED] 发送后内联 claim 抽取 LLM 在 turn 任务内同步执行（实测 5.2~44.5s×7），拖长 turn 与 per-chat 后续处理

- **用户可感知后果**: 回复虽已发出，但 turn 任务多挂 5~44s：同 chat 的下一条消息处理被顺延，turn_total 观测虚高，bg/memory 模型配额被占。
- **根因**: reply_service.py:198 await _ingest_memory_turn → process_instant_gate 在 gate 命中 relationship/major_event/contact/explicit_cmd 且规则无 claim 时内联调用 MemoryClaimExtractor.extract 的 LLM 路径。
- **证据**: `astrmai/conversation/execution/reply_service.py:L198-L203` — `await self._ingest_memory_turn(     event,     chat_id,     formatted_user_text,     artifact.persistable_text, )`
- **证据**: `astrmai/memory/services/memory_claim_service.py:L146-L153` — `response = await self.gateway.call_data_process_task(     prompt=envelope.prompt, ..., lane_key=LaneKey(subsystem="bg", task_family="memory", ...)`
- **运行时佐证**: trace ledger 7 次 family=memory_global_summary stage=gateway.chat（5263/8726/10783/11188/34392/44457ms + 1 error 6.3ms），全部出现在 executed turn 的 llm_call_ledger 中。
- **相关测试**: `tests/integration/test_message_to_reply_pipeline.py`
- **测试缺口**: 缺『post-send 记忆摄入不阻塞 turn 完成』的时延断言
- **最小修复边界**: reply_post_send._ingest_memory_turn 的 instant-gate LLM 部分改投递到 pipeline 后台 worker（依赖 ML-01 先修，否则后台必死）
- **回归风险**: 低——写入本就是异步语义，仅时序后移
- **建议验证**: 修复后 trace 中 executed turn 的 ledger 不再出现 memory_global_summary 长调用，turn_total 相应下降
- **适合立即开发**: 是 | **执行轮次**: R3
- **关联发现**: ML-01

### RT-04 [P2/VERIFIED] gateway.tool（dialog 主回复/工具环）完全不受 turn 预算约束，与 gateway.chat 的预算语义不一致

- **用户可感知后果**: 预算耗尽的轮次里主回复仍会继续生成并发送（turn 7edddd 预算 0 后 dialog 8.3s 成功、总轮长 420s 才回复）——预算体系无法保证"轮长上限"承诺；反向看这也是该轮唯一发出去回复的原因，说明当前预算语义未经设计确认。
- **根因**: tool_chat_in_lane_result 与 _tool_chat_in_lane_result_unlimited 全程无 clamp_timeout_to_turn_budget，per-attempt 超时为 _tool_loop_total_timeout=max(api_timeout, tool_timeout)，模型×重试循环无预算检查。
- **证据**: `astrmai/infrastructure/gateway/gateway_lane.py:L182-L185` — `def _tool_loop_total_timeout(self, tool_timeout: float, max_steps: int) -> float:     """Bound the whole agent run without multiplying the budget by logical ste`
- **证据**: `astrmai/infrastructure/gateway/gateway_lane.py:L729-L744` — `response = await asyncio.wait_for(     self.context.tool_loop_agent(...),     timeout=self._tool_loop_total_timeout(timeout, max_steps), )`
- **运行时佐证**: turn 7edddd6eb3d7: budget.remaining_ms=0 exhausted=true 后 gateway.tool[dialog] status=success elapsed 8305ms，reply_sent=true，总轮长 420459ms。
- **相关测试**: `tests/test_gateway_context_passthrough_refactor.py`
- **测试缺口**: 无 "预算耗尽时 tool 环行为" 的测试（应明确：主回复保留额 90s 内允许、超出则截断/降级）。
- **最小修复边界**: gateway_lane.tool_chat_in_lane_result：attempt 循环前 clamp（reserve_for_reply=False，因为它本身就是 reply），预算<主回复保留额时用保留额兜底而非直接失败。
- **回归风险**: 中：clamp 过严会把长工具链误杀；需与 main_reply_reserve_sec 语义联动。
- **建议验证**: 构造 deadline 已过的 event 调 tool_chat_in_lane_result，断言 timeout 被 clamp 至保留额。
- **适合立即开发**: 是 | **执行轮次**: R3
- **关联发现**: RT-01, RT-05

### RT-06 [P2/VERIFIED] cognitive_loop 在默认 think_level=1 上仍串行运行（8-35s LLM），think 分级未覆盖此高频成本

- **用户可感知后果**: 82% 的消息落在 think_level=1；其中过判定的轮次在 judge 之后、planner 之前再付一次 8-35s 的意图分类 LLM（多返回 reply/comfort 这类平凡结论），是私聊"judge 结束→context_build 9-10s 空档"的真身（且常被记到邻轮 ledger 制造观测假象）。
- **根因**: cognitive_loop.gate_decision 对 think_level>=1 即放行（L193-194），与 memory/goal/slang/jargon 的 >=2 门槛不一致；soft timeout 配置默认 2.5s 但服务器放宽后无预算联动。
- **证据**: `astrmai/conversation/planning/cognitive_loop.py:L192-L194` — `readonly_allowed = think_level is not None and think_level >= 3 if think_level is not None and think_level >= 1:     return CognitiveLoopGateDecision(True, "", `
- **证据**: `astrmai/conversation/planning/cognitive_loop.py:L222-L225` — `return await asyncio.wait_for(     self._decide_inner(event=event, prompt_envelope=prompt_envelope),     timeout=self._soft_timeout_seconds(), )`
- **运行时佐证**: trace: think_level 分布 0:89/1:481/2:3/3:12；cognitive_loop 池 22 次 p50≈15s max 34.5s；turn 99ebb0c5e1ce think_level=1 且 cognitive_loop_ran=true，其 10s LLM 被记到邻轮，表现为 judge→context_build 空档；CognitiveLoop timeout 日志 0 条（服务器已放宽超时）。
- **相关测试**: `tests/test_planner_cognitive_loop_refactor.py`
- **测试缺口**: 缺 think_level=1 平凡消息跳过/并行 cognitive_loop 的行为测试。
- **最小修复边界**: cognitive_loop.gate_decision 门槛提为 >=2（或 level 1 仅在含复杂度信号时放行）；或在 chat_loop_kernel 将其与 context_build 并行 gather。
- **回归风险**: 中：cognitive_loop 输出影响 memory_policy/intent，跳过后需确认 planner 默认流足够。
- **建议验证**: 回放 trace 比较 executed 轮 turn_total_elapsed_ms p50 与 cognitive_loop 池调用数变化。
- **适合立即开发**: 是 | **执行轮次**: R3
- **关联发现**: RT-01, RT-03

### RT-09 [P2/VERIFIED] judge prompt 缓存敌对结构未修：539 次调用 × p50 1977 字符动态段内嵌 1.4K 固定 rubric，前缀命中 0-25%

- **用户可感知后果**: judge 是调用量最大的池（539 次/16h，累计 1.07M prompt 字符），几乎全价计费且拉高 judge 延迟；对照 dialog 池 87.7% cached input，judge 每字符成本明显更高。
- **根因**: judge.py evaluate 的 f-string 把固定的动作表、人格维度 key、JSON schema、mood 标签说明放在动态 history/mood 之后的 user prompt 里，system 仅 222 字符 JUDGE_STABLE_PREFIX；任何历史变动都使全 prompt 前缀失效。
- **证据**: `astrmai/conversation/decision/judge.py:L419-L463` — `prompt = f""" 你是群聊中的这个角色的潜意识大脑...{history_context} 【近期发生的连续对话...】: {message} 【思考与决策流】...可选的人格维度 Key: ...请严格按照以下 JSON 格式输出...`
- **运行时佐证**: trace: judge 池 539 次 system_chars 恒 222、prompt_chars p50 1977/max 3148；7-25 分析实测 judge 缓存命中 0-25% 且列为 P1，未变。
- **测试缺口**: 缺 judge prompt 结构快照测试（固定段应全部位于 system/stable prefix）。
- **最小修复边界**: judge.py + judge_prompt.py：把动作说明/维度 key/JSON schema/mood 说明并入 JUDGE_STABLE_PREFIX（system），动态段只留 mood 数值、历史与消息且置于最尾。
- **回归风险**: 低-中：prompt 重排可能轻微影响判决分布，建议 A/B 比较 judge_outcomes 比例。
- **建议验证**: 重排后统计 judge 池 usage_input_cached/usage_input_tokens 应显著>0。
- **适合立即开发**: 是 | **执行轮次**: R3
- **关联发现**: RT-02

### RT-11 [P2/LIKELY] 全局 LLM 信号量(3) 把 ambient judge/mood 与主回复混排，skipped 轮 judge 条目出现 30-51.7s 排队

- **用户可感知后果**: 高峰期被忽略消息的 judge/mood 占满 3 个并发槽，真实回复的 dialog/planner 调用排队，用户可感知首响变慢（gateway.chat p50 7.4s vs judge 单 attempt 5s 上下；skipped 轮 judge ledger elapsed 高至 51.7s 而 attempt 仅数秒）。
- **根因**: _elastic_call_result 与 tool_chat_in_lane_result 共用 self._global_semaphore（max_concurrent_llm_calls 默认 3）；begin_llm_call 在 acquire 之前，故 ledger elapsed 含排队时间（也证明了排队存在）；无关键路径优先级。
- **证据**: `astrmai/infrastructure/gateway/gateway_call.py:L193-L194` — `) -> LLMCallResult:         async with self._global_semaphore:`
- **证据**: `astrmai/infrastructure/gateway/model_gateway.py:L38` — `self._global_semaphore = asyncio.Semaphore(max(1, int(self.settings.max_concurrent_llm_calls)))`
- **运行时佐证**: skipped 轮 judge 条目 elapsed 51.7/32.7/30.3/30.2s（attempts 1 且 attempt 用时远小于 elapsed）；7edddd judge elapsed 18.3s vs attempt 14.6s。缺每槽等待直方图故列 LIKELY。
- **测试缺口**: 缺并发压力下关键路径 vs 后台调用的排队指标与优先级测试。
- **最小修复边界**: model_gateway：拆分信号量（critical_path 独立配额或优先队列），并在 ledger metadata 记录 semaphore_wait_ms 以便定量。
- **回归风险**: 中：并发上限本是 429 保护，拆分需保持总并发不超 provider 限额。
- **建议验证**: 加 semaphore_wait_ms 后统计 executed 轮 dialog 的等待应≈0。
- **适合立即开发**: 是 | **执行轮次**: R3
- **关联发现**: RT-03, RT-02

### PL-02 [P2/VERIFIED] 主动链三层诊断全误标：日志称 'skipped by planner'、dispatcher status=skipped 无原因、trace proactive 字段恒空

- **用户可感知后果**: 运营者从日志/trace/WebUI intents 历史三个入口都看不出主动消息死在传感器层，误以为是 planner 判断'不合适'，PL-01 因此长期无人发现。
- **根因**: wakeup_service._on_complete 对 reply_sent=False 一律打 'skipped by planner'；trace 的 proactive.* 只在 planner._apply_proactive_context 填充，pre-planner 终结（sensor_filter/throttle/repeater）路径不写入 → is_proactive 恒 False。
- **证据**: `astrmai/proactive/wakeup_service.py:L181-L184` — `if not reply_sent:     logger.info(f"[Life] proactive wakeup skipped by planner: {...}")`
- **证据**: `astrmai/conversation/planning/planner.py:L1100-L1106` — `def _apply_proactive_context(self, event) -> None:     if not bool(event.get_extra("astrmai_is_proactive_event", False)):         return     ...     proactive.i`
- **运行时佐证**: 日志 14 条 'skipped by planner'（实际 0 次进 planner）；585 traces 中 proactive.is_proactive 全部 False，包括 14 条合成事件 turn。
- **测试缺口**: 缺 pre-planner 终结路径的 trace proactive 字段断言。
- **最小修复边界**: gate._finalize_pre_planner_turn 内补 proactive 上下文填充（与 planner._apply_proactive_context 同源化）；wakeup 日志按 blocked_reason 分流措辞。
- **回归风险**: 低——纯观测层。
- **建议验证**: 注入合成事件后检查 trace.proactive.is_proactive=True 且 blocked_reason=sensor_filtered。
- **适合立即开发**: 是 | **执行轮次**: R4
- **关联发现**: PL-01

### PL-05 [P2/VERIFIED] 另 7 个死配置键：debounce_window/max_message_length/repeater_threshold/throttle_probability/throttle_min_entropy/enable_relationship_engine/unknown_decay

- **用户可感知后果**: 用户调整群聊防抖窗口、复读阈值、限流概率、关系引擎开关、情绪平滑参数均无效果；UI 展示的行为承诺与实际不符（如复读阈值实际硬编码为 3 条且不可调）。
- **根因**: 功能重构后配置键未同步清理：防抖硬编码分档（0.25/0.45/0.70s）、限流改为能量驱动、RelationshipEngine 无条件实例化、mood 只用 decay_interval/decay_rate。
- **证据**: `astrmai/conversation/attention/window_buffer.py:L17-L24` — `if is_strong_wakeup:     return 0.10 gap = ... if gap < 1.0:     return 0.25 if gap < 5.0:     return 0.45 return 0.70`
- **证据**: `astrmai/conversation/attention/gate.py:L926-L929` — `if getattr(session, "last_message_hash", "") == msg_hash and msg_str.strip():     session.repeat_count = ... + 1     if session.repeat_count >= 2:         retur`
- **证据**: `astrmai/state/chat_state_service.py:L271` — `self.relationship_engine = RelationshipEngine(config=self.config)`
- **运行时佐证**: 全仓库（排除 venv/__pycache__）对 7 键零命中（脚本三级模式匹配 + 人工复核别名）。
- **测试缺口**: 缺 schema 键消费点存在性的静态守卫测试。
- **最小修复边界**: 从 _conf_schema.json + config.py 移除 7 键，或将 repeater/debounce/throttle 逻辑改回读配置（gate.py/window_buffer.py/energy_manager.py 函数级）。
- **回归风险**: 低（删除）——注意 repeater_threshold 若接回配置需保持默认行为 3 条。
- **建议验证**: config_consumption_matrix.md ① 清单复跑脚本应为 0 项。
- **适合立即开发**: 是 | **执行轮次**: R4
- **关联发现**: PL-04

### PL-06 [P2/VERIFIED] 越界配置=插件整体拒载：AstrMaiConfig 校验异常未捕获，且约 90 个数值键 UI 无范围约束提示

- **用户可感知后果**: 用户把 meme_probability 填成 101、bg_pool_size 填 0 或负超时后保存 → 下次加载 ValidationError → 整个 AstrMai 插件下线（所有会话失去响应），错误信息只在框架日志里；无裁剪+告警的降级路径。
- **根因**: main.py __init__ 中 AstrMaiConfig(**raw_config) 无 try/except；schema 仅 25 键声明 minimum/maximum（timing 20 + private_chat.topic_* 4 + min_memory_confidence），其余约 90 个数值键 pydantic 有 ge/le 但 UI 侧无约束。插件自有 WebUI 的 apply_config 反而会优雅拒绝——两条路径行为不一致。
- **证据**: `main.py:L62-L65` — `def __init__(self, context: Context, config: dict | None = None):     super().__init__(context)     self.raw_config = config or {}     self.config = AstrMaiConf`
- **证据**: `astrmai/webui/backend/adapters/plugin_api.py:L458-L471` — `try:     parsed_config = AstrMaiConfig(**config_data) except Exception as exc:     ... return {"status": "error", ...}`
- **运行时佐证**: 本地实测：api_timeout=-5 / bg_pool_size=0 / turn_total_budget_sec=999999 / meme_probability='abc' 全部 ValidationError。
- **相关测试**: `tests/test_config_standalone_refactor.py`
- **测试缺口**: 缺 '坏配置应降级加载而非拒载' 的合同测试。
- **最小修复边界**: main.py __init__：捕获 ValidationError，剔除违例字段回退默认并 logger.error 逐项汇总；长期为 schema 补齐 min/max。
- **回归风险**: 中——降级加载需保证剔除字段后的组合仍自洽（timing 别名连带校验）。
- **建议验证**: 注入 {'infra':{'api_timeout':-5}} 实例化插件，断言插件可用且日志含降级警告。
- **适合立即开发**: 是 | **执行轮次**: R4
- **关联发现**: PL-03

### PL-09 [P2/VERIFIED] 插件重载即短期上下文失忆：GroupDialogueStore/压缩链纯内存，AstrBot 侧任何配置保存都清零并掐掉在飞 turn

- **用户可感知后果**: 每次在 AstrBot 面板改配置（触发插件重载）后，bot 丢失所有群的热区/温区对话与压缩摘要链（表现为突然接不上话），正在生成的回复被取消不发送。
- **根因**: GroupDialogueStore 无持久化（纯 dict + asyncio.Lock）；terminate() → runtime_coordinator.shutdown cancel 在飞 turn + _states.clear()；私聊 pending 有持久化先例（_persist_pending_sessions）但群对话存储没有。
- **证据**: `astrmai/conversation/attention/group_dialogue_store.py:L53-L59` — `def __init__(self, *, hot_zone_ttl_seconds..., warm_zone_max_tokens: int = 1200):     ...     self._threads: dict[str, DialogueThread] = {}     self._lock = asy`
- **证据**: `astrmai/infrastructure/runtime/chat_runtime_coordinator.py:L401-L418` — `async def shutdown(self) -> int:     ... tasks = [...active_turn_tasks...]     self._states.clear()     for task in tasks:         task.cancel()`
- **测试缺口**: 缺重载前后对话上下文连续性的集成测试。
- **最小修复边界**: terminate 时将 dialogue_store 热/温区快照写入 cache 目录、启动时按 TTL 恢复（对齐 dream_scheduler_state.json 的做法）；或文档明示重载副作用。
- **回归风险**: 中——恢复需处理 TTL 过期与 schema 演进。
- **建议验证**: 改配置触发重载后，对同群提问上一分钟话题，bot 应能接上。
- **适合立即开发**: 是 | **执行轮次**: R4

### RT-08 [P2/VERIFIED] provider 能力解析全量失败（provider=unknown 1005/1005）：cache_control/provider session 特性被静默关闭，观测字段失真

- **用户可感知后果**: 所有 GatewayUsage/trace 的 provider 字段为 unknown（主控疑点⑤），按 provider 家族分析成本不可能；supports_cache_control/supports_remote_session 恒 False，显式缓存 hint 与远程会话复用永不启用——当前 87.7% dialog 缓存全靠 provider 隐式缓存，其余池无 hint 可用。
- **根因**: resolve_provider_capabilities 先尝试 context.get_provider_by_id（该 AstrBot 接口缺失或异常返回 None），随后把完整模型 ID（如 'code2/deepseek-v4-flash'）当 provider type 匹配已知家族表，必然落入 unknown 分支。
- **证据**: `astrmai/infrastructure/gateway/provider_capabilities.py:L107-L121` — `for accessor_name in ("get_provider_by_id", "get_provider"):     accessor = getattr(context, accessor_name, None) ... if provider is not None:     return infer_`
- **证据**: `astrmai/infrastructure/gateway/provider_capabilities.py:L54-L59` — `return ProviderCapabilities(     provider_family="unknown",     supports_native_prompt_cache=False,     supports_remote_session=False,     supports_cache_contro`
- **运行时佐证**: trace provider 字段: unknown 1005 / 空 17；日志 GatewayUsage 全部 provider=unknown（含 gemini/deepseek 池）；trace continuity request_provider_family 全 unknown。
- **测试缺口**: 缺对真实 AstrBot Context 接口名的集成断言（get_provider_by_id 存在性）。
- **最小修复边界**: provider_capabilities.resolve_provider_capabilities：字符串回退前按 '/' 拆 provider 段并查 context.get_all_providers()（或 provider_manager）按 id 前缀匹配 provider 对象/type。
- **回归风险**: 低-中：家族识别变化会改变 cache_control/session 行为，需灰度观察 429。
- **建议验证**: 启动后 GatewayUsage 行 provider 字段出现 native_chat/gemini 等真实家族。
- **适合立即开发**: 是 | **执行轮次**: R4
- **关联发现**: RT-10

### ID-05 [P2/VERIFIED] stage_ledger reply.send 的 sent_segment_count 恒为 0（满发路径从不写 metadata）——确认为 instrumentation bug，无真实丢段

- **用户可感知后果**: 运营者看 stage_ledger 会误判'回复 0 段发出'与 reply_stats 矛盾，浪费排查时间；掩盖真正 partial_sent 的可观测性（真 partial 与满发在 stage 里无法区分）。
- **根因**: _send_segments 只在异常部分发送或 freshness 截断分支写 artifact.metadata['sent_segment_count']；全量成功路径不写。reply_service stage 元数据默认 0，reply_stats 默认 len(segments)，两处默认值不一致。
- **证据**: `astrmai/conversation/execution/reply_artifact_builder.py:L544, L601-L604, L634-L637` — `sent_segment_count = 0 ... except: artifact.metadata["sent_segment_count"] = sent_segment_count ... if artifact.sent and sent_segment_count < len(artifact.segme`
- **证据**: `astrmai/conversation/execution/reply_service.py:L153, L181` — `send_stage["sent_segment_count"] = int(artifact.metadata.get("sent_segment_count", 0) or 0) ... sent_segment_count=int(artifact.metadata.get("sent_segment_count`
- **运行时佐证**: 67/67 executed turn：stage_meta=0、reply_stats=planned_segment_count、send_status=sent、sent=True——全部满发，主控疑点②闭环。
- **相关测试**: `tests/test_reply_service_refactor.py`
- **测试缺口**: 缺满发路径 stage metadata 与 reply_stats 一致性断言。
- **最小修复边界**: reply_artifact_builder._send_segments 发送循环结束后无条件写 artifact.metadata['sent_segment_count']=sent_segment_count。
- **回归风险**: 低。
- **建议验证**: 发一条多段回复，断言 stage_ledger reply.send metadata.sent_segment_count == reply_stats.sent_segment_count == 段数。
- **适合立即开发**: 是 | **执行轮次**: R5
- **关联发现**: ID-01

### RT-02 [P2/VERIFIED] analyze_turn_ledger.py judge 口径错误（按 stage 匹配），judge_calls_per_turn=0 掩盖了仍存在的同轮多次 judge（真实 p50=1/p95=2/max=10）

- **用户可感知后果**: 运营者依据 ledger_analysis 认为 7-25 的"judge 重复调用"已修复，实际 40/585 轮 judge>1，极端轮 150 秒内同一 focus 消息被判 10 次（f487f997baa2），judge 池 539 调用中 521 次花在最终被忽略/等待的消息上。
- **根因**: judge 经 chat_in_lane_result 记账为 stage='gateway.chat', pool='judge'；分析脚本用 stage 含 'judge' 匹配（永假）。planner._count_judge_calls 用 pool 判断（正确），同一提交 20bb585 两处口径不一致。附带机制问题：gate._debounce_and_judge 把 IGNORE 的 focus event 放回 window，下一批焦点选择可再次选中同一事件重复判决。
- **证据**: `scripts/analyze_turn_ledger.py:L160-L162` — `if stage == "attention.judge" or "judge" in stage:     judge_count += 1`
- **证据**: `astrmai/infrastructure/gateway/gateway_lane.py:L399-L404` — `call_id = begin_llm_call(     event,     stage="gateway.chat",     family=workload_policy.family.value,     pool=lane_key.task_family,`
- **证据**: `astrmai/conversation/attention/gate.py:L1509-L1510` — `elif judge_action == "IGNORE":     ...     self._append_attention_window(session, [focus_event])`
- **运行时佐证**: 本审计脚本重算：judge/turn p50=1 p95=2 max=10，40 turns>1；样本 f487f997baa2 十次 judge 各 4.6-20.3s 全 success 最终 ignore；ledger_analysis.json 报 p50/p95=0 而 judge_outcomes=420。
- **相关测试**: `tests/test_analyze_turn_ledger_refactor.py`
- **测试缺口**: 分析脚本测试未喂 pool='judge'+stage='gateway.chat' 条目；缺同 focus 连续 IGNORE 的再判决短路测试。
- **最小修复边界**: analyze_turn_ledger.analyze_traces 判定改为 pool=='judge' or stage=='attention.judge'；gate.py 焦点选择对连续 IGNORE 的 focus 加冷却/降权。
- **回归风险**: 低（脚本口径）；焦点冷却为中（影响群聊唤醒灵敏度）。
- **建议验证**: python scripts/analyze_turn_ledger.py <traces> 输出 judge_calls_per_turn_p50>=1。
- **适合立即开发**: 是 | **执行轮次**: R5
- **关联发现**: RT-03

### TG-04 [P2/VERIFIED] trace v2 memory_funnel 在 executed turns 中 64/67 缺失（prompt_refiner 7 条 early-return 不写 funnel），且无字段完整性契约测试；context_block_stats 的 511/585 缺失系误报

- **用户可感知后果**: 运营者用 analyze_turn_ledger/WebUI 排查'记忆为什么没注入'时，96% 的 executed turn 没有 memory_funnel，无法区分'合理跳过'与'仪表坏了'；c4aee57 宣称 complete turn trace observability，但无测试锚定 executed trace 必含哪些字段，本次缺失就是这样溜进生产的。附带澄清：context_block_stats 在 executed 内 67/67 全量存在，缺失的 511 条全是 skipped 状态，属预期，非回归。
- **根因**: astrmai_memory_funnel 只在 MemoryInjectionService.build_bundle 内部的 remember_funnel 写入；prompt_refiner._resolve_memory_injection 的 7 条 early-return（lightweight/near_context_priority/empty_query/think_level_0/think_level_1_no_intent/disable_rag|fast_mode/service_unavailable）在调 build_bundle 前 return，只写 decision.skip_reason。executed 里 memory.policy none=50/light=17，其中大多命中 early-return。
- **证据**: `astrmai/conversation/planning/prompt_refiner.py:L646-697` — `if event.get_extra("astrmai_lightweight_event", False): decision.skip_reason = "lightweight_event"; return decision, ""  # 共 7 条 early-return，均不写 funnel`
- **证据**: `astrmai/memory/services/memory_injection_service.py:L182-188` — `def remember_funnel(payload): ... event.set_extra("astrmai_memory_funnel", dict(payload))  # 仅 build_bundle 内部调用`
- **证据**: `scripts/analyze_turn_ledger.py:L196,L223` — `memory = _safe_dict(trace.get("memory_funnel")) ... missing["memory_funnel"] += 1`
- **运行时佐证**: 585 recent traces: memory_funnel 缺 580/585；executed 67 条中仅 3 条存在；executed memory.policy none=50/light=17；context_block_stats executed 67/67 全有（脚本 scratchpad/trace_fields_out.txt）
- **相关测试**: `tests/test_turn_trace_store_v2_refactor.py`, `tests/test_planner_cognitive_loop_refactor.py`, `tests/test_analyze_turn_ledger_refactor.py::test_load_traces_prefers_v2_recent_and_filters_instrumentation`
- **测试缺口**: 缺 executed trace 字段完整性契约测试（llm_call_ledger/stage_ledger/context_block_stats/memory_funnel 非空）；缺'early-return 跳过路径也产出 skipped funnel'的行为测试。
- **最小修复边界**: prompt_refiner.py::_resolve_memory_injection 各 early-return 前写 skipped funnel（或由 planner 在无 funnel 时补 skip 占位）+ tests/test_turn_trace_store_v2_refactor.py 加契约测试
- **回归风险**: 低：仅补观测字段，不影响行为
- **建议验证**: PYTHONIOENCODING=utf-8 python scripts/analyze_turn_ledger.py 后看 missing.memory_funnel 应≈skipped 数量而非全量
- **适合立即开发**: 是 | **执行轮次**: R5
- **关联发现**: 主控疑点②（reply.send sent_segment_count 矛盾）与④（judge attempts=0）同属 trace 完整性，归观测域；本条与其应在最终报告合并为 trace 契约主题

### TG-05 [P2/VERIFIED] 记忆闭环缺'修订'腿：WebUI update_canonical 修订内容→projector 重投影→检索/注入反映新内容 无任何测试（WebUI 测试全部 projector=None 或 mock store）

- **用户可感知后果**: 用户在 WebUI 修订/删除一条记忆后，若 update→project 的 wiring 回归（projector 获取路径变化、project 幂等短路返回旧文档等），bot 会继续引用旧记忆内容，而现有三层测试（WebUI mock、store 单测、写检注集成）全部绿灯——运营者以为改掉的错误事实仍被复述。
- **根因**: memory_ui_service.update_canonical（L295-352）修订后调 projector.project/cleanup_deleted，但 test_webui_backend_refactor 的 runtime-bound 测试把 index_projector 置 None 且 store 为 mock；integration/test_memory_write_retrieve_inject.py 只有 write→retrieve→inject 三腿；v2_services 的 update→search 测试是 store 直查（jargon status 翻转），不经 WebUI 层也不验证 auto 注入。
- **证据**: `astrmai/webui/backend/services/memory_ui_service.py:L338-346` — `changed = await store.update_memory(memory_id, **updates); projector = self.plugin_api.get_index_projector() ... projected = bool(await projector.project(memory`
- **证据**: `tests/test_webui_backend_refactor.py:L524-527` — `class _Engine: maintenance_service = _Maintenance(); v2_store = _Store(); index_projector = None`
- **相关测试**: `tests/integration/test_memory_write_retrieve_inject.py`, `tests/unit/memory/test_memory_v2_services.py::test_review_pending_jargon_is_hidden_from_default_retrieval_until_approved`, `tests/test_webui_backend_refactor.py::test_memory_ui_service_runtime_bound_canonical_actions_use_services_not_sql_fallback`
- **测试缺口**: 缺一条真实闭环：write→retrieve 命中旧内容→真实 MemoryUiService.update_canonical(content 修订)+真实 projector→retrieve 新内容命中且旧内容不再 top1→injection.build_bundle 渲染新内容。
- **最小修复边界**: tests/integration/test_memory_write_retrieve_inject.py 追加 1 条修订闭环测试（真实 store+projector+retrieval+MemoryUiService）
- **回归风险**: 低：纯加测试
- **建议验证**: python -m pytest tests/integration/test_memory_write_retrieve_inject.py -q
- **适合立即开发**: 是 | **执行轮次**: R5
- **关联发现**: memory 域代理可能报 projector/检索一致性代码问题，本条为其测试守护面

### TG-06 [P2/VERIFIED] WebUI 前后端契约无自动对齐校验：前端 app.js 75 个 api 路径 vs 后端注册表，测试只有手工镜像清单+JS 字符串 pin，历史已有 ≥4 例 FE/BE 漂移 bug

- **用户可感知后果**: 任一端改路径/字段，另一端 404 或渲染空数据，管理页局部报废且测试全绿——这不是假想：final-functional-audit/12 人工抓到双层 .data 解包、persona cache 路径不一致、review 字段名 expression vs text、legacy list 遗漏 canonical 共 4+ 例，全部属于'对齐靠人眼'导致。
- **根因**: test_native_admin_api_registers_core_routes（L18-88）用手工维护的路径清单对照后端注册表，前端不在校验环内；round11 用 assertIn 具体 JS 片段（test_round11_runtime_contracts.py:236-245）防既往 bug，重构即腐化；真正直调 route handler 的测试仅 1 条。
- **证据**: `pages/admin/app.js:L350-358` — `const api = { async get(path) { const { endpoint, params } = pluginEndpoint(path); return unwrapResponse(await ensureBridge().apiGet(endpoint, params)); } ... }`
- **证据**: `tests/test_plugin_pages_admin_refactor.py:L29-33` — `paths = {path for path, _, _, _ in registered}; self.assertIn(f"{PLUGIN_API_PREFIX}/dashboard", paths)  # 手工镜像清单`
- **相关测试**: `tests/test_plugin_pages_admin_refactor.py::test_native_admin_api_registers_core_routes`, `tests/unit/webui/test_round11_runtime_contracts.py`, `tests/test_webui_backend_refactor.py`
- **测试缺口**: 缺静态对齐测试：解析 app.js 全部 api.get/post 路径（模板参数归一为 {param}）→ 注册 register_astrmai_admin_pages 收集后端集合（{x}/<x> 双格式归一）→ 断言前端路径 ⊆ 后端注册表。
- **最小修复边界**: tests/test_plugin_pages_admin_refactor.py 新增 1 条 <50 行的静态对齐测试
- **回归风险**: 低：纯加测试；注意模板字符串路径的归一化规则
- **建议验证**: python -m pytest tests/test_plugin_pages_admin_refactor.py -q
- **适合立即开发**: 是 | **执行轮次**: R5
- **关联发现**: 与 webui 域代理的具体 FE/BE 漂移发现互补：本条是缺守护，该域可能报新漂移实例

### TG-07 [P2/VERIFIED] 4da2910 私聊 vision barrier 的 gate 消费侧组合分支无测试：屏障期间新消息 re-merge 续跑、abort 后池非空续跑、resolve 超时 outcome

- **用户可感知后果**: 用户发图后紧跟补充文字（最常见的私聊连续输入形态）恰好落在图片屏障窗口内时，走 gate.py:1311-1315 的 re-merge 或 1331-1336 的 abort 后续跑分支——回归会表现为补充消息被丢弃、或 require_analysis 失败通知后新消息永远不被处理。coordinator 本身 17 条测试覆盖良好，但这三条 gate 侧组合分支（4da2910 新增/重排）零测试。
- **根因**: 并发交织场景（prepare_batch 执行中 accumulation_pool 回填）需要事件同步器才能稳定构造，现有 gate 测试的 prepare_batch stub 都是瞬时返回，永远不会命中回填分支；resolve 超时需要慢 resolver，现有测试 resolver 全部即时返回。
- **证据**: `astrmai/conversation/attention/gate.py:L1311-1315` — `vision_outcome = await self.private_turn_coordinator.prepare_batch(batch_events, chat_id); async with session.lock: if session.accumulation_pool: session.accumu`
- **证据**: `astrmai/conversation/attention/gate.py:L1331-1336` — `async with session.lock: if session.accumulation_pool: current_is_strong_wakeup = False; continue; session.is_evaluating = False; return`
- **证据**: `astrmai/conversation/attention/private_turn_coordinator.py:L403-418` — `except asyncio.TimeoutError: return self._apply_failed_policy(event, ... outcome="resolve_timeout")`
- **运行时佐证**: traces: skipped_wait 83 + stale_drop 7 表明输入交织高频；vision fallback WARN 2 条
- **相关测试**: `tests/unit/conversation/test_private_turn_coordinator.py`, `tests/test_attention_gate_refactor.py::test_private_required_vision_failure_stops_before_mood_judge_and_system2`, `tests/test_attention_gate_refactor.py::test_debounce_worker_drain_loop_keeps_late_arrivals`
- **测试缺口**: 缺三条：① 慢 prepare_batch（await Event 控制）期间注入新消息→断言第二轮批次包含旧+新且不重复发送；② abort 分支后池非空→断言失败通知只发一次且新消息继续处理；③ 慢 resolver → outcome=resolve_timeout 且 downstream_action 符合 policy。
- **最小修复边界**: tests/test_attention_gate_refactor.py + tests/unit/conversation/test_private_turn_coordinator.py 各加 1-2 条
- **回归风险**: 中：该区域 4da2910 刚大改（gate +69 行、coordinator +411 行），下个提交极可能继续动
- **建议验证**: python -m pytest tests/unit/conversation/test_private_turn_coordinator.py tests/test_attention_gate_refactor.py -q
- **适合立即开发**: 是 | **执行轮次**: R5
- **关联发现**: conversation 域代理若报该区域代码缺陷则互为印证

### WU-06 [P2/VERIFIED] TurnTrace 样本库每条消息全文件读改写（15MB 实测，封顶约 42MB），与 WebUI 45s 轮询读共用一把锁

- **用户可感知后果**: 每条入站消息（含被忽略的 317/585）在消息处理路径上 await 一次"读整文件+解析+indent=2 序列化+整文件写"（本机实测 parse 0.21s + dumps 0.46s @15MB，服务器更慢）；文件按 max_global=2000+by_chat 双份增长到 ~42MB 时逼近每消息 2s 级；WebUI cognition 页每 45s 轮询 /cognition/recent-turns 再整文件解析一次，与聊天写入在同一 asyncio.Lock 上互相排队——dashboard 变慢与消息延迟互相放大。
- **根因**: TurnTraceSampleStore.append/recent 均为整文件 JSON 读写（turn_trace_store.py L88-133），gate._finalize_pre_planner_turn 对每个事件 await 该回调；raw_trace_store 同模式再写一次。样本在 by_chat 与 recent 双份存储且 indent=2 膨胀。
- **证据**: `astrmai/infrastructure/runtime/turn_trace_store.py:L94-L115` — `async with self._lock:     payload = await asyncio.to_thread(self._read_sync) ... await asyncio.to_thread(self._write_sync, payload)`
- **证据**: `astrmai/conversation/attention/gate.py:L981-L992` — `result = callback(str(chat_id ...), event, status=status, reply_text=reply_text)             if inspect.isawaitable(result):                 await result`
- **证据**: `astrmai/conversation/planning/planner.py:L895-L897` — `if self.turn_trace_store is not None and hasattr(self.turn_trace_store, "append"):             try:                 await self.turn_trace_store.append(item)`
- **运行时佐证**: 服务器文件 .agent/runtime-observability-c4aee57-20260726/turn_trace_samples_server.json：15.0MB，recent=585 + by_chat=392，平均 15KB/条；本机 parse=0.21s、dumps=0.46s；[TurnLedger] 日志证实 skipped_ignore turn 也全部落盘。
- **测试缺口**: 缺 append 复杂度/文件体积护栏测试；缺"skip 类 turn 是否需要全量落盘"的策略测试。
- **最小修复边界**: infrastructure/runtime/turn_trace_store.py——改 append-only JSONL 分片或 SQLite 表；短期缓解：去掉 indent、去掉 recent/by_chat 双份、skip 类 turn 存精简摘要、append 改后台队列。
- **回归风险**: 中——WebUI recent() 与 ledger 分析脚本读同一文件格式，需同步迁移读取端。
- **建议验证**: 压测：max_global 填满后测单条 append 耗时；观察消息 p95 延迟变化；WebUI /cognition/recent-turns 响应时间。
- **适合立即开发**: 是 | **执行轮次**: R5
- **关联发现**: 跨域：08 runtime/persistence 域应有同源发现

### ID-04 [P2/VERIFIED] 私聊 prompt 被硬编码'群聊/群友/群里'话术污染（warm/cold 摘要模板不分场景）

- **用户可感知后果**: 私聊每一轮的 warm_summary 都告诉模型'在延续刚才的群聊话题/群里在消化细节'，1:1 情感对话（如'我失恋了'）语境被误导，模型可能提及不存在的群和群友。
- **根因**: 私聊复用 GroupDialogueStore 与 context_compaction，其 topic 兜底模板与叙事文案硬编码群聊措辞，未按 chat 类型分支。
- **证据**: `astrmai/conversation/attention/group_dialogue_store.py:L348-L355` — `text=f"当前主要是在延续刚才的群聊话题，最近落点是“{self._preview_text(anchor.content, 28)}”。",`
- **证据**: `astrmai/conversation/attention/group_dialogue_store.py:L363-L380` — `"最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。" / "最近的推进主要来自群友之间的补充和接话..."`
- **证据**: `astrmai/conversation/attention/context_compaction.py:L1594-L1600` — `f"这段旧对话主要延续同一组群聊话题，后段落在“{...}”。",`
- **证据**: `astrmai/conversation/planning/planner_prompt_context.py:L323-L335, L425-L431` — `warm_bundle = None if is_lightweight_event else await self._get_warm_context_bundle(chat_id)`
- **运行时佐证**: 私聊 ff:FriendMessage:1481314186 全部 15 个 executed turn 的 warm_summary_preview 以'当前主要是在延续刚才的群聊话题'开头；私聊 traces 中 45 个 warm 字段命中'群聊'。主控疑点③确认，来源为 group_dialogue_store.py L352（非 compaction_providers/topic_summarizer）。
- **测试缺口**: 缺 FriendMessage 会话 warm/cold 摘要措辞断言。
- **最小修复边界**: group_dialogue_store._extract_warm_topic_units / _build_warm_summary + context_compaction._structure_from_segments 增加 is_private 判断（'FriendMessage' in chat_id）切换措辞；prompt_templates.py L155 摘要助手 system 同步。
- **回归风险**: 低——纯文案分支。
- **建议验证**: 私聊发两条消息后检查 trace warm_summary_preview 不再含'群聊/群友/群里'。
- **适合立即开发**: 是 | **执行轮次**: R6

### TL-01 [P2/VERIFIED] 二段披露展开机制 585 轮/16h 零触发：唯一入口是模型主动调 bot_capability_lookup 且需整轮重跑，实践中不可达

- **用户可感知后果**: 需要 identity/relationship/artifact 事实的轮次若关键词未命中，模型拿不到对应查询工具，只能臆答或答不知道；机制本身付出每轮 second_pass 计算与 executor 双跑复杂度却零收益。
- **根因**: 展开触发被埋在一个模型几乎从不调用的工具里（65/68 工具轮 0 工具调用，capability 工具 16h 0 次），且 planner guidance 从不提示该自检路径；触发后 executor 以 [SYSTEM TOOL DISCLOSURE] 前缀整轮重跑，成本双倍。
- **证据**: `astrmai/conversation/planning/tools/pfc_tools.py:L2374-L2382` — `package = normalize_requested_packages([kwargs.get("needed_package", "")]) if package and hasattr(event, "set_extra"):     existing = list(event.get_extra("astr`
- **证据**: `astrmai/conversation/execution/executor.py:L966-L989` — `expanded_tools, expanded_packages = self._expand_tools_for_disclosure_request(event, tools) if expanded_packages:     ...     result = await self.gateway.tool_c`
- **运行时佐证**: 585 traces：disclosure_expanded_packages 非空 0 次；tool_execution_trace 非空仅 3/68 工具轮；bot_capability_lookup 执行 0 次；62/70 轮仅披露 core 6 件套。
- **相关测试**: `tests/test_executor_refactor.py::test_tool_mode_can_expand_readonly_disclosure_package_once`, `tests/test_tool_disclosure_refactor.py`
- **测试缺口**: 缺产品级信号：无遥测告警断言展开使用率；无 guidance 层引导模型自检的契约测试。
- **最小修复边界**: planner.py::_append_tool_guidance 增加一行『工具不够时调用 bot_capability_lookup(needed_package=...)』提示；或 planner_side_inputs._build_execution_tools 在识别到 identity/relationship 疑问信号但关键词未命中时直接并包，弱化模型自检依赖。
- **回归风险**: 低：只增 guidance 文本或扩大只读包披露，无副作用工具。
- **建议验证**: 回放 trace 统计 disclosure_expanded_packages 与 identity/relationship 工具执行次数由 0 转正；单测 guidance 文案包含提示。
- **适合立即开发**: 是 | **执行轮次**: R6
- **关联发现**: TL-02

### TL-02 [P2/VERIFIED] social_intent(tease/comfort) 家族过滤清空披露层为图片/引用消息加的 artifact 工具，连 core 查询与 wait_and_listen 一并剥除

- **用户可感知后果**: 图片消息被判为调侃/安慰语境时（7/70 执行轮），模型既无查图工具也无记忆/人设查询与等待能力；叠加 vision 转写失败（同窗 vision timeout 40 次）时只能臆测图片内容，用户看到瞎编的看图回复。
- **根因**: planner_side_inputs L1005 仅在 intent_families is None 时才把披露家族并入 allowed_families；tease/comfort 的 intent_families 只含 fun 家族，随后 _filter_tools_by_families 将 disclosure 刚加的 artifact/core 工具全部滤除——两套过滤器语义冲突，披露决定被静默否决。
- **证据**: `astrmai/conversation/planning/planner_side_inputs.py:L699-L710` — `if social_intent == "comfort":     return {"reaction", "qq_reaction", "like"} if social_intent == "tease":     families = {"meme", "reaction", "qq_reaction", "l`
- **证据**: `astrmai/conversation/planning/planner_side_inputs.py:L1005-L1007` — `if explicit_tool_intent or (not allowed_families and intent_families is None):     allowed_families.update(disclosure_families)`
- **运行时佐证**: trace 1785050973（图片轮，reasons 含 artifact:message_artifact）filtered_tools=['qq_custom_face_send_tool','proactive_meme','message_reaction_action','message_emoji_like_action']，artifact 与 core 全部被滤。
- **相关测试**: `tests/test_planner_side_inputs_refactor.py`
- **测试缺口**: 缺 has_image/has_reply × social_intent 交叉的集成测试，断言 artifact 查询工具在图片轮不被家族过滤剥除。
- **最小修复边界**: planner_side_inputs._build_execution_tools：把 message_artifact/vision_message（及 wait）家族并入 intent_families 的白名单，或在 disclosure_reasons 含 message_artifact/reply_message/forward_message 时保护对应工具（类似 explicit_qq_action_restore 的保护逻辑）。
- **回归风险**: 中：扩大 tease/comfort 轮的工具面，需确认 fun 语境不滥用查询工具（均只读，风险有限）。
- **建议验证**: 构造 has_image=True + social_intent=tease 的单测断言 vision_message_analyze_tool 在 filtered_tools 中；回放 trace 观察图片轮 filtered_tools。
- **适合立即开发**: 是 | **执行轮次**: R6
- **关联发现**: TL-03, TL-07

### TL-03 [P2/VERIFIED] sanitized execution event 将消息组件替换为 Plain 占位，vision/artifact 工具的『当前消息』路径必然假阴性

- **用户可感知后果**: 用户发图后，工具循环里首次查图返回『当前没有发现可分析的图片或表情包片段』，模型可能直接断言用户没发图；实测一轮浪费 4 次工具调用、21.5s，最终答『图好像还在加载中』。
- **根因**: _build_sanitized_execution_event 把 message_obj.message 整体替换成 [Plain(safe_text)]，而 pfc 工具经 _get_current_event 拿到的正是该 sanitized 事件；message_id 留空分支从 message_obj 提取 image/forward 段必为空。工具需模型自行猜出 message_id 走 NapCat get_msg 才能兜回。
- **证据**: `astrmai/conversation/execution/executor.py:L106-L114` — `sanitized_message_obj = copy.copy(event.message_obj) safe_text = (... "[image-or-special-message]" if vision_bundle.image_urls else "[special-message]") sanitiz`
- **证据**: `astrmai/conversation/planning/tools/pfc_tools.py:L2053-L2061` — `payload = {"message": getattr(getattr(event, "message_obj", None), "message", None), ...} candidates = _extract_image_candidates(_payload_data(payload)) if not `
- **运行时佐证**: astrbot_since_c4aee57.log 06:12:40（L245-256）：vision_message_analyze_tool 首调返回『没有发现可分析的图片』→ artifact_lookup 取 message_id → 二次调用经 get_msg 才见到图片段；trace 1785017592 全轮 4 次工具调用后回复『图好像还在加载中』。
- **相关测试**: `tests/test_executor_vision_refactor.py`, `tests/test_pfc_tools_chat_extensions_refactor.py`
- **测试缺口**: 缺『sanitized 事件下 vision/artifact 工具仍能访问原始图片段』的回归测试。
- **最小修复边界**: executor._build_sanitized_execution_event：在 sanitized event 上以 extra（如 astrmai_original_message_segments）保留原始组件供工具读取；或 pfc_tools 的当前消息分支优先读该 extra。
- **回归风险**: 中：sanitize 本意是防注入/防误发原始段，需保证保留路径只供只读工具解析，不回流到 prompt。
- **建议验证**: 单测：构造带 Image 段事件 → sanitize → VisionMessageAnalyzeTool.call 应返回图片段信息而非『没有发现』。
- **适合立即开发**: 是 | **执行轮次**: R6
- **关联发现**: TL-02, TL-07

### TL-06 [P2/LIKELY] 『听说/据说/有人说/不确定』日常词直接构成 unverified_report 显式工具意图：升级 task tier 并强制 required 工具

- **用户可感知后果**: 含『听说』的普通闲聊会被升级为 task tier + 强制调用 unverified_report_record_tool；模型不配合时经 enforcement 重试后用户收到答非所问的『我还没能确认这次要执行的具体信息，所以没有操作…』澄清文案，或被写入无意义未核实报告。
- **根因**: GENERAL_EXPLICIT_TOOL_KEYWORDS['unverified_report'] 触发词过于日常，且 _simple_slot_resolution 的 required_tokens 与触发词同源，命中即 ready_required → build_explicit_invocation_plans 生成 required 计划，无二次置信判据。
- **证据**: `astrmai/conversation/planning/planner_side_inputs.py:L150` — `"unverified_report": ("听说", "据说", "有人说", "未确认", "不确定"),`
- **证据**: `astrmai/conversation/planning/tool_intent_resolution.py:L207-L215` — `elif family in {"unverified_report", "persona_fact"}:     resolutions.append(_simple_slot_resolution(family, message,         required_tokens=("听说", "据说", "有人说"`
- **证据**: `astrmai/conversation/planning/planner_side_inputs.py:L1143-L1151` — `executable_families = ready_families(intent_resolutions) ... plans = build_explicit_invocation_plans(executable_families, turn_tools.filtered_tools) ... turn_to`
- **运行时佐证**: 16h 窗口 0 次命中（executed 轮消息未含触发词），required 仅 self_lore 1 次（『你是谁』触发，链路本身工作正常）；风险为潜在高频误触发。
- **相关测试**: `tests/test_tool_invocation_contracts.py`
- **测试缺口**: 缺日常语料负样本测试：『我听说你会画画』不应产生 required 计划。
- **最小修复边界**: planner_side_inputs.GENERAL_EXPLICIT_TOOL_KEYWORDS：unverified_report 触发词收紧为组合模式（如需同时含转述源+断言结构），或将该家族从 required 降级为 optional（explicit_policy 保持 required 但 plans 侧跳过）。
- **回归风险**: 低：仅影响该家族触发精度；真实纠错场景另有 memory_correction 家族。
- **建议验证**: 单测負样本 + 回放线上一周 trace 统计 unverified_report required 触发率与其中误触占比。
- **适合立即开发**: 是 | **执行轮次**: R6

### TL-07 [P2/VERIFIED] perception.image_count 在全部 585 traces 恒 0，图片轮在观测层不可辨识

- **用户可感知后果**: 运营无法从 trace 定位图片轮，无法量化图片轮的披露正确率/vision 成功率；本轮审计的 image_count 对照分析被迫改用 vision ledger 反推。
- **根因**: perception 采集点未接通图片计数来源（disclosure 用的 direct_image_refs/extracted_image_refs/组件 hint 判 has_image 为真，但 turn_context.perception.image_count 从未被这些来源赋值）。
- **证据**: `astrmai/conversation/planning/planner_side_inputs.py:L959-L962` — `has_image = bool(     event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls", []))     or event.get_extra("extracted_image_refs", event.get_e`
- **运行时佐证**: 585/585 轮 perception.image_count=0；其中 trace 1785017592（vision 工具执行 2 次）、1785050973（vision ledger success）、1785051067（vision ledger 9 次尝试）均为图片轮但 image_count=0；disclosure_reasons 却含 artifact:message_artifact 证明 has_image 判定为真。
- **测试缺口**: 缺 perception 采集与 disclosure has_image 的一致性断言测试。
- **最小修复边界**: turn_context perception 装配点（sensors/ingress 或 planner 侧）用与 disclosure 相同的来源为 image_count 赋值。归属 observability/ingress 域，此处立据。
- **回归风险**: 低：纯观测字段。
- **建议验证**: 发一条图片消息，断言 trace perception.image_count>=1。
- **适合立即开发**: 是 | **执行轮次**: R6
- **关联发现**: TL-02, TL-03

### ML-07 [P2/VERIFIED] Dream 每轮往真实会话写『[dream_maintenance] 完成 N 次维护动作』运维噪声记忆；LLM 合并叙事不经 admission 治理直接 active

- **用户可感知后果**: 运维文案作为 importance=0.65 的 active 记忆可被检索注入聊天 prompt（用户可能看到 bot 提及『维护动作』）；dream 代理改写失真的合并叙事会替换原始记忆成为唯一活跃版本。
- **根因**: dream_scheduler 把 maintenance summary 写入真实 session（无 dedup、逐轮累积）；DreamAgent._tool_merge 经 add_memory(source=legacy_add_memory) 写入，source 不在 MemoryAdmissionService._GOVERNED_FACT_SOURCES。
- **证据**: `astrmai/proactive/dream_scheduler.py:L233-L237` — `await self.memory_engine.add_memory(     content=f"[dream_maintenance] {maintenance['summary']}",     session_id=session_id,     importance=0.65, )`
- **证据**: `astrmai/memory/dream/dream_agent.py:L226-L231` — `new_memory_id = await self.memory_engine.add_memory(     content=new_narrative,     session_id=session_id,     importance=0.7, )`
- **证据**: `astrmai/memory/services/memory_admission_service.py:L22` — `_GOVERNED_FACT_SOURCES = frozenset({"instant_gate", "instant_gate_llm", "memory_summary"})`
- **运行时佐证**: 本地无 DB；采样：SELECT COUNT(*) FROM canonical_memories WHERE content LIKE '[dream_maintenance]%' AND status='active';
- **相关测试**: `tests/unit/memory/test_dream_agent_gap_coverage.py`
- **测试缺口**: 缺『dream 运维摘要不得进入会话可检索层』断言
- **最小修复边界**: dream_scheduler: maintenance 摘要改写 __dream_diary__ 或仅存 meta；dream merge 写入走 admission（源加入治理名单）
- **回归风险**: 低
- **建议验证**: 跑一次 dream 后检索该会话，无 [dream_maintenance] 候选
- **适合立即开发**: 是 | **执行轮次**: R7
- **关联发现**: ML-08

### ML-08 [P2/LIKELY] Dream 事实晋升的 3 证据阈值可被单次 LLM 响应内重复项满足，写出 confidence=1.0 权威事实并可覆盖用户亲述事实

- **用户可感知后果**: 梦境代理幻觉出的 (subject,entity,attribute,value) 若在 detected_facts 里重复 3 次即被晋升为 confidence=1.0、importance=0.9 的权威事实，且经 authority EAV supersede 顶掉同 key 的真实事实。
- **根因**: normalize_dream_facts 不去重；_iter_detected_facts 对每项独立计数；confidence 缺省 0.9、signal 缺省 high 恰好通过 >=0.85/high 过滤；晋升写入 confidence 硬编码 1.0 且 source=dream_audit_pipeline 不受 admission 治理并在 _looks_like_authority_eav 白名单内。
- **证据**: `astrmai/memory/dream/promotion_engine.py:L105-L106` — `for (subject_id, entity, attribute, value), evidences in grouped.items():     if len(evidences) < self.PROMOTION_THRESHOLD:`
- **证据**: `astrmai/memory/dream/promotion_engine.py:L147-L149` — `importance=0.9, confidence=1.0,`
- **证据**: `astrmai/memory/dream/fact_contract.py:L21-L26` — `confidence = max(0.0, min(1.0, float(value.get("confidence_score", 0.9)))) ...signal = str(value.get("confidence_signal") or "high")`
- **证据**: `astrmai/memory/services/v2_store.py:L518-L519` — `return source in {"instant_gate", "instant_gate_llm", "dream_audit_pipeline", "authority_backfill"}`
- **运行时佐证**: 需 DB 佐证：SELECT * FROM canonical_memories WHERE source='dream_audit_pipeline' ORDER BY create_time DESC LIMIT 20; 检查 metadata.evidence_turns 是否同 turn 重复。
- **相关测试**: `tests/unit/memory/test_memory_promotion.py`
- **测试缺口**: 缺单响应重复事实不得计为多证据的测试
- **最小修复边界**: promotion_engine._iter_detected_facts 按 (key, evidence.turn_id) 去重；confidence 取证据实际置信度上限而非 1.0
- **回归风险**: 低
- **建议验证**: 单测：detected_facts 含同一事实 3 份 → 不晋升；3 个不同 turn 证据 → 晋升
- **适合立即开发**: 是 | **执行轮次**: R7
- **关联发现**: ML-03, ML-07

### ML-09 [P2/VERIFIED] 挖掘 fail-closed 无毒丸跳过：坏批次每 30min 原样重试可永久卡死单群学习；新窗口 16.6h 零挖掘日志，链路是否存活不可观测

- **用户可感知后果**: 某群一旦有让 enrichment 持续失败的消息批（坏 JSON/模型拒答），该群黑话与表达学习永久停滞，backlog 只增不减，且没有任何提级告警；运营者也无法从日志判断挖掘是否在正常工作。
- **根因**: process_logs_and_mine 失败时不标记 processed（fail-closed，f09cf65 引入，防数据丢失是对的），但 backlog 重试固定取同一头部批次，无失败计数/跳过机制；成功路径不打日志导致零日志无法区分『没跑』与『没失败』。
- **证据**: `astrmai/learning/evolution_manager.py:L604-L614` — `if not jargon_terminal:     await self._record_mining_outcome(..., reason="jargon_enrichment_failed_closed", ...)     raise RuntimeError("jargon enrichment fail`
- **证据**: `astrmai/learning/evolution_manager.py:L809-L812` — `except Exception as exc:     self._backlog_failure_until[group_id] = time.time() + self._backlog_failure_cooldown()     logger.warning(f"[Evolution-Backlog] min`
- **运行时佐证**: 旧日志：群 1075910254 连续 3 次失败（02:58/03:29/04:00，间隔=1800s 冷却）；新日志 16.6h 无任何 JargonEnricher/Evolution-Backlog/话题分割行。DB 定论 SQL：SELECT key,value FROM memory_v2_meta WHERE key LIKE 'learning_mining_ledger:%';
- **相关测试**: `tests/unit/learning/test_jargon_pipeline_migrated.py`, `tests/unit/learning/test_expression_enrichment_pipeline.py`
- **测试缺口**: 缺连续失败 N 次后跳过毒批（标记 processed + 提级告警）的测试
- **最小修复边界**: evolution_manager: _backlog_failure_until 增加按群失败计数，>=3 次跳过头部 min_mining_context 条并标记 processed；run_backlog_mining_once 成功时打 INFO 摘要
- **回归风险**: 低——跳过量以 overlap 尺寸为界
- **建议验证**: 注入必失败的 enricher stub，断言第 4 次重试后 head 批被跳过且 backlog 下降
- **适合立即开发**: 是 | **执行轮次**: R7

### WU-09 [P2/VERIFIED] 空数据三义性：前端把错误回退缓存成 180 秒"新鲜空数据"；runtime_bound:false 与真无数据渲染完全相同

- **用户可感知后果**: 一次瞬时 bridge/后端错误后，该 tab 稳定空白 3 分钟（toast 只闪一次且 5 秒去重，切换 tab 不重试）；组件未绑定(runtime_bound:false)、后端吞掉的异常（list_canonical SQL 回退 except→[]、_safe_count→0、recent_turn_traces 异常→内存回退）与真无数据在除 persona 外的所有页面都渲染成同一句"暂无数据"——即"页面空白但不知道为什么"的直接根源。
- **根因**: cachedFetch 把 safeFetch 的 fallback（含错误回退）以 updatedAt=now 写入 dataCache（TTL 180s）；table() 空态不接收 runtime_bound/error 信息；后端多处 `except Exception → 200 + 空集合`。
- **证据**: `pages/admin/app.js:L159-L165` — `const data = await safeFetch(fetchFn, cached?.data ?? fallback);   state.dataCache[key] = { data, updatedAt: Date.now() };   return data;`
- **证据**: `pages/admin/app.js:L440-L452` — `function table(headers, rows, empty = "暂无数据") {   if (!rows || rows.length === 0) {     return `<div class="empty-state"><p>${empty}</p></div>`;`
- **证据**: `astrmai/webui/backend/services/memory_ui_service.py:L275-L276` — `except Exception:                 return {"status": "ok", "runtime_bound": False, "items": [], "total": 0}`
- **相关测试**: `tests/test_webui_backend_refactor.py::test_review_ui_service_does_not_mask_bound_runtime_failures_as_empty_pending_list`
- **测试缺口**: 后端有个别防吞错测试；前端缓存层"错误不得当新鲜数据缓存"完全无测试（无前端测试基建）。
- **最小修复边界**: app.js::cachedFetch（错误回退不写缓存或标记 stale=true 下次强制重试）+ table()/asItems 空态透出 runtime_bound=false 与"加载失败"两种专属文案；后端 list_canonical 回退分支返回 status:degraded。
- **回归风险**: 低——渲染层与缓存策略改动。
- **建议验证**: 断网/停插件复现：首次 toast 后 3 分钟内页面固定空白；修复后应显示错误态并可重试。
- **适合立即开发**: 是 | **执行轮次**: R7
- **关联发现**: WU-08

### RT-07 [P3/VERIFIED] compaction_provider_id 配置指向不存在的 openai/deepseek-v4-pro：每次压缩首个尝试必失败

- **用户可感知后果**: 每次上下文压缩浪费一次失败往返并产生 ProviderNotFoundError 噪音（trace 3 次、star.context 警告 4 条）；若某天回落 provider 也不可用，压缩将静默失败。启动时无配置校验，运营者不知情。
- **根因**: 服务器 conversation.compaction_provider_id 残留旧 provider id；_resolve_provider_candidates 将其排在候选第一位且不做存在性校验。
- **证据**: `astrmai/conversation/attention/compaction_providers.py:L24-L28` — `candidates: list[str] = [] configured = str(self.provider_id or "").strip() if configured:     candidates.append(configured)`
- **证据**: `astrmai/conversation/attention/context_compaction.py:L215` — `self.provider_id = str(getattr(conversation, "compaction_provider_id", "") or "")`
- **运行时佐证**: trace 3 条 attention.compaction.v2 model_attempts[0]={model: openai/deepseek-v4-pro, error_kind: ProviderNotFoundError}; 日志 star.context:403 '没有找到 ID 为 openai/deepseek-v4-pro 的提供商' ×4。
- **合并说明**: 吸收 PL-07 的 compaction 误配主体（文案误导 + 无校验）；PL-07 中 gateway_call.py:389 provider=unknown 姊妹问题归入 RT-08 修复边界。
- **测试缺口**: 缺启动期 provider id 存在性校验测试。
- **最小修复边界**: bootstrap 或 compaction 初始化时对 compaction_provider_id 做一次 get_provider_by_id 校验，不存在则 WARN 并从候选剔除。
- **回归风险**: 低。
- **建议验证**: 配置假 id 启动，应见一次性 WARN 且候选列表不含该 id。
- **适合立即开发**: 是 | **执行轮次**: R1

### PL-10 [P3/NEEDS_RUNTIME_EVIDENCE] PluginLifecycleManager._terminated 永久闩锁：同实例 terminate 后 on_program_start 永拒，无解除路径

- **用户可感知后果**: 若 AstrBot 在禁用→启用/热重载场景复用同一插件实例（不重新 __init__），插件将静默拒绝启动（仅一行 'runtime startup rejected reason=terminated'），直到进程重启。
- **根因**: lifecycle.py terminate() 置 _terminated=True 后没有任何路径复位；on_program_start 第一行即拒绝。是否触发取决于 AstrBot 的插件 enable/disable 是否重建实例。
- **证据**: `astrmai/app/lifecycle.py:L53-L56,L307-L310` — `if self._terminated:     logger.warning("[AstrMai] runtime startup rejected reason=terminated")     return ... async def terminate(self):     self._terminated =`
- **运行时佐证**: 观测日志未出现该 reject 行（观测期无重载），需要框架 disable/enable 实测。
- **测试缺口**: 缺 terminate→initialize 同实例复活测试。
- **最小修复边界**: on_program_start 允许 _terminated 状态下重置标志重启（或 facade 在 initialize 时重建 LifecycleManager）。
- **回归风险**: 低。
- **建议验证**: AstrBot 面板禁用再启用插件（不重启进程），确认消息仍被处理。
- **适合立即开发**: 是 | **执行轮次**: R4

### PL-11 [P3/VERIFIED] agent.max_steps 被 executor 静默钳制到 >=5：schema/pydantic 允许 1-4 但无效

- **用户可感知后果**: 想限制聊天工具循环步数到 1-4 的用户设置无效，实际至少 5 步（成本/时延高于预期设置）。
- **根因**: executor.py L531 max_steps = max(5, config_max_steps) 的硬下限与配置声明（ge=1）不一致。
- **证据**: `astrmai/conversation/execution/executor.py:L529-L531` — `config_max_steps = getattr(self.config.agent, "max_steps", 5) ... max_steps = max(5, config_max_steps)`
- **相关测试**: `tests/test_executor_refactor.py`
- **测试缺口**: 缺 max_steps<5 时生效值的断言。
- **最小修复边界**: executor._execution_runtime_values：尊重配置或把 pydantic/schema 下限提到 5 并更新 hint。
- **回归风险**: 低。
- **建议验证**: 设 agent.max_steps=2，观察工具循环最多 2 步。
- **适合立即开发**: 是 | **执行轮次**: R4

### RT-10 [P3/VERIFIED] 观测字段小缺陷簇：prefix_changed_reason 稳定轮被标 unavailable_in_trace、63 次 attention.dispatch abandoned 为快照顺序伪影、trace created_at 是捕获时刻

- **用户可感知后果**: 缓存与延迟趋势分析失真：61/67 稳定轮的 prefix_changed_reason 显示 'unavailable_in_trace'（实为 stable）；63 个 'abandoned' dispatch 让人误判存在挂起；用 created_at 对齐阶段时间戳会得到负偏移（本审计即踩坑）。
- **根因**: ①context_engine 稳定时 reason=''，planner 用 `or "unavailable_in_trace"` 覆写空串；②skip 路径在 dispatch stage 内 finalize+快照，快照后 observe_stage 才把条目改回 success；③build_turn_trace_summary 的 created_at=time.time()（捕获时刻）。
- **证据**: `astrmai/conversation/planning/planner.py:L263` — `turn_context.continuity.prefix_changed_reason = str(prefix_status.get("prefix_changed_reason", "") or "unavailable_in_trace")`
- **证据**: `astrmai/conversation/planning/context_engine.py:L229-L230` — `prefix_stable = True prefix_changed_reason = ""`
- **证据**: `astrmai/conversation/planning/planner.py:L745-L753` — `item = build_turn_trace_summary(..., created_at=time.time(), ...) finalize_turn_telemetry(event, outcome=status) telemetry = turn_telemetry_snapshot(event)`
- **运行时佐证**: executed 轮 prefix_stable=True 61 个全部 reason='unavailable_in_trace'；stage_ledger abandoned: attention.dispatch 63 / memory.injection 1，与 skip 状态一一对应。
- **相关测试**: `tests/test_turn_trace_store_v2_refactor.py`
- **测试缺口**: 缺 'stable 轮 reason 应为 stable/空' 与 'skip 路径 dispatch 最终状态' 的断言。
- **最小修复边界**: planner.py L263 改为空串时置 'stable'；finalize_turn_telemetry 跳过名为 attention.dispatch 的外层 stage 或延迟快照；trace 增加 turn_started_at 已有——分析工具应使用它（scripts/analyze_turn_ledger.py 已用 started_at，文档化即可）。
- **回归风险**: 低。
- **建议验证**: 重放后 executed 轮 prefix_changed_reason 分布应为 stable/first_seen/frozen_rules_or_persona_changed。
- **适合立即开发**: 是 | **执行轮次**: R5
- **关联发现**: RT-01

### ID-08 [P3/VERIFIED] 撤回(recall)通知零处理：被撤回消息原文继续留在对话上下文中可被 bot 引用

- **用户可感知后果**: 用户撤回消息后，bot 后续回复仍可能原文复述该内容（隐私/尴尬）；撤回不产生任何'他撤回了'的语境信号。作者归属本身正确，不存在错绑到别人。
- **根因**: message_entry 把非 poke notice（group_recall/friend_recall）标记 non_conversational 后直接 return；全插件无 recall 消费者，dialogue_store/attention window/lane history 均不清理。
- **证据**: `astrmai/presentation/events/message_entry.py:L89-L97, L160-L176` — `if notice_type == "poke" or sub_type in {"poke", "戳一戳"}:     return "poke_notice", payload return "notice_passthrough", payload`
- **证据**: `astrmai/conversation/attention/group_dialogue_store.py:L92-L132` — `append_segment(...)  # 无按 event_id 删除/tombstone 接口被 recall 调用`
- **运行时佐证**: log 16h ≥5 条 group_recall notice（RecallGuard WARN 行 L671/L948/L979/L1167/L1192），插件侧无任何对应处理日志。
- **测试缺口**: 缺 recall notice → store tombstone 的用例（当前无此功能）。
- **最小修复边界**: message_entry notice 分类新增 recall 路由 → group_dialogue_store 按 event_id 打 tombstone（内容替换为'[已撤回]'，保留 speaker）。
- **回归风险**: 低。
- **建议验证**: 群里发消息→撤回→@bot 询问，检查 bot 不引用原文。
- **适合立即开发**: 是 | **执行轮次**: R6

### ID-10 [P3/VERIFIED] poke 目标解析兜底把'戳别人(目标缺失)'误记为'戳 bot'：无端回戳+好感度误结算

- **用户可感知后果**: 当适配器 payload 缺 target_id 时，B 戳 C 会被当作 B 戳 bot：bot 无端回戳 B 并给 B 加好感（affection intensity 0.35）；另 poke 叙事里 target 显示名可能混入群名片签名文本，观感差。
- **根因**: target_id 非数字且≠bot_id 时被清空，随后空/0 一律回填 bot_id；_resolve_name 的名片回退链未过滤签名样文本。
- **证据**: `astrmai/conversation/ingress/sensors.py:L500-L504` — `if target_id and not target_id.isdigit() and target_id != bot_id:     target_id = "" if not target_id or target_id == "0":     target_id = bot_id`
- **证据**: `astrmai/conversation/ingress/sensors.py:L605-L616` — `if actor_confident and target_is_bot and state_engine and hasattr(state_engine, "calculate_and_update_affection"):`
- **运行时佐证**: 16h 样本 30 例 peer poke target 均解析成功（未踩中兜底）；叙事污名样本：'肥鱼罐头az 戳了 袅袅都烫唧唧的季节里，希望你别中暑了~ 一下'（target 显示名=签名）。
- **相关测试**: `tests/original_ported/test_attention_interaction_narrative_ported.py`
- **测试缺口**: 缺 target_id 缺失/非数字场景断言（应降级为 unknown-target 而非 bot-target）。
- **最小修复边界**: sensors.process_poke_event：target 不可解析时标记 target_unknown，跳过回戳与好感结算；_resolve_name 过滤超长/含空格的名片文本。
- **回归风险**: 低。
- **建议验证**: 单测：payload 无 target_id 的 poke，断言不回戳、不结算好感。
- **适合立即开发**: 是 | **执行轮次**: R6
- **关联发现**: ID-03

### TL-08 [P3/VERIFIED] FAMILY_TO_PACKAGES['quote_reply'] 是死配置：quote_reply 属 PRECISION_ONLY，包映射永不生效，引用场景无自主 quote 能力为纯关键词依赖

- **用户可感知后果**: 对用户仅表现为引用场景 bot 从不主动用原生引用回复（16h 内 quote_reply_action 0 披露 0 执行）；对维护者是误导性配置——看映射以为 artifact 包含引用能力。
- **根因**: plan() 将 PRECISION_ONLY_FAMILIES 从包映射剔除（L375-379），quote_reply 只能经 exact_tool_names 显式注入；FAMILY_TO_PACKAGES['quote_reply']=('artifact','native_action') 与 TOOL_PACKAGES['artifact'] 不含 quote_reply_action 双重矛盾。
- **证据**: `astrmai/conversation/planning/tool_disclosure.py:L99, L132-L149` — `"quote_reply": ("artifact", "native_action"), ... PRECISION_ONLY_FAMILIES: frozenset[str] = frozenset({..., "quote_reply", ...})`
- **证据**: `astrmai/conversation/planning/tool_disclosure.py:L375-L379` — `package_families = [     family     for family in explicit_families     if family not in PRECISION_ONLY_FAMILIES ]`
- **运行时佐证**: 585 traces：quote_reply_action 披露 0 次；is_reply_to_bot 轮 0（观测缺失，见 TL-07）；artifact:reply_message 轮（1785065343）filtered 无 quote_reply_action。
- **相关测试**: `tests/test_tool_disclosure_refactor.py::test_explicit_high_side_effect_action_does_not_open_whole_native_package`
- **测试缺口**: 缺 FAMILY_TO_PACKAGES 与 TOOL_PACKAGES/PRECISION_ONLY 三者一致性检查测试。
- **最小修复边界**: tool_disclosure.py：删除 quote_reply（及其它 PRECISION_ONLY 家族）在 FAMILY_TO_PACKAGES 的映射，或加模块级断言保证三表一致。
- **回归风险**: 低：删除死配置无行为变化。
- **建议验证**: 新增一致性单测：PRECISION_ONLY_FAMILIES ∩ 有效包映射 = ∅。
- **适合立即开发**: 是 | **执行轮次**: R6

### TL-09 [P3/VERIFIED] 跨会话 handoff 仅内存驻留且注入块 360 字符截断，三方消歧指令位于截断尾部

- **用户可感知后果**: 插件重载/重启后目标好友回复传话消息时 bot 失忆冷启动，『传话人』语境断裂；摘要与消息取满长度时块尾的『区分发起人/机器人/收件人』指令被截掉，长消息传话场景更易把三方混淆。
- **根因**: CrossSessionHandoffStore 纯内存（bootstrap 每次新建实例），无 DB 落盘；_apply_private_jump_context 对整块 sys_inject 统一截到 PRIVATE_JUMP_CONTEXT_MAX_CHARS=360，且 _truncate_runtime_instruction_text 压平换行，指令排在块尾优先被截。
- **证据**: `astrmai/infrastructure/runtime/cross_session_handoff_store.py:L36-L43` — `class CrossSessionHandoffStore:     DEFAULT_TTL_SECONDS = 1800.0     ...     def __init__(self) -> None:         self._handoffs: dict[tuple[str, str], list[Cros`
- **证据**: `astrmai/conversation/planning/planner_side_inputs.py:L1288-L1309` — `+ f"【已经发给当前对方的消息】：{private_message}\n" "当前发消息给我的人是收件人，不一定是上一会话的发起人。"... sys_inject = self._truncate_runtime_instruction_text(sys_inject, self.PRIVATE_JUMP_CONTE`
- **运行时佐证**: 16h 窗口无 space_transition 实例；结构性推断（头部+90 字摘要+90 字消息 ≈260-300 字符，尾部 80 字符指令逼近 360 上限）。
- **测试缺口**: 缺长摘要/长消息下注入块保留消歧指令的测试；缺重启后 handoff 恢复的测试（当前设计即不支持）。
- **最小修复边界**: planner_side_inputs._apply_private_jump_context：把消歧指令移到块首或单独 clamp；handoff 落盘可选（persistence 增一张小表，lifecycle 恢复）。
- **回归风险**: 低：指令移位零风险；落盘为增量能力。
- **建议验证**: 单测：context_summary/message 各 90 字符时断言注入文本包含『收件人』消歧句。
- **适合立即开发**: 是 | **执行轮次**: R6

### ID-07 [P3/LIKELY] group_wait 残留不对称：reply: 键位等待无法被目标的普通跟进复活，unique-target 兜底因 has_explicit_thread 恒真成死分支

- **用户可感知后果**: 当唤醒消息带 Reply 组件时，bot 反问后用户用纯文本回答，等待不复活（不会立即接话，只能靠常规 judge，可能被 IGNORE）；低频（16h 样本 0 个 reply:* 等待）。
- **根因**: 等待按 ingress turn thread_id 键存（Reply 组件时为 reply:<msgid>），目标跟进消息键为 sender:<id>；unique-target 兜底要求 not has_explicit_thread，而 message_entry 先绑 turn（astrmai_turn_thread_id 恒非空）再处理等待，兜底永假。
- **证据**: `astrmai/state/group_wait/group_reply_wait_manager.py:L53-L62, L329-L337` — `if state is None and self.threaded_enabled and not has_explicit_thread and sender_id:  # has_explicit_thread 恒 True`
- **证据**: `astrmai/presentation/events/message_entry.py:L282-L285` — `await _bind_turn_identity(facade, event, scope) ... group_wait_result = await facade.handle_group_reply_wait(event, scope)`
- **运行时佐证**: log 16h：armed 39 次全部 thread=sender:*（主路径已修复，1 次成功 RESUME 06:42:42）；reply:* 等待 0 例，残留分支未踩中；另 37/39 等待超时未复活（命中率 2.6%，观察项）。
- **相关测试**: `tests/original_ported/test_group_wait_thread_signature_ported.py`, `tests/test_group_reply_wait_manager_concurrency_migrated.py`
- **测试缺口**: 缺 '等待键=reply:*，跟进=sender:* 纯文本' 组合用例。
- **最小修复边界**: group_reply_wait_manager.handle_incoming_message L329 条件放宽为 not incoming_thread_signature（turn_thread_id 不算 explicit），或 register 时同时登记 sender:<target> 别名键。
- **回归风险**: 低——仅扩大 resume 匹配面，budget/timeout 机制不变。
- **建议验证**: 单测构造 reply: 键等待 + 目标纯文本跟进，断言 RESUME。
- **适合立即开发**: 是 | **执行轮次**: R7
- **关联发现**: ID-01

### ML-11 [P3/VERIFIED] 演示场景启发式硬编码进生产：server_count=(\d+) 任意数字、火锅/芒果/蓝色词表进入 claim 规则与检索重排

- **用户可感知后果**: 聊天含『服务器』+任意数字即产出 asset:server_count 权威 claim（如聊 MC 服务器人数被记成用户资产）；意图重排/锚点词表只覆盖测试场景词汇，通用场景增益为零。
- **根因**: claim_rules.py 的 SERVER_COUNT_PATTERN=re.compile(r"(\d+)") 与 SERVER/ANXIETY 关键词、memory_retrieval_service._intent_rerank 的 food_terms、query_builder ANCHOR_TERMS 均为验收场景遗留。
- **证据**: `astrmai/memory/services/claim_rules.py:L9-L12` — `SERVER_COUNT_PATTERN = re.compile(r"(\d+)") SERVER_KEYWORDS = ("server", "servers")`
- **证据**: `astrmai/memory/services/memory_retrieval_service.py:L253-L254` — `food_terms = {"火锅", "芒果", "食物", "吃", "爱吃", "口味", "饭", "菜", "忌口", "餐"} non_food_terms = {"颜色", "蓝色", "音乐", "游戏", "跑步", "安静", "地点"}`
- **相关测试**: `tests/unit/memory/test_memory_claim_rules_zh.py`
- **测试缺口**: 缺负例测试（『服务器 100 人在线』不应产出 server_count claim）
- **最小修复边界**: claim_rules/claim_rules_zh 移除或收紧 server/anxiety 规则（要求『我有/我的+数量词+服务器』句式）；词表迁到配置
- **回归风险**: 低
- **建议验证**: 单测负例通过；相关既有测试（server 场景）同步更新
- **适合立即开发**: 是 | **执行轮次**: R7
- **关联发现**: ML-03

### TG-08 [P3/VERIFIED] 测试基建健康核实：收集 1673 条 0 错误、manual 脚本未腐化；session-state.md 测试计数(1142)过期

- **用户可感知后果**: 对运营者/接手者：以 session-state.md 为入口会低估测试面 (1142 vs 1673) 并使用过期的恢复命令预期。基建本身无问题：8 个 manual 脚本 AST 语法全过、astrmai 顶层 import 全部可解析(0 broken)、大 manual 脚本均有被收集的 wrapper 回归守护；scheduler_webui_fixture/state_bar_audit 巨型 helper 各有专测。
- **根因**: session-state.md 停留在 2026-07-05 快照，此后 10+ 提交新增数百条测试未回写文档。
- **证据**: `.agent/session-state.md:L34` — `- **1142 passed, 1 skipped**（从 936 增长）`
- **运行时佐证**: pytest --collect-only -q → '1673 tests collected in 9.12s'，无 collection error；manual API 检查 TOTAL broken: 0
- **相关测试**: `tests/test_group_trace_audit_refactor.py`, `tests/test_main_reply_cache_replay_live_refactor.py`, `tests/test_prompt_metrics_compare_refactor.py`, `tests/test_scheduler_fixture_refactor.py`
- **测试缺口**: 无（本条为基线核实+文档漂移）。
- **最小修复边界**: .agent/session-state.md 更新 Test Status 小节
- **回归风险**: 低：文档更新
- **建议验证**: python -m pytest --collect-only -q | tail -1
- **适合立即开发**: 是 | **执行轮次**: R7

### WU-11 [P3/VERIFIED] trace v2 新字段（llm_call_ledger/stage_ledger/reply_stats/budget/memory_funnel）已随 API 返回但管理页零呈现；工具披露表"工具"列恒为"-"

- **用户可感知后果**: c4aee57 补齐的 turn 级观测（每次 LLM 调用耗时/模型尝试、阶段账本、回复统计、预算耗尽）在 Turn Context 详情弹窗完全不可见（也无 raw JSON 兜底），排查延迟/预算问题仍要下载 trace 文件；"策略披露"表格工具列恒为"-"（数据字段是 tool_names 数组而非 tool_name）。注：简报疑点"WebUI 读 v1 字段"经核实不成立——前端读取的所有字段在 v2 schema 中均存在。
- **根因**: openTurnTrace 只渲染 9 个固定旧区块（perception/attention/cognitive/think/memory/follow_up/side_inputs/tools/continuity）；planner._remember_turn_trace L755-771 附加的 v2 字段无对应渲染；renderDashboardTools 读 item.tool_name||item.name，而 tool_trace_history 条目只有 tool_names。
- **证据**: `pages/admin/app.js:L1262-L1278` — `openModal(`Turn Context: ...`, `...${section("感知 Perception", ...)}...${section("连续性来源 Continuity", "", `<pre>${json(item.continuity || {})}</pre>`)}`);`
- **证据**: `astrmai/conversation/planning/planner.py:L755-L770` — `item.update({... "llm_call_ledger": telemetry["llm_call_ledger"], "context_block_stats": ..., "stage_ledger": ..., "reply_stats": ..., "budget": telemetry["budg`
- **证据**: `pages/admin/app.js:L1294` — `<td>${escapeHtml(item.tool_name || item.name || "-")}</td>`
- **运行时佐证**: 服务器 trace 样本 585 条均含 llm_call_ledger/stage_ledger（覆盖率 94%），确认数据已在 API 载荷中。
- **测试缺口**: 无前端渲染测试基建；可加 round11 式的 app.js 字段引用检查。
- **最小修复边界**: app.js::openTurnTrace——增加 LLM Calls/Stage Ledger/Reply Stats/Budget 区块（或至少 detailsJson 全量兜底）；renderDashboardTools 披露行改用 (item.tool_names||[]).join(', ')。
- **回归风险**: 低——纯前端展示。
- **建议验证**: 打开任一 executed turn 详情，应能看到 llm_call_ledger 表与 budget.remaining_ms。
- **适合立即开发**: 是 | **执行轮次**: R7
- **关联发现**: WU-06

### WU-12 [P3/VERIFIED] 计数口径与删除反馈错位集合：表达 total 含已删行、"黑话全量"实为已通过、legacy 事件删除返回 readonly 却 toast 已删除、Dashboard 待审仅统计表达

- **用户可感知后果**: (1) 学习页"表达习惯 total"把 deleted/rejected/pending 全算进（v2_store.list_canonical include_inactive 默认 True）→ 语料量虚高；(2) "黑话全量"tab 实查 status=active，标签与内容不符；(3) 无 canonical 映射的 legacy MemoryEvent 点删除：后端返回 status:readonly changed:false，前端一律 toast"记忆记录已删除"，行刷新后仍在——按钮看似坏了；(4) Dashboard"待审核项"仅表达不含黑话，与 Reviews 页四格口径不同。
- **根因**: canonical_kind_review_stats 的 total 用无状态过滤 count；app.js L1501 jargon_all 写死 status=active；app.js L1876-1885 删除后不检查 result.changed/status；dashboard snapshot pending_reviews 只算 expression_pattern。
- **证据**: `astrmai/webui/backend/services/runtime_memory_stats.py:L61-L64` — `total = await _count_canonical(store, kind=kind)  # include_inactive 默认 True，含 deleted/merged`
- **证据**: `pages/admin/app.js:L1501` — `const statusParam = state.reviewTab === "jargon_pending" ? "status=review_pending&" : "status=active&";`
- **证据**: `astrmai/webui/backend/services/memory_ui_service.py:L928-L933` — `return {"status": "readonly", "changed": False, "legacy": True, "message": "Legacy MemoryEvent rows are readonly; no canonical mapping was found."}`
- **相关测试**: `tests/unit/webui/test_round11_runtime_contracts.py::test_memory_event_create_is_visible_in_paired_list_and_deletable`
- **测试缺口**: 缺 readonly 删除结果的前端反馈约定测试；缺计数口径（total 是否含 inactive）契约测试。
- **最小修复边界**: runtime_memory_stats.py（total 限定 active+review_pending 或分列展示）；app.js 删除回调检查 result.status==='readonly'/changed===false 时改提示；"黑话全量"改名"黑话词库（已通过）"。
- **回归风险**: 低。
- **建议验证**: 对含 deleted 行的库比对学习页 total 与 SELECT COUNT(*) WHERE status IN ('active','review_pending')；删除 legacy 事件观察提示语。
- **适合立即开发**: 是 | **执行轮次**: R7
- **关联发现**: WU-03
