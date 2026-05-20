# Context Economy Benchmark Baseline

- Run ID: `20260518T030414Z-6d5ecde-kimi-replay-dream-memory-affinity-rerun`
- Sample Count: `40`
- Sample Path: `artifacts\context_economy_replay_seed_samples_multi_round.jsonl`

## Overview

- Total Calls: `40`
- Input Tokens: `1358`
- Cached Input Tokens: `395`
- Output Tokens: `633`
- Total Tokens: `1991`
- Cached Input Ratio: `29.1%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

## By Workload Family

### chat_dialog
- Calls: `9`
- Total Tokens: `300`
- Cached Input Ratio: `20.5%`
- Session Reuse Rate: `33.3%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### compaction_summary
- Calls: `8`
- Total Tokens: `404`
- Cached Input Ratio: `46.2%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### dream_generation
- Calls: `8`
- Total Tokens: `388`
- Cached Input Ratio: `33.9%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### memory_global_summary
- Calls: `4`
- Total Tokens: `270`
- Cached Input Ratio: `29.8%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### memory_structured_extraction
- Calls: `4`
- Total Tokens: `186`
- Cached Input Ratio: `25.0%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### persona_summary
- Calls: `6`
- Total Tokens: `410`
- Cached Input Ratio: `13.8%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

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
- Calls: `8`
- Total Tokens: `404`
- Cached Input Ratio: `46.2%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### dream_generation@v1
- Calls: `8`
- Total Tokens: `388`
- Cached Input Ratio: `33.9%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### memory_global_summary@v1
- Calls: `4`
- Total Tokens: `270`
- Cached Input Ratio: `29.8%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### memory_structured_extraction@v1
- Calls: `4`
- Total Tokens: `186`
- Cached Input Ratio: `25.0%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### persona_core_identity@v3
- Calls: `6`
- Total Tokens: `410`
- Cached Input Ratio: `13.8%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### proactive_wakeup_opening@v1
- Calls: `1`
- Total Tokens: `33`
- Cached Input Ratio: `22.7%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

## High Rotate Templates

- `proactive_wakeup_opening@v1` | calls `1` | rotate `0` | reuse `0.0%`
- `chat_dialog@v1` | calls `9` | rotate `0` | reuse `33.3%`
- `compaction_summary_v2@v2` | calls `8` | rotate `0` | reuse `50.0%`
- `dream_generation@v1` | calls `8` | rotate `0` | reuse `50.0%`
- `persona_core_identity@v3` | calls `6` | rotate `0` | reuse `50.0%`
- `memory_global_summary@v1` | calls `4` | rotate `0` | reuse `50.0%`
- `memory_structured_extraction@v1` | calls `4` | rotate `0` | reuse `50.0%`

## Low Reuse Templates

- `proactive_wakeup_opening@v1` | calls `1` | rotate `0` | reuse `0.0%`
- `chat_dialog@v1` | calls `9` | rotate `0` | reuse `33.3%`
- `compaction_summary_v2@v2` | calls `8` | rotate `0` | reuse `50.0%`
- `dream_generation@v1` | calls `8` | rotate `0` | reuse `50.0%`
- `persona_core_identity@v3` | calls `6` | rotate `0` | reuse `50.0%`
- `memory_global_summary@v1` | calls `4` | rotate `0` | reuse `50.0%`
- `memory_structured_extraction@v1` | calls `4` | rotate `0` | reuse `50.0%`

## High Traffic Templates

- `chat_dialog@v1` | calls `9` | rotate `0` | reuse `33.3%`
- `compaction_summary_v2@v2` | calls `8` | rotate `0` | reuse `50.0%`
- `dream_generation@v1` | calls `8` | rotate `0` | reuse `50.0%`
- `persona_core_identity@v3` | calls `6` | rotate `0` | reuse `50.0%`
- `memory_global_summary@v1` | calls `4` | rotate `0` | reuse `50.0%`
- `memory_structured_extraction@v1` | calls `4` | rotate `0` | reuse `50.0%`
- `proactive_wakeup_opening@v1` | calls `1` | rotate `0` | reuse `0.0%`
