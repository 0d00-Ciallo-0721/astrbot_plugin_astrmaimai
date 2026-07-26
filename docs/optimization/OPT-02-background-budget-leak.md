# OPT-02 后台任务 contextvar 预算泄漏（记忆抽取复活）

状态：代码完成（待线上日志复采验收） ｜ 优先级：P0 ｜ 依赖：无 ｜ 覆盖发现：RT-01(P0，已吸收 ML-01/PL-08) ｜ 单一根因，三处受害面：后台记忆抽取 100% 死亡、judge attempt 账本丢失过半、预算 clamp 错轮。

## 目标

- 常驻后台 worker（event_bus 3 个 worker + memory per-chat worker + session worker）里的 LLM 调用不再继承某个早已过期的 turn deadline。
- 基线 → 目标：instant 记忆 LLM 兜底 17/17 失败 → 成功率恢复正常；`turn_deadline_exhausted` 日志 71 条/16h → 仅出现在真实预算耗尽的轮内；judge attempt 账本丢失 278/539 → ≈0。

## 基线证据

- 双轨记账：`gateway_call.py:283-289` 的 `clamp_timeout_to_turn_budget`/`record_llm_attempt` 恒传 `event=None`，走 `_CURRENT_TELEMETRY` contextvar（`turn_call_ledger.py:215-232`）。
- contextvar 随 `asyncio.create_task` 拷贝：`event_bus.py:209-216` 的 worker 池在 `publish()` 内**懒启动**（第一次发布必然发生在某个 turn 的处理上下文里），`memory_turn_pipeline.py:170-177` 的 per-chat worker 同理——worker 永久携带第一轮 telemetry，360s 后 `remaining=0`，此后 worker 内所有调用秒抛 `asyncio.TimeoutError("turn_deadline_exhausted")`。
- 实证：日志 `turn_deadline_exhausted` 71 条（gateway_call 46 / gateway_tasks 6 / instant_memory_gate 17）；`instant llm backfill degraded` 17 条且 16h 内**零成功** backfill；运行时代理与记忆代理从两个入口独立命中同一根因（交叉验证记录见 `claude-audit-integration-20260727.md`）。

## 方案决策

两个修法二选一或组合，推荐 **(a)+(b) 双保险**：

- **(a) 显式透传**：`clamp_timeout_to_turn_budget`/`record_llm_attempt` 增加显式 event 参数，`chat_in_lane_result → _elastic_call_result` 链路把 event 传下去——轮内调用不再依赖 contextvar，根治双轨。
- **(b) worker 出生即斩断**：提供 `detach_turn_telemetry()` helper（清空 `_CURRENT_TELEMETRY` 再进入循环），在 `event_bus._worker_loop`、`memory_turn_pipeline._chat_worker`、`gate._spawn_session_worker` 入口调用——后台任务天然无 turn 预算。

## 实施步骤

1. 先写失败测试（OPT-13/TG-03 关联）：在一个 scope 内 spawn 常驻 worker，scope 过期后经 worker 调 `_elastic_call_result`，断言不被陈旧 deadline 拦截（当前红）。
2. 实现 (b)：`detach_turn_telemetry()` + 三个 worker 入口调用；这是最小止血，独立提交。
3. 实现 (a)：event 显式透传链路；同时核对 `record_llm_attempt` 落账目标——修复后 judge attempt 应回到本轮 ledger。
4. 回归：`python -m pytest tests/test_turn_call_ledger_refactor.py tests/unit/memory -q` + 全量 pytest。
5. 部署后取证：grep 新日志 `turn_deadline_exhausted` 应仅在 budget.exhausted=true 的轮出现；`analyze_turn_ledger.py` 看 instant backfill 成功计数 > 0、judge attempts 填充率恢复。

## 验收标准

- 新增 contextvar 继承测试 + 现有 ledger 测试全绿。
- 部署 24h：`instant llm backfill degraded` 计数 = 0（或仅偶发且原因非 deadline）；executed 轮 judge ledger 条目 `attempts>=1` 占比 >95%（基线 48%）。
- 轮内预算语义不回归：`test_turn_budget_clamps_noncritical_timeout_and_keeps_reply_reserve` 保持绿。

## 风险与回退

- **中风险**：改动触及所有 gateway 调用的预算路径。缓解：(b) 先行（只影响后台 worker，改动面极小）；(a) 跟进时保留 contextvar 作为 event 缺失时的回退，行为差异有测试锚定。
- 回退：两步独立提交，revert (a) 不影响 (b) 的止血效果。

## 完成记录

**2026-07-26 代码侧完成**（线上日志/trace 复采验收待部署后执行）：

- 审计阶段的一个事实修正：`turn_telemetry_scope` 的唯一入口在**根目录 main.py:224**（审计时只 grep 了 astrmai/ 子目录一度误判 scope 未被使用）；contextvar 复制链经逐层核实成立，且确认 `begin_llm_call` 在无上下文时安全 no-op（detach 不会引起后台调用崩溃）。
- 改动文件（4 个）：
  - `turn_call_ledger.py`：新增 `detach_turn_telemetry()`（斩断继承）与 `rebind_turn_telemetry(event)`（长驻 worker 逐批重绑），入 `__all__`。
  - `event_bus.py::_worker_loop`：入口 detach——publish() 懒启动的 3 个常驻 worker 不再携带首轮 deadline（订阅回调经 worker 派发，一并干净）。
  - `memory_turn_pipeline.py::_chat_worker`：入口 detach——instant backfill 不再被陈旧预算钳死（对应线上 17/17 全败）。
  - `gate.py::_debounce_and_judge`：排水循环每轮迭代 `rebind_turn_telemetry(batch_events[-1])`——晚到批次的 judge/mood 调用按本批 deadline 钳制、账本落回正确 turn（对应 278/539 judge attempt 丢失与跨轮误记）。方案比 OPT 文档原稿多出这一项：decision_router 的 judge clamp 本就是 event 级正确，真正错的是 gateway 层 contextvar 记账，逐批 rebind 同时修复钳制与归账，显式 event 全链路透传(方案 a)因此不再必要，避免了大改动面。
- 新增测试：`tests/test_turn_budget_context_leak_refactor.py` 4 条（继承机制文档化 + rebind 单元 + EventBus/MemoryPipeline 两条生产 wiring）。**行为级红验证**：git stash 掉两个 worker 文件后 wiring 测试精确变红（remaining=0.0 而非 None），恢复后 4/4 绿。
- 回归：受影响区域（gate/memory/kernel/regression）176 passed；全量套件绿（除 OPT-01 记录在案的 3 个 signin 时间窗历史 flaky）。
- 待部署验收：`instant llm backfill degraded` 计数应归零；`turn_deadline_exhausted` 仅出现于 budget.exhausted=true 的轮；executed 轮 judge ledger `attempts>=1` 占比 >95%（基线 48%）。
