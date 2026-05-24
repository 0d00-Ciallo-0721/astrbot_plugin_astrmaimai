# Prompt Metrics Before/After Report

- Generated At: `2026-05-23T16:00:41.024159+00:00`
- Baseline: `6d5ecde`
- Current: `semantic_system_hash_check`
- Baseline Root: `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final`
- Current Root: `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final`

## Trace Overview

- Baseline Sample Count: `10`
- Current Sample Count: `10`
- Status Mismatches: `0`

## Core Metrics

- System Prompt Length Mean: baseline `190.9` -> current `190.9` (delta `0.0`)
- System Prompt Length Median: baseline `240.0` -> current `240.0` (delta `0.0`)
- Dynamic Prompt Length Mean: baseline `186.1` -> current `186.1` (delta `0.0`)
- Dynamic Prompt Length Median: baseline `212.0` -> current `212.0` (delta `0.0`)
- Stable Prefix Hash Pairwise Rate: baseline `0.8571` -> current `0.8571` (delta `0.0`)
- Stable Prefix Hash Dominant Rate: baseline `0.875` -> current `0.875` (delta `0.0`)
- Semantic System Hash Pairwise Rate: baseline `0.8571` -> current `0.8571` (delta `0.0`)

## Prefix Diagnostics

- Baseline Native Prefix Stable Rate: `0.5`
- Current Native Prefix Stable Rate: `0.5`
- Baseline Prefix Changed Reasons: `{'first_seen': 2, 'frozen_rules_or_persona_changed': 1, 'unavailable_in_trace': 7}`
- Current Prefix Changed Reasons: `{'first_seen': 2, 'frozen_rules_or_persona_changed': 1, 'unavailable_in_trace': 7}`
- Block Analysis Modes: baseline `{'native_trace': 10}` | current `{'native_trace': 10}`
- Hook Changed System Case Ids: `[]`

## Semantic System Diagnostics

- Baseline Semantic System Hash Stats: `{'unique_count': 2, 'dominant_hash_rate': 0.875, 'pairwise_stability_rate': 0.8571, 'counts': {'ca19d720fe338e65c0da638939b8df17': 1, 'ff4af047ac839146964e924ff7ea94e7': 7}}`
- Current Semantic System Hash Stats: `{'unique_count': 2, 'dominant_hash_rate': 0.875, 'pairwise_stability_rate': 0.8571, 'counts': {'ca19d720fe338e65c0da638939b8df17': 1, 'ff4af047ac839146964e924ff7ea94e7': 7}}`

## Live Replay Evidence

- Cache Hit Rate: ``
- Usage Reporting Supported: ``
- Hash Stable But Cache Miss Case Ids: `[]`

## Remaining System Composition

- Baseline Frozen Prefix Blocks: `{'persona_core': 189, 'style_block': 96, 'system_rules': 1592, 'persona_or_identity': 1877}`
- Current Frozen Prefix Blocks: `{'persona_core': 189, 'style_block': 96, 'system_rules': 1592, 'persona_or_identity': 1877}`
- Baseline Soft Background Blocks: `{'cold_summary': 162, 'stable_state': 0, 'stable_behavior_rules': 0, 'stable_private_chat': 0, 'stable_expression': 0, 'stable_slang': 0, 'stable_jargon': 0}`
- Current Soft Background Blocks: `{'cold_summary': 162, 'stable_state': 0, 'stable_behavior_rules': 0, 'stable_private_chat': 0, 'stable_expression': 0, 'stable_slang': 0, 'stable_jargon': 0}`
- Block Delta: `{'persona_or_identity': {'baseline_mean_proxy': 1877, 'current_mean_proxy': 1877, 'delta_mean_proxy': 0, 'baseline_median_proxy': 1877, 'current_median_proxy': 1877, 'delta_median_proxy': 0, 'delta_mode': 'comparable'}, 'cold_summary': {'baseline_mean_proxy': 162, 'current_mean_proxy': 162, 'delta_mean_proxy': 0, 'baseline_median_proxy': 162, 'current_median_proxy': 162, 'delta_median_proxy': 0, 'delta_mode': 'comparable'}, 'stable_behavior_rules': {'delta_mode': 'not_comparable'}, 'system_rules': {'baseline_mean_proxy': 1592, 'current_mean_proxy': 1592, 'delta_mean_proxy': 0, 'baseline_median_proxy': 1592, 'current_median_proxy': 1592, 'delta_median_proxy': 0, 'delta_mode': 'comparable'}}`
- Largest Block In Current Trace: `persona_or_identity`

## System Rules Breakdown

- Current System Rules Breakdown: `[{'key': 'no_role_prefix', 'total_length': 328, 'count': 8, 'default_target': 'keep_in_system'}, {'key': 'no_protocol_leak', 'total_length': 232, 'count': 8, 'default_target': 'keep_in_system'}, {'key': 'tool_use_without_protocol', 'total_length': 176, 'count': 8, 'default_target': 'candidate_for_runtime_instruction'}, {'key': 'current_message_first', 'total_length': 160, 'count': 8, 'default_target': 'candidate_for_runtime_instruction'}, {'key': 'short_action_narration', 'total_length': 160, 'count': 8, 'default_target': 'candidate_for_runtime_instruction'}, {'key': 'no_hard_fabrication', 'total_length': 152, 'count': 8, 'default_target': 'keep_in_system'}, {'key': 'visible_reply_only', 'total_length': 152, 'count': 8, 'default_target': 'keep_in_system'}, {'key': 'memory_is_background', 'total_length': 144, 'count': 8, 'default_target': 'keep_in_system'}]`
- System Rules Migration Candidates: `{'current_message_first': 8, 'short_action_narration': 8, 'tool_use_without_protocol': 8}`
- System Rules Keep Items: `{'visible_reply_only': 8, 'no_role_prefix': 8, 'no_protocol_leak': 8, 'no_hard_fabrication': 8, 'memory_is_background': 8}`
- Next Real Migration Candidate: `current_message_first`
- Runtime Prompt Layer: `planner_runtime_instruction` is treated as dynamic prompt control text, not remaining system content.

## High-Dynamic Case Priority

_No high-dynamic migration targets identified._
- Stable Global Candidate: `no stable global migration candidate yet`

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
