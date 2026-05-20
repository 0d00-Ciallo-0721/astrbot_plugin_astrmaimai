# Group Trace Audit Summary

## followup_chain - Mainline Follow-up Chain

The user keeps following up on the same mainline after a bot reply.

- Total turns: 4
- Self-check passed turns: 4
- Self-check failed turns: 0
- States seen: NOT_READY
- Last reply preview: Yes—waiting for the reply chain to cool down helps ensure the focus tail is stable and contention-free, allowing compaction to proceed safely without risking data inconsistency ...
- Last fail reasons: (none)

## mainline_with_smalltalk - Mainline Washed by Smalltalk

A bot-directed mainline is followed by casual side chatter.

- Total turns: 5
- Self-check passed turns: 4
- Self-check failed turns: 1
- States seen: NOT_READY
- Last reply preview: Got it—focusing on the compaction state machine: the key takeaway is that it merges small segments based on size thresholds, runs in the background, and prioritizes space effici...
- Last fail reasons: (none)

## parallel_topics - Parallel Topics

Several small threads run in parallel across the recent window.

- Total turns: 5
- Self-check passed turns: 4
- Self-check failed turns: 1
- States seen: NOT_READY
- Last reply preview: Because stitching rule lines can fragment the narrative, pulling focus away from the core topic and making the warm summary harder to follow—topic-driven summaries prioritize co...
- Last fail reasons: mainline_context_not_reflected

## forced_compaction - Forced Compaction at 120

A long chat reaches forced compaction and then waits for the earliest safe hook.

- Total turns: 102
- Self-check passed turns: 72
- Self-check failed turns: 30
- States seen: COOLDOWN, FORCED_PENDING, NOT_READY, WAIT_NEXT_NODE
- Last reply preview: (empty)
- Last fail reasons: (none)

## post_compaction_recovery - Post-compaction Recovery

The first two turns after a compaction should still keep the recent mainline clear.

- Total turns: 123
- Self-check passed turns: 123
- Self-check failed turns: 0
- States seen: COOLDOWN, NOT_READY, WAIT_NEXT_NODE
- Last reply preview: Understood—staying on the compressed mainline. The audit thread is active and ready for your next input.
- Last fail reasons: (none)
