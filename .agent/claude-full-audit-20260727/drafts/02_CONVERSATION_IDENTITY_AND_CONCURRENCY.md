# 02 群聊身份与私聊连续输入 — 领域审计报告

> 审计代理：02_conversation_identity_and_concurrency。基线代码 4da2910；运行时证据 `.agent/runtime-observability-c4aee57-20260726/`（585 traces / 16h + astrbot_since_c4aee57.log）。全程只读。

## 1. 领域概述与总体结论

本领域回答五个问题：群聊身份绑定、thread 语义与 stale 判定、poke/图片/撤回/proactive 的身份注入、私聊连续输入协调、并发竞态。

**总体结论**：
1. **身份绑定链路本身是健康的**。focus 说话人身份（sender_id/sender_name）从 perception → focus_selector → thread_builder → planner(user_id=focus.get_sender_id) → current_speaker_block → prompt_refiner「最终发言人归因锁」→ group_actor_consistency 后置修复，全链一致。脚本对 67 个 executed turn 的 reply_preview 与同群其他 sender_name 做交叉匹配，**0 例把 B 的回复错称成 A**（an02_out.txt L138-141）。relationship/profile 按 focus sender_id 取（planner.py L1224 `user_id = event.get_sender_id()` → planning_input_loader._load_tool_state → get_user_profile/relationship_engine.get_or_create(user_id)），不存在"按 chat 取最后一个人"的塌缩。
2. **真正坏掉的是"回复期间的世界模型"**：freshness/stale 判定的线程隔离是死代码（ID-01），主动开口候选与 peer poke 在传感器层 100% 阵亡（ID-02/03），私聊 prompt 被硬编码"群聊"话术污染（ID-04）。
3. 并发原语（generation/send_claim/executor lock）实现是原子的、正确的；私聊路径缺一个"被取代任务提前取消"，但 16h 样本没有实际踩中。

## 2. 线程模型实测（问题 2 的答案）

系统里同时存在**两套互不相通的"线程"概念**：

- **turn thread_id**（`conversation/threading/group_thread_resolver.py`）：ingress 时绑定。群聊优先级 = `astrmai_thread_signature`(此时必为空) → reply 组件 `reply:<msgid>` → `sender:<qq>`(置信 0.6) → chat_id。私聊 = `private:<chat_id>`。Trace 实测 585 条：`sender:*` 537、`private:*` 18、空 30（proactive 合成事件）、`reply:*` 0。generation 序号是**全局单调**计数器（chat_runtime_coordinator.py L131-132 `self._generation_sequence += 1`），按 (chat_id, thread_id) 存最新值——所以私聊 trace 里 gen 29→30→32→35 的跳号只是别的会话消耗了序号，不代表本会话有 supersede。
- **focus thread_signature**（thread_builder.py L122-133）：focus 批次构建完才算出（`reply_mode|root.sender|…|sha10(root文本)`），由 `emit_legacy_focus_thread_extras` 写回 event（legacy_compat.py L76）。

**stale 判定用的是第二套**（reply_freshness.py L221-228 / chat_runtime_coordinator.evaluate_reply_freshness L431-477），而活动标记 `mark_activity` 在第一套都还没有的时刻就执行了——于是 `newer_activity_unknown_thread` 成为群聊唯一可能的跨线程判定结果（详见 ID-01）。**是的，A 的回复会因 B 的任意新消息被误判过期**；私聊方向（allow_parallel_threads=False）无漏判问题，未见私聊 stale。

`stale_category="newer_activity_unknown_thread"` 的产生路径：evaluate_reply_freshness L447-451 `different_known_thread` 要求**双方签名都非空**；latest 侧永远为空 → 永远走 L470-477 `same_thread=False` 分支 → reason=`superseded_by_newer_activity_unknown_thread`。

## 3. 逐条发现

### ID-01（P1/VERIFIED）群聊在途回复被无关消息击杀：freshness 线程隔离整体失效

**因果链（三处叠加）**：
1. `gate.py L1205 _record_event_activity` 在 process_event 早期执行，L535 读 `event.get_extra("astrmai_thread_signature")` ——该 extra 要到 worker 里 focus 构建完成（L1390 emit_legacy_focus_thread_extras）才会写入。所以 `mark_activity` 记录的 `latest_activity_thread_signature` **恒为空**（唯一例外：group_wait RESUME 事件 L416 提前带了旧签名）。
2. `chat_runtime_coordinator.evaluate_reply_freshness` L447-453：`different_known_thread` 需要双方签名非空 → `allow_parallel_threads`（reply_freshness.py L227 群聊传 True）**永远不会触发** `newer_activity_other_thread_ignored`。任何 >4s 的新消息 → `STALE_BUT_SALVAGEABLE`（≤6s）或 `EXPIRED`（>6s）。
3. `executor._evaluate_execution_freshness` L444-449 调 evaluate_reply_freshness 时**根本不传** `allow_parallel_threads`（默认 False）——即使 1/2 修好，执行中段检查（L759-762 `_check_pre_model_freshness`）仍会跨线程击杀。

