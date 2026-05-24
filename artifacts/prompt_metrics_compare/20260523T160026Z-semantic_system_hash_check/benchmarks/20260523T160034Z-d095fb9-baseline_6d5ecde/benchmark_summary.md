# Context Economy Benchmark Baseline

- Run ID: `20260523T160034Z-d095fb9-baseline_6d5ecde`
- Sample Count: `26`
- Sample Path: `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final\artifacts\prompt_metrics_compare\20260523T160026Z-semantic_system_hash_check\baseline_6d5ecde_context_economy_samples.jsonl`

## Overview

- Total Calls: `26`
- Input Tokens: `733`
- Cached Input Tokens: `227`
- Output Tokens: `357`
- Total Tokens: `1090`
- Cached Input Ratio: `31.0%`
- Session Reuse Rate: `76.9%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

## By Workload Family

### chat_dialog
- Calls: `10`
- Total Tokens: `324`
- Cached Input Ratio: `21.8%`
- Session Reuse Rate: `80.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### compaction_summary
- Calls: `2`
- Total Tokens: `101`
- Cached Input Ratio: `46.2%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### dream_generation
- Calls: `2`
- Total Tokens: `97`
- Cached Input Ratio: `33.9%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### memory_global_summary
- Calls: `4`
- Total Tokens: `226`
- Cached Input Ratio: `43.6%`
- Session Reuse Rate: `75.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### memory_structured_extraction
- Calls: `4`
- Total Tokens: `156`
- Cached Input Ratio: `30.2%`
- Session Reuse Rate: `75.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### persona_summary
- Calls: `4`
- Total Tokens: `186`
- Cached Input Ratio: `20.3%`
- Session Reuse Rate: `75.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

## By Template

### chat_dialog@v1
- Calls: `10`
- Total Tokens: `324`
- Cached Input Ratio: `21.8%`
- Session Reuse Rate: `80.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### compaction_summary_v2@v2
- Calls: `2`
- Total Tokens: `101`
- Cached Input Ratio: `46.2%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### dream_generation@v1
- Calls: `2`
- Total Tokens: `97`
- Cached Input Ratio: `33.9%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### memory_global_summary@v1
- Calls: `4`
- Total Tokens: `226`
- Cached Input Ratio: `43.6%`
- Session Reuse Rate: `75.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### memory_structured_extraction@v1
- Calls: `4`
- Total Tokens: `156`
- Cached Input Ratio: `30.2%`
- Session Reuse Rate: `75.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### persona_core_identity@v3
- Calls: `4`
- Total Tokens: `186`
- Cached Input Ratio: `20.3%`
- Session Reuse Rate: `75.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

## High Rotate Templates

- `compaction_summary_v2@v2` | calls `2` | rotate `0` | reuse `50.0%`
- `dream_generation@v1` | calls `2` | rotate `0` | reuse `50.0%`
- `memory_global_summary@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `memory_structured_extraction@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `persona_core_identity@v3` | calls `4` | rotate `0` | reuse `75.0%`
- `chat_dialog@v1` | calls `10` | rotate `0` | reuse `80.0%`

## Low Reuse Templates

- `compaction_summary_v2@v2` | calls `2` | rotate `0` | reuse `50.0%`
- `dream_generation@v1` | calls `2` | rotate `0` | reuse `50.0%`
- `memory_global_summary@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `memory_structured_extraction@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `persona_core_identity@v3` | calls `4` | rotate `0` | reuse `75.0%`
- `chat_dialog@v1` | calls `10` | rotate `0` | reuse `80.0%`

## High Traffic Templates

- `chat_dialog@v1` | calls `10` | rotate `0` | reuse `80.0%`
- `memory_global_summary@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `memory_structured_extraction@v1` | calls `4` | rotate `0` | reuse `75.0%`
- `persona_core_identity@v3` | calls `4` | rotate `0` | reuse `75.0%`
- `compaction_summary_v2@v2` | calls `2` | rotate `0` | reuse `50.0%`
- `dream_generation@v1` | calls `2` | rotate `0` | reuse `50.0%`
