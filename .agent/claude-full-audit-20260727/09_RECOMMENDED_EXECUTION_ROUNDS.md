# 09 推荐执行轮次（每轮 ≤10 项，可独立验证交付）

> 排序原则：先止血用户可感知损害（R1），再修数据质量（R2），然后是延迟成本（R3）、配置容错（R4）、观测与防回归（R5）、工具打磨（R6）、需要产品决策的治理项（R7）。
> 每轮结束的统一门槛：`python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths"` 全绿 + 该轮各项的专属验证命令通过 + 部署后按对应 trace 指标复核。

## Round 1：止血：回复丢弃、后台假死、主动链复活（小边界高价值）

共 9 项（P0, P1, P2, P3）

| ID | 级别 | 标题 | 最小修复边界 | 建议验证 |
|---|---|---|---|---|
| **ID-01** | P0 | 群聊在途回复被任意无关新消息击杀：freshness 线程隔离因 latest_activity_thread_signature 恒空而整体失效 | gate.py::_record_event_activity 改传 resolve_group_thread(event,chat_id).thread_id；chat_runtime_coordinator.evaluate_reply_freshness 改按 turn thread_id 比 | 构造群聊：A @bot 提问，5s 后 B 发无关消息；断言 A 的回复仍发送且 trace 无 newer_activity_unknown_thread；再跑 python scripts/analyze_turn_ledger.py  |
| **RT-01** | P0 | 跨 task 继承的 contextvar 遥测导致预算 clamp 用错轮：per-chat memory worker 里 instant backfill 100% 失败 turn_deadline_exhausted | turn_call_ledger.clamp_timeout_to_turn_budget/record_llm_attempt 增加显式 event 透传（chat_in_lane_result→_elastic_call_result 传 event）；或后台 worker 创建时用 conte | 复跑 scripts/analyze_turn_ledger.py 看 instant backfill 成功率>0；grep 日志 turn_deadline_exhausted 应仅出现于真实预算耗尽轮。 |
| **PL-01** | P0 | 主动开口全链路死于传感器过滤：合成事件 message_obj=None，wakeup/heartflow/signin 主动消息从未发出过 | gate.AttentionGate._passes_sensor_filters 或 sensors.should_process_message 对 astrmai_is_proactive_event 豁免组件文本检查；或 dispatcher 构造 event_data 时补 Plain 组 | 本地构造 ProactiveMessageIntent 走 dispatcher.dispatch，断言 turn status 进入 judge 阶段而非 skipped_sensor_filter；线上观察 'proactive wak |
| **ID-03** | P2 | Peer poke（群友互戳）虚拟事件 100% 被过滤，整套 peer 互动剧本为死代码 | sensors.py::should_process_message 对 astrmai_interaction_kind 非空事件放行（频控继续交给 playbook 的 peer_join_allowed + judge）。 | 模拟 B 戳 C 的 OneBot notice，断言 trace 进入 judge 而非 sensor_filter。 |
| **ID-06** | P2 | Proactive 事件的'当前发言人归因锁'指向幽灵用户 astrmai_proactive_candidate（被 ID-02 掩盖的二级缺陷） | planner_prompt_context._build_current_speaker_block 对 astrmai_is_proactive_event 返回空串（归因锁随之消失）；必须与 ID-02 同批修复。 | 修复 ID-02 后触发主动开口，检查 prompt/trace 无 '主动开口候选' 字样且回复不含无端第二人称。 |
| **TL-05** | P2 | is_stale_reply_reason 漏配 superseded_by_newer_activity_same/_unknown_thread 变体，过期回复被误判为模型失败并触发换模型重试 | reply_freshness.is_stale_reply_reason：把前缀改为 'superseded_by_newer_activity'（去冒号）即可同时覆盖三种格式。 | 单测 is_stale_reply_reason('superseded_by_newer_activity_unknown_thread:a:5.0s') is True；回放日志确认不再出现该 reason 的 model failed |
| **RT-07** | P3 | compaction_provider_id 配置指向不存在的 openai/deepseek-v4-pro：每次压缩首个尝试必失败 | bootstrap 或 compaction 初始化时对 compaction_provider_id 做一次 get_provider_by_id 校验，不存在则 WARN 并从候选剔除。 | 配置假 id 启动，应见一次性 WARN 且候选列表不含该 id。 |
| **TG-02** | P1 | Provider 失败矩阵缺 not-found 轴：_is_fatal_failure 无 not-found 关键字且零测试，缺失 provider 被空转重试 | gateway_policy.py::_is_fatal_failure 加 not-found 关键字（或独立 FailureKind.PROVIDER_NOT_FOUND）+ tests/test_gateway_policy_refactor.py 矩阵测试 | python -m pytest tests/test_gateway_policy_refactor.py tests/test_gateway_context_passthrough_refactor.py -q |
| **WU-08** | P2 | /learning/cooldowns 永远返回空对象：读取的属性名 _recent_patterns 从未存在（真实为 _recent_pattern_keys） | admin_ui_service.py::expression_cooldowns 与 learningservice.py::expression_cooldowns——读 _recent_pattern_keys（tuple key 需序列化为字符串）。 | 对 selector._recent_pattern_keys 注入一条记录后 GET /learning/cooldowns，断言 recent_patterns 非空。 |

