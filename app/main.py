"""FastAPI application entrypoint, lifecycle, and API endpoints."""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.analysis import AnalysisService
from app.config import settings
from app.db import Database, get_db
from app.importer import ImporterError, TelegramImporter
from app.models import (
    AnalysisEstimateResponse,
    AnalysisJobResponse,
    AnalysisStartRequest,
    ChatDetail,
    ChatSummary,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    ImportReport,
    ImportResponse,
    MemoryItem,
    PaginatedResponse,
    ProfileApplySuggestionRequest,
    ProfileResponse,
    ProfileUpdateRequest,
    ReplyRequest,
    ReplyResponse,
    TelegramBindingItem,
    TelegramBindingUpdateRequest,
    TelegramStatusResponse,
)
from app.providers.base import BaseAIProvider, ProviderError
from app.providers.gemini import GeminiProvider
from app.replies import ReplyService
from app.retrieval import MemoryRetriever

_shared_ai_provider: GeminiProvider | None = None
_background_tasks: set[asyncio.Task] = set()


def get_ai_provider() -> BaseAIProvider | None:
    global _shared_ai_provider
    if not settings.is_provider_configured:
        return None
    if _shared_ai_provider is None:
        _shared_ai_provider = GeminiProvider()
    return _shared_ai_provider


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _shared_ai_provider
    # Ensure database migrations are executed at startup
    db = get_db()
    db.run_migrations()
    analysis_service = AnalysisService(db)
    analysis_service.recover_stale_jobs()
    yield
    # Cleanup background tasks and provider resources on shutdown
    for t in list(_background_tasks):
        t.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    _background_tasks.clear()
    if _shared_ai_provider is not None:
        await _shared_ai_provider.close()
        _shared_ai_provider = None


app = FastAPI(
    title="Vkyd API",
    version="0.1.0",
    description="Attributable quote memory and humor profile engine",
    lifespan=lifespan,
)

static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    index_file = static_dir / "index.html"
    if not index_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Frontend index.html not found.",
        )
    return FileResponse(index_file)


# Standardized error handlers according to docs/contracts.md
@app.exception_handler(ProviderError)
async def handle_provider_error(request: Request, exc: ProviderError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(ImporterError)
async def handle_importer_error(request: Request, exc: ImporterError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    code = "not_found" if exc.status_code == 404 else "http_error"
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        return JSONResponse(status_code=exc.status_code, content={"error": detail})
    if isinstance(detail, str) and "_" in detail:
        code = detail
        message = detail.replace("_", " ").capitalize()
    else:
        message = str(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": message}},
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": {"code": "validation_error", "message": "Invalid request parameters."}},
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {"code": "internal_server_error", "message": "An unexpected error occurred."}
        },
    )


def is_allowed_origin(origin: str | None) -> bool:
    """
    Validate Origin header against configured local service.
    Allowed:
    - Non-browser requests (no Origin header present).
    - Browser requests where scheme is http, hostname is strictly in
      {'127.0.0.1', 'localhost', settings.app_host.lower()}, no userinfo,
      no path/query/fragment, and effective port matches settings.app_port.
    Rejects lookalikes (e.g. localhost.attacker.com), null, empty, unexpected ports/schemes.
    """
    if origin is None:
        return True
    if origin == "null" or not origin.strip():
        return False
    try:
        parsed = urlsplit(origin)
        if parsed.scheme != "http":
            return False
        if parsed.username or parsed.password:
            return False
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return False

        allowed_hosts = {"127.0.0.1", "localhost"}
        if settings.app_host:
            allowed_hosts.add(settings.app_host.lower())

        hostname = (parsed.hostname or "").lower()
        if hostname not in allowed_hosts:
            return False

        effective_port = parsed.port if parsed.port is not None else 80
        return effective_port == settings.app_port
    except Exception:
        return False


# Mutating route origin boundary check
@app.middleware("http")
async def verify_local_origin(request: Request, call_next: Any) -> Any:
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin")
        if not is_allowed_origin(origin):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": {
                        "code": "forbidden_origin",
                        "message": "Cross-origin requests are rejected.",
                    }
                },
            )
    return await call_next(request)


# Health endpoint
@app.get("/api/health", response_model=HealthResponse)
def get_health(db: Database = Depends(get_db)) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version="0.1.0",
        database_ready=db.check_readiness(),
        provider_configured=settings.is_provider_configured,
        telegram_configured=settings.is_telegram_configured,
    )


