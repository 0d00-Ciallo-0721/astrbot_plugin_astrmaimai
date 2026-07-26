# 03 记忆与学习质量审计报告

审计代理：03 记忆与学习质量 | 日期 2026-07-26 | 基线代码 4da2910（服务器运行 c4aee57）
证据源：源码全文阅读（memory/ 33 文件 + learning/ 全部 + 相关 conversation/infrastructure 文件）、`turn_trace_samples_server.json`（585 recent traces）、新旧两份 astrmai_diagnostics.log、AstrBot core FaissVecDB 源码（site-packages）、git show f09cf65/320663f。

---

## 1. 领域概述与数据流实测

写入链路（实测）：
```
reply_service.send() [turn 内, 已发送回复之后]
  → _ingest_memory_turn (await, 阻塞 turn 任务)
      → pipeline.record_turn        # 入会话 buffer（"用户/旁白：{sender}: {text}"）
      → pipeline.process_instant_gate  # 规则门 → (可选) 内联 claim LLM (实测 5.2~44.5s)
      → pipeline.publish_turn_committed → EventBus(懒启动 3 worker)
          → _chat_worker(长驻/chat) → run_llm_backfill  # LLM 兜底抽取 —— 实测 100% 失败
  [独立] pipeline._sweep_loop (60s) → run_maintenance_for_session (≥60 行或闲置30min)
      → SessionMemorySummarizer.summarize_session → TopicSummarizer + MemoryProcessor(2 段 LLM)
      → write_service.write(kind=memory/fact) + legacy MemoryEvent + cognitive_feedback
写入统一入口 MemoryWriteService.write → 噪声/注入过滤 → MemoryAdmissionService.evaluate
  → v2_store.upsert（dedup 合并 / authority EAV supersede）→ MemoryIndexProjector.project
```

检索/注入链路（实测）：
```
prompt_refiner._decide_memory_injection  # 前置门：think0→skip, think1→需关键词
  → MemoryInjectionService.build_bundle → MemoryQueryBuilder(意图/扩展词)
  → MemoryRetrievalService.retrieve
      policy=deep 或 think>=3 → retrieve_deep: LLM改写(8s硬限,实测3/3超时)
        → canonical FTS + hybrid(faiss/bm25) → LLM rerank + LLM guidance（无超时上限）
      否则 → 单轮 FTS+hybrid 融合
  → context_builder.render → funnel/trace 记录
```

学习链路：消息 → MessageRecorder(60s 窗口≥20条触发) / backlog worker(900s 扫描,≥40 未处理) → expression_miner + jargon_miner（候选提取纯算法 → LLM enrich）→ 全部落 review_pending + maintenance_only，由 auto_check_task(LLM 审核) 或 WebUI 晋升 active。

Dream 链路：dream_scheduler → DreamAgent(LLM 工具循环,可 merge/update/delete 记忆) → DreamGenerator(虚构梦境文本) → 写 `__dream_diary__`（隔离 OK）+ 写真实会话 `[dream_maintenance] ...` 运维噪声 + PromotionEngine（EAV 事实晋升, confidence=1.0）。

### 运行时总体数字（585 recent traces, 16.6h）

| 指标 | 数值 |
|---|---|
| memory.injected | **4/585（执行轮 2/69 = 2.9%）**；前一天旧快照 9/92 = 9.8% |
| skip_reason 分布 | think_level_0 = 52（执行轮 50/69=72%）、think_level_1_no_memory_intent = 13、near_context_priority 3、empty_query 1、no_result 1、其余 515 为未执行轮默认空 |
| 私聊注入 | **0/19**（最大私聊 1481314186：14 条回复全部 think0/think1-无关键词跳过） |
| memory_funnel | 5/585（4 injected + 1 skipped）——funnel 只在 build_bundle 真正执行时写入，think0/1 的跳过发生在 prompt_refiner 前置门（prompt_refiner.py:678-684），**属设计使然而非采集缺陷** |
| memory.injection stage | 6 条：308ms / 1960ms / 9431ms / 50179ms / 71896ms / 92167ms |
| think>=3 深检索 | 3 次，其中 **2 次所在 turn 以 stale_drop 告终（回复被丢弃）** |
| instant backfill | 17 次 WARN 全部 `所有模型均失败: turn_deadline_exhausted`，0 次成功痕迹 |
| turn ledger 中 bg/memory 内联调用 | 7 次 gateway.chat family=memory_global_summary，5.2s~44.5s，6 成功 1 失败 |
| mining/jargon/expression 日志行 | 新窗口 16.6h **0 条**（旧窗口 3 条同群 "jargon enrichment failed closed"，每 30min 重试） |

