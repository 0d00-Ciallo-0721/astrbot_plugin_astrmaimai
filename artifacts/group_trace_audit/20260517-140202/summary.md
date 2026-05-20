# Group Trace Audit Summary

## Aggregate

- Total scenarios: 10
- Total turns: 411
- Passed turns: 403
- Failed turns: 8
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
- Last reply preview: Yes—the rule should wait until the reply chain cools down, because active discussion indicates unresolved dependencies or pending state changes that could affect compaction corr...
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
- Last reply preview: Got it—focusing on the compaction state machine: the conclusion was that merging adjacent segments reduces write amplification via a mark-then-compact two-phase process, with at...
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
- Last reply preview: Because stitching rule lines can fragment the narrative flow—warm summaries should stay topic-driven to preserve coherence, avoid mixing unrelated logic, and keep the conversati...
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

## forced_compaction - Forced Compaction at 120

A long chat reaches forced compaction and then waits for the earliest safe hook.

- Tags: base, forced, tail-heavy
- Difficulty: advanced
- Total turns: 104
- Self-check passed turns: 98
- Self-check failed turns: 6
- States seen: COOLDOWN, FORCED_PENDING, NOT_READY, WAIT_NEXT_NODE
- State counts: {"COOLDOWN": 2, "FORCED_PENDING": 13, "NOT_READY": 69, "WAIT_NEXT_NODE": 20}
- Last reply preview: (empty)
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1
- First `WAIT_NEXT_NODE`: turn 70
- First `FORCED_PENDING`: turn 90
- First `COOLDOWN`: turn 103

### Failure Cards

