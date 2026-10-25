# Implementation contracts

These interfaces are the orchestrator's proposed baseline. A coding agent may make a small compatible improvement, but must report a changed contract before dependent work relies on it.

## Data conventions

- Internal IDs: generated UUID strings. Telegram source message IDs: retained integer or normalized string; unique only within a chat.
- Times: UTC ISO 8601 on the API; nullable when timezone cannot be resolved without guessing; raw export date is always retained in raw_date.
- Source text: exact concatenation of Telegram text parts with no whitespace rewriting. Supported text is defined by `is_supported_text=1` (non-service records with non-whitespace content across ASCII whitespace, Unicode whitespace, and zero-width characters). Text-bearing stickers retain sticker metadata and are retrievable in search. Migrations and repairs use the shared canonical predicate and refresh report counts without modifying raw text or resetting user data. Additive migration v4 repairs the empty_messages counter in chats.import_stats_json to consistently count both native empty (kind='empty') and reclassified whitespace records.
- Error response: `{ "error": { "code": "machine_code", "message": "safe explanation" } }`. No exception dump, credential, private filesystem path or request headers in browser responses.
- All lists have bounded pagination. All request text and upload sizes have explicit bounds.

## SQLite records

### chats

`id`, `name`, `source_chat_id`, `corpus_version`, `import_fingerprint`, `created_at`, `import_stats_json`.

Idempotent re-import of the same file returns the existing chat and report, without creating duplicate memories. A deliberately different corpus gets an isolated version/chat until incremental merge is implemented.

### messages

`id`, `chat_id`, `source_message_id`, `sender_id`, `sender_name`, `sent_at`, `raw_date`, `kind`, `text`, `reply_to_source_id`, `sticker_metadata_json`, `source_order`, `is_supported_text`.

Unique `(chat_id, source_message_id)`. Foreign key cascade on chat deletion. FTS index mirrors `chat_id`, exact message identity and searchable text. Parameterize SQL. Do not pass user-provided FTS operators directly to MATCH; normalize to escaped literal tokens, with a safe no-token branch.

### analysis_jobs / analysis_chunks / analysis_merge_nodes

Job: `id`, `chat_id`, `corpus_version`, `status`, `total_supported_messages`, `processed_unique_messages`, `total_chunks`, `completed_chunks`, `provider_calls`, `max_calls`, `last_error_code`, `active_worker_id`, `created_at`, `updated_at`.

Chunk: `id`, `job_id`, `ordinal`, `source_ids_json`, `context_ids_json`, `status`, `findings_json`, `attempts`, `claimed_worker_id`, `payload_json`.

Merge Node: `id`, `job_id`, `level`, `node_index`, `status`, `findings_json`, `created_at`. Unique `(job_id, level, node_index)`.

Statuses: `pending`, `running`, `completed`, `paused_budget`, `paused_cancelled`, `failed`.
- Single worker ownership & obsolete-worker isolation: atomic claim with unique worker ID (`active_worker_id` and `claimed_worker_id`). Chunk transitions and commits are guarded atomically; duplicate HTTP starts return existing running job without spawning duplicate background tasks; returning obsolete workers cannot mutate chunks or increment counters.
- Failed-work resumption: failed chunks retain their original bounded payload in `payload_json` (including split message fragments) and are retried on resume. Job completion is strictly gated on all planned chunks being validly completed and exact unique-message coverage matching `total_supported_messages`. Corrupted findings fail cleanly.
- Real call budget: every provider call (chunk, intermediate merge, root merge) is durably pre-reserved before dispatch. Untracked SDK/transport retries are disabled (`HttpRetryOptions(attempts=1)`, `max_retries=0`). Resuming `paused_budget` requires an explicit `max_calls` strictly greater than current `provider_calls`; omitting budget preserves the cap and returns HTTP 400 `budget_limit_reached` (never 404).
- Resumable hierarchy & typed merge schema: intermediate merge nodes are persisted in `analysis_merge_nodes` so resumed jobs do not pay for completed nodes again. Merge schema uses bounded typed `StyleAnalysis` (avoiding Developer API `additionalProperties` rejection). Intermediate finding models accept up to 10,000 characters of instructions and 50 memes, preserving style, evidence, and resumability.
- Sticky cancellation: status check before and after every provider await; cancelled workers cannot overwrite resumed work or commit stale findings.
- Bounded envelopes & safe errors: individual messages oversized beyond 4,000 characters are split into bounded slices with authentic evidence tracking; chunk envelopes cap total serialized characters at 30,000 accounting for context overlap and 100-character sender metadata bounds. Merge envelopes are capped at 40,000 chars, reply envelopes at 20,000 chars. Candidate quote views in replies are bounded (first 5, sender $\le 100$, text $\le 500$) while preserving exact original text in SQLite `ResolvedQuote`. Errors use fixed, safe categorization without echoing raw exceptions or private `input_value` dumps.