**运行时证据**：
- 7 例 stale_drop 全部集中在最活跃群 1062115731；该群 16h 状态分布 `executed 6 / stale_drop 7`——**被丢弃的成品回复比发出去的还多**。4 例 stale_category=`newer_activity_unknown_thread`（age 11.5/18.3/23.2/29.8s，均 << max 450s），受害消息含直接提问："为什么加了妃妃还要等同意"、"我是你的哥哥吗"、"fvv妃妃"。
- 日志 15:23:22（astrbot_since_c4aee57.log L3074-3075）完整链条：dialog 模型已产出 272 tokens → `stop expired tool execution: superseded_by_newer_activity_unknown_thread:师清漪:6.7s`。击杀者是师清漪 15:23:17 发的一张**被动图片**——这张图自己随后被 gate 判为 IGNORED_IMAGE。**bot 自己都不理的噪声消息，杀死了 bot 正在写的回复**。
- 附带两个 instrumentation 缺陷：executor.py L1081 把 freshness 中止 WARN 成 `tool model code3/deepseek-v4-pro failed, trying next: superseded_by_...`（污染模型失败统计——主控统计的 "executor tool model failed 2" 实为此类）；executor 预检路径不走 `_record_freshness_observation`，所以 3/7 stale_drop 的 stale_category/reason/age 全空。

**用户后果**：活跃群里直接向 bot 提问，LLM 成本照付（每次 2-4 次调用），回复静默蒸发，无任何补偿动作（`_allow_direct_reply_timeout` 只覆盖 reply_age 路径不覆盖 newer_activity 路径）。
**known_status**: NEW（历史审计只讨论过 group_wait 的签名错位，未涉及 mark_activity 恒空导致隔离失效）。
**最小修复**：`gate._record_event_activity` 改传 `resolve_group_thread(event, chat_id).thread_id`（sender:/reply: 级），`evaluate_reply_freshness` 同步改比较 turn thread_id；executor L444 补 `allow_parallel_threads=not is_private`；executor 预检路径补记 freshness 观测字段；freshness 中止不要走 "model failed" 日志分支。
**回归风险**：中——放开跨线程并行后，同群多线程同时回复的量会上升，需观察 send_claim/actor_consistency 兜底。

### ID-02（P1/VERIFIED）Proactive 合成事件 100% 死在传感器层，主动开口功能整体失效

`proactive/dispatcher.py L321-351` 构造的合成事件只有 `message_str`（"[主动开口候选]…"），`message_obj=None`（gate.py L39 `_SyntheticExternalEvent`）。而 `sensors.should_process_message`（sensors.py L205-231）**只从 message_obj.message 组件**提取 clean_text/has_payload，L317-318 `if not clean_text and not has_payload: return False`。合成事件没有任何组件 → 群聊路径在 gate.py L1137 被过滤，私聊路径在 L1046 被过滤。`is_virtual_poke` 旁路（L113）只给 poke 用，没有 proactive 旁路。

**运行时证据**：trace 中 14/14 个 `sender_id=astrmai_proactive_candidate` 的 turn 全部 `skipped_sensor_filter`，覆盖 8 个群，reply_sent 全 False。16h 内 bot 零次主动开口。
**测试缺口实锤**：`tests/test_attention_gate_refactor.py L1375-1411` 的 proactive 测试把事件直接塞进 `_debounce_and_judge` 的 accumulation_pool，绕过了 process_event 的传感器检查——所以回归没被接住。
**known_status**: NEW（`.agent/final-functional-audit/10_learning_proactive.md` 只报过"blocked 时冷却不回滚"，未发现 sensor 全灭；且该旧发现会放大本 bug：每次被过滤的候选仍可能占用节奏窗口）。
**最小修复**：`should_process_message` 开头对 `astrmai_is_proactive_event`（或统一的 `astrmai_synthetic_event`）返回 True；或 dispatcher 给合成事件带一个 Plain 组件。补一条走完整 process_event 的集成测试。
**回归风险**：低——旁路只影响合成事件。

