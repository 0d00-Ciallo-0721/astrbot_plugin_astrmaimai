# 开发窗口 06：Attention — 竞态条件 + 背压机制

## 必须先读取的审查报告
1. `artifacts/review_new/r05-attention.md` — 4🔴 12🟡 7🟢

## 审查范围
`astrmai/conversation/attention/`（12 个源文件）

---

## 🔴 严重（4 项）— 最高优先级

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `gate.py:423-441, 458-470` | **竞态条件：`process_event` 与 `_debounce_and_judge` 之间事件丢失窗口**。高并发下事件永久丢失。**修复**：重构锁持有范围或使用队列模式。 |
| 2 | `gate.py:288-292` | **无限制创建异步任务（缺乏背压）**。`_fire_background_task` 无 Semaphore 限流，大消息量 OOM。**修复**：添加 `asyncio.Semaphore` 限流。 |
| 3 | `context_compaction.py`(整体) | **文件 1720 行，单类 1600+ 行**。`ContextCompactionEngine` 职责过多。标记为技术债，本轮暂不拆分。 |
| 4 | `gate.py:67-69` | **`ATTENTION_WINDOW_TTL_SECONDS = 30.0` 过短**。建议提升至 120-300s。 |

---

## 🟡 中等（重点 6 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 5 | `context_compaction.py:520-530` | 失败冷却 10s < 成功冷却 20s — 反直觉，失败后应更长 |
| 6 | `context_compaction.py:340-375` | `detect_safe_window` 与 `_safety_analysis` 逻辑重叠，合并 |
| 7 | `thread_builder.py:130-190` | `build_focus_thread` O(n²×k) 排序 — 预建映射字典 |
| 8 | `window_buffer.py:46-47` | `merge` 先 prune 再合并，使用过时数据 |
| 9 | `gate.py:275-285` | `_should_skip_by_throttle` 对 `chat_state.should_drop` 访问无 None 安全 |
| 10 | `gate.py:245-260` | `_handle_repeater_echo` 接受 event 参数后 `_ = event` — 清理签名 |

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_attention_gate_refactor.py -q
```

## 成功标准
- 🔴 #1 #2 #4 修复（#3 标记技术债）
- 🟡 #7 #9 #10 修复
- 相关测试无回归
