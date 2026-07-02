# AstrMai Test Coverage Report

> Auto-generated during green hardening spec execution.
> Last updated: 2026-06-28

## Overview

| Category | Files | Percentage |
|----------|:----:|:----------:|
| Unit tests | 20 | 48% |
| Integration tests | 3 | 7% |
| Regression tests | 19 | 45% |
| **Total** | **42** | |

Source files: ~887 `.py` files. Test-to-source ratio: ~4.7%.

## Unit Tests (20 files)

| Subdirectory | Files | Modules covered |
|---|---|---|
| `unit/conversation/` | 2 | attention, planning |
| `unit/learning/` | 3 | evolution, mining, review |
| `unit/memory/` | 5 | engine, retrieval, v2_store, contracts |
| `unit/runtime/` | 2 | event_bus, lane_manager |
| `unit/state/` | 6 | chat_state, energy, mood, profile, relationship |
| `unit/webui/` | 2 | plugin_pages, admin |

## Integration Tests (3 files) ⚠️

| File | Coverage |
|------|----------|
| `integration/gateway/test_gateway_context_passthrough_migrated.py` | Gateway context passthrough |
| `integration/host/test_host_mock_migrated.py` | Host mock |
| `integration/runtime/test_runtime_contracts_migrated.py` | Runtime contracts |

**Gap**: Only 3 integration test files — severely underweight. Priority areas:
- Chat loop → attention gate → reply engine end-to-end
- Sys3 router → subagent → tool execution
- Memory write → index projection → retrieval

## Regression Tests (19 files)

| Subdirectory | Files |
|---|---|
| `regression/suites/` | 3 phase suites (P0/P1/P2 minimal) |
| `regression/` (root) | 16 behavior guard files |

## Key Module Coverage

| Module | Unit | Integration | Regression | Status |
|--------|:---:|:-----------:|:----------:|:------:|
| Gateway | — | ✅ | ✅ | OK |
| AttentionGate | ✅ | — | ✅ | Needs integration |
| Planner/Sys2 | ✅ | — | ✅ | Needs integration |
| Sys3 Router | — | — | — | ❌ Uncovered |
| Memory Engine | ✅ | — | — | Needs integration |
| State Engine | ✅ | — | ✅ | OK |
| WebUI Backend | ✅ | — | — | Needs integration |
