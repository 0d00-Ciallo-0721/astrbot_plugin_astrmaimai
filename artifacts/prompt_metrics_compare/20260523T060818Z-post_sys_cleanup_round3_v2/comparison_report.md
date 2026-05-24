# Prompt Metrics Before/After Report

- Generated At: `2026-05-23T06:08:32.768534+00:00`
- Baseline: `6d5ecde`
- Current: `post_sys_cleanup_round3_v2`
- Baseline Root: `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final__baseline_6d5ecde`
- Current Root: `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final`

## Trace Overview

- Baseline Sample Count: `10`
- Current Sample Count: `10`
- Status Mismatches: `0`

## Core Metrics

- System Prompt Length Mean: baseline `204.7` -> current `201.6` (delta `-3.1`)
- System Prompt Length Median: baseline `252.0` -> current `252.0` (delta `0.0`)
- Dynamic Prompt Length Mean: baseline `0.0` -> current `96.5` (delta `96.5`)
- Dynamic Prompt Length Median: baseline `0.0` -> current `98.0` (delta `98.0`)
- Stable Prefix Hash Pairwise Rate: baseline `0.8571` -> current `1.0` (delta `0.1429`)
- Stable Prefix Hash Dominant Rate: baseline `0.875` -> current `1.0` (delta `0.125`)

## Prefix Diagnostics

- Baseline Native Prefix Stable Rate: `None`
- Current Native Prefix Stable Rate: `0.0`
- Baseline Prefix Changed Reasons: `{'unsupported_in_baseline': 10}`
- Current Prefix Changed Reasons: `{'unavailable_in_trace': 10}`

## Remaining System Composition

- Baseline Frozen Prefix Blocks: `{}`
- Current Frozen Prefix Blocks: `{}`
- Baseline Semi-stable Blocks: `{}`
- Current Semi-stable Blocks: `{}`

## High-Dynamic Case Priority

_No high-dynamic migration targets identified._

## Benchmark Overview

- Baseline Avg Stable Prefix Length: `364.92`
- Current Avg Stable Prefix Length: `364.92`
- Baseline Avg Dynamic Payload Length: `111.92`
- Current Avg Dynamic Payload Length: `111.92`
- Benchmark Signal: `辅助无新增信号`

## Status Mismatches

_None._

## Limitations

- 旧基线版本没有原生落盘 continuity prompt 指标，因此 before 侧部分字段由同轮离线 replay 的最终 prompt 实测补采，而不是读取历史 artifact 字段。
- native_prefix_stable_rate 只对原生提供 prefix_stable 的版本有解释力；跨版本稳定性以 stable_prefix_hash 的派生统计为准。
- benchmark 汇总沿用 replay seed builder，只作为辅助参考，不作为主回复链 prompt 迁移收益的主证据。
