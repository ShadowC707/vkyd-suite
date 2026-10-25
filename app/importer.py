"""Single-chat Telegram JSON importer with rich-text normalization and atomic transactions."""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.db import Database
from app.models import ChatSummary, ImportReport, ImportResponse
from app.predicates import is_supported_message, is_supported_text_content

__all__ = [
    "ImporterError",
    "SQLITE_INT_MAX",
    "SQLITE_INT_MIN",
    "TelegramImporter",
    "extract_raw_text",
    "is_supported_message",
    "is_supported_text_content",
    "parse_message_date",
    "parse_source_id",
    "parse_string_field",
    "parse_tz",
]

SQLITE_INT_MIN = -9223372036854775808
SQLITE_INT_MAX = 9223372036854775807


class ImporterError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def parse_source_id(
    val: Any,
    field_name: str = "message ID",
    idx: int | None = None,
    error_code: str = "invalid_message_id",
) -> int:
    """Validate and parse a source ID within SQLite signed 64-bit integer bounds."""
    loc = f" at index {idx}" if idx is not None else ""
    if val is None or isinstance(val, bool):
        raise ImporterError(
            error_code,
            f"Record{loc} has invalid {field_name}: boolean or null values are not accepted.",
        )
    if isinstance(val, int):
        parsed = val
    elif isinstance(val, str):
        s = val.strip()
        if not re.fullmatch(r"^-?\d+$", s):
            raise ImporterError(
                error_code,
                f"Record{loc} has non-integer {field_name}: '{val}'.",
            )
        try:
            parsed = int(s)
        except ValueError as e:
            raise ImporterError(
                error_code,
                f"Record{loc} has invalid {field_name}: '{val}'.",
            ) from e
    elif isinstance(val, float):
        raise ImporterError(
            error_code,
            f"Record{loc} has invalid {field_name}: floating-point values are rejected ({val}).",
        )
    else:
        raise ImporterError(
            error_code,
            f"Record{loc} has invalid {field_name} of type {type(val).__name__}.",
        )

    if not (SQLITE_INT_MIN <= parsed <= SQLITE_INT_MAX):
        raise ImporterError(
            error_code,
            f"Record{loc} {field_name} {parsed} exceeds 64-bit integer bounds.",
        )
    return parsed


def parse_string_field(val: Any, field_name: str, idx: int) -> str | None:
    if val is None:
        return None
    if isinstance(val, (dict, list, bool)):
        raise ImporterError(
            "malformed_field",
            f"Record at index {idx} has invalid {field_name}: composite/bool not accepted.",
        )
    return str(val)


def extract_raw_text(raw_val: Any, idx: int | None = None) -> str:
    """
    Extract exact concatenated text from Telegram text field without modifying whitespace.
    Handles plain strings, lists of strings, and list of entity objects:
    [{"type": "bold", "text": "hello"}, " world"]
    Validates structure without coercing arbitrary objects into text.
    """
    loc = f" at index {idx}" if idx is not None else ""
    if raw_val is None:
        return ""
    if isinstance(raw_val, str):
        return raw_val
    if isinstance(raw_val, list):
        parts: list[str] = []
        for item_idx, item in enumerate(raw_val):
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text_val = item.get("text", "")
                if not isinstance(text_val, str):
                    raise ImporterError(
                        "malformed_text_entity",
                        f"Record{loc} entity at {item_idx} has non-string text field.",
                    )
                parts.append(text_val)
            else:
                raise ImporterError(
                    "malformed_text_entity",
                    f"Record{loc} part at {item_idx} has invalid type {type(item).__name__}.",
                )
        return "".join(parts)
    raise ImporterError(
        "malformed_text",
        f"Record{loc} text must be a string or list of text parts, got {type(raw_val).__name__}.",
    )


def parse_tz(tz_str: str | None) -> timezone | ZoneInfo | None:
    if not tz_str:
        return None
    s = tz_str.strip()
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})?", s)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        mins = int(m.group(3) or 0)
        return timezone(sign * timedelta(hours=hours, minutes=mins))
    try:
        return ZoneInfo(s)
    except Exception:
        return None


def parse_message_date(
    date_val: Any,
    date_unixtime_val: Any,
    import_timezone: str | None = None,
) -> tuple[str | None, str]:
    """
    Parse message date to (iso_utc_string_or_none, raw_date_string).
    1. Prefer valid date_unixtime as the canonical absolute instant.
    2. If absent/invalid, parse raw date with explicit timezone offset.
    3. If naive and import_timezone is provided, localize to that timezone.
    4. If naive and no import_timezone, record sent_at as None (unknown instant).
    Never invent current time or assume UTC for naive local dates.
    """
    raw_date = str(date_val or "") if not isinstance(date_val, (dict, list, bool)) else ""

    # 1. Prefer date_unixtime
    if date_unixtime_val is not None and not isinstance(date_unixtime_val, bool):
        try:
            if isinstance(date_unixtime_val, int):
                ts = date_unixtime_val
            elif isinstance(date_unixtime_val, str) and re.fullmatch(
                r"^-?\d+$", date_unixtime_val.strip()
            ):
                ts = int(date_unixtime_val.strip())
            else:
                ts = None
            if ts is not None and -62135596800 <= ts <= 253402300799:
                dt = datetime.fromtimestamp(ts, tz=UTC)
                return dt.isoformat(), raw_date
        except (ValueError, OSError, OverflowError):
            pass

    # 2. Offset-aware raw date string or import_timezone
    if raw_date:
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone(UTC).isoformat(), raw_date
            if import_timezone:
                tz = parse_tz(import_timezone)
                if tz is not None:
                    dt_aware = dt.replace(tzinfo=tz)
                    return dt_aware.astimezone(UTC).isoformat(), raw_date
            return None, raw_date
        except (ValueError, TypeError):
            return None, raw_date

    return None, raw_date


