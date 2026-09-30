"""
Minimal, self-contained Service Bus receive helper for the sandbox worker --
intentionally a near-duplicate of app/core/queue.py's shape rather than an
import of it (see this file's previous revision / worker.py's docstring for
why: this package must never drag in the control plane's FastAPI/SQLAlchemy/
Key-Vault-client code just to reuse ~20 lines).

LISTEN-ONLY, matching the real, already-provisioned SAS policy
(...-sbxjb2-listen): this identity cannot send. There is no send function in
this module -- a compromised sandbox worker process holding this connection
string cannot inject messages onto the queue even if it tried, because the
SAS itself doesn't grant Send rights, not because this code declines to
expose one.

The real queue (acb-msak-sbq-agent-runs) is session-enabled
("requiresSession": true), which is a hard Service Bus requirement, not a
design choice made here -- a plain non-session receiver is rejected outright
against a session-enabled entity. This receives via
session_id=NEXT_AVAILABLE_SESSION, which Service Bus resolves to whichever
session (run) currently has a message waiting.

Reads its connection string from a plain environment variable
(SERVICE_BUS_CONNECTION_STRING), NOT from Key Vault -- the sandbox worker
has no managed identity granting it Key Vault access in this design. In a
real deployment this value reaches the container via a Container Apps Job
secretRef sourced from Key Vault by the platform itself (the job's own
managed identity, scoped only to that one secret) -- the sandbox process
itself never calls Key Vault. Locally, the same docker-compose-injected env
var.
"""
from __future__ import annotations

import json
import os
from typing import Any

from azure.servicebus import NEXT_AVAILABLE_SESSION, ServiceBusClient
from azure.servicebus.exceptions import OperationTimeoutError, ServiceBusError


def _client() -> ServiceBusClient:
    connection_string = os.environ["SERVICE_BUS_CONNECTION_STRING"]
    return ServiceBusClient.from_connection_string(connection_string)


def receive_one(queue_name: str, wait_seconds: int = 20) -> dict[str, Any] | None:
    """Picks up the next available session on `queue_name`, reads its one
    message, and returns the decoded body -- or None if no session had a
    message waiting within `wait_seconds` (the normal, expected outcome of
    most poll iterations, not an error).

    A malformed (non-JSON) message body is dead-lettered rather than
    retried forever. Completing the message (removing it from the queue) is
    the caller's problem to get right in the sense that this function
    considers a message "done" the moment it hands the body back -- see
    worker.py's process_one for what happens if processing then fails
    partway through.
    """
    with _client() as client:
        try:
            with client.get_queue_receiver(
                queue_name, session_id=NEXT_AVAILABLE_SESSION, max_wait_time=wait_seconds
            ) as receiver:
                messages = receiver.receive_messages(max_message_count=1, max_wait_time=wait_seconds)
                if not messages:
                    return None
                message = messages[0]
                try:
                    body = json.loads(str(message))
                except (ValueError, TypeError):
                    receiver.dead_letter_message(message, reason="invalid-json")
                    return None
                receiver.complete_message(message)
                return body
        except (OperationTimeoutError, ServiceBusError):
            # No session was available within wait_seconds -- Service Bus's
            # session-receive raises rather than just returning empty the
            # way a plain (non-session) receiver does. Treated identically
            # to "no message" by the caller's poll loop.
            return None
