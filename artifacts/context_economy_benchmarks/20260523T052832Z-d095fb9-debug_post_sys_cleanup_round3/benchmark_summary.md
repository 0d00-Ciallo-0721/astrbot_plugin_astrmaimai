# Context Economy Benchmark Baseline

- Run ID: `20260523T052832Z-d095fb9-debug_post_sys_cleanup_round3`
- Sample Count: `41`
- Sample Path: `artifacts\context_economy_replay_seed_samples_post_sys_cleanup_round3.jsonl`

## Overview

- Total Calls: `41`
- Input Tokens: `1382`
- Cached Input Tokens: `396`
- Output Tokens: `648`
- Total Tokens: `2030`
- Cached Input Ratio: `28.6%`
- Session Reuse Rate: `80.5%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

## By Workload Family

### chat_dialog
- Calls: `11`
- Total Tokens: `372`
- Cached Input Ratio: `19.1%`
- Session Reuse Rate: `63.6%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### compaction_summary
- Calls: `8`
- Total Tokens: `404`
- Cached Input Ratio: `46.2%`
- Session Reuse Rate: `87.5%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### dream_generation
- Calls: `8`
- Total Tokens: `388`
- Cached Input Ratio: `33.9%`
- Session Reuse Rate: `87.5%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### memory_global_summary
- Calls: `4`
- Total Tokens: `270`
- Cached Input Ratio: `29.8%`
- Session Reuse Rate: `75.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### memory_structured_extraction
- Calls: `4`
- Total Tokens: `186`
- Cached Input Ratio: `25.0%`
- Session Reuse Rate: `75.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### persona_summary
- Calls: `6`
- Total Tokens: `410`
- Cached Input Ratio: `13.8%`
- Session Reuse Rate: `83.3%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

## By Template

### chat_dialog@v1
- Calls: `11`
- Total Tokens: `372`
- Cached Input Ratio: `19.1%`
- Session Reuse Rate: `63.6%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### compaction_summary_v2@v2
- Calls: `8`
- Total Tokens: `404`
- Cached Input Ratio: `46.2%`
- Session Reuse Rate: `87.5%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### dream_generation@v1
- Calls: `8`
- Total Tokens: `388`
- Cached Input Ratio: `33.9%`
- Session Reuse Rate: `87.5%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### memory_global_summary@v1
- Calls: `4`
- Total Tokens: `270`
- Cached Input Ratio: `29.8%`
- Session Reuse Rate: `75.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### memory_structured_extraction@v1
- Calls: `4`
- Total Tokens: `186`
- Cached Input Ratio: `25.0%`
- Session Reuse Rate: `75.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### persona_core_identity@v3
- Calls: `6`
- Total Tokens: `410`
- Cached Input Ratio: `13.8%`
- Session Reuse Rate: `83.3%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

## High Rotate Templates

- `chat_dialog@v1` | calls `11` | rotate `0` | reuse `63.6%`
- `memory_global_summary@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `memory_structured_extraction@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `persona_core_identity@v3` | calls `6` | rotate `0` | reuse `83.3%`
- `compaction_summary_v2@v2` | calls `8` | rotate `0` | reuse `87.5%`
- `dream_generation@v1` | calls `8` | rotate `0` | reuse `87.5%`

## Low Reuse Templates

- `chat_dialog@v1` | calls `11` | rotate `0` | reuse `63.6%`
- `memory_global_summary@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `memory_structured_extraction@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `persona_core_identity@v3` | calls `6` | rotate `0` | reuse `83.3%`
- `compaction_summary_v2@v2` | calls `8` | rotate `0` | reuse `87.5%`
- `dream_generation@v1` | calls `8` | rotate `0` | reuse `87.5%`

## High Traffic Templates

- `chat_dialog@v1` | calls `11` | rotate `0` | reuse `63.6%`
- `compaction_summary_v2@v2` | calls `8` | rotate `0` | reuse `87.5%`
- `dream_generation@v1` | calls `8` | rotate `0` | reuse `87.5%`
- `persona_core_identity@v3` | calls `6` | rotate `0` | reuse `83.3%`
- `memory_global_summary@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `memory_structured_extraction@v1` | calls `4` | rotate `0` | reuse `75.0%`