class TelegramImporter:
    def __init__(self, db: Database) -> None:
        self.db = db

    def import_chat_json(
        self,
        raw_bytes: bytes,
        name_override: str | None = None,
        import_timezone: str | None = None,
    ) -> ImportResponse:
        # 1. Parse JSON
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ImporterError("malformed_json", f"Invalid JSON payload: {e!s}") from e

        # 2. Validate Root Shape
        if not isinstance(data, dict):
            raise ImporterError("invalid_export_format", "Root JSON must be an object.")

        if "chats" in data:
            raise ImporterError(
                "account_export_rejected",
                "Account-wide export detected. Please export a single chat from Telegram Desktop.",
            )

        messages_raw = data.get("messages")
        if not isinstance(messages_raw, list):
            raise ImporterError(
                "invalid_export_format",
                "Export JSON must contain a 'messages' list.",
            )

        # 3. Validate Top-Level Metadata Shapes
        if name_override is not None:
            if isinstance(name_override, bool) or not isinstance(name_override, str):
                raise ImporterError(
                    "invalid_chat_name",
                    f"Chat name override must be a string, got {type(name_override).__name__}.",
                )
            if not name_override.strip():
                raise ImporterError(
                    "invalid_chat_name",
                    "Chat name override cannot be empty or whitespace-only.",
                )

        raw_name = data.get("name")
        if raw_name is not None and (isinstance(raw_name, bool) or not isinstance(raw_name, str)):
            raise ImporterError(
                "invalid_chat_name",
                f"Top-level chat name must be a string, got {type(raw_name).__name__}.",
            )

        chat_name: str
        if name_override is not None:
            chat_name = name_override.strip()
        elif raw_name is not None and raw_name.strip():
            chat_name = raw_name
        else:
            chat_name = "Imported Telegram Chat"

        source_chat_id: str | None = None
        raw_source_id = data.get("id")
        if raw_source_id is not None:
            parsed_source_id = parse_source_id(
                raw_source_id,
                field_name="source chat ID",
                error_code="invalid_source_chat_id",
            )
            source_chat_id = str(parsed_source_id)

        # 4. Compute Fingerprint for Idempotency
        fingerprint = hashlib.sha256(raw_bytes).hexdigest()

        with self.db.connection() as conn:
            existing = conn.execute(
                "SELECT * FROM chats WHERE import_fingerprint = ?;",
                (fingerprint,),
            ).fetchone()
            if existing:
                stats_dict = json.loads(existing["import_stats_json"])
                report = ImportReport(**stats_dict)
                total_msgs = conn.execute(
                    "SELECT count(*) FROM messages WHERE chat_id = ?;",
                    (existing["id"],),
                ).fetchone()[0]
                supp_msgs = conn.execute(
                    """
                    SELECT count(*) FROM messages
                    WHERE chat_id = ? AND is_supported_text = 1;
                    """,
                    (existing["id"],),
                ).fetchone()[0]
                summary = ChatSummary(
                    id=existing["id"],
                    name=existing["name"],
                    source_chat_id=existing["source_chat_id"],
                    corpus_version=existing["corpus_version"],
                    import_fingerprint=existing["import_fingerprint"],
                    created_at=existing["created_at"],
                    import_stats=report,
                    total_messages=total_msgs,
                    supported_text_messages=supp_msgs,
                    analyzed_messages=0,
                )
                return ImportResponse(chat=summary, report=report)

        # 5. Process and normalize messages
        seen_source_ids: set[int] = set()
        normalized_messages: list[dict[str, Any]] = []

        total_records = len(messages_raw)
        supported_text = 0
        service_records = 0
        empty_messages = 0
        media_only = 0
        unsupported_media = 0
        stickers_count = 0
        earliest_dt: str | None = None
        latest_dt: str | None = None

        for idx, raw_msg in enumerate(messages_raw):
            if not isinstance(raw_msg, dict):
                raise ImporterError(
                    "malformed_record",
                    f"Record at index {idx} must be an object, got {type(raw_msg).__name__}.",
                )

            source_id = raw_msg.get("id")
            if source_id is None:
                raise ImporterError(
                    "invalid_message_id", f"Record at index {idx} has no message ID."
                )
            source_id_int = parse_source_id(source_id, field_name="message ID", idx=idx)

            if source_id_int in seen_source_ids:
                raise ImporterError(
                    "duplicate_source_message_ids",
                    f"Duplicate source message ID {source_id_int} found in export file.",
                )
            seen_source_ids.add(source_id_int)

            # Extract fields
            msg_type = raw_msg.get("type", "message")
            raw_text = extract_raw_text(raw_msg.get("text"), idx=idx)
            sent_at, raw_date = parse_message_date(
                raw_msg.get("date"),
                raw_msg.get("date_unixtime"),
                import_timezone=import_timezone,
            )

            # Compare valid normalized instants only
            if sent_at is not None:
                if earliest_dt is None or sent_at < earliest_dt:
                    earliest_dt = sent_at
                if latest_dt is None or sent_at > latest_dt:
                    latest_dt = sent_at

            media_type = raw_msg.get("media_type")
            has_media = bool(
                media_type
                or raw_msg.get("file")
                or raw_msg.get("photo")
                or raw_msg.get("sticker_emoji")
            )
            is_sticker = bool(media_type == "sticker" or raw_msg.get("sticker_emoji"))

            if has_media:
                unsupported_media += 1
            if is_sticker:
                stickers_count += 1

            has_text = is_supported_text_content(raw_text)

            # Determine kind and supported status consistently
            if msg_type == "service" or "action" in raw_msg:
                kind = "service"
                service_records += 1
                is_supported = False
            elif is_sticker:
                kind = "sticker"
                if has_text:
                    supported_text += 1
                    is_supported = True
                else:
                    media_only += 1
                    is_supported = False
            elif has_media:
                if has_text:
                    kind = "message"  # Media with caption text is supported text
                    supported_text += 1
                    is_supported = True
                else:
                    kind = "media"
                    media_only += 1
                    is_supported = False
            else:
                if has_text:
                    kind = "message"
                    supported_text += 1
                    is_supported = True
                else:
                    kind = "empty"
                    empty_messages += 1
                    is_supported = False

            sender_name = parse_string_field(
                raw_msg.get("from") or raw_msg.get("actor"), "sender name", idx
            )
            sender_id = parse_string_field(
                raw_msg.get("from_id") or raw_msg.get("actor_id"), "sender ID", idx
            )

            reply_to = raw_msg.get("reply_to_message_id")
            reply_to_int: int | None = None
            if reply_to is not None:
                reply_to_int = parse_source_id(
                    reply_to,
                    field_name="reply_to_message_id",
                    idx=idx,
                    error_code="invalid_reply_id",
                )

            sticker_metadata: dict[str, Any] | None = None
            if is_sticker:
                sticker_metadata = {
                    "sticker_emoji": raw_msg.get("sticker_emoji"),
                    "file": raw_msg.get("file"),
                    "thumbnail": raw_msg.get("thumbnail"),
                    "width": raw_msg.get("width"),
                    "height": raw_msg.get("height"),
                }

            normalized_messages.append(
                {
                    "id": str(uuid.uuid4()),
                    "source_message_id": source_id_int,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "sent_at": sent_at,
                    "raw_date": raw_date,
                    "kind": kind,
                    "text": raw_text,
                    "reply_to_source_id": reply_to_int,
                    "sticker_metadata_json": json.dumps(sticker_metadata, ensure_ascii=False)
                    if sticker_metadata
                    else None,
                    "source_order": idx,
                    "is_supported_text": 1 if is_supported else 0,
                }
            )

        report = ImportReport(
            total_records=total_records,
            supported_text_messages=supported_text,
            service_records=service_records,
            empty_messages=empty_messages,
            media_only_messages=media_only,
            unsupported_media_count=unsupported_media,
            stickers_count=stickers_count,
            duplicate_records=0,
            earliest_sent_at=earliest_dt,
            latest_sent_at=latest_dt,
        )

        chat_id = str(uuid.uuid4())
        created_at_now = datetime.now(UTC).isoformat()
        stats_json = json.dumps(report.model_dump(), ensure_ascii=False)

        # 6. Atomic database insertion
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO chats (
                    id, name, source_chat_id, corpus_version,
                    import_fingerprint, created_at, import_stats_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    chat_id,
                    chat_name,
                    source_chat_id,
                    1,
                    fingerprint,
                    created_at_now,
                    stats_json,
                ),
            )

            conn.executemany(
                """
                INSERT INTO messages (
                    id, chat_id, source_message_id, sender_id, sender_name,
                    sent_at, raw_date, kind, text, reply_to_source_id,
                    sticker_metadata_json, source_order, is_supported_text
                ) VALUES (
                    :id, :chat_id, :source_message_id, :sender_id, :sender_name,
                    :sent_at, :raw_date, :kind, :text, :reply_to_source_id,
                    :sticker_metadata_json, :source_order, :is_supported_text
                );
                """,
                [{**m, "chat_id": chat_id} for m in normalized_messages],
            )

        summary = ChatSummary(
            id=chat_id,
            name=chat_name,
            source_chat_id=source_chat_id,
            corpus_version=1,
            import_fingerprint=fingerprint,
            created_at=created_at_now,
            import_stats=report,
            total_messages=len(normalized_messages),
            supported_text_messages=supported_text,
            analyzed_messages=0,
        )

        return ImportResponse(chat=summary, report=report)
