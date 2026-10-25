# Codex review: memory foundation

Date: 2026-10-25, Europe/Kyiv.

Decision: approved

Current approval: task 01r2 reviewed on 2026-10-25. Approved to proceed to task 02, with the minor empty-message report counter assigned as its first repair. This approves the POC memory foundation, not the entire product, all HTTP tests, human checkpoints or shipping readiness. Earlier findings below are historical; see the final review section for current evidence and limitations.

Scope: actual `app/`, `tests/`, planning artifacts, runner, and agent report. Application source was not edited by the reviewer; the user requested console agy as the code author.

## Independently verified

- `.venv/bin/python -u -m pytest -vv -s tests/test_config.py tests/test_importer.py tests/test_retrieval.py`: **18 passed**, one Starlette deprecation warning, exit 0.
- `.venv/bin/ruff check .`: **passed**, exit 0.
- The complete suite and the six HTTP tests stalled in `TestClient.__enter__` before the first API test executed. A separate bounded run with `-o faulthandler_timeout=8` was terminated by a 20-second timeout, exit 124. Stack showed the blocking portal waiting in `starlette.testclient.__enter__`/AnyIO. Cause is not established; do not label these six tests passed or infer an application defect from this environment alone.
- Additional synthetic in-process probes confirmed the findings below. No real credential was printed and no Gemini/Telegram call was made.

## Required changes before task 02

### R1 — P1: origin check accepts lookalike remote domains

Location: `app/main.py:92`.

The middleware uses prefix comparison. `http://localhost.attacker.invalid` passes the local-origin check. An in-process call to `verify_local_origin` with this Origin and method DELETE reached the downstream handler (`True`). `http://127.0.0.1.attacker.invalid` has the same problem.

Use a parsed exact origin tuple (scheme, hostname, effective port) and an explicit allowed-origin policy tied to the configured local service. Reject lookalike hosts, user-info tricks, malformed origins and `null`; restrict allowed ports/schemes as documented. Keep non-browser requests with no Origin usable. Add regression tests for exact and deceptive origins.

### R2 — P1: secret redaction covers repr but not str/serialization

Location: `app/config.py:18`, `app/config.py:31`, `app/config.py:67`.

With synthetic secrets and `_env_file=None`, `repr(settings)` was redacted, but `str(settings)` and `settings.model_dump_json()` contained the raw synthetic key. This is a redaction gap, not evidence that the user's real credential has leaked.

Use secret-aware types and explicit extraction only at the provider/Telegram boundary. Test repr, str, JSON serialization and the public safe view with synthetic secrets. Keep environment-backed tests hermetic; do not read/display the real `.env` in agent context.

### R3 — P2: malformed IDs cause unexpected exceptions or identity coercion

Location: `app/importer.py:145-160`, reply reference conversion at `app/importer.py:223-229`.

Actual probes: object message ID -> `TypeError`; oversized integer -> `OverflowError`; float `1.5` and boolean `true` -> accepted through `int()`. `int()` coercion can truncate a source identity and create false collisions. Nested/list reply IDs have the same unhandled-type risk. Certain metadata types also reach SQLite/Pydantic without adequate validation.

Validate record shape and ID domain before insertion. For this schema, accept exact signed-64-bit integers and documented decimal integer strings, reject bool/fractional values/objects/lists/out-of-range values, and return stable import errors. Validate important string fields and text parts without coercing arbitrary objects into pretend chat text. Ensure failures are atomic and add HTTP-level error tests.

### R4 — P2: malformed records disappear while coverage can claim 100%

Location: `app/importer.py:145-147`, `app/main.py:245`.

Importing `messages: [valid_message, 17]` returned `total_records=2` and `total_messages=1` without an excluded-record count. Detail coverage is hardcoded to 100% whenever anything was stored. This violates the project's central requirement of honest full-history accounting.

Prefer rejecting malformed records with indexed validation errors. Alternatively add explicit, reasoned excluded counts and compute coverage from a documented denominator. No silent omission. Define one supported-text predicate and use it consistently in report, list/detail, re-import, retrieval and future analysis: a synthetic text-bearing sticker currently reports one supported text message but returns zero search results. Whitespace-only strings also need consistent treatment across Python `.strip()` and SQLite `trim()`.

### R5 — P2: naive export dates are silently relabeled UTC