---

## 2. 逐条发现

### ML-01（P0/P1, VERIFIED）后台记忆 LLM 兜底抽取被 turn deadline contextvar 毒化，100% 失败

链条（每一环均已读码证实）：
1. `main.py:224` `with turn_telemetry_scope(event):` 包裹整条消息处理链；`turn_call_ledger.py:226-232` 在 ContextVar `_CURRENT_TELEMETRY` 上挂当轮 `TurnTelemetryContext`（含 `deadline_monotonic`，由 message_entry `configure_turn_budget` 设置，360s）。
2. `reply_post_send.py:129` 在 turn 内 publish `memory.turn_committed`；`event_bus.py:209-216` **懒启动** 3 个 `_worker_loop` —— `safe_create_task` 是裸 `asyncio.create_task`（plugin_helpers.py:48），按 asyncio 语义拷贝当前 context → worker 永久携带首轮的 telemetry。
3. worker 派发回调也用 `safe_create_task`（event_bus.py:165-169）→ `MemoryTurnPipeline.on_turn_committed` 里 `asyncio.create_task(self._chat_worker(...))`（memory_turn_pipeline.py:174）→ 长驻 chat worker 同样毒化。
4. worker 内 `run_llm_backfill` → `gateway.call_data_process_task` → `gateway_call.py:283-289`：`clamp_timeout_to_turn_budget(None, ...)` → `current_turn_telemetry(None)` 读 ContextVar 命中**早已过期**的首轮 deadline → `effective_timeout <= 0` → 每个模型直接 `raise asyncio.TimeoutError("turn_deadline_exhausted")`。

运行时证据：`services.instant_memory_gate:271` 17/17 条 WARN 全为 `所有模型均失败: turn_deadline_exhausted`，跨 06:50→20:00 无一成功；而**同一 lane（bg/memory）在 turn 内内联执行的 claim 抽取 7 次成功**（trace ledger family=memory_global_summary）——毒化只发生在 event-bus 派生的后台任务，与代码路径完全吻合。降级路径本身不写脏数据（`instant_memory_gate.py:270-273` 直接返回空结果），后果是 **LLM 兜底记忆抽取通道自首轮 360s 之后永久归零**，只剩正则门（6 条中文模式）和会话摘要在写记忆。附带影响：毒化任务里 `begin_llm_call(None)` 会把调用挂到已 finalize 的首轮 ledger（不可见/污染）。
修复边界：`event_bus.publish`/`_worker_loop`/`MemoryTurnPipeline.on_turn_committed` 创建长驻任务时用 `contextvars.copy_context()` 之前先清空 telemetry（如 `_CURRENT_TELEMETRY.set(None)` 包裹创建，或 EventBus 在 bootstrap 阶段预启动 worker）。

### ML-02（P1, VERIFIED）think>=3 深检索串行 3 次 LLM 且后两次无超时——50~92s，3 次深检索 2 次导致回复被 stale 丢弃

- `memory_retrieval_service.py:403`：`policy=="deep" or think_level>=3` 进 deep 路径（trace 中两轮 policy 实为 "light"，被 think=3 强制升级）。
- `_rewrite_queries`（L790-874）8s 硬限——实测 3/3 超时（deepseek-v4-flash bg 池 p50≈7.4s，8s 预算基本必败），纯烧 8s 后回退原 query。
- `_rerank_candidates`（L934）+ `_compress_guidance`（L968）经 `_call_deep_json`（L906-916）调用 `call_data_process_task(prompt, is_json=True)` —— **无 lane、无 timeout_override、无预算钳制**，各吃默认 API 超时；且对仅 2~3 条候选也照跑。
- Trace 证据：turn `1d01319296b6` memory.injection=50179ms（rewrite 8s + rerank/guidance ~42s），turn `dfdd1f5d89aa` 第一次 injection abandoned@92167ms、重试后 71896ms，两轮均 `status=stale_drop, reply_sent=False`——**用户发出"求助/加好友"类消息后 bot 憋了 100~180s 然后什么都没发**。
- 历史对照：7-25 分析已记录 query_rewrite 79s（KNOWN）；c4aee57 给 rewrite 加了 8s 硬限（部分修复），但 rerank/guidance 两条尾巴仍无界 → 判 KNOWN_FIXED_REGRESSION（声称收敛超时，实际主要耗时源未覆盖）。
修复边界：`_call_deep_json` 增加 `timeout_override=clamp_timeout_to_turn_budget(...)` 与 lane_key；候选数 ≤ top_k 时跳过 LLM rerank；guidance 可选关闭。

