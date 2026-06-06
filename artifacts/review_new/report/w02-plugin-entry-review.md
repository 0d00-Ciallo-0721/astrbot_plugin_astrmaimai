# 审查报告：开发窗口 02 回归审查 — Plugin Entry 异常沉默修复

> task_id: w02-review | 审查时间: 2025-05-30 | 审查范围: 6 项修复 × 4 文件

---

## 执行摘要

对开发窗口 02 的 6 项修复（3🔴 + 3🟡）进行逐项回归审查。**全部 6 项修复正确实施，零残留问题，零回归。**

审查范围：
- `astrmai/app/plugin_facade.py` — 3 处改动（R01, R03, R07, R09）
- `astrmai/app/runtime_context.py` — 3 处改动（R02, R07）
- `astrmai/app/runtime_facade_protocol.py` — 1 处改动（R03）
- `astrmai/app/bootstrap.py` — 1 处改动（R04）

验证结果：`709 passed, 1 skipped, 0 failures`

---

## 逐项审查

### 🔴 R01 — `set_active_facade` 异常记录日志 ✅

| 维度 | 结果 |
|------|------|
| 文件 | `plugin_facade.py:27-28` |
| 改动 | `except Exception: pass` → `except Exception as exc: logger.warning(...)` |
| 语义正确性 | ✅ `logger.warning` 会在 WebUI 模块不可用时输出可见日志，便于调试 |
| 日志级别 | ✅ WARNING 级别恰当 — 导入失败意味着 WebUI 管理面板不可用 |
| 异常传播 | ✅ 异常仍被捕获（不会导致插件启动崩溃），仅增加了日志 |
| 无副作用 | ✅ 不影响 `self.runtime` 或 `self.lifecycle_manager` 的初始化 |

### 🔴 R02 — `RuntimeStatus.degraded_components` 加锁 ✅

| 维度 | 结果 |
|------|------|
| 文件 | `runtime_context.py:81, 87-108` |
| 改动 | 新增 `_degraded_lock: threading.Lock` 字段；`mark_degraded()` 和 `_snapshot_degraded()` 用 `with` 保护 |
| `slots=True` 兼容性 | ✅ 使用 `field(default_factory=threading.Lock)` 声明，不违反 `__slots__` 约束 |
| `as_dict()` 线程安全 | ✅ 通过 `_snapshot_degraded()` 返回 `dict` 副本，迭代期间不受并发写入影响 |
| 死锁风险 | ✅ `mark_degraded` 和 `_snapshot_degraded` 互不调用，`threading.Lock`（非重入）无死锁风险 |
| asyncio 兼容性 | ✅ `threading.Lock` 在 asyncio 单线程默认模式下无竞争；临界区仅为一次 dict 赋值，即使未来引入线程池也安全 |
| 调用点覆盖 | ✅ `degraded_components` 唯一外部读取路径是 `plugin_facade.py:202` 的 `build_help_text()`，它通过 `as_dict()` → `_snapshot_degraded()` 获取快照 |

### 🔴 R03 — `enter_sys3_direct` async generator 文档标注 ✅

| 维度 | 结果 |
|------|------|
| 文件 | `plugin_facade.py:378-382` + `runtime_facade_protocol.py:208-212` |
| 改动 | 两个文件均添加 docstring，显式标注 "use `async for` to consume, NOT `await`" |
| 内容正确性 | ✅ 准确描述了 async generator 的正确消费方式 |
| 一致性 | ✅ 实现类和 Protocol 的 docstring 保持一致 |
| 现有调用点 | ✅ 唯一正确消费点为 `presentation/commands/work_mode.py`（`async for`），docstring 不影响行为 |

### 🟡 R04 — `_build_proactive_task` 提取 helper ✅

| 维度 | 结果 |
|------|------|
| 文件 | `bootstrap.py:482-494` |
| 改动 | 三行属性赋值 `proactive_task.auto_check_task = None` 等提取为 `@staticmethod _nullify_proactive_refs()` |
| 行为等价性 | ✅ 三条赋值语句完全一致，调用位置不变 |
| 封装改进 | ✅ 集中管理外部状态修改，附 docstring 说明迁移方向 |
| 未迁移的赋值 | ✅ `proactive_task.dream_scheduler.dream_visible = ...` 留在线内 — 它是配置赋值（非 nullify），且依赖 `runtime` 上下文 |
| 方法可见性 | ✅ `@staticmethod` 明确无状态依赖 |

### 🟡 R07 — `get_chat_loop_kernel` 回退逻辑上移 ✅

