# AstrMai 最终功能审计：Memory Write Governance

## 审计结论

本报告基于当前工作树（包含未提交的生产代码变更）审计 `astrmai/memory/services/` 中未归属报告 05 的写入与治理路径，以及 `astrmai/memory/dream/`、`astrmai/memory/persona/` 和相关生命周期、配置热更新、主动维护调用点。仅在需要证明真实生产调用链时读取相邻模块；`astrmai/infrastructure/security` 视为不透明依赖。

共确认 **13 项可达功能缺陷**：**P1 4 项、P2 8 项、P3 1 项**。未发现 P0 缺陷。

## Finding 06-01：群聊提交回合未传入发送者 ID，权威事实会跨成员互相覆盖

- **ID / 严重级别**：AM-MEM-06-01 / **P1**
- **文件:行**：`astrmai/conversation/execution/reply_post_send.py:115`；`astrmai/memory/services/memory_turn_pipeline.py:78`；`astrmai/memory/services/instant_memory_gate.py:110`；`astrmai/memory/services/instant_memory_gate.py:150`；`astrmai/memory/services/v2_store.py:535`
- **触发条件**：群聊中两个或更多成员先后表达会被即时门识别的同一实体/属性事实，例如各自的身份、偏好或联系方式。
- **真实调用链**：`ReplyService` 成功发送回复 → `ReplyPostSendService._ingest_memory_turn()` → `MemoryTurnPipeline.build_turn()` → `record_turn()` → `process_instant_gate()` → `InstantMemoryGate.process()` → `MemoryWriteService.write()` → `MemoryV2Store.upsert_record()`。
- **实际行为**：`_ingest_memory_turn()` 构造回合时没有传入 `event.get_sender_id()`；`build_turn()` 将 `sender_id` 保持为空。即时门随后用 `turn.sender_id or turn.chat_id` 生成 `subject_id`，因此同一群内所有成员都落到群会话 ID。权威去重键为 `subject:entity:attribute`，后写入成员会把前一成员的同属性事实判定为同一权威槽位并覆盖/取代。
- **期望行为**：群聊事实应以真实发送者作为主体；同一群内不同成员的同属性事实必须具有不同的规范主体和权威去重键。
- **生产影响**：形成跨用户记忆污染和事实丢失；后续上下文注入、主动行为和 Dream 治理可能把甲的属性归给乙，且后一次写入会使前一事实失去当前权威状态。
- **现有守卫为何失效**：权威冲突与去重逻辑只验证规范键是否相同，它假设上游已经提供正确主体；空 `sender_id` 被合法回退为 `chat_id`，不会触发拒绝或降级。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 06-02：正常卸载会直接丢弃未达到摘要阈值的已提交回合

- **ID / 严重级别**：AM-MEM-06-02 / **P1**
- **文件:行**：`astrmai/memory/services/memory_turn_pipeline.py:38`；`astrmai/memory/services/memory_turn_pipeline.py:57`；`astrmai/memory/services/memory_turn_pipeline.py:107`；`astrmai/app/lifecycle.py:209`
- **触发条件**：会话已经成功发送并记录若干回合，但累计数量尚未达到摘要阈值时发生插件热重载、正常卸载或进程停止。
- **真实调用链**：回复成功 → `ReplyPostSendService._ingest_memory_turn()` → `MemoryTurnPipeline.record_turn()` 将消息放入 `_session_history_buffer` → `PluginLifecycle._terminate_impl()` → `memory_pipeline.stop()`。
- **实际行为**：普通回合只保存在进程内 `_session_history_buffer`。`stop()` 取消工作任务、清空队列与任务集合，但没有对该缓冲区执行末次摘要、持久化或可恢复交接。默认阈值下，一个会话最多可有 29 个已提交回合在正常卸载时永久消失。
- **期望行为**：生命周期结束前应把非空会话缓冲区可靠落地，或保留可在下次启动继续消费的提交记录；正常卸载不应成为数据丢失事件。
- **生产影响**：短会话和低频会话最容易永远无法进入长期记忆；重载较频繁时，摘要写入会持续缺段，造成时间线与用户事实不完整。
- **现有守卫为何失效**：即时门只覆盖少量显式事实，不等价于回合摘要；`stop()` 关注任务取消和容器清理，没有对内存缓冲区设置 drain/flush 阶段，也没有重放来源。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 06-03：每日规范记忆衰减调用缺少必需参数，每天首次执行必然失败且当日不再重试

