# Task 02: full-history analysis and grounded replies

You are the console agy coding agent implementing Codex's second bounded slice. Read all `docs/`, `devpost/`, actual application source and the first task report before editing. Do not read `.env`, dump environment variables or expose credentials. Do not spawn other agents. Do not publish, deploy or send Telegram messages.

Codex has now approved task 01r2 for progression to this task. Read the **Final foundation review after task 01r2** section of `orchestration/reviews/01-foundation.md`; earlier changes_required decisions in that document are historical. Do not restart or reimplement the foundation. Keep using official `5-build` workflow and preserve existing owner-edited planning/profile content. Code approval does not claim human hands-on feedback or Git checkpoints occurred.

## First repair before analysis implementation

Fix the nonblocking migration report counter from the final review: `migration_3_repair_supported_text` counts unsupported `kind='message'` rows as empty but omits native `kind='empty'` rows. A direct synthetic check changed `empty_messages` from 1 to 0 after repair. Count native empty plus reclassified whitespace consistently, preserve all other source statistics and data, and include a versioned repair for databases that have already applied v3; editing an applied migration alone is insufficient. Add a focused regression using both native empty and whitespace-only records, run the relevant tests and fix failures before proceeding to the AI work below. This is part of task 02, not a request to restart the previous repair phases.

## Implement

1. Typed async provider interface and production Gemini adapter using current official SDK documentation. Configurable model, bounded timeout/retries/concurrency, structured output validation and sanitized provider errors. Fake provider is test-only.
2. Ordered chunk plan for every supported imported text message; evidence-bearing findings, bounded hierarchical merge, durable chunk ledger, honest unique-message coverage, call cap, pause/cancel/resume and restart recovery. No silent sampling.
3. Estimate/start/progress/cancel analysis endpoints. Analysis happens only after an explicit request. Missing credential/model unavailable is a clear state, never fake success.
4. Evidence-backed group profile, versioned owner edits, optimistic conflict handling, and credential-free exportable instructions. Validate every evidence ID against the active chat.
5. Grounded reply service with quote-only (no provider needed), generated, auto/silent actions. Resolve quote text/author server-side from validated candidates. Keep destination and executable actions outside model control.
6. Feedback persistence with bounded ranking impact and repeat/overuse handling.
7. Offline tests covering all second-slice acceptance cases including multiple chunks, partial coverage/resume, invalid provider output, fabricated/cross-chat IDs and prompt-injection source messages.
8. An opt-in tiny synthetic live Gemini smoke command that reads the credential only inside application runtime and prints no credential or raw provider request. Do not use real chat exports. If the environment permits the authorized small smoke call, run it once and report the sanitized result; do not repeatedly consume quota on permanent errors.

## Review boundary

Run relevant tests, fix failures, and write `orchestration/reports/02-intelligence.md`. Distinguish mocked/offline success from actual live provider verification. Stop for Codex review. Do not implement the final UI/Telegram slice yet. Report API changes before dependent work.