### ML-03（P1, VERIFIED 代码闭环）偏好类事实的 authority EAV 去重键过粗——新"喜欢"覆盖旧"喜欢"，旧事实被 supersede 且移出索引

- claim 抽取把所有喜好归到 `attribute="like"/"dislike"`（memory_claim_service.py:78）；instant gate 与 session summarizer 的 authority 写入 dedup_key 均为 `{subject}:{entity}:{attribute}`（instant_memory_gate.py:153、session_memory_summarizer.py:235）。
- `_looks_like_authority_eav`（v2_store.py:508-519）命中 → `mark_superseded_by_key`（L868-945）把同 key 全部旧记录标 superseded；write_service 再调 `index_projector.cleanup_deleted(superseded_old_ids)`（memory_write_service.py:134-135）移出 FTS/documents。
- 后果：用户说"我喜欢咖啡"（写入）→ 一周后说"我喜欢猫"→ 咖啡事实被替换、检索不到。多值属性（喜好集合）被建模成单值 EAV。dislike、relationship 等类别同理。
- DB 采样验证 SQL（服务器 /AstrBot/data/plugin_data/astrmai/astrmai.db）：
  `SELECT dedup_key, COUNT(*) n, SUM(status='superseded') dead FROM canonical_memories WHERE dedup_key LIKE '%:preference:like' GROUP BY dedup_key HAVING n>1;`
修复边界：偏好类 claim 的 dedup_key 携带 value 归一片段（`{subject}:preference:like:{norm(value)[:24]}`），或 attribute 细分为 like_food/like_color 等；conflict_resolver 对多值属性禁用 authority_override。

### ML-04（P1, VERIFIED 含框架源码）删除/替换后 FAISS 向量残留——投影清理绕过 FaissVecDB.delete，嵌入永不回收

- `MemoryIndexProjector.cleanup_deleted`（memory_index_projector.py:93-114）用裸 SQL `DELETE FROM documents ...` + `DELETE FROM memories_fts ...`；
- AstrBot 核心 `FaissVecDB.delete(doc_id)`（site-packages/astrbot/core/db/vec_db/faiss_impl/vec_db.py:168-180）才会同时 `embedding_storage.delete([int_id])`。投影器从不调用它，`rebuild_all/_clear_projected_documents` 同样只删行。
- `FaissVecDB.retrieve`（L103-150）先在 FAISS 索引上取 top fetch_k 个 id，再回表补文档；被 SQL 删掉的行导致幽灵 id 占据召回名额后被静默丢弃（VectorRetriever 有 metadata_filters 时 fetch_k=2k 只能缓解一半）。
- 触发面极广：每次 authority supersede（ML-03）、soft_delete、mark_stale、quality quarantine、WebUI 删除都会产生幽灵向量；嵌入索引只增不减，向量召回率随运行时间单调劣化，最终 deep/hybrid 检索"查得到分数、取不到文档"。
- 运行时佐证需 DB：`SELECT (SELECT COUNT(*) FROM embedding_storage表) - (SELECT COUNT(*) FROM documents)`（具体表名见 sqlite_init.sql），差值即幽灵数。
修复边界：cleanup_deleted 改走 `engine.faiss_db.delete(doc_id)`（先查 documents.doc_id）或补调 `embedding_storage.delete`; rebuild 路径同理。

### ML-05（P1, VERIFIED）注入率 2.9% 的主因是 think 门+关键词门的策略设计：正常聊天几乎永不读记忆，私聊 0 注入

