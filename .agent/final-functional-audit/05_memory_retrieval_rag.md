# Final Functional Audit: Memory Retrieval and RAG

## Audit result

- Working tree audited: current production tree, including uncommitted changes.
- Confirmed defects: **6** (**P0: 0, P1: 3, P2: 3, P3: 0**).
- Scope: `astrmai/memory/retrieval/`, `astrmai/memory/contracts/`, the assigned retrieval-facing services, and adjacent production callers needed to prove reachability.

## Findings

### AMR-01 / P1 - Canonical FTS collapses normal SQLite BM25 scores to the same relevance

- **File:line:** `astrmai/memory/services/v2_store.py:840` (subsequent weighted reorder at `v2_store.py:879`)
- **Trigger:** A canonical FTS query matches two or more memories with ordinary SQLite FTS5 `bm25()` values, which are lower-is-better and normally negative.
- **Real call chain:** `Planner._invoke_planning_llm()` (`astrmai/conversation/planning/planner.py:1098`) -> `PromptRefiner._decide_memory_injection()` (`prompt_refiner.py:791`) -> `MemoryInjectionService.build_bundle()` (`memory_injection_service.py:203`) -> `MemoryRetrievalService.retrieve()` (`memory_retrieval_service.py:317`) -> `_retrieve_once()` (`memory_retrieval_service.py:407`) -> `MemoryV2Store.search()` (`v2_store.py:768`) -> `bm25(canonical_fts)` ordered ascending (`v2_store.py:825-829`) -> relevance conversion (`v2_store.py:840`). The same store path is also reached by proactive recall, memory tools, persona lore recall, and topic merge lookup.
- **Actual behavior:** The SQL ordering is initially correct, but `1 / (1 + max(0, fts_score))` maps every negative BM25 score to `1.0`. The candidates are then sorted again using this flattened relevance plus importance, confidence, and recency, so lexical rank is discarded before service-level fusion.
- **Expected behavior:** The BM25-to-relevance transform must remain monotonic with SQLite's lower-is-better score direction, preserving the SQL rank while producing a comparable relevance scale.
- **Production impact:** A less relevant but newer or more important memory can replace the best lexical match and be injected into the reply prompt. This affects automatic RAG, explicit memory tools, proactive memory hints, and persona-lore recall whenever canonical FTS returns multiple rows.
- **Why existing guards fail:** `ORDER BY fts_score ASC` only protects the initial row order; the later weighted sort at line 879 overrides it after all negative scores have been flattened. Hybrid fusion cannot reliably repair the order, especially when embeddings are unconfigured or degraded.
- **Classification:** confirmed
- **Confidence:** 0.99

### AMR-02 / P1 - Deep retrieval records access and restores stale memories before final selection

- **File:line:** `astrmai/memory/services/v2_store.py:889` (state mutation at `v2_store.py:896-906`)
- **Trigger:** A request uses `policy="deep"` or `think_level >= 3`, causing the retrieval service to collect a temporal candidate pool larger than the final `top_k`; the stale-restoration branch additionally triggers when a pooled stale item has prior access.
- **Real call chain:** Cognitive decision sets deep policy (`astrmai/conversation/planning/planner.py:1256-1267`) -> `MemoryInjectionService.build_bundle()` sets `allow_stale` (`memory_injection_service.py:203-215`) -> `MemoryRetrievalService.retrieve_deep()` creates a pool of at least 20 candidates (`memory_retrieval_service.py:329-348`) -> `_retrieve_queries()` -> `_retrieve_once()` -> `MemoryV2Store.search()` -> `restore()` and `mark_accessed()` for the store's entire returned pool (`v2_store.py:889-906`) -> temporal/LLM reranking -> final `top_k` selection (`memory_retrieval_service.py:340-352`).
- **Actual behavior:** Every canonical candidate returned to the deep candidate pool receives a new `last_access_time` and incremented `access_count`, even though most are later discarded. Eligible stale pool entries are restored to `active` and reprojected before the final reranker decides whether they will be used.
- **Expected behavior:** Retrieval must remain read-only while collecting/reranking candidates. Access accounting and stale restoration should occur only for the final memories actually returned or injected.
- **Production impact:** Deep recall artificially heats unrelated memories, inflates access counters, delays or prevents decay, and can permanently reactivate stale records that the model never saw. Repeated deep requests progressively corrupt maintenance/decay behavior and retrieval ranking.
- **Why existing guards fail:** The store treats its local `result_limit` as the final selection boundary, but deep retrieval deliberately uses that boundary as an intermediate pool. No later layer rolls back access mutations or distinguishes pooled candidates from final selections.
- **Classification:** confirmed
- **Confidence:** 0.99

