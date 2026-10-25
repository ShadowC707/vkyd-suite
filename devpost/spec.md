---
status: approved
origin: orchestrator-planning-input
official_skill_workflow: routed_via_devpost_learn_pack
approved_by: codex-architect-review
---

# Technical specification: Vkyd

## Stack decision

Python 3.12+ source compatibility (local host is 3.14), FastAPI, Uvicorn, Pydantic, SQLite with FTS5 and `unicode61`, Google GenAI SDK, a small static HTML/CSS/JS frontend served from the same origin. Use `uv` for environment setup and a committed lockfile when dependency resolution is available. Testing: pytest and httpx with isolated temporary databases and fake provider/Telegram transports. Avoid a separate frontend build, Redis, external vector DB or agent framework in the first release.

## Module boundaries

- `app/config.py`: validated environment configuration, redacted secret handling.
- `app/db.py`: migrations, transactions, connections, per-chat key constraints.
- `app/importer.py`: Telegram normalization and import report.
- `app/retrieval.py`: safe Unicode query normalization, FTS ranking, reply-context expansion and feedback-aware reranking.
- `app/providers/base.py`, `gemini.py`, `fake.py`: typed provider interface, production Gemini, test-only deterministic fake.
- `app/analysis.py`: chunk scheduling, checkpointing, evidence validation and profile merge.
- `app/replies.py`: scoped retrieval, action validation, exact-quote resolution and duplicate suppression.
- `app/telegram.py`: transport, explicit binding, trigger parsing, dedupe and reply delivery.
- `app/main.py` and `app/static/`: API, local web workspace.
- `tests/fixtures/`: synthetic exports only.

## Full-history analysis

Persist every normalized supported message and analyze ordered, bounded chunks with reply context. Chunk IDs derive from corpus version and source message IDs. Store analysis results before advancing progress. Merge chunk findings in bounded batches if the findings exceed the provider context budget; preserve evidence IDs, multiplicities and uncertainty. Track total/processed messages and chunk counts. A budget stop is `paused_budget`, never `completed`. A restart can resume committed chunks. Model identifiers are configured, not guessed or silently replaced. The current official Google model guide lists `gemini-3.8-flash`; actual availability under this key remains unverified.

## Runtime grounding

Prompt content combines owner-approved instructions, a recent conversation fragment, and a bounded number of chat-scoped retrieved excerpts. For quote actions, the model selects only candidate IDs. Server validation resolves exact source text and sender; never trust a model-authored quote or attribution. Invalid actions fail closed without sending. Quote-only mode uses deterministic retrieval and does not need a model.

## Provider

Use current `google-genai` SDK with structured JSON output and Pydantic validation, explicit timeout, bounded retries with backoff for transient errors, and capped parallelism. Check current SDK examples during implementation before choosing generate-content/interactions method signatures. Return sanitized errors; never log full request headers or provider URLs containing secrets. Archive analysis is explicitly initiated and shows the chunk/call budget before any paid call.

## Storage and boundaries

See `docs/contracts.md`. Chat is the first scoping parameter for every memory/profile/retrieval operation. Use composite uniqueness for Telegram message IDs. Private SQLite data lives under ignored `data/`. No real exports in fixtures or demo assets. Tokens stay in the ignored `.env`, not SQLite or browser storage. Bind to localhost by default. Hosted multi-user access is out of scope; do not advertise this as safe for public hosting without authentication.

## Verification

See `docs/acceptance.md`. Keep ordinary tests offline. An opt-in live Gemini smoke check should send only a tiny synthetic prompt, never real chat history, and print only model name, pass/fail and sanitized error category. Actual Telegram posting requires user-supplied token/group binding and a deliberate start command; mocks do not constitute real Telegram verification.

## Orchestration

Codex owns scope, interfaces and review. Console agy writes application code in the three bounded tasks in `orchestration/tasks/`. It must not spawn additional agents, alter orchestrator documents silently, or publish/send anything externally. Every result is provisional until Codex inspects it. Current environment blocks agy startup; do not represent these draft plans as a completed build.
