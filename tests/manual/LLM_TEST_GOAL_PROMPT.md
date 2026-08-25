# AstrMai LLM Test Goal Prompt

Use this goal text when starting or resuming a bounded AstrMai LLM validation run:

> Complete a layered AstrMai LLM validation run using mock data first and a real
> Provider second. Keep the layers separate: offline/mock results use
> `measurement_scope=offline_mock`, direct OpenAI-compatible calls use
> `measurement_scope=provider_probe`, and only a real AstrBot event adapter may
> produce `measurement_scope=astrmai_host`. For every layer, write an independent
> run directory under `artifacts/live_validation/<run_id>/` and record public
> metadata only. Never write API keys, authorization headers, prompts, message
> bodies, or response bodies to artifacts.
>
> Offline phase: run the focused probe-contract tests and the full pytest suite.
> Verify configuration routing, disabled-model filtering, JSON/Tool Call/SSE
> contracts, failure classification, budget rejection, Host adapter contracts,
> terminal-state aggregation, runtime sampling, and redaction. Record test
> counts, skipped tests, warnings, commit id, and elapsed time.
>
> Real Provider phase: use the configured model and provider from the secret
> file/environment, never mutate production configuration. Use an explicit
> tiered budget rather than an unbounded run: smoke `12` calls, extended
> `60-120` calls, and soak `300-500` calls only after the lower tier passes.
> Provider concurrency must be explicitly selected and recorded; start with
> `1,2,3,4`, and test `8` only as a separate stress tier. Record model/provider
> ids, configured and effective endpoints, timeout values, retry/fallback fields
> (null when unmeasured), HTTP status, finish reason, contract fields, duration,
> context profile, prompt size, conversation rounds, token usage, and sanitized
> error class. Exclude budget-rejected requests from latency percentiles. If the
> key is invalid, classify the run as `auth_error`/`permission_error` and stop;
> do not retry or include it in performance statistics.
>
> Background safety: do not start AstrBot Host for this goal unless a real event
> adapter and valid Host authentication are present. If Host testing is enabled,
> enforce `max_background_concurrency=1` and `max_background_llm_calls=2`, keep
> total real Provider calls within the remaining global budget, and stop at the
> first budget breach, queue runaway, or non-zero active/queued background count
> after shutdown. Do not run B01/B05/B07/B08 from intent payloads alone; mark
> them `not_configured` or `measurement_incomplete` until Host evidence contains
> real event, turn, and trace ids plus scenario-specific metrics.
>
> At the end, generate or update a summary containing one row per run, totals
> by measurement scope, success/failure counts, P50/P95/P99 where meaningful,
> configuration mismatches, timeout/error classes, observed concurrency, and
> missing-evidence counts. State explicitly which layers were executed and do
> not claim production readiness from Provider-only evidence.

## Execution contract

### Offline/mock

```powershell
python -m pytest -q tests/test_live_test_harness.py tests/test_astrmai_host_probe.py
python -m pytest -q
python -m compileall astrmai tests
git diff --check
```

Expected output is recorded in the round summary, not inferred from a prior
run. A non-zero exit code blocks the next phase.

### Provider smoke

```powershell
python tests/manual/live_llm_probe.py `
  --levels 1 `
  --calls-per-level 1 `
  --max-calls 1 `
  --context-profile short `
  --rounds 1 `
  --timeout-sec 20 `
  --output-dir artifacts/live_validation
```

Before running, confirm that the key is supplied through the external secrets
file or environment and that the selected model/provider route is enabled.

For hidden-tail coverage, run separate bounded rounds with:

```powershell
python tests/manual/live_llm_probe.py --levels 1,2,3,4 --calls-per-level 15 --max-calls 60 --context-profile medium --rounds 4 --max-tokens 512
python tests/manual/live_llm_probe.py --levels 1,2,3,4 --calls-per-level 15 --max-calls 60 --context-profile long --rounds 8 --max-tokens 1024
python tests/manual/live_llm_probe.py --levels 1,2,4,8 --calls-per-level 10 --max-calls 40 --context-profile xlong --rounds 8 --max-tokens 1024
```

Do not combine all tiers in one command. Stop between tiers to inspect P95/P99,
read/connect timeouts, token growth, queue wait, and protocol-contract errors.

For a guarded sequential matrix, use `tests/manual/run_provider_matrix.py`.
It is dry-run by default; `--execute` is required to send requests, and the
global budget is checked before every case.

### Public observations to retain

- `run_id`, `measurement_scope`, `started_at`, `finished_at`, commit id
- model/provider ids and endpoint hosts, never credentials
- configured/effective Gateway and model timeouts
- request sequence, status, HTTP status, error class, finish reason
- elapsed time, first/last byte for SSE, JSON validity, Tool Call contract
- observed concurrency and budget counters
- context profile, requested prompt size, conversation rounds, and token usage
- Host-only: event/turn/trace presence, terminal status, queue/lock/background
  metrics, and `measurement_incomplete` counts

### Hard stop conditions

- Provider calls started reach the configured global maximum for the current
  tier (12 smoke, 60-120 extended, or an explicitly approved soak budget).
- Provider concurrency exceeds 2.
- Background LLM concurrency exceeds 1 or background calls exceed 2.
- Any budget guard reports exhaustion or any shutdown leaves background work
  active/queued.
- Host authentication/event adapter is absent: record `not_configured` and stop
  Host execution; do not fabricate IDs or mark the run passed.
