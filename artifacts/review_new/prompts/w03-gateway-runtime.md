# 开发窗口 03：Gateway / Runtime 并发安全 + 代码去重

## 必须先读取的审查报告
1. `artifacts/review_new/r02-gateway-runtime.md` — 4🔴 10🟡 10🟢

## 审查范围
`astrmai/infrastructure/gateway/` + `astrmai/infrastructure/runtime/`（27 个源文件）

---

## 🔴 严重（4 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `event_bus.py:78-80` | **`trigger_knowledge_update()` set() 后立即 clear() 竞争窗口**。替换为 `publish` 机制。 |
| 2 | `gateway_lane.py:440-635` | **`tool_chat_in_lane_result` 缺少单模型重试**。瞬时故障直接切换模型而非重试。补充至少 1 次重试。 |
| 3 | `lane_storage.py:27-92` | **`ensure_lane()` 持锁期间执行异步 I/O**。将 I/O 移出锁范围。 |
| 4 | `gateway_call.py:99-106` | **`_record_benchmark_sample` 吞掉所有异常仅 debug 日志**。改为 WARNING 级别。 |

---

## 🟡 中等（重点 7 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 5 | `chat_runtime_coordinator.py:72-86` | `try_acquire_executor` 返回未获取的锁，命名与语义不一致 |
| 6 | `gateway_lane.py:105-635` | `chat_in_lane_result` 与 `tool_chat_in_lane_result` 成功路径大量重复，提取 `_finalize_success_result` |
| 7 | `gateway_call.py:72-90` | JSON 路径与非 JSON 路径重复，提取统一方法 |
| 8 | `model_router.py:92-96` | `report_success` 在冷却期提前解除隔离，并发场景风险 |
| 9 | `raw_trace_store.py:48-52` + `turn_trace_store.py:45-49` | 文件写入非原子，改用临时文件 + `os.replace()` |
| 10 | `lane_history.py:29-35` | bot_speaker_names 两处实现使用不同数据源 |

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/ -q --tb=line -k "not (admin_full_fixture or import_boundaries)"
```

## 成功标准
- 🔴 4 项全部修复
- 🟡 #5 #8 #9 修复
- 代码去重（#6 #7）至少完成一项
