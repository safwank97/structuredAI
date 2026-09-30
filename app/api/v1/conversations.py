"""
Conversations, messages, and file uploads -- the first authenticated
endpoints in the Control Plane API. Every route here depends on
`get_current_user`, which opens an RLS-scoped session for the caller (see
app/core/deps.py) -- a query here can only ever see that user's own rows,
enforced by Postgres itself, not by an extra WHERE clause this code has to
remember to add.

Agent execution (the sandbox Container Apps Job, Service Bus queueing, SSE
streaming of an assistant's response) is a separate subsystem and is not
built yet -- sending a message here persists it (role="user") and returns
it, full stop. There is no assistant reply. That's a deliberate scope line,
not an oversight: this pass exists to prove conversations/messages/uploads
work end-to-end against real Postgres rows under RLS, the same way
app/api/v1/auth.py proved the auth lifecycle -- wiring an actual model or
agent run in is the next, much larger, piece of work.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.deps import get_current_user
from app.core.storage import save_upload
from app.core.upload_validation import (
    UNPARSEABLE_EXTENSIONS,
    UNPARSEABLE_NOTE,
    UploadRejected,
    extension_of,
    validate_upload,
)
from app.db.models.conversation import Conversation, Message
from app.db.models.file import UploadedFile
from app.db.models.user import User
from app.schemas.chat import (
    ConversationCreate,
    ConversationOut,
    ConversationRename,
    MessageCreate,
    MessageOut,
    UploadedFileOut,
)

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


def _format_file_size(num_bytes: int) -> str:
    """Human-readable size for the upload note's message text (e.g. "42.3
    MB") -- the UI parses that exact "Uploaded: <name> (<size>)" shape to
    render an attachment card instead of a plain text bubble, so this stays
    a plain string the message content owns rather than a separate API
    field; keeping it human-formatted here (not raw bytes) is what makes
    that renderable without the client re-deriving units itself."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover -- unreachable, loop's GB branch already returns


async def _get_owned_conversation(
    conversation_id: uuid.UUID, session: AsyncSession
) -> Conversation:
    """RLS already guarantees this SELECT can only ever return a row the
    caller owns -- a conversation_id belonging to another user comes back
    as `None` here exactly the same as an id that doesn't exist at all, so
    both cases collapse to the same 404. That's intentional: the response
    given to "someone else's conversation" and "no such conversation"
    should be indistinguishable, for the same enumeration reasons
    login already avoids revealing whether an email is registered.
    """
    result = await session.execute(select(Conversation).where(Conversation.id == conversation_id))
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    user_and_session: tuple[User, AsyncSession] = Depends(get_current_user),
) -> ConversationOut:
    user, session = user_and_session
    conversation = Conversation(user_id=user.id, title=payload.title)
    session.add(conversation)
    await session.flush()
    return ConversationOut.model_validate(conversation)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    user_and_session: tuple[User, AsyncSession] = Depends(get_current_user),
) -> list[ConversationOut]:
    user, session = user_and_session
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
    )
    return [ConversationOut.model_validate(c) for c in result.scalars().all()]


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationRename,
    user_and_session: tuple[User, AsyncSession] = Depends(get_current_user),
) -> ConversationOut:
    _, session = user_and_session
    conversation = await _get_owned_conversation(conversation_id, session)
    # Blank (after trimming) clears it back to "Untitled project" in the UI,
    # same as leaving a brand-new conversation's title unset -- rather than
    # storing an empty string as a distinct third state nothing else expects.
    conversation.title = payload.title.strip() or None
    # Deliberately NOT bumping updated_at here -- renaming isn't "activity"
    # the way sending a message or uploading a file is, and the chip list is
    # ordered by updated_at desc; silently reordering someone's project tiles
    # just because they fixed a typo in the title would be a surprising side
    # effect of what should be a purely cosmetic action.
    await session.flush()
    await session.refresh(conversation)
    return ConversationOut.model_validate(conversation)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: uuid.UUID,
    user_and_session: tuple[User, AsyncSession] = Depends(get_current_user),
) -> list[MessageOut]:
    _, session = user_and_session
    await _get_owned_conversation(conversation_id, session)
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    return [MessageOut.model_validate(m) for m in result.scalars().all()]


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_message(
    conversation_id: uuid.UUID,
    payload: MessageCreate,
    user_and_session: tuple[User, AsyncSession] = Depends(get_current_user),
) -> MessageOut:
    user, session = user_and_session
    conversation = await _get_owned_conversation(conversation_id, session)

    message = Message(
        conversation_id=conversation.id, user_id=user.id, role="user", content=payload.content
    )
    session.add(message)
    # Bump updated_at so the conversation list (ordered by it) surfaces the
    # most recently active thread first, the same way any chat product's
    # sidebar sorts. Set from Python, not from message.created_at -- that
    # column is server_default=func.now(), so it doesn't exist on the
    # in-memory object yet at this point in the transaction (only after a
    # flush/refresh round-trips it back from Postgres).
    conversation.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await session.refresh(message)
    return MessageOut.model_validate(message)


@router.post(
    "/{conversation_id}/files",
    response_model=UploadedFileOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    conversation_id: uuid.UUID,
    file: UploadFile = File(...),
    user_and_session: tuple[User, AsyncSession] = Depends(get_current_user),
) -> UploadedFileOut:
    user, session = user_and_session
    conversation = await _get_owned_conversation(conversation_id, session)

    settings = get_settings()
    filename = file.filename or "upload"
    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"File exceeds the {settings.max_upload_bytes // (1024 * 1024)} MiB limit",
        )

    # Extension allowlist + a signature ("magic bytes") check against the
    # actual bytes -- never trusts the filename's extension or the client's
    # Content-Type header on their own, since both are just attacker-
    # controlled strings (see app/core/upload_validation.py's module
    # docstring for why, and for exactly which formats this accepts).
    ext = extension_of(filename)
    try:
        validate_upload(ext, data)
    except UploadRejected as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, exc.reason)

    blob_path = save_upload(user.id, filename, data)
    uploaded = UploadedFile(
        user_id=user.id,
        blob_path=blob_path,
        original_filename=filename,
        content_type=file.content_type or "application/octet-stream",
        size_bytes=len(data),
        # No virus-scan pipeline wired up in this local stack -- marked
        # "clean" outright rather than left at the real default of
        # "pending_scan", which would need a scanner that never runs here
        # to ever move it forward. A real deployment leaves the model
        # default alone and lets the actual scan step transition it.
        status="clean",
    )
    session.add(uploaded)

    # Record the upload in the conversation's timeline as a system message,
    # so the chat UI has something to render inline without a second kind
    # of list to fetch and interleave client-side.
    note = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        role="system",
        content=f"Uploaded: {uploaded.original_filename} ({_format_file_size(uploaded.size_bytes)})",
    )
    session.add(note)

    if ext in UNPARSEABLE_EXTENSIONS:
        # A second, separate system message rather than tacking this onto
        # the upload note above -- keeps the attachment card's own format
        # identical across every accepted file type, with the caveat
        # appearing as its own plain message right underneath it.
        session.add(
            Message(
                conversation_id=conversation.id,
                user_id=user.id,
                role="system",
                content=UNPARSEABLE_NOTE,
            )
        )

    conversation.updated_at = datetime.now(timezone.utc)

    await session.flush()
    await session.refresh(uploaded)
    return UploadedFileOut.model_validate(uploaded)
