"""
Run this against a live `uvicorn app.main:app` (default http://127.0.0.1:8000)
to exercise the whole auth flow, plus conversations/messages/uploads, end to
end with real HTTP requests:

    python scripts/smoke_test.py

Two scenarios:
  1. Auth lifecycle -- register -> duplicate-register rejected -> login
     rejected before the account is verified -> login with wrong password
     rejected -> verify-email with a bogus token rejected -> verify-email
     succeeds (and logs the account straight in, the same as clicking the
     link in a real email would) -> re-using that same verification token
     is rejected -> login succeeds -> refresh rotates the token -> reusing
     the OLD refresh token is rejected (and revokes the session) -> logout.
  2. Conversations/messages/uploads, across TWO separate users -- this is
     the API-level equivalent of the psql RLS check done by hand against
     migration 0001 (SET app.current_user_id to each user in turn and
     confirming one user's row is invisible under the other's session): here
     it's done as an actual authenticated HTTP client would see it --
     user B's JWT can create/list/post to *their own* conversation, and gets
     a 404 (not a 403, not another user's data) when trying to touch user
     A's conversation_id directly.

Exits non-zero with the first failing assertion's message on failure, so
this is safe to wire into a CI step later.
"""
import io
import os
import sys
import uuid
from urllib.parse import parse_qs, urlparse

import httpx

BASE_URL = os.environ.get("SMOKE_TEST_BASE_URL", "http://127.0.0.1:8000")


def _extract_verification_token(register_body: dict) -> str:
    # Only present while settings.local_dev_email_stub is True (see
    # app/core/mailer.py) -- which is the default, and what docker-compose.yml
    # runs this script against. If this ever fires in CI, the stub was
    # turned off without swapping in a way for this script to read the real
    # mailbox, not that verification itself is broken.
    url = register_body.get("dev_verification_url")
    assert url, (
        "expected dev_verification_url in the register response -- is "
        "local_dev_email_stub disabled in this environment?"
    )
    token = parse_qs(urlparse(url).query).get("verify_token")
    assert token, f"couldn't find verify_token in dev_verification_url: {url}"
    return token[0]


def register_and_login(client: httpx.Client, label: str) -> dict:
    email = f"smoketest-{label}-{uuid.uuid4().hex[:8]}@example.com"
    password = "Correct-Horse-9"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "display_name": f"Smoke Test {label}"},
    )
    assert r.status_code == 201, f"register ({label}) failed: {r.status_code} {r.text}"
    token = _extract_verification_token(r.json())
    r = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 200, f"verify-email ({label}) failed: {r.status_code} {r.text}"
    return r.json()