# List chats
@app.get("/api/chats", response_model=PaginatedResponse[ChatSummary])
def list_chats(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Database = Depends(get_db),
) -> PaginatedResponse[ChatSummary]:
    with db.connection() as conn:
        total = conn.execute("SELECT count(*) FROM chats;").fetchone()[0]
        rows = conn.execute(
            """
            SELECT id, name, source_chat_id, corpus_version,
                   import_fingerprint, created_at, import_stats_json
            FROM chats
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?;
            """,
            (limit, offset),
        ).fetchall()

        items: list[ChatSummary] = []
        for r in rows:
            stats_dict = json.loads(r["import_stats_json"])
            report = ImportReport(**stats_dict)

            total_msgs = conn.execute(
                "SELECT count(*) FROM messages WHERE chat_id = ?;",
                (r["id"],),
            ).fetchone()[0]

            supp_msgs = conn.execute(
                """
                SELECT count(*) FROM messages
                WHERE chat_id = ? AND is_supported_text = 1;
                """,
                (r["id"],),
            ).fetchone()[0]

            job_row = conn.execute(
                """
                SELECT processed_unique_messages FROM analysis_jobs
                WHERE chat_id = ? AND corpus_version = ?
                ORDER BY created_at DESC LIMIT 1;
                """,
                (r["id"], r["corpus_version"]),
            ).fetchone()
            analyzed_unique = job_row["processed_unique_messages"] if job_row else 0

            items.append(
                ChatSummary(
                    id=r["id"],
                    name=r["name"],
                    source_chat_id=r["source_chat_id"],
                    corpus_version=r["corpus_version"],
                    import_fingerprint=r["import_fingerprint"],
                    created_at=r["created_at"],
                    import_stats=report,
                    total_messages=total_msgs,
                    supported_text_messages=supp_msgs,
                    analyzed_messages=analyzed_unique,
                )
            )

        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )


async def read_bounded_upload(file: UploadFile, max_bytes: int) -> bytes:
    """
    Read upload stream up to max_bytes + 1. If upload exceeds max_bytes,
    immediately stop reading, close file, and raise 413.
    Note: This bounds application memory consumption. It does not bound
    prior multipart-body parsing/spooling performed by Starlette/uvicorn
    before the endpoint handler is entered.
    """
    buffer = bytearray()
    chunk_size = 64 * 1024
    while len(buffer) <= max_bytes:
        to_read = min(chunk_size, max_bytes + 1 - len(buffer))
        chunk = await file.read(to_read)
        if not chunk:
            break
        buffer.extend(chunk)

    if len(buffer) > max_bytes:
        await file.close()
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "code": "payload_too_large",
                "message": f"Uploaded file exceeds maximum allowed size of {max_bytes} bytes.",
            },
        )
    return bytes(buffer)


# Import single-chat Telegram JSON
@app.post("/api/chats/import", response_model=ImportResponse)
async def import_chat(
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    timezone: str | None = Form(default=None),
    db: Database = Depends(get_db),
) -> ImportResponse:
    # 1. Read bounded content and close upload stream
    try:
        content = await read_bounded_upload(file, settings.max_import_size_bytes)
    finally:
        await file.close()

    # 2. Execute heavy synchronous import off the event loop
    importer = TelegramImporter(db)
    result = await run_in_threadpool(
        importer.import_chat_json, content, name_override=name, import_timezone=timezone
    )
    return result


