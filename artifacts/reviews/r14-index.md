# AstrMai 全量代码审查索引 (R12)

> 生成：本轮会话终态  
> 审查方式：12 个独立 subagent 并行审查  
> 测试基线：692 passed / 12 failed / 1 skipped

---

## 审查报告清单

| # | 报告文件 | 模块 | 文件数 | 🔴 | 🟡 | 🟢 | 总计 |
|---|---------|------|--------|----|----|----|------|
| 1 | `r12-gateway-runtime.md` | Gateway + Runtime | 21 | 2 | 8 | 5 | **15** |
| 2 | `r12-conversation.md` | Conversation/Planning/Execution | 12 | 3 | 10 | 7 | **20** |
| 3 | `r12-attention.md` | Attention | 11 | 2 | 7 | 3 | **12** |
| 4 | `r12-memory.md` | Memory | 12 | 3 | 5 | 5 | **13** |
| 5 | `r12-state.md` | State + Decay | 16 | 3 | 8 | 5 | **16** |
| 6 | `r12-proactive.md` | Proactive | 12 | 4 | 8 | 6 | **18** |
| 7 | `r12-webui.md` | WebUI | 23 | 5 | 7 | 5 | **17** |
| 8 | `r12-presentation.md` | Presentation/Events | 14 | 1 | 3 | 6 | **10** |
| 9 | `r12-plugin-entry.md` | Plugin Entry/Bootstrap | 7 | 3 | 11 | 8 | **22** |
| 10 | `r12-infrastructure.md` | Compat + Persistence | 14 | 3 | 4 | 5 | **12** |
| 11 | `r12-security.md` | 安全专项（全量） | ~120 | 3 | 4 | 6+3 | **16** |
| 12 | — | 测试质量（未完成） | — | — | — | — | — |
| | | **合计** | **~262** | **32** | **75** | **66+** | **171** |

---

## 🔴 严重发现汇总（32 项）

### Gateway/Runtime（2 项）
1. `gateway_lane.py:217-262` — chat_in_lane_result 双重冷却过滤导致 trace 与执行不一致
2. 见报告详情

### Conversation（3 项）
- 见 `r12-conversation.md`

### Attention（2 项）
1. `context_compaction.py:1654` — `@staticmethod` 方法使用 `self`，潜伏崩溃
2. 见报告详情

### Memory（3 项）
1. `contracts/memory_query.py:28-29` — `include_feedback`/`retrieve_keys` 死字段
2-3. 见报告详情

### State（3 项）
1. `mood_decay.py:11` — `config.energy` 无空安全访问
2-3. 见报告详情

### Proactive（4 项）
1. `rhythm.py:72-76` — `getattr(None, …)` 崩溃风险
2-4. 见报告详情

### WebUI（5 项）
1. `admin_ui_service.py:26-37` — God Object 反模式（1148 行，7 域委托回自身）
2-5. 见报告详情

### Presentation（1 项）
1. `events/message_entry.py:40` — 自消息检查滞后于防抖/权限

### Plugin Entry（3 项）
1. `lifecycle.py:211-216` — `_reset_runtime_status_flags` 未复位 3 个启动期标志
2-3. 见报告详情

### Infrastructure（3 项）
1. `orm_models.py:60-82` — ChatState 持久化严重不完整（12+ 字段仅 7 个入库）
2-3. 见报告详情

### Security（3 项 CRITICAL）
- 见 `r12-security.md`

---

## 已修复项（本轮已完成，不再出现在审查报告中）

| 来源 | 项 | 状态 |
|------|-----|------|
| 窗口 4 | D1-D5, D14-D17 | ✅ 已修复 |
| Phase 1 | D18, D20-D22, D41, D43 | ✅ 已修复 |
| Phase 2 | D6, D7 | ✅ 已修复 |
| Phase 3 | D24-D26 | ✅ 文档化/分段注释 |
| 窗口 1 | D58, D59 | ✅ 已修复 |
| 测试 | T1-T3, T6-T8 | ✅ 已修复（+6 通过） |

---

## 与上次审查 (R11) 对比

| 指标 | R11 | R12 | 变化 |
|------|-----|-----|------|
| 审查文件 | ~40 | ~262 | +555% |
| 发现总数 | 42 | 171 | +307% |
| 🔴 严重 | 12 | 32 | +167% |