def test_auth_lifecycle() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        email = f"smoketest-{uuid.uuid4().hex[:8]}@example.com"
        password = "Correct-Horse-9"

        r = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": "Smoke Test"},
        )
        assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
        register_body = r.json()
        assert register_body["user"]["email"] == email
        assert "access_token" not in register_body, (
            "register() must not hand back usable tokens -- an unverified "
            "account shouldn't be able to sign in yet"
        )
        print("register: OK")

        r = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": "Smoke Test"},
        )
        assert r.status_code == 409, f"expected 409 on duplicate email, got {r.status_code}"
        print("duplicate register rejected: OK")

        r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 403, (
            f"expected 403 logging in before email verification, got {r.status_code}"
        )
        print("login before verification rejected: OK")

        r = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
        assert r.status_code == 401, f"expected 401 on wrong password, got {r.status_code}"
        print("login with wrong password rejected: OK")

        r = client.post("/api/v1/auth/verify-email", json={"token": "not-a-real-token"})
        assert r.status_code == 400, f"expected 400 on a bogus verification token, got {r.status_code}"
        print("verify-email with bogus token rejected: OK")

        verification_token = _extract_verification_token(register_body)
        r = client.post("/api/v1/auth/verify-email", json={"token": verification_token})
        assert r.status_code == 200, f"verify-email failed: {r.status_code} {r.text}"
        verify_tokens = r.json()
        assert verify_tokens["user"]["email"] == email
        print("verify-email logs the account straight in: OK")

        r = client.post("/api/v1/auth/verify-email", json={"token": verification_token})
        assert r.status_code == 400, (
            f"expected 400 re-using an already-used verification token, got {r.status_code}"
        )
        print("re-using a verification token rejected: OK")

        r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
        login_tokens = r.json()
        old_refresh = login_tokens["refresh_token"]
        print("login: OK")

        r = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
        assert r.status_code == 200, f"refresh failed: {r.status_code} {r.text}"
        new_tokens = r.json()
        assert new_tokens["refresh_token"] != old_refresh
        print("refresh rotates token: OK")

        r = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
        assert r.status_code == 401, f"expected 401 reusing rotated-away token, got {r.status_code}"
        print("reused old refresh token rejected: OK")

        r = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]}
        )
        assert r.status_code == 401, (
            "expected the *new* token to be revoked too after reuse was detected, "
            f"got {r.status_code}"
        )
        print("reuse-detection revokes the whole session chain: OK")

        r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200
        fresh_refresh = r.json()["refresh_token"]
        r = client.post("/api/v1/auth/logout", json={"refresh_token": fresh_refresh})
        assert r.status_code == 204, f"logout failed: {r.status_code} {r.text}"
        r = client.post("/api/v1/auth/refresh", json={"refresh_token": fresh_refresh})
        assert r.status_code == 401, "expected logged-out refresh token to be rejected"
        print("logout: OK")


