---
status: approved
origin: orchestrator-planning-input
official_skill_workflow: routed_via_devpost_learn_pack
approved_by: codex-architect-review
---

# Product requirements: Vkyd

## Audience and promise

Small friend groups with recurring jokes and a long Telegram history. Preserve the group's shared references and use them at a fitting moment. Humor quality is subjective; the app must make its evidence and controls visible rather than promise every joke will land.

## Primary flow

Import JSON -> inspect import report -> explicitly start analysis -> review/edit profile -> try a conversation fragment -> see reply and provenance -> rate it -> optionally connect Telegram.

## Interface

One responsive web workspace with four tabs: Memory, Style, Playground, Telegram. A persistent chat selector keeps the active profile explicit. Use an editorial scrapbook feel: warm paper, dark ink, coral highlights, compact quote cards, generous readable spacing, a clear source drawer. English UI for the submission; preserve Ukrainian/surzhyk content exactly. No framework/CDN dependency is necessary for the first build.

### Memory

- Upload `.json` with a visible size limit; show validation errors before doing any AI work.
- Display imported records, supported text, service records, empty/media-only messages, unsupported media and duplicate counts.
- Search results show exact quotes with sender/date, reply context and provenance.
- Full archive analysis is an explicit action, with progress, estimated chunk/call count, budget, cancellation and resumption.
- Separate import coverage, AI analysis coverage and media understanding coverage. Imported is not the same as analyzed.

### Style

- Show languages, reply length, register, humor techniques and local memes.
- Each inferred meme has evidence IDs, a contextual explanation, uncertainty and editable approval state.
- Profile text is editable and versioned; analysis reruns do not silently overwrite user edits.
- Export a self-contained instruction document and structured profile without credentials. Explain that instructions need a runtime that loads them.
- Only owner-approved chat settings are instructions; quotes and source messages remain untrusted data.

### Playground

- Enter a short conversation, choose mode and optionally exclude particular quotes.
- Modes: quote-only, generated, auto (quote/generated/silent). Optional sticker mode becomes available only with usable assets.
- Visible answer is distinct from provenance, retrieval matches and debug explanation.
- Feedback: landed, missed, overused. State that this adjusts ranking, not model weights.
- Clearly label offline demo, missing provider, provider failure, quota failure and real AI success. Never disguise a heuristic response as AI output.

### Telegram

- Token is configured in server environment only; show configured/not configured, never its value.
- Explicitly bind a Telegram group to an imported chat profile; no first-chat or last-chat defaults.
- Allowlist enforcement, per-chat rate limit and pause control.
- First mode responds only to explicit command, mention or reply to this bot.
- Replies to other people or other bots are not triggers by themselves.
- Setup is available without any bot token; actual polling/sending starts only after explicit operator action and valid binding.

## Edge cases

Malformed JSON; wrong root shape; Telegram multi-chat account export instead of single chat; no text; text entity arrays; missing senders; source ID collisions across chats; giant messages; missing reply parents; duplicated imports; old and new date formats; model timeout, malformed structured output, refusal or unavailable model; unknown/cross-chat quote ID; missing sticker files; stale jobs; concurrent requests; no relevant memory.

## Acceptance experience

The app remains usable for importing, browsing and quote-only search without paid APIs. AI profile generation and generated replies clearly require a functioning provider. A failed call preserves the archive and job checkpoint. The public demo uses synthetic data with two visibly different group styles.
