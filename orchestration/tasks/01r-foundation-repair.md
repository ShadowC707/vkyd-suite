# Task 01r: repair independently reproduced foundation defects

You are the console agy code author; Codex remains architect/reviewer. The original implementation is present and must be fixed forward, not regenerated. Read `orchestration/reviews/01-foundation.md` fully, then the affected application code and tests. Preserve architecture and user work. Do not read `.env` into agent context, dump environment variables, print secrets, spawn agents, publish, push, deploy, contact people or call live Gemini/Telegram services.

## Work

Implement R1-R7 from the reviewer document. Each has an observed failure and a required behavior; add regression tests for those behaviors rather than mirroring implementation. Keep existing tests passing. Update affected API/model/schema contracts only where the fix requires it, explain changes, and avoid unrelated refactors.

1. Exact parsed origin checks; reject remote lookalikes, malformed/null origins and unexpected scheme/port while keeping intended local browser and no-Origin CLI flows working.
2. Secret-aware configuration redaction across str/repr/normal JSON serialization; extract credentials only at runtime integration boundaries. Test synthetic keys only and use `_env_file=None` to avoid local configuration dependence.
3. Strict source/reply ID validation with sensible SQLite integer bounds; stable errors for malformed record/metadata/text shapes. No silent float truncation or bool conversion.
4. No silent malformed-record skipping; truthful coverage and one supported-text definition shared by import reports, list/detail, reimport, search and upcoming analysis. Retain sticker metadata; if sticker captions count as supported text, make them retrievable too. Handle Unicode/ASCII whitespace consistently.
5. Correct absolute date normalization using export epoch when valid; preserve raw local date, define unknown/naive-date policy without inventing current time, and test chronology. If this requires a nullable timestamp or another migration, migrate existing local DBs without dropping them.
6. Bounded upload read and cleanup; no event-loop blocking for the heavyweight synchronous importer. Explain multipart/body/spooling limits honestly.
7. Literal wildcard/emoji fallback search and a bounded request query length.

## Documentation and evidence

- README must no longer claim independent approval of the original slice. Report actual checks and explicit unverified areas.
- The early learner hands-on checkbox was reopened by the reviewer; do not check it off merely because tests pass.
- Do not fabricate past interviews, human feedback, approvals or commits. The folder was not a Git repository at review time. Respect the user's local permission and publication scope; no destructive Git actions and no push.
- The official organizer's installed skill instructions may guide repair, but the user has already authorized architecture and agy coding. Do not restart the idea interview or introduce an unrelated approval flow.

## Verify

Run the offline test suite and Ruff. If HTTP tests hang, use a bounded timeout and record the first blocked stage/diagnostic. Do not mark a hung or unexecuted test passed. Distinguish running in a normal local terminal from the orchestrator's restricted sandbox.

Write `orchestration/reports/01r-foundation-repair.md` with changed files, R1-R7 resolution, exact commands/results, any migrations and remaining limitations. Stop for Codex independent re-review. Do not begin task 02 or self-approve the review.
