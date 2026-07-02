# Design Document — AstrMai P1 Fix Round-2

> 本文档对应 Spec `astrmai-audit-round2/p1-fix`，描述 21 项 P1 缺陷的修复设计方案。  
> 不包含 P0/P2/P3 修复、架构重构、新功能开发。  
> 凡涉及锁语义或 LLM 调用链的改动，优先最小化影响范围。

## 1. Overview

### 1.1 整体策略

按「Wave 1 生命周期 → Wave 2 数据一致性 → Wave 3 错误处理 → Wave 4 事件流」顺序修复。每个 Wave 内部按文件分组串行执行，Wave 之间无交叉依赖。

| 阶段 | 主要动作 | 改动文件 | 改动类型 |
|------|---------|---------|---------|
| ① Wave 1 | 添加 done_callback、dict 上限限制、清除 Event、调用 dispose | persona_summarizer.py, v2_store.py, memory_engine.py, memory_turn_pipeline.py, reflector.py, event_bus.py, lane_manager.py, chat_runtime_coordinator.py, plugin_facade.py | 补丁 (patch) |
| ② Wave 2 | 修复 TOCTOU、标记范围修正、WAL 启用、任务创建锁、session worker 异步化 | reflect_tracker.py, database_service.py, context_compaction.py, gate.py | 补丁 (patch) |
| ③ Wave 3 | 添加 lane_key、raise 替代 return None、闭包检查、safe_create_task、异常兜底 | memory_retrieval_service.py, hybrid_retriever.py, bootstrap.py, lifecycle.py, plugin_facade.py | 补丁 (patch) |
| ④ Wave 4 | 实现 heartflow_is_command 检查、注册 on_llm_response hook | main.py | 新增 (add) |

### 1.2 设计边界（重申）

- 不创建新文件
- 不修改 `_conf_schema.json`
- 不修改任何 `__init__.py` 或包导出
- 不修改 `metadata.yaml`
- 不修改 AstrBot 框架 API 调用签名（保持向后兼容）

### 1.3 与 AstrBot 框架的接口预留

| 预留点 | 位置 | 用途 |
|--------|------|------|
| `@filter.on_llm_response()` | main.py:80 (new) | LLM 响应后监控钩子 |
| `event.get_extra("heartflow_is_command")` | main.py:136 (new) | HeartCore 命令标记检查 |
| `safe_create_task()` | lifecycle.py:23 | 已有导入，用于 RuntimeError guard |

## 2. Architecture

### 2.1 系统总体形态

```
main.py (入口/Hooks)
  └── plugin_facade.py (Facade)
        ├── lifecycle.py (track_task)
        ├── bootstrap.py (bridge)
        ├── gate.py (session worker)
        ├── reflector.py / reflect_tracker.py (learning)
        ├── memory_engine.py / v2_store.py / memory_turn_pipeline.py (memory)
        ├── persona_summarizer.py (persona)
        ├── hybrid_retriever.py / memory_retrieval_service.py (retrieval)
        ├── lane_manager.py / event_bus.py / chat_runtime_coordinator.py (infra)
        ├── database_service.py / persistence_manager.py (persistence)
        └── context_compaction.py (conversation)
```

### 2.2 模块依赖图（现状不变）

```
main.py → plugin_facade.py → [all subsystems]
                              ├── lifecycle.py
                              ├── bootstrap.py → plugin_facade (闭包)
                              ├── gate.py → plugin_facade.sys2_process
                              ├── learning/ (reflector, reflect_tracker)
                              ├── memory/ (engine, v2_store, turn_pipeline, persona)
                              ├── infra/ (lane_manager, event_bus, chat_runtime)
                              ├── persistence/ (database_service, persistence_manager)
                              └── conversation/ (context_compaction)
```

### 2.3 关键不变量（本 Spec 阶段冻结）

| 不变量 | 来源 | 冻结理由 |
|--------|------|---------|
| `asyncio.Lock` 用于所有 mutable dict 保护 | v2_store.py:62, lane_manager.py:结构 | 不改锁类型 |
| `fire-and-forget` 模式保持已有 `_background_tasks` 追踪 | event_bus.py:155 | 不改派发模式 |
| LLM 调用统一通过 `gateway.call_data_process_task()` | memory_retrieval_service.py:383 | 不改调用路径 |
| Handler 均为 `async def` 或 async generator | main.py:135 | 不改 handler 类型 |

## 3. Wave 1 — 生命周期/资源泄漏修复 (R1–R9)

### 3.1 R1: persona_summarizer.py — create_task 无 done_callback

**涉及文件**: `astrmai/memory/persona/persona_summarizer.py:196,271`

