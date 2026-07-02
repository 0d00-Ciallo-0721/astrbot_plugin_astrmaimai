# Design Document + Implementation Plan — AstrMai MEDIUM 修复

> Spec: `astrmai-medium-round7-20260630` | 9R → 9T

---

## Design & Tasks (合并)

### R1: `_derive_phase` 补 ACTIVE 映射

**设计**: 在 phase map 中加两行  
**文件**: `chat_loop_kernel.py:1881-1897`  
**改动**: +2 行 `PROACTIVE_WAKEUP: "ACTIVE"`, `HEARTFLOW_EVALUATE: "ACTIVE"`  
**任务**: T1 — 读 `_derive_phase`，在 phase_map 中补两个条目

### R2: CAS 失败时跳过而非应用 stale delta

**设计**: CAS 失败时不计算 delta，直接 `return`  
**文件**: `chat_state_service.py:306-310`  
**改动**: 将 `else: delta = ... clamp(current + delta)` 改为 `else: logger.debug(...); return`  
**任务**: T2 — 读 CAS 逻辑，改 else 分支

### R3: 消息到达不重置 fairness

**设计**: 删除非 heartbeat 分支中的 `consecutive_selected_count = 0`  
**文件**: `chat_loop_kernel.py:1866-1867`  
**改动**: 删除一行  
**任务**: T3 — 读 `_update_state`，删 `consecutive_selected_count = 0`

### R4: Token 阈值触发压缩

**设计**: `maybe_compact` 开头加 token 检查，复用 `token_estimator.estimate_tokens()`  
**文件**: `context_compaction.py:1237`  
**改动**: +8 行  
**任务**: T4 — 在 `now = monotonic()` 后加 token 检查

### R5: 修正 `_stability_analysis_v2` 传参

**设计**: 第四参数从 `safety_reason` 改为 `detect_safe_window` 返回的第二个元素  
**文件**: `context_compaction.py:1035-1047`  
**改动**: ±1 行  
**任务**: T5 — 读 `build_decision_snapshot`，修正第四参数

### R6: mark_accessed 不复活 stale

**设计**: 移除 `CASE WHEN status = 'stale' THEN 'active'`  
**文件**: `v2_store.py:1006`  
**改动**: -1 行  
**任务**: T6 — 读 `mark_accessed`，移除 stale→active 逻辑

### R7: import_module 惰性化

**设计**: `Comp` 改为 `_comp` 缓存，首次访问时加载  
**文件**: `plugin_helpers.py:10,79-81`  
**改动**: ±3 行  
**任务**: T7 — 重构为惰性加载

### R8: safe_create_task 纳入跟踪

**设计**: 接受可选 `track_set` 参数  
**文件**: `plugin_helpers.py:23-37`  
**改动**: +3 行  
**任务**: T8 — 加 `track_set` 参数

### R9: dedupe 从 sys 移出

**设计**: 模块级 `_debounce_cache` + `_debounce_lock`（asyncio.Lock）  
**文件**: `dedupe.py:22-24`  
**改动**: ±3 行  
**任务**: T9 — 替换全局状态

---

## Execution

T1–T9 全部可并行，9 文件，~+25/-10 行。