- **ID / 严重级别**：AM-MEM-06-03 / **P1**
- **文件:行**：`astrmai/proactive/decay_service.py:59`；`astrmai/proactive/decay_service.py:63`；`astrmai/proactive/decay_service.py:66`；`astrmai/memory/memory_engine.py:668`
- **触发条件**：主动维护循环运行到每日记忆衰减时间窗。
- **真实调用链**：主动任务维护循环 → `DecayService.run_if_due()` → `memory_engine.apply_daily_decay()`。
- **实际行为**：`MemoryEngine.apply_daily_decay()` 要求必填 `decay_rate`，调用点未传参数，因而抛出 `TypeError`。服务在调用前已经更新 `_last_memory_decay`，异常随后被捕获，所以同一天后续维护周期不会再次尝试。
- **期望行为**：调用应传入当前配置的衰减率，并只在操作成功后推进每日执行时间；临时失败至少应保留后续重试机会。
- **生产影响**：规范记忆的分数衰减和伴随的到期清理长期不发生，旧数据持续保持过高权重并积累，影响检索排序、容量治理和后续 Dream 判断。
- **现有守卫为何失效**：异常捕获避免了维护循环崩溃，却没有恢复执行标记；时间守卫因此把失败当成当日已完成。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 06-04：旧版 documents 迁移读取了规范库而非旧库，并把“表不存在”永久记为已迁移

- **ID / 严重级别**：AM-MEM-06-04 / **P1**
- **文件:行**：`astrmai/memory/memory_engine.py:69`；`astrmai/memory/memory_engine.py:91`；`astrmai/memory/memory_engine.py:209`；`astrmai/memory/services/v2_store.py:1625`；`astrmai/memory/services/v2_store.py:1632`；`astrmai/memory/services/memory_migration_service.py:173`
- **触发条件**：从含有旧版 `documents` 表的部署升级并首次初始化 v2 规范存储。
- **真实调用链**：`MemoryEngine.initialize()` → `MemoryV2Store.import_legacy_documents()` → 打开数据库并检查 `documents` 表；维护侧的 `MemoryMigrationService._scan_documents()` 使用同一错误数据源。
- **实际行为**：引擎明确把旧 documents 库作为 `legacy_db_path` 传入 store，但导入和扫描实际打开的是 `store.db_path`（v2 规范库）。新规范库没有 `documents` 表时，导入器立即写入“documents table unavailable”的迁移完成标记。真实旧库中的文档没有被读取，后续启动也因标记存在而不再尝试。
- **期望行为**：导入与扫描必须连接 `legacy_db_path`；只有完成真实源表的遍历后才能记录迁移已应用，源暂不可用时不能生成不可逆成功标记。
- **生产影响**：升级前积累的会话摘要和文档不会进入规范 ID、v2 状态与索引投影体系，后续统一维护、迁移核验和治理对这些数据不可见。
- **现有守卫为何失效**：表存在性检查针对的是错误数据库；迁移标记只表达“分支已执行”，没有证明连接的是预期旧库或已消费源记录。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 06-05：三类旧数据导入最多消费 1000 条后即永久标记完成

