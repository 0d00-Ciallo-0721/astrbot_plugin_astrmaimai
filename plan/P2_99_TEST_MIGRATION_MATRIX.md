# P2.99 Test Migration Matrix

This document records the current refactor test migration status. It is an
acceptance artifact for the architecture regression suite and does not define
new runtime behavior.

## Current Buckets

| Bucket | Purpose | Status |
| --- | --- | --- |
| `tests/unit` | Focused unit coverage for isolated contracts | Present |
| `tests/integration` | Cross-module integration coverage | Present |
| `tests/regression` | Architecture and behavior regression checks | Present |
| `tests/fixtures` | Shared static fixtures | Present |
| `tests/helpers` | Shared test stubs and utilities | Present |

## Migration Notes

- New refactor tests should prefer `tests/helpers` over importing root-level
  `tests.test_*` modules.
- Legacy `original_ported` tests may remain while their contracts are updated
  to match the refactored implementation.
- Architecture tests should ignore local virtual environments and generated
  dependency trees.
