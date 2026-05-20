# Group Trace Audit Summary

## followup_chain - Mainline Follow-up Chain

The user keeps following up on the same mainline after a bot reply.

- Total turns: 4
- Self-check passed turns: 4
- Self-check failed turns: 0
- States seen: NOT_READY
- Last reply preview: Yes, the rule should wait until the reply chain cools down—this ensures the focus tail fully settles and avoids premature compaction during active synchronization.
- Last fail reasons: (none)

## mainline_with_smalltalk - Mainline Washed by Smalltalk

A bot-directed mainline is followed by casual side chatter.

- Total turns: 5
- Self-check passed turns: 4
- Self-check failed turns: 1
- States seen: NOT_READY
- Last reply preview: Got it—focusing on the compaction state machine: the key takeaway is that merging small segments optimizes space, with transitions driven by size thresholds and I/O pressure, en...
- Last fail reasons: (none)

## parallel_topics - Parallel Topics

Several small threads run in parallel across the recent window.

- Total turns: 5
- Self-check passed turns: 4
- Self-check failed turns: 1
- States seen: NOT_READY
- Last reply preview: Because stitching rule lines can fragment the narrative, dilute focus, and pull the warm summary away from the core topic—undermining its purpose of maintaining a clear, continu...
- Last fail reasons: mainline_context_not_reflected

## forced_compaction - Forced Compaction at 120

A long chat reaches forced compaction and then waits for the earliest safe hook.

- Total turns: 137
- Self-check passed turns: 137
- Self-check failed turns: 0
- States seen: COOLDOWN, NOT_READY, WAIT_NEXT_NODE
- Last reply preview: (empty)
- Last fail reasons: (none)

## post_compaction_recovery - Post-compaction Recovery

The first two turns after a compaction should still keep the recent mainline clear.

- Total turns: 123
- Self-check passed turns: 123
- Self-check failed turns: 0
- States seen: COOLDOWN, NOT_READY, WAIT_NEXT_NODE
- Last reply preview: Understood—staying on the compressed mainline: the audit thread is intact and active. Proceeding with the current point.
- Last fail reasons: (none)
