# 审查报告：astrmai/memory/
> task_id: r12-memory | 审查时间: 2025-07-14

## 概述
- 审查文件数: 12 (contracts: 3, services: 6, retrieval: 3)
- 发现总数: 13
- 严重: 3 | 中等: 5 | 建议: 5

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `contracts/memory_query.py:28-29` | **`MemoryQuery.include_feedback` 和 `retrieve_keys` 是死字段** — 两个字段在 dataclass 中声明，在 `_retrieve_queries`（`memory_retrieval_service.py:108-118`）中被复制到 scoped_query，但下游所有检索逻辑（`_retrieve_once`、`v2_store.search`、`_hybrid_search`）均从未读取或求值。这构成误导性 API 表面：调用方设置 `include_feedback=True` 不会有任何效果。应移除字段或实现对应的过滤逻辑。 |
| 2 | `services/memory_engine.py:377-402` | **`get_cognitive_feedback` 从遗留 `documents` 表读取而非 `canonical_memories`** — `record_cognitive_feedback`（第 313 行）通过 `write_service.write` 写入 `v2_store`，后者存储在 `canonical_memories` 表中。但 `get_cognitive_feedback` 仍对旧的 `documents` 表执行 `PRAGMA table_info` + `SELECT` 查询。这意味着通过新路径写入的任何认知反馈（使用 `record_cognitive_feedback`）永远不会被 `get_cognitive_feedback` 找到。仅内存缓存（每 chat 32 条）可工作。 |
| 3 | `services/memory_engine.py:377` | **遗留 `documents` 表不存在时，认知反馈查询静默降级为仅缓存** — 在新安装或没有遗留数据的部署中，`PRAGMA table_info(documents)` 返回空集，`columns` 为空，跳过整个 SQL 查询块。`get_cognitive_feedback` 只会返回 `_cognitive_feedback_cache` 中的内容（仅 32 条/chat），且重启后丢失。应改为从 `canonical_memories` 表查询 `kind = 'feedback'`。 |

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | `services/memory_retrieval_service.py:146` | **`_hybrid_search` 强耦合到 MemoryEngine 的私有方法** — 通过 `self.engine._search_memories`（单下划线，约定为私有）访问。如果该方法被重命名、移除或签名变更，`RetrievalService` 静默返回空列表（`hasattr` 返回 False），用户不会收到警告。建议将 `_search_memories` 提升为 MemoryEngine 的公共方法，或让 RetrievalService 直接持有 HybridRetriever 引用。 |
| 5 | `services/memory_engine.py:480` | **`recall` 在检索后过滤认知反馈内容** — 调用 `retrieval_service.retrieve(memory_query)` 获取所有候选项，然后通过列表推导式过滤掉 `_is_cognitive_feedback_content` 为 True 的条目。这意味着认知反馈项被检索、评分、融合后又被丢弃，浪费了 v2_store 和混合检索引擎的资源。建议在 MemoryQuery 中增加 `exclude_kinds={"feedback"}` 或类似机制，在检索层进行过滤。 |
| 6 | `services/memory_engine.py:224` | **`_ensure_faiss_initialized` 中 BM25 重新初始化守卫永远为 False** — `if not self.bm25_retriever:` 检查在 `initialize()`（第 132 行）中已经初始化了 `self.bm25_retriever` 后，此检查永远为 False。这是一个死代码路径。当 faiss 懒加载时，如果第一次加载失败，BM25 也会丢失，因为此守卫不会重新初始化。 |
| 7 | `services/memory_engine.py:334` | **认知反馈的 `dedup_key` 包含无界长度的摘要/指导语** — `dedup_key` 构造为 `f"feedback:{chat_id}:{source}:{summary}:{guidance}"`。虽然 `summary` 和 `guidance` 被截断到 500 字符，但组合键长度可达 ~1000+ 字符。SQLite 索引键长度有限（默认约 1000 字节），可能导致插入失败或索引被截断。建议用 `hash(summary) + hash(guidance)` 的十六进制摘要替换摘要/指导语的原始内容。 |
| 8 | `services/v2_store.py:72` 与 `services/memory_engine.py:59` | **v2_store 和 Faiss/BM25 共享同一个 SQLite 文件 `docs.db`** — `MemoryV2Store` 和 `FaissVecDB`/`BM25Retriever` 都使用 `self.db_path`（`docs.db`）。虽然它们使用不同的表（`canonical_memories` vs `documents`），但同文件意味着：DDL 迁移可能相互干扰；打开多个连接可能导致 WAL 锁竞争；如果任一组件执行破坏性操作（如 `DROP TABLE`），可能导致整个数据库不可用。建议将 v2_store 的表放在独立的 `memory_v2.db` 文件中。 |

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 9 | `services/memory_engine.py:388` | **`get_cognitive_feedback` 使用 `LIKE '[cognitive_feedback:%'` 导致全表扫描** — 在 `documents` 表上使用前导通配符的 LIKE 查询无法使用索引，在大量反馈记录上性能会严重下降。如果迁移到 canonical_memories 表（见发现 #2），应使用 `WHERE kind = 'feedback'` 加索引的查询。 |
| 10 | `services/memory_engine.py:396`、`services/memory_retrieval_service.py:82` 等多处 | **多处使用裸 `except Exception` + `logger.debug` 导致静默降级** — 检索和反馈路径中多个关键操作被 `try/except` 包裹，错误仅以 DEBUG 级别记录。在生产环境（通常不启用 DEBUG 日志）中，错误完全不可见，运维人员无法诊断检索质量下降或反馈丢失的问题。建议至少对结构性/意外异常使用 `logger.warning`。 |
| 11 | `services/memory_engine.py:279` | **`_remember_cognitive_feedback` 使用列表拼接 `[*items, signal]` 而非 `append`** — `self._cognitive_feedback_cache[signal.chat_id] = items[-32:]` 在每次调用时创建新列表。对于热路径调用（每个对话消息都可能触发），这会增加 GC 压力。建议改用 `list.append` + `list.pop(0)` 或 `collections.deque`。 |
| 12 | `services/memory_engine.py:469-477` | **`recall` 未向 MemoryQuery 传递 `layers`** — `recall` 创建的 MemoryQuery 仅设置 `query`、`session_id`、`persona_id`、`top_k`，`layers` 使用默认空列表。这意味着检索路径中 `intent="jargon"` 或 `intent="expression_pattern"` 的快捷路由（`_retrieve_once` 中的检查）永远不会通过 `recall` 触发，限制了语义搜索的覆盖范围。 |
| 13 | `contracts/memory_query.py:27` | **`layers` 字段注释不足** — `layers: List[str]` 的默认空列表行为在文档中未说明。实际代码检查 `set(query.layers)` 的真值来决定是否按 kind 过滤。当 `layers=[]` 时不过滤（返回所有 kind），但调用方可能会误以为空列表意味着选择"默认层"。建议加 docstring 或用 `None` 语义区分"未指定"和"明确为空"。 |

