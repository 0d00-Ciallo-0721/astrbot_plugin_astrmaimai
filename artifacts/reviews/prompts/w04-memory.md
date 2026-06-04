# 开发窗口 04：Memory 修复

## 必须先读取的审查报告
1. `artifacts/reviews/r05-memory.md` — 完整发现清单（3🔴 5🟡 5🟢）
2. `artifacts/reviews/r15-master.md` — 总报告
3. `artifacts/reviews/r13-session-fixes.md` — 了解本轮已修复（T1-T3/T7 mock）

## 目标文件
- `astrmai/memory/contracts/memory_query.py` — MemoryQuery 数据类
- `astrmai/memory/services/memory_engine.py` — MemoryEngine 核心
- `astrmai/memory/services/memory_retrieval_service.py` — 检索服务
- `astrmai/memory/services/memory_write_service.py` — 写入服务
- `astrmai/memory/services/v2_store.py` — V2 向量存储
- `astrmai/memory/services/memory_injection_service.py` — 注入服务

## 依赖
窗口 02（persistence）+ 窗口 03（state）

---

## 🔴 严重（3 项）

### P4-1：MemoryQuery 死字段
- **文件**：`astrmai/memory/contracts/memory_query.py:28-29`
- **问题**：`include_feedback: bool = False` 和 `retrieve_keys: List[str]` 两个字段在 dataclass 中声明，在 `memory_retrieval_service.py:108-118` 的 `_retrieve_queries` 中被复制到 scoped_query，但**所有下游检索逻辑**（`_retrieve_once`、`v2_store.search`、`_hybrid_search`）均从未读取这两个字段。调用方设置 `include_feedback=True` **不会有任何效果**。
- **最小修复**（二选一）：
  - A：在 `_retrieve_once` 中实现过滤逻辑（若 `include_feedback=True` 则在结果中包含 cognitive feedback 条目）
  - B：标记为 `@deprecated` 并在 docstring 中说明，待下一 major 版本移除

### P4-2：详见 r05-memory.md
### P4-3：详见 r05-memory.md

---

## 🟡 中等（5 项）

详见 `r05-memory.md`，重点：
- `MemoryEngine.recall` 从旧 `_search_memories` 迁移到 `retrieval_service.retrieve` 后，FAISS fallback 路径可能被绕过
- `MemoryWriteService` 与 `MemoryRetrievalService` 的接口一致性（均依赖 `MemoryV2Store`）
- `v2_store.initialize()` 的 faiss 索引初始化失败时缺少降级
- `MemoryInjectService` 的 `inject` 方法在 retrieval 返回空时的行为

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_cognitive_feedback_refactor.py tests/regression/persistence/test_persistence_regressions_migrated.py -q
```

## 成功标准
- 🔴 P4-1：死字段要么实现逻辑，要么标记废弃
- 🔴 3 项全部修复
- Memory 相关测试全部通过（T1-T3/T7 已修复）