#### 3.1.1 当前状态
```python
# line 195: task = asyncio.create_task(self._generate_all_shards_background(raw_text, cache_key))
# line 196: self.pending_tasks[cache_key] = task  # 无 add_done_callback

# line 271: task = asyncio.create_task(self._generate_all_shards_background(original_prompt, cache_key))
# line 272: self.pending_tasks[cache_key] = task  # 无 add_done_callback
```

#### 3.1.2 设计决策
添加 `_handle_background_task_result` 方法（参照 `lifecycle.py:28-34`），两处 `create_task` 后加 `.add_done_callback()`。

#### 3.1.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| persona_summarizer.py | +6 行方法 + 2 行回调 | +8 |

#### 3.1.4 禁止改动
- **不**修改 `_generate_all_shards_background` 内部逻辑

---

### 3.2 R2: v2_store.py — _session_locks 无界增长

**涉及文件**: `astrmai/memory/services/v2_store.py:60-61,68-75`

#### 3.2.1 当前状态
```python
self._session_locks: dict[str, asyncio.Lock] = {}
# _get_session_lock 中无限添加新 lock
```

#### 3.2.2 设计决策
在 `_get_session_lock` 的 guard 块中添加：当 `len(self._session_locks) > 200` 时，`pop(next(iter(self._session_locks)))`。将 dict 改为 `OrderedDict` 支持 LRU。

#### 3.2.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| v2_store.py | dict→OrderedDict +3行 | +4/−1 |

---

### 3.3 R3: memory_engine.py — _cognitive_feedback_cache 无界增长

**涉及文件**: `astrmai/memory/services/memory_engine.py:87`

#### 3.3.1 当前状态
```python
self._cognitive_feedback_cache: dict[str, list[CognitiveFeedbackSignal]] = {}
```

#### 3.3.2 设计决策
在每次写入后检查 `len() > 100`，超限时删除最旧 key。

#### 3.3.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| memory_engine.py | +3 行检查 | +3 |

---

### 3.4 R4: memory_turn_pipeline.py — 4 dict 无界增长

**涉及文件**: `astrmai/memory/services/memory_turn_pipeline.py:38-44,52`

#### 3.4.1 当前状态
已有 `_sweep_loop` 但未实现定期清理。

#### 3.4.2 设计决策
在 `_sweep_loop` 中添加每 60 秒清理 30 分钟无活动 chat 的逻辑。

#### 3.4.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| memory_turn_pipeline.py | +10 行清理 | +10 |

---

### 3.5 R5: reflector.py — _pending_reflections 无界增长

**涉及文件**: `astrmai/learning/review/reflector.py:33,60-63`

#### 3.5.1 当前状态
```python
self._pending_reflections: List[Dict] = []
# record_usage 无限 append
```

#### 3.5.2 设计决策
`record_usage` 末尾添加长度检查，超 200 截断。

#### 3.5.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| reflector.py | +3 行 | +3 |

---

### 3.6 R6: event_bus.py — affection_changed Event 从未清除

**涉及文件**: `astrmai/infrastructure/runtime/event_bus.py:57-66`

#### 3.6.1 当前状态
```python
async def trigger_affection_change(self):
    self.affection_changed.set()
    await self.publish(self.TOPIC_AFFECTION_CHANGED)
    # ← 无 .clear()
```

#### 3.6.2 设计决策
`publish` 之后添加 `self.affection_changed.clear()`。

#### 3.6.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| event_bus.py | +1 行 | +1 |

---

### 3.7 R7: lane_manager.py — _lane_locks 无界增长

**涉及文件**: `astrmai/infrastructure/runtime/lane_manager.py:89-93`

#### 3.7.1 当前状态
`_lane_locks` 无淘汰机制。

#### 3.7.2 设计决策
`_get_lane_lock` 中添加 LRU 淘汰：`len > 100` 时 `popitem(last=False)`。

#### 3.7.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| lane_manager.py | +3行 | +3 |

---

### 3.8 R8: chat_runtime_coordinator.py — _states 从未清理

**涉及文件**: `astrmai/infrastructure/runtime/chat_runtime_coordinator.py:26-35`

#### 3.8.1 当前状态
无清理方法。

#### 3.8.2 设计决策
添加 `prune_inactive(max_idle_sec=1800)` 方法，在 terminate 中调用。

#### 3.8.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| chat_runtime_coordinator.py | +8行 | +8 |
| plugin_facade.py | +1行 | +1 |

---

### 3.9 R9: persistence_manager.py — dispose() 从未被调用

**涉及文件**: `astrmai/infrastructure/persistence/persistence_manager.py:54-55`

#### 3.9.1 当前状态
`dispose()` 已实现但未被调用。

#### 3.9.2 设计决策
在 `plugin_facade.py:terminate()` 中调用 `self.runtime.persistence.dispose()`。

#### 3.9.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| plugin_facade.py | +1 行 | +1 |