### ID-03（P2/VERIFIED）Peer poke（群友互戳）100% 被过滤，整套 peer 互动剧本是死代码

`sensors.process_poke_event` L644 `event.set_extra("is_virtual_poke", target_is_bot)` —— peer poke（戳别人）为 False；虚拟叙事文本只写进 message_str，message_obj 里只有 Poke 组件（不在 media_classes）→ 与 ID-02 同因，在 `should_process_message` 步骤 4 被拒。下游为 peer poke 准备的全部逻辑——`_peer_poke_join_allowed`、`astrmai_peer_poke_*` extras（L634-675）、planner_prompt_context.py L525-529 的 peer_poke 引导——全部不可达。

**运行时证据**：30/30 条 "X 戳了 Y 一下，这是群友之间的轻互动" trace 全部 `skipped_sensor_filter`；日志 31 条"捕获互动事件"里 30 条 peer。bot 目标的 poke 正常（is_virtual_poke=True 旁路 + 日志 15:22:53 "已回戳反击用户: 1711338653" 回戳成功，但 16h 仅 1 例、无对应文本回复 trace，poke 文本回复路径样本不足）。
**known_status**: NEW。
**最小修复**：同 ID-02 —— `should_process_message` 对 `astrmai_interaction_kind` 非空的事件直接放行（判额度交给 judge/playbook 的 peer_join_allowed）。

### ID-04（P2/VERIFIED）私聊 prompt 注入"群聊/群友/群里"话术

私聊与群聊共用 `GroupDialogueStore`。其 warm topic 兜底模板硬编码群聊语境：
- group_dialogue_store.py **L352** `"当前主要是在延续刚才的群聊话题，最近落点是…"`；L363/371/379 `"群里现在是在顺着那个回应继续消化细节" / "来自群友之间的补充和接话"`；
- context_compaction.py **L1598** 冷摘要 `"这段旧对话主要延续同一组群聊话题…"`；
- infrastructure/context_economy/prompt_templates.py L155 摘要 system prompt `"你是群聊话题摘要助手"`。

planner_prompt_context._get_warm_context_bundle（L323-335）对私聊同样取用并放进 warm_zone_summary → prompt_refiner 注入正文。

**运行时证据**：私聊 ff:FriendMessage:1481314186 的**每一个** executed turn（15/16）warm_summary 都是"延续刚才的群聊话题…"，45 个 warm 字段命中"群聊"。用户在私聊里说"我失恋了"，prompt 却告诉模型"群里现在在顺着回应消化细节"。
**known_status**: NEW。**最小修复**：模板按 `"FriendMessage" in chat_id`（或传入 is_private）切换措辞（"这段私聊/对方"），三处文件同步。回归风险低。

### ID-05（P2/VERIFIED）stage_ledger reply.send `sent_segment_count=0` 与 reply_stats=2 的矛盾 = 纯 instrumentation，不是丢段

`reply_artifact_builder._send_segments` 的本地计数 `sent_segment_count`（L544-567）只有在**异常部分发送**（L604）或**中途 freshness 截断**（L634-636）时才写进 `artifact.metadata`；全量成功路径从不写。而 reply_service.py L153 stage 元数据 `metadata.get("sent_segment_count", 0)` 默认 0，L181 reply_stats 却默认 `len(artifact.segments)`。

**运行时证据**：67/67 个 executed turn stage_meta=0 且 reply_stats=planned、send_status=sent、`sent=True`——全部满发。**结论：主控样本疑点②确认为 instrumentation bug，无真实丢段**。附带确认：真正的部分发送路径存在且受 ID-01 影响（每段发送前都 `_check_reply_freshness`，reply_artifact_builder L548-553），但样本里 0 例 partial_sent。
**最小修复**：`_send_segments` 成功路径结尾统一写 `artifact.metadata["sent_segment_count"] = sent_segment_count`。

### ID-06（P2/VERIFIED-代码链闭环）Proactive 的"当前发言人"是幽灵用户（被 ID-02 掩盖）

`planner_prompt_context._build_current_speaker_block`（L148-180，L463 调用）对 proactive 合成事件照常生成 `QQ: astrmai_proactive_candidate / 昵称: 主动开口候选`，prompt_refiner L136-159 的「最终发言人归因锁」进一步要求"回复中的第二人称、昵称和关系称呼必须指向这一位"。主动开口本无对话对象，一旦 ID-02 修复，每条主动消息都会被指令锁死到一个不存在的人身上（可能对空喊"你"或复读"主动开口候选"）。当前因 ID-02 从未到达 planner，无运行时样本。
**最小修复**：`_build_current_speaker_block` 对 `astrmai_is_proactive_event` 返回 ""（并跳过归因锁）。**必须与 ID-02 同一批修**。

