# Codex review: full-history analysis and grounded replies

Date: 2026-10-25, Europe/Kyiv.

Decision: task_03_authorized_with_required_preflight

Current action: run task `03`. First repair G1-G2 in the final review section below, then complete the web/Telegram slice in the same run. Task 02's final acceptance is deferred until those regressions pass; this is explicit authorization to progress, not an assertion that remaining failures are fixed. Earlier decisions and findings are retained as history.

Foundation approval remains valid. This decision concerns task 02 only. Application code remains assigned to console agy; the reviewer inspected and exercised the actual implementation without editing it.

## Independent verification

- `.venv/bin/python -u -m pytest -q tests/test_config.py tests/test_importer.py tests/test_retrieval.py tests/test_migrations.py`: **38 passed**, one deprecation warning, exit 0.
- Selected synchronous provider-sanitization and restart-recovery tests: **2 passed**, exit 0.
- `.venv/bin/ruff check .`: **passed**, exit 0.
- Six existing asynchronous analysis test functions were called directly with `asyncio.run` and isolated temporary directories, avoiding the previously stalled pytest AnyIO/TestClient path. All six passed: multi-chunk coverage, the existing budget/resume case, pre-execution cancellation, evidence filtering, small hierarchical merge, and provider-error status.
- Additional direct synthetic probes reproduced the failures below. No live API calls, real chat history or actual credential values were used.
- The agent reports **72** offline tests passing in its normal terminal. That complete suite was not independently executed here. Passing the existing tests does not cover concurrent execution, merge-stage cancellation, full merge cost accounting or preservation of the active owner profile.

## I1 — P1: call cap and usage ledger do not cover actual work

Affected: `AnalysisService.execute_job`, `_merge_and_save_profile`, provider retry boundary, analysis estimate.

Probe: 65 synthetic messages, chunk size 5, 13 chunks, max_calls=14, FakeAIProvider.

Observed: `status=completed`, `provider_calls=14`, actual provider invocations **17**. Three intermediate merge calls were uncounted. A separate first-call failure used one provider call but recorded **0**. Adapter retries are also outside the service ledger.

Required: reserve/count a permitted attempt durably before dispatch, including failures, every merge level and retries. Never dispatch after the authorized cap. Record logical progress separately from attempted calls. Pause and resume every phase without repeating paid successful merge nodes. Include merge phases in the estimate. Ensure SDK-level automatic retries cannot escape the accounting boundary (disable untracked retries or account for them). Budget changes on resume must be explicit and validated, not an accidental silent expansion. Do not run a cap-exceeding call and merely report the excess later.

Regressions must compare actual fake/transport invocation count with the durable ledger and cap, including failed calls, retries, a 13-chunk merge near budget, and resumption during merge.

## I2 — P1: duplicate workers inflate coverage; cancellation can be lost

Affected: `execute_job`, `cancel_job`, `start_or_resume_analysis`, provider lifecycle.

Probe: run two `execute_job` coroutines for the same one-chunk/five-message job with a provider that yields once inside analyze_chunk.

Observed: **processed_unique_messages=10 / total_supported_messages=5**, completed_chunks=2 / total_chunks=1. Both workers processed the running chunk because the selector accepts any status other than completed. The API schedules another create_task even if the existing job is already running.

Probe: request cancellation from inside a merge callback (simulating cancellation while awaiting the provider).

Observed: cancel returned `paused_cancelled`, but the execution subsequently persisted a profile and finished **completed**.

Required: atomically claim one owner per job/corpus, make repeated start requests return the existing job without launching a duplicate, guard chunk and final-profile commits by current ownership/state, and make cancellation sticky across every provider await/merge phase. A cancelled old worker must not overwrite a resumed new worker. Keep explicit strong references/lifecycle management for background tasks and consume/sanitize exceptions. Share concurrency limits at the intended application/provider scope; constructing a new provider/semaphore per HTTP request does not establish a process-wide limit.

Regressions must use controlled Events/barriers (not timing guesses) to reproduce concurrent starts, cancellation during chunk and merge, and resume with an old worker still returning. Coverage must never exceed unique total, profiles must not duplicate through competing completion, and cancellation must not be overwritten.

