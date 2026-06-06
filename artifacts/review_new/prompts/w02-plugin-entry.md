# 开发窗口 02：Plugin Entry 异常沉默修复 + 测试补齐

## 必须先读取的审查报告
1. `artifacts/review_new/r01-plugin-entry.md` — 3🔴 6🟡 6🟢

## 审查范围
`astrmai/app/`（5 个源文件）：bootstrap、lifecycle、plugin_facade、runtime_context

---

## 🔴 严重（3 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `plugin_facade.py:23-27` | **`set_active_facade` 导入/调用异常被静默吞没**。`except Exception: pass` 不记录日志。**修复**：至少 `logger.warning` 记录异常。 |
| 2 | `runtime_context.py:78-87` | **`RuntimeStatus.degraded_components` 非线程安全**。`mark_degraded()` 在多协程下修改 dict。**修复**：加 `threading.Lock` 或文档标注。 |
| 3 | `plugin_facade.py:27` | **`enter_sys3_direct` async generator 误用风险**。调用方若用 `await` 而非 `async for` 会静默失败。**修复**：文档字符串显式标注。 |

---

## 🟡 中等（6 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | `bootstrap.py:190-193` | `_build_proactive_task` 通过属性赋值修改外部对象内部状态，破坏封装 |
| 5 | `bootstrap.py:88-89` | `_build_core_services` 方法过长（~50 行），提取 `_wire_core_cross_references` |
| 6 | `lifecycle.py:176-196` | `_terminate_impl` 后台任务取消硬编码 `timeout=3.0` |
| 7 | `plugin_facade.py:172-183` | `get_chat_loop_kernel` 回退逻辑泄露内部实现细节 |
| 8 | `plugin_facade.py:299-307` | `is_framework_command` 内部 `except Exception: pass` 不记录日志 |

---

## 🟢 建议（选做）

- `runtime_context.py` 所有 `XxxServices` dataclass 字段类型 `Any` → 逐步引入 Protocol
- `lifecycle.py` 数据库同步间隔硬编码 15s → 提取为可配置参数

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/ -q --tb=line -k "not (admin_full_fixture or import_boundaries)"
```

## 成功标准
- 🔴 3 项全部修复
- 🟡 #4 #7 #8 修复
- 无新增测试失败