Location: `app/importer.py:44-55`, caller at `app/importer.py:172`.

A naive `2026-01-01T12:00:00` is assigned `+00:00`; `date_unixtime` is not read. A missing date is replaced by the current import time, inventing an archival timestamp. This can corrupt chronology/context even while raw text is preserved.

Prefer a valid `date_unixtime` as the absolute instant, preserve original date text, and use explicit offset-aware dates when available. For dates with no unambiguous timezone, require/document an import-time timezone or record unknown rather than guessing UTC/current time. Earliest/latest comparisons must use valid normalized instants only. Include synthetic mismatched local-date/epoch, offset-aware, missing and invalid date tests.

### R6 — P2: upload limit is checked after an unbounded read

Location: `app/main.py:185-198`.

`await file.read()` reads the complete upload before the size check. This is not an in-memory bound for oversized exports. Heavy synchronous normalization/SQLite insertion also runs directly in an async endpoint.

Read at most limit+1 bytes (or a bounded stream), close uploads, and reject early. Document separately whether the limit also bounds multipart-body handling; do not claim a total request-body cap unless enforced before spooling. Move expensive synchronous work into a worker thread or an appropriate synchronous route. Add a meaningful fake-stream test proving the bounded read and an oversized request test.

### R7 — P2: fallback search treats percent as a wildcard

Location: `app/retrieval.py:64-83`.

For an archive containing `abc` and `100% yes`, query `%` returned both. The fallback uses SQL LIKE without escaping. This is a literal-search correctness bug, not SQL injection.

Escape LIKE wildcards and escape characters with a matching ESCAPE clause, or use a literal substring operation. Bound the query length. Test percent, underscore, escape characters, quotes, emoji and combined content while preserving chat isolation.

## Documentation/process corrections

- The agent marked the early learner hands-on checkpoint complete based on its own tests. Mechanical verification does not establish that the user tried the app. Reopen the checkpoint unless actual user feedback exists.
- The report and metadata attribute planning approval to Codex before this independent review. The underlying product/architecture remains the orchestrator's intended plan, but do not use those metadata as proof of code approval or learner interaction.
- `git status` currently reports no Git repository. No verified commit/checkpoint exists. Do not claim the official build workflow is fully completed; a `.gitignore` alone provides no current tracking state.
- The supplied `Pasted text.txt` attachment was resolved by matching its exact file ID, but its text extraction was empty and downloading failed in this restricted environment. Its contents have not been read. This review is based on the actual workspace source and agent report, not the unreadable attachment.

## Next action

Run `bash scripts/agy-task.sh 01r` from a normal local terminal. The existing execution environment still prevents starting console agy's language server. The orchestrator has prepared the repair task; it has not been executed here. Task 02 remains blocked until a new independent review records `Decision: approved`.

---

## Independent re-review after task 01r

Date: 2026-10-25, Europe/Kyiv.

Decision at that re-review: changes_required

The preceding review is historical. Task 01r has now been implemented by agy. Its report claims 40 passing tests in a normal local terminal; the following is the independent result, not a repetition of that claim.

### Verified improvements

- `.venv/bin/python -u -m pytest -q tests/test_config.py tests/test_importer.py tests/test_retrieval.py`: **32 passed**, one deprecation warning, exit 0.
- `.venv/bin/ruff check .`: **passed**, exit 0.
- The origin lookalike is rejected; the expected local origin is accepted.
- Synthetic secret tests verify SecretStr redaction through repr, str and JSON serialization.
- Invalid source IDs now return a stable importer error rather than the original TypeError.
- Epoch takes precedence: local `2026-01-01T12:00:00` with epoch `1767261600` becomes `2026-01-01T10:00:00+00:00`; absent dates remain unknown.
- A direct asyncio bounded-upload probe consumed exactly 101 bytes for a 100-byte limit, closed the stream and raised HTTP 413.
- The selected v1-to-v2 migration test passed before the next selected AnyIO test stalled. That combined run timed out (exit 124); it is not a passing complete test run. The fake bounded stream was subsequently verified directly without the pytest AnyIO fixture. Root cause of the sandbox/AnyIO stall remains unestablished; the agent's claim that it is definitively caused by socket restrictions is not independently supported.

### Three remaining concrete fixes

**R1b — malformed origin ports still raise ValueError.**