### AMR-03 / P1 - Legacy canonical migration creates an empty FTS index and never backfills it

- **File:line:** `astrmai/memory/services/v2_store.py:370` (FTS table creation without backfill at `v2_store.py:269-281`)
- **Trigger:** Upgrade startup finds a legacy `docs.db` containing `canonical_memories` while `memory_v2.db` does not yet exist.
- **Real call chain:** `LifecycleManager.initialize_memory()` (`astrmai/app/lifecycle.py:40-48`) -> `MemoryEngine.initialize()` (`memory_engine.py:194-215`) -> `MemoryV2Store.initialize()` (`v2_store.py:195`) -> `_migrate_from_legacy_db()` copies canonical rows (`v2_store.py:319-384`) -> `canonical_fts` is created (`v2_store.py:269-281`) -> later `MemoryV2Store.search()` queries the empty FTS table (`v2_store.py:818-844`) and falls back to a bounded recent-row scan (`v2_store.py:845-877`).
- **Actual behavior:** Migrated canonical rows are never inserted into `canonical_fts`. The fallback first limits rows by newest `update_time` and only then checks text overlap, so with default `top_k=5` only the newest 40 canonical rows are searchable; older migrated memories remain unreachable through canonical retrieval. There is no later migration or startup repair for FTS completeness.
- **Expected behavior:** Migration must populate FTS for every searchable migrated canonical row, or initialization must detect and rebuild an incomplete canonical FTS projection.
- **Production impact:** After upgrade, established long-term memories can silently disappear from recall. The impact is complete when embeddings are unconfigured (the schema default) or FAISS/provider startup degrades; even with vectors, canonical fallback no longer provides the promised independent retrieval path.
- **Why existing guards fail:** The fallback is bounded before matching and therefore is not a full-store fallback. FAISS initialization is lazy and optional, while the index-consistency repair only covers the legacy vector/document projection, not `canonical_fts`.
- **Classification:** confirmed
- **Confidence:** 0.98

### AMR-04 / P2 - Adaptive `candidate_limit` does not reach fusion or intent reranking

- **File:line:** `astrmai/memory/services/memory_retrieval_service.py:381` (scoped `top_k` assignment at line 387)
- **Trigger:** `memory.adaptive_top_k_enabled` is enabled. `MemoryQueryBuilder` sets an injection `top_k` and a larger `candidate_limit` (normally `top_k * 3`), optionally together with `intent_rerank_enabled`.
- **Real call chain:** Hot/startup config -> `MemoryQueryBuilder.build()` (`memory_query_builder.py:294-363`) -> `MemoryRetrievalService.retrieve()` computes the larger collection limit (`memory_retrieval_service.py:320-326`) -> `_retrieve_queries()` creates each scoped query with `top_k=limit`, where `limit` is still injection `top_k` (`memory_retrieval_service.py:374-397`) -> canonical and hybrid sources each return at most that smaller `top_k` -> `MemoryV2Store.search()` truncates to `result_limit` before returning (`v2_store.py:889`).
- **Actual behavior:** The canonical store may scan `candidate_limit` rows internally, but it returns only injection `top_k`; the hybrid path also receives only injection `top_k`. With FAISS unavailable, fusion/intent reranking sees exactly `top_k` candidates, so the configured larger candidate pool cannot rescue a candidate filtered out by the store's pre-rerank ordering. Even with two sources, the pool can never reach the requested `3 * top_k` and is usually smaller after ID fusion.
- **Expected behavior:** `candidate_limit` should control the number of candidates returned from each retrieval stage into dedup/fusion/reranking, while `top_k` should be applied only after those stages for injection.
- **Production impact:** The newly exposed adaptive-top-k and intent-rerank features can report themselves enabled while operating on an already-truncated pool, reducing recall and making intent reranking ineffective for the cases it is intended to improve.
- **Why existing guards fail:** `collection_limit` only limits accumulation after `_retrieve_once()`; it does not enlarge the scoped query's `top_k`. `_resolve_search_limit()` broadens only the store's internal SQL scan and is followed by a truncation before the service receives candidates.
- **Classification:** confirmed
- **Confidence:** 0.97

### AMR-05 / P2 - Hybrid RRF relevance is fused on a scale far below all other score components

