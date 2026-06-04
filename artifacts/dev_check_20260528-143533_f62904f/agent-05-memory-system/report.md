# Agent 05

Agent ID:
`019e6d48-7b85-7db1-acbd-6a6666dfbd2a`

状态：
已完成

Findings:
1. `[P1] Dream 维护仍然是 legacy-first 双轨执行，不是 canonical/v2 主轨。` `DreamAgent` 先按旧 `MemoryEvent` 选 session 和 seed，只有旧表路径失败时才回退 canonical；而 merge/update/delete 又会同时改 legacy 行和 canonical projection。只要库里还留着旧表数据，dream 就不会稳定覆盖 v2 主存，反而会继续扩大同步债。Refs: [dream_agent.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/dream/dream_agent.py:222), [dream_agent.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/dream/dream_agent.py:473)

2. `[P1] Memory injection 确实可能污染主回复，因为记忆文本会被原样抬升进最终提示词。` 写入侧只过滤空串/JSON/异常噪声，没有把“指令型文本”降权或转义；`MemoryContextBuilder` 直接把 `summary/content` 拼成内部 memory block，而 `PromptRefiner` 又把这段 block 直接塞进最终 prompt。被记住的用户原话如果带有“忽略前文/按我说做”之类措辞，会变成高权重上下文。Refs: [memory_write_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_write_service.py:15), [memory_context_builder.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_context_builder.py:30), [prompt_refiner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/prompt_refiner.py:946)

3. `[P2] Persona summarizer 直接做 destructive 的长期记忆重建，persona 与 memory 边界不清。` 后台 shard 任务一开始就 `clear_persona_lore()` 然后把原始 persona prompt 重新写回 `persona_lore`；这让“人格摘要”不再是只读/只算摘要，而是会主动改长期记忆。中途失败时还会出现 clear 已执行但 lore 未成功回写的窗口。Refs: [persona_summarizer.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/persona/persona_summarizer.py:276), [memory_engine.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_engine.py:516)

4. `[P2] 主 v2 检索链路的 trace 不够可解释，出了误注入/漏召回时很难复盘。` 自动注入侧只有 `policy/source/layers/counts/ids/preview` 这类轻量字段，而且只挂在 event extra 上；持久化 `RetrievalTrace` 又只覆盖 ReAct 工具链，记录的也是 tool calls、selected ids、final answer、source layers。canonical vs hybrid 各自分数、query rewrite、rerank、visibility/stale 过滤决策都没有持久化。Refs: [memory_query.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/contracts/memory_query.py:88), [memory_injection_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_injection_service.py:91), [retrieval_trace.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/contracts/retrieval_trace.py:10), [react_retriever.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/retrieval/react_retriever.py:321)

假设 / 开放点：
- `retrieve_keys` 目前看起来更像“被透传但未生效”的接口：它从注入入口写进 `MemoryQuery`，但在 v2 retrieval 里只是继续拷贝，没有真正参与 layer/score/filter 决策。若窗口1把它当成“检索边界控制”，那这部分现在是空合同。Refs: [memory_injection_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_injection_service.py:128), [memory_retrieval_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_retrieval_service.py:92)

验证：
- 已只读运行：`python -B -m pytest -q -p no:cacheprovider tests/unit/memory/test_memory_v2_services.py tests/regression/memory/test_react_retriever_traces_migrated.py tests/regression/memory/test_memory_v2_tool_injection.py tests/unit/memory/test_memory_contracts_migrated.py`
- 结果：`49 passed`
- 警告：3 条三方库 `DeprecationWarning`，1 条 `after_nonebot_init` 未 awaited 的 `RuntimeWarning`
- 本次未修改任何代码或文件。
