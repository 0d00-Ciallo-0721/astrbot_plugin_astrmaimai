# Context Economy Benchmark Baseline

- Run ID: `20260518T023944Z-6d5ecde-kimi-replay-multi-round`
- Sample Count: `26`
- Sample Path: `artifacts\context_economy_replay_seed_samples_multi_round.jsonl`

## Overview

- Total Calls: `26`
- Input Tokens: `875`
- Cached Input Tokens: `189`
- Output Tokens: `418`
- Total Tokens: `1293`
- Cached Input Ratio: `21.6%`
- Session Reuse Rate: `19.2%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `23.1%`

## By Workload Family

### chat_dialog
- Calls: `9`
- Total Tokens: `300`
- Cached Input Ratio: `20.5%`
- Session Reuse Rate: `33.3%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### compaction_summary
- Calls: `3`
- Total Tokens: `156`
- Cached Input Ratio: `45.0%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `100.0%`

### dream_generation
- Calls: `3`
- Total Tokens: `150`
- Cached Input Ratio: `33.3%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### memory_global_summary
- Calls: `2`
- Total Tokens: `138`
- Cached Input Ratio: `28.1%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### memory_structured_extraction
- Calls: `2`
- Total Tokens: `93`
- Cached Input Ratio: `24.6%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### persona_summary
- Calls: `6`
- Total Tokens: `423`
- Cached Input Ratio: `6.2%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `50.0%`

### proactive_generation
- Calls: `1`
- Total Tokens: `33`
- Cached Input Ratio: `22.7%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

## By Template

### chat_dialog@v1
- Calls: `9`
- Total Tokens: `300`
- Cached Input Ratio: `20.5%`
- Session Reuse Rate: `33.3%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### compaction_summary_v2@v2
- Calls: `3`
- Total Tokens: `156`
- Cached Input Ratio: `45.0%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `3`

### dream_generation@v1
- Calls: `3`
- Total Tokens: `150`
- Cached Input Ratio: `33.3%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### memory_global_summary@v1
- Calls: `2`
- Total Tokens: `138`
- Cached Input Ratio: `28.1%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### memory_structured_extraction@v1
- Calls: `2`
- Total Tokens: `93`
- Cached Input Ratio: `24.6%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### persona_core_identity@v2
- Calls: `3`
- Total Tokens: `207`
- Cached Input Ratio: `12.6%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### persona_core_identity@v3
- Calls: `3`
- Total Tokens: `216`
- Cached Input Ratio: `0.0%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `3`

### proactive_wakeup_opening@v1
- Calls: `1`
- Total Tokens: `33`
- Cached Input Ratio: `22.7%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

## High Rotate Templates

- `compaction_summary_v2@v2` | calls `3` | rotate `3` | reuse `0.0%`
- `persona_core_identity@v3` | calls `3` | rotate `3` | reuse `0.0%`
- `dream_generation@v1` | calls `3` | rotate `0` | reuse `0.0%`
- `persona_core_identity@v2` | calls `3` | rotate `0` | reuse `0.0%`
- `memory_global_summary@v1` | calls `2` | rotate `0` | reuse `0.0%`
- `memory_structured_extraction@v1` | calls `2` | rotate `0` | reuse `0.0%`
- `proactive_wakeup_opening@v1` | calls `1` | rotate `0` | reuse `0.0%`
- `chat_dialog@v1` | calls `9` | rotate `0` | reuse `33.3%`

## Low Reuse Templates

- `compaction_summary_v2@v2` | calls `3` | rotate `3` | reuse `0.0%`
- `persona_core_identity@v3` | calls `3` | rotate `3` | reuse `0.0%`
- `dream_generation@v1` | calls `3` | rotate `0` | reuse `0.0%`
- `persona_core_identity@v2` | calls `3` | rotate `0` | reuse `0.0%`
- `memory_global_summary@v1` | calls `2` | rotate `0` | reuse `0.0%`
- `memory_structured_extraction@v1` | calls `2` | rotate `0` | reuse `0.0%`
- `proactive_wakeup_opening@v1` | calls `1` | rotate `0` | reuse `0.0%`
- `chat_dialog@v1` | calls `9` | rotate `0` | reuse `33.3%`

## High Traffic Templates

- `chat_dialog@v1` | calls `9` | rotate `0` | reuse `33.3%`
- `compaction_summary_v2@v2` | calls `3` | rotate `3` | reuse `0.0%`
- `persona_core_identity@v3` | calls `3` | rotate `3` | reuse `0.0%`
- `dream_generation@v1` | calls `3` | rotate `0` | reuse `0.0%`
- `persona_core_identity@v2` | calls `3` | rotate `0` | reuse `0.0%`
- `memory_global_summary@v1` | calls `2` | rotate `0` | reuse `0.0%`
- `memory_structured_extraction@v1` | calls `2` | rotate `0` | reuse `0.0%`
- `proactive_wakeup_opening@v1` | calls `1` | rotate `0` | reuse `0.0%`