## I3 — P1: alleged bounded hierarchy has no actual context-size bound

Affected: chunk planning, `_merge_and_save_profile`, provider/request models.

Probe: 51 one-message chunks. Recorded merge input batch sizes: `[5,5,5,5,5,5,5,5,5,5,1,11]`. The final merge grows with the corpus; only one reduction level is implemented. It is not a recursive bound. Chunking by 50 messages also leaves one arbitrarily large message unbounded. ReplyRequest accepted a **100,000-character context** without validation.

Required: documented/configurable byte, character or token bounds on every provider input and generated structured result, plus bounded recursive merging until all final inputs fit. Preserve evidence and style through every level. Account for overlap, metadata and current profile in the request envelope. For a message that cannot fit, split with traceable coverage semantics or report an explicit oversize limitation before paying for requests; never silently truncate it and claim complete analysis. Keep completed merge nodes durable for resumability/budget enforcement. Bound ordinary reply/profile inputs and exclusions too.

Test a hierarchy needing more than two levels with a spy provider that rejects oversize envelopes, oversized individual text, bounded profile findings and coverage consistency. Do not assert boundedness solely because one small fake merge returns successfully.

## I4 — P1: analysis silently replaces owner-edited active instructions

Affected: `_merge_and_save_profile`, `get_latest_profile`, profile/meme version storage.

Probe: create a valid owner profile with `update_profile(chat,0,'OWNER_EDIT_KEEP_THIS',...)`, then analyze the chat.

Observed: active profile version 2 is model-written, `edited_by_user=False`, and the owner's text is no longer the active instructions. Saving the old version in history is not preserving the owner's active choice. The merge reads the prior version before an await and derives the next version from that stale snapshot, so a concurrent owner edit also risks duplicate version numbers.

Required: AI analysis may produce a stored suggestion, but must not replace active owner edits without explicit application. Use transactional version allocation/conflict detection or a clear active/suggested-profile mechanism. Enforce uniqueness for chat/version and guard concurrent edits. Meme approvals/edits must belong to the intended profile version or have an explicit versioned override policy; retrieving every historical chat meme as if it belongs to the latest profile is not a snapshot. Only approved/current memory should feed runtime behavior and export. Preserve owner changes during in-flight analysis and resume.

Regressions: owner profile before analysis remains active; owner edit while merge is awaiting remains active; stale saves cannot create duplicate versions; rejected/current versus historical memes follow the documented runtime/export policy.

## I5 — P2: provider validation is permissive and error messages echo output

Affected: providers/base.py, providers/gemini.py, request models, ReplyService.

Probes:

- `ProviderReplyDecision(action='unsupported_action')` is accepted because action is an unrestricted string.
- A whitespace generated response is stored/returned as `action='generate'` with **text length 0**.
- A mocked invalid structured response with an identifiable synthetic string in `style.languages` caused the public ProviderError message to include that raw model-output string through `str(validation_error)`.

Required: validate action/mode/verdict/state enums and bounded payload/evidence shapes. Normalize/reject empty generated output before storing/sending. Use fixed, categorized public validation errors without echoing raw provider output, request URLs, chat excerpts or secrets. The original spec requires invalid model-selected quotes to fail closed; do not silently convert an invalid selection into an apparently successful model-grounded quote. If an explicit fallback mode is retained, label it as degraded deterministic behavior and document the contract change.

The current injection test uses a fake that always returns a fixed normal reply regardless of input. It proves only that this fake returned its fixed text. Test actual server validation and prompt construction/trust separation, and avoid claiming this establishes LLM prompt-injection resistance. Keep credentials and destination/tool authority outside model context.

## Live provider status

The agent report records one synthetic smoke request returning `quota_exceeded` / RESOURCE_EXHAUSTED. It does not establish a working live generation/structured-analysis path or the exact billing/quota cause. Codex did not repeat the live call. Keep live availability **unverified/blocked by the reported provider error**; do not silently substitute FakeAIProvider in production, change the user's credential/model, or repeatedly retry a permanent failure.

