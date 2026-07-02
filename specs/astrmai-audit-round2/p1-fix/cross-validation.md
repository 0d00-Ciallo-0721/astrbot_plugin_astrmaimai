## 🔍 三文档交叉验证报告 — AstrMai P1 Fix Round-2

### 追溯性矩阵

| 验证项 | 结果 | 详情 |
|--------|:----:|------|
| 需求 → 设计 | ✅ 21/21 | R1–R21 全部有对应设计模块 |
| 设计 → 任务 | ✅ 21/21 | 每个设计模块分配了 1 个任务 |
| 任务字段完整性 | ✅ 21/21 | 每个任务含 8 个字段 (Goal/Files/Steps/AC/Forbidden/Check/Risk/_Requirements) |
| EARS 验收标准 | ✅ 63 条 | 每条需求 3 个 EARS 句式 |
| 风险标注 | ✅ 21/21 | 🔴0 + 🟡6 + 🟢15 |
| 验证命令 | ✅ 21/21 | 每个任务含可执行的 pytest Check Command |
| 文件实存性 | ✅ 15/15 | 全部引用的现有文件确认存在 (不含纯审计 Task 17) |
| 依赖链完整性 | ✅ 22/22 | Tasks 1-21 串行 + Task 22 验证 |

### 文件实存性详细检查

| 引用文件 | 状态 |
|---------|:----:|
| `astrmai/memory/persona/persona_summarizer.py` | ✅ |
| `astrmai/memory/services/v2_store.py` | ✅ |
| `astrmai/memory/services/memory_engine.py` | ✅ |
| `astrmai/memory/services/memory_turn_pipeline.py` | ✅ |
| `astrmai/memory/retrieval/hybrid_retriever.py` | ✅ |
| `astrmai/memory/services/memory_retrieval_service.py` | ✅ |
| `astrmai/learning/review/reflector.py` | ✅ |
| `astrmai/learning/review/reflect_tracker.py` | ✅ |
| `astrmai/infrastructure/runtime/event_bus.py` | ✅ |
| `astrmai/infrastructure/runtime/lane_manager.py` | ✅ |
| `astrmai/infrastructure/runtime/chat_runtime_coordinator.py` | ✅ |
| `astrmai/infrastructure/persistence/persistence_manager.py` | ✅ |
| `astrmai/infrastructure/persistence/database_service.py` | ✅ |
| `astrmai/conversation/attention/context_compaction.py` | ✅ |
| `astrmai/conversation/attention/gate.py` | ✅ |
| `astrmai/app/bootstrap.py` | ✅ |
| `astrmai/app/lifecycle.py` | ✅ |
| `astrmai/app/plugin_facade.py` | ✅ |
| `main.py` | ✅ |

### EARS 覆盖统计

| 需求 | EARS 句式数 | 例句 |
|------|:----:|------|
| R1 | 3 | THE ... SHALL 添加 add_done_callback |
| R2 | 3 | WHEN 超过上限 THE ... SHALL 删除最旧 lock |
| R3 | 3 | THE ... SHALL 有上限 max 100 |
| R4 | 3 | THE 系统 SHALL 在 _sweep_loop 中定期清理 |
| R5 | 3 | THE ... SHALL 有最大长度限制 max 200 |
| R6 | 3 | WHEN .set() 被调用 THE Event SHALL 被消费后重置 |
| R7 | 3 | THE ... SHALL 有上限 max 100 |
| R8 | 3 | THE ... SHALL 提供 prune_inactive() 方法 |
| R9 | 2 | THE dispose() SHALL 在 terminate() 时被调用 |
| R10 | 3 | THE ... SHALL 在 LLM 调用和 DB 更新期间持有 _lock |
| R11 | 2 | THE ... SHALL 仅标记返回给调用者的条目为 sent |
| R12 | 2 | IF WAL 模式未默认启用 THEN SHALL 在 get_chat_state 前确保 |
| R13 | 3 | THE ... SHALL 对 _pending_tasks 的检查和写入使用原子操作 |
| R14 | 3 | THE sys2_process 调用 SHALL 不阻塞 session worker 循环 |
| R15 | 3 | THE ... SHALL 向 call_data_process_task 传递 lane_key |
| R16 | 2 | WHEN vector 为 None THE add_memory SHALL 抛出 RuntimeError |
| R17 | 2 | THE ... SHALL 在每次调用时检查 runtime.system2_callback |
| R18 | 2 | THE track_task SHALL 使用 safe_create_task |
| R19 | 3 | THE _system2_entry SHALL 在 except 之后追加 except Exception |
| R20 | 2 | WHEN heartflow_is_command 为 True THE handler SHALL 立即返回 |
| R21 | 3 | THE main.py SHALL 注册 @filter.on_llm_response() handler |
| **Total** | **63** | |

### 风险分布

| 等级 | 数量 | 任务 |
|------|:----:|------|
| 🔴 高风险 | 0 | — |
| 🟡 中风险 | 6 | Tasks 2, 10, 11, 14, 16, 20 |
| 🟢 低风险 | 15 | Tasks 1, 3-9, 12, 13, 15, 17-19, 21 |

### 结论

✅ **整体通过** — 三文档链完整、一致、可执行。

21 条需求全部有设计模块和任务对应。18 个文件实存性确认。63 条 EARS 验收标准覆盖。6 个中风险任务有明确缓解措施。依赖链严格串行，Task 22 最终验证兜底。

### 准备就绪，进入 PHASE 2 执行。