**轮次验收门槛**: 部署后 24h 复核 trace：stale_drop 中 unknown_thread 占比应≈0、instant backfill 降级日志应消失、proactive trace 应出现非空 dispatch_status、compaction 首试 ProviderNotFoundError 应消失。

## Round 2：人工校准闭环与记忆数据质量

共 10 项（P1, P2）

| ID | 级别 | 标题 | 最小修复边界 | 建议验证 |
|---|---|---|---|---|
| **WU-01** | P1 | 表达审核"编辑通过/编辑驳回"静默丢弃人工修改后的表达文本（replacement 不随 approve/reject 落库） | astrmai/webui/backend/services/review_ui_service.py::submit_review（将 replacement/apply_replacement 并入 extra_update 或在主路径调用 service.update_review）；或 le | 单测：facade.submit_expression_review 返回含 id 的 dict，service.update_review 桩记录 kwargs，断言 replacement_expression==编辑文本且 apply |
| **WU-02** | P1 | 审核权重输入被当作"相对 1.0 的增量"应用，每次编辑通过都使权重漂移（clamp 3.0） | review_ui_service.py::submit_review——先 get_pattern 取当前权重再算 delta（与降级路径同法），或给 facade 链路增加绝对权重参数。 | 单测：current weight=2.0，提交 weight=2.0，断言最终 weight==2.0（现行代码会得 3.0）。 |
| **WU-03** | P1 | pending_human 表达候选计入"待审核"徽标但不出现在人工待审队列（auto-check 升级人工的候选不可见） | learning/review/review_service.py::list_pending_reviews——canonical 分支改为独立查询（statuses=review_pending，review_status∈{pending,revision_needed,pending_hum | 构造 review_status=pending_human 的 expression_pattern，GET /astrmai/admin/reviews/pending 应包含它；expression_auto_check_task.r |
| **WU-04** | P1 | MemoryMaintenanceService.run_once（索引一致性修复+黑话/表达积压过期清理）无任何调度器，唯一入口是前端从不调用的 WebUI 端点 | astrmai/proactive/proactive_task.py（或 dream_scheduler）——在日常低峰任务里调用 memory_engine.maintenance_service.run_once()；或至少在管理页记忆质量面板加"执行维护"按钮接通现有端点。 | grep -rn "run_once" astrmai \| grep maintenance 确认调用方；接通后观察 /memories/quality/overview 的 index 异常数归零、review_pending 超期条目减 |
| **WU-05** | P2 | 表达审核通过/驳回不同步召回索引投影（jargon/canonical 路径都同步，唯独 expression 缺失） | expression_pattern_service.py::update_review / ReviewUiService 层——审批状态变化后按 jargon 同款调用 projector.project/cleanup_deleted（service 需可访问 projector，或在 Rev | 审批一条表达后调用 GET /memories/diagnostics/index，missing_projection_ids 不应包含该 id。 |
| **WU-07** | P2 | 黑话"驳回并删除"硬删除抹掉 rejected 墓碑，挖掘器会把同一噪声词重新捞回待审队列 | memory_ui_service.py::reject_jargon——改为置 status=rejected（软墓碑，交给维护 purge 7 天 grace）而非直接 hard_delete；或 hard_delete 时把词写入独立 rejected_terms 表供 miner 去重。 | 驳回一条黑话后对同群跑 run_expression_backfill/挖掘，断言 existing_terms 仍包含该词、候选不重现。 |
| **WU-10** | P2 | 黑话/表达关键字搜索只过滤当前页：服务端 query 过滤发生在 LIMIT/OFFSET 之后，total 用未过滤总数 | memory_ui_service.py::list_jargon——query 下推为 SQL LIKE/FTS（过滤后再分页并返回过滤后 total）；app.js loadReviews 表达 tab 把 keyword 作为 /reviews?keyword= 传给后端（后端已实现）。 | 造 30+ 条黑话使匹配项落在第 2 页，搜索后第 1 页应显示命中且 total=匹配数。 |
| **ML-03** | P1 | 偏好类权威事实 dedup_key 只到 attribute 级（{uid}:preference:like），新偏好 supersede 旧偏好导致旧事实丢失 | memory_claim_service 偏好类 dedup_key 追加 value 归一片段，或 MemoryConflictResolver 对 like/dislike 禁用 authority_override（走普通 dedup 写入） | 单测：instant gate 连续处理『我喜欢咖啡』『我喜欢猫』，断言 store 中两条 active 偏好 |
| **ML-04** | P1 | 索引清理绕过 FaissVecDB.delete，删除/替换后嵌入向量永不回收——向量召回被幽灵 id 逐渐挤占 | MemoryIndexProjector.cleanup_deleted：查出 documents.doc_id 后改调 engine.faiss_db.delete(doc_id)（或补调 embedding_storage.delete），rebuild 路径同步修改 | 集成测试或线上跑一次 supersede 后查 embedding 行数减少；maintenance run_once 的 index_repair 报告不再出现 orphan 增长 |
| **ML-10** | P2 | 会话摘要主路径（pipeline buffer）说话人解析失败——参与者全部 unknown，群记忆无法归属到人 | memory_turn_pipeline.record_turn 存结构化 dict（sender_id/sender_name/text）替代拼接字符串；或 _build_topic_messages 增加『用户/旁白：sender: text』解析分支 | 单测：record_turn 两轮后 run_maintenance，断言 topic_messages sender 非 unknown 且摘要 metadata.speaker_ids 非空 |

**轮次验收门槛**: WebUI 上人工操作全链路手测：编辑通过保留文本、权重不漂移、pending_human 出现在队列、维护任务可触发且 /learning/cooldowns 非空；DB 抽样确认偏好不再互相覆盖。

## Round 3：延迟预算统一与模型调用成本

共 10 项（P1, P2）

| ID | 级别 | 标题 | 最小修复边界 | 建议验证 |
|---|---|---|---|---|
| **RT-03** | P1 | mood LLM 串行前置于 judge 且与 judge 内嵌 mood 双重计算：364 次调用中约 302 次花在最终不回复的消息上，构成群聊 ingress p50 4.4s 延迟 | gate._apply_primary_mood_update / decision_router.evaluate：将独立 mood LLM 调用改为 (a) 复用 judge 返回的 mood_tag/mood_delta，或 (b) 与 judge 并行 gather，或 (c) think_ | 回放 trace 统计 mood 池调用数应降至≈executed 轮数；attention.dispatch p50 应降到 <1s。 |
| **RT-04** | P2 | gateway.tool（dialog 主回复/工具环）完全不受 turn 预算约束，与 gateway.chat 的预算语义不一致 | gateway_lane.tool_chat_in_lane_result：attempt 循环前 clamp（reserve_for_reply=False，因为它本身就是 reply），预算<主回复保留额时用保留额兜底而非直接失败。 | 构造 deadline 已过的 event 调 tool_chat_in_lane_result，断言 timeout 被 clamp 至保留额。 |
| **RT-05** | P1 | 视觉链路重试乘法（框架5×网关3×池7模型）+ executor 旁路无超时 + 合并循环重置屏障 deadline：单图可烧掉整个 360s 轮预算 | ①executor._analyze_direct_images 传 timeout_override 并套用 vision_barrier_total 类似的总额；②coordinator 屏障 deadline 存到 session/事件级别，合并迭代不重置；③call_vision_task  | 模拟 502 provider 重放图片消息，断言总视觉耗时≤配置总额且轮长≤budget。 |
| **RT-06** | P2 | cognitive_loop 在默认 think_level=1 上仍串行运行（8-35s LLM），think 分级未覆盖此高频成本 | cognitive_loop.gate_decision 门槛提为 >=2（或 level 1 仅在含复杂度信号时放行）；或在 chat_loop_kernel 将其与 context_build 并行 gather。 | 回放 trace 比较 executed 轮 turn_total_elapsed_ms p50 与 cognitive_loop 池调用数变化。 |
| **RT-09** | P2 | judge prompt 缓存敌对结构未修：539 次调用 × p50 1977 字符动态段内嵌 1.4K 固定 rubric，前缀命中 0-25% | judge.py + judge_prompt.py：把动作说明/维度 key/JSON schema/mood 说明并入 JUDGE_STABLE_PREFIX（system），动态段只留 mood 数值、历史与消息且置于最尾。 | 重排后统计 judge 池 usage_input_cached/usage_input_tokens 应显著>0。 |
| **RT-11** | P2 | 全局 LLM 信号量(3) 把 ambient judge/mood 与主回复混排，skipped 轮 judge 条目出现 30-51.7s 排队 | model_gateway：拆分信号量（critical_path 独立配额或优先队列），并在 ledger metadata 记录 semaphore_wait_ms 以便定量。 | 加 semaphore_wait_ms 后统计 executed 轮 dialog 的等待应≈0。 |
| **ML-02** | P1 | think>=3 深检索串行 rewrite(8s必超时)+rerank+guidance 两次无超时 LLM，注入耗时 50~92s，3 次深检索 2 次以 stale_drop 丢弃回复 | memory_retrieval_service._call_deep_json 加 timeout_override=clamp_timeout_to_turn_budget(reserve_for_reply=True) 与 lane_key；_rerank_candidates 在 len(c | 构造 think3 消息实测 memory.injection stage 耗时 <10s 且 turn 不再 stale_drop；单测断言 _call_deep_json 传入了 timeout_override |
| **ML-06** | P2 | 发送后内联 claim 抽取 LLM 在 turn 任务内同步执行（实测 5.2~44.5s×7），拖长 turn 与 per-chat 后续处理 | reply_post_send._ingest_memory_turn 的 instant-gate LLM 部分改投递到 pipeline 后台 worker（依赖 ML-01 先修，否则后台必死） | 修复后 trace 中 executed turn 的 ledger 不再出现 memory_global_summary 长调用，turn_total 相应下降 |
| **ID-09** | P2 | 私聊回复中位延迟 44s：settle→mood→judge→cognitive→tools 五段串行，且私聊 judge 16h 内 0 次非 REPLY 纯属延迟 | gate._debounce_and_judge 私聊分支：mood 更新改 fire-and-forget 或与 judge 并行；私聊默认 should_skip_judge=True（保留可配置开关）；发送后记账移出 turn 关键路径统计。 | 对比修改前后私聊 trace reply_age p50；断言私聊 executed turn 关键路径 LLM 调用数从 4-5 降至 2-3。 |
| **TL-04** | P1 | gateway 层 side-effect 中止保护被 executor 模型级联绕过：space_transition 可能向好友重复真发私聊，失败尝试排队的动作随 fallback 提交 | executor._run_tool_mode except 分支：检查 _tool_side_effect_count(event)（或 LLMCascadeFailureException 增加 side_effect 标志）> 进入循环前基线时停止级联、改走 _handle_required_ | 单测模拟：ToolSet 内工具 call 时向 event 写 cross_session_sends 后 gateway 抛 LLMCascadeFailureException，断言第二模型未被调用或未再触发 send_message |

**轮次验收门槛**: 部署后 24h 复核 trace：非回复轮 LLM 调用占比显著下降（基线 88%）、gateway.tool 受预算约束（预算 0 时拒发）、vision 轮 turn_total_elapsed_ms P95 < 120s、memory.injection P95 < 30s。

## Round 4：配置落地、容错与生命周期

共 9 项（P1, P2, P3）

| ID | 级别 | 标题 | 最小修复边界 | 建议验证 |
|---|---|---|---|---|
| **PL-02** | P2 | 主动链三层诊断全误标：日志称 'skipped by planner'、dispatcher status=skipped 无原因、trace proactive 字段恒空 | gate._finalize_pre_planner_turn 内补 proactive 上下文填充（与 planner._apply_proactive_context 同源化）；wakeup 日志按 blocked_reason 分流措辞。 | 注入合成事件后检查 trace.proactive.is_proactive=True 且 blocked_reason=sensor_filtered。 |
| **PL-03** | P1 | UI '合并私聊连续输入' 开关是死键：timing.turn_merge_enabled 被 pydantic 静默丢弃，无法关闭合并 | config.py LEGACY_TIMING_NAMESPACE_FIELDS 增加 ('turn_merge_enabled','private_chat','turn_merge_enabled') 并在 TimingConfig 增加该字段（bool, default True）。 | AstrMaiConfig(**{'timing':{'turn_merge_enabled':False}}).private_chat.turn_merge_enabled 应为 False。 |
| **PL-04** | P1 | '启用基础内容安全过滤（NSFW/自残/PII 检测）' 是虚假开关：全仓库不存在任何实现 | 二选一：在 reply_service/output_guard 实现最小过滤并接开关；或从 schema+config.py 删除该键并在变更说明标注。 | 开启开关后发送含测试敏感词的回复，断言被拦截或改写。 |
| **PL-05** | P2 | 另 7 个死配置键：debounce_window/max_message_length/repeater_threshold/throttle_probability/throttle_min_entropy/enable_relationship_engine/unknown_decay | 从 _conf_schema.json + config.py 移除 7 键，或将 repeater/debounce/throttle 逻辑改回读配置（gate.py/window_buffer.py/energy_manager.py 函数级）。 | config_consumption_matrix.md ① 清单复跑脚本应为 0 项。 |
| **PL-06** | P2 | 越界配置=插件整体拒载：AstrMaiConfig 校验异常未捕获，且约 90 个数值键 UI 无范围约束提示 | main.py __init__：捕获 ValidationError，剔除违例字段回退默认并 logger.error 逐项汇总；长期为 schema 补齐 min/max。 | 注入 {'infra':{'api_timeout':-5}} 实例化插件，断言插件可用且日志含降级警告。 |
| **PL-09** | P2 | 插件重载即短期上下文失忆：GroupDialogueStore/压缩链纯内存，AstrBot 侧任何配置保存都清零并掐掉在飞 turn | terminate 时将 dialogue_store 热/温区快照写入 cache 目录、启动时按 TTL 恢复（对齐 dream_scheduler_state.json 的做法）；或文档明示重载副作用。 | 改配置触发重载后，对同群提问上一分钟话题，bot 应能接上。 |
| **PL-10** | P3 | PluginLifecycleManager._terminated 永久闩锁：同实例 terminate 后 on_program_start 永拒，无解除路径 | on_program_start 允许 _terminated 状态下重置标志重启（或 facade 在 initialize 时重建 LifecycleManager）。 | AstrBot 面板禁用再启用插件（不重启进程），确认消息仍被处理。 |
| **PL-11** | P3 | agent.max_steps 被 executor 静默钳制到 >=5：schema/pydantic 允许 1-4 但无效 | executor._execution_runtime_values：尊重配置或把 pydantic/schema 下限提到 5 并更新 hint。 | 设 agent.max_steps=2，观察工具循环最多 2 步。 |
| **RT-08** | P2 | provider 能力解析全量失败（provider=unknown 1005/1005）：cache_control/provider session 特性被静默关闭，观测字段失真 | provider_capabilities.resolve_provider_capabilities：字符串回退前按 '/' 拆 provider 段并查 context.get_all_providers()（或 provider_manager）按 id 前缀匹配 provider 对象/ty | 启动后 GatewayUsage 行 provider 字段出现 native_chat/gemini 等真实家族。 |

**轮次验收门槛**: 构造越界配置值加载插件应裁剪+告警而非拒载；UI 改 timing.turn_merge_enabled 应真实生效；重载后短期上下文恢复或有明确降级日志。

## Round 5：观测契约完整性与跨模块回归测试

共 10 项（P1, P2, P3）

| ID | 级别 | 标题 | 最小修复边界 | 建议验证 |
|---|---|---|---|---|
| **TG-01** | P1 | 群聊身份隔离无端到端回归：speaker block、关系数据、终线 guard 三个身份来源各自单测，无一测试断言三者指向同一 sender | 新增 tests/regression/conversation/test_group_identity_isolation_e2e.py（gate._debounce_and_judge + planner._prepare_plan_context + executor._finalize_re | python -m pytest tests/test_executor_refactor.py -k actor -q（现状）；新测试落地后跑该文件 |
| **TG-03** | P1 | Turn 总预算端到端零守护：配置接线、网关耗尽分支、judge 耗尽降级三个执法点均无测试，接线失败会静默让预算失效 | 新增 tests/test_turn_budget_e2e_refactor.py（message_entry._configure_turn_budget + gateway_call + decision_router 三点） | python -m pytest tests/test_turn_call_ledger_refactor.py -q |
| **TG-04** | P2 | trace v2 memory_funnel 在 executed turns 中 64/67 缺失（prompt_refiner 7 条 early-return 不写 funnel），且无字段完整性契约测试；context_block_stats 的 511/585 缺失系误报 | prompt_refiner.py::_resolve_memory_injection 各 early-return 前写 skipped funnel（或由 planner 在无 funnel 时补 skip 占位）+ tests/test_turn_trace_store_v2_refacto | PYTHONIOENCODING=utf-8 python scripts/analyze_turn_ledger.py 后看 missing.memory_funnel 应≈skipped 数量而非全量 |
| **TG-05** | P2 | 记忆闭环缺'修订'腿：WebUI update_canonical 修订内容→projector 重投影→检索/注入反映新内容 无任何测试（WebUI 测试全部 projector=None 或 mock store） | tests/integration/test_memory_write_retrieve_inject.py 追加 1 条修订闭环测试（真实 store+projector+retrieval+MemoryUiService） | python -m pytest tests/integration/test_memory_write_retrieve_inject.py -q |
| **TG-06** | P2 | WebUI 前后端契约无自动对齐校验：前端 app.js 75 个 api 路径 vs 后端注册表，测试只有手工镜像清单+JS 字符串 pin，历史已有 ≥4 例 FE/BE 漂移 bug | tests/test_plugin_pages_admin_refactor.py 新增 1 条 <50 行的静态对齐测试 | python -m pytest tests/test_plugin_pages_admin_refactor.py -q |
| **TG-07** | P2 | 4da2910 私聊 vision barrier 的 gate 消费侧组合分支无测试：屏障期间新消息 re-merge 续跑、abort 后池非空续跑、resolve 超时 outcome | tests/test_attention_gate_refactor.py + tests/unit/conversation/test_private_turn_coordinator.py 各加 1-2 条 | python -m pytest tests/unit/conversation/test_private_turn_coordinator.py tests/test_attention_gate_refactor.py -q |
| **ID-05** | P2 | stage_ledger reply.send 的 sent_segment_count 恒为 0（满发路径从不写 metadata）——确认为 instrumentation bug，无真实丢段 | reply_artifact_builder._send_segments 发送循环结束后无条件写 artifact.metadata['sent_segment_count']=sent_segment_count。 | 发一条多段回复，断言 stage_ledger reply.send metadata.sent_segment_count == reply_stats.sent_segment_count == 段数。 |
| **RT-02** | P2 | analyze_turn_ledger.py judge 口径错误（按 stage 匹配），judge_calls_per_turn=0 掩盖了仍存在的同轮多次 judge（真实 p50=1/p95=2/max=10） | analyze_turn_ledger.analyze_traces 判定改为 pool=='judge' or stage=='attention.judge'；gate.py 焦点选择对连续 IGNORE 的 focus 加冷却/降权。 | python scripts/analyze_turn_ledger.py <traces> 输出 judge_calls_per_turn_p50>=1。 |
| **RT-10** | P3 | 观测字段小缺陷簇：prefix_changed_reason 稳定轮被标 unavailable_in_trace、63 次 attention.dispatch abandoned 为快照顺序伪影、trace created_at 是捕获时刻 | planner.py L263 改为空串时置 'stable'；finalize_turn_telemetry 跳过名为 attention.dispatch 的外层 stage 或延迟快照；trace 增加 turn_started_at 已有——分析工具应使用它（scripts/analyze_ | 重放后 executed 轮 prefix_changed_reason 分布应为 stable/first_seen/frozen_rules_or_persona_changed。 |
| **WU-06** | P2 | TurnTrace 样本库每条消息全文件读改写（15MB 实测，封顶约 42MB），与 WebUI 45s 轮询读共用一把锁 | infrastructure/runtime/turn_trace_store.py——改 append-only JSONL 分片或 SQLite 表；短期缓解：去掉 indent、去掉 recent/by_chat 双份、skip 类 turn 存精简摘要、append 改后台队列。 | 压测：max_global 填满后测单条 append 耗时；观察消息 p95 延迟变化；WebUI /cognition/recent-turns 响应时间。 |

**轮次验收门槛**: 新增回归测试全绿；trace 契约字段（memory_funnel/sent_segment_count/prefix_changed_reason）在 executed 轮次填充率 = 100%；analyze_turn_ledger 的 judge 口径与人工抽查一致。

## Round 6：工具链路与交互打磨

共 10 项（P2, P3）

| ID | 级别 | 标题 | 最小修复边界 | 建议验证 |
|---|---|---|---|---|
| **TL-01** | P2 | 二段披露展开机制 585 轮/16h 零触发：唯一入口是模型主动调 bot_capability_lookup 且需整轮重跑，实践中不可达 | planner.py::_append_tool_guidance 增加一行『工具不够时调用 bot_capability_lookup(needed_package=...)』提示；或 planner_side_inputs._build_execution_tools 在识别到 identity | 回放 trace 统计 disclosure_expanded_packages 与 identity/relationship 工具执行次数由 0 转正；单测 guidance 文案包含提示。 |
| **TL-02** | P2 | social_intent(tease/comfort) 家族过滤清空披露层为图片/引用消息加的 artifact 工具，连 core 查询与 wait_and_listen 一并剥除 | planner_side_inputs._build_execution_tools：把 message_artifact/vision_message（及 wait）家族并入 intent_families 的白名单，或在 disclosure_reasons 含 message_artifact | 构造 has_image=True + social_intent=tease 的单测断言 vision_message_analyze_tool 在 filtered_tools 中；回放 trace 观察图片轮 filtered_too |
| **TL-03** | P2 | sanitized execution event 将消息组件替换为 Plain 占位，vision/artifact 工具的『当前消息』路径必然假阴性 | executor._build_sanitized_execution_event：在 sanitized event 上以 extra（如 astrmai_original_message_segments）保留原始组件供工具读取；或 pfc_tools 的当前消息分支优先读该 extra。 | 单测：构造带 Image 段事件 → sanitize → VisionMessageAnalyzeTool.call 应返回图片段信息而非『没有发现』。 |
| **TL-06** | P2 | 『听说/据说/有人说/不确定』日常词直接构成 unverified_report 显式工具意图：升级 task tier 并强制 required 工具 | planner_side_inputs.GENERAL_EXPLICIT_TOOL_KEYWORDS：unverified_report 触发词收紧为组合模式（如需同时含转述源+断言结构），或将该家族从 required 降级为 optional（explicit_policy 保持 require | 单测負样本 + 回放线上一周 trace 统计 unverified_report required 触发率与其中误触占比。 |
| **TL-07** | P2 | perception.image_count 在全部 585 traces 恒 0，图片轮在观测层不可辨识 | turn_context perception 装配点（sensors/ingress 或 planner 侧）用与 disclosure 相同的来源为 image_count 赋值。归属 observability/ingress 域，此处立据。 | 发一条图片消息，断言 trace perception.image_count>=1。 |
| **TL-08** | P3 | FAMILY_TO_PACKAGES['quote_reply'] 是死配置：quote_reply 属 PRECISION_ONLY，包映射永不生效，引用场景无自主 quote 能力为纯关键词依赖 | tool_disclosure.py：删除 quote_reply（及其它 PRECISION_ONLY 家族）在 FAMILY_TO_PACKAGES 的映射，或加模块级断言保证三表一致。 | 新增一致性单测：PRECISION_ONLY_FAMILIES ∩ 有效包映射 = ∅。 |
| **TL-09** | P3 | 跨会话 handoff 仅内存驻留且注入块 360 字符截断，三方消歧指令位于截断尾部 | planner_side_inputs._apply_private_jump_context：把消歧指令移到块首或单独 clamp；handoff 落盘可选（persistence 增一张小表，lifecycle 恢复）。 | 单测：context_summary/message 各 90 字符时断言注入文本包含『收件人』消歧句。 |
| **ID-04** | P2 | 私聊 prompt 被硬编码'群聊/群友/群里'话术污染（warm/cold 摘要模板不分场景） | group_dialogue_store._extract_warm_topic_units / _build_warm_summary + context_compaction._structure_from_segments 增加 is_private 判断（'FriendMessage' in | 私聊发两条消息后检查 trace warm_summary_preview 不再含'群聊/群友/群里'。 |
| **ID-08** | P3 | 撤回(recall)通知零处理：被撤回消息原文继续留在对话上下文中可被 bot 引用 | message_entry notice 分类新增 recall 路由 → group_dialogue_store 按 event_id 打 tombstone（内容替换为'[已撤回]'，保留 speaker）。 | 群里发消息→撤回→@bot 询问，检查 bot 不引用原文。 |
| **ID-10** | P3 | poke 目标解析兜底把'戳别人(目标缺失)'误记为'戳 bot'：无端回戳+好感度误结算 | sensors.process_poke_event：target 不可解析时标记 target_unknown，跳过回戳与好感结算；_resolve_name 过滤超长/含空格的名片文本。 | 单测：payload 无 target_id 的 poke，断言不回戳、不结算好感。 |

**轮次验收门槛**: 图片轮 vision 工具可见性手测；'听说'类日常消息不再触发强制工具轮；撤回消息不再被引用。

## Round 7：学习/Dream 治理与设计决策项

共 10 项（P1, P2, P3）

| ID | 级别 | 标题 | 最小修复边界 | 建议验证 |
|---|---|---|---|---|
| **ML-05** | P1 | think 门 + 窄关键词门使记忆注入率仅 2.9%（私聊 0/19）：正常聊天读不到已写入的记忆 | prompt_refiner think1 门放宽：复用 MemoryQueryBuilder.QueryIntentClassifier，identity/preference/location 意图也放行；可选为 think0 私聊提供 FTS-only 轻量注入 | 回放私聊『我叫什么名字』应产生 memory.injected=true；统计一周 trace 注入率回升到目标区间 |
| **ML-07** | P2 | Dream 每轮往真实会话写『[dream_maintenance] 完成 N 次维护动作』运维噪声记忆；LLM 合并叙事不经 admission 治理直接 active | dream_scheduler: maintenance 摘要改写 __dream_diary__ 或仅存 meta；dream merge 写入走 admission（源加入治理名单） | 跑一次 dream 后检索该会话，无 [dream_maintenance] 候选 |
| **ML-08** | P2 | Dream 事实晋升的 3 证据阈值可被单次 LLM 响应内重复项满足，写出 confidence=1.0 权威事实并可覆盖用户亲述事实 | promotion_engine._iter_detected_facts 按 (key, evidence.turn_id) 去重；confidence 取证据实际置信度上限而非 1.0 | 单测：detected_facts 含同一事实 3 份 → 不晋升；3 个不同 turn 证据 → 晋升 |
| **ML-09** | P2 | 挖掘 fail-closed 无毒丸跳过：坏批次每 30min 原样重试可永久卡死单群学习；新窗口 16.6h 零挖掘日志，链路是否存活不可观测 | evolution_manager: _backlog_failure_until 增加按群失败计数，>=3 次跳过头部 min_mining_context 条并标记 processed；run_backlog_mining_once 成功时打 INFO 摘要 | 注入必失败的 enricher stub，断言第 4 次重试后 head 批被跳过且 backlog 下降 |
| **ML-11** | P3 | 演示场景启发式硬编码进生产：server_count=(\d+) 任意数字、火锅/芒果/蓝色词表进入 claim 规则与检索重排 | claim_rules/claim_rules_zh 移除或收紧 server/anxiety 规则（要求『我有/我的+数量词+服务器』句式）；词表迁到配置 | 单测负例通过；相关既有测试（server 场景）同步更新 |
| **WU-09** | P2 | 空数据三义性：前端把错误回退缓存成 180 秒"新鲜空数据"；runtime_bound:false 与真无数据渲染完全相同 | app.js::cachedFetch（错误回退不写缓存或标记 stale=true 下次强制重试）+ table()/asItems 空态透出 runtime_bound=false 与"加载失败"两种专属文案；后端 list_canonical 回退分支返回 status:degraded。 | 断网/停插件复现：首次 toast 后 3 分钟内页面固定空白；修复后应显示错误态并可重试。 |
| **WU-11** | P3 | trace v2 新字段（llm_call_ledger/stage_ledger/reply_stats/budget/memory_funnel）已随 API 返回但管理页零呈现；工具披露表"工具"列恒为"-" | app.js::openTurnTrace——增加 LLM Calls/Stage Ledger/Reply Stats/Budget 区块（或至少 detailsJson 全量兜底）；renderDashboardTools 披露行改用 (item.tool_names\|\|[]).join(',  | 打开任一 executed turn 详情，应能看到 llm_call_ledger 表与 budget.remaining_ms。 |
| **WU-12** | P3 | 计数口径与删除反馈错位集合：表达 total 含已删行、"黑话全量"实为已通过、legacy 事件删除返回 readonly 却 toast 已删除、Dashboard 待审仅统计表达 | runtime_memory_stats.py（total 限定 active+review_pending 或分列展示）；app.js 删除回调检查 result.status==='readonly'/changed===false 时改提示；"黑话全量"改名"黑话词库（已通过）"。 | 对含 deleted 行的库比对学习页 total 与 SELECT COUNT(*) WHERE status IN ('active','review_pending')；删除 legacy 事件观察提示语。 |
| **TG-08** | P3 | 测试基建健康核实：收集 1673 条 0 错误、manual 脚本未腐化；session-state.md 测试计数(1142)过期 | .agent/session-state.md 更新 Test Status 小节 | python -m pytest --collect-only -q \| tail -1 |
| **ID-07** | P3 | group_wait 残留不对称：reply: 键位等待无法被目标的普通跟进复活，unique-target 兜底因 has_explicit_thread 恒真成死分支 | group_reply_wait_manager.handle_incoming_message L329 条件放宽为 not incoming_thread_signature（turn_thread_id 不算 explicit），或 register 时同时登记 sender:<target> | 单测构造 reply: 键等待 + 目标纯文本跟进，断言 RESUME。 |

**轮次验收门槛**: 产品决策记录归档；dream 噪声记忆停止增长；mining 毒丸场景注入测试通过。
