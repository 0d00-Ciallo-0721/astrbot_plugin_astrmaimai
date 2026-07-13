# Final Functional Audit Dispatch Plan

## Current Dispatch State

- The initial parallel dispatch was started on 2026-07-13.
- The sub-agent concurrency limit was reached during the bulk spawn call.
- The bulk call did not return the IDs of agents that were successfully accepted
  before the limit was reached.
- Do not assume a domain is complete until its report file exists and contains a
  completed audit.
- On resume, list this directory, keep completed reports, and dispatch only the
  missing report files in small tracked batches.

## Global Rules

- Review the current working tree, including uncommitted production changes.
- Read the global `astrbot-plugin-dev/SKILL.md` first.
- Review functional correctness, runtime stability, and module cooperation.
- Do not inspect tests or discuss test coverage.
- Do not review security policy, authentication, authorization, or permission
  hardening.
- Exclude `astrmai/infrastructure/security/` and treat it as an opaque dependency.
- Do not modify production code. Each agent may write only its assigned report.
- Do not report style, naming, duplication, dead code, or refactoring suggestions.
- Findings require a reachable production call chain and exact file/line evidence.

## Assignments

### 01 Entry, App, and Config

Output: `01_entry_app_config.md`

Scope: `main.py`, `config.py`, `_conf_schema.json`, `metadata.yaml`,
`requirements.txt`, and `astrmai/app/`.

Review AstrBot hook delegation, bootstrap wiring, optional degradation, lifecycle
start/stop, task cleanup, Facade/runtime protocols, config/schema agreement, hot
config propagation, idempotence, rollback, and compatibility exports.

### 02 Conversation Ingress and Attention

Output: `02_conversation_ingress_attention.md`

Scope: `astrmai/conversation/ingress/`, `attention/`, `contracts/`,
`concurrency/`, and `threading/`.

Review event normalization, commands, poke/direct calls, dedupe, scope,
TurnIdentity, group-thread resolution, focus selection, group/private separation,
generation ownership, flags, stale work, and cross-thread leakage.

### 03 Conversation Decision and Planning

Output: `03_conversation_decision_planning.md`

Scope: `astrmai/conversation/decision/` and `planning/`.

Review Judge to ActionPlan/Planner contracts, prompt/context shapes, focus and
history separation, goals, behavior policies, model fallbacks, tool results,
agency/cognitive-loop state, and None/type/error paths.

### 04 Conversation Execution and Presentation

Output: `04_conversation_execution_presentation.md`

Scope: `astrmai/conversation/execution/`, `conversation/loop/`, and
`astrmai/presentation/`.

Review Planner to ReplyArtifact contracts, final-send claim, stale suppression,
cancellation, duplicate final/follow-up sends, segmentation, history recording,
event stopping, private chat behavior, external results, and error fallbacks.

### 05 Memory Retrieval and RAG

Output: `05_memory_retrieval_rag.md`

Scope: `astrmai/memory/retrieval/`, `memory/contracts/`, and retrieval-facing
services: query builder, retrieval service, injection service, context builder,
v2 store, memory engine, scoring, and observer.

Review query layering and flags, FTS/BM25 direction, FAISS/embedding fallback,
candidate limits, top-k, fusion, dedup, rerank, score scale, injection budget,
trace runtime behavior, hot embedding config, and degradation.

### 06 Memory Write and Governance

Output: `06_memory_write_governance.md`

Scope: remaining `astrmai/memory/services/`, `memory/dream/`, `memory/persona/`,
and `memory/utils.py`, excluding report 05 files.

Review turn ingestion, instant gate, writes, canonical IDs, v2 upsert/indexing,
partial writes, migration, maintenance, summaries, expression patterns, tools,
Persona, Dream actions, batches, retries, and background lifecycle.

### 07 Gateway and Context Economy

Output: `07_gateway_context_economy.md`

Scope: `astrmai/infrastructure/gateway/` and `context_economy/`.

Review provider/model/lane selection, retries, timeouts, cooldowns, accounting,
result construction, usage/trace side effects, task/vision/judge/mood calls,
tool loops, prompt-cache capabilities, budgets, and fallback availability.

### 08 Runtime, Persistence, and Shared

Output: `08_runtime_persistence_shared.md`

Scope: `astrmai/infrastructure/runtime/`, `persistence/`, `compat/`, and
`astrmai/shared/`.

Review SQLite and repository/service contracts, transactions, schema startup,
session lifecycle, lanes, event bus, runtime coordinator, generation/cancellation,
trace stores, task shutdown, compatibility synchronization, filesystem paths, and
shared production contracts.

### 09 State and Sessions

Output: `09_state_sessions.md`

Scope: `astrmai/state/`.

Review group wait, private chat state, group/private isolation, mood, energy,
relationship, profiles, clocks, locks, cleanup, invalid values, persistence, and
conversation/proactive integration.

### 10 Learning and Proactive

Output: `10_learning_proactive.md`

Scope: `astrmai/learning/` and `astrmai/proactive/`.

Review logging to mining/profiling/review/governance, queue and batch retries,
partial persistence, service lifecycle, schedulers, wakeup, decay, diary, Dream,
review dispatch, Heartflow, task isolation, timing, config refresh, and duplicate
or delayed proactive replies.

### 11 Multimodal and Workmode

Output: `11_multimodal_workmode.md`

Scope: `astrmai/multimodal/` and `astrmai/workmode/`.

Review image decode/transform fallback, VisualCortex lifecycle, meme sending,
Sys3 router/subagent/tool contracts, providers, cron recovery, cancellation,
timeouts, optional tools, and return to normal chat. Exclude security policy.

### 12 WebUI and Plugin Pages

Output: `12_webui_plugin_pages.md`

Scope: `astrmai/webui/` and `pages/admin/`, excluding `astrmai/webui/venv` and
runtime/generated data.

Review Plugin Pages registration, bridge/API paths, route-service-repository and
runtime contracts, serialization, pagination, filters/counts, diagnostics
degradation, hot config propagation, views, static paths, and reachable page or
runtime failures. Exclude authentication and authorization.

## Resume Procedure

1. List files in `.agent/final-functional-audit/`.
2. Validate that each existing numbered report has a completion summary.
3. Spawn missing assignments in tracked batches and retain every returned agent ID.
4. Wait for each tracked agent, verify its report exists, then close that agent.
5. After reports 01-12 exist, spawn the cross-module reviewer for
   `13_cross_module_integration.md`.
6. Consolidate confirmed findings into `FINAL_REPORT.md` without changing source.
