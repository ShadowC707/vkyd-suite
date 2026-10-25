---
doc: checklist
status: approved
---

# Build Checklist

Build mode: fast

## Slices

- [x] **1. Official workflow and memory foundation**
  Becomes usable: Telegram single-chat JSON importer, SQLite FTS5 lexical quote search with Ukrainian/emoji support, atomic transactions, and foundational HTTP API (health, list/get/import/delete chat, memory search).
  Why now: Grounding memories and attributable quotes is the project kernel. Every later slice (analysis, reply generation, web playground, Telegram bot) depends on isolated, verified storage and retrieval.
  PRD ref: `prd.md > Memory`
  Spec ref: `spec.md > Module boundaries` (`app/config.py`, `app/db.py`, `app/importer.py`, `app/retrieval.py`, `app/main.py`)
  Build: Initialize Python project with `uv` lockfile; build configuration, SQLite migrations, normalized Telegram JSON importer, FTS5 retrieval engine with safe query tokenizer, and FastAPI endpoints.
  Verify (mechanical): Run offline test suite (`pytest`) verifying schema creation, import roundtrip, rich-text preservation, chat isolation, duplicate handling, deletion cascades, and malicious FTS query safety.
  Learner check: Import synthetic Telegram chats, query Ukrainian/emoji phrases via memory search endpoint, and verify exact quotes and reply context without SQL/FTS errors.
  Commit: `Implement official workflow and memory foundation`

- [x] **2. Grounded intelligence and humor profile analysis**
  Becomes usable: Bounded chunk analysis, checkpointed progress, evidence validation, editable humor profile, and grounded reply decision generation.
  Why now: Transforms raw chat memory into an attributable profile and grounded reply recommendations without modifying model weights.
  PRD ref: `prd.md > Style`, `prd.md > Playground`
  Spec ref: `spec.md > Full-history analysis`, `spec.md > Runtime grounding`, `spec.md > Provider`
  Build: Implement chunk scheduler, profile generator, evidence validator, provider interface with Gemini and deterministic test fake, and reply engine with quote-only and generated modes.
  Verify (mechanical): Run offline unit tests with fake provider verifying chunk coverage, budget pausing, resume capability, and candidate quote ID validation.
  Learner check: Run analysis on a synthetic chat, inspect generated memes with evidence message IDs, and generate quote/replies.
  Commit: `Implement chunk analysis, humor profile, and grounded replies`

- [x] **3. Web workspace and Telegram integration**
  Becomes usable: Four-tab web interface (Memory, Style, Playground, Telegram) and explicit Telegram group-bound bot worker.
  Why now: Completes the user experience, allowing browser interaction and live group invocation.
  PRD ref: `prd.md > Interface`, `prd.md > Telegram`
  Spec ref: `spec.md > Module boundaries` (`app/static/`, `app/telegram.py`)
  Build: Create static responsive HTML/CSS/JS frontend; implement Telegram bot polling adapter with allowlist enforcement, cooldown, and invocation filters.
  Verify (mechanical): Run test suite with fake Telegram transport verifying command addressing, mention filtering, and sticker file ID safety.
  Learner check: Open web interface, switch between chats, test playground replies, and inspect Telegram connection status.
  Commit: `Implement web workspace and Telegram adapter`

## Hands-on Checkpoints

- [ ] Early usable behavior explored — learner feedback not yet verified; agent mechanical checks do not satisfy this checkpoint
- [ ] Final kick-the-tires exploration and feedback completed

## Final Review

- [ ] Final review complete — feedback resolved and learner confirms ready to ship

## Code Tour and App Map

- [ ] Learning activity complete — guided route, focused alternative, prior practice connected, or brief recap
- [ ] Optional edit and transfer reflection addressed — offered/declined/already covered/not applicable as appropriate
- [ ] `devpost/app-map.html` generated from finished code, checked, and shown, including a project-grounded practice to reuse

Activity and evidence: [to be recorded upon completion of build]
Route and stops: [to be recorded upon completion of build]
Edit outcome: [to be recorded upon completion of build]
Reflection: [to be recorded upon completion of build]
Activity mode: [to be recorded upon completion of build]

## Revisions

- 2026-10-25 05:35 Europe/Kyiv live update during handoff: same full-history job resumed by the active coding agent after compacting merge instructions. Status running, ledger 737/1000, current saved merge nodes 144 (112/29/3), no final profile. Earlier envelope-failure snapshot below is historical; recovery and changed upper-level cache have not received independent acceptance. Do not launch a duplicate worker; record terminal outcome before archiving.

- 2026-10-25 handoff: Task 04 completed 559/559 real-history chunks (241,569 unique supported messages) and 158 intermediate merge nodes; job failed at `envelope_exceeded`, ledger 724/1000, no final profile. Read-only partition probe reproduces oversized pairs. Telegram token configured but allowlist/bindings empty; no verified round trip. Independent selected handoff tests: 57 passed; Ruff/JS syntax passed; full suite timed out at 30s, exit 124. Task 04 remains unapproved pending complete review. Private archive handoff to two colleagues is authorized; GitHub/demo/submission and all human checkpoints remain open. See `../TODO.md` and `../docs/handoff.md`.

- 2026-10-25 independent 03r re-review: code accepted for local MVP hands-on exploration. 47 selected pytest checks, 46 direct async tests, JS syntax and Ruff passed; repeated original synthetic failure probes now show correct behavior. Full pytest timed out at health here; 129 passes are author-reported. Quote/provenance/feedback and delayed-profile JS probes passed. Nonblocking N1 (obsolete poll clears new timer) and incomplete browser concurrency-test evidence are recorded. Human feedback, remote Gemini structured success, real Telegram and shipping remain open; code review does not check the learner boxes.