Update after the user's billing confirmation: task 02r now authorizes one bounded synthetic structured-output request, after repairs and offline verification, using the existing key/model with no retries. This supersedes the earlier no-live-request restriction only for that check. No real chat history or bulk analysis is authorized; positive billing balance alone does not establish working model access.

## Next action

Run `bash scripts/agy-task.sh 02r` in a normal local terminal. Repair I1-I5 in one focused pass, add regressions that reproduce the observed failures, and return for Codex independent review. Keep task 03 pending until task 02 is approved.

## Independent re-review after task 02r

Date: 2026-10-25, Europe/Kyiv. Decision remains **changes_required**.

The repair does improve default call reservation, durable merge nodes, duplicate-worker claiming, cancellation during merge, recursive item-count reduction, owner-instruction preservation, action enums and fail-closed quote decisions. The original 13-chunk/cap-14 regression and the submitted owner/cancellation tests pass independently. These changes are retained.

Independent verification of the current files:

- Core pytest command over config/importer/retrieval/migrations: **39 passed**, one deprecation warning, exit 0.
- Ruff: **passed**, exit 0.
- **21 existing async analysis/provider/prompt-construction tests** invoked directly with `asyncio.run`, isolated temporary databases and bounded awaits: passed. Provider calls in these checks were fakes/mocks.
- Full pytest run under a 20-second timeout: **exit 124**, not a passing suite. A separate selection including HTTP profile tests also stalled. The underlying runner issue is not established. The author's report of 87 passes in their normal terminal is separate evidence.
- New deterministic synthetic probes below used temporary databases, events and mocks. No live requests, real chat data or actual credentials were used.

### R1 — P1: failed chunks are skipped on resume and completion can be false

Affected: `create_or_resume_job`, `execute_job`, final merge gate.

Probe: 10 messages, two chunks, first provider call fails. Resume with a working fake. Result: **status=completed, coverage=5/10, chunk states=[failed, completed]**. The failed chunk is not returned to pending; the loop treats absence of pending/running work as complete and merges only successful chunks.

Repair: retry unfinished failed chunks using their original bounded payload, retain completed work/call charges, and require every planned chunk and exact unique-message coverage before completion. Apply the same rule to older resumable jobs and corrupted/incomplete findings: no silently suppressed parsing failure followed by successful completion. Test a failed split-message fragment too.

### R2 — P1: an old worker still mutates the resumed worker's chunk

Affected: chunk success, failure and cleanup writes around provider awaits.

Deterministic event ordering: old worker enters chunk call; cancel; resume; new worker enters the same chunk call; release and await old worker; release new worker. Result: **completed, coverage=15/10, completed_chunks=3/2**.

The obsolete worker's cleanup changes a chunk now owned by the new worker to pending. The new worker increments counters even if its completed-chunk UPDATE changed zero rows. Failure paths also write chunk state without guarding the current owner. A job-level ownership check outside the commit transaction does not protect these writes.

Repair: guard every chunk claim/cleanup/error/commit with current job and chunk ownership in the same transaction; invalidate old claims on cancellation/resume. Only count a genuinely committed transition once. Test both old-success and old-error returns while the replacement worker is held at an event barrier. Keep cancellation sticky and completed work intact.

### R3 — P1: explicit call caps can still grow silently or be exceeded by configured retries

Probes:

- A job paused at an explicitly chosen cap of **1** resumes without `max_calls` at **50**, taken from the global default. The report says an explicit increase is required, but the code does not enforce it.
- `GeminiProvider(max_retries=2)` with a mocked 429 and a job cap of **1** makes **3 SDK invocations**, while the durable ledger says **1**. The constructor/environment setting remains a supported escape from accounting.

Repair: preserve the saved cap when omitted; require an explicit valid higher cap for a budget pause. Disable unaccounted adapter retries for analysis (including non-default settings), or route each attempt through durable reservation. Explicitly configure SDK/transport single-attempt behavior rather than relying on default retry behavior; the installed SDK also contains implicit aiohttp connection-error retries when retry options are absent. Test the production adapter with a fake SDK/transport, not only FakeAIProvider. Invalid budget requests must return a stable 4xx budget error, not `404 chat_not_found` for an existing chat.

