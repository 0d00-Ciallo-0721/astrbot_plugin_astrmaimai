# 开发窗口 04：Conversation Planning — Prompt 乱码修复 + 记录完整性

## 必须先读取的审查报告
1. `artifacts/review_new/r03-conversation-planning.md` — 1🔴 6🟡 7🟢

## 审查范围
`astrmai/conversation/planning/`（19 个源文件）

---

## 🔴 严重（1 项）— P0

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `prompt_refiner.py:105-113` | **`_render_runtime_guidance_cluster` 包含乱码中文**。三个 section 标题编码损坏：`---???????---`（内在驱动）、`---褰撳墠鐘舵€佷笌绾︽潫---`（当前状态与约束）、`---鏈疆涓婁笅鏂囪В閲?--`（本轮上下文解析）。这些乱码被注入 LLM prompt。**修复**：重写为正确的 UTF-8 中文。 |

---

## 🟡 中等（6 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 2 | `conversation_continuity.py:174-178` | **wait/ignore/lightweight 轮次不记录到 `state.turns`**。与注释矛盾，下游 agency feedback 可能失效。 |
| 3 | `cognitive_loop.py:58` | **`COMPLEXITY_HINTS` 包含单字符 `"查"`**，过于宽泛。任何含"查"的消息触发 complexity 判定。 |
| 4 | `cognitive_loop.py:345-349` | **问号判定逻辑冗余**。原始文本与去空白文本混用，建议统一用 `compact`。 |
| 5 | `conversation_continuity.py:68-93` | **短文本字符级 Jaccard 相似度虚高**。"你好" vs "你好呀" ≈ 0.8，建议用词级比较。 |
| 6 | `context_engine.py:238-243` | **`_resolve_visual_memory_refs` 循环内独立打开 DB session**。移到循环外。 |

---

## 🟢 建议（重点关注）

- `planner.py:890-1319` `plan_and_execute` ~430 行 — 标记技术债，暂不拆分
- `cognitive_loop.py:36` `SOFT_TIMEOUT_SECONDS = 2.5` 硬编码 → 迁入配置

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_cognitive_loop_refactor.py tests/test_conversation_continuity_refactor.py tests/test_planner_cognitive_loop_refactor.py -q
```

## 成功标准
- 🔴 #1 修复（P0 最高优先级）
- 🟡 #2 #3 修复
- 64+ 相关测试通过