- **ID / 严重级别**：AM-MEM-06-05 / **P2**
- **文件:行**：`astrmai/memory/memory_engine.py:676`；`astrmai/memory/memory_engine.py:691`；`astrmai/memory/memory_engine.py:730`；`astrmai/memory/memory_engine.py:736`；`astrmai/memory/memory_engine.py:751`；`astrmai/memory/memory_engine.py:785`；`astrmai/memory/memory_engine.py:791`；`astrmai/memory/memory_engine.py:807`；`astrmai/memory/memory_engine.py:837`
- **触发条件**：旧版 `MemoryEvents`、jargon 或 expression patterns 任一来源表中可迁移记录超过默认 `limit=1000`。
- **真实调用链**：启动导入或 `MemoryMigrationService.execute()` → `MemoryEngine.import_legacy_memory_events()` / `import_legacy_jargon()` / `import_legacy_expression_patterns()` → 限量查询 → 写入 v2 → 记录迁移已应用。
- **实际行为**：每个导入器只读取前 1000 条，却无论源表是否还有剩余记录都写入一次性完成标记。后续执行看到该标记直接返回，无法消费第 1001 条及以后数据。
- **期望行为**：应分页直至源数据耗尽，或保存可继续推进的游标；只有确认没有剩余可迁移记录时才标记完成。
- **生产影响**：较长时间运行的实例升级后会静默遗留部分事件、术语和表达模式，遗留记录没有规范 ID 与 v2 投影，治理结果取决于记录在查询排序中的位置。
- **现有守卫为何失效**：`limit` 只限制单次查询，没有“是否还有下一页”的判断；完成标记优先于剩余量检查，重复执行保护反而封死了续跑路径。
- **分类**：confirmed（已确认）
- **置信度**：0.99

## Finding 06-06：`memory.min_memory_confidence` 配置从未进入写入决策

- **ID / 严重级别**：AM-MEM-06-06 / **P2**
- **文件:行**：`astrmai/config.py:186`；`_conf_schema.json:573`；`astrmai/memory/memory_engine.py:198`；`astrmai/memory/services/memory_write_service.py:61`
- **触发条件**：管理员把 `memory.min_memory_confidence` 设为非零阈值，随后即时门、摘要回填、迁移或表达模式产生低于该阈值的写请求。
- **真实调用链**：各生产写入方构造 `MemoryWriteRequest` → `MemoryWriteService.write()` → 内容过滤 → `MemoryV2Store.upsert_record()` → 索引投影。
- **实际行为**：配置和配置描述宣称低于阈值的记忆不会持久化，但 `MemoryWriteService` 没有接收或读取该配置，也未比较请求置信度。所有通过内容过滤的低置信度请求仍然写入规范库并投影。
- **期望行为**：统一写入入口应按当前配置执行置信度门控；阈值为 0 时关闭门控，非零时拒绝低于阈值的请求。
- **生产影响**：管理员无法通过配置控制低可信记忆进入长期状态，低质量事实会照常参与检索、冲突取代和主动行为。
- **现有守卫为何失效**：内容过滤只判断内容类型/格式，不判断置信度；配置值没有被注入写入服务，因此所有上游路径都绕过了声明的全局门槛。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 06-07：表达模式人工替换只改内容不改去重键，后续学习会把旧表达写回来

- **ID / 严重级别**：AM-MEM-06-07 / **P2**
- **文件:行**：`astrmai/memory/services/expression_pattern_service.py:50`；`astrmai/memory/services/expression_pattern_service.py:245`；`astrmai/memory/services/expression_pattern_service.py:298`；`astrmai/memory/services/expression_pattern_service.py:337`；`astrmai/memory/services/expression_pattern_service.py:377`；`astrmai/memory/services/v2_store.py:449`
- **触发条件**：人工审核表达模式时提供替换文本，之后旧表达或替换后的表达再次被生产学习路径观察到。
- **真实调用链**：审核入口 → `ExpressionPatternService.update_review(replacement=...)` → `MemoryWriteService.write()` 更新原记录内容；之后表达学习 → `write_pattern()` → 按表达计算 dedup key → 查找/合并 → `MemoryV2Store.upsert_record()`。
- **实际行为**：审核替换更新了 `content`，但沿用由旧表达生成的 `dedup_key`。旧表达再次出现时会命中该记录并把内容更新回旧表达；替换后的表达再次出现时使用新键，可能另建一条记录，造成已审核语义回退和重复状态。
- **期望行为**：替换文本与其规范去重身份应原子变更；后续旧表达不能覆盖审核结果，替换后的表达应继续累积到同一规范记录。
- **生产影响**：人工纠正无法稳定生效，表达学习结果会随后续流量反复回退或分裂，最终注入人格表达的内容不符合审核决定。
- **现有守卫为何失效**：upsert 保证的是“同一 dedup key 合并”，但键与当前内容已经失配；审核状态元数据不会阻止该键上的后续内容更新。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 06-08：Persona 提示词在 ID 不变时更新，摘要缓存永久返回旧内容