## 亮点

- **架构设计优秀**：`MemoryV2Store` 作为权威存储，`MemoryIndexProjector` 负责将 canonical 记录投影到混合索引，实现了清晰的读写分离和存储/索引解耦。
- **`MemoryQuery` dataclass 设计全面**：涵盖会话、人物、策略、时间窗口、排除列表等丰富的检索参数，虽有个别字段未使用（见 #1），但整体设计为未来扩展提供了良好的基础。
- **错误韧性设计**：多处 `asyncio.gather(..., return_exceptions=True)` 和 `try/except` 降级处理，确保单个检索源故障不会使整个检索崩溃。
- **懒加载机制**：Faiss 向量库仅在首次检索请求时才初始化，避免启动时因 embedding 模型不可用而导致整体失败，并实现了指数退避重试。
- **`MemoryUpsertResult` 作为 dict 子类提供属性访问**：同时支持 `.memory_id` 属性访问和 `["memory_id"]` 字典访问，兼容新旧调用方。

## 总结

astrmai/memory 模块整体架构设计良好，成功地将原有基于 Faiss+BM25 的混合检索重构为以 `MemoryV2Store`（SQLite canonical 表）为权威存储的双层架构。迁移路径清晰（`MemoryMigrationService` 配合版本记录），投影机制（`MemoryIndexProjector`）确保索引层与存储层最终一致。

**主要风险**集中在 `cognitive_feedback` 子系统中：`get_cognitive_feedback` 仍从遗留 `documents` 表读取，而写入已改道 `canonical_memories` 表（发现 #2），这是一个功能性回归 bug，会导致新写入的反馈不可见。另外，`MemoryQuery` 中有两个死字段（`include_feedback`、`retrieve_keys`）可能误导调用方（发现 #1）。

次要改进点包括：解除 `RetrievalService` 对私有方法 `_search_memories` 的耦合（#4）、修复反馈后置过滤的效率问题（#5）、将 v2_store 分离到独立数据库文件以避免表间锁竞争（#8）。
