---
status: approved
origin: orchestrator-planning-input
official_skill_workflow: routed_via_devpost_learn_pack
approved_by: codex-architect-review
---

# Scope: Vkyd

## User's idea

Generate custom Telegram bots that learn a particular chat's communication style, inside jokes, favorite stickers and quotes from its available exported history. Use an API model and editable instructions/memory, not LoRA. Include control over joke frequency, sources and feedback.

## Kernel

Turn a chat export into an editable humor profile and an attributable quote memory, then produce an appropriate reply for a new conversation fragment.

## First complete release

1. Import a Telegram Desktop single-chat JSON export.
2. Normalize every supported message, retaining source IDs, text entities, reply relationships and sticker metadata; count excluded or unsupported records.
3. Index the complete imported text corpus locally.
4. Analyze all supported text in bounded chunks, with durable progress, a call budget and explicit coverage.
5. Generate an editable evidence-backed group humor profile, avoiding unsupported claims about individual preferences.
6. In a playground, select quote-only, generated text, or automatic choice including silence. Display source evidence separately from the visible reply.
7. Accept feedback and persist it to ranking/preferences without modifying model weights.
8. Connect an optional Telegram bot to an explicitly bound group; reply only when invoked.

## Deliberate first-release boundaries

- Index sticker metadata and allow stickers received by the configured bot to be registered. Sending stickers is optional until a valid bot-specific file ID exists.
- No OCR/transcription of every photo, voice note or video. Count these as unsupported content and report that limit.
- No autonomous collection from external meme sites or channels. Users may curate additional quotes/stickers later.
- No spontaneous group participation, hosted multi-user SaaS, automatic bot-factory provisioning, or fine-tuning.
- No invented claims of being a real participant; bots use their own identity.

## Definition of done

A clean checkout can run locally with documented commands. Two synthetic chat exports yield isolated profiles and memories. A Ukrainian conversation fragment retrieves an authentic relevant quote from the correct chat. Real Gemini integration is verified separately from mocked tests, or clearly reported unavailable. A working Telegram round trip is separately reported as verified or blocked by missing token/group configuration.

## Decisions already supplied

- User authorized starting implementation and gave Codex architecture/review responsibility.
- User explicitly requested that console `agy` write application code.
- User authorized using their Google AI Studio credential. It belongs in ignored local configuration only.
- The team has three people and is targeting Build With AI: Basics.
- Repository starts without application code.

These are inputs to the official skill workflow, not evidence that the workflow ran.
