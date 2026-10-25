# Codex review: web experience, Telegram and combined MVP

Date: 2026-10-25, Europe/Kyiv.

Decision: **approved_for_local_mvp_testing** after task 03r.
Next action: run the local app and perform the no-key synthetic user flow. No further full repair round is required before hands-on testing. Live Gemini/Telegram verification and submission readiness remain separate.

The 03r re-review at the end of this file supersedes the initial failures below. One nonblocking progress-poll lifecycle issue is recorded as N1; approval is for local MVP exploration, not a claim of complete production or live-service verification.

## Initial task 03 review — historical

The interface files, Telegram adapter, suggestion inspect/apply endpoints, MIT license and main G1-G2 repairs exist. Keep them. The shipped interface is not usable yet because the JavaScript bundle fails parsing. No live Telegram run or paid Gemini verification is approved by this review. Codex edited orchestration/review documentation only, not application code.

## Independent evidence

- Core pytest over config/importer/retrieval/migrations: **40 passed**, one deprecation warning, exit 0.
- Selected restart, provider-sanitization, historical-meme, active-profile-normalization and quote-format tests: **5 passed**, one deprecation warning, exit 0.
- **41 existing async analysis/provider/reply/Telegram tests** invoked directly with `asyncio.run`, temporary databases and bounded awaits: passed. This includes the new ordinary legacy-recovery and five-by-9,000 merge tests, plus four existing Telegram service tests.
- `ruff check .`: passed. `ruff format --check .`: passed, **76 files already formatted**.
- `pytest --collect-only -q`: **119 tests collected**. The author reports all 119 passing in their normal terminal. A complete run here under a 20-second timeout returned **exit 124**; its cause is not established, and it is not a passing full-suite result.
- `node --check app/static/app.js`: **exit 1**, `SyntaxError: Unexpected token 'if'` at line 1256. Python API tests and Ruff do not parse or execute this JavaScript.
- Additional isolated DOM-function and fake-transport probes reproduced the failures below. The DOM probe evaluated the actual extracted function bodies because the complete shipped bundle does not parse; it is not a successful whole-browser test.
- Live browser inspection was unavailable: the browser-control bridge returned a tab/session error and an empty tab inventory. The author's desktop/mobile screenshot claims are not independently verified here. A screenshot of static HTML is not evidence that import, quote, feedback or switching handlers executed.
- No live Gemini or Telegram request, real archive upload, credential dump, publication or deployment occurred. Only public Telegram documentation was retrieved.

## S1 — P0: shipped JavaScript does not parse

Affected: `app/static/app.js`, around `renderImportResult` and the stray repeated import-finish callback before the drag/drop block.

`node --check` fails at line 1256. Consequently the browser cannot execute the IIFE: chat loading, import events, search, analysis, profile editing, replies and bindings are all blocked. The extra callback also refers to an undefined `report` variable.

Repair the actual bundle, check its syntax, and verify that it boots and executes the core flow. Do not count static asset HTTP 200 responses or screenshots as functional JavaScript evidence.

## S2 — P1: frontend reads fields the actual API does not return

The following are independently observed in the actual function bodies and an isolated DOM probe using the real ReplyResponse shape:

- API quote replies use `action='quote'`, `text=null`, and `quote.text` containing the exact original. `renderPlaygroundReply` reads `data.text`, so the displayed quote body is **empty**.
- Feedback checks `lastReplyData.id` but constructs the URL with `lastReplyData.reply_id`. The probe produced **`/api/chats/chat_a/replies/undefined/feedback`**.
- The source drawer expects `retrieved_quotes` or `source_quotes`, neither present in ReplyResponse. It says there are no quotes even for a real resolved quote. Render actual selected quote/evidence, or add a real documented bounded provenance field; do not invent candidate provenance.
- Import results read `imported_supported_messages` instead of `supported_text_messages`, displaying zero imported text. They invent a UTC timezone even though import dates can be unresolved.
- Meme titles read `name` instead of `label`.
- Progress logic uses `paused`/`cancelled` instead of the actual `paused_cancelled` state, ignores `can_resume`, and loses the error category behind an unknown-error message. A cancelled job is not presented as a resumable cancellation.

Align the complete UI with the real schemas, including modes, actions, states, selected quote text, IDs, report counts, meme labels and error codes. Add tests that execute the shipped JS against real API-shaped responses. Existing `tests/test_e2e.py` tests the HTTP API; its name does not make it a browser-flow test. The HTML-injection test confirms text preservation in JSON, not browser script non-execution.

## S3 — P1: old responses overwrite the newly selected chat's state