- **ID / 严重级别**：AM-MEM-06-08 / **P2**
- **文件:行**：`astrmai/memory/persona/persona_summarizer.py:174`；`astrmai/memory/persona/persona_summarizer.py:204`；`astrmai/memory/persona/persona_summarizer.py:247`；`astrmai/conversation/context/context_engine.py:313`
- **触发条件**：运行中修改当前 Persona 的 prompt/设定，但保持同一个 Persona ID。
- **真实调用链**：`ContextEngine` 每轮取得当前 Persona ID 与 prompt → `PersonaSummarizer.get_summary(persona_id, prompt)` → 命中 `summary_cache[persona_id]` → 直接返回旧摘要和旧 shards。
- **实际行为**：缓存键只有 Persona ID。首次生成时虽保存 `raw_hash`，后续快速路径从不把当前 prompt 的哈希与缓存哈希比较；只要缓存被视为完整，就不会重新生成。
- **期望行为**：同一 ID 的源 prompt 发生变化时应使摘要、风格分片和 self-lore 失效并基于新内容重建。
- **生产影响**：管理员修改人格后，对话上下文、表达风格和自我认知仍长期使用旧设定；表面配置已更新，实际人格行为却不随之变化。
- **现有守卫为何失效**：完整性检查只判断缓存字段是否齐全和后台任务是否存在，不验证缓存对应的源内容版本；已保存的 `raw_hash` 没有参与命中条件。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 06-09：Persona 分片生成任务未纳入插件生命周期，卸载后仍可继续调用与写入

- **ID / 严重级别**：AM-MEM-06-09 / **P2**
- **文件:行**：`astrmai/memory/persona/persona_summarizer.py:26`；`astrmai/memory/persona/persona_summarizer.py:204`；`astrmai/memory/persona/persona_summarizer.py:281`；`astrmai/memory/persona/persona_summarizer.py:288`；`astrmai/app/runtime_context.py:315`；`astrmai/app/lifecycle_helpers.py:165`
- **触发条件**：首次生成或刷新 Persona 时后台分片尚未完成，插件在此期间被热重载或卸载。
- **真实调用链**：对话上下文请求 Persona 摘要 → `PersonaSummarizer` 创建 `asyncio` 后台任务并放入 `pending_tasks` → 生命周期终止收集注册任务拥有者 → 持久层/组件继续释放，而该任务不在收集集合中。
- **实际行为**：Persona 任务可继续执行多轮 LLM 调用、清理/新增 self-lore 并持久化缓存。运行时任务拥有者列表不包含 PersonaSummarizer，通用收集器只识别组件 `_background_tasks`，而此处使用 `pending_tasks`，也没有独立 stop/cancel 生命周期方法。
- **期望行为**：插件终止时应取消并等待所有 Persona 后台任务，确保它们不会在所属运行时释放后继续访问服务或与新实例并发写入。
- **生产影响**：热重载期间旧实例与新实例可能同时生成并写 Persona/self-lore；旧任务还可能访问已释放依赖，导致卸载后异常、状态覆盖或跨实例竞态。
- **现有守卫为何失效**：任务有局部集合和完成回调，但该集合未接入全局生命周期协议；完成回调只移除引用，不负责插件终止时取消任务。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 06-10：Dream 的事实产出协议与晋升消费协议断开，正常 Dream 运行无法产生晋升候选

