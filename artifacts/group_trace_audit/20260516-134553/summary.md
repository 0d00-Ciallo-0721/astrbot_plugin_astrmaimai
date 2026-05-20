# Group Trace Audit Summary

## followup_chain - Mainline Follow-up Chain

The user keeps following up on the same mainline after a bot reply.

- Total turns: 4
- Self-check passed turns: 2
- Self-check failed turns: 2
- States seen: NOT_READY
- Last reply preview: Yes—let’s pause briefly for the cognitive echo to fade, so the next compaction cycle starts clean.
- Last fail reasons: mainline_anchor_missed, warm_summary_not_reflected, did_not_address_last_question

## mainline_with_smalltalk - Mainline Washed by Smalltalk

A bot-directed mainline is followed by casual side chatter.

- Total turns: 5
- Self-check passed turns: 4
- Self-check failed turns: 1
- States seen: NOT_READY
- Last reply preview: Got it—focusing on the state machine: the two-phase commit ensures atomic transitions, and rollback is triggered on any phase failure to maintain consistency.
- Last fail reasons: warm_summary_not_reflected

## parallel_topics - Parallel Topics

Several small threads run in parallel across the recent window.

- Total turns: 5
- Self-check passed turns: 3
- Self-check failed turns: 2
- States seen: NOT_READY
- Last reply preview: A warm summary shouldn’t just stitch rule lines because that risks creating a fragmented, mechanical recap—losing the narrative flow and context that make summaries useful. Topi...
- Last fail reasons: warm_summary_not_reflected, did_not_address_last_question

## forced_compaction - Forced Compaction at 120

A long chat reaches forced compaction and then waits for the earliest safe hook.

- Total turns: 139
- Self-check passed turns: 137
- Self-check failed turns: 2
- States seen: COOLDOWN, NOT_READY, WAIT_NEXT_NODE
- Last reply preview: (empty)
- Last fail reasons: (none)

## post_compaction_recovery - Post-compaction Recovery

The first two turns after a compaction should still keep the recent mainline clear.

- Total turns: 123
- Self-check passed turns: 123
- Self-check failed turns: 0
- States seen: COOLDOWN, NOT_READY, WAIT_NEXT_NODE
- Last reply preview: Understood—staying on the compressed mainline. The core thread is intact and ready for audit continuation.
- Last fail reasons: (none)
