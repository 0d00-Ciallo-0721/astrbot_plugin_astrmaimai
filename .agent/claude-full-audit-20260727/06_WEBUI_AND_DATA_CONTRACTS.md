# 06 — WebUI 与数据契约一致性 审计报告

> 审计对象：`astrmai/webui/**`（plugin_pages.py、backend/services/*、backend/routes/*、adapters/plugin_api.py）+ 前端 `pages/admin/app.js`(2366L) + 相关 persistence / learning / memory 交叉链路。
> 方法：app.js 全文精读；plugin_pages 全部 119 条注册路由 vs 前端 75 个调用形状双向 diff（scratchpad/contract_diff.py）；4 个 WebUI 相关提交 (60f70e1 / bb10a8a / 320663f / fe12bcc) git show 核对；对 `.agent/runtime-observability-c4aee57-20260726/turn_trace_samples_server.json`(15MB) 实测解析/序列化成本；写动作逐条追到 v2_store / projector / 运行态缓存。

## 0. 结论速览

生产 API 面 = `plugin_pages.py` 经 `context.register_web_api` 注册（main.py L71）；`backend/server.py` + `routes/*` 的 FastAPI 面只用于 dev/tests（无任何生产启动点）。**前后端路径契约总体健康**：75 个前端调用形状全部有对应注册路由，无 404 级断链。真正的问题集中在三处：
1. **人工校准写动作的语义断链**（60f70e1 引入）：编辑后的表达文本在"保存并通过"时被静默丢弃；权重输入被当作 `weight-1.0` 的增量误用；auto-check 升级为 `pending_human` 的候选从人工待审队列消失。
2. **写动作与索引/维护闭环断裂**：表达审批不触发投影；`MemoryMaintenanceService.run_once`（唯一的索引修复+积压清理入口）没有任何调度器，仅存在于前端从不调用的端点上。
3. **观测面板的静默失真**：/learning/cooldowns 因属性名错位永远为空；错误响应被前端缓存为"新鲜的空数据"180 秒；trace 存储每消息全文件重写正在变成聊天路径的隐藏税。

## 1. 生产调用链实测

```
AstrBot WebUI iframe → window.AstrBotPluginPage bridge → apiGet/apiPost("admin/…")
  → register_web_api("/astrmai/admin/…") [plugin_pages.py L776-783, GET/POST/PUT/PATCH/DELETE + werkzeug 别名]
  → AstrMaiAdminPageApi._page_handler → 各 *UiService → PluginApiAdapter(facade) → runtime 组件 / get_db() 直查 SQLite
```
- 前端 `unwrapResponse`(app.js L278-286)：`status=="error"||ok===false` 抛错 → toast；含 `data` 键且有 status/runtime_bound → 解包 data；否则原样返回。`runtime_bound:false` 对用户完全不可见。
- 数据缓存：`cachedFetch` TTL 180s（L159-165），`safeFetch` 捕获一切错误返回 fallback 并 toast（5s 去重）。
- turn trace 读链：`/cognition/recent-turns` → `planner.turn_trace_store.recent()`（TurnTraceSampleStore，整文件 JSON 解析）→ fallback `planner.turn_trace_history`（进程内 300 条）。

### 双向契约 diff 结果（scratchpad/contract_diff.py）

- **前端 → 后端：0 断链**（`/tools/executions`、`/cognition/observability/timeline|search`、unified-timeline 等经变量路径调用，逐一人工核对）。
- **后端 → 前端：44 条注册路由 UI 从不调用**，其中值得注意：
  - `POST /memories/maintenance/run`、`POST /memories/diagnostics/index/repair`、整套 `/memories/migration/*` —— 治理动作有 API 无 UI（与 WU-04 相关）。
  - `POST /reviews/batch` —— 前端 `state.selectedReviews` 状态残留但批量 UI 已删除。
  - `GET /cognition/chats/{id}/unified-timeline` —— 前端 `loadUnifiedTimeline`(app.js L1166-1169) 定义后从未被调用（死代码）。
  - `POST /reviews`（create_review）、`GET /learning/expression-stats`、`GET /chats/{id}/activity` 等纯 API 面。
- schemas.py 的 pydantic 模型只在 dev FastAPI 面生效；注意 `DashboardSnapshot` 缺 `total_canonical_memories` 字段，若未来切到 FastAPI 面会把前端在用的字段过滤掉（当前无影响，仅记录）。

## 2. 逐条发现

### WU-01 (P1) "编辑通过/编辑驳回"静默丢弃人工修改后的表达文本

- 前端 `openExpressionCalibration`（app.js L710-738）在 approve/reject 时走 `POST /reviews/{id}/submit`，payload 带 `replacement: data.expression`（L661-673）。
- `ReviewUiService.submit_review`（review_ui_service.py L200-241）主路径把 replacement 传给 facade：`plugin_api.submit_review(..., replacement_expression=replacement or "")`。
- 但 `ExpressionReviewService.submit_review`（learning/review/review_service.py L96-113）在 `decision=="approved"/"rejected"` 分支**不带** `replacement_expression/apply_replacement`，只有 `revision_needed/revised/replace` 分支带（前端从不发这些 action）。
- 随后的 `extra_update`（review_ui_service.py L226-239）只补 situation/style/shared_scope/review_reason/review_suggestion，**不含 expression**。
- 结果：用户在弹窗里改好表达文本点"保存并通过"→ toast 成功 → 落库的仍是原始文本。单独"编辑"（`POST /reviews/{id}` → `update_review_record` L312-340）反而正确（带 apply_replacement）。
- 测试盲区：`tests/unit/webui/test_webui_gap_coverage.py::test_review_submit_forwards_manual_calibration_fields` 构造的 `_PluginApi.submit_review` 返回 `{"status":"deferred"}`（无 id）→ 只测了降级路径，恰好绕开了生产主路径的丢字缺陷。

### WU-02 (P1) 权重输入被当作"相对 1.0 的增量"，每次编辑通过都推高权重

- review_ui_service.py L223：`weight_delta=float(weight) - 1.0 if weight is not None else 0.0` —— 假定当前权重恒为 1.0。
- `ExpressionPatternService.update_review` L316：`metadata["weight"] = clamp(current_weight + weight_delta)`。
- 弹窗预填 `weight: item.weight ?? 1.0`（app.js L721）。若某条权重已是 2.0，用户不改动直接"保存并通过"→ delta=1.0 → 权重变 3.0；输入 1.5 想降权 0.5 的项 → 实际变 (0.5+0.5)=1.0 而非 1.5。
- 同函数的降级分支 L260-264 用 `delta = weight - current.weight`（正确算法），两条路径语义不一致，证明主路径是笔误而非设计。
- 后果：人工校准的表达权重单调漂移到 3.0 上限，表达选择排序（list_patterns 按 weight 降序，expression_pattern_service.py L199-206）被人工操作污染。

### WU-03 (P1) `pending_human` 表达候选计入"待审核"徽标，却不出现在人工待审队列

- 徽标口径：`canonical_kind_review_stats`（runtime_memory_stats.py L60-70）`pending = count(kind=expression_pattern, status=review_pending)` —— 含 review_status=pending_human 的行。
- 队列口径：`/reviews/pending` → facade → `ExpressionReviewService.list_pending_reviews` → `ExpressionPatternService.list_reviewable_patterns`（expression_pattern_service.py L209-221）→ **只保留 review_status ∈ {pending, revision_needed}**。
- 而 `expression_auto_check_task.py` L119-126 与 `expression_pattern_enricher.py` L196 恰恰把"需要人来定夺"的候选标成 `pending_human`。R09-04 修复（pending_human 退出自动审核循环）是对的，但同一个 helper 也是 WebUI 人工队列的数据源，导致**升级给人的候选人看不见**，只能去无过滤器的"表达全量"里翻。
- 旧语义对照：legacy 路径 `db.list_expression_reviews_async(statuses=["pending","revision_needed","pending_human"])`（review_service.py L46-50）和 `ReviewUiService.list_pending` 降级分支 L151-155 都包含 pending_human；`tests/regression/review/test_review_service_migrated.py::L47` 仍在断言 pending_human 出现在待审列表——但它测的是 legacy 分支。
- 用户感知："表达待审核 3"，点进表达待审 tab 空空如也（正是"计数与列表对不上"的教科书案例；页面空态文案只为黑话场景写了提示）。

### WU-04 (P1) `MemoryMaintenanceService.run_once`（索引修复+候选积压清理）没有任何自动调度，唯一入口是前端从不调用的端点

- 全仓 `run_once` 调用点检索：唯一调用方是 `memory_ui_service.run_maintenance`（L615-620）→ 路由 `POST /memories/maintenance/run`（plugin_pages.py L672）→ **契约 diff 证实 app.js 从不调用**。
- 被跳过的例行任务（memory_maintenance_service.py L45-231）：
  - `purge_jargon_candidates`：review_pending 14 天 / pending_human 14 天 / rejected 7 天过期清理 —— 从不执行 → 黑话待审队列只能靠人手清；
  - `purge_kind_candidates(expression_pattern)`：pending 21 天 / rejected 14 天 —— 同上；
  - `check_consistency + repair_consistency`（L191-228）—— 运行期唯一的索引自愈点，从不执行。
- 实际在跑的只有 `DecayService.run_once`（proactive_task.py L787 → apply_daily_decay），它只做衰减+物理删除的投影清理，不做修复/积压清理。
- 索引修复仅剩启动时一次：`memory_engine.py L313-328`（`_index_consistency_repaired` 进程级一次性标志）。
- 后果：待审积压随挖掘无限增长；WU-05 造成的投影缺口在重启前不自愈；记忆质量面板"索引异常"数字持续上涨且操作者没有对应按钮（"重建召回索引"是全量重建，语义过重）。

### WU-05 (P2) 表达审核通过/驳回不同步召回索引投影（与 canonical/jargon 路径不一致）

- `ExpressionPatternService.update_review`（L246-380）把 status→active、visibility→auto_and_tool 写库（`store.update_memory` 会同步 canonical_fts），**但从不调用 `index_projector.project/cleanup_deleted`**。
- 对照组：`MemoryUiService.update_canonical` L339-346、`update_jargon` L1185-1189、`maintenance.restore/mark_stale` 都有投影同步。
- 写入期投影：`MemoryWriteService.write` L136-147 对新写入（含 review_pending）无条件 project；维护/重启的 inactive 清理会把 pending 期投影删掉 → 审批通过后既不重投影 → `missing_projection_ids`。
- 影响面经核实是有界的：检索会用 canonical 批量水合并按 canonical status/visibility 过滤（memory_retrieval_service.py L604-648），所以不会把 pending/rejected 泄进回复；受损的是**已通过条目的向量召回缺位**（FTS 路径仍可命中），且持续到重启（结合 WU-04 无运行期修复）。质量面板"索引异常"随每次审批增长，操作者无法解释。

### WU-06 (P2) TurnTrace 样本库"每事件全文件重写 + WebUI 全文件解析"，聊天路径隐藏税并将随封顶线性放大

- `TurnTraceSampleStore.append`（turn_trace_store.py L88-115）：每次 append = 读整个 JSON → 解析 → `json.dumps(indent=2)` → 整文件重写；`recent()` L117-133 同样整文件解析；两者共用同一把 `asyncio.Lock`。
- 调用侧：`gate._finalize_pre_planner_turn`（gate.py L973-997）**await** `planner.record_turn_trace` → append —— 对**每条**入站消息执行，包括 317 条 skipped_ignore / 102 条 sensor_filter（服务器 16h 样本）；raw_trace_store 是第二次同模式全文件重写。
- 实测（本机，服务器只会更慢）：15.0MB 现文件 parse=0.21s、dumps=0.46s；平均单条 15KB；`max_global=2000` + by_chat 双份存储 → 封顶 ≈ 42MB，届时每条消息约 2s 级 CPU/IO，且 WebUI cognition 页 45s 轮询 `/cognition/recent-turns`（clearDashboardCache 后必然重读全文件）与聊天写入在同一把锁上串行。
- 佐证：服务器日志 [TurnLedger] 显示 skipped_ignore turn 也全部落盘；无持久化失败告警（说明这条税一直在交）。
- 与领域 08（runtime/persistence）交叉：修复应在存储结构（分片/append-only JSONL/SQLite），WebUI 侧的症状是 dashboard 变慢。

### WU-07 (P2) 黑话"驳回并删除"抹掉 rejected 墓碑，同一噪声词会被挖掘器重新捞回待审队列

- UI 驳回 = `reject_jargon` → `delete_jargon` → `v2_store.hard_delete`（v2_store.py L1543-1571）：canonical 行 + canonical_fts + memory_dedup_aliases + legacy Jargon 行 + 投影全删，**无任何拒绝记录残留**。
- 而挖掘去重恰恰依赖这些行：`JargonMiner.mine`（jargon_miner.py L53-66）`existing_terms` 取 `statuses=["active","review_pending","rejected","stale"]` —— rejected 行就是防重挖墓碑。
- 结果：操作者驳回一条 LLM 挖出的噪声（未被 `JargonCandidateExtractor.noise_reason` 静态规则覆盖的那类）→ 下轮挖掘同群语料再次提出 → 重新出现在黑话待审 → 无限循环。UI 文案承诺"删除后不可恢复"，但没承诺"不会再回来"。
- 设计对照：维护路径的 rejected 清理本来有 7 天 grace（保留一段墓碑期），说明墓碑语义是被认可的；UI 硬删跳过了它。

### WU-08 (P2) `/learning/cooldowns` 自诞生起永远返回空——读的属性名从未存在

- `AdminUiService.expression_cooldowns`（admin_ui_service.py L1054-1063）与 `LearningService.expression_cooldowns`（learningservice.py L37-46）都取 `_as_dict(selector).get("_recent_patterns", {})`。
- `ExpressionSelector` 的真实属性是 `_recent_pattern_keys`（expression_policy.py L383；git log -S 证实从初始提交就叫这个名，`_recent_patterns` 从未存在）。
- 于是"表达冷却"面板永远显示 `{"recent_patterns": {}}` 且 `runtime_bound: true` —— 看起来一切正常、只是"没有冷却"。排查表达重复/冷却问题时这个诊断口是假的。

### WU-09 (P2) 空数据三义性：错误被缓存成"新鲜的空数据"180 秒；`runtime_bound:false` 与真无数据不可区分

- 前端：`cachedFetch`（app.js L159-165）把 `safeFetch` 的 fallback（含错误回退）以 `updatedAt=now` 写入缓存 → 一次瞬时 bridge/后端错误 = 该 tab 3 分钟稳定空白（toast 只闪一次且 5s 去重），切 tab 往返也不重试，只有手动"刷新"按钮清缓存。
- 前端：除 persona 页（L2239-2260 有专门错误态）外，所有列表把三种情况渲染成同一句"暂无数据"（table() L440-452）：真无数据 / `runtime_bound:false`（组件未绑定）/ 后端吞掉的异常。
- 后端吞异常样本（返回 200 + 空）：`memory_ui_service.list_canonical` SQL 回退 `except Exception → items:[]`（L275-276）；`admin_ui_service._safe_count → 0`（L102-103）；`recent_turn_traces` store 异常 → 静默转进程内历史（L595-604）；`heartflow_chats` get_all_states 异常 → {}（heartflowservice.py L66-69）。
- 运营者感知即简报所述"页面空白但不知道为什么"。最小改进：cachedFetch 不缓存错误回退（或标记 stale）；table() 空态接受 runtime_bound/error 提示语。

### WU-10 (P2) 黑话/表达关键字搜索只过滤"当前页"，总数与分页按未过滤集合计算

- 服务端：`list_jargon`（memory_ui_service.py L656-672）先 `LIMIT/OFFSET` 取页再 `_filter_jargon_rows(query)` 过滤，`total` 用未过滤总数 → 搜索命中在后页时第一页显示"没有结果"但"共 N 条"很大，翻页按钮按未过滤总数排布。
- 前端再叠一层本页 filter（app.js L1520-1521 对 expression tab 是唯一过滤层，jargon tab 是第二层）→ 表达全量搜索同样只搜当前 25 条。
- 用户感知：搜索一个明确存在的词 → "当前分类暂无数据"。

### WU-11 (P3) c4aee57 的 v2 观测字段已随 API 返回但 UI 无任何呈现；工具披露表"工具"列恒为 "-"

- 简报的高危疑点核实结论：**前端读的 turn trace 字段全部存在于 v2 schema**（`build_turn_trace_summary` turn_context.py L343-633 提供 cognitive.social_intent / continuity.has_heartflow_context / tools.filtered_tools / removed_by_* / side_inputs.timings / follow_up 全套）——没有 v1 字段断链。
- 但反向缺口存在：planner L745-848 附加的 `llm_call_ledger / stage_ledger / reply_stats / budget / memory_funnel / decision_observation / turn_total_elapsed_ms` 随 `/cognition/recent-turns` 完整返回，而 `openTurnTrace`（app.js L1262-1278）只渲染 9 个固定旧区块，无 raw JSON 兜底 → 新观测能力在管理页不可见，排查延迟/预算问题仍要下载 trace 文件。
- 小项：工具"策略披露"行 `item.tool_name || item.name`（app.js L1294）恒为 "-"，因 `tool_trace_history` 条目只有 `tool_names` 数组（planner.py L666-682）。

### WU-12 (P3) 计数口径与删除反馈的小型语义错位集合

1. 学习页"表达习惯 total"/审核页"表达语料"= `count(kind=expression_pattern)` 含 deleted/rejected/review_pending（list_canonical `include_inactive` 默认 True，v2_store.py L1212）→ 语料数虚高；对照 legacy 口径（dashboard_repository L46-71）语义不同。
2. "黑话全量" tab 实际查询 `status=active`（app.js L1501）→ 是"已通过词库"不是全量；标签误导但删除按钮文案已按已通过写。
3. 无 canonical 映射的 legacy `MemoryEvent` 删除：后端返回 `{"status":"readonly", changed:false}`（memory_ui_service.py L928-933），前端不检查 changed，一律 toast"记忆记录已删除"（app.js L1884）且行刷新后仍在。
4. Dashboard "待审核项"仅统计表达（不含黑话待审），与 Reviews 页四格口径不同。

## 3. 管理动作 → 运行态生效性总表（Q3 答案）

| 动作 | DB | FTS | 向量投影 | 运行态缓存 | 生效时点 |
|---|---|---|---|---|---|
| 表达 批准/驳回 (/reviews/{id}/submit) | ✔ | ✔(update_memory._sync_fts) | ✘（WU-05） | 无缓存，选择器每轮直查 store | 提示词注入下一轮即生效；向量召回要等重启 |
| 表达 编辑保存 (/reviews/{id}) | ✔（文本生效） | ✔ | ✘ | 同上 | 同上 |
| 黑话 批准 (/memories/jargon/{id}/approve) | ✔ | ✔ | ✔ projector.project | 挖掘 existing_terms 每轮直查 | 下一轮生效 |
| 黑话 驳回 (= 硬删) | ✔ 物理删 | ✔ | ✔ cleanup | 墓碑消失 → 可能被重挖（WU-07） | 立即；副作用滞后出现 |
| canonical 修订/删除/恢复/过期/隔离 | ✔ | ✔ | ✔（project/cleanup） | — | 下一轮生效 |
| 用户画像编辑/切片 | ✔ | — | — | ✔ replace_cached_profile + relationship align（12-01 已修复且现状良好） | 立即 |
| persona slices 编辑 | ✔ cache 文件 | — | — | ✔ summarizer.cache 同步（带锁） | 下一轮（UI 文案如实） |
| 记忆反馈禁用 | ✔ soft_delete（mem_ 前缀走持久路径） | ✔ | ✔ | 内存禁用键 7d TTL | 立即 |
| 清理 heartflow cooldown / chat runtime | 内存 | — | — | ✔ 直接改 manager 私有 dict | 立即 |

## 4. 领域级测试缺口

- `tests/test_webui_backend_refactor.py`（54 测试）覆盖了服务装配、降级路径、防"吞错成空"（如 `test_review_ui_service_does_not_mask_bound_runtime_failures_as_empty_pending_list`），但：
  1. **无生产主路径的校准断言**：approve+replacement 落库、weight 绝对值落库（现有 gap-coverage 测试用 deferred 桩恰好绕开主路径，见 WU-01）。
  2. **无 pending_human 出现在人工队列的 canonical 路径断言**（regression 测试只锁 legacy 分支）。
  3. **无 双向契约测试**：app.js 调用集合 vs plugin_pages 注册集合（本审计的 contract_diff.py 可直接改造成测试）。
  4. **无 审批→投影一致性断言**（approve 后 check_consistency 应为 0 missing）。
  5. **无 maintenance run_once 调度存在性断言**（防止治理任务成为孤儿端点）。
  6. **无 trace store 体量/性能护栏**（append 复杂度或文件上限）。

## 5. 附录：分析脚本输出摘要

- contract_diff.py：frontend 75 形状 / backend 119 路由；前端缺口 0；后端未用 44（正文列关键项）。
- trace 文件实测：size=15.0MB，recent=585，by_chat=392（15 chats）；parse=0.21s，dumps(indent=2)=0.46s；avg entry=15KB；按 max_global=2000+by_chat 封顶投影 ≈ 42MB。statuses: skipped_ignore 317 / sensor_filter 102 / skipped_wait 83 / executed 67+2 / stale_drop 7 / repeater_echo 7。