- 前置门 `prompt_refiner.py:678-684`：think0（执行轮的 72%，含私聊 cooldown_simple_turn）直接 policy=none；think1 需命中 `MEMORY_INTENT_KEYWORDS = {记得,刚才,之前,上次,回忆,想起,remember,...}`（memory_injection_service.py:22-33）。
- 实测：私聊 1481314186 十四条回复全被 think0/think1-无关键词跳过；执行轮 think 分布 GROUP 0:46/1:3/2:2，PRIV 0:7/1:10/3:1。用户问"我叫什么名字"不含触发词 → 不检索 → 已写入的 identity 事实读不出来；只有"你还记得X吗"这类句式可触发。
- 与旧快照对比（9.8% → 2.9%）为流量结构波动，非新回归；7-25 分析已标 P3 KNOWN_OPEN。但结合 ML-01（写入兜底断）与 ML-02（深检索必死），当前系统状态为：**写入持续膨胀、读取几乎为零、深读必超时**——记忆子系统对用户可感知价值趋近于 0，同时持续消耗摘要/挖掘 LLM 成本。
修复边界：think1 放宽为"命中记忆意图 OR query_builder 识别出 identity/preference 意图"；私聊 think0 保留轻量 FTS-only 注入（无 LLM 成本）。

### ML-06（P2, VERIFIED）发送后的内联 claim 抽取 LLM 在 turn 任务内同步执行，实测 5.2~44.5s

- `reply_service.py:198` await `_ingest_memory_turn` → `process_instant_gate` → gate 命中 relationship/major_event/contact/explicit_cmd 且规则无 claim 时内联调 `MemoryClaimExtractor.extract` LLM（memory_claim_service.py:146）。
- Trace ledger 7 次 family=memory_global_summary stage=gateway.chat：5263/8726/10783/11188/34392/44457ms + 1 error。回复已发出、用户不直接感知，但 turn 任务多挂 5~44s：turn_total 虚高、per-chat 串行的后续消息处理被顺延、且这些调用占用 bg/memory lane 与模型配额。
修复边界：`_ingest_memory_turn` 的 instant-gate 部分丢给 `_chat_worker`（先修 ML-01 才安全）或 `asyncio.create_task` + 干净 context。

### ML-07（P2, VERIFIED）Dream 每次运行往真实会话写一条运维噪声记忆，且 LLM 合并叙事不受 admission 治理

- dream_scheduler.py:231-241：`add_memory(content=f"[dream_maintenance] session=... 完成，共执行 N 次维护动作。", session_id=真实会话, importance=0.65)`——kind=memory、active、可被检索注入到聊天 prompt；文案随 N 变化无 dedup，按 dream 频率线性累积（梦境日记本身写 `__dream_diary__` 隔离，无问题）。
- DreamAgent `_tool_merge`（dream_agent.py:226-243）把 LLM 生成的 new_narrative 经 `add_memory`（source=legacy_add_memory，**不在 admission 治理名单**）写成 active 记忆并 mark_merged 原始记录——梦境代理若改写失真，失真版本成为唯一活跃版本。
- 采样 SQL：`SELECT COUNT(*) FROM canonical_memories WHERE content LIKE '[dream_maintenance]%' AND status='active';`
修复边界：dream_maintenance 摘要改写入 `__dream_diary__` 或 metadata-only；merge 叙事走 admission（加入 _GOVERNED_FACT_SOURCES 或单列治理）。

### ML-08（P2, LIKELY）Dream 事实晋升的"3 证据"阈值可被单次 LLM 响应内的重复项满足，产出 confidence=1.0 的权威事实

