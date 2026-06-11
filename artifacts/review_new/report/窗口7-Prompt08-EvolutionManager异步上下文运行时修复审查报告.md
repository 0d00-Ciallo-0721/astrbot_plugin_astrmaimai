# 窗口7-Prompt08-EvolutionManager异步上下文运行时修复审查报告

## 审查范围
- `astrmai/learning/evolution_manager.py`
- `astrmai/conversation/planning/planner_side_inputs.py`
- `tests/test_learning_refactor.py`
- `tests/test_planner_side_inputs_refactor.py`
- `tests/test_learning_event_collaboration_refactor.py`
- `tests/test_planning_input_loader_refactor.py`
- `tests/test_planner_cognitive_loop_refactor.py`
- `artifacts/review_new/04-模块-M4-记忆与学习.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 审查结论
- 本轮深度代码审查未发现新的 P1/P2 级问题。
- `EvolutionManager` 的 active patterns 读取边界已形成闭环：
  - 已新增 `get_active_patterns_canonical_async()` 作为正式 async 入口。
  - 同步包装 `get_active_patterns_canonical()` / `get_active_patterns()` 已收紧为 sync-only 兼容接口。
  - 遗留 `PlannerSideInputMixin._load_planning_side_inputs()` 已切到直接 `await` async API，不再依赖 `to_thread + sync wrapper`。
- 本窗口相关历史遗留问题已清理完成；当前剩余仅为兼容边界上的受控误用风险，不构成未闭环缺陷。

## 已确认事实
### 1. async 正式路径已收口
- `get_active_patterns_canonical_async(chat_id, limit=5)` 直接 `await expression_pattern_service.render_active_patterns(...)`。
- 活动事件循环中的正式调用方不再经过同步包装，因此不再命中旧的直接运行时炸点。

### 2. sync/async 边界已明确
- 同步包装在无活动事件循环时仍保持兼容，可继续返回字符串结果。
- 若在活动 loop 中误调同步包装，现在会抛出明确的 sync-only 边界错误，并引导调用 async API。
- 该行为与本轮文档更新结论一致，未发现“代码已改、报告未对齐”或“报告先于事实”的问题。

### 3. 遗留调用链已复核
- 仓内生产代码中，`PlannerSideInputMixin._load_planning_side_inputs()` 是本轮唯一需要收口的直接调用边界，现已切换。
- 当前真实主链路 `Planner -> PlanningInputLoader` 未被本轮扩大改动范围，行为保持不变。
- 对 Prompt06 / Prompt07 在汇总报告中的“已复核”结论追加核验后，未发现与当前仓内状态冲突的遗留问题。

## 审查验证
### 已执行测试
- `python -m unittest tests.test_learning_refactor tests.test_learning_event_collaboration_refactor tests.test_planner_side_inputs_refactor tests.test_planning_input_loader_refactor -q`
- `python -m unittest tests.test_learning_refactor tests.test_learning_event_collaboration_refactor tests.test_planner_side_inputs_refactor tests.test_planning_input_loader_refactor tests.test_planner_cognitive_loop_refactor -q`
- `python -m unittest tests.unit.state.test_group_reply_wait_manager_concurrency_migrated tests.original_ported.test_group_reply_wait_manager_ported tests.test_proactive_scheduler_refactor -q`

### 已覆盖的关键场景
- 活动事件循环内调用新 async API，可正常返回 active patterns。
- 活动事件循环内误调同步 API，会得到明确边界错误。
- 纯同步上下文调用同步 API 仍保持兼容。
- 遗留 `PlannerSideInputMixin` 路径已确认走 async API，不再依赖同步包装。
- Prompt06 / Prompt07 相关测试复跑通过，可支撑汇总报告中的已修复结论。

## 备注
- 复测过程中可见仓内既有 `DeprecationWarning` 与运行日志输出，但未发现与本窗口改动直接相关的新 warning 类型或失败。
- 审查中额外搜索到一些手工脚本和旧测试 stub 仍只提供 `get_active_patterns()`；当前仓内无生产调用点再使用这些对象走 `_load_planning_side_inputs()`，因此本轮不构成实际缺陷。

## 最终结论
- 本窗口无历史遗留问题。
- `Prompt08 / EvolutionManager 异步上下文运行时修复` 已通过深度代码审查。
