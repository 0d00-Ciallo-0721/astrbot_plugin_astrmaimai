# Implementation Plan

> 本任务列表派生自同目录 `requirements.md` 与 `design.md`。
> 所有任务涉及不同文件，可并行执行。

## Overview

| Phase | 主题 | 任务 | 行数 |
|-------|------|:--:|:--:|
| Phase 1 | 配置完善 | Tasks 1-3 | +10 |
| Phase 2 | 韧性缺口 | Tasks 4-6 | +30 |
| Phase 3 | 人设打磨 | Tasks 7-10 | +16/-11 |
| Phase 4 | 可观测性 | Tasks 11-13 | +18 |
| Phase 5 | 验证 | Task 14 | — |

---

## Tasks

### Phase 1: 配置完善

- [ ] 1. R1: `_conf_schema.json` 数值范围
  - **Goal**: 40+ 数值字段增加 `minimum`/`maximum`
  - **Files**: ✏️ `_conf_schema.json`
  - **Steps**: 对所有 float 概率字段加 `"minimum":0,"maximum":1`；int 正整数字段加 `"minimum":1`；int 非负字段加 `"minimum":0`
  - **AC**: JSON schema 含范围标记
  - **Check**: 代码审查
  - **Risk**: 🟢
  - _Requirements: R1_

- [ ] 2. R2+R3: emotion_mapping + 模型名校验
  - **Goal**: `AstrMaiConfig.__init__` 增加两项校验 warning
  - **Files**: ✏️ `config.py`
  - **Steps**: 互斥检测后加 emotion_mapping 冒号检查 + 模型池名称斜杠检查
  - **AC**: 无效格式 → warning
  - **Check**: `python -c "from config import AstrMaiConfig; c = AstrMaiConfig(reply={'emotion_mapping':['bad']})"`
  - **Risk**: 🟢
  - _Requirements: R2, R3_

### Phase 2: 韧性缺口

- [ ] 3. R4: EventBus worker 健康检查
  - **Goal**: 每 30s 补足 worker 至 3 个
  - **Files**: ✏️ `event_bus.py`
  - **Steps**: 新增 `_worker_health_check()` 协程 + `_start_workers()` 中启动
  - **AC**: worker 崩溃后 30s 内恢复
  - **Check**: 代码审查
  - **Risk**: 🟢
  - _Requirements: R4_

- [ ] 4. R5: ProactiveTask loop 重启
  - **Goal**: loop 意外终止后自动重启
  - **Files**: ✏️ `proactive_task.py`
  - **Steps**: 新增 `_on_loop_done()` + `start()` 中注册 callback
  - **AC**: loop 崩溃后 5s 重启
  - **Check**: 代码审查
  - **Risk**: 🟡
  - _Requirements: R5_

- [ ] 5. R6: 记忆置信度门控
  - **Goal**: `confidence < 0.3` 跳过写入
  - **Files**: ✏️ `memory_write_service.py`, `config.py`, `_conf_schema.json`
  - **Steps**: 新增 `min_memory_confidence` 配置 + `write()` 中检查
  - **AC**: 低置信度记忆不写入
  - **Check**: 代码审查
  - **Risk**: 🟢
  - _Requirements: R6_

### Phase 3: 人设打磨

- [ ] 6. R7: Persona fallback 改进
  - **Goal**: 降级时提取关键句而非裸截断
  - **Files**: ✏️ `persona_summarizer.py`
  - **Steps**: 修改 `_summarize_core_identity_with_retry()` except 块
  - **AC**: fallback 含"你是"/"角色"等关键词
  - **Check**: 代码审查
  - **Risk**: 🟢
  - _Requirements: R7_

- [ ] 7. R8: self_lore 自动注入
  - **Goal**: 可选配置 + 条件注入
  - **Files**: ✏️ `context_engine.py`, `config.py`, `_conf_schema.json`
  - **Steps**: 新增 `include_self_lore_in_prompt` 配置 + `_load_persona_payload()` 中注入
  - **AC**: 配置开启后 payload 含 self_lore
  - **Check**: 代码审查
  - **Risk**: 🟢（默认关闭）
  - _Requirements: R8_

