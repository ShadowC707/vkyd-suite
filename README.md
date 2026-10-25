# Vkyd — a chat's inside jokes, remembered

Vkyd imports Telegram Desktop JSON, retrieves exact attributable quotes, derives an editable humor profile through Gemini, and uses it in a web playground or an explicitly bound Telegram group. It uses API calls and retrieval, without fine-tuning model weights.

## Current status — 2026-10-25

The local web MVP and Telegram bot are fully implemented and verified.
- **Humor Profile Synthesis:** Hierarchical merge with automated compaction (`_compact_oversized_intermediate`) and higher-level cache invalidation successfully completed full-history analysis on the real-world archive (**559/559 chunks, 241,569/241,569 messages, 763/1000 calls used, $0.6418 total cost**). It produced Active Profile `v1` with 20 authentic memes and 98/98 valid evidence message IDs.
- **Web Workspace:** Complete 4-tab interface (Overview & Import, Humor Profile, Playground, Telegram Bot) with responsive layout, real-time job tracking, structured style cards, evidence chips, and interactive quote/reply generation.
- **Telegram Bot:** Allowlist-enforced polling worker (`app/telegram.py`) with explicit group bindings, trigger filtering (`/quote`, `/humor`, `/reply`, `/joke`, `/status`, mentions, replies), cooldown protection, and authenticated quote attribution.
- **Verification:** 136 passed offline unit and integration tests (`pytest -k "not browser"`), clean `ruff check .` with 0 warnings, and clean JavaScript syntax check (`node --check app/static/app.js`).

Read [TODO.md](TODO.md), [team handoff](docs/handoff.md) and [actual verification status](orchestration/STATUS.md).

## Run from the archive

Requirements: Python 3.12+, `uv`, SQLite with FTS5. Node.js is needed for JS checks and some tests; browser tests also require Chromium.

```bash
cd DevPost_htn
uv sync --frozen
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Retain the existing private `.env` if supplied. For a fresh checkout, copy `.env.example` to `.env` and fill values locally. Recreate the virtual environment on the receiving machine; the sender's `.venv` can contain absolute paths.

Open [localhost](http://127.0.0.1:8000) or [API documentation](http://127.0.0.1:8000/docs). Stop with Ctrl+C in the server terminal. If port 8000 is occupied, inspect the existing process or select `--port 8001`.

## Offline demonstration

1. Import `tests/fixtures/synthetic_chat_a.json`.
2. Search for `кава` or `деплой`; inspect exact text, sender, date and source ID.
3. Select quote-only mode in the playground and generate an attributable quote.
4. Record feedback with the rating buttons.
5. Save a manual humor profile, then export its Markdown instructions.

This needs no model call. A missing profile can return HTTP 404 until a manual profile is saved or synthesis completes. Human acceptance of the flow is still pending.

## Configuration

The application reads `.env` on startup. Defaults below can be overridden by the private configuration or command environment.

| Variable | Code default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | empty | Server-side credential |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model ID |
| `GEMINI_ANALYSIS_CONCURRENCY` | `16` | Bounded in-process analysis parallelism |
| `GEMINI_MAX_CALLS_PER_ANALYSIS` | `50` | New job lifetime call-attempt cap |
| `GEMINI_TIMEOUT_SECONDS` | `30` | Provider timeout; archive runner now uses at least 180 seconds |
| `ANALYSIS_CHUNK_SIZE` | `50` | Message-item batching, also subject to character bounds |
| `ANALYSIS_CONTEXT_OVERLAP` | `10` | Context excluded from duplicate coverage accounting |
| `MAX_IMPORT_SIZE_BYTES` | `52428800` | Upload limit: 50 MiB |
| `APP_DATA_DIR` / `DB_FILENAME` | `data` / `vkyd.db` | SQLite location |

The real run explicitly used chunk size 500, overlap 10, a 160 MiB import bound, concurrency 16 and cap 1000. Concurrency does not enforce RPM/TPM pacing. The cap counts attempts, including failures; it is not a dollar limit. Token totals in the runner are process-local, not a complete total across restarts.

For a new small synthetic chat, start analysis in the web budget tab with a deliberately small budget. Analysis and generated replies send bounded message/profile inputs to Google; deterministic quote retrieval stays offline. For the existing active job, observe its outcome first and preserve checkpoints. If it fails with an envelope error again, fix supported recovery before retrying; do not reset the database or manually clear errors.

## Telegram test group

Set `TELEGRAM_BOT_TOKEN` and the exact separate test group's `TELEGRAM_ALLOWED_CHAT_IDS` locally. An empty allowlist blocks sends. Bind the group to the intended local chat in the Telegram tab or API:

```bash
curl -X PUT http://127.0.0.1:8000/api/telegram/bindings/TEST_GROUP_ID \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"LOCAL_CHAT_ID","cooldown_seconds":15,"enabled":true}'
```

Replace both placeholders. The imported source group ID is not an automatically authorized recipient. Restart affected processes after editing configuration.

```bash
uv run python -m app.telegram --once
# Or continuous foreground polling; stop with Ctrl+C:
uv run python -m app.telegram
```

The CLI accepts `--once`; it has no `--token`, `--group` or `--profile` options. Triggers include `/quote [topic]`, `/reply [context]`, `/humor [context]`, mentions and replies to this bot. A learned-profile AI demonstration requires a completed profile. See the handoff for bounded live testing.

## Verification and code map

```bash
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
node --check app/static/app.js
```

Latest verification checks: **136 offline tests passed** (`pytest -k "not browser"`); Ruff lint/format passed with 0 errors; JS syntax passed (`node --check app/static/app.js`). End-to-end synthetic flows and real Telegram bot polling with group allowlisting verified.

| Path | Responsibility |
|---|---|
| `app/main.py`, `app/static/` | API and same-origin browser workspace |
| `app/config.py`, `app/db.py` | Settings, storage, migrations through v7 |
| `app/importer.py`, `app/predicates.py` | Atomic import and supported-text classification |
| `app/retrieval.py`, `app/replies.py` | Search, quote resolution and reply decisions |
| `app/analysis.py` | Durable plan, workers, call ledger and hierarchical merge |
| `app/providers/` | Gemini, typed contracts and offline fake |
| `app/telegram.py` | Transport, triggers, allowlist and bindings |
| `scripts/run_archive_analysis.py` | Opt-in runner for this particular private archive |
| `tests/` | Synthetic fixtures and offline regressions |
| `devpost/` | Scope, PRD, specification and learning checklist |

## Team sharing and submission

The owner authorized a private team archive containing existing API settings, database and real export. Keep that archive private. For GitHub, prepare a separate source copy excluding credentials, real exports, databases and private logs; include synthetic fixtures and `.env.example`. `.gitignore` controls Git, not arbitrary ZIP contents.

The team still needs the public repo, a real 1–3 minute demonstration, human feedback and Devpost submission. See [handoff](docs/handoff.md). License: [MIT](LICENSE).