- promotion_engine.py:105-106：`len(evidences) >= PROMOTION_THRESHOLD(3)` 的 evidences 来自 `_iter_detected_facts`——同一次 dream 的 detected_facts[:12] 未去重（fact_contract.normalize_dream_facts 无去重），LLM 把同一事实列 3 遍即触发晋升；confidence 缺省 0.9 / signal 缺省 "high"（fact_contract.py:21-26）恰好越过 `confidence<0.85 且 signal 非 high` 的过滤。
- 晋升写入：`kind=fact, importance=0.9, confidence=1.0, source=dream_audit_pipeline`（promotion_engine.py:139-154）→ `_looks_like_authority_eav` 白名单直接 supersede 同 key 旧事实——**LLM 幻觉可覆盖用户亲口说的事实**。admission 不治理该 source。
- 历史对照：06-10（协议断开导致晋升永不发生）已修复，本条是修复后引入的新风险面。
修复边界：`_iter_detected_facts` 按 (subject,entity,attribute,value,evidence.turn_id) 去重；confidence 上限取 evidences 实际置信度而非硬编码 1.0。

### ML-09（P2, VERIFIED 设计 + 运行时印证）挖掘 fail-closed 无毒丸跳过：同一批次每 30min 原样重试，可永久卡死一个群的学习；新窗口 16.6h 零挖掘日志无法证明链路活着

- `process_logs_and_mine`（evolution_manager.py:604-614）jargon enrichment 非 terminal → raise → 日志不标记 processed → `_backlog_failure_until` 冷却 1800s 后 `_load_unprocessed_logs(limit=batch_size)` 取回**同一批头部日志**重试。若某批内容让模型持续返回坏 JSON（invalid_json/invalid_schema 均 retryable），该群 head-of-line 永久阻塞，backlog 只增不减。旧日志实测：群 1075910254 连续 3 次（02:58/03:29/04:00，间隔≈失败冷却 30min）——是重试机制在按设计空转。
- f09cf65 验证：该提交把"静默丢批"改为 fail-closed + terminal/retryable 语义（jargon_enricher.py +120 行）——**数据不再丢，声称成立**；但没有毒丸计数/跳过机制，也没有 backlog 卡死告警。
- 新窗口 16.6h `JargonEnricher/Evolution-Backlog/TopicSummarizer 分割` 日志全为 0：live 触发条件苛刻（60s 内 ≥20 条），backlog 阈值 40 条/群按流量应可达——挖掘是否真的运行需 DB 定论：`SELECT key, value FROM memory_v2_meta WHERE key LIKE 'learning_mining_ledger:%';` 与 `SELECT COUNT(*) FROM message_log WHERE processed=0 GROUP BY group_id;`
修复边界：`_backlog_failure_until` 升级为按 (group, first_log_id) 的失败计数，≥N 次后跳过头部 min_mining_context 条并标记 processed（毒丸丢弃），同时 WARN 提级。

### ML-10（P2, VERIFIED）会话摘要主路径丢失说话人身份：buffer 行格式与解析正则不匹配，群摘要参与者全成 "unknown"

- 主路径（sweep 驱动）`run_maintenance_for_session` → `summarize_session(chat_history_text)`，messages=None → `_build_topic_messages` 用 `^\[time\] sender: content` 正则解析（session_memory_summarizer.py:364），但 buffer 行是 `用户/旁白：{sender}: {text}`（memory_turn_pipeline.py:137）——无 `[time]` 前缀 → 全部 fallback `sender="unknown"`。
- 后果：TopicSummarizer 的 participants=["unknown"]、canonical 摘要 metadata.speaker_ids=[]、evidence_message_ids=[]；群记忆无法归属到人，检索"张三说过什么"只能靠正文里碰巧带名字。备用路径 `extract_and_summarize_history`（MessageLog 驱动，diary_service 调用）有真实 sender_id，主路径没有。
- 这同时回答结构化问题：canonical_memories 有 sender_id/session_id 结构列，但 (a) 检索 `store.search` 不接受 sender 过滤（v2_store.py:988-1043 仅 session/persona/kind/visibility），(b) 名字只存在于自由文本，按名检索=FTS bigram 模糊匹配。
修复边界：record_turn buffer 行带结构（存 dict 而非拼接字符串），或 `_build_topic_messages` 兼容 `用户/旁白：sender: text` 格式。

### ML-11（P3, VERIFIED）演示场景启发式硬编码进生产规则

- claim_rules.py：`SERVER_COUNT_PATTERN = re.compile(r"(\d+)")` + SERVER_KEYWORDS——句子含"server/服务器"+任意数字即产出 `asset:server_count=<第一个数字>` 权威 claim（certainty 0.7-0.85）；ANXIETY/anx 关键词同源。
- memory_retrieval_service._intent_rerank 食物词表（火锅/芒果）、query_builder ANCHOR_TERMS（蓝色/跑步）均为测试场景词表泛化不足。影响低频但会产出错误权威事实（如聊 Minecraft 服务器人数）。

