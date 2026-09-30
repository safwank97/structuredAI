"""
Sandbox Job Runs -- the agent-execution subsystem fronted by this control
plane. Two kinds of routes live in this module, deliberately kept in one
file because they're two halves of the same lifecycle:

- `/api/v1/conversations/{id}/runs` and `/api/v1/runs/{id}[/cancel]`: the
  user-facing lifecycle, authenticated the normal way (get_current_user, a
  real JWT access token). Starting a run enqueues one session-scoped
  message (session_id=run_id -- see app/core/queue.py) onto the real,
  already-provisioned acb-msak-sbq-agent-runs Service Bus queue and returns
  immediately with status="queued" -- nothing in this process blocks
  waiting for a sandbox worker to pick it up. The sandbox worker (a
  separate process, sandbox_worker/worker.py) reports its result back via
  Redis (the "stream relay" in the target architecture, acb-msak-redis --
  see sandbox_worker/result_publisher.py and
  app/worker/result_consumer.py), a separate control-plane-side process
  that flips the row to succeeded/failed and inserts its run_usage row.
  This API process itself never touches Redis or Service Bus receive --
  it only ever enqueues (this file) or reads whatever result-consumer has
  already written to Postgres (the GET routes below).

- `/internal/runs/{id}/file` and `/internal/runs/{id}/llm`: the credential-
  broker routes (O1). These are the sandbox worker's ONLY way to read the
  uploaded file or talk to an LLM -- authenticated via get_run_claims (a
  run-provenance token, never a user's JWT, never a real DB/storage/LLM
  credential handed to the worker itself). See app/core/deps.py's
  get_run_claims docstring and app/core/security.py's create_run_token
  docstring for the full reasoning.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.core import queue
from app.core.deps import get_current_user, get_run_claims
from app.core.security import RunTokenClaims, create_run_token
from app.core.storage import read_upload
from app.db.models.conversation import Conversation
from app.db.models.file import UploadedFile
from app.db.models.run import AgentRun
from app.db.models.user import User
from app.schemas.run import (
    BrokerFileOut,
    BrokerLlmRequest,
    BrokerLlmResponse,
    RunCreate,
    RunOut,
)

router = APIRouter(tags=["runs"])


async def _get_owned_conversation(conversation_id: uuid.UUID, session: AsyncSession) -> Conversation:
    """Same collapse-to-404 reasoning as conversations.py's helper of the
    same name -- duplicated rather than imported across modules so each
    router file stays independently readable; if a third router needs this
    again it's worth promoting to a shared module then."""
    result = await session.execute(select(Conversation).where(Conversation.id == conversation_id))
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


async def _reap_stale_runs(user_id: uuid.UUID, session: AsyncSession) -> None:
    """Honest-failure reaper (O7), lazy/request-triggered version -- see
    app/config.py's run_timeout_minutes docstring for exactly why this is a
    stopgap and not a real background scheduler. Runs opportunistically
    before every new-run admission check so a user is never permanently
    blocked behind a run that will never finish.

    Covers two distinct stuck states, both measured against the same
    run_timeout_minutes threshold for simplicity (a real deployment might
    reasonably want separate thresholds -- a "queued too long" almost
    certainly means "no worker ever picked this up" rather than "still
    working," so it could time out faster than a "running too long" -- not
    tuned separately here):
      - status='running' whose started_at is stale: the worker claimed it
        (POST .../started) but never posted a result -- a crash, OOM, or a
        lost results-queue message.
      - status='queued' whose queued_at is stale: no worker ever claimed it
        at all -- e.g. sandbox-worker isn't running, or Service Bus
        delivery failed silently. Without this branch a queued run with no
        worker available would occupy an admission-control slot forever.
    """
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.run_timeout_minutes)
    result = await session.execute(
        select(AgentRun).where(
            AgentRun.user_id == user_id,
            AgentRun.status.in_(("queued", "running")),
        )
    )
    for run in result.scalars().all():
        stale = (
            (run.status == "running" and run.started_at is not None and run.started_at < cutoff)
            or (run.status == "queued" and run.queued_at < cutoff)
        )
        if not stale:
            continue
        reason = "no worker ever claimed it" if run.status == "queued" else "no result was ever posted"
        run.status = "failed"
        run.error_message = (
            f"Timed out after {settings.run_timeout_minutes} minutes ({reason}) -- treated as "
            "an infrastructure failure, not a silent success. See O7."
        )
        run.completed_at = datetime.now(timezone.utc)