Affected: async readers, mutation completions and progress polling in `app.js`.

Aborting a fetch does not invalidate a response already received or its pending JSON parsing. Deterministic DOM probe: hold chat A's profile response body, switch/reset to chat B, then release A. Result: active chat is **B**, editor text is **CHAT_A_PRIVATE_STYLE**, and `activeProfile.chat_id` is **A**. A subsequent save uses the current B destination and the stale profile's version/text, risking a cross-chat overwrite when versions match.

Use captured chat identity plus a selection/request generation check after every await and before state/UI updates, including error/finally paths. Invalidate old poll callbacks as well as timers; a late completed A poll must not call `selectChat(A)` and switch the user back. Scope search ordering too. Clear `lastReplyData`, source/attribution bodies, suggestion dialogs, errors and button state on switching. Test delayed JSON resolution, old mutation completion, old poll completion, empty selection and two overlapping searches; AbortController alone is insufficient.

## S4 — P1: Telegram defaults bypass the allowlist, and real transport serialization is wrong

Synthetic service/transport probes:

- `allowed_chat_ids=set()` plus an enabled binding **sends one quote message** to that group. `if self.allowed_chat_ids and ...` treats an empty environment allowlist as unrestricted. The CLI explicitly prints `NONE (unrestricted)`, contradicting the required explicit allowlist. Empty allowlist must permit no model calls or sends; a DB binding must not override it.
- `/quote@other_bot ...` sent as a reply to this bot is parsed as **triggered / auto** because reply detection precedes explicit command-target rejection. Reject another bot's command before reply/mention routing.
- The actual HttpTelegramTransport through `httpx.MockTransport` serializes `allowed_updates` as **`message`**, not a JSON array. `json.loads` of the captured value fails. Official getUpdates documentation requires a JSON-serialized list: [Telegram Bot API — getUpdates](https://core.telegram.org/bots/api#getupdates). Test the actual request encoding, not only FakeTelegramTransport; JSON POST or correctly encoded query parameters are both suitable.
- `format_quote_attribution('A quote', 'S'*5000, None, 1)` produces **5,017 characters** despite the claimed bound. Bound the attribution metadata and complete outgoing payload while preserving explicit truncation and real source identity.

Use current official reply parameters for plain-text sending and verify the serialized request with a fake HTTP transport. Bound an incoming Telegram context before ReplyRequest validation; an over-limit message must not produce a raw Pydantic chat-content dump or crash the polling batch. Keep sticker sending disabled as documented, no autonomous interjections, and no live polling during repair.

## S5 — P1: recovery validation and oversize findings still violate the history contract

The ordinary G1 and G2 probes now pass. Two uncovered cases remain:

- `_validate_chunk_payload` checks JSON/list shape and whether each item has an ID, but does not check expected primary/context message completeness. Probe: five-message one-chunk job, replace its payload with only its first valid item while keeping the five source IDs. Result: **completed, coverage=5/5**, while the provider saw **only ID [1]**. Detect missing/wrong-chat IDs and incomplete split identity, then durably recover all expected work or reject safely before dispatch. Do not credit the source-ID ledger for absent payload content.
- `_partition_merge_batches` silently assigns `item.instructions_summary = item.instructions_summary[:8000]` for an oversized input. A schema-valid finding with a 10,000-character summary and 35 memes is mutated to **8,000 characters**. This contradicts the no-silent-truncation requirement. Preserve findings or reject an individually oversized node honestly before reservation; do not mutate stored/model findings to manufacture fit.

The test named `test_g2_individually_oversized_node_handled_explicitly_without_dispatch_budget` actually uses roughly 7,500-character summaries, succeeds with one root call, and does not test the 45,000-character failure claimed in the report. Replace that evidence with a real schema-valid oversized envelope regression and verify no mutation/no non-dispatched budget charge.

Keep completed-node reuse, worker ownership, cancellation, original source text and all passing regressions. Mark permanently unrecoverable/local-envelope failures consistently and offer a real documented recovery route; do not advertise a retry that repeats the same impossible plan.

## Documentation and next step

The report's per-module test counts and some scenario descriptions do not match the actual tests. Correct them from actual output. README must describe stored data remaining local while explicitly started Gemini analysis sends bounded message/profile inputs to Google. Its sample model (`gemini-2.5-flash`) differs from the configured `gemini-3.8-flash`; do not change the user's model or credential to resolve documentation. The no-key demo currently exports a profile without first creating one; include the actual manual-profile step or explain the not-yet-created state.

Run `bash scripts/agy-task.sh 03r`. Repair S1-S5 in place and prove the user-facing flow with the shipped JS and actual fake HTTP transports. Preserve the design and existing backend instead of rewriting the application. Return for independent review. Human hands-on, real provider generation, Telegram posting, public repo and hackathon submission remain separate pending steps.

## Independent re-review after task 03r

Date: 2026-10-25, Europe/Kyiv. Combined task 02/03 code is accepted **for local MVP hands-on testing**, with the limits below. Human learning checkpoints and live service acceptance are not completed by this code review.

### Confirmed repairs and checks

- `node --check app/static/app.js`: **passed**, exit 0.
- Core pytest selection: **40 passed**; seven additional synchronous restart/sanitization/profile/quote/merge-immutability checks: **7 passed**. Both commands exited 0 with one deprecation warning each.
- **46 existing async analysis/provider/reply/Telegram tests** invoked directly through asyncio with temporary databases and bounded awaits: passed (29 analysis, 7 provider, 2 reply, 8 Telegram).
- Ruff lint and format: **passed**, 79 files already formatted.
- Complete pytest collected **129 items**, passed the analysis tests, then stalled at `tests/test_api.py::test_health_endpoint`. The bounded 45-second run exited **124**. This identifies the stalled test in this run, not a proven underlying cause. The author's normal-terminal result is **129 passed in 22.63s**; the full suite is not independently verified here.
- Repeated the exact S4-S5 synthetic probes: empty allowlist sent **0** messages; another bot's explicitly addressed command was rejected even when replying to this bot; actual HTTP transport emitted a JSON update-filter list; corrupted one-of-five payload was recovered and actually sent IDs **1,2,3,4,5** before reporting **5/5**; schema-valid oversized findings retained their **10,000-character** summary.
- A Node VM harness loaded the **whole, syntactically unchanged shipped bundle**, exposing closures for observation only, with a minimal fake DOM/network. The real ReplyResponse shape rendered the exact quote text, included it in provenance and submitted feedback to `/replies/reply_wire_id/feedback`. A delayed old-chat profile body was ignored after switching; no old instructions were inserted. This is meaningful JS logic verification, not a real-browser rendering pass.
- Inspected the saved synthetic screenshot `tests/artifacts/browser_test_success.png`: the paper/coral interface, imported chat and coverage overview render coherently. It is a small captured viewport, not independent evidence for every interaction or responsive width.
- The new Chromium tests genuinely drive the shipped page's import/search/quote/feedback/profile handlers with a fake provider. Their claimed stale-poll and overlapping-search coverage is overstated: the stale-poll section merely reads the current selected value without releasing an old poll, and the search section does not delay/reorder responses or assert the latest query's specific results. The profile-delay test delays fetch before dispatch rather than JSON after response. Keep the tests, but describe their actual evidence and add real barriers when addressing N1.

The original S1-S2 blockers and the demonstrated cross-chat profile overwrite are closed. Telegram's tested allowlist/target/serialization boundaries and the demonstrated incomplete-payload/silent-mutation failures are closed. Accepted limitations include explicit rejection of unreconstructable/oversized analysis data, disabled sticker sending and unverified remote structured inference. The validator checks expected IDs, chat membership and presence of a final split fragment; this should not be described as proof that every possible corrupt split span is complete.

### N1 — P2, nonblocking follow-up: an obsolete poll can stop the replacement timer

Deterministic VM probe: hold chat A's poll at fetch, switch/reset to B, start B's poll, then release A. The old callback detects it is obsolete, but calls `clearInterval(pollIntervalId)` using the global ID now belonging to B. Result: **B's new timer is cleared**. The backend analysis continues; the defect stalls displayed progress and does not reintroduce the old profile overwrite or bypass the backend budget.

Follow-up: each poll callback must capture and clear only its own timer ID and must not null a newer global handle. Add an Event/deferred-promise test that actually releases an old poll while the replacement timer exists; the current browser test does not exercise it. This limited UI lifecycle repair can accompany the next coding task. It is not a reason to postpone the no-key manual user flow.

### Next step and remaining evidence

Start `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` in the user's normal terminal. Import `tests/fixtures/synthetic_chat_a.json`, search, choose deterministic quote mode, record feedback, manually create/save a profile and export it. This does not require an inference call. Record actual learner feedback before checking human hands-on items.

Working remote Gemini structured inference remains unverified by Codex; the user reports general API access working. No live request was made in this review. Telegram transport was verified only with fakes; no real polling or sending is authorized by this approval. Public repository, demo video and human submission materials remain pending. Do not interpret local MVP acceptance as permission to publish, spend more API credits or message a group.
