# Group Trace Audit Summary

## Aggregate

- Total scenarios: 1
- Total turns: 104
- Passed turns: 104
- Failed turns: 0
- Worst WAIT_NEXT_NODE scenario: forced_compaction
- Earliest COOLDOWN scenario: forced_compaction
- Most common block reason: recent_dense_bot_exchange
- Most unstable score bucket: tail_activity_score

## forced_compaction - Forced Compaction at 120

A long chat reaches forced compaction and then waits for the earliest safe hook.

- Tags: base, forced, tail-heavy
- Difficulty: advanced
- Total turns: 104
- Self-check passed turns: 104
- Self-check failed turns: 0
- States seen: COOLDOWN, FORCED_PENDING, NOT_READY, WAIT_NEXT_NODE
- State counts: {"COOLDOWN": 2, "FORCED_PENDING": 13, "NOT_READY": 69, "WAIT_NEXT_NODE": 20}
- Last reply preview: (empty)
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1
- First `WAIT_NEXT_NODE`: turn 70
- First `FORCED_PENDING`: turn 90
- First `COOLDOWN`: turn 103

### Compression Trajectory

- Turn 90 [active_tail]: state=FORCED_PENDING, force_next_safe_hook=False, block=recent_dense_bot_exchange
- Turn 91 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=False, block=recent_dense_bot_exchange
- Turn 92 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=False, block=recent_dense_bot_exchange
- Turn 93 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=False, block=recent_dense_bot_exchange
- Turn 94 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=False, block=recent_dense_bot_exchange
- Turn 95 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=False, block=recent_dense_bot_exchange
- Turn 96 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=False, block=recent_dense_bot_exchange
- Turn 97 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=False, block=recent_dense_bot_exchange
- Turn 98 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=False, block=recent_dense_bot_exchange
- Turn 99 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=False, block=recent_dense_bot_exchange
- Turn 100 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=True, block=forced_waiting_for_safe_hook
- Turn 101 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=True, block=forced_waiting_for_safe_hook
- Turn 102 [forced_pending_window]: state=FORCED_PENDING, force_next_safe_hook=True, block=forced_waiting_for_safe_hook
- Turn 103 [natural_pause]: state=COOLDOWN, force_next_safe_hook=False, block=(none)
- Turn 104 [natural_pause]: state=COOLDOWN, force_next_safe_hook=False, block=(none)
