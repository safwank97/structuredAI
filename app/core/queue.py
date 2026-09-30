"""
Service Bus client for the control plane's one job here: enqueueing a run
request for the sandbox worker to pick up (see app/db/models/run.py's
AgentRun docstring for the row each message corresponds to).

Confirmed against the real, already-provisioned namespace (`az servicebus
queue show`):
  - ONE queue, acb-msak-sbq-agent-runs, not two. There is no reverse queue
    for the sandbox to send results back through -- that flow is Redis
    (stream relay), not Service Bus. See app/worker/result_consumer.py for
    the current (stubbed, not-yet-built) state of that side.
  - The queue has "requiresSession": true. That is NOT optional -- Service
    Bus rejects a plain, non-session send/receive against a session-enabled
    entity outright. Every send here carries a session_id.
  - The control plane's SAS policy (...-cplane-send) is send-only. This
    module deliberately has no receive/listen function at all: even if one
    were written, that SAS would reject it with an auth error, and having
    dead code here that *looks* like it could listen would misrepresent
    what this identity can actually do.

Talks to the real Azure Service Bus namespace directly -- no local
stand-in, unlike Blob Storage/email. It didn't need one: the namespace,
queue, and this SAS policy already existed for real before this code was
written.

IMPORTANT CAVEAT ON VERIFICATION: this sandbox's outbound network is
locked down the same way it has been for every other Azure SDK package in
this project -- `pip install azure-servicebus` cannot reach PyPI from here,
so this module is written strictly to the documented azure-servicebus SDK
surface for session-enabled queues, but has NOT been executed against the
real queue from this environment. Please verify on your own machine before
trusting this beyond "it should work as documented."
"""
from __future__ import annotations

import json
from typing import Any

from azure.servicebus import ServiceBusClient, ServiceBusMessage

from app.config import get_settings


def _client() -> ServiceBusClient:
    settings = get_settings()
    return ServiceBusClient.from_connection_string(settings.service_bus_send_connection_string)


def send_run_request(session_id: str, body: dict[str, Any]) -> None:
    """JSON-encodes `body` and sends it as a single Service Bus message on
    session `session_id` (this project uses the run_id as the session id --
    one message per session is fine, sessions are a hard requirement of the
    queue, not something this design otherwise needed multiple messages
    per). Synchronous (the non-async azure.servicebus client), called from
    the async FastAPI app via starlette's run_in_threadpool -- see
    app/api/v1/runs.py's create_run.
    """
    settings = get_settings()
    with _client() as client:
        with client.get_queue_sender(settings.service_bus_queue) as sender:
            sender.send_messages(ServiceBusMessage(json.dumps(body), session_id=session_id))
