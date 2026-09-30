# Deferred items

Running list of things that are deliberately documented-not-built (the
DWG-parsing pattern), or known-incomplete, across this project. Kept here
instead of scattered across chat history so both of us can check it before
assuming something is done. Each item names the real reason it's deferred,
not just that it is.

## Infra / credentials

1. **Postgres app-role (`acb_app`) password does not exist in Key Vault
   yet.** Only the admin password (`acb-msak-pg-admin-password`) has ever
   been created. `app/config.py` expects a secret named
   `acb-msak-pg-app-password` once it's real. Steps to create it for real
   (run against the real Azure Postgres, not local docker-compose):

   ```bash
   # 1. Generate a strong password
   APP_PW=$(openssl rand -base64 32)

   # 2. Run the migration against the REAL Postgres as admin -- this is
   #    what actually creates the acb_app role, with this password baked
   #    into it via CREATE ROLE ... WITH LOGIN PASSWORD (see
   #    alembic/versions/0001_initial_schema.py's upgrade()).
   export DATABASE_URL="postgresql+asyncpg://acbmsakadmin:<admin-password>@acb-msak-postgre-sql.postgres.database.azure.com:5432/postgres"
   export ACB_APP_ROLE_PASSWORD="$APP_PW"
   python -m alembic upgrade head

   # 3. Store that same password in Key Vault under the name this code expects
   az keyvault secret set --vault-name acb-msak-kvault \
     -n acb-msak-pg-app-password --value "$APP_PW"

   # 4. Validate it was stored (subject to the same network ACL that blocked
   #    the other `az keyvault secret show` calls earlier -- this may also
   #    403 from outside the VNet/allowed IP; that's a separate, already-known
   #    constraint, not a sign step 3 failed)
   az keyvault secret show --vault-name acb-msak-kvault -n acb-msak-pg-app-password
   ```

   The role is only ever created once (the migration checks
   `pg_roles` first) -- re-running `alembic upgrade head` later with a
   different `ACB_APP_ROLE_PASSWORD` will NOT change an existing role's
   password. Rotating it later is a separate `ALTER ROLE acb_app WITH
   PASSWORD '...'` + a new `az keyvault secret set` (same name, new
   version).

2. **No APIM subscription key/product created for the sandbox's calls to
   APIM.** Real APIM instances commonly gate access with a subscription key
   even when network reachability (the firewall allow-list) already permits
   the call. Not yet needed because the sandbox doesn't call APIM directly
   yet (see "LLM call routed through the control plane" below) -- walk
   through creating a product + subscription in APIM and storing the key in
   Key Vault when that direct-to-APIM wiring is actually built.

## Architecture gaps vs. the target diagram

3. **LLM call is routed through the control plane's own
   `/internal/runs/{id}/llm` endpoint, not directly to APIM.** Target
   architecture has the sandbox worker calling APIM directly (through the
   firewall allow-list), with APIM injecting the real Anthropic key from Key
   Vault. Chosen to restructure toward this (see item 5) rather than keep
   the control-plane-broker shape as a permanent stand-in.

4. **File access is an HTTP broker endpoint (`GET
   /internal/runs/{id}/file`), not a platform-mounted volume.** This is
   exactly the "RPC-per-file" pattern O3 says not to do. Target: Blob
   Storage mounted directly into the ACA Job's filesystem, tenant-scoped
   path, no network round-trip needed for file I/O at all.

5. ~~**Result/streaming delivery is not wired to anything yet.**~~
   **Resolved 2026-09-30.** `sandbox_worker/result_publisher.py` now XADDs
   the result onto a Redis Stream (`agent-run-results`, a logical key
   inside `acb-msak-redis` -- nothing to provision in the portal for it);
   `app/worker/result_consumer.py`'s `run_forever()` reads it via a
   consumer group (XREADGROUP/XACK) and calls the existing
   `_handle_result()` unchanged. Chosen over plain pub/sub specifically
   because a consumer-group stream gives at-least-once delivery (an unacked
   entry gets redelivered to the same consumer name on restart via a
   startup recovery pass reading `start_id="0"`) -- pub/sub would silently
   drop a terminal result if the consumer wasn't actively subscribed at
   publish time, which isn't acceptable here. NOT yet covered: the SSE
   relay of live chunks to the browser (O9) -- this pass only wires the
   one terminal result per run, not a token-by-token stream; that's still
   future work once a UI exists to receive it. Also NOT yet verified: that
   `acb-msak-redis`'s network ACLs actually allow reaching it from outside
   its VNet the way Service Bus does -- see item 16 below and
   `docker-compose.yml`'s comment on this.

6. **DXF/PDF content parsing is metadata-only.** The sandbox worker
   reports filename/extension/size, not real drawing content. `ezdxf`
   (DXF, genuinely open) or a PDF text/layout extractor are real, buildable
   tools -- not wired in this pass.

7. **DWG upload is accepted but never parsed.** Closed/undocumented format;
   real parsing needs a paid SDK or API. Documented in-product as a future
   paid-tier capability (see `app/core/upload_validation.py`).

8. **Real deny-by-default network egress for the sandbox (O2) is partial
   in local Docker Compose.** The worker is provably cut off from Postgres
   (separate Docker network), but still has normal internet egress to
   reach Service Bus -- Compose can't express "allow only these specific
   external endpoints, block everything else." Real version needs Azure
   NSG/firewall rules with an allow-list (Service Bus + APIM, once direct-
   to-APIM lands), enforced at the real subnet/firewall layer.

