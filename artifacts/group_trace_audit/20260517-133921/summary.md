# Group Trace Audit Summary

## Aggregate

- Total scenarios: 10
- Total turns: 411
- Passed turns: 375
- Failed turns: 36
- Worst WAIT_NEXT_NODE scenario: forced_compaction
- Earliest COOLDOWN scenario: post_compaction_recovery
- Most common block reason: recent_dense_bot_exchange
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
- Last reply preview: Yes—the rule should wait until the reply chain cools down, because active discussion indicates unresolved contention in the focus window; compaction should only proceed once the...
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
- Last reply preview: Got it—focusing on the compaction state machine: the key takeaway is that atomic mark-then-compact phases prevent data loss during segment merging, and the state transitions are...
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
- Last reply preview: A warm summary should avoid stitching rule lines because doing so risks pulling in unrelated or prematurely resolved details, which can fragment the active topic thread—our curr...
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
- Last reply preview: Yes, the mainline from just now—the compressed audit thread—is still active and ready for continuation.
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
- Self-check passed turns: 2
- Self-check failed turns: 33
- States seen: NOT_READY
- State counts: {"NOT_READY": 35}
- Last reply preview: Yes—still delay compaction while the chain is live to prevent fragmentation and ensure consistency.
- Last fail reasons: did_not_address_last_question

### State Timeline

- First `NOT_READY`: turn 1

### Failure Cards

- Turn 2: did_not_address_last_question
  - Input: Tail drag 2: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 2: @AstrMai sho...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“Tail drag 2: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Reply: Yes—keep delaying compaction while the chain is live to avoid fragmentation and maintain consistency.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": [], "matched_mainline_tokens": ["delay", "compaction", "chain"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"]}
- Turn 4: did_not_address_last_question
  - Input: Tail drag 4: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 4: @AstrMai sho...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“Tail drag 4: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Reply: Yes—keep delaying compaction while the chain is live to prevent fragmentation and ensure consistency.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": [], "matched_mainline_tokens": ["delay", "compaction", "chain", "event"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"]}
- Turn 5: did_not_address_last_question
  - Input: Tail drag 5: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 5: @AstrMai sho...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“Tail drag 5: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Reply: Yes—still delay compaction while the chain is live to prevent fragmentation and ensure consistency.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "chain", "event"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"]}
- Turn 6: did_not_address_last_question
  - Input: Tail drag 6: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 6: @AstrMai sho...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 6: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Reply: Yes—still delay compaction while the chain is live to prevent fragmentation and ensure consistency.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "chain", "event"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"]}
- Turn 7: did_not_address_last_question
  - Input: Tail drag 7: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 7: @AstrMai sho...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 7: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Reply: Yes—still delay compaction while the chain is live to prevent fragmentation and ensure consistency.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "chain", "event"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"]}
- Turn 8: did_not_address_last_question
  - Input: Tail drag 8: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 8: @AstrMai sho...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 8: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Reply: Yes—still delay compaction while the chain is live to prevent fragmentation and ensure consistency.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "chain", "event"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"]}
- Turn 9: did_not_address_last_question
  - Input: Tail drag 9: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 9: @AstrMai sho...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 9: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Reply: Yes—still delay compaction while the chain is live to prevent fragmentation and ensure consistency.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "chain", "event"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"]}
- Turn 10: did_not_address_last_question
  - Input: Tail drag 10: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 10: @AstrMai sh...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 10: @AstrMai shou...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Reply: Yes—still delay compaction while the chain is live to prevent fragmentation and ensure consistency.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "chain", "event"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"]}
- Turn 11: did_not_address_last_question
  - Input: Tail drag 11: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 11: @AstrMai sh...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 11: @AstrMai shou...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Reply: Yes—still delay compaction while the chain is live to prevent fragmentation and ensure consistency.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "chain", "event"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"]}