- **ID / 严重级别**：AM-MEM-06-10 / **P2**
- **文件:行**：`astrmai/memory/dream/dream_agent.py:121`；`astrmai/memory/dream/dream_agent.py:139`；`astrmai/memory/dream/dream_generator.py:157`；`astrmai/memory/dream/promotion_engine.py:47`；`astrmai/memory/dream/promotion_engine.py:93`；`astrmai/memory/dream/dream_scheduler.py:111`
- **触发条件**：Dream 定时任务完成一次正常的搜索、思考和生成流程，并进入维护/晋升审计。
- **真实调用链**：`DreamScheduler` → `DreamAgent.run()` 生成日志 → `DreamGenerator.build_maintenance_request()` 提取 `detected_facts` → `PromotionEngine.run_audit()` 扫描候选 → 晋升写入。
- **实际行为**：Agent 日志只生成 `[思考]`、`[行动]`、`[结束]` 段，也没有向模型提供输出 `[fact]` 记录的工具或格式协议；Generator 却只接受以字面量 `[fact]` 开头的行，因此 `detected_facts` 正常为空。PromotionEngine 的补充扫描仅查询 `casual/topic`，并要求候选元数据含 `promotion_entity` 和 `promotion_attribute`；当前这些晋升元数据由事实类写入产生，而 topic 写入路径没有提供它们，故补充扫描也得不到正常候选。
- **期望行为**：Dream 的生产者应明确输出消费者可解析的结构化事实，或晋升器应直接消费实际存在的候选种类/元数据；重复证据达到阈值后应进入冲突与晋升判断。
- **生产影响**：Dream 看似完整运行并生成日记，但重复事实晋升链路长期为空，设计中的后台记忆巩固不发生。
- **现有守卫为何失效**：重复次数、置信度和冲突守卫位于候选生成之后；生产者/消费者契约不匹配使候选在进入这些守卫前就归零。
- **分类**：confirmed（已确认）
- **置信度**：0.98

## Finding 06-11：Dream 默认风格字符串已损坏，并进入模型提示与可见降级文案

- **ID / 严重级别**：AM-MEM-06-11 / **P2**
- **文件:行**：`astrmai/memory/dream/dream_generator.py:17`；`astrmai/memory/dream/dream_generator.py:53`；`astrmai/memory/dream/dream_generator.py:59`；`astrmai/memory/dream/dream_generator.py:122`；`astrmai/memory/dream/dream_scheduler.py:106`
- **触发条件**：定时 Dream 使用默认方式生成日记，调用方没有显式传入 style；模型生成失败时还会走本地降级文案。
- **真实调用链**：`DreamScheduler._run_dream()` → `DreamGenerator.generate_dream()` → 从 `DREAM_STYLES` 随机选风格 → 拼接 LLM prompt；异常/空响应 → 返回 fallback 文案。
- **实际行为**：`DREAM_STYLES` 中的字符串是已损坏的乱码。定时调用始终依赖默认随机选择，这些乱码会进入模型提示；降级文案也直接回显该字符串。
- **期望行为**：默认风格应是可读且语义明确的风格名，提示和用户可见降级结果都不应包含损坏文本。
- **生产影响**：Dream 输出风格约束失真，模型可能忽略或误解要求；生成失败时用户会直接看到乱码日记标题/描述。
- **现有守卫为何失效**：style 只做随机选择和字符串插值，没有有效值校验；异常降级复用了同一损坏值。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 06-12：即时记忆门把固定乱码前缀写入每条命中的规范记忆

