# OPT-06 记忆读取链恢复（注入率 2.9% → 目标区间）

状态：代码完成（待注入率/时延复采） ｜ 优先级：P1 ｜ 依赖：OPT-02（已完成） ｜ 覆盖发现：ML-05(P1)、ML-02(P1，已吸收 RT-12)、ML-06(P2) ｜ 记忆系统"写入持续膨胀、读取近乎为零"失衡的读取侧三连修。

## 目标

- 用户问"我叫什么名字""我喜欢吃什么"这类身份/偏好问题时，bot 能读回已写入的事实（当前私聊 19 条回复 0 注入）。
- 深度检索（think>=3）不再把 turn 拖到 50-92s 导致回复被 stale 丢弃——深度记忆功能当前实际不可用（3 次触发 2 次超时丢弃）。
- 发送后的 claim 抽取不再阻塞 turn 5-44s。
- 基线 → 目标：注入率 2.9%（执行轮 2/69）→ 结合产品预期设定目标区间（建议先放到 15-30% 观察 token 成本）；memory.injection p50 29.8s/max 92s → p95 < 10s。

## 基线证据

- **ML-05**：`prompt_refiner.py:678-684` 三重门——think<=0 直接 none（52 次）、think==1 需命中 10 词关键词表（`MEMORY_INTENT_KEYWORDS`：记得/之前/上次/回忆/想起…，13 次未命中跳过）、fast_mode 跳过；identity/preference 类问句不含触发词。72% 执行轮 think0。
- **ML-02**：`memory_retrieval_service.py:906-916` 的 `_call_deep_json`（rerank/guidance 两次 LLM）不传 lane/timeout/预算钳制，各吃默认 API 超时；query_rewrite 8s 硬限在 p50≈7.4s 的池上几乎必超时（白烧 8s）；对 2-3 条候选也照跑 rerank。trace 实证：injection 50.2s→stale_drop、92.2s abandoned + 71.9s 重试→stale_drop。
- **ML-06**：`reply_service.py:198-203` 发送后内联 `await _ingest_memory_turn`→instant gate 命中特定类别且规则无 claim 时**同步**调 LLM 抽取——ledger 实测 7 次 5.2~44.5s，全在 executed turn 内，拖长同 chat 后续处理。

## 方案决策

- ML-05 放宽是**产品决策+工程实现**的组合：推荐复用 `MemoryQueryBuilder.QueryIntentClassifier`（已存在），identity/preference/location 意图在 think1 放行；think0 私聊可选提供 FTS-only 轻量注入（不走深链路，成本可控）。先在配置里做成可调门槛，灰度观察 token 成本与 near_context 冲突后定稿。
- ML-02 是纯止血：无界等待变有界（降级路径已存在，异常即回退）。

## 实施步骤

1. ML-02：`_call_deep_json` 加 `timeout_override=clamp_timeout_to_turn_budget(reserve_for_reply=True)` 与 lane_key；`_rerank_candidates` 在 `len(candidates)<=query.top_k` 时直接返回；query_rewrite 池不可用时跳过而非等满 8s；react_retriever 的 step 循环整体包 `asyncio.wait_for`（RT-12 合并项）。单测：mock 慢 LLM 断言 deep 路径总时延不超 turn 预算；候选≤top_k 时跳过 rerank。
2. ML-05：think1 门放宽接入意图分类器 + 配置开关；回放"我叫什么名字"断言 `memory.injected=true`。
3. ML-06：instant-gate 的 LLM 部分改投递到 pipeline 后台 worker（**必须在 OPT-02 合入后**，否则后台必死）；断言 post-send 摄入不再出现在 executed turn 的 llm_call_ledger。
4. 部署后一周 trace 复采：注入率、memory.injection 分位数、think3 轮 stale_drop 率。

## 验收标准

- 三项单测绿 + 全量 pytest 绿。
- 部署复采：think>=3 轮 memory.injection p95 <10s 且不再因深检索 stale_drop；私聊 identity/preference 问句注入命中；executed turn ledger 无 memory_global_summary 长调用；token 成本增幅在灰度预算内（观察 usage_input_tokens 池级变化）。

## 风险与回退

- ML-05 **中风险**：放宽后注入次数上升，token 成本与 near_context 冲突需监控；配置开关保底，可回默认收紧。
- ML-02 低风险：把无界变有界，降级路径已有。
- ML-06 低风险：写入本就是异步语义，仅时序后移；依赖 OPT-02 就绪。
- 各项独立提交可单独 revert。

## 完成记录

**2026-07-26 代码侧完成**（注入率/时延复采待部署后执行）：

- 审计后事实修正：`_rewrite_queries` 在 20bb585 后已具备预算钳制 + 硬取消 + budget_exhausted 跳过（ML-02 的 query_rewrite 半边已修），本 OPT 只补真实缺口。
- ML-02 改动：`memory_retrieval_service._call_deep_json` 加 turn 预算钳制（clamp 12s 上限、预算 <0.5s 直接跳过）+ lane_key + `max_retries_override=0`，rerank/guidance 两个调用点带 `scope_id`；`_rerank_candidates` 候选数 ≤ top_k 时跳过整次 LLM；`react_retriever.retrieve` 整个迭代循环受 turn 预算约束（20s 上限、预算尽即收束到已收集信息），`_react_step` 逐步携带 `timeout_override`（≤8s）。
- ML-05 改动：`prompt_refiner` 新增 `_think1_memory_gate_passes`——关键词未命中时复用 `QueryIntentClassifier` 语义意图（identity/location/food_preference/preference_general/dislike/recent_reference）放行；新配置 `memory.think1_semantic_intent_enabled`（默认开，schema+pydantic 同步添加，遵守 OPT-10 的一致性教训）。
- ML-06 改动：`MemoryClaimExtractor.extract` 增加 `allow_llm` 参数；instant gate 内联路径 `allow_llm=False`（只做规则抽取），LLM 精炼交给 OPT-02 复活的后台 backfill worker。**权衡记录**：规则命中但无结构化 claim 的写入将以无 claim 形式立即落库（不再产生 authority EAV），换取 turn 不被 5-44s 内联 LLM 拖长；如后续要求这类写入也有 LLM claim，应扩展 backfill 对 gate-hit 轮的补抽取，不要回退内联。
- 测试：新增 `tests/regression/memory/test_memory_read_path_hotfix.py` 12 条；**stash 红验证 10/12 变红**。既有测试更新 1 处：`test_deep_retrieval_reranks_and_attaches_guidance` 补第三条记忆使候选数 > top_k（rerank 真实运行），断言按新契约放宽。memory 全套 191 passed。
- 待部署验收：一周 trace 注入率（基线 2.9%）与 token 成本增幅；`memory.injection` p95 <10s 且深检索不再 stale_drop；executed turn ledger 无 memory_global_summary 长调用。