# Get single chat detail
@app.get("/api/chats/{chat_id}", response_model=ChatDetail)
def get_chat(
    chat_id: str,
    db: Database = Depends(get_db),
) -> ChatDetail:
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT id, name, source_chat_id, corpus_version,
                   import_fingerprint, created_at, import_stats_json
            FROM chats
            WHERE id = ?;
            """,
            (chat_id,),
        ).fetchone()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "chat_not_found",
                    "message": f"Chat with ID {chat_id} was not found.",
                },
            )

        stats_dict = json.loads(row["import_stats_json"])
        report = ImportReport(**stats_dict)

        total_msgs = conn.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ?;",
            (chat_id,),
        ).fetchone()[0]

        supp_msgs = conn.execute(
            """
            SELECT count(*) FROM messages
            WHERE chat_id = ? AND is_supported_text = 1;
            """,
            (chat_id,),
        ).fetchone()[0]

        job_row = conn.execute(
            """
            SELECT id, processed_unique_messages FROM analysis_jobs
            WHERE chat_id = ? AND corpus_version = ?
            ORDER BY created_at DESC LIMIT 1;
            """,
            (chat_id, row["corpus_version"]),
        ).fetchone()
        analyzed_unique = job_row["processed_unique_messages"] if job_row else 0
        ai_coverage = round((analyzed_unique / supp_msgs) * 100.0, 2) if supp_msgs > 0 else 0.0

        latest_job = None
        if job_row:
            analysis_svc = AnalysisService(db)
            latest_job = analysis_svc.get_job(chat_id, job_row["id"])

        coverage = {
            "import_coverage_percent": round((total_msgs / report.total_records) * 100.0, 2)
            if report.total_records > 0
            else 0.0,
            "ai_analysis_coverage_percent": ai_coverage,
            "analyzed_message_count": analyzed_unique,
            "total_supported_text": supp_msgs,
        }

        return ChatDetail(
            id=row["id"],
            name=row["name"],
            source_chat_id=row["source_chat_id"],
            corpus_version=row["corpus_version"],
            import_fingerprint=row["import_fingerprint"],
            created_at=row["created_at"],
            import_stats=report,
            total_messages=total_msgs,
            supported_text_messages=supp_msgs,
            analyzed_messages=analyzed_unique,
            coverage=coverage,
            latest_job=latest_job,
        )


# Delete chat and all local memories
@app.delete("/api/chats/{chat_id}")
def delete_chat(
    chat_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    with db.transaction() as conn:
        row = conn.execute("SELECT id FROM chats WHERE id = ?;", (chat_id,)).fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "chat_not_found",
                    "message": f"Chat with ID {chat_id} was not found.",
                },
            )

        # Explicit FTS cleanup and foreign key cascade
        conn.execute("DELETE FROM messages_fts WHERE chat_id = ?;", (chat_id,))
        conn.execute("DELETE FROM chats WHERE id = ?;", (chat_id,))

    return {"deleted": True, "id": chat_id}


# Search memories in chat
@app.get("/api/chats/{chat_id}/memories", response_model=PaginatedResponse[MemoryItem])
def search_memories(
    chat_id: str,
    q: str = Query(default="", max_length=500, description="Search query or quote snippet"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Database = Depends(get_db),
) -> PaginatedResponse[MemoryItem]:
    retriever = MemoryRetriever(db)
    try:
        return retriever.search_memories(chat_id=chat_id, query=q, limit=limit, offset=offset)
    except ValueError as e:
        if str(e) == "Chat not found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "chat_not_found",
                    "message": f"Chat with ID {chat_id} was not found.",
                },
            ) from e
        raise


# --- Slice 02: Analysis, Profile, Reply, and Feedback Endpoints ---


@app.post("/api/chats/{chat_id}/analysis/estimate", response_model=AnalysisEstimateResponse)
def estimate_analysis(
    chat_id: str,
    db: Database = Depends(get_db),
) -> AnalysisEstimateResponse:
    service = AnalysisService(db)
    try:
        return service.estimate_chunks(chat_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "chat_not_found", "message": str(e)},
        ) from e


@app.post("/api/chats/{chat_id}/analysis", response_model=AnalysisJobResponse)
async def start_or_resume_analysis(
    chat_id: str,
    body: AnalysisStartRequest | None = None,
    wait: bool = Query(default=False, description="Wait for analysis execution synchronously"),
    db: Database = Depends(get_db),
    provider: BaseAIProvider | None = Depends(get_ai_provider),
) -> AnalysisJobResponse:
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "credential_missing",
                "message": "AI provider is not configured. Configure GEMINI_API_KEY.",
            },
        )

    service = AnalysisService(db)
    try:
        max_calls = body.max_calls if body else None
        chunk_size = body.chunk_size if body else None
        overlap = body.context_overlap if body else None
        job = service.create_or_resume_job(
            chat_id,
            max_calls=max_calls,
            chunk_size=chunk_size,
            overlap=overlap,
        )
    except ValueError as e:
        err_msg = str(e)
        if "not found" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "chat_not_found", "message": err_msg},
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "budget_limit_reached", "message": err_msg},
        ) from e

    if job.status == "completed":
        return job

    if job.status == "running":
        # Do not launch duplicate worker for already-running job
        return job

    if wait:
        return await service.execute_job(chat_id, job.id, provider)

    # Launch tracked background analysis execution
    task = asyncio.create_task(service.execute_job(chat_id, job.id, provider))
    _background_tasks.add(task)

    def _cleanup_task(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            with contextlib.suppress(Exception):
                t.exception()

    task.add_done_callback(_cleanup_task)
    return job


@app.get("/api/chats/{chat_id}/analysis/{job_id}", response_model=AnalysisJobResponse)
def get_analysis_progress(
    chat_id: str,
    job_id: str,
    db: Database = Depends(get_db),
) -> AnalysisJobResponse:
    service = AnalysisService(db)
    try:
        return service.get_job(chat_id, job_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "job_not_found", "message": str(e)},
        ) from e


@app.post("/api/chats/{chat_id}/analysis/{job_id}/cancel", response_model=AnalysisJobResponse)
def cancel_analysis(
    chat_id: str,
    job_id: str,
    db: Database = Depends(get_db),
) -> AnalysisJobResponse:
    service = AnalysisService(db)
    try:
        return service.cancel_job(chat_id, job_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "job_not_found", "message": str(e)},
        ) from e


@app.get("/api/chats/{chat_id}/profile", response_model=ProfileResponse)
def get_profile(
    chat_id: str,
    db: Database = Depends(get_db),
) -> ProfileResponse:
    service = AnalysisService(db)
    profile = service.get_latest_profile(chat_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "profile_not_found",
                "message": f"No humor profile generated yet for chat '{chat_id}'.",
            },
        )
    return profile


@app.put("/api/chats/{chat_id}/profile", response_model=ProfileResponse)
def update_profile(
    chat_id: str,
    body: ProfileUpdateRequest,
    db: Database = Depends(get_db),
) -> ProfileResponse:
    service = AnalysisService(db)
    try:
        return service.update_profile(
            chat_id=chat_id,
            expected_version=body.expected_version,
            instructions_md=body.instructions_md,
            structured=body.structured,
            memes_update=body.memes,
        )
    except ValueError as e:
        err_msg = str(e)
        if "Version conflict" in err_msg:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "version_conflict", "message": err_msg},
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_update", "message": err_msg},
        ) from e


@app.get("/api/chats/{chat_id}/profile/export")
def export_profile(
    chat_id: str,
    db: Database = Depends(get_db),
) -> Response:
    service = AnalysisService(db)
    try:
        md_content = service.export_profile_markdown(chat_id)
        return Response(
            content=md_content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="profile_{chat_id}.md"'},
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "profile_not_found", "message": str(e)},
        ) from e


@app.post("/api/chats/{chat_id}/reply", response_model=ReplyResponse)
async def generate_reply(
    chat_id: str,
    body: ReplyRequest,
    db: Database = Depends(get_db),
    provider: BaseAIProvider | None = Depends(get_ai_provider),
) -> ReplyResponse:
    retriever = MemoryRetriever(db)
    analysis_service = AnalysisService(db)
    reply_service = ReplyService(db, retriever, analysis_service)
    try:
        return await reply_service.generate_reply(chat_id=chat_id, request=body, provider=provider)
    except ValueError as e:
        err_msg = str(e)
        if "not found" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "chat_not_found", "message": err_msg},
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_request", "message": err_msg},
        ) from e


@app.post("/api/chats/{chat_id}/replies/{reply_id}/feedback", response_model=FeedbackResponse)
def record_feedback(
    chat_id: str,
    reply_id: str,
    body: FeedbackRequest,
    db: Database = Depends(get_db),
) -> FeedbackResponse:
    retriever = MemoryRetriever(db)
    analysis_service = AnalysisService(db)
    reply_service = ReplyService(db, retriever, analysis_service)
    try:
        return reply_service.record_feedback(
            chat_id=chat_id,
            reply_id=reply_id,
            verdict=body.verdict,
        )
    except ValueError as e:
        err_msg = str(e)
        if "not found" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "reply_not_found", "message": err_msg},
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_feedback", "message": err_msg},
        ) from e


# --- Profile Suggestion Endpoints ---


@app.get("/api/chats/{chat_id}/profile/suggestion", response_model=ProfileResponse)
def get_profile_suggestion(
    chat_id: str,
    db: Database = Depends(get_db),
) -> ProfileResponse:
    service = AnalysisService(db)
    sugg = service.get_suggested_profile(chat_id)
    if not sugg:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "suggestion_not_found",
                "message": f"No pending AI suggestion found for chat '{chat_id}'.",
            },
        )
    return sugg


@app.post("/api/chats/{chat_id}/profile/apply-suggestion", response_model=ProfileResponse)
def apply_profile_suggestion(
    chat_id: str,
    body: ProfileApplySuggestionRequest,
    db: Database = Depends(get_db),
) -> ProfileResponse:
    service = AnalysisService(db)
    try:
        return service.apply_suggested_profile(
            chat_id=chat_id,
            expected_active_version=body.expected_active_version,
            suggestion_id=body.suggestion_id,
        )
    except ValueError as e:
        err_msg = str(e)
        if "Version conflict" in err_msg:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "version_conflict", "message": err_msg},
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_request", "message": err_msg},
        ) from e


# --- Telegram Status and Binding Endpoints ---


@app.get("/api/telegram/status", response_model=TelegramStatusResponse)
def get_telegram_status(db: Database = Depends(get_db)) -> TelegramStatusResponse:
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT b.telegram_chat_id, b.chat_id, c.name as chat_name,
                   b.enabled, b.cooldown_seconds, b.last_sent_at
            FROM telegram_bindings b
            LEFT JOIN chats c ON b.chat_id = c.id
            ORDER BY b.telegram_chat_id ASC;
            """
        ).fetchall()
        bindings = [
            TelegramBindingItem(
                telegram_chat_id=r["telegram_chat_id"],
                chat_id=r["chat_id"],
                chat_name=r["chat_name"],
                enabled=bool(r["enabled"]),
                cooldown_seconds=r["cooldown_seconds"],
                last_sent_at=r["last_sent_at"],
            )
            for r in rows
        ]

    return TelegramStatusResponse(
        configured=settings.is_telegram_configured,
        bot_username=None,  # Offline/safe reading without hitting external network
        allowed_chat_ids=sorted(settings.parsed_telegram_allowed_chat_ids),
        bindings=bindings,
    )


