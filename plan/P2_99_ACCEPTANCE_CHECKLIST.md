# P2.99 Acceptance Checklist

## Objective

This checklist captures the minimum evidence required before a refactor-era release is ready to ship.

## Required Gates

- [ ] `python -m py_compile` passes for the release compile gate targets.
- [ ] Architecture contract suites pass.
- [ ] State and reply chain suites pass.
- [ ] Scheduler and proactive suites pass.
- [ ] Memory / review / learning / persona suites pass.
- [ ] WebUI / plugin page / fixture suites pass.

## Real Provider Evidence

- [ ] Live mood semantic audit passes with no drift cases and no parse failures.
- [ ] Host ingress mood audit matches direct provider results.
- [ ] Host post-send audit matches expected `mood` and `social_score` outcomes.
- [ ] Provider matrix clearly separates recommended, backup, and not recommended models.
- [ ] Fallback validation contains at least one successful switch record.

## Business Smoke Evidence

- [ ] Group `@bot` normal QA path is covered.
- [ ] Negative / sarcasm input path is covered.
- [ ] Tool / memory intent path is covered.
- [ ] Private chat keeps the “no energy cost” design while mood still updates.
- [ ] Scheduler / proactive smoke is covered.
- [ ] Admin console pages have valid host or direct-open acceptance evidence.

## Release Artifacts

- [ ] `artifacts/state_bar_audit/` contains the latest mood and host-chain audit outputs.
- [ ] `artifacts/release_validation/pre_release_full_test_report.md` exists.
- [ ] `artifacts/release_validation/pre_release_full_test_report.json` exists.

## Sign-off Rule

The release is ready only when the compile gate, local regressions, real-provider core chain, fallback evidence, and business smoke are all green, or any remaining risk is explicitly documented and accepted.
