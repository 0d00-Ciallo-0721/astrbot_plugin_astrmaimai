# Group Trace Audit Summary

## Aggregate

- Total scenarios: 10
- Total turns: 411
- Passed turns: 411
- Failed turns: 0
- Worst WAIT_NEXT_NODE scenario: forced_compaction
- Earliest COOLDOWN scenario: post_compaction_recovery
- Most common block reason: recent_dense_bot_exchange
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
- Most unstable score bucket: tail_activity_score

## followup_chain - Mainline Follow-up Chain

The user keeps following up on the same mainline after a bot reply.

- Tags: base, tail-heavy
- Difficulty: base
- Total turns: 4
- Self-check passed turns: 4
- Self-check failed turns: 0
- States seen: NOT_READY
- State counts: {"NOT_READY": 4}
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
- Last reply preview: I am staying on the mainline: we are still talking about reply chain. For this turn, we can continue, but it is better to confirm against the same chain first.
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

## mainline_with_smalltalk - Mainline Washed by Smalltalk

A bot-directed mainline is followed by casual side chatter.

- Tags: base, smalltalk
- Difficulty: base
- Total turns: 5
- Self-check passed turns: 5
- Self-check failed turns: 0
- States seen: NOT_READY
- State counts: {"NOT_READY": 5}
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
- Last reply preview: I am staying on the mainline: we are still talking about compaction state machine. For this turn, I will continue from the earlier conclusion and avoid drifting into background ...
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

## parallel_topics - Parallel Topics

Several small threads run in parallel across the recent window.

- Tags: base, parallel
- Difficulty: base
- Total turns: 5
- Self-check passed turns: 5
- Self-check failed turns: 0
- States seen: NOT_READY
- State counts: {"NOT_READY": 5}
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
- Last reply preview: I am staying on the mainline: we are still talking about warm summary. For this turn, the key reason is that the earlier chain has not fully settled yet.
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

## forced_compaction - Forced Compaction at 120

A long chat reaches forced compaction and then waits for the earliest safe hook.

- Tags: base, forced, tail-heavy
- Difficulty: advanced
- Total turns: 104
- Self-check passed turns: 104
- Self-check failed turns: 0
- States seen: COOLDOWN, FORCED_PENDING, NOT_READY, WAIT_NEXT_NODE
- State counts: {"COOLDOWN": 2, "FORCED_PENDING": 13, "NOT_READY": 69, "WAIT_NEXT_NODE": 20}
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
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

## post_compaction_recovery - Post-compaction Recovery

The first two turns after a compaction should still keep the recent mainline clear.

- Tags: base, recovery
- Difficulty: base
- Total turns: 123
- Self-check passed turns: 123
- Self-check failed turns: 0
- States seen: COOLDOWN, NOT_READY, WAIT_NEXT_NODE
- State counts: {"COOLDOWN": 25, "NOT_READY": 78, "WAIT_NEXT_NODE": 20}
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
- Last reply preview: I am staying on the mainline: we are still talking about just now. For this turn, I will continue from the earlier conclusion and avoid drifting into background chatter.
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

## long_tail_drag - Long Tail Drag

A long bot-directed tail keeps the active chain alive for many rounds.

- Tags: tail-heavy, forced
- Difficulty: advanced
- Total turns: 35
- Self-check passed turns: 35
- Self-check failed turns: 0
- States seen: NOT_READY
- State counts: {"NOT_READY": 35}
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
- Last reply preview: I am staying on the mainline: we are still talking about delay compaction. For this turn, we can continue, but it is better to confirm against the same chain first.
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

## unsettled_topic_shift - Unsettled Topic Shift

A new topic begins before the old mainline has fully closed.

- Tags: closure, safe-window
- Difficulty: advanced
- Total turns: 4
- Self-check passed turns: 4
- Self-check failed turns: 0
- States seen: NOT_READY
- State counts: {"NOT_READY": 4}
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
- Last reply preview: I am staying on the mainline: we are still talking about previous chain. For this turn, I will continue from the earlier conclusion and avoid drifting into background chatter.
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

## parallel_multi_user_bot - Parallel Multi-user Bot Threads

Several users ask AstrMai different questions in overlapping reply branches.

- Tags: parallel, tail-heavy
- Difficulty: advanced
- Total turns: 4
- Self-check passed turns: 4
- Self-check failed turns: 0
- States seen: NOT_READY
- State counts: {"NOT_READY": 4}
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
- Last reply preview: I am staying on the mainline: we are still talking about topic density. For this turn, I will continue from the earlier conclusion and avoid drifting into background chatter.
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

## vision_mixed_context - Vision Mixed Context

An image-heavy message sequence still needs warm summary to preserve the mainline.

- Tags: vision, mixed
- Difficulty: advanced
- Total turns: 4
- Self-check passed turns: 4
- Self-check failed turns: 0
- States seen: NOT_READY
- State counts: {"NOT_READY": 4}
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
- Last reply preview: I am staying on the mainline: we are still talking about compaction mainline. For this turn, I will continue from the earlier conclusion and avoid drifting into background chatter.
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

## post_compaction_fast_followup - Post-compaction Fast Follow-up

A new bot-directed follow-up arrives immediately after compaction recovery starts.

- Tags: recovery, tail-heavy
- Difficulty: advanced
- Total turns: 123
- Self-check passed turns: 123
- Self-check failed turns: 0
- States seen: COOLDOWN, NOT_READY, WAIT_NEXT_NODE
- State counts: {"COOLDOWN": 25, "NOT_READY": 78, "WAIT_NEXT_NODE": 20}
- Failure kinds: {}
- Protocol passthrough: {}
- Vision failures: {}
- Last reply preview: I am staying on the mainline: we are still talking about continue immediately. For this turn, we can continue, but it is better to confirm against the same chain first.
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
