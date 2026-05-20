# Group Trace Audit Summary

## Aggregate

- Total scenarios: 1
- Total turns: 35
- Passed turns: 1
- Failed turns: 34
- Worst WAIT_NEXT_NODE scenario: (none)
- Earliest COOLDOWN scenario: (none)
- Most common block reason: (none)
- Most unstable score bucket: (none)

## long_tail_drag - Long Tail Drag

A long bot-directed tail keeps the active chain alive for many rounds.

- Tags: tail-heavy, forced
- Difficulty: advanced
- Total turns: 35
- Self-check passed turns: 1
- Self-check failed turns: 34
- States seen: NOT_READY
- State counts: {"NOT_READY": 35}
- Last reply preview: Yes—still delay compaction while the chain is live. Prevents contention and keeps tail drag stable.
- Last fail reasons: did_not_address_last_question

### State Timeline

- First `NOT_READY`: turn 1

### Failure Cards

- Turn 2: did_not_address_last_question
  - Input: Tail drag 2: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 2: @AstrMai sho...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“Tail drag 2: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Recent: Alice (回复 AstrMai，@我): Tail drag 1: @AstrMai should we still delay compaction while this same chain is live? AstrMai: Yes, we should delay compaction while the chain is live—avo...
  - Focus anchor: delay compaction
  - Reply: Yes—same reasoning: delaying compaction while the chain is live prevents contention and keeps tail drag under control.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": [], "matched_mainline_tokens": ["delay", "compaction", "same", "chain", "event", "dra"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"], "recent_transcript_included": true, "focus_anchor": "delay compaction", "prompt_mainline_bucket": ["delay", "compaction", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“tail", "drag", "astrmai", "sho", "”。", "最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。", "现在还没收口的问题更像是“tail", "shoul", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着"], "prompt_context_hits": ["delay", "compaction", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着", "现在还没收口的问题更像是“tail"], "gap_classification": "model_response_gap"}
- Turn 3: did_not_address_last_question
  - Input: Tail drag 3: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 3: @AstrMai sho...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“Tail drag 3: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Recent: Alice (回复 AstrMai，@我): Tail drag 1: @AstrMai should we still delay compaction while this same chain is live? AstrMai: Yes, we should delay compaction while the chain is live—avo...
  - Focus anchor: delay compaction
  - Reply: Yes—still the same: delay compaction while the chain is live to avoid contention and keep tail drag in check.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "same", "chain", "dra"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"], "recent_transcript_included": true, "focus_anchor": "delay compaction", "prompt_mainline_bucket": ["delay", "compaction", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“tail", "drag", "astrmai", "sho", "”。", "最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。", "现在还没收口的问题更像是“tail", "shoul", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着"], "prompt_context_hits": ["delay", "compaction", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着", "现在还没收口的问题更像是“tail"], "gap_classification": "model_response_gap"}
- Turn 4: did_not_address_last_question
  - Input: Tail drag 4: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 4: @AstrMai sho...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“Tail drag 4: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Recent: Alice (回复 AstrMai，@我): Tail drag 1: @AstrMai should we still delay compaction while this same chain is live? AstrMai: Yes, we should delay compaction while the chain is live—avo...
  - Focus anchor: delay compaction
  - Reply: Yes—still the same: delay compaction while the chain is live to avoid contention and keep tail drag in check.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "same", "chain", "dra"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"], "recent_transcript_included": true, "focus_anchor": "delay compaction", "prompt_mainline_bucket": ["delay", "compaction", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“tail", "drag", "astrmai", "sho", "”。", "最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。", "现在还没收口的问题更像是“tail", "shoul", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着"], "prompt_context_hits": ["delay", "compaction", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着", "现在还没收口的问题更像是“tail"], "gap_classification": "model_response_gap"}
- Turn 5: did_not_address_last_question
  - Input: Tail drag 5: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 5: @AstrMai sho...”。 最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。 现在还没收口的问题更像是“Tail drag 5: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个点继续接。
  - Recent: Bob (回复 AstrMai，@我): Tail drag 2: @AstrMai should we still delay compaction while this same chain is live? AstrMai: Yes—same reasoning: delaying compaction while the chain is li...
  - Focus anchor: delay compaction
  - Reply: Yes—still the same: delay compaction while the chain is live to avoid contention and keep tail drag in check.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "same", "chain", "dra"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"], "recent_transcript_included": true, "focus_anchor": "delay compaction", "prompt_mainline_bucket": ["delay", "compaction", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“tail", "drag", "astrmai", "sho", "”。", "最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。", "现在还没收口的问题更像是“tail", "shoul", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着"], "prompt_context_hits": ["delay", "compaction", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "event", "最近的推进是我刚给过回应，群里现在是在顺着", "现在还没收口的问题更像是“tail"], "gap_classification": "model_response_gap"}
- Turn 6: did_not_address_last_question
  - Input: Tail drag 6: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 6: @AstrMai sho...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 6: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Alice (回复 AstrMai，@我): Tail drag 3: @AstrMai should we still delay compaction while this same chain is live? AstrMai: Yes—still the same: delay compaction while the chain is liv...
  - Focus anchor: delay compaction
  - Reply: Yes—still the same: delay compaction while the chain is live to avoid contention and keep tail drag in check.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "same", "chain", "dra"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"], "recent_transcript_included": true, "focus_anchor": "delay compaction", "prompt_mainline_bucket": ["delay", "compaction", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“tail", "drag", "astrmai", "sho", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“tail", "shoul", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，"], "prompt_context_hits": ["delay", "compaction", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "现在还没收口的问题更像是“tail"], "gap_classification": "model_response_gap"}
