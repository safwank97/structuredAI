"""
Sandbox worker -- the process that will eventually run inside the real
acb-msak-acapps-env-sbxjb2 Container Apps Job. It is its own small,
self-contained package (see queue_client.py's docstring for why it doesn't
import anything from app/*): the sandbox is meant to be a minimal, isolated
surface with no Postgres/Key-Vault/Blob credentials, only a (listen-only)
Service Bus connection string and, per message, a one-time run-provenance
token that authorizes exactly one run's worth of broker calls.

Per message, in order:
  1. receive one message from acb-msak-sbq-agent-runs (run_id, user_id,
     run_token, question) -- a session-enabled queue, see queue_client.py
  2. POST /internal/runs/{run_id}/started   (queued -> running; the control
     plane is the one writing this to Postgres, per O1 -- this process
     never gets a DB credential of its own)
  3. GET  /internal/runs/{run_id}/status    (cooperative cancellation check
     before doing any real work -- O6)
  4. GET  /internal/runs/{run_id}/file      (the only way this process ever
     sees the uploaded bytes today -- O1. Per the target architecture this
     should be a platform-mounted Blob volume instead, avoiding RPC-per-file
     per O3 -- not yet rebuilt that way; see DEFERRED_ITEMS.md.)
  5. "parse" the file -- see _extract_text_excerpt()'s docstring for exactly
     how much real parsing this does today (not much yet -- flagged, not
     silently overclaimed)
  6. GET  /internal/runs/{run_id}/status again, right before the one step
     with real (even if currently stubbed) cost -- O6
  7. POST /internal/runs/{run_id}/llm       (the only way this process ever
     talks to a model today -- O1. Per the target architecture this should
     be a direct call to APIM instead, with APIM injecting the real
     Anthropic key -- not yet rebuilt that way; see DEFERRED_ITEMS.md.)
  8. deliver the result -- a Redis Streams publish (result_publisher.py)
     that the control plane's result-consumer reads via a consumer group
     and writes to Postgres (app/worker/result_consumer.py). See
     _deliver_result() below for what happens if the publish itself fails.

Deliberately minimal otherwise: one message at a time, no retry/backoff
beyond whatever Service Bus's own session-lock redelivery gives for free, no
wall-clock budget of its own beyond each HTTP call's timeout. That's enough
to prove the request-dispatch half of the pipeline and the credential-
isolation boundary for this pass; a hardened retry/dead-letter policy, a
per-run wall-clock budget, and multiple worker replicas (KEDA-scaled off
queue depth) are real future work -- see DEFERRED_ITEMS.md, not silently
assumed to already exist.
"""
from __future__ import annotations

import base64
import os
import sys
import traceback

import httpx

from sandbox_worker.queue_client import receive_one
from sandbox_worker.result_publisher import publish_result

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://api:8000")
REQUESTS_QUEUE = os.environ.get("SERVICE_BUS_QUEUE", "acb-msak-sbq-agent-runs")
POLL_WAIT_SECONDS = int(os.environ.get("SERVICE_BUS_RECEIVE_WAIT_SECONDS", "20"))
HTTP_TIMEOUT_SECONDS = float(os.environ.get("CONTROL_PLANE_TIMEOUT_SECONDS", "30"))


