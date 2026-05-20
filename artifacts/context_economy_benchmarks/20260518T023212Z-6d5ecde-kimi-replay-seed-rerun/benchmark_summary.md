# Context Economy Benchmark Baseline

- Run ID: `20260518T023212Z-6d5ecde-kimi-replay-seed-rerun`
- Sample Count: `6`
- Sample Path: `artifacts\context_economy_replay_seed_samples.jsonl`

## Overview

- Total Calls: `6`
- Input Tokens: `185`
- Cached Input Tokens: `68`
- Output Tokens: `92`
- Total Tokens: `277`
- Cached Input Ratio: `36.8%`
- Session Reuse Rate: `33.3%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `33.3%`

## By Workload Family

### chat_dialog
- Calls: `2`
- Total Tokens: `45`
- Cached Input Ratio: `31.0%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `50.0%`

### memory_global_summary
- Calls: `2`
- Total Tokens: `102`
- Cached Input Ratio: `50.0%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `0.0%`

### persona_summary
- Calls: `2`
- Total Tokens: `130`
- Cached Input Ratio: `28.4%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Rate: `50.0%`

## By Template

### chat_dialog@v1
- Calls: `2`
- Total Tokens: `45`
- Cached Input Ratio: `31.0%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `1`

### memory_global_summary@v1
- Calls: `2`
- Total Tokens: `102`
- Cached Input Ratio: `50.0%`
- Session Reuse Rate: `50.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### persona_core_identity@v2
- Calls: `1`
- Total Tokens: `63`
- Cached Input Ratio: `32.6%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `0`

### persona_core_identity@v3
- Calls: `1`
- Total Tokens: `67`
- Cached Input Ratio: `24.4%`
- Session Reuse Rate: `0.0%`
- Primary Hit Rate: `100.0%`
- Lane Rotate Count: `1`

## High Rotate Templates

- `persona_core_identity@v3` | calls `1` | rotate `1` | reuse `0.0%`
- `chat_dialog@v1` | calls `2` | rotate `1` | reuse `50.0%`
- `persona_core_identity@v2` | calls `1` | rotate `0` | reuse `0.0%`
- `memory_global_summary@v1` | calls `2` | rotate `0` | reuse `50.0%`

## Low Reuse Templates

- `persona_core_identity@v3` | calls `1` | rotate `1` | reuse `0.0%`
- `persona_core_identity@v2` | calls `1` | rotate `0` | reuse `0.0%`
- `chat_dialog@v1` | calls `2` | rotate `1` | reuse `50.0%`
- `memory_global_summary@v1` | calls `2` | rotate `0` | reuse `50.0%`

## High Traffic Templates

- `chat_dialog@v1` | calls `2` | rotate `1` | reuse `50.0%`
- `memory_global_summary@v1` | calls `2` | rotate `0` | reuse `50.0%`
- `persona_core_identity@v3` | calls `1` | rotate `1` | reuse `0.0%`
- `persona_core_identity@v2` | calls `1` | rotate `0` | reuse `0.0%`
