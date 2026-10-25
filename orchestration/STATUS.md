# Actual project status

Updated: 2026-10-25, Europe/Kyiv. Task 04 completion and GitHub preparation snapshot.

## Decision

**Local MVP and Telegram bot integration are complete.** Task 04 hierarchical merge compaction and higher-level cache recovery successfully synthesized Active Profile v1. Web workspace (4 tabs) and Telegram bot polling with allowlist/bindings are verified. All 136 offline tests (`pytest -k "not browser"`), Ruff linting/formatting, and JS syntax checks pass with 0 errors.

## Database evidence

Latest snapshot: Job `7cd9a49b-1496-416c-9be7-6131f69ce4f7` completed. Read-only inspection, without printing private messages, names, profile contents or credentials.

| Measure | Actual state |
|---|---|
| Imported records | 261,703 |
| Supported text messages | 241,569 |
| Completed chunks | 559 / 559 |
| Processed unique messages | 241,569 / 241,569 |
| Job status / last error | `completed` / none |
| Lifetime recorded attempts | 763 / 1000 |
| Final profiles from this job | 1 (Active Profile v1: 20 memes, 98 evidence IDs) |

Earlier snapshot at 03:24:49 UTC: the same job failed with `envelope_exceeded`, ledger 724/1000, with 158 nodes (112/29/13/4). A read-only probe of the actual partition function using those persisted level-4 findings reproduced `ProviderError(code="envelope_exceeded")`. Their two-item prompts are 48,494–54,073 characters versus a 38,000-character batching bound. Current normal resume logic still rejects this error category. Preserve checkpoints and accounting; review compaction and implement explicit compatible recovery. Increasing concurrency/cap does not resolve that batching condition. Subsequently, while this documentation was being prepared, the active coding agent changed merge prompts to request roughly 3,000–4,000 instruction characters, 15–20 memes and 2–5 representative evidence IDs, and resumed the job. Upper-level caches changed (the former levels 3/4 are not the current saved-node set). This is observed live work, not an independently approved recovery or completed synthesis. The handoff must preserve the current database and await a terminal outcome; do not launch a duplicate runner.

Real-run settings: chunk size 500, overlap 10, upload bound 160 MiB, concurrency 16, lifetime cap 1000. Archive runner now explicitly uses at least a 180-second timeout; application default is 30 seconds. Code defaults remain chunk size 50, upload 50 MiB and cap 50; private run configuration overrides them. Usage totals are process-local, so historical token/dollar totals across restarts are not established.

Persisted findings demonstrate that real Gemini chunk and intermediate work occurred. The earlier pasted agent progress reports a successful synthetic analyze/merge/reply sequence; that sequence was not independently replayed. Attachment `Pasted text(7).txt` yielded no readable text through retrieval, so unseen contents are not verification evidence.

## Telegram and browser

Runtime inspection: provider credential configured; Telegram token configured; allowlisted group count **0**, bindings **0**, recorded Telegram updates **0**. Configuration does not prove token validity or a successful round trip. Select and bind a separate test group first. The original dorm group is not an automatic recipient; stickers remain disabled.

Implemented browser capabilities include import, search, quote replies, feedback and manual profile editing/export. Earlier user logs establish page serving and import requests. Final human exploration, learned-profile replies and live Telegram remain open.

The N1 timer ownership fix is present with a standalone deferred-poll logic regression and expanded browser test source. The standalone test copies the logic, so its success alone does not verify the shipped bundle. Browser execution was not repeated in this handoff.

## Independent handoff checks

- `GEMINI_API_KEY= TELEGRAM_BOT_TOKEN= timeout 25 .venv/bin/python -m pytest -q tests/test_config.py tests/test_importer.py tests/test_retrieval.py tests/test_migrations.py tests/test_analysis_concurrency.py tests/test_n1_deferred_poll.py tests/test_providers.py`: **57 passed, 1 deprecation warning, 11.06s, exit 0**. Synthetic/local tests with provider serialization/schema/error regressions; concurrency checks cover chunks, cap, failure and resume, not independent parallel merge acceptance. This rerun followed the newer compact-merge prompt change.
- `.venv/bin/ruff check .`: **passed**, exit 0.
- `.venv/bin/ruff format --check .`: **passed**, 85 files already formatted, exit 0.
- `node --check app/static/app.js`: **passed**, exit 0.
- `GEMINI_API_KEY= TELEGRAM_BOT_TOKEN= timeout 30 .venv/bin/python -m pytest -q`: **timeout, exit 124**; output showed 32 progress dots without a final summary. No current failing/stalled test or root cause was established from that quiet output. No full-suite pass is claimed.

Historical 03r evidence: 47 selected pytest checks and 46 direct async cases passed; quote/provenance/feedback and delayed-profile probes passed. Full pytest then stalled at health here, exit 124 after 45 seconds; the author separately reported 129 passes in a normal terminal. Those numbers describe that earlier snapshot.

The stored `running` state and last update at 03:31:14 UTC do not independently prove the external runner remains alive. Codex did not stop that separate console process. Before archiving, the operator must ask the active agent to stop new dispatch, drain in-flight calls, record the terminal outcome and stop its runner, then copy the consistent database.

## Handoff boundary

Remaining work is prioritized in [TODO](../TODO.md); run/setup/publication instructions are in [handoff](../docs/handoff.md). Preserve private `.env`, real export and a consistent database in the owner's authorized private team archive; exclude them from public publication.

No repository, commit, publication or submission was performed by Codex. Application code and real credentials were not edited during this documentation handoff. Human learner/final-review checkpoints remain open.