def _extract_text_excerpt(filename: str, data: bytes) -> str:
    """Deliberately NOT real drawing parsing yet. A DXF is genuinely open
    and parseable (ezdxf would be the real tool -- this was the whole reason
    DXF was accepted for upload in the first place, unlike DWG); a PDF's
    text/layout could be pulled with a real extractor library. Neither is
    wired in here -- this only reports file metadata (name, extension,
    size), so the pipeline's SHAPE (upload -> parse -> LLM call -> result)
    is real and testable end to end today, while what "parse" actually
    extracts is honestly minimal. This is a documented, deliberate scope
    line for this pass, not a silent gap -- see DEFERRED_ITEMS.md."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else "unknown"
    return f"file={filename} ext={ext} size_bytes={len(data)}"


def _deliver_result(run_id: str, user_id: str, result: dict) -> None:
    """Publishes `result` to Redis (Streams) for the control plane's
    result-consumer to pick up -- see result_publisher.py and
    app/worker/result_consumer.py for the full design.

    A publish failure here is caught and logged loudly rather than raised.
    This is deliberate, not a case of swallowing an error for convenience:
    this function is called both from the normal success path and from
    main()'s top-level crash handler below, so letting an exception escape
    here would either mask the real underlying error (in the crash-handler
    case, where the original exception is what actually matters) or crash
    the whole worker process over one run's delivery hiccup. The backstop
    for "the result never actually arrived anywhere" already exists and is
    real: app/api/v1/runs.py's O7 reaper flips a run stuck in 'running'
    past run_timeout_minutes to a clearly-labeled infra-timeout failure, so
    a lost result here is surfaced (late, but honestly) rather than hidden
    forever."""
    try:
        publish_result(result)
    except Exception as exc:  # noqa: BLE001 -- see docstring for why this is caught, not re-raised
        print(
            f"[worker] run {run_id} (user {user_id}): FAILED TO PUBLISH RESULT TO REDIS: {exc}. "
            "This run's outcome is now lost unless the O7 reaper catches the stuck run later. "
            "Outcome that failed to deliver was:",
            file=sys.stderr,
        )
        print(f"[worker]   {result}", file=sys.stderr)
        return
    print(f"[worker] run {run_id} (user {user_id}): result published to Redis (status={result.get('status')})")


def _get(client: httpx.Client, path: str, run_token: str) -> dict:
    response = client.get(
        f"{CONTROL_PLANE_URL}{path}",
        headers={"Authorization": f"Bearer {run_token}"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _post(client: httpx.Client, path: str, run_token: str, json_body: dict) -> dict:
    response = client.post(
        f"{CONTROL_PLANE_URL}{path}",
        headers={"Authorization": f"Bearer {run_token}"},
        json=json_body,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def process_one(client: httpx.Client, message: dict) -> None:
    run_id = message["run_id"]
    user_id = message["user_id"]
    run_token = message["run_token"]
    question = message.get("question", "")

    try:
        _post(client, f"/internal/runs/{run_id}/started", run_token, json_body={})
    except httpx.HTTPStatusError as exc:
        # A redelivered message (worker crashed after marking started but
        # before completing) hits this as a 409 -- not fatal, just skip the
        # transition and keep going rather than wedge on it.
        print(f"[worker] run {run_id}: could not mark started ({exc}); continuing", file=sys.stderr)

    status_body = _get(client, f"/internal/runs/{run_id}/status", run_token)
    if status_body.get("status") == "cancelled":
        print(f"[worker] run {run_id}: cancelled before work started, aborting", file=sys.stderr)
        return

    file_body = _get(client, f"/internal/runs/{run_id}/file", run_token)
    data = base64.b64decode(file_body["data_base64"])
    excerpt = _extract_text_excerpt(file_body["original_filename"], data)

    status_body = _get(client, f"/internal/runs/{run_id}/status", run_token)
    if status_body.get("status") == "cancelled":
        print(f"[worker] run {run_id}: cancelled before LLM call, aborting", file=sys.stderr)
        return

    llm_body = _post(
        client,
        f"/internal/runs/{run_id}/llm",
        run_token,
        json_body={
            "question": question,
            "file_excerpt": excerpt,
            "original_filename": file_body["original_filename"],
        },
    )

    _deliver_result(
        run_id,
        user_id,
        {
            "run_id": run_id,
            "user_id": user_id,
            "status": "succeeded",
            "review_text": llm_body["review_text"],
            "input_tokens": llm_body["input_tokens"],
            "output_tokens": llm_body["output_tokens"],
            "cost_usd": llm_body["cost_usd"],
        },
    )
    print(f"[worker] run {run_id}: succeeded")


def main() -> None:
    print(f"[worker] starting -- polling {REQUESTS_QUEUE} via control plane at {CONTROL_PLANE_URL}")
    with httpx.Client() as client:
        while True:
            message = receive_one(REQUESTS_QUEUE, wait_seconds=POLL_WAIT_SECONDS)
            if message is None:
                continue
            run_id = message.get("run_id", "<unknown>")
            user_id = message.get("user_id")
            try:
                process_one(client, message)
            except Exception as exc:  # noqa: BLE001 -- top-level loop guard must never die
                # A crash here must never look like a clean success (O7): if
                # both run_id and user_id are known (the message decoded far
                # enough to have them), report the run as failed explicitly
                # rather than silently dropping it. If user_id itself is
                # missing (a badly malformed message), there's nothing safe
                # to attribute this to -- it's just logged, and the control
                # plane's timeout reaper is the backstop for that rarer case.
                print(f"[worker] run {run_id}: unhandled error: {exc}", file=sys.stderr)
                traceback.print_exc()
                if user_id is not None:
                    _deliver_result(
                        run_id,
                        user_id,
                        {
                            "run_id": run_id,
                            "user_id": user_id,
                            "status": "failed",
                            "error_message": f"Sandbox worker crashed: {exc}",
                        },
                    )


if __name__ == "__main__":
    main()