### R4 — P1: the real merge schema cannot be converted, and valid merge outputs break the next level

Offline test using the installed Google SDK's schema transformer in Developer API mode:

- `ChunkAnalysisResult`: conversion succeeds.
- `MergeProfileResult`: **ValueError: additionalProperties is only supported ... not in Gemini Developer API mode**. Its `structured: dict[str, Any]` produces the rejected schema.

This establishes a local production-path blocker independent of billing and without diagnosing the cause of the earlier live 400. Mocking `generate_content` bypasses this conversion and misses the defect.

A separate six-chunk probe returns a valid `MergeProfileResult` with about 2,700 characters of instructions. Conversion to `ChunkAnalysisResult` fails because its summary permits only 2,000 characters; 21-50 valid merge memes likewise exceed the intermediate's limit of 20. Intermediate conversion also replaces real style with defaults and discards `structured`.

Repair: use a bounded typed provider schema compatible with the installed SDK; test actual request construction/serialization for analysis, merge and reply using a non-network transport. Make intermediate outputs and subsequent inputs compatible by construction, preserving style, evidence and persisted-node resumability. Do not fix the failure by silently truncating analyzed history or losing style.

### R5 — P1: envelope and safe-error claims still exceed the implementation

Probes:

- A one-character message with a synthetic long sender produces **100,095 serialized characters** under the alleged 25,000-character chunk envelope. Only text lengths are counted.
- `ProfileUpdateRequest` accepts a **100,000-character structured value**. List item strings, structured maps and profile meme lists remain unbounded.
- A normal short reply context can send **200,125 characters of candidate payload** because complete long quote text and sender metadata are forwarded without a provider envelope.
- A valid long intermediate causes the outer `Analysis execution failed: {exception}` handler to expose a Pydantic `input_value=` dump and a prefix of synthetic private model content. Fixing the adapter's validation message alone does not fix the public service error.

Repair: enforce documented limits on complete serialized provider inputs, including names, metadata, current profile, quote candidates and all merge outputs; give every inference an explicit output budget. Bound nested provider/API strings, collections and structured values. Reject oversize analysis before spending, or split it with traceable coverage; exact stored quote text stays intact. Safe errors must be fixed categories/messages across service and adapter paths. Add spies on actual serialized requests, not just a sum of message text or merge-item counts.

### R6 — P2: editing a profile moves its historical meme snapshot

Probe: generate profile 1 with one meme, edit to profile 2. Profile 1's stored meme count changes from **1 to 0**. `update_profile` moves rows instead of copying/versioning them, despite its docstring saying it copies. Meme edits are scoped by chat and ID rather than the intended active version; null-profile fallback can also reintroduce unversioned memory.

Repair: preserve historical snapshots and scope edits to the expected active profile, with explicit inherited-meme identity semantics. Normalize existing multiple-active flags safely. Keep the successfully implemented owner-instruction preservation. A minimal way to inspect/apply saved AI suggestions can be finished with task 03's profile UI; it is not a reason to rewrite the profile system now.

### Provider status and next action

The user now reports API access working. Treat that as their access test; the local report still records the single authorized structured smoke attempt returning 400. This review made **zero live requests** and does not identify that 400's root cause. Fix the demonstrated schema-conversion blocker offline first. No additional live request or real archive upload is authorized by task 02r2.

Run `bash scripts/agy-task.sh 02r2`. Repair R1-R6 without restarting the application, then return for independent review. Task 03 remains pending. Human learning/feedback checkboxes remain separate and unchecked without actual feedback.

## Independent re-review after task 02r2 and authorization for task 03

Date: 2026-10-25, Europe/Kyiv.

The main R1-R6 repairs are independently confirmed for new well-formed jobs: failed-work resume, old-success/old-error worker isolation, explicit saved caps, single-attempt adapter behavior, compatible SDK schemas and intermediates, safe service errors, bounded quote views, owner-profile preservation and historical meme snapshots. Migration 6 adds durable payloads and per-chunk ownership. Keep these improvements.

Independent checks:

