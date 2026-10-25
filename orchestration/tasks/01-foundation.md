# Task 01: official workflow and memory foundation

You are the console agy coding agent. Codex is the architect and reviewer. The user explicitly requested that you write application code. Implement this slice; do not just propose a plan.

## Read first

Read `devpost/scope.md`, `devpost/prd.md`, `devpost/spec.md`, `docs/architecture.md`, `docs/contracts.md`, `docs/acceptance.md` and `.env.example`. Do not read `.env`, dump the environment, search home directories, print tokens, or include credentials/private exports in any report. Application code reads environment variables at runtime; the agent does not need to see their values.

## Required official skills

The user targets Build With AI: Basics. Use the installed official Devpost Learn Skill Pack, starting with `1-start`, routing through `2-scope`, `3-prd`, `4-spec`, then the relevant `5-build` slice. Existing documents are planning inputs, NOT proof the skills ran. Use the supplied idea/decisions as context and follow real skill instructions. Do not fabricate interviews, explicit approvals, learning reflections or completed skill steps. Record what actually ran. If required skills are missing, stop and report the exact installation issue instead of generating fake substitutes.

Routine implementation choices are authorized. Ask only if a missing product decision materially blocks work. Preserve the user's architecture/reviewer arrangement and this task boundary. Do not create/update a personal skill or change globally installed skills.

## Implement

1. Python project setup, real resolved dependencies and lockfile where available, localhost FastAPI app, environment configuration and SQLite migrations.
2. Single-chat Telegram JSON importer with rich-text normalization, source identities, context/reply references, honest import report and atomic transactions.
3. Chat-isolated SQLite FTS5 retrieval and safe literal search for Ukrainian/emoji content; empty/no-token search works without SQL errors.
4. Health, list/detail/import/delete chat and memory search endpoints in `docs/contracts.md`.
5. Two synthetic exports with different chat styles and overlapping source message IDs.
6. Meaningful offline tests for import round trip, rich text fidelity, isolation, idempotency, deletion including FTS, malformed input, limits and hostile search syntax.
7. Update README with actual setup/test commands and state. Preserve truthful statements about unimplemented later slices.

## Constraints

- Do not implement AI generation, Telegram sending or the complete UI in this slice.
- No external calls are necessary except resolving packages. Do not use the Gemini key or send messages.
- Never copy old app code from another user project. Use normal dependencies freely.
- Do not change architecture/contracts silently. Explain necessary deviations in the report.
- Do not spawn additional agents; do not publish, commit/push, deploy or message anyone.
- Keep all changes in this directory. Avoid generated dependency folders in source packages.

## Check and report

Run the relevant offline tests and compilation/type/lint checks appropriate to the source actually written. Fix failures. Write `orchestration/reports/01-foundation.md` with changed files, exact commands/results, actual official skill steps, blockers and next-step recommendation. No secrets. Do not mark the entire project complete. Stop for Codex review after this slice.
