"""
Result consumer -- the control-plane-side process meant to turn a sandbox
worker's result into a Postgres write. This is what would make the control
plane the sole writer of agent_runs / run_usage / messages, per the user's
own explicit design: the sandbox worker never gets a database credential of
its own, only Service Bus (send is not even available to it; see
sandbox_worker/queue_client.py) and a one-time run-provenance token per run.

STATUS: the Postgres-writing logic below (_handle_result) is real and did
not need to change once the transport was decided. How a result actually
reaches this process went through two corrected assumptions before landing
here:

  1. This used to drain a second Service Bus queue
     ("sandbox-run-results"). That queue does not exist on the real,
     already-provisioned namespace -- there is exactly one queue
     (acb-msak-sbq-agent-runs), and it is one-directional (control plane ->
     sandbox only; confirmed by its send-only vs listen-only SAS policies).
  2. Per the target architecture, results/streaming come back via Redis
     (the "stream relay" in the architecture diagram), using this
     project's already-provisioned acb-msak-redis instance.

`run_forever()` below now consumes a Redis Stream (REDIS_STREAM_NAME) via a
consumer group, rather than plain pub/sub -- a result that's published
while this process isn't actively blocked on a read must still be
delivered once it comes back up, and pub/sub has no such guarantee (a
message published with no subscriber listening is simply gone). A stream +
consumer group gives that at-least-once guarantee: XREADGROUP hands a
message to exactly one consumer in the group and leaves it in a "pending"
state until explicitly XACKed, so this process crashing mid-_handle_result
does not lose the message -- it gets redelivered to whichever consumer
picks it up next (here, the recovery pass at startup below, since this
runs as a single replica per consumer name).

Each Stream entry carries the whole result as one JSON-encoded field
("body") rather than being spread across native Stream fields -- simpler to
keep in exact sync with sandbox_worker/result_publisher.py's XADD call,
which cannot import this module's constants (see that file's docstring for
why) and must match this one's REDIS_STREAM_NAME by literal string value
instead.

RLS bootstrapping note (still applies): agent_runs is
RLS-protected and "fails closed" with no app.current_user_id set (see
app/core/deps.py's module docstring), so this consumer cannot look a run up
by run_id alone -- it has to already know which user's RLS context to open.
Whatever transport is used, the result payload needs to carry user_id (put
there by the control plane at enqueue time, echoed through unmodified by
the sandbox worker -- see runs.py's create_run comment on this field). A
wrong or forged user_id there is not a privilege-escalation risk: RLS still
filters the subsequent SELECT on it, so a bad value just makes the target
row invisible (handled below as "no matching run, dropping message"), never
visible under the wrong tenant.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone

import redis.asyncio as redis
from sqlalchemy import select

from app.config import get_settings
from app.core.deps import scoped_session
from app.db.models.conversation import Message
from app.db.models.run import AgentRun, RunUsage

# Must match sandbox_worker/result_publisher.py's REDIS_STREAM_NAME exactly
# -- see that file's docstring for why this is a duplicated literal rather
# than a shared import.
REDIS_STREAM_NAME = "agent-run-results"
REDIS_CONSUMER_GROUP = "result-consumer"
# Docker/Container Apps sets HOSTNAME to the container's own id automatically
# -- that gives every replica of this process a free, unique consumer name
# with zero extra config. Uniqueness matters here: two consumers sharing one
# name would each think they own the other's pending (unacked) entries.
REDIS_CONSUMER_NAME = os.environ.get("HOSTNAME", "result-consumer-1")
# How long one XREADGROUP call blocks waiting for a new entry before
# returning empty and looping again -- just a polling cadence, not a
# correctness knob.
BLOCK_MS = 20_000


async def _handle_result(body: dict) -> None:
    try:
        run_id = uuid.UUID(body["run_id"])
        owner_user_id = uuid.UUID(body["user_id"])
    except (KeyError, ValueError, TypeError) as exc:
        print(f"[result-consumer] malformed result message, dropping: {exc}", file=sys.stderr)
        return

    incoming_status = body.get("status", "failed")

    async with scoped_session(owner_user_id) as session:
        result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            print(
                f"[result-consumer] run {run_id}: no matching row under user {owner_user_id}, "
                "dropping message",
                file=sys.stderr,
            )
            return
        if run.status == "cancelled":
            # Cancellation is user-initiated and final -- a result arriving
            # after the fact (the worker raced the cancel request) does not
            # get to overwrite it.
            print(f"[result-consumer] run {run_id}: result arrived after cancellation, ignoring")
            return
        if run.status in ("succeeded", "failed"):
            # Already terminal (e.g. the reaper already timed it out, or a
            # duplicate delivery) -- Service Bus's PEEK_LOCK mode can
            # redeliver a message if completion didn't round-trip, so this
            # has to be idempotent rather than assume exactly-once delivery.
            print(f"[result-consumer] run {run_id}: already {run.status}, ignoring duplicate result")
            return

        run.completed_at = datetime.now(timezone.utc)
        if incoming_status == "succeeded":
            run.status = "succeeded"
            session.add(
                RunUsage(
                    run_id=run.id,
                    user_id=owner_user_id,
                    input_tokens=int(body.get("input_tokens", 0) or 0),
                    output_tokens=int(body.get("output_tokens", 0) or 0),
                    cost_usd=float(body.get("cost_usd", 0) or 0),
                )
            )
            if run.conversation_id is not None:
                session.add(
                    Message(
                        conversation_id=run.conversation_id,
                        user_id=owner_user_id,
                        role="assistant",
                        content=body.get("review_text") or "(no review text returned)",
                    )
                )
        else:
            run.status = "failed"
            run.error_message = str(body.get("error_message") or "Sandbox worker reported failure")[:2000]
            if run.conversation_id is not None:
                session.add(
                    Message(
                        conversation_id=run.conversation_id,
                        user_id=owner_user_id,
                        role="system",
                        content=f"Review run failed: {run.error_message}",
                    )
                )
        await session.flush()

    print(f"[result-consumer] run {run_id}: recorded status={incoming_status}")


async def _drain(client: redis.Redis, *, start_id: str, block_ms: int | None = None) -> None:
    """One XREADGROUP call, dispatching whatever entries it returns to
    _process_entry. start_id="0" asks Redis for entries already delivered
    to REDIS_CONSUMER_NAME but never XACKed (the crash-recovery case);
    start_id=">" asks for genuinely new entries -- see run_forever()."""
    kwargs: dict = {"count": 10}
    if block_ms is not None:
        kwargs["block"] = block_ms
    response = await client.xreadgroup(
        REDIS_CONSUMER_GROUP, REDIS_CONSUMER_NAME, {REDIS_STREAM_NAME: start_id}, **kwargs
    )
    if not response:
        return
    for _stream_name, entries in response:
        for entry_id, fields in entries:
            await _process_entry(client, entry_id, fields)


async def _process_entry(client: redis.Redis, entry_id: str, fields: dict) -> None:
    raw = fields.get("body")
    if raw is None:
        print(f"[result-consumer] entry {entry_id}: missing 'body' field, acking and dropping", file=sys.stderr)
        await client.xack(REDIS_STREAM_NAME, REDIS_CONSUMER_GROUP, entry_id)
        return

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[result-consumer] entry {entry_id}: invalid JSON body ({exc}), acking and dropping", file=sys.stderr)
        await client.xack(REDIS_STREAM_NAME, REDIS_CONSUMER_GROUP, entry_id)
        return

    try:
        await _handle_result(body)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see comment below
        # Deliberately NOT acked: leaving this entry pending means it gets
        # redelivered to this same consumer name via the id="0" recovery
        # pass the next time this process starts (see run_forever()). A
        # result that crashes on its way into Postgres must not silently
        # look like it was ever recorded (O7) -- better to retry it (or
        # require a human to look at repeated log lines for the same
        # entry_id) than to ack and lose it.
        print(f"[result-consumer] entry {entry_id}: _handle_result failed, leaving pending for retry: {exc}", file=sys.stderr)
        traceback.print_exc()
        return

    await client.xack(REDIS_STREAM_NAME, REDIS_CONSUMER_GROUP, entry_id)


async def run_forever() -> None:
    """Consumes REDIS_STREAM_NAME via a Redis Streams consumer group and
    calls _handle_result(body) for each entry -- see this module's
    docstring for the full at-least-once delivery reasoning."""
    settings = get_settings()
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)

    try:
        await client.xgroup_create(REDIS_STREAM_NAME, REDIS_CONSUMER_GROUP, id="0", mkstream=True)
        print(f"[result-consumer] created consumer group {REDIS_CONSUMER_GROUP!r} on stream {REDIS_STREAM_NAME!r}")
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
        print(f"[result-consumer] consumer group {REDIS_CONSUMER_GROUP!r} already exists on {REDIS_STREAM_NAME!r}")

    print(
        f"[result-consumer] consuming {REDIS_STREAM_NAME!r} as {REDIS_CONSUMER_NAME!r} "
        f"in group {REDIS_CONSUMER_GROUP!r}"
    )

    # Recovery pass: re-deliver anything already assigned to THIS consumer
    # name that was never XACKed (this same process crashing mid-
    # _handle_result on a previous run is the main way that happens).
    # start_id="0" means "this consumer's own pending entries", not "new
    # entries" -- see the Redis Streams docs on XREADGROUP's id argument.
    await _drain(client, start_id="0")

    while True:
        await _drain(client, start_id=">", block_ms=BLOCK_MS)


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
