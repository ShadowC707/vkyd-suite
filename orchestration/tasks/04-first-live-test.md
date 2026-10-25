# Task 04: full real-history Gemini run and an isolated Telegram test

You are console agy, the application code author. Codex is architect/orchestrator/reviewer. Read current architecture/contracts, the final 03r section of `orchestration/reviews/03-experience-and-telegram.md`, current source and reports. Do not restart interviews, regenerate the app or self-approve. User authorization now explicitly covers real Gemini inference using the existing configured credential and the user's exported archive, and a Telegram test in a **separate test group**. The user chose the **whole history**, not a 200-message preview. This supersedes prior task files' no-live prohibition only within the limits below.

## Observed starting state (aggregate metadata only)

- Real private export: `tests/fixtures/result.json`, 139,743,051 bytes, 261,703 records, 241,569 supported-text records, 7,592,148 supported-text characters. Codex printed no chat names, messages, authors or credentials.
- The exact export path is now ignored by Git. Git is not initialized. Never add this file, derived profiles, real reply content, media or DBs to fixtures/public tests, Git, Library, reports, archives or submission artifacts. Leave the original export intact.
- Configured model: `gemini-3.8-flash`; google-genai SDK 2.33.0; key configured. Actual remote structured output remains unverified: a previous application smoke returned 400.
- Telegram token is not configured; allowlist and bindings are empty. The export's original source chat ID is NOT authorization to send there.
- Current import limit is 50 MiB, so the 139.7 MB export cannot be imported through the unchanged upload limit.
- Pure offline planning with current 25k-character envelope/overlap=10: chunk_size=50 -> 4,832 chunks + base 1,211 merges = 6,043 calls; chunk_size=500 -> 559 chunks + base 141 merges = 700 calls. chunk_size=1000 saves only seven more calls. All 241,569 supported source IDs were retained in both larger plans. These are base estimates, not exact future dynamic-merge costs or token-price quotes.
- Codex's isolated execution environment cannot resolve the Gemini host (gaierror errno -3); no inference requests were made. Browser bridge could not access the user's localhost tab either. Run live checks in the user's normal console environment, not through workarounds to Codex sandbox restrictions.

## 1. Small readiness repairs and safe configuration

1. Fix N1 in `startPollingProgress`: each callback must capture/clear only its own interval, never the current global timer belonging to a replacement chat. Add a real deferred-poll regression that starts B's timer before A returns; existing stale-poll browser test merely reads the dropdown and is not that regression. Preserve post-await profile/reply/chat guards.
2. Make analysis chunk count configurable, defaulting compatibly for old tests/jobs. For this new full-history job choose chunk_size=500, overlap=10 and existing per-message/per-prompt envelopes. Persist the plan/config identity as needed; never reinterpret or reset an older running job's plan. Do not increase the actual character/token envelope or truncate content to reduce calls. Keep exact source text, reply references and chronological order.
3. Set a bounded 160 MiB local import limit (`MAX_IMPORT_SIZE_BYTES=167772160`) through existing configuration for this run, preserving bounded upload reading and atomic import. Do not make upload sizes unlimited. Changing `.env` non-secret options is authorized, but load/edit it only through runtime code: never cat/read it into agent prompts or logs, and preserve credentials, permissions and other settings. Explain that a previously running uvicorn process needs restarting to pick up configuration/code.
4. Keep private data local except the bounded model inputs required for the expressly requested Gemini analysis. All coding/debug/regression tests use synthetic fixtures. Runtime programs may parse the real export and send planned inputs to Google; their stdout/report must contain aggregates/IDs/status, not message text or private profile contents. Do not read the raw archive into coding-agent context.

## 2. Prove the production Gemini path before bulk analysis

Use current official Google documentation and the installed SDK to verify request construction. Sources checked by Codex on 2026-10-25:

- https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/gemini-api/docs/pricing

Current structured-output examples use `client.interactions.create` with `response_format` JSON schema; current production code uses `aio.models.generate_content`. Do not assume either path works remotely merely because mocked serialization passed. Diagnose actual sanitized request/schema errors and use the documented production path appropriate to the existing model. Do not blindly migrate endpoints without evidence. Thinking `minimal` is not supported for this model; if explicitly tuning thinking, use a documented supported option such as low. Preserve typed local validation.

- First run focused offline provider regressions/transport serialization. Then execute a tiny synthetic structured analyze -> merge -> reply sequence using the real configured model and server-side credential. At most **6 actual synthetic inference attempts** total; normally three suffice. An extra attempt is allowed only after a concrete request/schema fix and corresponding offline test, not blind retry.
- Disable app and SDK/transport automatic retries. Authentication, billing/quota/429 and network failures stop the live stage. Do not rotate models, keys, projects or accounts.
- Every call needs a finite timeout and explicit bounded output tokens. Account for thinking/output exhaustion and inspect safe finish/error/usage metadata, not raw response content.
- Errors must be diagnostically useful without echoing secrets or content: safe status, provider category and schema field/constraint when available. Never print raw request/response bodies, headers, credential URLs, API key, real message fragments or private validation input dumps. A pre-dispatch failure is not a successful or billed inference.
- Close resources. Record attempted/dispatched/schema-valid separately. If synthetic production operations cannot succeed, stop before sending the real archive; report the concrete block and do not claim a working API path.

