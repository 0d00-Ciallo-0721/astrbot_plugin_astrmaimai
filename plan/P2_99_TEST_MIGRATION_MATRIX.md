# P2-99 Test Migration Matrix

This matrix records the permanent test buckets expected by the refactored AstrMai repository. It exists so directory-contract tests can verify that migrated tests remain discoverable and are not silently folded back into ad-hoc locations.

## Buckets

| Bucket | Path | Purpose | Acceptance Evidence |
| --- | --- | --- | --- |
| Unit | `tests/unit/` | Narrow tests for single services, contracts, and pure helpers. | Unit files are importable and runnable through `pytest`. |
| Integration | `tests/integration/` | Cross-module runtime paths with real intermediate code and mocked external edges. | Integration smoke tests remain separate from unit suites. |
| Regression | `tests/regression/` | Bug-fix guards, architecture contracts, and migrated production incident checks. | Regression files describe the historical failure they guard. |
| Fixtures | `tests/fixtures/` | Shared local runtime fixtures and data builders. | Fixtures contain no production code side effects. |
| Helpers | `tests/helpers/` | Test-only helpers, stubs, and audit generators. | Helpers are imported only by tests or manual audit scripts. |

## Migration Rules

- New tests should go into the narrowest bucket that proves the behavior.
- Production code must not import `tests.*`.
- Manual audit scripts stay under `tests/manual/` and are not part of the default acceptance contract.
- Architecture tests may assert file and directory presence, but should not depend on machine-local data.
- Plugin Page checks must verify `pages/admin/` as the supported management entry.

## Current Acceptance Commands

```powershell
python -m pytest tests/regression/architecture/test_directory_contracts_refactor.py -q
python -m pytest -q
```