- Turn 12: did_not_address_last_question
  - Input: Tail drag 12: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 12: @AstrMai sh...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 12: @AstrMai shou...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Reply: Yes—still delay compaction while the chain is live to prevent fragmentation and ensure consistency.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "chain", "event"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "tail", "drag", "10", "astrmai", "should"]}

## unsettled_topic_shift - Unsettled Topic Shift

A new topic begins before the old mainline has fully closed.

- Tags: closure, safe-window
- Difficulty: advanced
- Total turns: 4
- Self-check passed turns: 4
- Self-check failed turns: 0
- States seen: NOT_READY
- State counts: {"NOT_READY": 4}
- Last reply preview: No, the previous chain isn’t settled yet—let’s resolve it before moving to safe window timing.
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
- Last reply preview: Understood—staying on your topic density branch. Let’s dive deeper: what specific metrics or signals are you using to measure density in your context? That’ll help refine the sc...
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

## vision_mixed_context - Vision Mixed Context

An image-heavy message sequence still needs warm summary to preserve the mainline.

- Tags: vision, mixed
- Difficulty: advanced
- Total turns: 4
- Self-check passed turns: 3
- Self-check failed turns: 1
- States seen: NOT_READY
- State counts: {"NOT_READY": 4}
- Last reply preview: Got it—focusing on the compaction mainline question now, not the image. What’s the specific detail you need clarified?
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

### Failure Cards

- Turn 1: did_not_address_last_question
  - Input: AstrMai, can you keep the compaction mainline while I send a screenshot?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“AstrMai, can you keep the...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“AstrMai, can you keep the c...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Reply: On it—holding the compaction mainline while you send the screenshot.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["compaction mainline", "screenshot"], "matched_mainline_tokens": ["compaction", "mainline", "screenshot"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "astrmai", "can", "you", "keep", "the", "compaction", "mainline"]}

## post_compaction_fast_followup - Post-compaction Fast Follow-up

A new bot-directed follow-up arrives immediately after compaction recovery starts.

- Tags: recovery, tail-heavy
- Difficulty: advanced
- Total turns: 123
- Self-check passed turns: 121
- Self-check failed turns: 2
- States seen: COOLDOWN, NOT_READY, WAIT_NEXT_NODE
- State counts: {"COOLDOWN": 25, "NOT_READY": 78, "WAIT_NEXT_NODE": 20}
- Last reply preview: Yes—zero drift, immediate continuation. The mainline is locked and ready.
- Last fail reasons: mainline_anchor_missed, did_not_address_last_question

### State Timeline

- First `NOT_READY`: turn 1
- First `WAIT_NEXT_NODE`: turn 79
- First `COOLDOWN`: turn 99

### Failure Cards

- Turn 122: did_not_address_last_question
  - Input: AstrMai, right after compaction, can you still follow the exact mainline?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“AstrMai, right after comp...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“AstrMai, right after compac...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Reply: Yes, I’m still locked onto the exact mainline—post-compaction continuity is intact. Ready for the next step.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["exact mainline"], "matched_mainline_tokens": ["compaction", "exact", "mainline"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "fast", "follow", "up", "setup", "121", "keep", "the"]}
- Turn 123: mainline_anchor_missed, did_not_address_last_question
  - Input: And can you continue immediately without drifting?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“And can you continue imme...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“And can you continue immedi...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Reply: Yes—zero drift, immediate continuation. The mainline is locked and ready.
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": [], "matched_mainline_tokens": [], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "astrmai", "right", "after", "compaction", "can", "you", "still"]}

### Recovery Snapshot

- Turn 1: state=NOT_READY, recent_reason=warm_sufficient, reply=(empty)
- Turn 2: state=NOT_READY, recent_reason=warm_sufficient, reply=(empty)
- Turn 3: state=NOT_READY, recent_reason=warm_sufficient, reply=(empty)
- Turn 4: state=NOT_READY, recent_reason=warm_sufficient, reply=(empty)
