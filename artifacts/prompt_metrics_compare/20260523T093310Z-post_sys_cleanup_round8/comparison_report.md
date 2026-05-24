# Prompt Metrics Before/After Report

- Generated At: `2026-05-23T09:33:24.804678+00:00`
- Baseline: `6d5ecde`
- Current: `post_sys_cleanup_round8`
- Baseline Root: `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final__baseline_6d5ecde`
- Current Root: `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final`

## Trace Overview

- Baseline Sample Count: `10`
- Current Sample Count: `10`
- Status Mismatches: `0`

## Core Metrics

- System Prompt Length Mean: baseline `204.7` -> current `233.9` (delta `29.2`)
- System Prompt Length Median: baseline `252.0` -> current `301.0` (delta `49.0`)
- Dynamic Prompt Length Mean: baseline `0.0` -> current `136.1` (delta `136.1`)
- Dynamic Prompt Length Median: baseline `0.0` -> current `143.0` (delta `143.0`)
- Stable Prefix Hash Pairwise Rate: baseline `0.8571` -> current `0.7143` (delta `-0.1428`)
- Stable Prefix Hash Dominant Rate: baseline `0.875` -> current `0.75` (delta `-0.125`)

## Prefix Diagnostics

- Baseline Native Prefix Stable Rate: `None`
- Current Native Prefix Stable Rate: `0.5`
- Baseline Prefix Changed Reasons: `{'unsupported_in_baseline': 10}`
- Current Prefix Changed Reasons: `{'first_seen': 2, 'cold_summary_changed': 1, 'unavailable_in_trace': 7}`
- Block Analysis Modes: baseline `{'fallback_parsed': 8, 'native_trace': 2}` | current `{'native_trace': 10}`

## Remaining System Composition

- Baseline Frozen Prefix Blocks: `{'system_rules': 2047, 'persona_or_identity': 2047}`
- Current Frozen Prefix Blocks: `{'persona_core': 189, 'style_block': 96, 'system_rules': 1592, 'stable_state': 0, 'stable_behavior_rules': 240, 'stable_private_chat': 0, 'cold_summary': 162, 'persona_or_identity': 1877}`
- Baseline Semi-stable Blocks: `{}`
- Current Semi-stable Blocks: `{'stable_expression': 0, 'stable_slang': 0, 'stable_jargon': 0}`
- Block Delta: `{'persona_or_identity': {'delta_mode': 'not_comparable'}, 'cold_summary': {'delta_mode': 'not_comparable'}, 'stable_behavior_rules': {'delta_mode': 'not_comparable'}, 'system_rules': {'baseline_mean_proxy': 2047, 'current_mean_proxy': 1592, 'delta_mean_proxy': -455, 'baseline_median_proxy': 2047, 'current_median_proxy': 1592, 'delta_median_proxy': -455, 'delta_mode': 'comparable'}}`
- Largest Block In Current Trace: `persona_or_identity`

## System Rules Breakdown

- Current System Rules Breakdown: `[{'key': 'no_role_prefix', 'total_length': 328, 'count': 8, 'default_target': 'keep_in_system'}, {'key': 'no_protocol_leak', 'total_length': 232, 'count': 8, 'default_target': 'keep_in_system'}, {'key': 'tool_use_without_protocol', 'total_length': 176, 'count': 8, 'default_target': 'candidate_for_runtime_instruction'}, {'key': 'current_message_first', 'total_length': 160, 'count': 8, 'default_target': 'candidate_for_runtime_instruction'}, {'key': 'short_action_narration', 'total_length': 160, 'count': 8, 'default_target': 'candidate_for_runtime_instruction'}, {'key': 'no_hard_fabrication', 'total_length': 152, 'count': 8, 'default_target': 'keep_in_system'}, {'key': 'visible_reply_only', 'total_length': 152, 'count': 8, 'default_target': 'keep_in_system'}, {'key': 'memory_is_background', 'total_length': 144, 'count': 8, 'default_target': 'keep_in_system'}]`
- System Rules Migration Candidates: `{'current_message_first': 8, 'short_action_narration': 8, 'tool_use_without_protocol': 8}`
- System Rules Keep Items: `{'visible_reply_only': 8, 'no_role_prefix': 8, 'no_protocol_leak': 8, 'no_hard_fabrication': 8, 'memory_is_background': 8}`
- Next Real Migration Candidate: `current_message_first`

## High-Dynamic Case Priority

- `pushback_strict` | dynamic `230` | system `49` | largest `persona_or_identity` | migratable `stable_behavior_rules` -> target `planner_runtime_instruction`
- `zh_boundary_mild` | dynamic `230` | system `49` | largest `persona_or_identity` | migratable `stable_behavior_rules` -> target `planner_runtime_instruction`
- `zh_memory_intent` | dynamic `230` | system `49` | largest `persona_or_identity` | migratable `stable_behavior_rules` -> target `planner_runtime_instruction`
- `zh_tool_intent` | dynamic `230` | system `49` | largest `persona_or_identity` | migratable `stable_behavior_rules` -> target `planner_runtime_instruction`
- `tool_intent` | dynamic `172` | system `49` | largest `persona_or_identity` | migratable `stable_behavior_rules` -> target `planner_runtime_instruction`
- `deep_memory` | dynamic `114` | system `49` | largest `persona_or_identity` | migratable `stable_behavior_rules` -> target `planner_runtime_instruction`
- Stable Global Candidate: `stable_behavior_rules`

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
- runtime_inflation_unresolved_due_to_baseline_gap
- system_rules inflation dominates replay delta