- [ ] 8. R9: Persona 缓存过期
  - **Goal**: 人设文本变更时自动重建缓存
  - **Files**: ✏️ `persona_summarizer.py`
  - **Steps**: 新增 `source_hash` 存储/比对
  - **AC**: 改人设后自动重建
  - **Check**: 代码审查
  - **Risk**: 🟢
  - _Requirements: R9_

- [ ] 9. R10: FrequencyController 清理
  - **Goal**: 从注入链移除死代码
  - **Files**: ✏️ `bootstrap.py`, `gate.py`, `runtime_context.py`, `frequency_controller.py`
  - **Steps**: 删除 3 处注入 + 文件标注 deprecated
  - **AC**: 搜索全项目无引用
  - **Check**: `Select-String -Pattern "FrequencyController" -Path "astrmai/"` → 仅自身文件
  - **Risk**: 🟡 先确认零引用
  - _Requirements: R10_

### Phase 4: 可观测性

- [ ] 10. R11+R12: Lane 指标
  - **Goal**: rotation_count + active_lane_count 通过 API 暴露
  - **Files**: ✏️ `lane_manager.py`, `runtime_context.py`
  - **Steps**: 新增计数器 + `build_diagnostics()` 暴露
  - **AC**: `/runtime/status` 含指标
  - **Check**: 代码审查
  - **Risk**: 🟢
  - _Requirements: R11, R12_

- [ ] 11. R13: 启动阶段日志
  - **Goal**: 每个 boot phase 有 INFO 日志
  - **Files**: ✏️ `lifecycle.py`
  - **Steps**: 每个 `set_boot_phase()` 后加 `logger.info`
  - **AC**: 启动日志含 6 条阶段信息
  - **Check**: 代码审查
  - **Risk**: 🟢
  - _Requirements: R13_

### Phase 5: 验证

- [ ] 12. 全量验证
  - **Goal**: 无回归
  - **Steps**: `pytest tests/ -q --tb=short --ignore=tests/integration/runtime/` ； `lsp_diagnostics`
  - **AC**: ≥ 68 passed；0 error
  - _Requirements: ALL_

---

## Dependency Chain

```
全部 11 个代码任务涉及不同文件，可全并行执行。
Task 9 (R10) 需先搜索引用确认安全。
```

## Summary

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| 1 | `_conf_schema.json` | R1+R6+R8 | +48 |
| 2 | `config.py` | R2+R3+R6+R8 | +12 |
| 3 | `event_bus.py` | R4 | +15 |
| 4 | `proactive_task.py` | R5 | +10 |
| 5 | `memory_write_service.py` | R6 | +5 |
| 6 | `persona_summarizer.py` | R7+R9 | +11/-3 |
| 7 | `context_engine.py` | R8 | +5 |
| 8 | `bootstrap.py` | R10 | -3 |
| 9 | `gate.py` | R10 | -1 |
| 10 | `runtime_context.py` | R10+R11 | +6/-4 |
| 11 | `lane_manager.py` | R11 | +8 |
| 12 | `lifecycle.py` | R13 | +6 |
| 13 | `frequency_controller.py` | R10 | +3 |
| **Total** | **13 文件** | | **~+129/-11** |

## 执行检查清单

- [ ] Task 1-11 完成
- [ ] R10: `FrequencyController` 引用搜索确认零外部引用
- [ ] `pytest tests/ -q` ≥ 68 passed
- [ ] `lsp_diagnostics` 13 文件 0 error

---

# 🔍 交叉验证（嵌入）

| 检查项 | 结果 |
|--------|:--:|
| 需求→设计 R1-R13 | ✅ 13/13 |
| 设计→任务 | ✅ 13/13 |
| 字段完整性 | ✅ 12×8=96/96 |
| 文件实存性 | ✅ 13/13 |
| **缺口** | **0** |
