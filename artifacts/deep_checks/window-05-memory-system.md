# 窗口 5：Memory 主系统

模块：
Memory 主系统（`astrmai/memory/services/*`、`astrmai/memory/retrieval/*`、`astrmai/memory/persona/*`、`astrmai/memory/dream/*`）

职责：
负责 memory write / retrieve / inject、dream 维护、persona 汇总、retrieval trace，以及 canonical v2 store 与 legacy 读写通道的协作。

关键文件：
- `astrmai/memory/services/memory_engine.py`
- `astrmai/memory/services/instant_memory_gate.py`
- `astrmai/memory/services/memory_index_projector.py`
- `astrmai/memory/services/memory_retrieval_service.py`
- `astrmai/memory/retrieval/react_retriever.py`
- `astrmai/memory/retrieval/bm25.py`
- `astrmai/memory/persona/persona_summarizer.py`
- `astrmai/memory/dream/dream_agent.py`
- `astrmai/memory/services/session_memory_summarizer.py`

现有测试：
- `tests/unit/memory/test_memory_v2_services.py`
- `tests/regression/memory/test_memory_v2_tool_injection.py`
- `tests/regression/memory/test_react_retriever_traces_migrated.py`
- `tests/unit/memory/test_memory_contracts_migrated.py`
- 实跑：`python -m pytest tests/unit/memory/test_memory_v2_services.py tests/regression/memory/test_memory_v2_tool_injection.py tests/regression/memory/test_react_retriever_traces_migrated.py tests/unit/memory/test_memory_contracts_migrated.py -q`
- 结果：`45 passed, 1 warning`

主要发现：
1. `[高]` `dream` 仍只以旧 `MemoryEvent` 作为维护种子，canonical-only 的即时记忆进不了 dream consolidation。
   - 依据：`astrmai/memory/dream/dream_agent.py:372`、`:404` 选 session 和取 seed 都只查 `MemoryEvent`。
   - 进一步依据：`astrmai/memory/services/instant_memory_gate.py:35`、`:112` 命中后只通过 `write_service` 写 canonical memory，不同步写 `MemoryEvent`。
2. `[高]` 持久化后的 cognitive feedback / recent memory 读取仍依赖旧 `documents` 投影，且 `get_recent_memories()` 还强依赖 FAISS ready。
   - 依据：`astrmai/memory/services/memory_engine.py:390` 与 `:759` 读取链仍会卡在旧 projection / 索引状态。
   - 进一步依据：`astrmai/memory/services/memory_index_projector.py:16` 只有 `engine.retriever` 已就绪时才把 projection 写回 `documents`。
3. `[中]` retrieval trace 对 v2 结果基本不可解释。
   - 依据：`astrmai/memory/retrieval/react_retriever.py:312` 只用正则从结果文本抓 `evt_*`。
   - 进一步依据：`astrmai/memory/services/memory_retrieval_service.py:399` 的 v2 `render_recall()` 返回纯摘要，不带 canonical memory id，因此 `selected_memory_ids` 经常为空。
4. `[中]` v2 / legacy 仍是运行时双轨，不只是迁移兼容。
   - 依据：`astrmai/memory/retrieval/bm25.py:10` 继续读 `documents`；`astrmai/memory/services/session_memory_summarizer.py:149` 同时写 canonical 和 `MemoryEvent`；`astrmai/memory/persona/persona_summarizer.py:24`、`:287` 仍落 `persona_cache.json` 并反向写 memory。

未实现/不完整项：
1. 没有覆盖“重启后仅靠 canonical store 读取 cognitive feedback / recent memories”的测试。
2. 没有覆盖“instant gate 写入的 canonical-only memory 能被 dream maintenance 看见”的测试。
3. `memory injection` 主链未发现明确污染回复，但缺少端到端测试证明 internal block 不会泄露到最终 reply。

高风险点：
1. 即时记忆到 dream 维护链条天然断开，意味着用户刚写入的重要记忆不会进入后续 consolidation。
2. canonical store 明明有数据，但读取仍可能被旧 documents / FAISS 状态卡死，重启后行为尤为不稳定。

建议下一步：
1. 先补 canonical-only 的 dream / feedback / recent-memory 回归测试，验证重启后不依赖旧 projection 也能回读。
2. 再收敛 v2 / legacy 双轨，至少先让 retrieval trace 能稳定带出 canonical memory id，并明确哪些旧通道仍是必须兼容。