9. **KEDA-based autoscaling of the sandbox worker is not built.** Currently
   a simple polling loop against the request queue. Real deployment scales
   the Container Apps Job from zero on Service Bus queue depth via KEDA.

10. **Admission control (O5) has no prewarmed pool.** The hard per-user
    concurrency cap (`max_concurrent_runs_per_user`) is real and enforced;
    the "modest prewarmed pool to cut cold-start latency" half of O5 is not
    built.

11. **The O7 reaper is lazy, not a real scheduler.** Only runs when a user
    tries to start a new run (`_reap_stale_runs` in `app/api/v1/runs.py`).
    A stuck run belonging to a user who never starts another one won't get
    caught until they do. Real version wants a periodic job (a small
    Container Apps Job on a timer, or a background task in the API
    process).

12. **Cancellation (O6) is cooperative only.** `POST /api/v1/runs/{id}/cancel`
    flips Postgres state and the worker checks it between steps. The real
    Azure hard-kill path (`az containerapp job stop-execution`) for a
    worker that stops responding entirely is not wired in.

13. **No real virus-scan pipeline for uploads.** Every upload is marked
    `status="clean"` outright; no scanner runs.

14. **`AgentRun.container_name` is never populated.** Column exists,
    nothing writes to it yet -- wire it once a real ACA Job execution
    actually happens (real name: `acb-msak-acapps-env-sbxjb2`, never
    `acb-msak-acapps-sbx-jb` -- that resource doesn't exist and won't until
    its subnet's SAL issue is resolved).

15. **JWT signing key's real Key Vault secret name is unconfirmed.**
    Unlike Postgres/Service Bus/Redis, no one has verified whether or where
    a JWT signing secret was actually provisioned in `acb-msak-kvault`.
    `app/config.py` currently guesses `jwt-signing-key`.

16. **Sandbox worker's Redis credential is not separately scoped.**
    `sandbox_worker/result_publisher.py` currently reuses the exact same
    `acb-msak-redis-pkey` primary key that the control plane's
    result-consumer uses to read the stream, because that's the only real
    credential provisioned against `acb-msak-redis` so far. Unlike Service
    Bus (which already has a genuine send-only-vs-listen-only SAS split), a
    compromised sandbox worker holding this value could read or write
    anything in the cache, not just XADD onto `agent-run-results`. Real
    fix: a Redis ACL user for the sandbox, restricted to `+xadd` on that
    one key and nothing else (Azure Cache for Redis supports Redis 6 ACLs
    on the Enterprise/Enterprise Flash tiers; confirm the real cache's tier
    supports this before assuming it's available). Not yet set up.

17. **Whether `acb-msak-redis` is reachable from outside its VNet is
    unverified.** Nothing in this project's Azure CLI output so far has
    confirmed or denied `acb-msak-redis`'s `networkAcls`/public-network-
    access setting, unlike Key Vault (`defaultAction: Deny` + an IP-allow
    rule, confirmed) or Service Bus (confirmed reachable -- the smoke test
    has enqueued onto it successfully). If `result-consumer` or
    `sandbox-worker` fail to connect once `REDIS_PASSWORD` is set, check
    this first (`az redis show --query "publicNetworkAccess"` or the
    portal's Networking blade) before assuming the new code is wrong.

18. **Redis firewall temporarily re-allow-lists the dev laptop's public IP,
    not just the Azure Firewall's static IP.** `acb-msak-redis`'s firewall
    was intentionally left with only `AllowFirewallStaticIp`
    (`acb-msak-az-fw-pubip`, 172.173.110.176) after step 9f's cleanup --
    the real target design routes all cloud-side traffic to Redis through
    the Azure Firewall, never directly from a laptop. Local `docker
    compose` testing doesn't go through that firewall at all, though --
    `result-consumer`/`sandbox-worker` running on a dev laptop reach
    `acb-msak-redis` over the laptop's own ISP-assigned public IP, which is
    exactly why v1.18's first real test timed out connecting to Redis (a
    clean TCP-level `TimeoutError`, not an auth or DNS failure) even with a
    correct connection string and `publicNetworkAccess: Enabled`. A second
    rule (`AllowMyCurrentDevIp`, the dev laptop's public IP) has been
    temporarily re-added so local verification can continue -- resolves
    item 17 above for the purpose of local testing, but does not change
    what the real deployment should look like.

    THIS RULE MUST BE REMOVED before the interview, ideally right after
    local Sandbox Job Runs verification is done and this workload is
    fully exercised from inside Azure instead (Container Apps reaching
    `acb-msak-redis` from within the VNet, or via the firewall's static IP
    the way the design intends). Removal command (mirrors the delete
    already done once for the original temporary rule in step 9f):

    ```bash
    az redis firewall-rules delete -g acb-msak-rg --name acb-msak-redis \
      --rule-name AllowMyCurrentDevIp
    ```

## Format

New items go at the end of their section with a number, not inserted
mid-list, so an earlier conversation's item N still means the same thing
later. Mark an item resolved by striking it through and dating it, rather
than deleting it -- a fixed gap is still useful history.
