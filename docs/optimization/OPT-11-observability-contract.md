# OPT-11 观测契约完整性（funnel / 口径 / trace 存储性能）

状态：**已完成**（含 G6 焦点冷却、G8 结构迁移） ｜ 优先级：P2 ｜ 依赖：无（OPT-08 的量化验收依赖本 OPT 的口径修正，建议先做 RT-02 部分） ｜ 覆盖发现：TG-04(P2)、ID-05(P2)、RT-10(P3)、RT-02(P2)、WU-06(P2)、WU-11(P3) ｜ 本轮审计三次差点被观测层误导（judge"已修复"假象、dispatch abandoned 伪影、funnel 缺失误判），这些失真必须清掉。

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

### G6 补充（2026-07-26）：RT-02 附带 —— judge 焦点冷却

RT-02 的口径修正（analyze 脚本按 pool 判定）已在本 OPT 完成；其附带的**机制问题**在此补齐：
`gate.py` 把 IGNORE 的 focus 放回 attention window，下一批仍可能选中同一事件重复判决
（线上实测同一 focus 150s 内判 10 次；judge 池 539 次调用中 521 次花在最终被忽略的消息上）。

- `gate.py` IGNORE 分支：累加 `astrmai_judge_ignored_rounds`（开关关闭时不累加）。
- `focus_selector.score_focus_candidate`：按轮次线性降权，reason 标 `judge_ignored_cooldown`。
- **强唤醒豁免**（本改动最大风险面——群聊唤醒灵敏度）：@bot / 回复 bot / 点名 / 直接视觉
  永不受冷却影响，四种信号各有独立断言锁死。
- 配置：`attention.judge_ignore_focus_cooldown_enabled`（默认开）+
  `judge_ignore_focus_penalty`（默认 150，0 等同关闭），schema + pydantic 双侧登记。
- **过程纠错**：初版写 `int(getattr(...) or 150)`，penalty=0（"关闭降权"的合法取值）被
  falsy 吞成 150，被自己的负向用例抓出——与 OPT-10 的 PL-03 同类陷阱，已改为显式 None 判定。

测试：`tests/regression/conversation/test_judge_focus_cooldown.py` 9 条（红验证 5 红），
含源码级装配断言防止实现被摘掉。全量回归 **1820 passed, 1 skipped**。

### G8 补充（2026-07-26）：WU-06 结构迁移 —— append-only JSONL

短期缓解（去 indent）之上完成结构迁移：整文件 JSON 读改写 → append-only JSONL。

**实测收益**（同机同数据）：

| 文件大小 | 单条 append |
|---|---|
| 3.6MB | 0.47ms |
| 7.6MB | 0.53ms |
| 11.6MB | 0.50ms |
| 15.6MB | 0.62ms |

对照基线（旧整文件重写）：15MB 时 parse 0.21s + dumps 0.46s ≈ **700ms/条** → 约 1100 倍，
且**不随文件增长劣化**（旧实现是线性劣化）。

**语义变化（重要，已在测试中固化）**：append-only 下同 turn_id 的旧行**不再被物理删除**，
由读取端「后写覆盖先写」去重，压实时物理合并。因此 per-chat 上限从"每次写入即刻生效的
瞬时不变式"变为"压实后的保留保证"；`recent()` 额外兜底 `max_global` 截断，保证对外语义不变。

- 写入端：`_append_line_sync` + `_maybe_compact_sync`（超 `max_global × 2` 触发压实，
  tmp+replace 原子替换）；首次写入自动迁移 legacy 整文件历史（`_migrate_legacy_sync`）。
- 读取端：`recent()` 读尾部行、去重、按 max_global/limit 截断；JSONL 不存在时回落 legacy
  整文件——`.agent/runtime-observability-*` 的归档快照继续可读。
- 崩溃鲁棒：截断的半行跳过而非整份报废。
- 分析脚本：`load_traces` 双格式；新增 `scripts/convert_turn_trace_to_jsonl.py`（**真实归档
  实测**：585 样本转换后分析结果与原格式一致）。

**测试**：`tests/regression/architecture/test_trace_store_jsonl.py` 12 条（append-only 字节前缀
断言 / 去重 / 压实上限 / 半行容错 / legacy 回落与迁移 / 分析脚本双格式），红验证 **6 红**。
既有 4 条测试按新格式更新（3 条直接读 `.json` 文件的断言改读 JSONL，行为断言全部保持不变）。

全量回归 **1841 passed, 1 skipped**。

### G9 补充（2026-07-26）：trace 字段契约锚定测试

OPT-11 步骤 2 的遗留项。c4aee57 宣称 "complete turn trace observability" 却没有任何测试
锚定"executed trace 必须包含哪些字段"——正因如此，`memory_funnel` 在 executed 轮
64/67 缺失才能一路溜进生产（运营者排查"记忆为什么没注入"时 96% 的轮次无数据，
无法区分"合理跳过"与"仪表坏了"）。

**测试**：`tests/regression/architecture/test_trace_field_contract.py` 5 条。
必备字段集固化为 `llm_call_ledger` / `stage_ledger` / `context_block_stats` /
`memory_funnel` / `reply_stats` / `budget`，另加 `decision_observation` 对四种 status
（executed / skipped_wait / skipped_ignore / stale_drop）全覆盖。

**过程记录（重要，避免后人写出假绿测试）**：初版直接往 event 上塞
`astrmai_llm_call_ledger` 等 extra，结果 ledger 断言恒为 0 条——`_remember_turn_trace`
**优先读 telemetry 快照**，只有快照缺失才回落 extras，塞 extra 的路径整个被盖掉。
改为走真实仪表 API（`begin_llm_call`/`finish_llm_call`/`observe_stage`/
`record_context_block_stats`/`record_reply_stats`，包在 `turn_telemetry_scope` 内）
才测到真实组装路径。同理，trace 组装是 `_remember_turn_trace`（planner 方法）而非
`build_turn_trace_summary`（`contracts/turn_context.py` 模块级函数）——后者只产出基底，
观测字段是在 planner 里 merge 进去的，只测前者会漏掉整个 merge 段。

**红验证**（prompt_refiner.py 无本地改动，OPT-11 外包裹已在 4765ebf 提交，
故改用源码回滚而非 stash）：把 `prompt_refiner.py` 回滚到 46739d3（外包裹之前）+
从 planner 的 `item.update` 里删掉 `stage_ledger` 一行 → **2 红 3 绿**，
红项精确命中"字段缺失"与"外包裹缺失"；还原后 **5 绿**。

`MemoryFunnelWrapperContractTests` 用源码级断言锚定外包裹结构（`_decide_memory_injection`
必须在 `_decide_memory_injection_inner` 之前、且包裹体内写 `astrmai_memory_funnel`
的 skipped 记录）——funnel 的补写发生在 prompt_refiner 内部，行为级测试要拉起整条
记忆决策链才能覆盖，源码级锚定在此处性价比更高，且能挡住"未来新增 early-return
绕过包裹"这一真实回归形态。
