# 开发窗口 07：Conversation/Planning/Execution 修复

## 必须先读取的审查报告
1. `artifacts/reviews/r03-conversation.md` — 完整发现清单（3🔴 10🟡 7🟢）
2. `artifacts/reviews/r15-master.md` — 总报告
3. `artifacts/reviews/r13-session-fixes.md` — 了解本轮已修复（D22）

## 目标文件
- `astrmai/conversation/planning/planner.py` — Planner 决策核心
- `astrmai/conversation/planning/cognitive_loop.py` — 认知循环
- `astrmai/conversation/planning/conversation_continuity.py` — 对话连续性
- `astrmai/conversation/planning/planner_side_inputs.py` — Planner 侧输入
- `astrmai/conversation/execution/executor.py` — 执行器（877 行）
- `astrmai/conversation/execution/reply_artifact_builder.py` — 回复构建

## 依赖
窗口 06（attention）+ 窗口 02（gateway）

---

## 🔴 严重（3 项）

详见 `r03-conversation.md` 的 🔴 部分：
1. Planner/CognitiveLoop 决策链的边界条件
2. Executor text_mode/tool_mode 的 stale_drop 路径
3. conversation_continuity 状态更新逻辑

---

## 🟡 中等（10 项）

详见 `r03-conversation.md`，重点：
- wait/ignore 分支的 continuity 记录使用 `lightweight_event=True`（D22 已文档化为有意设计）
- `executor.py` stale_drop 的 debug_trace 信息不足（D49）
- `planner.py` wait/ignore 与正常 reply 路径收尾重复（D50）
- `planner.py` `_adjust_expression_habits_for_behavior` 合并策略健壮性（D51）
- `conversation_continuity.py` `ContinuitySnapshot` 字段膨胀 ~120 字段（D52）
- executor 中 `_inject_direct_vision_context` tempfile 清理（D54 已修复）
- `planner_side_inputs.py` wait 跳过与 CognitiveLoop 判定语义重叠（D23）

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_planner_cognitive_loop_refactor.py tests/test_chat_loop_kernel_refactor.py tests/original_ported/test_dialog_focus_continuity_regression_ported.py tests/regression/conversation/ -q
```

## 成功标准
- 🔴 3 项修复
- Conversation 相关测试全部通过