def test_conversations_and_isolation() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        user_a = register_and_login(client, "a")
        user_b = register_and_login(client, "b")
        headers_a = {"Authorization": f"Bearer {user_a['access_token']}"}
        headers_b = {"Authorization": f"Bearer {user_b['access_token']}"}

        r = client.get("/api/v1/conversations", headers=headers_a)
        assert r.status_code == 200 and r.json() == [], "fresh user should have zero conversations"
        print("list conversations (empty): OK")

        r = client.post("/api/v1/conversations", json={"title": "User A's chat"}, headers=headers_a)
        assert r.status_code == 201, f"create conversation failed: {r.status_code} {r.text}"
        conversation_a = r.json()
        print("create conversation: OK")

        r = client.get("/api/v1/conversations", headers=headers_a)
        assert r.status_code == 200
        assert [c["id"] for c in r.json()] == [conversation_a["id"]]
        print("list conversations (one): OK")

        r = client.post(
            f"/api/v1/conversations/{conversation_a['id']}/messages",
            json={"content": "hello from user A"},
            headers=headers_a,
        )
        assert r.status_code == 201, f"post message failed: {r.status_code} {r.text}"
        message = r.json()
        assert message["role"] == "user"
        assert message["content"] == "hello from user A"
        print("post message: OK")

        r = client.post(
            f"/api/v1/conversations/{conversation_a['id']}/messages",
            json={"content": ""},
            headers=headers_a,
        )
        assert r.status_code == 422, f"expected 422 on empty message content, got {r.status_code}"
        print("empty message content rejected: OK")

        r = client.get(f"/api/v1/conversations/{conversation_a['id']}/messages", headers=headers_a)
        assert r.status_code == 200
        assert [m["content"] for m in r.json()] == ["hello from user A"]
        print("list messages: OK")

        # A real (if tiny) PDF signature -- validate_upload() in
        # app/core/upload_validation.py checks the actual bytes, not just
        # the ".pdf" extension or a client-supplied Content-Type, so this
        # has to genuinely start with "%PDF-" to be accepted.
        pdf_bytes = b"%PDF-1.4\n%mock pdf content for smoke test\n"
        files = {"file": ("notes.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
        r = client.post(
            f"/api/v1/conversations/{conversation_a['id']}/files", files=files, headers=headers_a
        )
        assert r.status_code == 201, f"upload failed: {r.status_code} {r.text}"
        uploaded = r.json()
        assert uploaded["original_filename"] == "notes.pdf"
        assert uploaded["size_bytes"] == len(pdf_bytes)
        print("upload file: OK")

        r = client.get(f"/api/v1/conversations/{conversation_a['id']}/messages", headers=headers_a)
        assert r.status_code == 200
        contents = [m["content"] for m in r.json()]
        assert any("notes.pdf" in c for c in contents), "expected an upload note in the message list"
        print("upload recorded in conversation timeline: OK")

        # ---- Upload type lockdown: extension allowlist + content signature ----
        files = {"file": ("game.scx", io.BytesIO(b"not a real drawing"), "application/octet-stream")}
        r = client.post(
            f"/api/v1/conversations/{conversation_a['id']}/files", files=files, headers=headers_a
        )
        assert r.status_code == 415, f"expected 415 on a disallowed extension, got {r.status_code}"
        print("upload with disallowed extension rejected: OK")

        # Right extension, wrong content -- proves the signature check runs,
        # not just the extension allowlist (renaming a non-PDF to .pdf must
        # still be rejected).
        files = {"file": ("fake.pdf", io.BytesIO(b"this is not actually a pdf"), "application/pdf")}
        r = client.post(
            f"/api/v1/conversations/{conversation_a['id']}/files", files=files, headers=headers_a
        )
        assert r.status_code == 415, (
            f"expected 415 on a .pdf-named file that isn't really a PDF, got {r.status_code}"
        )
        print("upload with mismatched content rejected: OK")

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"rest-of-file-does-not-matter-here"
        files = {"file": ("elevation.png", io.BytesIO(png_bytes), "image/png")}
        r = client.post(
            f"/api/v1/conversations/{conversation_a['id']}/files", files=files, headers=headers_a
        )
        assert r.status_code == 201, f"PNG upload failed: {r.status_code} {r.text}"
        print("PNG upload accepted: OK")

        dxf_bytes = b"  0\nSECTION\n  2\nHEADER\n  0\nENDSEC\n  0\nEOF\n"
        files = {"file": ("floorplan.dxf", io.BytesIO(dxf_bytes), "application/octet-stream")}
        r = client.post(
            f"/api/v1/conversations/{conversation_a['id']}/files", files=files, headers=headers_a
        )
        assert r.status_code == 201, f"DXF upload failed: {r.status_code} {r.text}"
        print("DXF upload accepted: OK")

        dwg_bytes = b"AC1032" + b"binary-payload-does-not-matter-here"
        files = {"file": ("sitework.dwg", io.BytesIO(dwg_bytes), "application/octet-stream")}
        r = client.post(
            f"/api/v1/conversations/{conversation_a['id']}/files", files=files, headers=headers_a
        )
        assert r.status_code == 201, f"DWG upload failed: {r.status_code} {r.text}"
        print("DWG upload accepted: OK")

        r = client.get(f"/api/v1/conversations/{conversation_a['id']}/messages", headers=headers_a)
        assert r.status_code == 200
        contents = [m["content"] for m in r.json()]
        assert any("DWG parsing isn't available" in c for c in contents), (
            "expected the DWG-unparseable note to appear in the conversation timeline"
        )
        print("DWG upload carries the not-yet-parseable note: OK")

        # ---- Rename ----
        r = client.patch(
            f"/api/v1/conversations/{conversation_a['id']}",
            json={"title": "Riverside Tower -- MEP review"},
            headers=headers_a,
        )
        assert r.status_code == 200, f"rename failed: {r.status_code} {r.text}"
        assert r.json()["title"] == "Riverside Tower -- MEP review"
        print("rename own conversation: OK")

        r = client.patch(
            f"/api/v1/conversations/{conversation_a['id']}",
            json={"title": ""},
            headers=headers_a,
        )
        assert r.status_code == 200 and r.json()["title"] is None, (
            "expected a blank title to clear back to null (Untitled project)"
        )
        print("clearing a title back to Untitled project: OK")

        # ---- Isolation: user B must not be able to touch user A's conversation ----
        r = client.get(f"/api/v1/conversations/{conversation_a['id']}/messages", headers=headers_b)
        assert r.status_code == 404, (
            f"expected 404 (not 200, not 403) when user B reads user A's conversation, "
            f"got {r.status_code}"
        )
        print("cross-user read of another user's conversation -> 404: OK")

        r = client.post(
            f"/api/v1/conversations/{conversation_a['id']}/messages",
            json={"content": "trying to post into someone else's chat"},
            headers=headers_b,
        )
        assert r.status_code == 404, (
            f"expected 404 when user B posts into user A's conversation, got {r.status_code}"
        )
        print("cross-user post into another user's conversation -> 404: OK")

        r = client.patch(
            f"/api/v1/conversations/{conversation_a['id']}",
            json={"title": "trying to rename someone else's project"},
            headers=headers_b,
        )
        assert r.status_code == 404, (
            f"expected 404 when user B renames user A's conversation, got {r.status_code}"
        )
        print("cross-user rename of another user's conversation -> 404: OK")

        r = client.get("/api/v1/conversations", headers=headers_b)
        assert r.status_code == 200 and r.json() == [], "user B's own list must stay empty"
        print("user B's own conversation list unaffected: OK")

        # No Authorization header at all -> 401, not a crash.
        r = client.get("/api/v1/conversations")
        assert r.status_code == 401, f"expected 401 with no token, got {r.status_code}"
        print("unauthenticated request rejected: OK")


def test_sandbox_run_lifecycle() -> None:
    """Covers what's testable about the Sandbox Job Runs pipeline WITHOUT a
    reachable real Azure Service Bus: 404/validation behavior on
    /api/v1/conversations/{id}/runs and /api/v1/runs/{id}[/cancel], and that
    the internal /internal/runs/... broker routes correctly refuse a normal
    user's access token (they require a run-provenance token, never a user
    JWT -- see app/core/deps.py's get_run_claims).

    Deliberately does NOT assert that creating a run reaches
    status="succeeded" -- that requires SERVICE_BUS_SEND_CONNECTION_STRING
    to point at the real, reachable Azure Service Bus queue, a running
    sandbox-worker, AND REDIS_PASSWORD to point at the real, reachable
    acb-msak-redis cache, none of which this script can fabricate or fake
    locally (see app/core/queue.py's module docstring: this subsystem
    deliberately talks to the real thing, there is no local stand-in to
    test against instead). The Redis-based result delivery itself is real
    now (see app/worker/result_consumer.py and
    sandbox_worker/result_publisher.py) -- but this test still cancels the
    run immediately after creating it (below), on purpose, so it never lets
    the worker actually pick it up and exercise that path. This only
    asserts that a run reaches status="queued" (i.e. the enqueue itself
    succeeded) and that cancellation works; the full succeeded/failed path
    needs to be verified by hand (create a run and don't cancel it, then
    poll GET /api/v1/runs/{id} and watch `docker compose logs sandbox-worker
    result-consumer`) until this script is extended to wait for a terminal
    status against real, reachable infrastructure.
    """
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        user = register_and_login(client, "sbxrun")
        headers = {"Authorization": f"Bearer {user['access_token']}"}

        r = client.post("/api/v1/conversations", json={}, headers=headers)
        assert r.status_code == 201, f"create conversation failed: {r.status_code} {r.text}"
        conversation_id = r.json()["id"]

        # ---- Creating a run against a file that doesn't exist (or isn't
        # this user's) must 404, the same "not found vs not yours" collapse
        # as every other owned-resource lookup in this API. This alone
        # doesn't touch Service Bus -- the file lookup happens first. ----
        r = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"uploaded_file_id": str(uuid.uuid4()), "question": "Any issues?"},
            headers=headers,
        )
        assert r.status_code == 404, (
            f"expected 404 for a nonexistent uploaded_file_id, got {r.status_code} {r.text}"
        )
        print("create run against nonexistent file -> 404: OK")

        # ---- GET/cancel on a nonexistent run -> 404 ----
        bogus_run_id = uuid.uuid4()
        r = client.get(f"/api/v1/runs/{bogus_run_id}", headers=headers)
        assert r.status_code == 404, f"expected 404 for nonexistent run, got {r.status_code}"
        print("get nonexistent run -> 404: OK")

        r = client.post(f"/api/v1/runs/{bogus_run_id}/cancel", headers=headers)
        assert r.status_code == 404, f"expected 404 cancelling a nonexistent run, got {r.status_code}"
        print("cancel nonexistent run -> 404: OK")

        # ---- A real user access token must NOT work against the internal
        # broker routes -- those require a run-provenance token (a
        # different `type` claim entirely, see create_run_token/
        # decode_run_token), never a user's JWT. This is the O1/O4 boundary
        # itself, directly testable without any Service Bus involvement. ----
        r = client.get(f"/internal/runs/{bogus_run_id}/status", headers=headers)
        assert r.status_code == 401, (
            f"expected 401 -- a user access token must not satisfy get_run_claims, "
            f"got {r.status_code} {r.text}"
        )
        print("user access token rejected by internal broker route -> 401: OK")

        r = client.get(f"/internal/runs/{bogus_run_id}/status")
        assert r.status_code == 401, f"expected 401 with no token at all, got {r.status_code}"
        print("unauthenticated request to internal broker route rejected: OK")

        # ---- Now upload a real file and attempt the live part. This is the
        # one assertion in this function that depends on
        # SERVICE_BUS_SEND_CONNECTION_STRING being real and reachable -- reported
        # clearly rather than silently skipped or silently passed either
        # way. ----
        pdf_bytes = b"%PDF-1.4\n%fake-pdf-for-smoke-test\n"
        files = {"file": ("floor-plan.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
        r = client.post(f"/api/v1/conversations/{conversation_id}/files", files=files, headers=headers)
        assert r.status_code == 201, f"upload failed: {r.status_code} {r.text}"
        uploaded_file_id = r.json()["id"]

        try:
            r = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={"uploaded_file_id": uploaded_file_id, "question": "Any code issues?"},
                headers=headers,
                timeout=15,
            )
        except httpx.TimeoutException as exc:
            print(
                f"NOTE: creating a run timed out ({exc}) -- this is expected if "
                "SERVICE_BUS_SEND_CONNECTION_STRING isn't set to a real, reachable Azure Service Bus "
                "connection string in this environment (see docker-compose.yml's top comment and "
                ".env.example). Not treated as a smoke-test failure since this script cannot supply "
                "that credential itself; verify this step by hand once real Service Bus access is "
                "configured."
            )
            return

        if r.status_code == 201:
            run = r.json()
            assert run["status"] == "queued", f"expected status='queued' right after creation, got {run}"
            print("create run against a real file -> 201 queued, enqueued onto Service Bus: OK")

            r = client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)
            assert r.status_code == 200 and r.json()["status"] == "cancelled", (
                f"expected cancel to succeed, got {r.status_code} {r.text}"
            )
            print("cancel a queued run -> status='cancelled': OK")

            r = client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)
            assert r.status_code == 409, f"expected 409 cancelling an already-cancelled run, got {r.status_code}"
            print("cancel an already-cancelled run -> 409: OK")
        else:
            print(
                f"NOTE: creating a run returned {r.status_code} rather than 201 -- this is "
                "expected if SERVICE_BUS_SEND_CONNECTION_STRING isn't set to a real, reachable "
                "Azure Service Bus connection string in this environment (see docker-compose.yml's "
                "top comment and .env.example). Not treated as a smoke-test failure since this "
                "script cannot supply that credential itself; verify this step by hand once real "
                f"Service Bus access is configured. Response body: {r.text[:500]}"
            )


def main() -> None:
    test_auth_lifecycle()
    print()
    test_conversations_and_isolation()
    print()
    test_sandbox_run_lifecycle()
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nSMOKE TEST FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except httpx.ConnectError:
        print(
            f"\nCould not connect to {BASE_URL} -- is `uvicorn app.main:app` running?",
            file=sys.stderr,
        )
        sys.exit(1)
