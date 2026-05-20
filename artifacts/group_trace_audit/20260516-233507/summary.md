# Group Trace Audit Summary

## followup_chain - Mainline Follow-up Chain

The user keeps following up on the same mainline after a bot reply.

- Total turns: 4
- Self-check passed turns: 4
- Self-check failed turns: 0
- States seen: NOT_READY
- Last reply preview: Yes—the rule should wait until the reply chain (i.e., the active focus tail overlap) cools down, because initiating compaction during unresolved overlap risks data inconsistency...
- Last fail reasons: (none)

## mainline_with_smalltalk - Mainline Washed by Smalltalk

A bot-directed mainline is followed by casual side chatter.

- Total turns: 5
- Self-check passed turns: 5
- Self-check failed turns: 0
- States seen: NOT_READY
- Last reply preview: Understood—focusing on the compaction state machine: the conclusion stands—merge adjacent segments to reduce write amplification, accept higher read latency, pending cost model ...
- Last fail reasons: (none)

## parallel_topics - Parallel Topics

Several small threads run in parallel across the recent window.

- Total turns: 5
- Self-check passed turns: 5
- Self-check failed turns: 0
- States seen: NOT_READY
- Last reply preview: Because stitching rule lines can fragment the narrative, introduce inconsistencies, and pull focus away from the core topic—undermining the very purpose of a warm summary: clari...
- Last fail reasons: (none)

## forced_compaction - Forced Compaction at 120

A long chat reaches forced compaction and then waits for the earliest safe hook.

- Total turns: 104
- Self-check passed turns: 104
- Self-check failed turns: 0
- States seen: COOLDOWN, FORCED_PENDING, NOT_READY, WAIT_NEXT_NODE
- Last reply preview: (empty)
- Last fail reasons: (none)

## post_compaction_recovery - Post-compaction Recovery

The first two turns after a compaction should still keep the recent mainline clear.

- Total turns: 123
- Self-check passed turns: 123
- Self-check failed turns: 0
- States seen: COOLDOWN, NOT_READY, WAIT_NEXT_NODE
- Last reply preview: Understood—staying on the compressed mainline. The audit thread is active and focused on the point just now. Proceeding.
- Last fail reasons: (none)
