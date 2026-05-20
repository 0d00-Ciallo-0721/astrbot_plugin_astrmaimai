# Group Trace Audit Summary

## followup_chain - Mainline Follow-up Chain

The user keeps following up on the same mainline after a bot reply.

- Total turns: 4
- Self-check passed turns: 4
- Self-check failed turns: 0
- States seen: NOT_READY
- Last reply preview: Yes—waiting for the reply chain to cool down helps ensure the tail is stable and resource contention is minimized, allowing compaction to proceed without interference.
- Last fail reasons: (none)

## mainline_with_smalltalk - Mainline Washed by Smalltalk

A bot-directed mainline is followed by casual side chatter.

- Total turns: 5
- Self-check passed turns: 4
- Self-check failed turns: 1
- States seen: NOT_READY
- Last reply preview: Got it—focusing back on the compaction state machine: the key takeaway is that merging small segments optimizes for space efficiency and reduces write amplification, despite tem...
- Last fail reasons: (none)

## parallel_topics - Parallel Topics

Several small threads run in parallel across the recent window.

- Total turns: 5
- Self-check passed turns: 5
- Self-check failed turns: 0
- States seen: NOT_READY
- Last reply preview: A warm summary should avoid stitching rule lines because doing so risks fragmenting the emotional and contextual flow—prioritizing rigid structure over the natural, topic-driven...
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
- Last reply preview: Understood—I’m staying on the exact point just now: the compressed mainline is active and ready for continued audit discussion. No shift, no drift.
- Last fail reasons: (none)
