# Group Trace Audit Summary

## Aggregate

- Total scenarios: 1
- Total turns: 4
- Passed turns: 3
- Failed turns: 1
- Worst WAIT_NEXT_NODE scenario: vision_mixed_context
- Earliest COOLDOWN scenario: (none)
- Most common block reason: (none)
- Most unstable score bucket: closure_score

## vision_mixed_context - Vision Mixed Context

An image-heavy message sequence still needs warm summary to preserve the mainline.

- Tags: vision, mixed
- Difficulty: advanced
- Total turns: 4
- Self-check passed turns: 3
- Self-check failed turns: 1
- States seen: NOT_READY
- State counts: {"NOT_READY": 4}
- Last reply preview: Got it—focusing on the compaction mainline now. The earlier question was about the root cause of the compaction stall; I’ll address that directly.
- Last fail reasons: (none)

### State Timeline

- First `NOT_READY`: turn 1

### Failure Cards

- Turn 1: did_not_address_last_question
  - Input: AstrMai, can you keep the compaction mainline while I send a screenshot?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“AstrMai, can you keep the...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“AstrMai, can you keep the c...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Reply: On it—holding the compaction mainline while you send the screenshot. 📸
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["compaction mainline", "screenshot"], "matched_mainline_tokens": ["compaction", "mainline", "screenshot"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "astrmai", "can", "you", "keep", "the", "compaction", "mainline"]}