## 3. Import the whole archive, estimate and execute with an explicit saved cap

Use the actual importer/storage/analysis/provider implementation, through an opt-in CLI helper if that makes this operational run safer and easier to resume. Never fabricate an API success or replace Gemini with FakeAIProvider.

1. Import the whole export locally, idempotently, without resetting existing synthetic chats. Validate exact corpus/report counts and all supported IDs. Real data stays out of stdout and public artifacts.
2. Build the full 500-item/25k-character plan, compute exact current chunk count and baseline dynamic-merge estimate, and display aggregate coverage/call estimate plus a token-cost estimate and its assumptions before starting work. Prefer documented count-tokens functionality/local compatible tokenization; any remote token-count request must be bounded and reported separately from inference. Do not call it an exact final price. Current standard published price is $0.75/M input and $3.75/M output including thinking through Dec 31, 2026; verify if changed. No search/grounding tools or paid extra services are needed.
3. Respect explicit `GEMINI_MAX_CALLS_PER_ANALYSIS` from the invoking terminal as the real archive job's lifetime cap. With the user's full-history choice the proposed command sets it to **1000**; if absent, preserve the existing configured/default **50**, never silently raise it. Apply the cap to every actual chunk/intermediate/root attempt, failure included. Do not auto-increase on resume. The initial root plan is about 700 calls; dynamic merge or errors may require more. Pause honestly if the cap is reached and retain successful work.
4. Run the real job to completion or a genuine bounded pause/error. Do not sample history, generate a subset profile, omit failed/safety-blocked chunks, skip text, or claim full coverage prematurely. A running/paused full-history job remains partial even when all raw messages are indexed. Surface progress and sanitized usage/remaining work. Preserve completed nodes and charges on restart/resume.
5. If complete, verify exact unique coverage, active/suggested profile policy, grounded quote attribution and one or two real generated playground replies using the learned profile. Never expose private generated text in the public report. Extra live playground diagnostics are capped at **2** inference calls. Do not represent generic fallback persona as the fully learned profile.

The call cap is not a dollar cap; report actual token usage/cost estimates separately and stop rather than claiming a dollar guarantee. Do not alter billing, buy credits, enable auto-reload, or use fallback credentials. If a quota/balance/provider condition blocks completion, retain progress and report it without repeated retries.

## 4. Bootstrap only the separate Telegram test group

The user must create/provide their bot through BotFather and place `TELEGRAM_BOT_TOKEN` in their local `.env`; never ask them to paste the token into chat. They chose a separate test group. Do not infer the destination from the private export, message another group, or turn empty allowlist into unrestricted access.

- Provide a bounded read-only setup/discovery mode if needed: verify `getMe`, then inspect only update metadata to locate a unique setup marker command that the user posts in the separate test group. Print only that group ID/type and masked readiness; no messages/other group titles or participant data. Do not send a setup message to discover a destination. Do not acknowledge/drop unrelated updates, register a webhook or change privacy/admin settings.
- Bind only an explicitly configured allowlisted test group to the real imported corpus/profile. If multiple destinations exist or the marker is ambiguous, stop that dependent step and state exactly which non-secret group ID is needed. The original dorm chat is never a default destination.
- If token/target are missing, continue all independent Gemini/import/planning work and report `telegram_setup_required` with exact local setup commands. Missing Telegram config is not an excuse to abandon the authorized AI work or fabricate a successful Telegram test.
- Once token and exact test destination are established, run the worker explicitly in the foreground, bounded to a test window and at most **3 sent test replies**, only in response to the user's `/quote`, `/reply`/`/humor`, or mention commands in that test group. Test one authentic quote with attribution and one AI reply only after the full profile is genuinely ready. No autonomous interjections/startup posts, no posting to the dorm chat, and no sticker sending yet.
- Keep transport serialization, allowlist/binding, other-bot targeting, dedupe, cooldown/pause, bounded payloads and cleanup verified through offline fakes before live operation. Close Gemini and Telegram resources on exit. Do not leave an orphaned web server or poller on a port in the background; track/stop only processes you created, never kill the user's server blindly. Print explicit restart/run commands for any process the user owns.

## Verify and report honestly

Run necessary focused regressions, then the offline suite, Ruff lint/format and shipped JS syntax checks with sensible timeouts. Use synthetic fixtures only. User logs show the app starts and imports a synthetic chat; this is actual early exploration, not confirmation that all human checkpoints or shipping are done.

Write `orchestration/reports/04-first-live-test.md`: code/config changes, N1 evidence, actual synthetic model results, real import counts, plan parameters, actual attempts/token usage and coverage/completion state, interruption/resume commands, Telegram readiness/actual test result, exact regression output and remaining blockers. Do not include real quotes, profile contents, names, tokens or full IDs of unrelated chats. Update actual status/docs and stop for Codex independent review. Do not self-approve, publish, push, deploy, create a public repo or generate human Devpost survey/submission text.
