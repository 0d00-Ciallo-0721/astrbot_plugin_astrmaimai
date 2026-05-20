# Scheduler Profile Matrix Benchmark

- Label: `p6_2_live`
- Profiles: `dialogue_first, balanced, maintenance_friendly`
- Scenarios: `hot_dialogue_pressure, maintenance_backlog, busy_executor_pressure, retry_pressure_mix, forced_promotion_pressure`

## hot_dialogue_pressure

- Description: High WAITING/BUSY/ACTIVE pressure with small maintenance backlog.
- Batch Limit: `20`

| Profile | Selected | Dialogue | Maintenance | Forced Promotion | Fill Rate | Poll Mode | Busy Backpressure | Maintenance Backpressure |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| dialogue_first | 12 | 12 | 0 | 0 | 0.60 | FAST | false | false |
| balanced | 12 | 12 | 0 | 0 | 0.60 | FAST | false | false |
| maintenance_friendly | 12 | 12 | 0 | 0 | 0.60 | FAST | false | false |

## maintenance_backlog

- Description: Maintenance-heavy due set without dialogue pressure.
- Batch Limit: `20`

| Profile | Selected | Dialogue | Maintenance | Forced Promotion | Fill Rate | Poll Mode | Busy Backpressure | Maintenance Backpressure |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| dialogue_first | 14 | 4 | 10 | 0 | 0.70 | NORMAL | false | false |
| balanced | 14 | 4 | 10 | 0 | 0.70 | NORMAL | false | false |
| maintenance_friendly | 14 | 4 | 10 | 0 | 0.70 | NORMAL | false | false |

## busy_executor_pressure

- Description: Executor backlog is high enough to trigger busy backpressure.
- Batch Limit: `20`

| Profile | Selected | Dialogue | Maintenance | Forced Promotion | Fill Rate | Poll Mode | Busy Backpressure | Maintenance Backpressure |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| dialogue_first | 20 | 16 | 4 | 0 | 1.00 | FAST | true | false |
| balanced | 20 | 16 | 4 | 0 | 1.00 | FAST | true | false |
| maintenance_friendly | 20 | 14 | 6 | 0 | 1.00 | FAST | true | false |

## retry_pressure_mix

- Description: Due set mixes retry pressure, light dialogue work, and maintenance backlog.
- Batch Limit: `18`

| Profile | Selected | Dialogue | Maintenance | Forced Promotion | Fill Rate | Poll Mode | Busy Backpressure | Maintenance Backpressure |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| dialogue_first | 9 | 9 | 0 | 0 | 0.50 | FAST | false | false |
| balanced | 9 | 9 | 0 | 0 | 0.50 | FAST | false | false |
| maintenance_friendly | 9 | 9 | 0 | 0 | 0.50 | FAST | false | false |

## forced_promotion_pressure

- Description: Several chats are due for starvation protection and should surface differently by profile.
- Batch Limit: `8`

| Profile | Selected | Dialogue | Maintenance | Forced Promotion | Fill Rate | Poll Mode | Busy Backpressure | Maintenance Backpressure |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| dialogue_first | 4 | 4 | 0 | 0 | 0.50 | FAST | false | false |
| balanced | 7 | 4 | 3 | 3 | 0.88 | FAST | false | false |
| maintenance_friendly | 7 | 4 | 3 | 3 | 0.88 | FAST | false | false |

## Recommendations

- `hot_dialogue_pressure` [low] Increase dialogue_first fairness_penalty_multiplier or shrink maintenance slots for stronger contrast.
