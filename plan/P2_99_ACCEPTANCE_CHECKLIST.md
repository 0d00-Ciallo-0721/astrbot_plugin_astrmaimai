# P2.99 Acceptance Checklist

This checklist captures the acceptance conditions for the refactor test layout
and migration hygiene.

## Checklist

- Refactor test buckets exist: unit, integration, regression, fixtures, helpers.
- WebUI shell files are split into components and pages.
- Plugin Page regression tests cover native bridge usage and API registration.
- Local virtual environments are excluded from architecture path scans.
- Full regression can be used to detect new failures without known baseline
  noise from missing migration artifacts.
