"""
FastAPI dependencies for DB access and authentication.

Three ways a handler gets a DB session, matching the RLS design in
migration 0001 (conversations, messages, uploaded_files, agent_runs and
run_usage have RLS enabled with a policy keyed on the Postgres session
variable app.current_user_id; `users` and `refresh_tokens` are deliberately
NOT RLS-restricted -- see the migration for why in each case):

- `get_unscoped_session`: no app.current_user_id set. Used by register,
  login, refresh and logout, which all need to read/write `users` and/or
  `refresh_tokens` before (or without ever needing) a resolved identity.

- `scoped_session(user_id)`: a plain async context manager (not a FastAPI
  dependency) that opens a transaction and immediately sets
  app.current_user_id = user_id. Register and login both call this
  directly once they know which user_id the refresh token they're about to
  issue belongs to -- inserting into refresh_tokens through the *unscoped*
  session would actually be REJECTED by its own RLS policy (Postgres uses
  the USING clause as the WITH CHECK for INSERT when no WITH CHECK is given,
  and current_setting(..., true) is NULL with no context set, so
  `user_id = NULL` is never true) -- this bit us once already while writing
  this module, hence the comment.

- `get_current_user`: the FastAPI dependency every authenticated endpoint
  depends on. Resolves the caller from the JWT, then opens a `scoped_session`
  for that user_id and yields (user, session) for the duration of the
  request. There is no code path in this module that reaches an
  authenticated endpoint's DB session without RLS active.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RunTokenClaims, decode_access_token, decode_run_token
from app.db.base import get_session_factory
from app.db.models.run import AgentRun
from app.db.models.user import User

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


async def get_unscoped_session() -> AsyncIterator[AsyncSession]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        async with session.begin():
            yield session


async def set_rls_context(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Set app.current_user_id for the remainder of `session`'s current
    transaction. SET LOCAL's variable-value position can't be a bind
    parameter, only a literal. Safe to inline here because `user_id` is
    always a `uuid.UUID` object (never raw client input) -- str(uuid.UUID)
    always yields the canonical 36-char hex-with-dashes form, nothing else,
    so there is no injection surface even though this looks like raw string
    formatting.

    Used by `scoped_session` below, and by any future endpoint that needs
    to switch an already-open unscoped session over to a specific user's
    RLS context mid-transaction (register/login/refresh don't need this --
    neither `users` nor `refresh_tokens` carries RLS, see migration 0001).
    """
    await session.execute(text(f"SET LOCAL app.current_user_id = '{user_id}'"))


@asynccontextmanager
async def scoped_session(user_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """A fresh session/transaction, RLS-scoped to `user_id` from the start.
    Used where user_id is already known before any DB work begins (e.g.
    get_current_user, resolved from a JWT) -- unlike register/login, there
    is no earlier same-transaction write this needs to stay ordered after.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        async with session.begin():
            await set_rls_context(session, user_id)
            yield session


async def get_current_user(
    token: str | None = Depends(_oauth2_scheme),
) -> AsyncIterator[tuple[User, AsyncSession]]:
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        user_id = decode_access_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    async with scoped_session(user_id) as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
        yield user, session


async def get_run_claims(
    token: str | None = Depends(_oauth2_scheme),
) -> AsyncIterator[tuple[RunTokenClaims, AsyncSession]]:
    """The sandbox-job equivalent of get_current_user -- but for the sandbox
    worker, not a human. Every internal broker endpoint (app/api/v1/runs.py's
    /internal/runs/... routes) depends on this instead of get_current_user:
    the sandbox worker never holds a user's JWT or password, only the
    one-time run-provenance token the control plane minted when the run was
    enqueued (see create_run_token). This is the O1 credential-isolation
    boundary in code: decode_run_token rejects anything that isn't
    specifically a run-provenance token (including, deliberately, a real
    user access token -- the two are not interchangeable in either
    direction), and the yielded session is RLS-scoped to the run's owning
    user_id, so a broker call can never read or write another tenant's rows
    even if the run_id in the URL were somehow guessed.

    Also verifies the run itself still exists and hasn't been cancelled --
    a worker that raced a Stop request finds out here, at the first broker
    call it makes afterward, rather than being trusted to check on its own.
    """
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        claims = decode_run_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired run token")

    async with scoped_session(claims.user_id) as session:
        result = await session.execute(select(AgentRun).where(AgentRun.id == claims.run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
        if run.status == "cancelled":
            raise HTTPException(status.HTTP_409_CONFLICT, "Run was cancelled")
        yield claims, session