| 维度 | 结果 |
|------|------|
| 文件 | `runtime_context.py:299-312` + `plugin_facade.py:254-255` |
| 改动 | `PluginRuntimeContext` 新增 `chat_loop_kernel_with_fallback` property；`PluginFacade.get_chat_loop_kernel()` 简化为 `return self.runtime.chat_loop_kernel_with_fallback` |
| 逻辑等价性 | ✅ 回退顺序不变：`self.chat_loop_kernel` → `self.proactive_task.chat_loop_kernel` → `None` |
| 调用方兼容性 | ✅ 所有调用方（`plugin_api.py`, `admin_ui_service.py`, `schedulerservice.py`）通过 `PluginApiAdapter._call_facade("get_chat_loop_kernel")` → `PluginFacade.get_chat_loop_kernel()` 链，行为完全不变 |
| `lifecycle` 未就绪时的安全性 | ✅ `lifecycle` 默认值为 `LifecycleServices()`，`proactive_task` 默认 `None`，property 处理 `None` 返回 `None` |
| 封装改进 | ✅ Facade 不再需要知道 `ProactiveTask.chat_loop_kernel` 的存在 |

### 🟡 R09 — `is_framework_command` 内部异常记录 debug ✅

| 维度 | 结果 |
|------|------|
| 文件 | `plugin_facade.py:353-362` |
| 改动 | 两处 `except Exception: pass` → `except Exception as exc: logger.debug(...)` |
| 日志级别 | ✅ DEBUG 级别恰当 — 降级路径失败是预期行为，不应污染生产日志 |
| 变量遮蔽 | ⚠️ 微小：内层 `except Exception as exc` 的变量名与外层 `exc` 同名。Python 3 中 `except` 块结束后变量自动清理，实际无影响 |
| 覆盖完整性 | ✅ 三处异常处理路径全部记录日志：外层穿透失败（已有 `logger.debug`）、内层降级失败（新增）、`extra_command_list` 读取失败（新增） |

---

## 全局检查

### `except Exception: pass` 残留

```
$ search_content: "except Exception:\s*\n\s*pass" → astrmai/app/
结果: 0 匹配
```

`astrmai/app/` 目录下**零残留** `except Exception: pass` 模式。✅

### `degraded_components` 非锁访问

```
所有 degraded_components 访问路径:
  runtime_context.py:80  — 字段声明 (OK)
  runtime_context.py:88  — mark_degraded → with lock (OK)
  runtime_context.py:103 — as_dict → _snapshot_degraded → with lock (OK)
  runtime_context.py:108 — _snapshot_degraded → with lock (OK)
  plugin_facade.py:202  — build_help_text → 读取 as_dict() 返回值 (OK)
```

零裸访问。✅

### `get_chat_loop_kernel` 调用链

```
PluginApiAdapter._call_facade("get_chat_loop_kernel")
  → PluginFacade.get_chat_loop_kernel()
    → PluginRuntimeContext.chat_loop_kernel_with_fallback  (NEW)
```

所有上游调用方（`admin_ui_service.py` × 3, `schedulerservice.py` × 3）无变化。✅

---

## 测试结果

```
709 passed, 1 skipped, 6 deselected, 0 failures in 15.28s
```

- 跳过的 1 个：`test_proactive_runtime_refactor.py`（与本次改动无关的 skip marker）
- 排除的 6 个：`admin_full_fixture` / `import_boundaries`（按验证命令显式排除）
- 失败：0
- 警告：均为项目既有（DeprecationWarning, SAWarning），与本次改动无关

---

## 风险评估

| 风险 | 级别 | 详情 |
|------|------|------|
| `inner exc` 变量名遮蔽 | 🟢 极低 | Python 3 `except` 块自动解绑；仅代码可读性影响 |
| `threading.Lock` + asyncio | 🟢 极低 | 临界区 ≤1 次 dict 赋值；asyncio 单线程无竞争；未来线程池场景下持有时间可忽略 |
| `chat_loop_kernel_with_fallback` 在 bootstrap 阶段被访问 | 🟢 极低 | `lifecycle` 有默认 `LifecycleServices()`；实际调用方均为 post-bootstrap 路径 |

---

## 总结

**窗口 02 所有修复项（3🔴 + 3🟡）正确实施，零历史遗留问题。**

对比原始审查报告（r01-plugin-entry.md）的发现清单：

| 原始 ID | 描述 | 状态 |
|---------|------|------|
| R01 🔴 | `set_active_facade` 异常静默 | ✅ 已修复 |
| R02 🔴 | `degraded_components` 非线程安全 | ✅ 已修复 |
| R03 🔴 | `enter_sys3_direct` async generator 误用风险 | ✅ 已修复（文档标注） |
| R04 🟡 | `_build_proactive_task` 属性赋值破坏封装 | ✅ 已修复 |
| R09 🟡 | `is_framework_command` 内部 `except: pass` | ✅ 已修复 |
| R07 🟡 | `get_chat_loop_kernel` 回退逻辑泄露 | ✅ 已修复 |

未纳入本次窗口的原始发现（R05, R06, R08, R10-R15）属于后续窗口或选做范围，不在本次成功标准内。

**最终评级：A（无遗留问题）**