---

## 4. Wave 2 — 数据一致性/竞态修复 (R10–R14)

### 4.1 R10: reflect_tracker.py — try_consume_feedback TOCTOU 竞态

**涉及文件**: `astrmai/learning/review/reflect_tracker.py:70-134`

#### 4.1.1 当前状态
line 70 读 candidates → lock 释放 → LLM 调用(1-5s) → line 132 pop。并发可处理同一 pattern_id。

#### 4.1.2 设计决策
在 lock 内立即 `pop` 候选条目，LLM 调用在 lock 外进行。

#### 4.1.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| reflect_tracker.py | ~15 行重构 | +10/−8 |

---

### 4.2 R11: reflect_tracker.py — get_unsent_requests 标记 ALL 为 sent

**涉及文件**: `astrmai/learning/review/reflect_tracker.py:55-58`

#### 4.2.1 当前状态
line 56 for 循环遍历所有 `_pending.values()`，标记全部 sent。

#### 4.2.2 设计决策
仅标记 `requests` 中对应 pattern_id 的条目。

#### 4.2.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| reflect_tracker.py | +3/−2 行 | 净+1 |

---

### 4.3 R12: database_service.py — get_chat_state 无锁读取

**涉及文件**: `astrmai/infrastructure/persistence/database_service.py:189-209`

#### 4.3.1 当前状态
```python
with sqlite3.connect(self.persistence.db_path) as conn:
    cursor = conn.execute("SELECT ...")
```

#### 4.3.2 设计决策
connect 后执行 `PRAGMA journal_mode=WAL`。

#### 4.3.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| database_service.py | +1 行 | +1 |

---

### 4.4 R13: context_compaction.py — 压缩任务创建竞态

**涉及文件**: `astrmai/conversation/attention/context_compaction.py:297-314`

#### 4.4.1 当前状态
check (`existing`) 和 create/write 之间非原子。

#### 4.4.2 设计决策
使用 `asyncio.Lock` 保护 check-create-write 区域。

#### 4.4.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| context_compaction.py | +2 行 lock | +2 |

---

### 4.5 R14: gate.py — Session worker 阻塞

**涉及文件**: `astrmai/conversation/attention/gate.py:839-844`

#### 4.5.1 当前状态
```python
await self.sys2_process(...)  # 阻塞整个循环
```

#### 4.5.2 设计决策
`asyncio.create_task(self.sys2_process(...))` + 添加到追踪集合。

#### 4.5.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| gate.py | +3/−1 行 | 净+2 |

---

## 5. Wave 3 — 错误处理/LLM 韧性修复 (R15–R19)

### 5.1 R15: memory_retrieval_service.py — LLM 调用缺 lane_key

**涉及文件**: `astrmai/memory/services/memory_retrieval_service.py:383`

#### 5.1.1 当前状态
```python
response = await gateway.call_data_process_task(prompt=prompt, is_json=True)
```

#### 5.1.2 设计决策
添加 `lane_key=LaneKey(subsystem="bg", task_family="query_rewrite", scope_id="global")`。

#### 5.1.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| memory_retrieval_service.py | +1行 | +1 |

---

### 5.2 R16: hybrid_retriever.py — add_memory 返回 None

**涉及文件**: `astrmai/memory/retrieval/hybrid_retriever.py:27-31`

#### 5.2.1 当前状态
```python
return None  # 调用者误作有效 doc_id
```

#### 5.2.2 设计决策
`raise RuntimeError("Vector store offline")`。

#### 5.2.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| hybrid_retriever.py | 1行更改 | +1/−1 |

---

### 5.3 R17: bootstrap.py — 闭包捕获 pre-binding

**涉及文件**: `astrmai/app/bootstrap.py:504-510`

#### 5.3.1 当前状态
闭包内每次调用检查 `runtime.system2_callback is None`，已做保护。

#### 5.3.2 设计决策
审计确认：当前实现已正确。无需修改。

#### 5.3.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| bootstrap.py | 0 行 (审计确认) | 0 |

---

### 5.4 R18: lifecycle.py — track_task 无 RuntimeError guard

**涉及文件**: `astrmai/app/lifecycle.py:22-25`

#### 5.4.1 当前状态
```python
task = asyncio.create_task(coro)
```

#### 5.4.2 设计决策
`asyncio.create_task(coro)` → `safe_create_task(coro)`（已导入）。

#### 5.4.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| lifecycle.py | 1行更改 | +1/−1 |

---

### 5.5 R19: plugin_facade.py — 异常仅捕获 LLMCascade

**涉及文件**: `astrmai/app/plugin_facade.py:451-510`

#### 5.5.1 当前状态
```python
except LLMCascadeFailureException:
    ...
finally:
    ...
```