- Turn 7: did_not_address_last_question
  - Input: Tail drag 7: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 7: @AstrMai sho...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 7: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Bob (回复 AstrMai，@我): Tail drag 4: @AstrMai should we still delay compaction while this same chain is live? AstrMai: Yes—still the same: delay compaction while the chain is live ...
  - Focus anchor: delay compaction
  - Reply: Yes—still the same: delay compaction while the chain is live to avoid contention and keep tail drag in check.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "same", "chain", "dra"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"], "recent_transcript_included": true, "focus_anchor": "delay compaction", "prompt_mainline_bucket": ["delay", "compaction", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“tail", "drag", "astrmai", "sho", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“tail", "shoul", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，"], "prompt_context_hits": ["delay", "compaction", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "现在还没收口的问题更像是“tail"], "gap_classification": "model_response_gap"}
- Turn 8: did_not_address_last_question
  - Input: Tail drag 8: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 8: @AstrMai sho...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 8: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Alice (回复 AstrMai，@我): Tail drag 5: @AstrMai should we still delay compaction while this same chain is live? AstrMai: Yes—still the same: delay compaction while the chain is liv...
  - Focus anchor: delay compaction
  - Reply: Yes—still the same: delay compaction while the chain is live to avoid contention and keep tail drag in check.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "same", "chain", "dra"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"], "recent_transcript_included": true, "focus_anchor": "delay compaction", "prompt_mainline_bucket": ["delay", "compaction", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“tail", "drag", "astrmai", "sho", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“tail", "shoul", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，"], "prompt_context_hits": ["delay", "compaction", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "现在还没收口的问题更像是“tail"], "gap_classification": "model_response_gap"}
- Turn 9: did_not_address_last_question
  - Input: Tail drag 9: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 9: @AstrMai sho...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 9: @AstrMai shoul...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Bob (回复 AstrMai，@我): Tail drag 6: @AstrMai should we still delay compaction while this same chain is live? AstrMai: Yes—still the same: delay compaction while the chain is live ...
  - Focus anchor: delay compaction
  - Reply: Yes—still the same: delay compaction while the chain is live to avoid contention and keep tail drag in check.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "same", "chain", "dra"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"], "recent_transcript_included": true, "focus_anchor": "delay compaction", "prompt_mainline_bucket": ["delay", "compaction", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“tail", "drag", "astrmai", "sho", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“tail", "shoul", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic", "当前主线还在围绕我刚接住的那个点继续展开，"], "prompt_context_hits": ["delay", "compaction", "topic", "当前主线还在围绕我刚接住的那个点继续展开，", "现在还没收口的问题更像是“tail"], "gap_classification": "model_response_gap"}
- Turn 10: did_not_address_last_question
  - Input: Tail drag 10: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 10: @AstrMai sh...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 10: @AstrMai shou...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Alice (回复 AstrMai，@我): Tail drag 7: @AstrMai should we still delay compaction while this same chain is live? AstrMai: Yes—still the same: delay compaction while the chain is liv...
  - Focus anchor: delay compaction
  - Reply: Yes—still the same: delay compaction while the chain is live to avoid contention and keep tail drag in check.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "same", "chain", "dra"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["bob", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"], "recent_transcript_included": true, "focus_anchor": "delay compaction", "prompt_mainline_bucket": ["delay", "compaction", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“tail", "drag", "10", "astrmai", "sh", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“tail", "shou", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic"], "prompt_context_hits": ["delay", "compaction", "topic", "现在还没收口的问题更像是“tail"], "gap_classification": "model_response_gap"}
- Turn 11: did_not_address_last_question
  - Input: Tail drag 11: @AstrMai should we still delay compaction while this same chain is live?
  - Warm: 当前主线还在围绕我刚接住的那个点继续展开，焦点落在“Tail drag 11: @AstrMai sh...”。 最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。 现在还没收口的问题更像是“Tail drag 11: @AstrMai shou...”，后面大概率还会继续接着问。 互动主线仍然直接连着我，大家在顺着同一个...
  - Recent: Bob (回复 AstrMai，@我): Tail drag 8: @AstrMai should we still delay compaction while this same chain is live? AstrMai: Yes—still the same: delay compaction while the chain is live ...
  - Focus anchor: delay compaction
  - Reply: Yes—still the same: delay compaction while the chain is live to avoid contention and keep tail drag in check.
  - Gap: model_response_gap
  - Detail: {"question_type": "should_can", "matched_mainline_anchors": ["delay compaction"], "matched_mainline_tokens": ["delay", "compaction", "same", "chain", "dra"], "matched_question_tokens": [], "matched_background_terms": [], "warm_quote_tokens": ["alice", "回复", "astrmai，", "tail", "drag", "astrmai", "should", "we"], "recent_transcript_included": true, "focus_anchor": "delay compaction", "prompt_mainline_bucket": ["delay", "compaction", "当前主线还在围绕我刚接住的那个点继续展开，焦点落在“tail", "drag", "11", "astrmai", "sh", "”。", "最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的", "bot", "往返闭环。", "现在还没收口的问题更像是“tail", "shou", "”，后面大概率还会继续接着问。", "互动主线仍然直接连着我，大家在顺着同一个点继续接。", "topic"], "prompt_context_hits": ["delay", "compaction", "topic", "现在还没收口的问题更像是“tail"], "gap_classification": "model_response_gap"}
