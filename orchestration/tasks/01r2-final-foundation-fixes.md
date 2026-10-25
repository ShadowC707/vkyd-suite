# Task 01r2: finish three remaining foundation repairs

You are console agy, the application code author. Codex is architect/reviewer. Read the **Independent re-review after task 01r** section in `orchestration/reviews/01-foundation.md`, affected code, tests and the last report. The overall idea, architecture and scope remain authorized. Do not restart interviews or regenerate the application.

Fix only these three observed issues:

1. **R1b:** `is_allowed_origin('http://localhost:bad')` and `is_allowed_origin('http://localhost:99999')` raise ValueError because parsed property validation is outside the try. Return False for malformed origins and ensure the middleware returns the usual 403 without reaching the handler. Add focused direct and HTTP regression tests.
2. **R3b:** Importing `{"name":{"nested":1},"messages":[]}` raises SQLite ProgrammingError. Strictly validate top-level chat name, explicit name override and source chat ID shapes before insertion/early-return behavior. Return a stable importer 4xx error for invalid metadata. Do not convert composite objects, lists or booleans to pretend names/IDs. Test malformed inputs and atomic DB non-modification.
3. **R4b:** Migration 2's SQLite-trim predicate differs from the current canonical Python supported-text predicate. Repair flags and relevant report counts for both v1 upgrades and already-migrated-v2 DBs through a safe additive migration/versioned repair. Preserve raw text, identities, replies and FTS consistency. Share one predicate implementation without circular imports. Test Unicode whitespace, zero-width content, ordinary text, service messages, text-bearing stickers, fresh DBs, v1 upgrades, applied-v2 repair and repeated migration runs. Do not invent archival coverage or reset user data.

The prior 32 non-HTTP tests passed independently and must keep passing. The existing migration test passed; the reviewer's selected AnyIO test stalled, while a direct asyncio fake-stream check succeeded. Do not claim a definite root cause for the test-runner stall without an actual isolated reproduction proving it. Run the whole offline suite in your normal local terminal with a sensible timeout, plus Ruff, and record exact outcomes honestly.

Do not read `.env` into agent prompts, dump the environment, expose credentials, make live API calls, spawn agents, push, deploy or message people. Do not broaden scope. Keep the learner hands-on checkbox open unless actual feedback exists. No fabricated prior approvals/commits.

Update only affected documentation/contracts and write `orchestration/reports/01r2-final-foundation-fixes.md` with changes, regression commands/results and migration behavior. Stop for Codex review; do not start task 02 or self-approve the review.
