"""
Password hashing (bcrypt via passlib) and JWT access/refresh token
issuance & verification.

Token design:
- Access token: short-lived (15 min default), JWT, carries `sub` (user id)
  and `type: access`. Sent as `Authorization: Bearer <token>` on every API
  call. Never stored server-side -- verified purely by signature + expiry.
- Refresh token: long-lived (30 days default), a random opaque string (NOT
  a JWT) whose SHA-256 hash is stored in the refresh_tokens table. The raw
  token is only ever seen by the client; the server can always look it up
  by hash, check revoked_at/expires_at, and rotate it. This is deliberately
  NOT a JWT: a JWT refresh token can't be revoked before its own expiry
  without a server-side blocklist anyway, so a DB-backed opaque token gets
  the same guarantee with less machinery.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
import uuid

import jwt
from passlib.context import CryptContext

from app.config import get_settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return _pwd_context.verify(plain_password, password_hash)


def create_access_token(user_id: uuid.UUID) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> uuid.UUID:
    """Raises jwt.PyJWTError (expired/invalid signature/malformed) on failure --
    callers turn that into a 401, they don't need to inspect the error."""
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return uuid.UUID(payload["sub"])


def generate_refresh_token() -> tuple[str, str]:
    """Returns (raw_token_to_send_to_client, sha256_hash_to_store_in_db)."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class RunTokenClaims:
    """Decoded contents of a run-provenance token -- see create_run_token."""

    __slots__ = ("run_id", "user_id", "conversation_id", "uploaded_file_id")

    def __init__(
        self,
        run_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        uploaded_file_id: uuid.UUID | None,
    ) -> None:
        self.run_id = run_id
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.uploaded_file_id = uploaded_file_id


def create_run_token(
    *,
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    uploaded_file_id: uuid.UUID | None,
) -> str:
    """Signs a short-lived, unforgeable "who/what is this run" claim -- issued
    once by the control plane when a run is enqueued (see app/api/v1/runs.py),
    carried untouched through the sandbox worker, and presented back on every
    broker call (file download, LLM proxy) and in the final result message.

    This is what makes a run's identity server-asserted rather than
    something the sandbox could claim for itself: the sandbox worker never
    holds `jwt_secret_key`, so it can read this token but cannot mint a new
    one or alter an existing one's claims -- any tampering breaks the
    signature and decode_run_token rejects it.

    Deliberately a distinct `type` from an access token: decode_access_token
    rejects this outright (wrong type, see its check below), and
    decode_run_token rejects a real user access token the same way -- a run
    token leaking out of the sandbox can't be replayed as a user session,
    and a stolen user access token can't be used to impersonate a run.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "type": "run_provenance",
        "run_id": str(run_id),
        "user_id": str(user_id),
        "conversation_id": str(conversation_id),
        "uploaded_file_id": str(uploaded_file_id) if uploaded_file_id else None,
        "iat": now,
        "exp": now + timedelta(minutes=settings.run_token_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_run_token(token: str) -> RunTokenClaims:
    """Raises jwt.PyJWTError (expired/invalid signature/wrong type/malformed)
    on failure -- callers turn that into a 401, same convention as
    decode_access_token."""
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != "run_provenance":
        raise jwt.InvalidTokenError("not a run provenance token")
    return RunTokenClaims(
        run_id=uuid.UUID(payload["run_id"]),
        user_id=uuid.UUID(payload["user_id"]),
        conversation_id=uuid.UUID(payload["conversation_id"]),
        uploaded_file_id=(
            uuid.UUID(payload["uploaded_file_id"]) if payload.get("uploaded_file_id") else None
        ),
    )


def generate_email_verification_token() -> tuple[str, str]:
    """Same (raw, sha256-hash) shape as generate_refresh_token -- a separate
    function only so call sites read clearly (an email verification token
    and a refresh token are not interchangeable even though the underlying
    primitive is identical); hash_refresh_token is reused as-is for hashing
    since it's just a generic sha256-hex helper despite its name."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw)