- 2026-10-25 independent task 03 review: web files, Telegram adapter, suggestion endpoints and license exist. 45 selected pytest checks, 41 direct async cases and Ruff passed; full pytest timed out here. Shipped JS fails parsing; isolated real-function/transport probes reproduced blank quotes, undefined feedback IDs, stale cross-chat profiles, empty-allowlist sends, wrong update-filter encoding, incomplete payload coverage and clipped findings. Repairs S1-S5 are assigned to task 03r. Slices 2/3 and human feedback checkpoints remain open; no live provider or Telegram run was authorized.

- 2026-10-25 Task 03: Completed preflight repairs G1 (legacy/unreconstructable payload reconciliation without partial drops or reset of paid work) and G2 (dynamic merge batching by serialized envelope <= 40k chars without charging local preflights, intermediate merge node caching and resume). Implemented responsive static same-origin frontend (`index.html`, `app.css`, `app.js`) with warm-paper scrapbook design, safe DOM text node rendering (0 raw `innerHTML`), chat switcher with in-flight abort, import modal, memory search with pagination, analysis budget controls with pause/resume, author profile editor with optimistic locking, AI suggestion inspector/apply, playground with grounded quotes and feedback, and Telegram configuration view. Built standalone Telegram polling worker (`app/telegram.py`) with group allowlisting, identity verification, triggers, cooldown, and safe quote formatting. Added 119 passing tests covering unit, integration, fake-transport, and end-to-end synthetic flows. MIT License added. Awaiting Codex independent review; human checkpoints remain open.

- 2026-10-25 independent 02r2 re-review: 44 selected pytest checks, 32 direct async scenarios and Ruff lint/format passed; complete pytest timed out here, while the author reports 102 passes locally. Main R1-R6 fixes are confirmed for well-formed new jobs. Two remaining reproduced cases (G1 legacy payload recovery and G2 serialized-size merge planning/accounting) are mandatory preflight repairs in task 03, which Codex explicitly authorizes to continue through the UI/Telegram work in the same run. Final Slice 2 acceptance awaits those repairs; human checkpoints remain open.

- 2026-10-25 independent 02r re-review: 39 core pytest checks, 21 directly invoked async tests and Ruff passed; full pytest timed out here. Synthetic probes still reproduced false completion at 5/10, obsolete-worker inflation to 15/10, implicit cap increase and unaccounted configurable retries, unsupported production merge schema, incomplete serialized bounds, unsafe outer errors and mutable historical meme snapshots. Task 02r2 is assigned; Slice 2 and human checkpoints remain open. The user reports API access working; no new live request was made in this review.

- 2026-10-25 independent task 02 review: 40 selected pytest checks and Ruff passed; six existing async analysis cases passed via direct asyncio. Additional synthetic scenarios reproduced budget overrun (17 calls against cap 14), duplicate coverage (10/5), merge cancellation lost, owner instructions replaced, and unsafe/permissive output validation. Task 02 remains open; I1-I5 are assigned to task 02r before UI/Telegram work. Full 72-test success and the live quota error are agent-reported, not independent end-to-end verification.

- 2026-10-25 task 02: resolved preflight migration report counter via additive migration v4 (consistently counting native empty and reclassified whitespace messages without altering other statistics). Implemented Slice 02 grounded intelligence: typed async provider interface, production Gemini adapter with structured output and sanitized errors, deterministic test fake, ordered chunk scheduler with durable ledger, honest unique-message coverage without double-counting context overlap, call cap and pause/cancel/resume, evidence validation against active chat, versioned profile edits with optimistic conflict handling, grounded reply service (quote-only, generated, auto/silent), feedback persistence with bounded ranking adjustment, and opt-in synthetic live smoke test command. All 72 tests and Ruff pass cleanly. Awaiting Codex independent review; Slice 2 and learner checkpoints remain open.

- 2026-10-25 Codex final foundation review after 01r2: code approved for task 02. Independently verified 37 non-HTTP/migration tests and Ruff; the agent/user terminal summary reports all 47 tests passing, while full HTTP execution remains unverified in the review environment. A minor empty-message report counter is assigned as the first repair in task 02. This code milestone does not mark learner feedback, Git commits or shipping complete.

- 2026-10-25 task 01r2: resolved R1b (malformed origin ports), R3b (top-level chat metadata validation and atomic DB non-modification), and R4b (canonical supported-text predicate, v1 upgrade, additive v2 repair, report count refresh, and repeated migration idempotency). Entire offline suite (47 tests) and Ruff passed cleanly in local terminal. Slice 1 remains unchecked awaiting Codex independent review; learner checkpoint remains open.

- 2026-10-25 re-review of task 01r: 32 non-HTTP tests and Ruff passed; three remaining reproduced issues (malformed origin ports, top-level chat metadata, migrated supported-text flags) are assigned to task 01r2. Slice 1 remains unchecked until re-review; learner feedback is still not replaced by agent tests.

- 2026-10-25: Codex independently reviewed slice 1 and reopened it for R1-R7 fixes in `orchestration/reviews/01-foundation.md`. Eighteen non-HTTP tests and Ruff passed independently; HTTP tests stalled in the review environment. An agent's mechanical tests were not accepted as learner hands-on feedback. Task 02 is pending repair and re-review.