#### 5.5.2 设计决策
在 `except LLMCascadeFailureException` 后添加 `except Exception as e:` 兜底。

#### 5.5.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| plugin_facade.py | +4 行 | +4 |

---

## 6. Wave 4 — 事件流/Hook 修复 (R20–R21)

### 6.1 R20: main.py — heartflow_is_command 未实现

**涉及文件**: `main.py:134-137`

#### 6.1.1 当前状态
`on_global_message` 不做命令标记检查。

#### 6.1.2 设计决策
入口处检查 `event.get_extra("heartflow_is_command")`，True 则 `return`。

#### 6.1.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| main.py | +2 行 | +2 |

---

### 6.2 R21: main.py — 缺 on_llm_response hook

**涉及文件**: `main.py`

#### 6.2.1 当前状态
无 on_llm_response hook。

#### 6.2.2 设计决策
注册 `@filter.on_llm_response()` handler，记录响应摘要（200 字符截断）。

#### 6.2.3 影响范围
| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| main.py | +10 行 | +10 |

---

## 7. Risk Assessment

| 风险 | 等级 | 触发条件 | 缓解措施 |
|------|------|---------|---------|
| R2 v2_store LRU 淘汰活跃 session lock | 🟡 | 活跃 session > 200 时误淘汰 | lock 被淘汰后下次 `_get_session_lock` 重新创建 |
| R4 sweep_loop 延迟清理 | 🟢 | 无 `_instant_llm_last_check` 记录 | 使用 `time.time()` 回退 |
| R10 try_consume_feedback 重构 | 🟡 | 修改锁范围引入逻辑错误 | 最小改动：仅将 pop 前移到 lock 内 |
| R12 WAL mode 兼容性 | 🟢 | 旧版 sqlite3 不支持 WAL | WAL 从 sqlite 3.7.0 (2010) 开始支持 |
| R14 gate 异步化 sys2_process | 🟡 | 孤儿 task 未被追踪 | 添加到 `_background_tasks` + done_callback |
| R16 raise RuntimeError 破坏调用链 | 🟡 | 调用者未捕获 RuntimeError | 搜索调用点确认有 try/except |
| R17 bootstrap 闭包审计 | 🟢 | 审计结论 "已修复" 错误 | 代码已有检查，类型安全 |
| R20 heartflow_is_command | 🟡 | 标记语义不明确 | AstrBot docs 确认：标记 = 跳过 LLM 处理 |
| R18 safe_create_task 替换 | 🟢 | safe_create_task 未导入 | 确认 lifecycle.py 已有 `from ...shared.helpers import safe_create_task` |
| R21 on_llm_response 新增 | 🟢 | 钩子链中断其他插件 | try/except 包裹全逻辑 |

## 8. Verification Matrix

| 需求 | 验证方式 | 通过标准 |
|------|---------|---------|
| R1 | pytest + lsp_diagnostics | 无 import error，done_callback 方法存在 |
| R2 | pytest + lsp_diagnostics | OrderedDict 导入正确，淘汰逻辑可行 |
| R3 | pytest + lsp_diagnostics | 写入点有上限检查 |
| R4 | pytest + lsp_diagnostics | _sweep_loop 含清理逻辑 |
| R5 | pytest + lsp_diagnostics | record_usage 有长度限制 |
| R6 | pytest + lsp_diagnostics | trigger_affection_change 末尾有 .clear() |
| R7 | pytest + lsp_diagnostics | _get_lane_lock 有 LRU 淘汰 |
| R8 | pytest + lsp_diagnostics | prune_inactive 方法存在 |
| R9 | pytest + lsp_diagnostics | terminate() 调用 dispose() |
| R10 | pytest + lsp_diagnostics | try_consume_feedback 有 lock 保护 pop |
| R11 | pytest + lsp_diagnostics | get_unsent_requests 仅标记返回条目 |
| R12 | pytest + lsp_diagnostics | get_chat_state 启用 WAL |
| R13 | pytest + lsp_diagnostics | schedule_compaction_evaluation 有 lock |
| R14 | pytest + lsp_diagnostics | sys2_process 使用 create_task |
| R15 | pytest + lsp_diagnostics | call_data_process_task 传 lane_key |
| R16 | pytest + lsp_diagnostics | add_memory raise 替代 return None |
| R17 | pytest + lsp_diagnostics | 审计确认无修改 (0 diff) |
| R18 | pytest + lsp_diagnostics | track_task 使用 safe_create_task |
| R19 | pytest + lsp_diagnostics | _system2_entry 有 except Exception 兜底 |
| R20 | pytest + lsp_diagnostics | on_global_message 检查 heartflow_is_command |
| R21 | pytest + lsp_diagnostics | on_llm_response handler 已注册 |
| ALL | `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py` | 现有测试全部通过 |