@router.post(
    "/api/v1/conversations/{conversation_id}/runs",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    conversation_id: uuid.UUID,
    payload: RunCreate,
    user_and_session: tuple[User, AsyncSession] = Depends(get_current_user),
) -> RunOut:
    user, session = user_and_session
    conversation = await _get_owned_conversation(conversation_id, session)

    # RLS makes this the same "not found vs not yours" collapse as every
    # other owned-resource lookup in this API -- a file_id belonging to
    # another user simply isn't visible in this scoped session.
    file_result = await session.execute(
        select(UploadedFile).where(UploadedFile.id == payload.uploaded_file_id)
    )
    uploaded_file = file_result.scalar_one_or_none()
    if uploaded_file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Uploaded file not found")

    await _reap_stale_runs(user.id, session)

    settings = get_settings()
    in_flight = await session.execute(
        select(AgentRun).where(
            AgentRun.user_id == user.id,
            AgentRun.status.in_(("queued", "running")),
        )
    )
    in_flight_count = len(in_flight.scalars().all())
    if in_flight_count >= settings.max_concurrent_runs_per_user:
        # Admission control (O5): refuse new work outright rather than
        # accept it and let it queue indefinitely or contend unboundedly
        # for sandbox capacity -- the caller finds out immediately, not
        # after a long silent wait.
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"You already have {in_flight_count} run(s) in progress "
            f"(limit {settings.max_concurrent_runs_per_user}). Wait for one to "
            "finish or cancel it before starting another.",
        )

    run = AgentRun(
        user_id=user.id,
        conversation_id=conversation.id,
        uploaded_file_id=uploaded_file.id,
        status="queued",
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)

    # Explicit commit HERE, before this row is ever mentioned outside this
    # process. Real bug this fixes (found 2026-09-30, the first time an
    # actual sandbox-worker raced to consume its own freshly-created run --
    # scripts/smoke_test.py never catches this because it cancels the run
    # before the worker gets this far): get_current_user's own
    # scoped_session doesn't commit until ITS post-yield cleanup runs,
    # which happens only AFTER this function returns -- i.e. AFTER
    # queue.send_run_request() below already put the message on the REAL
    # Service Bus queue. A sandbox-worker fast enough to receive that
    # message and call /internal/runs/{id}/started before this process's
    # own (still-pending) transaction commits sees nothing: under ordinary
    # READ COMMITTED isolation, this row simply isn't visible yet to any
    # other Postgres connection. The result was a spurious, intermittent
    # 404 on the very first broker call of an otherwise entirely correct
    # run -- not a bug in the broker routes themselves (see
    # get_run_claims/broker_mark_started), purely a commit-ordering race.
    # flush() alone was enough before this pass because nothing OUTSIDE
    # this same transaction ever needed to see this row before the HTTP
    # response was even sent; Service Bus breaks that assumption by being
    # an external, immediately-consumable side channel with its own
    # network round trip. Safe to commit explicitly mid-request: this
    # session factory is built with expire_on_commit=False (see
    # app/db/base.py), so `run`'s already-loaded attributes stay usable
    # below without needing another refresh, and nothing after this point
    # touches an RLS-protected table again anyway.
    await session.commit()

    run_token = create_run_token(
        run_id=run.id,
        user_id=user.id,
        conversation_id=conversation.id,
        uploaded_file_id=uploaded_file.id,
    )

    # Service Bus I/O is blocking network I/O -- offloaded to a worker
    # thread so it never stalls the event loop the rest of this API process
    # shares with every other concurrent request. session_id=run.id because
    # the real queue is session-enabled (Service Bus requires it, not a
    # choice made here) -- see app/core/queue.py's module docstring. Sent
    # only after the explicit commit above -- see that comment for why.
    await run_in_threadpool(
        queue.send_run_request,
        str(run.id),
        {
            "run_id": str(run.id),
            # Carried through untouched by the worker -- once result
            # delivery is wired (Redis, per the target architecture; see
            # app/worker/result_consumer.py and DEFERRED_ITEMS.md), whatever
            # consumes that side will need this the same way the run token
            # does: to know which user's RLS context to open, since
            # agent_runs can't be looked up by run_id alone under RLS. A
            # wrong/forged value here is safe, not a privilege-escalation
            # risk -- RLS still filters on it, so a bad user_id just makes
            # the row invisible, never visible under the wrong tenant.
            "user_id": str(user.id),
            "run_token": run_token,
            "question": payload.question,
        },
    )

    return RunOut.model_validate(run)


