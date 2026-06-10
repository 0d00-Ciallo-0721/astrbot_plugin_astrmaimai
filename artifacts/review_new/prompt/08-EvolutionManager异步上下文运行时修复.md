# Prompt 08：`EvolutionManager` 异步上下文运行时修复

## 任务目标
修复 `get_active_patterns_canonical()` 在活动事件循环中直接抛 `RuntimeError` 的运行时风险。

本轮目标不是“把异常吞掉”，而是把 API 边界理顺：
- 该能力应该是 async，就改成 async-safe 路径
- 如果必须保留 sync 包装，就明确同步/异步边界

## 必读报告
- `artifacts/review_new/04-模块-M4-记忆与学习.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 必读代码
- `astrmai/learning/evolution_manager.py`
- `memory_engine.expression_pattern_service.render_active_patterns(...)` 的调用关系
- 任何直接调用 `get_active_patterns()` / `get_active_patterns_canonical()` 的地方

## 必须完成的修复
1. 不再让 async 上下文一调用就直接炸 `RuntimeError`。
2. 明确 sync/async API 的职责边界。
3. 补回归，覆盖活动事件循环下的真实调用场景。

## 实施要求
- 不要只改报错文案。
- 不要简单把 `asyncio.run(...)` 换个地方继续硬调。
- 优先做真正可运行的边界修复。

## 验证要求
至少执行：
- learning / memory 相关测试
- 新增最小异步上下文回归

## 完成标准
- 活动事件循环不再成为直接炸点
- 调用边界清晰
- 更新：
  - `artifacts/review_new/04-模块-M4-记忆与学习.md`
  - 如影响总判断，再更新 `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

