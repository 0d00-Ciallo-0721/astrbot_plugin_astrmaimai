# Assignment 12 - WebUI and Plugin Pages Functional Audit

## Audit Result

- Completed against the current working tree on 2026-07-13.
- Confirmed findings: **8** (`P0: 0`, `P1: 1`, `P2: 5`, `P3: 2`).
- Scope: `astrmai/webui/` and `pages/admin/`, plus the minimum adjacent production code needed to prove runtime call chains.
- Exclusions preserved: no tests or coverage artifacts were inspected; authentication, authorization, security policy, `astrmai/infrastructure/security/`, `astrmai/webui/venv`, and runtime/generated data were not audited.
- Production code was not modified.

## Method and Reachability

The primary production entry is `main.py:67`, which calls `register_astrmai_admin_pages(context, self.facade)`. The registered routes under `/astrmai/admin` are invoked by `pages/admin/app.js` through the injected `window.AstrBotPluginPage` bridge. Bridge behavior was checked against the current official [AstrBot Plugin Pages contract](https://docs.astrbot.app/en/dev/star/guides/plugin-pages.html), including plugin-local endpoint prefixing and automatic unwrapping of `{status: "ok", data: value}` responses.

The standalone FastAPI tree in `astrmai/webui/backend/server.py` is not mounted or started by the AstrMai plugin entry, so issues requiring that separate app were not reported without another reachable AstrMai production caller.

## Confirmed Findings

### 12-01 - P1 - WebUI profile mutations bypass the live profile cache and can be overwritten

- **Severity:** P1
- **File:line:** `astrmai/webui/backend/services/user_ui_service.py:107` (basic update), `:142` (delete), `:148` and `:194` (slice mutation); conflicting runtime cache at `astrmai/state/user_profile_service.py:135-142`.
- **Trigger:** An active user's profile has already been loaded into `UserProfileService.user_profiles`, then an administrator saves, deletes, or changes slices from the Users page.
- **Reachable production call chain:** `pages/admin/app.js:1225-1249` -> bridge `admin/users/...` -> routes registered at `astrmai/webui/plugin_pages.py:690-699` -> `AstrMaiAdminPageApi.update_user`/slice handlers at `plugin_pages.py:530-569` -> `UserUiService` direct SQL update/commit -> subsequent message processing calls `ChatStateService`/planner/execution -> `UserProfileService.get_user_profile()` -> cached object returned at `user_profile_service.py:139-142` without re-reading the row.
- **Actual:** The page reload reads the updated database row and appears successful, while the live conversation runtime continues using the old cached `UserProfile`. The persisted manual-lock metadata is likewise absent from the cached object. A later runtime save of that stale dirty object can overwrite the administrator's database changes; deletion can be followed by recreation from the cached profile.
- **Expected:** Page mutations must go through the runtime profile service or atomically invalidate/replace the corresponding live cache entry before reporting success.
- **Production impact:** Profile names, identity, social score, tags, persona analysis, and slices can remain ineffective in live replies and learning after a successful admin action, then be silently lost. Delete is not reliable for an already active user.
- **Existing guard and why it fails:** SQL commits and persisted `manual_locked_fields` protect only the database row. No handler obtains the bound state engine or invalidates `user_profiles`; the runtime's cache-first guard explicitly prevents the fresh row from being loaded.
- **Classification:** Route-service-runtime state coherence / hot propagation.
- **Confidence:** High.

### 12-02 - P2 - Plugin Page reads successful bridge responses as if the envelope still existed

- **Severity:** P2
- **File:line:** `pages/admin/app.js:117`, `:121`, `:482`, `:484`, and `:671`.
- **Trigger:** Open the Overview or Cognition dashboard using the current AstrBot Plugin Pages bridge while runtime health, observability, or scheduler endpoints return their normal `{status: "ok", data: ...}` payloads.
- **Reachable production call chain:** page `api.get()` at `app.js:475-479` or `:724-730` -> `bridge.apiGet("admin/...")` at `app.js:237-240` -> registered handlers in `astrmai/webui/plugin_pages.py` -> `RuntimeUiService`/`AdminUiService` returns a successful `status + data` response -> AstrBot bridge resolves the Promise to `data` -> render code tries to read a second `.data` level.
- **Actual:** `healthData` and observability data become `{}`, so a running system is shown as "system status unconfirmed" and observability counts are zero. Scheduler status, due-selection report, and per-chat state also render empty even when the backend returned them.
- **Expected:** Treat the bridge result as the already-unwrapped business object, or return/normalize a response shape that the page consumes consistently.
- **Production impact:** Core health and scheduler diagnostics are materially false or unavailable; operators cannot distinguish healthy, degraded, and inactive runtime states from this page.
- **Existing guard and why it fails:** `safeFetch()` handles rejected requests only. These requests succeed, while optional chaining and `|| {}` convert the shape mismatch into plausible empty diagnostics instead of surfacing an error.
- **Classification:** Plugin Pages bridge serialization/response contract.
- **Confidence:** High.

### 12-03 - P2 - Persona diagnostics use a different cache file from the live PersonaSummarizer

- **Severity:** P2
- **File:line:** `astrmai/webui/backend/paths.py:32-33`; live path assignment at `astrmai/infrastructure/persistence/persistence_manager.py:30-32`; read path consumed at `astrmai/webui/backend/services/persona_ui_service.py:23-24`.
- **Trigger:** Use the default paths (no `ASTRMAI_PERSONA_CACHE_PATH` override) after the live `PersonaSummarizer` has generated persona summary/slices.
- **Reachable production call chain:** `pages/admin/app.js:1259` -> registered `/persona/slices` at `astrmai/webui/plugin_pages.py:700` -> `PersonaUiService.get_persona_slices()` -> `PluginApiAdapter.read_persona_cache()` -> `default_persona_cache_path()` reads `plugin_data/astrmai/persona_cache.json`. The live runtime is built at `astrmai/app/bootstrap.py:293` with `PersistenceManager`, whose summarizer cache is `plugin_data/astrmai/cache/persona_cache.json`.
- **Actual:** The page reads a sibling file that the active summarizer does not write. With no stale file it reports an `ok` response containing empty/partial slices; with a stale file it reports obsolete persona data.
- **Expected:** Read the bound persistence/summarizer cache or use the same `cache/persona_cache.json` path as the runtime.
- **Production impact:** Persona ID may be current while summary, first-person rewrite, readiness, timestamp, and all eight shards are missing or stale, making the Persona Slices diagnostic unreliable.
- **Existing guard and why it fails:** Missing files are intentionally converted to `{}` by `_read_json`; `_select_cache_payload` then builds an empty payload and the service still returns `status: ok`, so neither the bridge nor page degradation UI detects the mismatch.
- **Classification:** Persistence path/runtime contract.
- **Confidence:** High.

### 12-04 - P2 - Dashboard user count queries a non-existent table and suppresses the degradation

- **Severity:** P2
- **File:line:** `astrmai/webui/backend/services/dashboard_repository.py:11`, `:16-25`, and `:79-82`; actual schema at `astrmai/infrastructure/persistence/persistence_schema.py:223`; degradation aggregation at `astrmai/webui/backend/services/dashboard_service.py:48-54`.
- **Trigger:** Open the Dashboard with one or more rows in the normal `user_profiles` table.
- **Reachable production call chain:** `pages/admin/app.js:475` -> `/astrmai/admin/dashboard` registered by `plugin_pages.py` -> `DashboardService.get_snapshot()` -> `DashboardRepository.snapshot_counts()` -> `count_table("UserProfile")` -> SQLite `no such table` -> repository catches `sqlite3.OperationalError` and returns `0` -> page renders `snapshot.total_users` at `app.js:495`.
- **Actual:** Total users is always zero for the production schema. Because the repository converts the schema failure to a valid count, `DashboardService` does not add `counts` to `degraded_components` and can report `degraded: false`.
- **Expected:** Count `user_profiles`; if a required table/query fails, preserve that diagnostic failure rather than representing it as a real zero.
- **Production impact:** The primary dashboard reports false population data and conceals database/schema degradation, undermining operational diagnosis.
- **Existing guard and why it fails:** The table whitelist permits the wrong name, and the broad OperationalError fallback erases the distinction between an empty table and a failed query before the service-level degradation guard can observe it.
- **Classification:** Repository-schema contract / diagnostics degradation.
- **Confidence:** High.

### 12-05 - P2 - Review rows omit the canonical `expression` field and display no content

- **Severity:** P2
- **File:line:** `pages/admin/app.js:1075`; producer contracts at `astrmai/learning/review/review_service.py:17-23` and `astrmai/webui/backend/services/review_ui_service.py:42-60`.
- **Trigger:** Open either Pending or All Reviews with any normal expression-review record.
- **Reachable production call chain:** `pages/admin/app.js:1067-1068` -> `/reviews/pending` or `/reviews` registered in `plugin_pages.py` -> `ReviewUiService`/`ExpressionReviewService` serializes the phrase as `expression` -> `loadReviews()` renders only `item.text || item.pattern || item.content`.
- **Actual:** The review table shows `-` in the content column even though the API item contains the expression. Approve and Reject controls remain active.
- **Expected:** Render `item.expression` (with any required legacy aliases as fallback) so the operator can see what is being reviewed.
- **Production impact:** Administrators are asked to approve or reject expression patterns without seeing their content, making the core review workflow functionally unsafe and largely unusable.
- **Existing guard and why it fails:** `asItems()` validates only list shape; the fallback chain does not include the canonical field and silently substitutes `-`.
- **Classification:** Service-view serialization contract.
- **Confidence:** High.

### 12-06 - P2 - Review pagination and totals cover only a bounded prefix, while the page exposes no navigation

- **Severity:** P2
- **File:line:** `pages/admin/app.js:1067-1068`; `astrmai/webui/backend/services/review_ui_service.py:81-125` and `:142-164`.
- **Trigger:** More than 50 review records exist, or a status/keyword match lies outside the bounded recent records fetched for the requested page.
- **Reachable production call chain:** Reviews tab -> fixed `/reviews?page_size=50` and `/reviews/pending` requests -> `ReviewUiService.list_reviews()` -> runtime fetch limited to `max(page * page_size, page_size)` or fallback SQL limited at `review_ui_service.py:91-93` -> filtering occurs after that bounded fetch -> response `total` is computed as `len(filtered)` -> page renders one table with no page/next controls.
- **Actual:** Only the first 50 all-review records and the service's default pending slice are reachable from the UI. Backend `total` describes only the fetched prefix, and post-fetch filtering can return an empty/incomplete page even when matching records exist later.
- **Expected:** Apply filters and a real `COUNT(*)` over the full dataset before limit/offset (or use a runtime repository with equivalent semantics), and expose page navigation using returned `page`, `page_size`, and `total`.
- **Production impact:** Older or less-recent review items become impossible to inspect or decide from the admin page; counts and filtered results are misleading as the corpus grows.
- **Existing guard and why it fails:** Numeric clamps prevent extreme page sizes but do not establish full-dataset count/filter semantics. The state model contains pagination fields, yet `loadReviews()` neither stores them nor renders controls.
- **Classification:** Pagination/filter/count contract.
- **Confidence:** High.

### 12-07 - P3 - Dashboard database-size field is serialized under one name and read under another

- **Severity:** P3
- **File:line:** Producer `astrmai/webui/backend/services/dashboard_service.py:65`; consumer `pages/admin/app.js:498`.
- **Trigger:** Open the Dashboard with any database file, including a non-empty production database.
- **Reachable production call chain:** Dashboard page -> `/dashboard` -> `DashboardService.get_snapshot()` returns `db_size_kb` -> bridge passes the plain snapshot -> page reads `snapshot.database_size`.
- **Actual:** The database-size metric always displays `-` despite the backend calculating a numeric size.
- **Expected:** Consume `db_size_kb` and label units, or serialize the field name the page expects.
- **Production impact:** Operators lose a basic growth/health signal; other dashboard data remains usable.
- **Existing guard and why it fails:** The `|| "-"` fallback hides the contract mismatch as missing data.
- **Classification:** View serialization contract.
- **Confidence:** High.

### 12-08 - P3 - Creating a memory event writes canonical storage that the paired event list never reads

- **Severity:** P3
- **File:line:** `astrmai/webui/backend/services/memory_ui_service.py:160-162` and `:541-620`; route registration at `astrmai/webui/plugin_pages.py:669-670`.
- **Trigger:** A Plugin Page client calls `POST admin/memories/events`, then reads `GET admin/memories/events` or opens the Memories/Events view.
- **Reachable production call chain:** bridge POST -> `AstrMaiAdminPageApi.create_memory_event()` at `plugin_pages.py:469` -> `MemoryUiService.create_event()` writes through canonical `write_service` or directly into `canonical_memories` and returns `mode: canonical_redirect` -> bridge GET/page `app.js:1117` -> `list_memory_events()` -> `MemoryUiService.list_events()` selects only legacy `MemoryEvent` rows.
- **Actual:** A successful create is absent from the corresponding event list and from the Events tab. It is discoverable only through the separate canonical-memory API, which this page does not use.
- **Expected:** The event list should include canonical event records (or the create route should return/use a resource collection that the page actually lists).
- **Production impact:** API clients and future UI actions receive success but cannot observe/manage the created event through the paired list; the current Events view presents an incomplete memory set.
- **Existing guard and why it fails:** The `canonical_redirect` marker documents the write mode but no list reconciliation follows it. The Events page unconditionally calls the legacy-only list endpoint.
- **Classification:** Route-service read/write collection contract.
- **Confidence:** High.

## Confirmed Non-Findings

- `pages/admin/index.html` is correctly discoverable at `pages/<name>/index.html`; `./style.css` and `./app.js` are relative paths and both files exist.
- Frontend plugin-local prefix `admin/...` correctly maps to registered `/astrmai/admin/...` routes under AstrBot's bridge contract. A missing `/astrmai` prefix in `app.js` is not a defect.
- Every `api.get()`/`api.post()` path used by the current page has a corresponding registered Plugin Pages route and compatible HTTP method, including POST aliases for actions where the page does not use PUT/DELETE.
- The Quart request proxy used by `plugin_pages.py` remains an officially supported compatibility path in the current Plugin Pages runtime; the wrapper's use of that proxy was not reported as a framework migration defect.
- The current Plugin Page exposes no config-edit route or settings UI. Hot-config code in the separate FastAPI route tree has no normal `main.py`/Plugin Pages call path, so no hot-config finding was confirmed for the shipped page.

## Paths Reviewed

- `main.py` Plugin Pages registration and adjacent `astrmai/app/plugin_facade.py`, `runtime_facade_protocol.py`, and `bootstrap.py` contracts needed for reachability.
- `astrmai/webui/plugin_pages.py`.
- `astrmai/webui/backend/`: `db.py`, `paths.py`, `repositories.py`, `schemas.py`, `server.py`, adapter code, all route modules (excluding auth behavior), and all service modules.
- `pages/admin/index.html`, `pages/admin/app.js`, and `pages/admin/style.css`.
- Adjacent production contracts used to prove findings: persistence schema/manager, live user-profile cache, review service, memory store accessors, runtime coordinator, observability, scheduler/chat-loop, learning/proactive runtime, and persona summarizer wiring. `astrmai/infrastructure/security/` was not opened.

## Verification

- `node --check pages/admin/app.js` passed.
- All 42 Python files under `astrmai/webui/` (excluding `venv`) parsed successfully with `ast.parse` using `python -B`; this did not create bytecode.
- Static asset references and all current page bridge call paths were cross-checked against the registration table.
- No tests were read or run, in accordance with the assignment.