- Core pytest over config/importer/retrieval/migrations: **40 passed**, one deprecation warning, exit 0.
- Four additional synchronous restart/sanitization/historical-meme/active-profile-normalization tests: **4 passed**, one deprecation warning, exit 0.
- **32 existing async tests** called directly with `asyncio.run`, temporary databases and bounded awaits: passed. This includes all new R1-R5 async regressions and actual Google SDK request serialization/response parsing through `httpx.MockTransport` for analysis, merge and reply. No network was used.
- Ruff lint and format checks: **passed**, 73 files already formatted.
- Complete pytest under a 20-second timeout: **exit 124**. The full suite is not independently verified here; the author reports **102 passed** in their normal terminal. Do not label the coding author's report as the independent review.
- Two additional synthetic probes reproduced G1-G2 below. No live API request or real archive was used.

### G1 — required preflight: legacy payload recovery still discards history

Affected: `execute_job` fallback using `re_planned[0][2]`.

Probe: one legacy count-based chunk holding five supported messages of 8,000 characters each (40,000 total), no `payload_json` and no payload array in `findings_json`, source IDs `[1,2,3,4,5]`. This represents the older planner's resumable data shape. The fallback replans the recovered messages but sends only the first new chunk.

Observed: **status=completed, coverage=5/5**, while the spy saw only source IDs **[1,2]** and **15,987 payload characters**. All five IDs were credited from the old source-ID ledger. A missing/corrupt stored payload must not be silently recovered as a prefix and counted as whole-history coverage.

Required: either durably replace/reconcile the unfinished legacy plan with every bounded fragment and accurate unique coverage, preserving successful work/charges; or stop before dispatch with a stable, non-resumable recovery-required error and an explicit safe recovery route. Never take only the first re-planned chunk. Test the legacy shape, absent and malformed payloads, and partial split fragments. If exact reconstruction is impossible, honest rejection is acceptable for the POC; do not reset user data or completed-call accounting.

### G2 — required preflight: valid findings can never pass the fixed-size merge

Affected: merge planning and pre-dispatch accounting.

Probe: five completed chunks, each with a schema-valid 9,000-character summary (below the allowed 10,000). Exercise the real production adapter's prompt guard, mocking only the eventual structured inference method. The fixed five-item root exceeds the 40,000-character merge envelope.

Observed: **failed / merge_envelope_exceeded**, five actual inference dispatches, but **provider_calls=6**. Resume unchanged: same error, no new inference, **provider_calls=7**. The merge structure never adapts, and local envelope rejection consumes ledger budget.

Required: plan merge requests by the actual serialized envelope, reducing fan-in and adding durable levels when necessary. Keep all findings/style/evidence; never silently truncate them to fit. Ensure valid bounded outputs are consumable by a subsequent stage; handle an individually oversized existing node explicitly. Validate local payload/schema/envelope before reserving a dispatch attempt, or expose a clearly separate reservation count so non-dispatch failure cannot pretend to be an actual API call. Repeated deterministic local rejection must not consume the call budget. Preserve single-attempt transport, cancellation ownership and completed-node reuse. Estimates should distinguish a lower-bound/planned count from data-dependent extra merge work. Add a real adapter/serialized-prompt spy regression for this exact five-by-9,000 case and resume at the budget boundary.

### Progression decision

Codex explicitly authorizes `bash scripts/agy-task.sh 03`: repair G1-G2 first and run their regressions, then implement the already planned web experience, suggestion inspect/apply flow and optional Telegram adapter in that same run. Do not stop for another approval between the preflight and the UI work. If preflight cannot be made safe, report the concrete blocker instead of claiming completion.

Final acceptance of tasks 02 and 03 will use the combined result, including G1-G2 checks. This allows useful implementation to progress while retaining the observed correctness requirements. It does not mark the human learner checkpoints, real Gemini generation, Telegram posting or hackathon submission complete.

The user reports API access working. The recorded earlier structured smoke still returned 400; SDK serialization now passes offline, which does not prove remote server acceptance. This review made zero live requests. Task 03 permits offline verification only; real chat uploads and Telegram posting remain unauthorized.

The newly attached `Pasted text(2).txt` matched the supplied file ID, but text extraction returned no readable content. This review is grounded in actual workspace source, the local task report and executed probes, not presumed attachment contents.