- **ID / 严重级别**：AM-MEM-06-12 / **P3**
- **文件:行**：`astrmai/memory/services/instant_memory_gate.py:51`；`astrmai/memory/services/instant_memory_gate.py:52`
- **触发条件**：任意用户文本命中即时记忆门的正则分类规则。
- **真实调用链**：提交回合 → `MemoryTurnPipeline.process_instant_gate()` → `InstantMemoryGate.process()` → 构造内容 → `MemoryWriteService.write()` → v2 规范库与索引投影。
- **实际行为**：规范内容被构造成字面量 `"[????|{category}] ????{text}"`。原文本虽仍在尾部，但每条即时记忆都带有无语义的问号前缀，该内容随后成为规范正文并进入索引。
- **期望行为**：规范正文应使用可读、稳定的分类标记，或直接保存清洁的原始事实文本。
- **生产影响**：即时记忆在检索注入、管理展示、Dream 消费和后续摘要中持续携带污染文本，降低可读性并可能诱导模型复述乱码。
- **现有守卫为何失效**：内容过滤只判断是否允许写入，不会识别该固定字符串为损坏内容；投影器忠实复制规范正文，使污染扩散到检索层。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## Finding 06-13：已声明热应用成功的 Memory/Dream 配置仍被现存子服务继续缓存旧值

- **ID / 严重级别**：AM-MEM-06-13 / **P2**
- **文件:行**：`astrmai/app/plugin_facade.py:185`；`astrmai/app/plugin_facade.py:238`；`astrmai/memory/memory_engine.py:103`；`astrmai/memory/services/memory_turn_pipeline.py:27`；`astrmai/proactive/proactive_task.py:156`；`astrmai/memory/dream/dream_scheduler.py:21`；`astrmai/memory/dream/dream_scheduler.py:256`
- **触发条件**：运行中通过配置热应用修改 `memory.summary_threshold`、维护评分参数、`life.dream_interval_min` 或 `life.dream_visible` 等已参与现有组件构造的值。
- **真实调用链**：配置热应用 → `PluginFacade._apply_hot_config_locked()` → 各组件 `refresh_config()` → 现有 Memory pipeline / summarizer / maintenance / DreamScheduler 继续运行。
- **实际行为**：`MemoryEngine.refresh_config()` 只替换引擎自身配置并刷新少数组件，已经构造的 pipeline、session summarizer、instant gate、maintenance 和 memory tool 仍持有旧配置对象/派生参数。主动任务刷新虽然替换服务配置，DreamScheduler 的 `_dream_interval` 和 `dream_visible` 是构造/单次 configure 时缓存的字段，刷新不会重新计算。因此热应用返回后，相关运行时行为仍保持旧值直至重启。
- **期望行为**：被标记为可热应用的配置应传递到所有存活子服务并重新计算派生字段；若某项只能重启生效，应由热应用流程明确拒绝而不是报告已应用。
- **生产影响**：运维调整摘要触发频率、维护策略、Dream 周期或可见性时观察不到预期效果，可能继续产生不希望的可见 Dream、按旧阈值写入或以旧周期执行。
- **现有守卫为何失效**：热应用只调用顶层 refresh 方法，没有验证所有下游持有者是否更新；字段赋值成功被当作整体生效，缺少对子服务缓存和派生值的传播。
- **分类**：confirmed（已确认）
- **置信度**：1.00

## 已核验的生产路径

- 回复成功后的 committed-turn ingestion、会话缓冲、即时门、批量摘要与后台回填。
- `MemoryWriteRequest` 规范化、规范 ID/dedup key、v2 upsert、权威取代与索引投影。
- 旧 documents、MemoryEvents、jargon、expression patterns 的迁移、维护扫描与完成标记。
- 每日衰减、过期清理、维护评分、摘要生成和生命周期停止顺序。
- 表达模式学习、审核替换和后续合并。
- Persona 摘要缓存、分片生成、self-lore 写入及后台任务生命周期。
- Dream Agent、日记生成、维护请求、事实晋升、定时调度与可见性配置。
- 配置声明、热应用入口及存活子服务的运行时传播。

以上结论均具有可达生产触发路径；未把纯推测或仅凭局部代码无法证明的情形列为 finding。
