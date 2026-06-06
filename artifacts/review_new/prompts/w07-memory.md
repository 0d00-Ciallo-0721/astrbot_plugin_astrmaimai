# 开发窗口 07：Memory — supersede 逻辑漏洞 + 迁移安全

## 必须先读取的审查报告
1. `artifacts/review_new/r06-memory.md` — 3🔴 8🟡 7🟢

## 审查范围
`astrmai/memory/`（16 个源文件）

---

## 🔴 严重（3 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `v2_store.py:560-590` | **`mark_superseded_by_key` 早 break 导致残留活跃副本**。多条比新记录更新的旧记录未被处理。**修复**：不 break，循环处理全部旧记录。 |
| 2 | `v2_store.py:162-204` | **ATTACH DATABASE 主键冲突风险**。旧版 docs.db 已有 canonical_memories 表时 INSERT 崩溃。**修复**：`INSERT OR IGNORE` 或先 DELETE 冲突行。 |
| 3 | `memory_engine.py:250-268` | **`_run_documents_query` 默认 `db_path` 脆弱**。默认指向 docs.db 但 canonical_memories 在 memory_v2.db。**修复**：废弃默认值，强制显式指定。 |

---

## 🟡 中等（重点 5 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | `v2_store.py:1080-1105` | `apply_decay` FTS 删除与主表删除保护条件不同步 |
| 5 | `instant_memory_gate.py:180-200` | `run_llm_backfill` TypeError 捕获范围过宽，缩小到仅包裹 gateway 调用 |
| 6 | `memory_retrieval_service.py:205-240` | canonical 与 hybrid 双源 session_id 隔离策略不一致 |
| 7 | `memory_retrieval_service.py:280-295` | `conflict_penalty=0.2` 硬编码，纳入 `MemoryScoringConfig` |
| 8 | `memory_engine.py:340-370` | 函数内嵌 `import asyncio`，移到文件顶部 |

---

## 🟢 建议（选做）

- `v2_store.py:280-285` CJK 双字分片高频召回噪音 → 改用 jieba
- `memory_engine.py:200-210` defense-in-depth 检查假阴性 → 增加 metadata 兜底
- `utils.py:55-70` RRF 融合 doc_id 命名空间约束 → 添加文档注释

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_cognitive_feedback_refactor.py tests/unit/memory/ tests/regression/memory/ tests/test_memory_refactor.py -q
```

## 成功标准
- 🔴 3 项全部修复
- 🟡 #4 #5 #8 修复
- 80+ 相关测试通过