- **File:line:** `astrmai/memory/services/memory_retrieval_service.py:595`
- **Trigger:** FAISS and/or legacy BM25 returns hybrid candidates and the service fuses them with canonical candidates or ranks hybrid-only candidates.
- **Real call chain:** `MemoryRetrievalService._hybrid_search()` (`memory_retrieval_service.py:483`) -> `MemoryEngine.search_memories()` (`memory_engine.py:174`) -> `HybridRetriever.search()` (`hybrid_retriever.py:42`) -> `RRFFusion.fuse()` (`astrmai/memory/utils.py:56`) -> hybrid importance/time multiplication (`hybrid_retriever.py:79-103`) -> `_fuse_candidates()` applies `hybrid_weight` beside canonical/importance/confidence components (`memory_retrieval_service.py:589-612`).
- **Actual behavior:** With RRF constant 60, the best single-source rank contributes about `1/61 = 0.0164` and the best dual-source rank about `0.0328`. `HybridRetriever` multiplies that again by importance and decay; `_fuse_candidates()` then multiplies by `0.45`. For a typical importance `0.5`, the maximum dual-source hybrid contribution is about `0.0074`, while static importance plus confidence contributes about `0.115` and canonical relevance can contribute `0.25`. Semantic/sparse match quality is therefore numerically negligible at the final ranking layer.
- **Expected behavior:** Hybrid relevance should be normalized to the same scale as canonical relevance before weighted fusion, or fusion should use ranks consistently without applying weights intended for `[0,1]` scores.
- **Production impact:** Final ordering is dominated by stored importance/confidence/recency rather than query match. Strong semantic matches can lose to weak matches with higher static metadata, producing irrelevant injected memory even when FAISS succeeds.
- **Why existing guards fail:** RRF correctly orders its own top-k results, but the service performs a second numeric fusion without normalization. No score-range check or source-wise calibration exists before applying the configured-looking weights.
- **Classification:** confirmed
- **Confidence:** 0.96

### AMR-06 / P2 - Hot-applied temporal and hybrid-decay settings remain bound to old runtime objects

- **File:line:** `astrmai/memory/services/memory_engine.py:103`
- **Trigger:** The admin config path hot-applies a change to `memory.time_decay_rate` or any `memory.deep_temporal_*` value without restarting the plugin.
- **Real call chain:** Admin apply -> `PluginApiAdapter.apply_config()` (`astrmai/webui/backend/adapters/plugin_api.py:432-461`) -> `PluginFacade.apply_hot_config()` refreshes components (`astrmai/app/plugin_facade.py:185-218`) -> `MemoryEngine.refresh_config()` (`memory_engine.py:103-117`) -> later hybrid retrieval reads `HybridRetriever.config` captured at construction (`hybrid_retriever.py:17-21,82`) and deep retrieval reads `MemoryRetrievalService.scoring` captured at construction (`memory_retrieval_service.py:22-25`).
- **Actual behavior:** `MemoryEngine.refresh_config()` refreshes injection/query-builder config and handles embedding-model changes, but it does not replace `retrieval_service.scoring` and does not update the existing `retriever.config`. The API reports the hot apply as successful; these retrieval settings continue using pre-apply values until a restart. `time_decay_rate` changes only take effect incidentally if the embedding pool also changes and forces creation of a new `HybridRetriever`.
- **Expected behavior:** Every memory setting advertised through hot apply should affect the next retrieval, or the apply response should require a restart for those keys.
- **Production impact:** Operators cannot tune deep temporal ranking or hybrid decay live, and observed retrieval behavior contradicts the active configuration shown by the admin page. This can leave an incorrect decay/ranking policy in production indefinitely.
- **Why existing guards fail:** The hot-config dispatcher only checks for a `refresh_config()` method and treats a non-throwing call as success. The engine's refresh method updates selected children but has no propagation step for the two retrieval objects that cache derived configuration.
- **Classification:** confirmed
- **Confidence:** 0.99

## Reviewed production paths

- Automatic memory injection from planner/prompt refinement through query building, canonical/hybrid retrieval, final context selection, and the outer flexible prompt budget.
- Light and deep policies, think-level routing, visibility and layer filters, stale handling, exclusion of already injected IDs, persona-lore scope, jargon/expression specialized retrieval, and proactive recall callers.
- Canonical FTS and bounded fallback search, legacy BM25 direction, FAISS vector retrieval, RRF source merge, canonical hydration, deduplication, intent reranking, temporal reranking, LLM reranking/guidance, and trace persistence.
- Startup migration/projection flow, lazy FAISS/provider degradation, embedding-model hot changes, index rebuild/consistency repair, memory observer event flow, and shutdown of the memory pipeline.
- No additional confirmed reachable defect was found in embedding-model hot reset itself: it invalidates the vector runtime, forces a lazy rebuild, and leaves canonical retrieval available while vector initialization is unavailable.
