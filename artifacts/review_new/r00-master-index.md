# AstrMai 全量代码审查总报告（修复后 v2）

> 审查轮次：修复后全量审查  
> 审查方式：12 个独立 subagent 并行审查  
> 测试基线：713 passed / 2 failed / 1 skipped  
> 上一轮基线：692 passed / 12 failed / 1 skipped  
> 审查源文件数：~260+

---

## 与上一轮对比

| 指标 | 上一轮 (r01-r15) | 本轮 (review_new) | 变化 |
|------|:---:|:---:|:---:|
| 测试通过 | 692 | 713 | +21 ✅ |
| 测试失败 | 12 | 2 | -10 ✅ |
| 🔴 严重 | 32 | 34 | +2 |
| 🟡 中等 | 75 | 97 | +22 |
| 🟢 建议 | 64 | 87 | +23 |
| **总计** | **171** | **218** | **+47** |

> 注：发现数量增加主要因为本轮审查更深入（每个 subagent 有更详细的审查指令，覆盖了更多边界条件），而非代码质量下降。12 个失败测试减少到 2 个，说明修复有效。

---

## 报告索引

| # | 文件 | 模块 | 🔴 | 🟡 | 🟢 | 总计 |
|---|------|------|:--:|:--:|:--:|:---:|
| r01 | `r01-plugin-entry.md` | Plugin Entry / Bootstrap | 3 | 6 | 6 | **15** |
| r02 | `r02-gateway-runtime.md` | Gateway / Runtime | 4 | 10 | 10 | **24** |
| r03 | `r03-conversation-planning.md` | Conversation / Planning | 1 | 6 | 7 | **14** |
| r04 | `r04-conversation-execution.md` | Conversation / Execution | 3 | 7 | 6 | **16** |
| r05 | `r05-attention.md` | Attention | 4 | 12 | 7 | **23** |
| r06 | `r06-memory.md` | Memory | 3 | 8 | 7 | **18** |
| r07 | `r07-state.md` | State / Decay / Energy | 4 | 7 | 6 | **17** |
| r08 | `r08-proactive.md` | Proactive | 3 | 9 | 6 | **18** |
| r09 | `r09-webui.md` | WebUI | 3 | 12 | 14 | **29** |
| r10 | `r10-presentation.md` | Presentation / Events | 0 | 4 | 5 | **9** |
| r11 | `r11-infrastructure.md` | Infrastructure (Compat / Persistence) | 2 | 7 | 8 | **17** |
| r12 | `r12-security.md` | 安全专项（全量） | 4 | 9 | 5 | **18** |
| **合计** | | | **34** | **97** | **87** | **218** |

---

## 🔴 严重发现速览（34 项）

### Plugin Entry (3)
- 详见 `r01-plugin-entry.md`

### Gateway / Runtime (4)
- 详见 `r02-gateway-runtime.md`

### Conversation Planning (1)
- PromptRefiner 乱码注入（`_render_runtime_guidance_cluster` 三个 section 标题为 mojibake）

### Conversation Execution (3)
- 详见 `r04-conversation-execution.md`

### Attention (4)
- 详见 `r05-attention.md`

### Memory (3)
- 详见 `r06-memory.md`

### State (4)
- 详见 `r07-state.md`

### Proactive (3)
- HeartflowManager 内存泄漏（`_states`/`_pulses` 字典随 chat_id 线性增长）
- DecayService 异常重试风暴（memory decay 失败后每秒重试）
- diary_service.py 中文乱码

### WebUI (3)
- 详见 `r09-webui.md`

### Presentation (0)
- 无严重发现 ✅

### Infrastructure (2)
- `_CHAT_STATES_COLUMNS` 全局缓存不感知运行时 DDL 变更
- `_read_freshness_budget` 事件系统 JSON 序列化后丢失类型信息

### Security (4)
- 详见 `r12-security.md`

---

## 修复窗口回顾：上一轮修复完成情况

### w01 — Presentation/Events ✅
- 1🔴 + 2🟡 = 3 项修复（message_entry 异常保护、review_commands 校验）
- 遗留 3🟡（is_direct_call_event 位置、handle_group_reply_wait 未包裹、submit_review 缺 pattern_id 校验）

### w02 — Infrastructure/Persistence ✅
- ChatState 6 字段持久化（DDL + load + save + get 四路对齐）
- legacy freshness_budget 修复
- lastmessagemetadatadb 显式建表
- 遗留 2🟡（judgment_mode 类型、list() 冗余拷贝）

### w03 — Conversation ✅
- JSON 解析贪心正则→括号计数（含字符串字面量处理）
- gate_decision 去重
- prompt 缓存复用
- COMPLEXITY_HINTS 问号处理
- 遗留 12 项推迟

### w04 — Memory (Part 1) ✅
- MemoryQuery 死字段 DeprecationWarning
- get_cognitive_feedback 数据源迁移（documents → canonical_memories）
- search_memories 提升为公共方法
- dedup_key SHA1 哈希缩减
- 死代码删除

### w05 — Memory (Part 2) ✅
- v2_store 独立数据库（memory_v2.db）+ 自动迁移
- recall exclude_kinds 前置过滤 + 安全网
- 17 处日志 debug→warning
- 列表拼接优化、layers 参数、docstring

### w06 — State ✅
- config.energy/config.mood 空安全
- CAS 比较点修正（衰减后 vs 衰减后）
- DecayService 使用服务层接口
- should_drop_by_energy docstring
- last_passive_decay_time 双哨兵

---

## 剩余 2 个测试失败

| # | 测试 | 根因 |
|---|------|------|
| 1 | `test_admin_full_fixture_supports_backend_service_views` | `no such table: canonical_memories` — v2_store 独立数据库迁移后某处仍引用旧路径 |
| 2 | `test_project_files_do_not_embed_local_absolute_paths` | `.agent/compact-report.md` 包含本地绝对路径 |

---

## 各模块质量评级总览

| 模块 | 评级 | 关键风险 |
|------|:--:|------|
| Plugin Entry | — | 见 r01 |
| Gateway / Runtime | — | 见 r02 |
| Conversation Planning | B- | PromptRefiner 乱码 |
| Conversation Execution | — | 见 r04 |
| Attention | — | 见 r05 |
| Memory | — | 见 r06 |
| State | — | 见 r07 |
| Proactive | ⭐⭐⭐⭐ | 内存泄漏、重试风暴 |
| WebUI | — | 见 r09 |
| Presentation | ✅ | 0🔴，质量最佳 |
| Infrastructure | B- | 缓存不感知 DDL |
| Security | — | 见 r12 |

---

*审查时间：2026-06-06*  
*审查引擎：Reasonix Code (deepseek-v4-pro) × 12 parallel subagents*
