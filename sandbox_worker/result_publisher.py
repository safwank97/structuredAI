"""
Publishes a completed (or failed) run's result to Redis, for the control
plane's result-consumer to pick up and write to Postgres -- see
app/worker/result_consumer.py's module docstring for the full design and
why Redis Streams specifically (not plain pub/sub): a stream + consumer
group gives at-least-once delivery, so a terminal success/failure record
can't be silently dropped just because the consumer wasn't subscribed at
the exact moment of publish. Plain pub/sub has no such guarantee -- a
message published with no subscriber listening is simply gone.

Like queue_client.py, this connects to the REAL acb-msak-redis cache
directly, using a plain env-var-sourced credential -- no Key Vault access
here, per O1 (the sandbox has no path to secrets of its own; see
queue_client.py's docstring for the same reasoning applied to Service Bus).

KNOWN GAP, flagged rather than glossed over: this currently reuses the
SAME Redis primary key the control plane's result-consumer uses to read
from this stream. There is only one real credential provisioned against
acb-msak-redis so far -- unlike Service Bus, which already has two
separately-scoped SAS policies (send-only for the control plane, listen-
only for the sandbox), Redis has no equivalent read/write split set up on
the real cache yet. A real deployment should give the sandbox a Redis ACL
user restricted to XADD on this one stream key and nothing else. Not yet
provisioned -- see DEFERRED_ITEMS.md.
"""
from __future__ import annotations

import json
import os

import redis

# Must match app/worker/result_consumer.py's REDIS_STREAM_NAME exactly.
# This is a logical key created at runtime inside the existing acb-msak-redis
# cache (via XADD), not a separate Azure resource -- there is nothing to
# provision for it in the portal. Still duplicated as a literal here rather
# than imported, because this package deliberately never imports from app/*
# (see queue_client.py's module docstring for why).
REDIS_STREAM_NAME = "agent-run-results"

REDIS_HOST = os.environ.get("REDIS_HOST", "acb-msak-redis.redis.cache.windows.net")
REDIS_SSL_PORT = int(os.environ.get("REDIS_SSL_PORT", "6380"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

# Non-SSL access is disabled on the real cache (confirmed via `az redis
# show`), so this is unconditionally a TLS connection -- there is no
# plaintext fallback to configure here.
_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_SSL_PORT,
            password=REDIS_PASSWORD,
            ssl=True,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
        )
    return _client


def publish_result(result: dict) -> None:
    """XADDs `result` (JSON-encoded into a single 'body' field, rather than
    spread across many Redis Stream fields) onto REDIS_STREAM_NAME. Letting
    the caller decide what to do if this raises (a connection error, an
    auth failure, etc.) rather than swallowing it here -- see worker.py's
    _deliver_result for how the one caller of this function handles that."""
    _get_client().xadd(REDIS_STREAM_NAME, {"body": json.dumps(result)})