### ID-07（P3/LIKELY）group_wait 残留不对称：reply: 键位的等待无法被普通跟进复活

历史审计（final-functional-audit/02、09）报过"等待按晚期签名键存、来消息按 sender: 键查 → 永不复活"。现版已修主路径：`_thread_id_from_event` L56-58 优先取 `astrmai_turn_thread_id`，日志 16h 全部 `armed wait ... thread=sender:*` 且有 1 次成功 RESUME（06:42:42）。**残留**：若唤醒消息带 Reply 组件，ingress thread 为 `reply:<msgid>`，等待也按此键存；目标用户后续纯文本是 `sender:<id>` 键 → L319 查空；L329 unique-target 兜底要求 `not has_explicit_thread`，而 message_entry L282 先绑 turn 再 L285 处理等待，`astrmai_turn_thread_id` 恒非空 → **兜底永假**（对所有消息都是死分支）；除非用户恰好 Reply bot 的 outbound（L321-327）。16h 样本 0 个 reply:* 等待，未实际踩中。另：39 次 armed 仅 1 次 RESUME、其余超时——等待命中率 2.6%，纯观察项。
**最小修复**：L329 放宽为 `not incoming_signature`（turn_thread_id 不算 explicit），或 register 时同时登记 sender 别名键。

### ID-08（P3/VERIFIED）撤回事件零处理：被撤回消息继续留在上下文里

`message_entry._classify_event_route`（L89-97）把非 poke 的 notice（含 group_recall/friend_recall）标记 `notice_passthrough` + `astrmai_non_conversational` 后直接 return（L160-176）；全插件 grep 无任何 recall 消费者（qq_action_dispatcher 的 withdraw 是 bot 撤自己的消息，message_recall_lookup 是查询工具）。后果：用户撤回后，原文仍留在 group_dialogue_store / attention window / lane history，bot 之后可能原文引用已撤回内容（隐私/尴尬），也不会解除对原作者的关联（作者归属本身是正确的，不存在错绑）。日志 16h 出现 ≥5 次 group_recall（RecallGuard WARN 行）。
**修复方向**：notice_passthrough 时按 message_id 在 dialogue_store 打 tombstone（保留"某人撤回了一条消息"的事件语义即可）。

### ID-09（P2/VERIFIED）私聊回复中位延迟 44s：五段串行 LLM 链 + 每条私聊都过一次必然 REPLY 的 judge

问题 4 的时间线重建（an02b_out.txt）：trace `created_at` 是**完成时刻**，attention.dispatch 相对它 -total_ms 处即真实起点。典型 turn（"呜呜呜"，64.4s）：t0 ingress(0.5s) → **+11.0s mood LLM**(3.5s) → **+14.5s judge LLM**(13.5s) → +28.7s cognitive chat_dialog(15.0s) → +44.0s context_build → +45.2s chat_tools(6.3s) → **+51.5s reply.send**；尾部 ~12s 为发送后记账（memory_global_summary 等）。主控疑点①的 14.1s 空档 = settle(1.5s) + 排队/落库 + mood LLM 串行；9.1s 空档 = cognitive_loop LLM。这是**合并等待窗 + 串行架构**，不是卡死缺陷；但代价真实：executed 私聊 reply_age p50=44.3s / max=357.4s（n=16）。其中 judge：18 个私聊 turn 里 14 次 judge LLM 调用（5-17s/次），私聊 16h **零次** IGNORE/WAIT（16 executed + 2 topic_confirmation）——私聊 judge 是纯延迟。turn_merge/settle 已经承担了"等他说完"的职能（gate L1293-1315），私聊可默认跳过 judge 或降级为规则判断；mood 可与 judge 并行。
**related**: 延迟/网关领域（lane 排队、mood pool 364 次调用的成本归属该域细查）。

### ID-10（P3/VERIFIED）poke 目标解析兜底会把"戳别人"误记成"戳 bot"

