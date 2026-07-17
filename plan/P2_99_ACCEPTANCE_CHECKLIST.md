# P2-99 Acceptance Checklist

Use this checklist before declaring the refactored test layout accepted.

## Directory Layout

- [ ] `tests/unit/` exists for narrow unit coverage.
- [ ] `tests/integration/` exists for cross-module flows.
- [ ] `tests/regression/` exists for bug-fix and architecture guards.
- [ ] `tests/fixtures/` exists for shared test data.
- [ ] `tests/helpers/` exists for stubs and audit utilities.

## Plugin Page Contract

- [ ] `pages/admin/index.html` exists.
- [ ] `pages/admin/app.js` exists.
- [ ] `pages/admin/style.css` exists.
- [ ] `astrmai/webui/backend/server.py` does not expose the old standalone frontend directory.

## Runtime Safety

- [ ] Test helpers are not imported by production modules.
- [ ] Manual audit scripts are optional and do not block normal plugin startup.
- [ ] Plugin runtime does not require test files.

## Verification

- [ ] `python -m pytest tests/regression/architecture/test_directory_contracts_refactor.py -q`
- [ ] Targeted suites for the current change pass.
- [ ] `python -m compileall -q astrmai`
- [ ] `git diff --check`