@app.put("/api/telegram/bindings/{telegram_chat_id}", response_model=TelegramBindingItem)
def update_telegram_binding(
    telegram_chat_id: str,
    body: TelegramBindingUpdateRequest,
    db: Database = Depends(get_db),
) -> TelegramBindingItem:
    with db.transaction() as conn:
        chat = conn.execute("SELECT id, name FROM chats WHERE id = ?;", (body.chat_id,)).fetchone()
        if not chat:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "chat_not_found", "message": f"Chat '{body.chat_id}' not found."},
            )

        conn.execute(
            """
            INSERT INTO telegram_bindings (
                telegram_chat_id, chat_id, enabled, cooldown_seconds, last_sent_at
            ) VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(telegram_chat_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                enabled = excluded.enabled,
                cooldown_seconds = excluded.cooldown_seconds;
            """,
            (telegram_chat_id, body.chat_id, 1 if body.enabled else 0, body.cooldown_seconds),
        )

        row = conn.execute(
            """
            SELECT b.telegram_chat_id, b.chat_id, c.name as chat_name,
                   b.enabled, b.cooldown_seconds, b.last_sent_at
            FROM telegram_bindings b
            JOIN chats c ON b.chat_id = c.id
            WHERE b.telegram_chat_id = ?;
            """,
            (telegram_chat_id,),
        ).fetchone()

        return TelegramBindingItem(
            telegram_chat_id=row["telegram_chat_id"],
            chat_id=row["chat_id"],
            chat_name=row["chat_name"],
            enabled=bool(row["enabled"]),
            cooldown_seconds=row["cooldown_seconds"],
            last_sent_at=row["last_sent_at"],
        )


@app.delete("/api/telegram/bindings/{telegram_chat_id}")
def delete_telegram_binding(
    telegram_chat_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT telegram_chat_id FROM telegram_bindings WHERE telegram_chat_id = ?;",
            (telegram_chat_id,),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "binding_not_found", "message": "Binding not found."},
            )
        conn.execute(
            "DELETE FROM telegram_bindings WHERE telegram_chat_id = ?;", (telegram_chat_id,)
        )
    return {"deleted": True, "telegram_chat_id": telegram_chat_id}
