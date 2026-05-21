# P10.2 / P10.3 audit baseline

## mood
- audit mode: `static_and_chain_level`
- live LLM semantic audit: `not_run` (live mood audit disabled; set ASTRMAI_ENABLE_LIVE_MOOD_AUDIT=1 to run)
- parser failures: `none`
- fallback issues: `{"over_neutralized": 0, "direction_conflict": 0, "mixed_affect_flattened": 0}`
- live drift cases: `none`
- live parse failures: `none`
- verdict: fallback quality is acceptable for obvious positive/negative text, and sarcasm plus mixed affect now stay directionally stable in the local heuristic

## social_score
- audit mode: `static_and_host_chain_semantics`
- issue cases: `none`
- mixed affect score: `0.2900`
- mixed affect remap suppressed: `True`
- verdict: social_score direction stays aligned on obvious positive and negative text, and mixed affect no longer escalates into an overly strong support-style uplift

## stance
- audit mode: `chain_level_plus_prompt_surface`
- guarded follow-up probability: `0.0280`
- cool follow-up probability: `0.0480`
- neutral follow-up probability: `0.0800`
- warm follow-up probability: `0.0800`
- first reply prompt-only: `False`
- soft first-reply cases: `none`
- verdict: stance is real at tool and follow-up layers, and guarded/cool now also apply deterministic first-reply text constraints

## Audit Readout
- mood: primary update, Judge micro-adjust, and post-send settlement are all live; this baseline now separates static-chain checks from optional live semantic drift checks.
- social_score: the audit now distinguishes text classification, mood-tag remap, and final score amplitude so mixed affect does not silently inherit an overly positive support event.
- stance: guarded/cool affect tool filtering, follow-up probability, and deterministic first-reply text constraints; the remaining question is how much tightening each social_intent should apply.
