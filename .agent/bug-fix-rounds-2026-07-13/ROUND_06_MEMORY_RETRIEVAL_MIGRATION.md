# Round 06：记忆检索、RAG 与升级迁移

数量：9。依赖：Round 05 lane/gateway 稳定。

完成标准：canonical lexical rank 可用；deep retrieval 在最终选择前只读；candidate pool 与 injection top-k 分离；旧数据完整迁移。

## R06-01 / P1：canonical FTS 把所有负 BM25 分数压成 1.0
- 原始 ID：`AMR-01`；验证级别：B。
- 主文件：`astrmai/memory/services/v2_store.py`。
- 修复边界：保持 SQLite FTS5 lower-is-better 的单调顺序并归一化到 higher-is-better；后续 weighted sort 不得抹平 lexical rank。
- 回归目标：多条不同 BM25 分数保持严格相关顺序。

## R06-02 / P1：deep candidate pool 在最终选择前 mark_accessed/restore stale
- 原始 ID：`AMR-02`；验证级别：B。
- 主文件：`astrmai/memory/services/v2_store.py`, `memory_retrieval_service.py`。
- 修复边界：候选收集和 rerank 只读；只对最终返回/注入项更新 access 和 stale restoration。
- 回归目标：被 rerank 丢弃项的 access_count/status 不变。

## R06-03 / P1：legacy canonical 迁移后 FTS projection 为空且不回填
- 原始 ID：`AMR-03`；验证级别：B。
- 主文件：`astrmai/memory/services/v2_store.py`。
- 修复边界：迁移事务内投影全部 canonical rows，初始化检测 projection 完整性并可重建。
- 回归目标：迁移后旧记忆可由 canonical FTS 查到，不依赖 bounded recent fallback/FAISS。

## R06-04 / P2：adaptive candidate_limit 未进入 source return/fusion/rerank
- 原始 ID：`AMR-04`；验证级别：B。
- 主文件：`astrmai/memory/services/memory_retrieval_service.py`, `v2_store.py`。
- 修复边界：candidate_limit 控制粗召回返回数量，top_k 只在 dedup/rerank 后用于注入；禁止二次 `*8` 放大。
- 回归目标：candidate_limit=24 时 fusion 可见至多 24 候选且 store 不拉 192。

## R06-05 / P2：Hybrid RRF 分数未归一化便与 [0,1] 组件融合
- 原始 ID：`AMR-05`；验证级别：B。
- 主文件：`astrmai/memory/utils.py`, `astrmai/memory/retrieval/hybrid_retriever.py`, `memory_retrieval_service.py`。
- 修复边界：统一 score scale 或全程 rank fusion，避免静态 importance/confidence 淹没 query relevance。
- 回归目标：强 lexical/vector match 能稳定高于弱匹配高 importance 记录。

## R06-06 / P2：memory temporal/hybrid hot settings 仍绑定旧 runtime object
- 原始 ID：`AMR-06`；验证级别：B。
- 主文件：`astrmai/memory/services/memory_engine.py`, `memory_retrieval_service.py`, `astrmai/memory/retrieval/hybrid_retriever.py`。
- 修复边界：刷新 scoring 和 retriever config/derived values；embedding rebuild 逻辑保持独立。
- 回归目标：仅修改 temporal/decay 配置后下一次 retrieve 使用新值，无需顺带更换 embedding model。

## R06-07 / P1：群聊 memory turn 丢 sender_id，权威事实跨成员覆盖
- 原始 ID：`AM-MEM-06-01`；验证级别：B。
- 主文件：`astrmai/conversation/execution/reply_post_send.py`, `astrmai/memory/services/memory_turn_pipeline.py`, `instant_memory_gate.py`。
- 修复边界：群事实 subject 必须使用真实 sender；chat ID 只表示会话范围。
- 回归目标：同群甲乙相同 attribute 产生不同 canonical subject/dedup key。

## R06-08 / P1：shutdown 丢弃未达到 summary threshold 的 committed turns
- 原始 ID：`AM-MEM-06-02`；验证级别：B。
- 主文件：`astrmai/memory/services/memory_turn_pipeline.py`, `astrmai/app/lifecycle.py`。
- 修复边界：stop 前 drain/flush 非空 session buffer，或持久化可重放 journal；取消顺序不能先清数据。
- 回归目标：低于阈值的短会话正常卸载后可在 store/恢复队列观察到。

## R06-09 / P1：legacy documents 导入打开 v2 DB 并把缺表标为完成
- 原始 ID：`AM-MEM-06-04`；验证级别：B。
- 主文件：`astrmai/memory/services/memory_engine.py`, `v2_store.py`, `memory_migration_service.py`。
- 修复边界：始终读取 `legacy_db_path`；只有真实源遍历完成才写 applied marker，源不可用保持可重试。
- 回归目标：旧 docs.db 记录被导入；错误路径/临时缺表不会生成不可逆成功标记。
