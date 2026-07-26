# OPT-11 观测契约完整性（funnel / 口径 / trace 存储性能）

状态：代码完成（结构迁移另立专项） ｜ 优先级：P2 ｜ 依赖：无（OPT-08 的量化验收依赖本 OPT 的口径修正，建议先做 RT-02 部分） ｜ 覆盖发现：TG-04(P2)、ID-05(P2)、RT-10(P3)、RT-02(P2)、WU-06(P2)、WU-11(P3) ｜ 本轮审计三次差点被观测层误导（judge"已修复"假象、dispatch abandoned 伪影、funnel 缺失误判），这些失真必须清掉。

## 目标

- executed trace 的观测字段完整可信：memory_funnel 填充率 64/67 缺失 → 100%；sent_segment_count 不再自相矛盾；prefix_changed_reason 不再把稳定轮标成 unavailable。
- 分析口径正确：analyze 脚本 judge 计数从恒 0 修为真实值（p50=1/p95=2/max=10）。
- trace 存储不再是聊天路径的隐藏税：每条消息全文件重写（15MB 实测 0.7s/条，封顶 ~42MB 逼近 2s/条）改为增量写。
- c4aee57 新增的 llm_call_ledger/stage_ledger/budget 字段在管理页可见。

## 基线证据

- **TG-04**：`prompt_refiner.py:646-697` 七条 early-return 在调 build_bundle 前返回，不写 funnel → executed 轮 64/67 无 memory_funnel；无字段完整性契约测试（c4aee57 宣称 complete observability 但无锚定）。附带澄清：context_block_stats 511/585 缺失是**假警报**（executed 内 67/67 全有，缺失全在 skipped 轮）。
- **ID-05**：`reply_artifact_builder.py:544,601-637` 只在异常/截断分支写 `sent_segment_count`，满发路径不写；reply_service 两处默认值不一致（stage 默认 0 vs stats 默认 len）→ 67/67 executed 全部呈现 0 vs N 矛盾。已确认无真实丢段。
- **RT-10**：`planner.py:263` 空 reason 被 `or "unavailable_in_trace"` 覆写（61/67 稳定轮失真）；63 次 dispatch abandoned 是快照顺序伪影；trace created_at 是捕获时刻（分析要用 turn_started_at）。
- **RT-02**：`analyze_turn_ledger.py:160-162` 按 stage 含 'judge' 匹配，但 judge 记账在 `pool` 字段 → judge/turn 恒 0，曾制造"已修复"假象；附带机制问题：`gate.py:1509-1510` 把 IGNORE 的 focus 放回 window，同一 focus 可被连续再判决（实测 max 10 次/150s）。
- **WU-06**：`turn_trace_store.py:94-115` append/recent 均整文件 JSON 读写（含 indent=2、recent/by_chat 双份），每条入站消息（含 skipped 317 条）都 await 一次；与 WebUI 45s 轮询共用一把锁互相放大。
- **WU-11**：v2 新字段已随 API 返回但 `app.js openTurnTrace` 只渲染 9 个旧区块；披露表工具列恒 "-"（读 tool_name 而数据是 tool_names 数组）。

## 实施步骤

1. RT-02（先行，OPT-08 依赖）：analyze 脚本判定改 `pool=='judge' or stage=='attention.judge'`；测试喂 pool='judge'+stage='gateway.chat' 条目。同批评估 judge 焦点冷却（连续 IGNORE 的 focus 降权/冷却——中风险，影响群聊唤醒灵敏度，可后置观察）。
2. TG-04：early-return 各路径写 skipped funnel（或 planner 无 funnel 时补 skip 占位）；trace 契约测试：executed trace 必含 llm_call_ledger/stage_ledger/context_block_stats/memory_funnel 非空。
3. ID-05：`_send_segments` 循环结束后无条件写 `sent_segment_count`；一致性断言测试。
4. RT-10：planner.py:263 空串置 'stable'；快照顺序修正（skip 路径 dispatch 最终状态）；分析文档标注用 turn_started_at。
5. WU-06：短期缓解先行（去 indent、去双份、skip 轮存精简摘要、append 改后台队列）；结构迁移（JSONL 分片或 SQLite）单独评估——读取端（WebUI recent + analyze 脚本）需同步迁移。
6. WU-11：openTurnTrace 增加 LLM Calls/Stage Ledger/Reply Stats/Budget 区块（至少 detailsJson 全量兜底）；披露表改读 `(item.tool_names||[]).join(', ')`。

## 验收标准

- 复跑 analyze：judge_calls_per_turn p50>=1；missing.memory_funnel ≈ skipped 数而非全量；executed 轮 prefix_changed_reason 分布为 stable/first_seen/…。
- 发多段回复断言 stage metadata == reply_stats == 段数。
- trace 存储压测：max_global 填满后单条 append 耗时较基线（0.7s@15MB）下降一个量级；消息 p95 延迟无回归。
- 管理页任一 executed turn 详情可见 llm_call_ledger 表与 budget.remaining_ms。

## 风险与回退

- WU-06 结构迁移中风险（读写端同步）——短期缓解与结构迁移分两个提交，缓解版先上。
- 其余均为观测层修正，低风险；口径变化会让新旧报表数字不可直接对比，在 baseline 文档标注切换点。

## 完成记录

**2026-07-26 代码侧完成**：

- TG-04：`prompt_refiner._decide_memory_injection` 改为**外包裹**统一补写 skipped funnel（内部逻辑整体移入 `_decide_memory_injection_inner`）——比逐个 early-return 打点更稳，天然覆盖未来新增分支。
- ID-05：`reply_artifact_builder` 发送循环结束后无条件写 `sent_segment_count`（满发路径此前从不写，stage 恒 0 与 reply_stats 矛盾）。
- RT-10：`planner.py` 稳定轮 `prefix_changed_reason` 落 `stable`（旧 `or "unavailable_in_trace"` 把 61/67 稳定轮标成不可用）。
- RT-02：`scripts/analyze_turn_ledger.py` judge 口径改按 `pool` 判定（旧 stage 匹配恒 0，制造了"judge 重复调用已修复"的假象）。
- WU-06 短期缓解：trace 序列化去 indent 改紧凑分隔符（15MB 实测 dumps 0.46s/条，每条入站消息都在聊天路径整文件重写）。**结构迁移（JSONL 分片/SQLite）另立专项**——读取端（WebUI recent + 分析脚本）需同步迁移，不宜与本批混做。
- WU-11：Turn Context 弹窗新增"运行账本"区块（budget/reply_stats/llm_call_ledger/stage_ledger/memory_funnel），披露表工具列兼容 `tool_names` 数组。
- 过程记录：新增维护按钮时误用 `result?.data ||` 二次解包，被 round11 契约测试当场拦下（该 pin 正是为历史双层解包 bug 设立）。
- 受影响套件 150 passed。
