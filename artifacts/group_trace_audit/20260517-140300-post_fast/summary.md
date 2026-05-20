# Group Trace Audit Summary

## Aggregate

- Total scenarios: 1
- Total turns: 123
- Passed turns: 123
- Failed turns: 0
- Worst WAIT_NEXT_NODE scenario: post_compaction_fast_followup
- Earliest COOLDOWN scenario: post_compaction_fast_followup
- Most common block reason: (none)
- Most unstable score bucket: stability_score

## post_compaction_fast_followup - Post-compaction Fast Follow-up

A new bot-directed follow-up arrives immediately after compaction recovery starts.

- Tags: recovery, tail-heavy
- Difficulty: advanced
- Total turns: 123
- Self-check passed turns: 123
- Self-check failed turns: 0
- States seen: COOLDOWN, NOT_READY, WAIT_NEXT_NODE
- State counts: {"COOLDOWN": 25, "NOT_READY": 78, "WAIT_NEXT_NODE": 20}
- Last reply preview: Yes, I can continue immediately without drifting—the mainline stays locked and responsive post-compaction.
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1
- First `WAIT_NEXT_NODE`: turn 79
- First `COOLDOWN`: turn 99

### Recovery Snapshot

- Turn 1: state=NOT_READY, recent_reason=warm_sufficient, reply=(empty)
- Turn 2: state=NOT_READY, recent_reason=warm_sufficient, reply=(empty)
- Turn 3: state=NOT_READY, recent_reason=warm_sufficient, reply=(empty)
- Turn 4: state=NOT_READY, recent_reason=warm_sufficient, reply=(empty)
