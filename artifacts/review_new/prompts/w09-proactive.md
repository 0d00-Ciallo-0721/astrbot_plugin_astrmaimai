# 开发窗口 09：Proactive — 内存泄漏 + 重试风暴 + 乱码修复

## 必须先读取的审查报告
1. `artifacts/review_new/r08-proactive.md` — 3🔴 9🟡 6🟢

## 审查范围
`astrmai/proactive/`（12 个源文件）

---

## 🔴 严重（3 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `heartflow/manager.py:53-57` | **心流管理器内存泄漏**。`_states`、`_pulses_by_chat`、`_impulse_decisions_by_chat`、`_action_decisions_by_chat` 四个字典按 chat_id 增长永不清除。**修复**：每次 tick 清理超过 2×ACTIVE_CHAT_TTL 的条目。 |
| 2 | `decay_service.py:38-44` | **Memory decay 异常重试风暴**。失败后 `_last_memory_decay` 未更新，每 ~60s 重试。**修复**：失败时也更新时间戳，或实现退避。 |
| 3 | `diary_service.py:7-8, 50-51` | **硬编码中文乱码**。`[浣犵殑鏍稿績浜鸿]`→`[你的核心人设]`、`[鍐呴儴鏃ヨ]`→`[内部日记]`。 |

---

## 🟡 中等（重点 5 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | `decay_service.py:26-33` | 社交分数衰减死区：±10 内永不归零 |
| 5 | `proactive_task.py:455-472` | Profiling 持有全局信号量阻塞其他服务，内部用子信号量 |
| 6 | `proactive_task.py:482-483` | 日记窗口仅 1 小时可能丢失执行，扩大到 2-3h 或补跑 |
| 7 | `rhythm.py:76-79` | 时区依赖未处理，增加启动时 TZ 验证日志 |
| 8 | `heartflow/manager.py:520-525` | `_compute_visible_candidate_score` 接受 now 参数后立即 `del now` |

---

## 🟢 建议（选做）

- 心流字典改用 `cachetools.TTLCache`
- memory decay 增加熔断退避
- topic digest 先 `should_run` 再 fire task

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/regression/proactive/ -q
```

## 成功标准
- 🔴 3 项全部修复
- 🟡 #4 #7 #8 修复
- 相关测试无回归
