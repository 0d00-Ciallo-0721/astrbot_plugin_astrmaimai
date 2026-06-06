# 开发窗口 05：Conversation Execution — Stale/Fallback/资源泄漏修复

## 必须先读取的审查报告
1. `artifacts/review_new/r04-conversation-execution.md` — 3🔴 7🟡 6🟢

## 审查范围
`astrmai/conversation/execution/` + `decision/` + `loop/`（19 个源文件）

---

## 🔴 严重（3 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `executor.py:564-570` | **stale_drop 与模型池耗尽路径隐式 fallback 冲突**。第二模型 freshness 过期后 `return None` 但 `_handle_fatal_fallback` 被调用，向用户发送 fallback 文本。**修复**：`_handle_fatal_fallback` 入口检查 `execution_status == "stale_drop"` 跳过发送。 |
| 2 | `executor.py:511-516` | **SYSTEM_WAIT_SIGNAL 路径丢失 reply_mode 上下文**。`return None` 未标记 wait 状态。**修复**：return 前 `event.set_extra("astrmai_execution_signal", "wait")`。 |
| 3 | `executor.py:369-390` | **`_inject_direct_vision_context` 临时文件清理遗漏异常路径**。可能误删原始图片文件。**修复**：记录原始路径，finally 中只删除 `tempfile.mkstemp` 创建的文件。 |

---

## 🟡 中等（重点 5 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | `judge.py:315-320` | `_load_recent_history_records` 多个 loader 无短路，找到有效结果后 break |
| 5 | `chat_loop_kernel.py:1780-1790` | MAINTENANCE 阶段饥饿循环 — 连续 N 轮停留在 MAINTENANCE 且 budget blocked 时强制降级 IDLE |
| 6 | `chat_loop_kernel.py:1510-1530` | PROACTIVE_WAKEUP 与 HEARTFLOW_EVALUATE 互斥导致 heartflow 静默丢失 — 标记 pending_heartflow |
| 7 | `followup_manager.py:53-58` | 私聊 `wait_for_new_message` 阻塞收尾流程 — 将等待移到独立 task |
| 8 | `reply_artifact_builder.py:240-260` | `_merge_wait_targets` 去重逻辑不完整，emit 前合并现有 wait_targets |

---

## 🟢 建议（清理）

- `judge.py:100-110` `BrainActionPlan.should_act()` 死代码 — 删除
- `chat_loop_kernel.py:45-110` `SCHEDULER_POLICY_PROFILES` 三组配置重复 — 提取 base_profile

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_executor_refactor.py tests/test_chat_loop_kernel_refactor.py tests/test_judge_history_window_refactor.py -q
```

## 成功标准
- 🔴 3 项全部修复
- 🟡 #4 #5 #7 修复
- 相关测试无回归