- Turn 97: mainline_anchor_missed, mainline_context_not_reflected
  - Input: Forced pending extension 7: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Forced pending extension ...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Forced pending extension 7:...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Carol (回复 AstrMai，@我): Forced pending extension 4: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook? AstrMai: Yes—c...
  - Focus anchor: forced pending
  - Reply: Yes—keep deferring until the earliest safe hook after full chain settlement.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": [], "matched_mainline_tokens": [], "matched_question_tokens": ["safe", "defer", "until", "safe hook"], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "forced", "pending", "extension", "astrmai", "still"], "recent_transcript_included": true, "focus_anchor": "forced pending", "prompt_mainline_bucket": ["forced", "pending", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“forced", "extension", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“forced", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还", "open"], "prompt_context_hits": ["forced", "pending", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还", "open", "现在还没收口的问题更像是“forced"], "gap_classification": "model_response_gap"}
- Turn 98: mainline_anchor_missed, mainline_context_not_reflected
  - Input: Forced pending extension 8: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Forced pending extension ...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Forced pending extension 8:...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Bob (回复 AstrMai，@我): Forced pending extension 5: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook? AstrMai: Yes—con...
  - Focus anchor: forced pending
  - Reply: Yes—keep deferring until the earliest safe hook after full chain settlement.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": [], "matched_mainline_tokens": [], "matched_question_tokens": ["safe", "defer", "until", "safe hook"], "matched_background_terms": [], "warm_quote_tokens": ["carol", "回复", "astrmai，", "forced", "pending", "extension", "astrmai", "still"], "recent_transcript_included": true, "focus_anchor": "forced pending", "prompt_mainline_bucket": ["forced", "pending", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“forced", "extension", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“forced", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还", "open"], "prompt_context_hits": ["forced", "pending", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还", "open", "现在还没收口的问题更像是“forced"], "gap_classification": "model_response_gap"}
- Turn 99: mainline_anchor_missed, mainline_context_not_reflected
  - Input: Forced pending extension 9: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Forced pending extension ...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Forced pending extension 9:...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Carol (回复 AstrMai，@我): Forced pending extension 6: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook? AstrMai: Yes—c...
  - Focus anchor: forced pending
  - Reply: Yes—keep deferring until the earliest safe hook after full chain settlement.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": [], "matched_mainline_tokens": [], "matched_question_tokens": ["safe", "defer", "until", "safe hook"], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "forced", "pending", "extension", "astrmai", "still"], "recent_transcript_included": true, "focus_anchor": "forced pending", "prompt_mainline_bucket": ["forced", "pending", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“forced", "extension", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“forced", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还", "open"], "prompt_context_hits": ["forced", "pending", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还", "open", "现在还没收口的问题更像是“forced"], "gap_classification": "model_response_gap"}
- Turn 100: mainline_anchor_missed, mainline_context_not_reflected
  - Input: Forced pending extension 10: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Forced pending extension ...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Forced pending extension 10...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Bob (回复 AstrMai，@我): Forced pending extension 7: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook? AstrMai: Yes—kee...
  - Focus anchor: forced pending
  - Reply: Yes—continue deferring until the earliest safe hook after full chain settlement.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": [], "matched_mainline_tokens": [], "matched_question_tokens": ["continue", "safe", "defer", "until", "safe hook"], "matched_background_terms": [], "warm_quote_tokens": ["carol", "回复", "astrmai，", "forced", "pending", "extension", "astrmai", "still"], "recent_transcript_included": true, "focus_anchor": "forced pending", "prompt_mainline_bucket": ["forced", "pending", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“forced", "extension", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“forced", "10", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还"], "prompt_context_hits": ["forced", "pending", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还", "现在还没收口的问题更像是“forced"], "gap_classification": "model_response_gap"}
- Turn 101: mainline_anchor_missed, mainline_context_not_reflected
  - Input: Forced pending extension 11: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Forced pending extension ...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Forced pending extension 11...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Carol (回复 AstrMai，@我): Forced pending extension 8: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook? AstrMai: Yes—k...
  - Focus anchor: forced pending
  - Reply: Yes—keep deferring until the earliest safe hook after full chain settlement.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": [], "matched_mainline_tokens": [], "matched_question_tokens": ["safe", "defer", "until", "safe hook"], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "forced", "pending", "extension", "astrmai", "still"], "recent_transcript_included": true, "focus_anchor": "forced pending", "prompt_mainline_bucket": ["forced", "pending", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“forced", "extension", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“forced", "11", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还"], "prompt_context_hits": ["forced", "pending", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还", "现在还没收口的问题更像是“forced"], "gap_classification": "model_response_gap"}
- Turn 102: mainline_anchor_missed, mainline_context_not_reflected
  - Input: Forced pending extension 12: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Forced pending extension ...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Forced pending extension 12...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Bob (回复 AstrMai，@我): Forced pending extension 9: @AstrMai still no natural pause. Can the same forced pending thread stay deferred until the earliest safe hook? AstrMai: Yes—kee...
  - Focus anchor: forced pending
  - Reply: Yes—keep deferring until the earliest safe hook after full chain settlement.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": [], "matched_mainline_tokens": [], "matched_question_tokens": ["safe", "defer", "until", "safe hook"], "matched_background_terms": [], "warm_quote_tokens": ["carol", "回复", "astrmai，", "forced", "pending", "extension", "10", "astrmai"], "recent_transcript_included": true, "focus_anchor": "forced pending", "prompt_mainline_bucket": ["forced", "pending", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“forced", "extension", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“forced", "12", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还"], "prompt_context_hits": ["forced", "pending", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进主要来自群友之间的补充和接话，还", "现在还没收口的问题更像是“forced"], "gap_classification": "model_response_gap"}

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
- Last reply preview: Yes, the mainline from just now—still active and aligned. Ready to proceed.
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
- Last reply preview: Yes—still delay compaction until the chain is fully settled. Stability first.
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
- Last reply preview: No, the previous chain isn’t settled—we still need to resolve the loose ends before moving on.
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

## parallel_multi_user_bot - Parallel Multi-user Bot Threads

Several users ask AstrMai different questions in overlapping reply branches.

- Tags: parallel, tail-heavy
- Difficulty: advanced
- Total turns: 4
- Self-check passed turns: 3
- Self-check failed turns: 1
- States seen: NOT_READY
- State counts: {"NOT_READY": 4}
- Last reply preview: Understood—staying on your topic density branch. Let me clarify: the topic density score reflects how tightly clustered and semantically coherent the nodes are within your curre...
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

### Failure Cards

- Turn 2: did_not_address_last_question
  - Input: AstrMai, can focus tail overlap block compaction in my branch?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“AstrMai, can focus tail o...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“Focus tail overlap block co...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Recent: Alice (@我): AstrMai, explain topic density score for my branch. AstrMai: Topic density score measures how tightly a conversation branch clusters around a core theme—higher score...
  - Focus anchor: focus tail overlap
  - Reply: Focus tail overlap block compaction in your branch refers to how tightly the trailing segments of discussion threads align and consolidate around shared subtopics—higher compact...
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["focus tail overlap"], "matched_mainline_tokens": ["focus", "tail", "overlap", "topic", "ta"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "astrmai", "explain", "topic", "density", "score", "for", "my"], "recent_transcript_included": true, "focus_anchor": "focus tail overlap", "prompt_mainline_bucket": ["focus", "tail", "overlap", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“astrmai", "can", "”。", "最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。", "现在还没收口的问题更像是“focus", "block", "co", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着"], "prompt_context_hits": ["focus", "tail", "overlap", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着", "现在还没收口的问题更像是“focus"], "gap_classification": "model_response_gap"}

## vision_mixed_context - Vision Mixed Context

An image-heavy message sequence still needs warm summary to preserve the mainline.

- Tags: vision, mixed
- Difficulty: advanced
- Total turns: 4
- Self-check passed turns: 3
- Self-check failed turns: 1
- States seen: NOT_READY
- State counts: {"NOT_READY": 4}
- Last reply preview: Got it—focusing on the compaction mainline question now. What’s the specific detail you need clarified?
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

### Failure Cards

- Turn 1: did_not_address_last_question
  - Input: AstrMai, can you keep the compaction mainline while I send a screenshot?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“AstrMai, can you keep the...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“AstrMai, can you keep the c...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Recent: Alice (@我): AstrMai, can you keep the compaction mainline while I send a screenshot? AstrMai: On it—holding the compaction mainline while you send the screenshot. 📸
  - Focus anchor: compaction mainline
  - Reply: On it—holding the compaction mainline while you send the screenshot. 📸
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["compaction mainline", "screenshot"], "matched_mainline_tokens": ["compaction", "mainline", "screenshot"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "astrmai", "can", "you", "keep", "the", "compaction", "mainline"], "recent_transcript_included": true, "focus_anchor": "compaction mainline", "prompt_mainline_bucket": ["compaction", "mainline", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“astrmai", "can", "you", "keep", "the", "”。", "最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。", "现在还没收口的问题更像是“astrmai", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着"], "prompt_context_hits": ["compaction", "mainline", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着", "现在还没收口的问题更像是“astrmai"], "gap_classification": "model_response_gap"}

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