### profiles / memes

Profile: `id`, `chat_id`, `version`, `source_job_id`, `instructions_md`, `structured_json`, `edited_by_user`, `is_active`, `created_at`. Unique `(chat_id, version)`.

Meme finding: `id`, `chat_id`, `profile_id`, `label`, `description`, `usage_context`, `evidence_message_ids`, `confidence`, `approval_state`.

- Preserving owner active profile: if an active profile was edited by the owner (`edited_by_user = 1`), new AI analysis results are saved as suggestions (`is_active = 0`), preserving the owner's active instructions for runtime replies and exports. If no owner profile exists, analysis results become `is_active = 1`. Concurrent owner edits while merge is awaiting provider survive.
- Version-consistent meme snapshots: when updating an active profile, inherited memes are copied into the new profile version with fresh snapshot UUIDs, leaving historical profile meme rows intact. Meme edit updates must reference IDs belonging to the expected active profile version; stale or historical meme IDs are rejected with HTTP 400 `invalid_update`. Legacy multiple-active profile flags are safely normalized to a single active profile (latest version) without data loss. Unversioned fallback (`profile_id IS NULL`) is eliminated. Rejected memes (`approval_state = 'rejected'`) are excluded from markdown exports and grounded context.
- Evidence IDs must exist in that chat; invalid evidence is rejected/removed and reported. Confidence is model-assessed uncertainty, not a calibrated probability.

### replies / feedback

Reply: `id`, `chat_id`, `mode`, `action`, `text`, `quote_id`, `sticker_id`, `evidence_ids_json`, `provider_kind`, `created_at`.

Feedback: `reply_id`, `chat_id`, `verdict` (`landed`, `missed`, `overused`), `created_at`. Validate ownership and apply a bounded deterministic ranking adjustment. Do not label this model training or repeatedly multiply duplicated feedback events.

### telegram_bindings / telegram_updates / stickers

Binding: unique `telegram_chat_id` -> one `chat_id`, enabled/pause state, cooldown seconds, last send timestamp. Group must also appear in environment allowlist.

Updates: record processed Telegram update IDs transactionally to avoid duplicate sends as far as practical. Explain residual delivery uncertainty if a send succeeds but the acknowledgement is lost.

Sticker: `id`, `chat_id`, `description`, `tags`, `file_unique_id`, `bot_id`, `file_id`, `export_metadata_json`. Only a valid `file_id` belonging to the active bot can be used for sending.

## HTTP API

