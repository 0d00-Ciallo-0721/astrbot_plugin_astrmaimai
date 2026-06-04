# 开发窗口 10：Plugin Entry/Bootstrap/Facade 修复

## 必须先读取的审查报告
1. `artifacts/reviews/r01-plugin-entry.md` — 完整发现清单（3🔴 11🟡 8🟢）
2. `artifacts/reviews/r15-master.md` — 总报告
3. `artifacts/reviews/r13-session-fixes.md` — 了解本轮已修复（D24/D25/D26分段注释 + D58/D59）

## 目标文件
- `astrmai/app/bootstrap.py` — Bootstrap（~405 行）
- `astrmai/app/lifecycle.py` — 生命周期管理（~251 行）
- `astrmai/app/plugin_facade.py` — PluginFacade
- `astrmai/app/runtime_facade_protocol.py` — Facade 协议
- `astrmai/app/runtime_context.py` — 运行时上下文
- `main.py` — 插件入口（139 行）

## 依赖
所有模块（入口层是最顶层）

---

## 🔴 严重（3 项）

### P10-1：_reset_runtime_status_flags 未复位所有启动期标志
- **文件**：`astrmai/app/lifecycle.py:211-216`
- **问题**：`bootstrap_completed`、`boot_logged`、`work_mode_enabled` 三个 `RuntimeStatus` 字段在 shutdown 时未被重置。`is_running` 和 `lifecycle_started` 的复位在 `terminate()` 开头直接赋值，其余标志在 `_reset_runtime_status_flags` 内复位——两种方式散落两处。
- **后果**：热重启时残余 `bootstrap_completed=True` 导致诊断显示"已完成"但实际未重新引导。
- **最小修复**：
  1. 在 `_reset_runtime_status_flags()` 中添加 `bootstrap_completed`/`boot_logged`/`work_mode_enabled` 的复位
  2. 将 `is_running`/`lifecycle_started` 的复位也移入 `_reset_runtime_status_flags()`，统一管理

### P10-2：详见 r01-plugin-entry.md
### P10-3：详见 r01-plugin-entry.md

---

## 🟡 中等（11 项）

详见 `r01-plugin-entry.md`，重点：
- **D27**：`_get_runtime()` 仍被 10+ 窄域 accessor 调用（`plugin_api.py:82-115`），底层全量暴露 runtime — 独立窗口
- **D24/D25**：`_build_core_services` (~70行) 和 `_build_lifecycle_stack` (~80行) 方法过长（本轮已加 8 段注释）
- **D26**：MemoryEngine ↔ DatabaseService 循环引用通过 back-link（本轮已文档化）
- **D28**：`PluginRuntimeContext` property 膨胀至 30+（已通过 5 个分组 dataclass 遏制）
- `PluginFacade` 26 个 public 方法与 `RuntimeFacadeProtocol` 一致性（ARCHITECTURE-BOUNDARIES 确认 26/26 对齐）
- `lifecycle.py` 的 `terminate()` 缺少 try/finally（D13 已修复）
- `ACTIVE_FACADE` 仍是模块级全局变量而非 ContextVar（D56 部分修复）

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/ -q --tb=line
```

## 成功标准
- 🔴 P10-1：所有启动期标志在 `_reset_runtime_status_flags` 中统一复位
- 🔴 3 项修复
- 全量测试无新增回归（692+ passed）
