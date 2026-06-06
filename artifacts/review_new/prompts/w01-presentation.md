# 开发窗口 01：Presentation 输入校验 + 防御性加固

## 必须先读取的审查报告
1. `artifacts/review_new/r10-presentation.md` — 0🔴 4🟡 5🟢

## 审查范围
`astrmai/presentation/`（11 个源文件），本模块是质量最佳的模块（0 严重），本轮做精细打磨。

---

## 🟡 中等问题（4 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `commands/review_commands.py:15-34` | **`submit_review` 未校验 `decision` 字段**。`decision` 仅透传，未做 `"approve"/"reject"/"skip"` 白名单校验。传非法值导致下游不确定行为。 |
| 2 | `commands/review_commands.py:11` | **`list_pending_reviews` 的 `limit` 无范围校验**。可传入负数/零/极大值。**修复**：`limit = max(1, min(200, int(limit)))`。 |
| 3 | `dto/command_models.py:15-16` | **`WorkCommandRequest.from_message` 前缀匹配过宽**。`raw.startswith("/work")` 会误匹配 `/working`、`/workflow`。**修复**：改为 `raw == "/work" or raw.startswith("/work ")`。 |
| 4 | `commands/review_commands.py:28-32` | **字段截断至 1000 字符无日志**。用户不知道内容被截断。**修复**：截断时 `logger.warning` 记录原长度。 |

---

## 🟢 建议项（5 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 5 | `events/message_entry.py:30,34` | `check_framework_command` / `check_message_scope_access` 未包裹 try/except，建议加防御 |
| 6 | `events/message_entry.py:47` | `facade.track_incoming_user_activity` 未包裹 try/except |
| 7 | `events/message_entry.py:55` | `is_direct_call_event(event)` 未包裹 try/except |
| 8 | `dto/command_models.py:18` | `is_empty` 属性名歧义，建议改为 `has_query` |
| 9 | `events/startup_hooks.py:9` | `on_program_start` 无错误处理 |

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_attention_gate_refactor.py tests/test_reply_service_refactor.py tests/test_legacy_compat_refactor.py -q
```

## 成功标准
- 🟡 4 项全部修复
- 🟢 5 项中至少修复 #5 #6 #7 #9（防御性包裹）
- 相关测试无回归