sensors.py L500-504：target_id 非数字且≠bot → 置空 → 空/0 一律回填 bot_id。NapCat raw payload 缺 target_id 时，B 戳 C 会被当成 B 戳 bot：触发好感度结算（L605-616）+ 回戳（L621-632）——bot 无端回戳并给 B 加好感。样本内 raw payload 均带 target_id 未踩中（30 peer 均正确识别），属低频防御缺陷。另：`_resolve_name` 会把群名片/签名混入 target 显示名（trace 见 "戳了 袅袅都烫唧唧的季节里，希望你别中暑了~ 一下"），叙事文本质量问题。

## 4. 问题 1/3/5 的其余勘验结论（无发现级别问题）

- **@提及者/引用作者**：`_extract_reply_target`（gate L359-372）与 dialogue segment 的 reply_target_sender_id/name 全程分离存储；`_format_segment_line` 渲染 `回复 X` 前缀；thread_builder `resolve_thread_root` 优先按 reply_target 找根。身份不混。
- **图片消息**：私聊图片走 vision barrier（private_turn_coordinator._prepare_event），vision 记录绑定在事件上、由 `bind_batch_context` 聚到 focus event，picid 以 message_id+index+source_ref 哈希——不会窜到别人的消息上。群被动图片直接 IGNORED_IMAGE（gate L1146）。
- **私聊图片屏障**：文本在 `wait_for_input_stability`（1.5s 静默）后与图片同批处理；vision 未完成不会先发文本（prepare_batch 串行在 judge 之前，vision_barrier_total_timeout 180s 上限 + require/timeout_fallback 两策略，fe12bcc/4da2910 行为正确）。
- **旧话题承接**：conversation_continuity.evaluate_private_message 状态机（active/candidate/stale_needs_confirmation/awaiting_confirmation）实测 2 次 executed_topic_confirmation 正常走通。
- **并发原语**：advance_generation/register_turn_task/claim_send 全部在单一 asyncio.Lock 内原子完成；send claim 防重发（send_key 含 generation）；executor per-chat lock max_pending=2。群聊 sys2 经 `_run_managed_system2_task` 注册可被新 generation 取消；**私聊 sys2 是 worker 内联 await（gate L1532），不注册、不可被提前取消**——被取代时会跑完再在发送口被 stale_generation 拦下，配合 begin/finish_pending_batch(revision) 把旧输入并入下一批（rebind 正确，成本浪费）。16h 私聊无一次实际 supersede（generation 是全局序号，跳号≠取代），故仅作为设计注记不单列 finding。
- **finish_pending_batch 微竞态**：发送成功后、finish 前若来新消息，revision 错位导致已回答的 msg1 被并入下一批 rich_text（可能二次回答）。窗口 <1s，标记为 ID-09 关联的观察项。

## 5. 领域测试缺口

1. **合成事件过传感器**：无任何测试走 `process_event → _passes_sensor_filters` 验证 proactive/peer_poke 虚拟事件放行（现有测试直接注入 accumulation_pool，tests/test_attention_gate_refactor.py L1375-1411）。
2. **mark_activity 签名完整性**：test_reply_freshness_budget_ported.py 只测显式签名组合，无"ingress 时签名尚未生成"的集成断言。
3. **reply.send 满发路径的 metadata**：test_reply_service_refactor 未断言 stage metadata 与 reply_stats 的 sent_segment_count 一致。
4. **私聊 store 措辞**：无针对 FriendMessage chat 的 warm/cold 摘要措辞断言。
5. **group_wait reply: 键复活**：test_group_wait_thread_signature_ported.py 未覆盖"等待键=reply:*、跟进=sender:*"组合。

## 6. 附录：分析脚本输出摘要

脚本：scratchpad/an02.py、an02b.py（输出 an02_out.txt、an02b_out.txt）。关键数字：
- 585 traces：executed 67 + executed_topic_confirmation 2 / skipped_ignore 317 / sensor_filter 102（=proactive 14 + peer_poke 30 + 空文本 25 + 命令与媒体链接等 33）/ skipped_wait 83 / stale_drop 7 / repeater 7。
- thread_id：sender:* 537 / private:* 18 / 空 30 / reply:* 0。
- 群 1062115731：executed 6 vs stale_drop 7（4 例 newer_activity_unknown_thread age 11.5-29.8s）。
- sent_segment_count stage=0 vs stats>0：67/67，全部 send_status=sent。
- 私聊：18 traces 全 executed/confirm，judge LLM 14 次，reply_age p50 44.3s max 357.4s；warm 字段"群聊"命中 45 处。
- 日志：GroupWait armed 39 / expired 37 / RESUME 1；"捕获互动事件" 31（30 peer 全过滤）；回戳 1；group_recall ≥5（RecallGuard 侧）。
