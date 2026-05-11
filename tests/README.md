# Refactor Test Layout

## Directory Rules

- `unit/`
  Single-module and pure-logic tests.
- `integration/`
  Multi-module wiring and runtime collaboration tests.
- `regression/`
  Historical bug baselines, behavior guards, and architecture boundary checks.
- `fixtures/`
  Reusable temporary environments and resource wrappers.
- `helpers/`
  Shared stub installers and test support functions.

## Helper / Fixture Policy

- New refactor-side tests should prefer `helpers/` for shared AstrBot, planner, executor, or reply stubs.
- `fixtures/` is for reusable runtime wrappers such as `TempAstrbotEnv`, not for ad-hoc per-test setup code.
- Top-level refactor tests under `astrmai_plugin_refactored_final/tests/` should not import `tests.test_*` helper functions from the original root test directory.
- Categorized `*_migrated.py` files under `unit/`, `integration/`, and `regression/` are allowed to mirror original root tests while migration is in progress.

## Migration Strategy

- Use `P2_99_TEST_MIGRATION_MATRIX.md` as the source of truth for batch ordering.
- Do not try to empty the original root `tests/` directory in one shot.
- Every new refactor test should declare its ownership bucket clearly and keep behavior aligned with the original baseline.