@router.get("/api/v1/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID,
    user_and_session: tuple[User, AsyncSession] = Depends(get_current_user),
) -> RunOut:
    _, session = user_and_session
    result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return RunOut.model_validate(run)


@router.post("/api/v1/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(
    run_id: uuid.UUID,
    user_and_session: tuple[User, AsyncSession] = Depends(get_current_user),
) -> RunOut:
    """Sets status='cancelled' immediately, regardless of whether the run is
    merely queued (never started -- this alone stops it from ever starting)
    or already running (the sandbox worker discovers this cooperatively,
    either the next time it calls a broker endpoint -- see get_run_claims'
    409 check -- or via its own status poll, once the worker process
    exists). This is the local-dev cancellation mechanism; a real Azure
    deployment additionally has `az containerapp job stop-execution` as a
    hard kill for a worker that stops responding to the cooperative signal
    -- deliberately not the same mechanism, and documented as such in the
    deferred-items note (O6)."""
    _, session = user_and_session
    result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if run.status in ("succeeded", "failed", "cancelled"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run already {run.status}")

    run.status = "cancelled"
    run.completed_at = datetime.now(timezone.utc)
    await session.flush()
    await session.refresh(run)
    return RunOut.model_validate(run)


# --------------------------------------------------------------------------
# Internal credential-broker routes -- sandbox worker only, never a user.
# --------------------------------------------------------------------------


@router.post("/internal/runs/{run_id}/started", response_model=RunOut)
async def broker_mark_started(
    run_id: uuid.UUID,
    claims_and_session: tuple[RunTokenClaims, AsyncSession] = Depends(get_run_claims),
) -> RunOut:
    """Called once by the sandbox worker right after it pulls a message off
    the real acb-msak-sbq-agent-runs queue and before it does any file/LLM
    work -- flips
    queued -> running and stamps started_at. This keeps the control plane as
    the sole writer of agent_runs (per the user's explicit design: the
    worker never gets Postgres credentials, it only ever calls this
    authenticated broker endpoint), and it's what makes the O7 reaper's
    "running past run_timeout_minutes" check meaningful -- started_at has to
    come from somewhere, and it can't come from the worker writing the DB
    directly."""
    claims, session = claims_and_session
    if claims.run_id != run_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Run token does not match this run")
    result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if run.status != "queued":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is {run.status}, not queued")
    run.status = "running"
    run.started_at = datetime.now(timezone.utc)
    await session.flush()
    await session.refresh(run)
    return RunOut.model_validate(run)


@router.get("/internal/runs/{run_id}/status")
async def broker_get_status(
    run_id: uuid.UUID,
    claims_and_session: tuple[RunTokenClaims, AsyncSession] = Depends(get_run_claims),
) -> dict[str, str]:
    """The cooperative-cancellation check point (O6): get_run_claims itself
    already 409s if the run has been cancelled, so the worker's poll loop
    just needs to call this (cheaply, no file/LLM I/O) before starting work
    and periodically during it, and abort as soon as it gets a 409 rather
    than a 200. This is the local-dev cooperative mechanism -- see
    cancel_run's docstring for how it differs from the real Azure hard-kill
    path."""
    claims, session = claims_and_session
    if claims.run_id != run_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Run token does not match this run")
    result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return {"status": run.status}


@router.get("/internal/runs/{run_id}/file", response_model=BrokerFileOut)
async def broker_get_file(
    run_id: uuid.UUID,
    claims_and_session: tuple[RunTokenClaims, AsyncSession] = Depends(get_run_claims),
) -> BrokerFileOut:
    claims, session = claims_and_session
    if claims.run_id != run_id:
        # Defense in depth: the token is already scoped to one run_id, but
        # this rejects outright a worker that (whether by bug or by an
        # attempt to reuse a token against a different run_id in the URL)
        # asks for a run other than the one its own token names.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Run token does not match this run")
    if claims.uploaded_file_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This run has no associated file")

    result = await session.execute(
        select(UploadedFile).where(UploadedFile.id == claims.uploaded_file_id)
    )
    uploaded_file = result.scalar_one_or_none()
    if uploaded_file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Uploaded file not found")

    data = await run_in_threadpool(read_upload, uploaded_file.blob_path)
    return BrokerFileOut(
        original_filename=uploaded_file.original_filename,
        content_type=uploaded_file.content_type,
        data_base64=base64.b64encode(data).decode("ascii"),
    )


@router.post("/internal/runs/{run_id}/llm", response_model=BrokerLlmResponse)
async def broker_call_llm(
    run_id: uuid.UUID,
    payload: BrokerLlmRequest,
    claims_and_session: tuple[RunTokenClaims, AsyncSession] = Depends(get_run_claims),
) -> BrokerLlmResponse:
    """The sandbox worker's only path to a model call -- it never holds a
    model-provider API key itself. Today (local_dev_llm_stub=True) this
    returns a deterministic canned review instead of calling a real model,
    so smoke tests are fast, free, and reproducible. The real swap-in point
    is APIM as the credential broker in front of the actual model
    deployment (already named in app/db/models/run.py's RunUsage docstring
    as the intended source of run_usage rows, via an Event Hub consumer
    reading APIM's access logs) -- wiring that in is deferred to the
    Cloud-integration step, documented here rather than silently assumed.
    """
    claims, _session = claims_and_session
    if claims.run_id != run_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Run token does not match this run")

    settings = get_settings()
    if not settings.local_dev_llm_stub:
        # There is no real model client wired up yet -- fail loudly rather
        # than silently falling through to the stub if this flag is ever
        # flipped off before the real integration exists.
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "local_dev_llm_stub is disabled but no real LLM provider is wired up yet",
        )

    filename = payload.original_filename or "the uploaded drawing"
    question_part = f' The reviewer also asked: "{payload.question}"' if payload.question else ""
    excerpt_note = (
        f" (extracted content: {len(payload.file_excerpt)} characters analyzed)"
        if payload.file_excerpt
        else " (no extractable text content -- format not yet parsed beyond signature validation)"
    )
    review_text = (
        f"[LOCAL DEV STUB -- not a real model response] Reviewed {filename}{excerpt_note}. "
        "This stand-in does not perform an actual plan review; it exists so the "
        "run pipeline (upload -> parse -> LLM call -> chat result) can be smoke-"
        f"tested end to end without a real model provider wired up yet.{question_part}"
    )
    # Fabricated-but-plausible figures, scaled loosely off input size, so
    # run_usage rows and any cost-reporting UI built against them have
    # something non-zero and non-constant to render during local testing.
    input_tokens = max(50, len(payload.file_excerpt) // 4 + len(payload.question) // 4)
    output_tokens = max(30, len(review_text) // 4)
    cost_usd = round((input_tokens * 0.000003) + (output_tokens * 0.000015), 4)

    return BrokerLlmResponse(
        review_text=review_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