| Method/path | Contract |
| --- | --- |
| `GET /api/health` | Version, DB readiness, provider configured boolean, Telegram configured boolean. Never secrets. |
| `GET /api/chats` | Paginated chat summaries and analysis coverage. |
| `POST /api/chats/import` | Multipart single JSON export; name override optional. Validate file size and root shape. Return chat and import report; does not call AI. |
| `GET /api/chats/{id}` | Summary, coverage and metadata. |
| `DELETE /api/chats/{id}` | Delete all local memories, FTS data, profile versions, feedback and bindings for this chat; not provider retention promises. |
| `GET /api/chats/{id}/memories?q=...` | Chat-scoped, paginated quote search with reply context. |
| `POST /api/chats/{id}/analysis/estimate` | Chunk estimate and configured call cap; no AI calls. |
| `POST /api/chats/{id}/analysis` | Explicit start or resume. Return durable job ID; cap calls. Missing provider -> clear error, no fake success. |
| `GET /api/chats/{id}/analysis/{job_id}` | Progress, error category and resume eligibility. |
| `POST /api/chats/{id}/analysis/{job_id}/cancel` | Finish/abort current bounded call safely and save checkpoint. |
| `GET /api/chats/{id}/profile` | Latest current profile or clear not-yet-generated state. |
| `PUT /api/chats/{id}/profile` | Validated edit creates version. Reject stale writes using expected version. |
| `GET /api/chats/{id}/profile/export` | Download instructions and approved profile; no raw archive or secrets by default. |
| `GET /api/chats/{id}/profile/suggestion` | Fetch latest pending AI-generated profile suggestion, if any. |
| `POST /api/chats/{id}/profile/apply-suggestion` | Explicitly apply AI profile suggestion with expected_active_version lock. |
| `POST /api/chats/{id}/reply` | Bounded `context`, `mode`, optional `excluded_quote_ids`; grounded result. |
| `POST /api/chats/{id}/replies/{reply_id}/feedback` | One bounded verdict for a reply belonging to this chat. |
| `GET /api/telegram/status` | Redacted setup state and configured group bindings. |
| `PUT /api/telegram/bindings/{telegram_chat_id}` | Bind existing profile/chat; environment allowlist is also required. |
| `DELETE /api/telegram/bindings/{telegram_chat_id}` | Remove binding between Telegram group ID and local chat profile. |

Polling is an explicit CLI command in the first version, not an implicit side effect of opening a web page. Support invocation equivalent to `uv run python -m app.telegram`; document actual command after implementation.

## Provider-neutral reply decision

Required fields: `action` (`quote`, `generate`, `silent`), `quote_id` (nullable), `sticker_id` (nullable), `text` (nullable), `evidence_ids` (bounded list).

Do not render raw model output.
- `quote` mode resolves text server-side from SQLite. If the model selects an unverified or fabricated quote ID, it strictly fails closed to `action = "silent"` with null quote, never guessing or authoring quotes.
- `generate` mode bounds generated text (max 500 characters). Empty or whitespace-only model outputs are normalized to `action = "silent"`.
- `silent` has no sent payload.
- Structured validation errors use fixed, categorized public error messages without leaking raw model outputs, URLs, headers, or secrets.
- Conversation context has bounded length (max 4,000 characters) and is delimited inside `<chat_context>` tags as untrusted data.
- The model cannot choose a Telegram destination, path, token, executable or URL to fetch.

## Importer specifics

- Accept a single-chat export with `messages` array. Reject an account-wide `chats` export with instructions to export one chat; do not silently import the first chat.
- Top-level metadata (`name`, explicit `name` override, and source chat `id`) are strictly validated before early-return or persistence. Composite objects, lists, and booleans are rejected with stable 4xx importer error codes (`invalid_chat_name`, `invalid_source_chat_id`) without string coercion.
- Text may be string or array of strings/entity objects with `text`. Preserve ordering, emoji, newline and mixed scripts.
- Service records are counted separately; media-only messages retain metadata and do not count as text analyzed by the LLM.
- Retain reply references even if parent was omitted from export.
- Reject duplicate source IDs within the same file rather than arbitrarily overwriting one.
- No ZIP extraction, remote fetches, opening exported media paths, or URL execution during import.

## Operational boundaries

Local server binds to `127.0.0.1`. Same-origin frontend, no permissive CORS, reject unexpected browser origins for mutating routes. Malformed origin headers (e.g. invalid port syntax) evaluate to disallowed and return 403 without reaching endpoint handlers. Network calls run with timeouts and do not block the event loop. SQLite connections/transactions must not be shared unsafely across async tasks. Only one analysis task owns a chat corpus at a time. Use a standard migration/version strategy rather than silently dropping tables.