### 治理面良好项（不立 finding，供主控平衡）

- **admission 实际拦截**：疑问句/命令句/不确定语气/无主体/无证据/未水合 fallback → review_pending+maintenance_only+confidence≤0.49（320663f 引入，git 验证成立）；quality_audit/quarantine 可批量补扫历史污染。
- **jargon/expression**：一律 review_pending+maintenance_only 落库，LLM auto_check 或 WebUI 审核后才 active；dedup（jargon 归一词、expression 四元组）+ 证据 message_id 防重复计数 + maintenance 定期清 pending/rejected——**污染防线是完整的**。
- **topic**：digest dedup + 0.85 相似度合并 + valid_until 14/30 天 TTL + 单批 ≤8。**feedback**：per (chat,source) rolling dedup + 72h TTL + tool_only 可见性 + 内存缓存 32/chat。均有上限与衰减。
- **WebUI 修订闭环**：update 后 `projector.project`、拒绝/删除后 `cleanup_deleted`（memory_ui_service.py:338-345）——除 ML-04 的向量层外闭环完整。
- **历史 06-01/02/03/06/07/10/12 复核**：均已在当前代码修复（sender_id 已传入、shutdown flush force、min_confidence 生效、replace_dedup_identity、dream fact 协议接通、乱码前缀移除）。
- 注意：`MemoryMaintenanceService.run_once`（索引一致性修复+过期候选清理）**只有 WebUI 手动入口**（memory_ui_service.py:620），日志里 "repair_scheduled=true" 文案并无对应调度器；自动调度的只有 proactive decay_service 的 apply_daily_decay。投影失败的记忆需人工触发修复。

## 3. 领域级测试缺口

1. 无任何测试覆盖"后台任务继承 turn telemetry contextvar"（ML-01）：需要一个 `turn_telemetry_scope 内 publish → worker 内 call → clamp 应返回 requested` 的回归测试（tests/test_turn_call_ledger_refactor.py 只测同步 scope）。
2. retrieve_deep 的端到端时延契约无测试：`_call_deep_json` 可被 mock 成 sleep，断言 deep 路径总耗时 ≤ 预算（现有 tests/unit/memory/test_memory_query_optimization.py 只测排序逻辑）。
3. 无 preference 多值语义测试：连续写两条不同 like 后两条都应可检索（tests/unit/memory/test_memory_conflict_resolution.py 只测覆盖方向正确性）。
4. 无向量删除一致性测试：cleanup_deleted 后 faiss 检索不应返回幽灵（tests 中无任何 embedding_storage 断言）。
5. `_build_topic_messages` 对 pipeline buffer 行格式的解析无测试（sender 全 unknown 未被任何断言捕获）。
6. promotion_engine 对重复 detected_facts 的去重无测试（tests/unit/memory/test_memory_promotion.py 未覆盖单响应重复）。

## 4. 附录：分析脚本输出摘要

脚本：scratchpad/analyze_memory_traces.py, analyze_memory_traces2.py, compare_old_traces.py。
关键输出已内嵌上文表格；完整输出见 scratchpad/memory_trace_report.txt / memory_trace_report2.txt。
- 585 traces status: skipped_ignore 317 / skipped_wait 83 / sensor_filter 102 / executed 67+2 / stale_drop 7 / repeater_echo 7
- injected 4（2 executed + 2 stale_drop）；stale_drop 两轮 think=3、memory.injection 50.2s/92.2s+71.9s、reply_sent=False
- ledger families: judge 539 / mood 364 / chat_tools 69 / chat_dialog 30 / memory_global_summary 7 / compaction 3 / vision 10
- 旧快照（176 traces）：injected 9/92 执行轮，think0 占 87%
- 新日志 WARN 178 总分布：gateway_call 48+9、request_retry 43、instant_memory_gate 17、recallguard 15、context_economy 12、gateway_tasks 8+4、memory_retrieval(deep rewrite) 3、其余零星
