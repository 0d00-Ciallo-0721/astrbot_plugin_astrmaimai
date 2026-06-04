# Agent 05

Agent ID:
`019e71b7-c6a5-74b1-ac1f-85edb8db9084`

状态：
已完成

发现：
1. `[P1]` Dream 的通用检索仍会串到 `feedback/tool_only` 层，边界没有收紧。`DreamAgent._tool_search_memory()` 直接发起不限 `kind`、不限 `visibility_mode` 的查询，所以它会看到同会话下的 `feedback`、以及其他 `tool_only` 记录；而认知反馈正是以 `kind="feedback", visibility="tool_only"` 写入 canonical 的。这样 Dream 整理会把“内部反馈提示”当成长期记忆素材参与推理，违反了 dream 与反馈层分离。[dream_agent.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/dream/dream_agent.py:197) [memory_engine.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_engine.py:368) [v2_store.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/v2_store.py:621)
2. `[P1]` 长期记忆主链仍在双写 legacy `MemoryEvent`，而后续维护路径只改 canonical，双轨债务仍然活着。`SessionMemorySummarizer` 先 canonical write，再额外保存一份 legacy `MemoryEvent`；但 `DreamAgent` 在 canonical 命中时更新/删除只操作 canonical，不回写 legacy。结果就是同一条记忆会出现 canonical/legacy 漂移，Dream fallback 仍可能读到旧版本。
3. `[P2]` auto injection 的 `selected_ids` 仍按“预选中”而不是“实际渲染到 prompt”记账。`build_bundle()` 先 `select()`，再交给 `render_prompt_block()` 按 `max_chars` 截断；但 trace 和后续工具排除用的还是截断前的 `selected`。一旦记忆块超长，未真正注入的 memory id 也会被标成“已注入”，随后 tool 检索被错误排除，trace 也会失真。
4. `[P2]` retrieval trace 在 light/jargon 路径下仍不够可解释。`MemoryRetrievalService.retrieve()` 的 light 路径只填 `selected`，不填 `retrieved`；`jargon/expression_pattern` 早返回路径连 `search_steps` 都不落。可 `MemoryInjectionService` 和 `ReActRetriever` 的 trace summary 又依赖这些字段算 `retrieved_count/search_steps`，所以现在很多“实际命中过”的检索在落库后会表现成 `retrieved_count=0` 或缺少搜索步骤。

测试缺口：
- 缺少 Dream 检索不会带入 `feedback/tool_only/non-dream kind` 的回归测试。
- 缺少 memory injection 在 `max_chars` 截断时，`selected_ids` 只统计真实渲染项的测试。
- 缺少 light/jargon 路径 trace 必须填充 `retrieved_count/search_steps` 的测试。

验证：
只做了静态复检；按只读要求未运行 `pytest`。
