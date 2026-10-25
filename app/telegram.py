"""Telegram polling worker, trigger parsing, dedupe, and group binding adapter."""

import argparse
import asyncio
import contextlib
import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

import httpx

from app.analysis import AnalysisService
from app.config import settings
from app.db import Database, get_db
from app.models import ReplyRequest
from app.providers.base import BaseAIProvider
from app.replies import ReplyService
from app.retrieval import MemoryRetriever

logger = logging.getLogger("vkyd.telegram")


class TelegramError(Exception):
    """Sanitized Telegram error that never exposes bot tokens or raw webhook URLs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TelegramTransport(ABC):
    """Abstract async transport for Telegram Bot API operations."""

    @abstractmethod
    async def get_me(self) -> dict[str, Any]:
        """Fetch active bot user profile."""
        ...

    @abstractmethod
    async def get_updates(
        self, offset: int | None = None, timeout: int = 30
    ) -> list[dict[str, Any]]:
        """Fetch pending updates via long-polling."""
        ...

    @abstractmethod
    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send plain-text message to Telegram chat."""
        ...

    @abstractmethod
    async def send_sticker(
        self,
        chat_id: int | str,
        sticker: str,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send validated sticker file_id to Telegram chat."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close transport client session."""
        ...


class HttpTelegramTransport(TelegramTransport):
    """Official HTTP transport using httpx.AsyncClient with masked error handling."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.telegram.org",
        timeout: float = 35.0,
    ) -> None:
        if not token or not token.strip():
            raise TelegramError("credential_missing", "Telegram bot token is not configured.")
        self._token = token.strip()
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/bot{self._token}",
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    def _mask_token_in_error(self, exc: Exception) -> TelegramError:
        msg = str(exc).replace(self._token, "[REDACTED_TELEGRAM_TOKEN]")
        return TelegramError("transport_error", f"Telegram request failed: {msg}")

    async def get_me(self) -> dict[str, Any]:
        try:
            resp = await self._client.get("/getMe")
            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "Unknown error")
                raise TelegramError("api_error", f"Telegram getMe failed: {desc}")
            return data["result"]
        except TelegramError:
            raise
        except Exception as e:
            raise self._mask_token_in_error(e) from None

    async def get_updates(
        self, offset: int | None = None, timeout: int = 30
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = offset
        try:
            resp = await self._client.get("/getUpdates", params=params)
            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "Unknown error")
                raise TelegramError("api_error", f"Telegram getUpdates failed: {desc}")
            return data.get("result", [])
        except TelegramError:
            raise
        except Exception as e:
            raise self._mask_token_in_error(e) from None

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        try:
            resp = await self._client.post("/sendMessage", json=payload)
            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "Unknown error")
                raise TelegramError("api_error", f"Telegram sendMessage failed: {desc}")
            return data.get("result", {})
        except TelegramError:
            raise
        except Exception as e:
            raise self._mask_token_in_error(e) from None

    async def send_sticker(
        self,
        chat_id: int | str,
        sticker: str,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "sticker": sticker,
        }
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        try:
            resp = await self._client.post("/sendSticker", json=payload)
            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "Unknown error")
                raise TelegramError("api_error", f"Telegram sendSticker failed: {desc}")
            return data.get("result", {})
        except TelegramError:
            raise
        except Exception as e:
            raise self._mask_token_in_error(e) from None

    async def close(self) -> None:
        await self._client.aclose()


class FakeTelegramTransport(TelegramTransport):
    """In-memory test double for deterministic Telegram integration tests."""

    def __init__(
        self,
        bot_id: int = 12345678,
        bot_username: str = "vkyd_test_bot",
        bot_name: str = "Vkyd Test Bot",
    ) -> None:
        self.bot_id = bot_id
        self.bot_username = bot_username
        self.bot_name = bot_name
        self.updates_queue: list[dict[str, Any]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.sent_stickers: list[dict[str, Any]] = []

    async def get_me(self) -> dict[str, Any]:
        return {
            "id": self.bot_id,
            "is_bot": True,
            "first_name": self.bot_name,
            "username": self.bot_username,
        }

    async def get_updates(
        self, offset: int | None = None, timeout: int = 30
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for upd in self.updates_queue:
            uid = upd.get("update_id", 0)
            if offset is not None and uid < offset:
                continue
            results.append(upd)
        self.updates_queue = remaining
        return results

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        record = {
            "chat_id": str(chat_id),
            "text": text,
            "reply_to_message_id": reply_to_message_id,
            "sent_at": datetime.now(UTC).isoformat(),
        }
        self.sent_messages.append(record)
        return {"message_id": len(self.sent_messages), **record}

    async def send_sticker(
        self,
        chat_id: int | str,
        sticker: str,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        record = {
            "chat_id": str(chat_id),
            "sticker": sticker,
            "reply_to_message_id": reply_to_message_id,
            "sent_at": datetime.now(UTC).isoformat(),
        }
        self.sent_stickers.append(record)
        return {"message_id": len(self.sent_stickers), **record}

    async def close(self) -> None:
        pass


def parse_trigger(
    message: dict[str, Any],
    bot_id: int | str,
    bot_username: str,
) -> tuple[bool, str, str]:
    """
    Evaluates whether an incoming Telegram message targets this bot.
    Returns: (is_triggered, mode, context_or_query).

    Modes:
    - 'quote': quote-only retrieval requested.
    - 'generate': freestyle humor generated response.
    - 'auto': grounded personality style response.
    - 'system_help': built-in help text.
    - 'system_status': built-in status info.
    """
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        return False, "", ""

    lower_username = bot_username.lower()

    # 1. Command detection (e.g. /quote, /quote@bot_username, /humor)
    # Commands addressed to another bot MUST be rejected before reply or mention checks.
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd_token = parts[0].lower()
        remainder = parts[1].strip() if len(parts) > 1 else ""

        # Extract base command and optional @bot target
        if "@" in cmd_token:
            cmd_name, target_bot = cmd_token.split("@", 1)
            if target_bot != lower_username:
                # Command explicitly targeted a different bot in group - REJECT IMMEDIATELY
                return False, "", ""
        else:
            cmd_name = cmd_token

        if cmd_name == "/quote":
            return True, "quote", remainder
        if cmd_name in ("/reply", "/speak", "/humor"):
            return True, "auto", remainder or "say something funny"
        if cmd_name == "/joke":
            return True, "generate", remainder or "tell a joke"
        if cmd_name in ("/help", "/start"):
            return True, "system_help", ""
        if cmd_name == "/status":
            return True, "system_status", ""
        # Unknown commands are ignored without autonomous replies
        return False, "", ""

    # 2. Direct reply to this bot's message
    reply_to = message.get("reply_to_message")
    if reply_to:
        reply_sender = reply_to.get("from") or {}
        if str(reply_sender.get("id")) == str(bot_id):
            return True, "auto", text

    # 3. Mention detection (@bot_username in text)
    mention_pattern = rf"@\b{re.escape(lower_username)}\b"
    if re.search(mention_pattern, text.lower()):
        cleaned_context = re.sub(mention_pattern, "", text, flags=re.IGNORECASE).strip()
        return True, "auto", cleaned_context or "say something"

    # No autonomous interjections
    return False, "", ""


def format_quote_attribution(
    text: str,
    sender_name: str | None,
    sent_at: str | None,
    source_message_id: int | None = None,
    max_telegram_chars: int = 4000,
) -> str:
    """
    Format exact quote with sender name, date, and provenance attribution.
    Handles oversized quotes and oversized metadata by safely bounding all components
    while preserving explicit truncation and source identity.
    Guarantees total character length <= max_telegram_chars.
    """
    sender = (sender_name or "Participant").strip()
    date_str = ""
    if sent_at:
        with contextlib.suppress(Exception):
            dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
            date_str = dt.strftime("%d.%m.%Y")

    trunc_marker = " [...]"

    # Allocate attribution budget (at most 500 chars or half of max_telegram_chars)
    max_attr_len = min(500, max(50, max_telegram_chars // 2))
    reserved_suffix_len = (
        (len(date_str) + 4 if date_str else 0)
        + (len(str(source_message_id)) + 4 if source_message_id else 0)
        + 10
    )
    max_sender_len = max(20, max_attr_len - reserved_suffix_len)
    if len(sender) > max_sender_len:
        sender = sender[: max_sender_len - len(trunc_marker)] + trunc_marker

    provenance_parts = [sender]
    if date_str:
        provenance_parts.append(date_str)
    if source_message_id:
        provenance_parts.append(f"#{source_message_id}")
    provenance_line = f"— {', '.join(provenance_parts)}"

    prefix = "«"
    suffix = f"»\n\n{provenance_line}"
    overhead = len(prefix) + len(suffix)

    available_text_len = max(0, max_telegram_chars - overhead)
    if len(text) <= available_text_len:
        quote_body = text
    else:
        slice_len = max(0, available_text_len - len(trunc_marker))
        quote_body = text[:slice_len] + trunc_marker

    result = f"«{quote_body}»\n\n{provenance_line}"
    if len(result) > max_telegram_chars:
        result = result[:max_telegram_chars]
    return result


class TelegramBotService:
    """
    Production-grade Telegram polling adapter.
    Enforces environment allowlist, durable update dedupe, group bindings,
    cooldown enforcement, trigger filtering, and safe attribute-grounded responses.
    """

    def __init__(
        self,
        db: Database,
        transport: TelegramTransport,
        reply_service: ReplyService,
        provider: BaseAIProvider | None = None,
        allowed_chat_ids: set[str] | None = None,
    ) -> None:
        self.db = db
        self.transport = transport
        self.reply_service = reply_service
        self.provider = provider
        self.allowed_chat_ids = (
            allowed_chat_ids
            if allowed_chat_ids is not None
            else settings.parsed_telegram_allowed_chat_ids
        )
        self.bot_id: int | None = None
        self.bot_username: str | None = None
        self.bot_name: str | None = None

    async def initialize(self) -> dict[str, Any]:
        """Fetch bot identity from Telegram."""
        me = await self.transport.get_me()
        self.bot_id = me["id"]
        self.bot_username = me.get("username", "")
        self.bot_name = me.get("first_name", "Vkyd Bot")
        logger.info("Telegram bot initialized: @%s (ID: %s)", self.bot_username, self.bot_id)
        return me

    def is_update_processed(self, update_id: int) -> bool:
        """Check durable deduplication table."""
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM telegram_updates WHERE update_id = ?;",
                (update_id,),
            ).fetchone()
            return row is not None

    def mark_update_processed(self, update_id: int, telegram_chat_id: str | None) -> None:
        """Record update_id durably in SQLite transaction."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO telegram_updates (update_id, telegram_chat_id, processed_at)
                VALUES (?, ?, ?);
                """,
                (update_id, telegram_chat_id, now),
            )

    def get_effective_binding(self, telegram_chat_id: str | int) -> dict[str, Any] | None:
        """
        Validate group against both environment allowlist AND database binding.
        Returns binding dictionary if valid and enabled; otherwise None.
        """
        str_id = str(telegram_chat_id)

        # 1. Environment allowlist check (binding MUST NOT bypass allowlist)
        # Empty or unset allowlist means NO authorized groups (explicit allowlist required)
        if not self.allowed_chat_ids or str_id not in self.allowed_chat_ids:
            logger.debug("Telegram chat %s not in environment allowlist.", str_id)
            return None

        # 2. Database binding check
        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT b.*, c.name as chat_name
                FROM telegram_bindings b
                JOIN chats c ON b.chat_id = c.id
                WHERE b.telegram_chat_id = ? AND b.enabled = 1;
                """,
                (str_id,),
            ).fetchone()
            return dict(row) if row else None

    def check_cooldown(self, binding: dict[str, Any]) -> bool:
        """
        Returns True if cooldown is active (do not send).
        Returns False if send is permitted.
        """
        last_sent = binding.get("last_sent_at")
        cooldown_sec = binding.get("cooldown_seconds", 10)
        if not last_sent:
            return False
        try:
            last_dt = datetime.fromisoformat(last_sent.replace("Z", "+00:00"))
            elapsed = (datetime.now(UTC) - last_dt).total_seconds()
            return elapsed < cooldown_sec
        except Exception:
            return False

    def update_last_sent(self, telegram_chat_id: str | int) -> None:
        """Update last_sent_at timestamp in telegram_bindings."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE telegram_bindings SET last_sent_at = ? WHERE telegram_chat_id = ?;",
                (now, str(telegram_chat_id)),
            )

    def _resolve_validated_sticker(self, chat_id: str, sticker_id: str | None) -> str | None:
        """
        Catalog lookup: send ONLY validated sticker file_ids owned by the active bot.
        Returns validated file_id, or None if unverified.
        """
        if not sticker_id or not self.bot_id:
            return None

        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT file_id, bot_id FROM stickers
                WHERE id = ? AND chat_id = ?;
                """,
                (sticker_id, chat_id),
            ).fetchone()
            if row and row["file_id"] and str(row["bot_id"]) == str(self.bot_id):
                return row["file_id"]
        # Sticker is unverified or owned by another bot: fallback cleanly
        return None

    async def process_update(self, update: dict[str, Any]) -> dict[str, Any] | None:
        """
        Process single Telegram update with dedupe, allowlist guard, trigger check, and send.
        Returns execution summary or None.
        """
        update_id = update.get("update_id")
        if update_id is None:
            return None

        # 1. Durable dedupe check
        if self.is_update_processed(update_id):
            return None

        message = update.get("message")
        if not message:
            self.mark_update_processed(update_id, None)
            return None

        msg_chat = message.get("chat") or {}
        telegram_chat_id = str(msg_chat.get("id", ""))
        message_id = message.get("message_id")

        self.mark_update_processed(update_id, telegram_chat_id)

        # 2. Binding and environment allowlist verification
        binding = self.get_effective_binding(telegram_chat_id)
        if not binding:
            # Group is not allowlisted or unbound: do NOT call model or send message
            logger.info(
                "Received update %s from chat_id=%s (ignored: not allowlisted or unbound).",
                update_id,
                telegram_chat_id,
            )
            return None

        # 3. Trigger evaluation
        if not self.bot_id or not self.bot_username:
            await self.initialize()

        assert self.bot_id is not None
        assert self.bot_username is not None

        is_triggered, mode, query_context = parse_trigger(message, self.bot_id, self.bot_username)
        if not is_triggered:
            # No autonomous interjections
            logger.info(
                "Received update %s from chat_id=%s (ignored: no command/mention/reply trigger).",
                update_id,
                telegram_chat_id,
            )
            return None

        logger.info(
            "Processing update %s from chat_id=%s (mode=%s, profile=%s).",
            update_id,
            telegram_chat_id,
            mode,
            binding.get("chat_name"),
        )

        # 4. Handle built-in system commands
        if mode == "system_help":
            help_text = (
                f"👋 Привіт! Я {self.bot_name} (@{self.bot_username}).\n\n"
                f"Прив'язаний профіль: «{binding.get('chat_name', '')}»\n\n"
                "Команди:\n"
                "• /quote [тема] — знайти автентичну цитату з історії\n"
                "• /reply або /humor [контекст] — згенерувати відповідь у стилі чату\n"
                "• /joke [тема] — згенерувати жарт у стилі групи\n"
                "• /status — перевірити статус бота та кулдаун\n"
                f"Також відповідаю на прямі відповіді або згадки @{self.bot_username}."
            )
            await self.transport.send_message(
                chat_id=telegram_chat_id,
                text=help_text,
                reply_to_message_id=message_id,
            )
            return {"action": "system_help", "chat_id": telegram_chat_id}

        if mode == "system_status":
            status_text = (
                f"🤖 Статус бота @{self.bot_username}:\n"
                f"• Чат ID: {telegram_chat_id}\n"
                f"• Профіль: {binding.get('chat_name')}\n"
                f"• Кулдаун: {binding.get('cooldown_seconds')}с\n"
                f"• AI провайдер: {'Налаштовано' if self.provider else 'Тільки цитати'}"
            )
            await self.transport.send_message(
                chat_id=telegram_chat_id,
                text=status_text,
                reply_to_message_id=message_id,
            )
            return {"action": "system_status", "chat_id": telegram_chat_id}

        # 5. Cooldown check
        if self.check_cooldown(binding):
            logger.info("Cooldown active for Telegram chat %s; skipping send.", telegram_chat_id)
            return {"action": "cooldown_skipped", "chat_id": telegram_chat_id}

        # 6. Generate attributable grounded reply
        req_mode = "quote" if mode == "quote" else ("generate" if mode == "generate" else "auto")
        ctx = (query_context or "").strip()
        if len(ctx) > 4000:
            ctx = ctx[:4000]
        if not ctx:
            ctx = " "
        reply_req = ReplyRequest(
            context=ctx,
            mode=req_mode,
        )

        try:
            decision = await self.reply_service.generate_reply(
                chat_id=binding["chat_id"],
                request=reply_req,
                provider=self.provider,
            )
        except Exception as gen_err:
            logger.warning(
                "Reply generation failed for update %s: %s",
                update_id,
                type(gen_err).__name__,
            )
            return None

        # 7. Deliver response
        sent_info: dict[str, Any] = {"decision": decision.action}

        if decision.action == "quote" and decision.quote:
            # Check for validated sticker
            val_sticker = self._resolve_validated_sticker(binding["chat_id"], decision.sticker_id)
            if val_sticker:
                await self.transport.send_sticker(
                    chat_id=telegram_chat_id,
                    sticker=val_sticker,
                    reply_to_message_id=message_id,
                )
                sent_info["sticker_sent"] = True

            formatted_text = format_quote_attribution(
                text=decision.quote.text,
                sender_name=decision.quote.sender_name,
                sent_at=decision.quote.sent_at,
                source_message_id=decision.quote.source_message_id,
            )
            await self.transport.send_message(
                chat_id=telegram_chat_id,
                text=formatted_text,
                reply_to_message_id=message_id,
            )
            self.update_last_sent(telegram_chat_id)
            sent_info["text_sent"] = True
            logger.info("Sent quote reply to chat_id=%s.", telegram_chat_id)

        elif decision.action == "generate" and decision.text:
            await self.transport.send_message(
                chat_id=telegram_chat_id,
                text=decision.text,
                reply_to_message_id=message_id,
            )
            self.update_last_sent(telegram_chat_id)
            sent_info["text_sent"] = True
            logger.info("Sent generated reply to chat_id=%s.", telegram_chat_id)

        return sent_info

    async def poll_once(self, offset: int | None = None) -> tuple[int, int]:
        """
        Poll single batch of updates.
        Returns: (processed_count, next_offset).
        """
        updates = await self.transport.get_updates(offset=offset, timeout=5)
        processed = 0
        max_id = offset or 0
        for upd in updates:
            uid = upd.get("update_id", 0)
            if uid >= max_id:
                max_id = uid + 1
            try:
                await self.process_update(upd)
            except Exception as e:
                logger.warning(
                    "Error processing update %s: %s",
                    uid,
                    type(e).__name__,
                )
            processed += 1
        return processed, max_id

    async def poll_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Run long-polling loop until stop_event is set."""
        await self.initialize()
        offset: int | None = None
        logger.info("Starting Telegram polling for @%s...", self.bot_username)

        while stop_event is None or not stop_event.is_set():
            try:
                updates = await self.transport.get_updates(offset=offset, timeout=20)
                for upd in updates:
                    uid = upd.get("update_id", 0)
                    offset = max(offset or 0, uid + 1)
                    await self.process_update(upd)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Polling error: %s; sleeping 5s...", e)
                await asyncio.sleep(5)


def build_telegram_bot_service(
    db: Database | None = None,
    transport: TelegramTransport | None = None,
    provider: BaseAIProvider | None = None,
) -> TelegramBotService:
    """Factory creating fully wired TelegramBotService instance."""
    database = db or get_db()
    database.run_migrations()

    if transport is None:
        token = settings.get_telegram_bot_token()
        if not token:
            raise TelegramError("credential_missing", "TELEGRAM_BOT_TOKEN is not configured.")
        transport = HttpTelegramTransport(token=token)

    from app.main import get_ai_provider

    ai_prov = provider if provider is not None else get_ai_provider()
    retriever = MemoryRetriever(database)
    analysis_service = AnalysisService(database)
    reply_service = ReplyService(database, retriever, analysis_service)

    return TelegramBotService(
        db=database,
        transport=transport,
        reply_service=reply_service,
        provider=ai_prov,
        allowed_chat_ids=settings.parsed_telegram_allowed_chat_ids,
    )


async def run_bot_cli(args: argparse.Namespace) -> None:
    """CLI runner function."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not settings.is_telegram_configured:
        print("ERROR: TELEGRAM_BOT_TOKEN is not configured in .env.")
        return

    service = build_telegram_bot_service()
    try:
        me = await service.initialize()
        print(f"Logged in as @{me.get('username')} (ID: {me.get('id')})")
        allowed_repr = (
            list(service.allowed_chat_ids)
            if service.allowed_chat_ids
            else "NONE (all disabled; explicit allowlist required)"
        )
        print(f"Allowlisted groups: {allowed_repr}")

        if args.once:
            print("Polling single batch...")
            count, _ = await service.poll_once()
            print(f"Processed {count} updates.")
        else:
            print("Starting long polling... Press Ctrl+C to stop.")
            await service.poll_forever()
    finally:
        await service.transport.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vkyd Telegram Bot Poller")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit")
    parsed_args = parser.parse_args()
    try:
        asyncio.run(run_bot_cli(parsed_args))
    except KeyboardInterrupt:
        print("\nBot stopped cleanly.")