In `app/main.py:is_allowed_origin`, `urlsplit()` is inside the try, but evaluating `parsed.port` is outside. Direct calls with `http://localhost:bad` and `http://localhost:99999` both raised ValueError instead of returning False. Wrap property evaluation/validation too, reject malformed ports with the normal 403 path, and test those cases directly and at the HTTP boundary.

**R3b — top-level chat metadata is not validated before SQLite insertion.**

`TelegramImporter.import_chat_json` still passes top-level `name` directly into SQLite. Importing `{"name":{"nested":1},"messages":[]}` raised sqlite3.ProgrammingError rather than a stable 4xx importer error. Validate top-level name, override and source chat ID shapes before persistence and preferably before the idempotent early return. Do not coerce arbitrary objects/lists/bools into strings. Test representative invalid names/source IDs and assert the DB is unchanged after rejection.

**R4b — migration uses a different supported-text predicate.**

Migration 2 sets `is_supported_text` using SQLite trim over space/tab/newline/carriage return. The current importer additionally handles Unicode whitespace and zero-width formatting characters. A synthetic v1 DB containing `U+200B`, `U+00A0`, vertical tab and normal text produced **three predicate mismatches** after migration. This directly affects future full-history coverage counts.

Use the canonical shared predicate when populating/migrating flags. Also repair databases that already applied v2 via an additive migration or explicit versioned repair; editing v2 alone will not update those files. Refresh affected supported-text report counts without inventing original import coverage or losing exact text, source IDs, reply references or FTS mappings. Test v1 upgrade, already-applied-v2 repair, and fresh database consistency on Unicode/zero-width/sticker/service cases. Run migrations again to verify they are idempotent.

### Next action after re-review

Run `bash scripts/agy-task.sh 01r2`. This is a narrow finish to the same repair, not a new feature phase. Fix only R1b/R3b/R4b, add focused regressions, run checks, and stop for Codex re-review. Do not restart the plan or implement task 02 yet. Application code remains assigned to console agy.

The new `Pasted text(1).txt` was matched to its exact supplied file ID, but extracted text is again unavailable. This re-review uses the actual workspace code and `orchestration/reports/01r-foundation-repair.md`, not presumed attachment contents.

---

## Final foundation review after task 01r2

Date: 2026-10-25, Europe/Kyiv.

Decision: approved

### Independently verified

- `.venv/bin/python -u -m pytest -q tests/test_config.py tests/test_importer.py tests/test_retrieval.py tests/test_migrations.py`: **37 passed**, one Starlette deprecation warning, exit 0.
- `.venv/bin/ruff check .`: **passed**, exit 0.
- Direct synthetic probes confirmed invalid/out-of-range/negative origin ports and lookalike remote hosts return False; the intended local origin is accepted.
- Invalid composite chat name and source ID now raise the expected stable ImporterError codes and leave chats/messages empty.
- Dedicated migration tests cover fresh databases, v1 upgrade, applied-v2 repair, preserved identities/text/replies/FTS, corrected supported-text flags and repeated runs.

The coding agent reports **47** total tests passing in its ordinary local terminal, and the user supplied that terminal summary directly. The reviewer independently verified the 37-test non-HTTP/migration subset above. Full HTTP/AnyIO execution remains unverified in this restricted review environment, where it previously stalled; no definite root cause is asserted.

### Minor report counter follow-up, assigned to task 02 preflight

In `migration_3_repair_supported_text`, `actual_empty` counts unsupported `kind='message'` rows but omits rows already classified as `kind='empty'`. A synthetic import containing one genuinely empty record reported `empty_messages=1`; explicitly executing the repair changed that counter to 0. Supported-text flags, stored text, identities and quote retrieval remain unaffected, so this report-only mismatch does not block starting the next phase.

Task 02 must first correct this counter, cover already-applied-v3 databases with a versioned repair if needed, and add a focused regression for native empty rows together with reclassified whitespace rows. Verify it before implementing analysis. Do not claim it is fixed until that code runs and passes checks.

### Handoff

The outstanding blocking findings R1-R7 and R1b/R3b/R4b are resolved sufficiently for the planned POC foundation. The next implementation assignment is `orchestration/tasks/02-intelligence.md`. Keep code changes with console agy; Codex retains architecture and independent review. User hands-on feedback and Git checkpoints are not fabricated or marked complete by this code approval.
