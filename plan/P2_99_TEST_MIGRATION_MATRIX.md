# P2.99 Test Migration Matrix

This document is retained as a compatibility artifact for regression tests that
still require the original phase 2.99 planning path.

## Current Role

- Preserve the historical file contract used by architecture and release checks.
- Mark the repo as still carrying the migrated `unit`, `integration`, and `regression` test buckets.
- Defer any fuller historical reconstruction to a dedicated docs cleanup window.

## Active Buckets

| Bucket | Purpose | Status |
|---|---|---|
| `unit` | Local service and contract coverage | Active |
| `integration` | Runtime and boundary wiring coverage | Active |
| `regression` | Legacy behavior lock coverage | Active |
