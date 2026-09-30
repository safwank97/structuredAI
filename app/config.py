"""
Application settings.

Non-secret configuration comes from environment variables (set on the Container
App / Container Apps Job via `--env-vars`, or a local `.env` file for dev).
Secrets (DB password, JWT signing key, Service Bus connection strings, Redis
key) are pulled from Key Vault at startup using the cplane user-assigned
managed identity (acb-msak-iam-id-cplane), which already holds the
"Key Vault Secrets User" role on acb-msak-kvault.

Locally (developer laptop), DefaultAzureCredential falls back to `az login`
credentials, so `az login` once and this works without any code change --
just make sure your own account (or the identity you're testing as) can read
secrets from acb-msak-kvault, or set LOCAL_DEV_SECRETS=1 to skip Key Vault
entirely and read secrets from environment variables instead (fastest local
loop, e.g. against the throwaway Postgres used for migration testing).
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- non-secret config ---
    environment: str = "local"
    local_dev_secrets: bool = False

    key_vault_name: str = "acb-msak-kvault"
    # Client ID of acb-msak-iam-id-cplane -- set this as an env var on the
    # Container App/Job so DefaultAzureCredential picks the right
    # user-assigned identity (a Container App can have more than one).
    azure_client_id: str | None = None

    postgres_host: str = "acb-msak-postgre-sql.postgres.database.azure.com"
    postgres_port: int = 5432
    postgres_db: str = "postgres"
    postgres_app_user: str = "acb_app"

    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    # Local-disk stand-in for Blob Storage -- see app/core/storage.py's
    # module docstring for why this isn't Azure Blob (or even Azurite) yet.
    upload_dir: str = "/app/data/uploads"
    # 250 MiB -- sized for the real expected input, not a generic default:
    # multi-hundred-page architectural/structural PDF drawing sets (floor
    # plans, elevations, MEP sheets) commonly land in the 50-100 MiB range
    # per file, so 20 MiB (the original placeholder) would have rejected a
    # normal submission on day one. NOTE: the current upload endpoint reads
    # the whole file into memory (`await file.read()`) before writing it --
    # fine at this size for a single-instance dev/demo container, but a real
    # deployment handling concurrent 250 MiB uploads should stream straight
    # to Blob Storage in chunks instead of buffering the whole file in RAM
    # first. Flagging here rather than fixing now since it's a rewrite of
    # app/core/storage.py's interface, not a config change.
    max_upload_bytes: int = 250 * 1024 * 1024  # 250 MiB

    # Local-disk-uploads-style stand-in, this time for a transactional email
    # provider: there's no SMTP relay or Azure Communication Services/
    # SendGrid account wired up in this local docker-compose stack, so
    # app/core/mailer.py just logs the verification email's content and
    # hands the raw link straight back in the register API response instead
    # of "sending" it anywhere. That response field only exists while this
    # is True -- flip it off (and finish wiring a real provider, likely via
    # a Key-Vault-backed API key the same way jwt_secret_key is loaded
    # above) before this ever runs anywhere reachable by someone other than
    # the account holder themselves.
    local_dev_email_stub: bool = True
    email_verification_ttl_hours: int = 24

    # --- Sandbox Job Runs (O1-O9 subsystem) ---
    # Run-provenance token TTL -- see app/core/security.py create_run_token().
    # Generous relative to access_token_ttl_minutes (15 min) because it has
    # to outlive one whole run: file download + LLM call(s) + posting the
    # result, not one HTTP request.
    run_token_ttl_minutes: int = 60

    # ONE real Service Bus queue, one direction: control plane -> sandbox
    # worker (confirmed against the real namespace -- acb-msak-sbq-agent-runs
    # has a send-only SAS for the control plane and a listen-only SAS for
    # the sandbox worker; there is no second queue for a reverse direction).
    # It is also session-enabled ("requiresSession": true on the real
    # queue), which is NOT optional -- Service Bus refuses a plain
    # non-session send/receive against a session-enabled entity. See
    # app/core/queue.py and sandbox_worker/queue_client.py: every send
    # carries session_id=<run_id>, every receive asks for
    # NEXT_AVAILABLE_SESSION.
    #
    # Results/streaming do NOT come back over this queue or a second one --
    # per the target architecture, the sandbox worker publishes to Redis
    # (stream relay) and the control plane consumes from there. That Redis
    # wiring is not built yet (see DEFERRED_ITEMS.md) -- app/worker/
    # result_consumer.py is currently a stub for exactly this reason.
    service_bus_queue: str = "acb-msak-sbq-agent-runs"
    # How long a pull-loop receive blocks waiting for a session/message
    # before returning empty -- the sandbox worker's request-poll loop uses
    # this.
    service_bus_receive_wait_seconds: int = 20

    # --- Redis (stream relay + revocation signaling, per the target
    # architecture diagram) -- real resource, not a local stand-in:
    # acb-msak-redis.redis.cache.windows.net. Non-SSL access is disabled on
    # the real cache, so this is always the SSL port. NOT YET USED by any
    # code in this pass -- see DEFERRED_ITEMS.md; settings are wired ahead
    # of the actual client code landing next.
    redis_host: str = "acb-msak-redis.redis.cache.windows.net"
    redis_ssl_port: int = 6380

    # Admission control (O5) -- a hard per-user cap on runs that are queued
    # or actively running at once, enforced in app/api/v1/runs.py before a
    # new run is admitted. This is the real, testable half of O5; the other
    # half (a prewarmed worker pool to cut cold-start latency) is
    # deliberately NOT built -- see the deferred-items note delivered
    # alongside this build for why.
    max_concurrent_runs_per_user: int = 2

    # Honest-failure reaper (O7) -- a run whose worker died, was OOM-killed,
    # or otherwise vanished without ever posting a result should not sit in
    # "running" forever (or worse, be silently reinterpreted as success). A
    # run still "running" past this many minutes is flipped to a failed,
    # clearly-labeled infra timeout the next time that user hits the
    # admission-control check in runs.py. NOTE: that makes this a lazy,
    # request-triggered reaper, not a real background scheduler -- a stuck
    # run belonging to a user who never starts another run would never get
    # reaped. A real deployment wants a proper periodic job (a small
    # Container Apps Job on a timer trigger, or an asyncio background task
    # in the API process) instead. Flagged here rather than silently
    # papered over.
    run_timeout_minutes: int = 15

    # Local-dev stand-in for the real LLM call: rather than wire a real
    # model provider key into this throwaway docker-compose stack (and
    # rack up real cost on every smoke-test run), app/api/v1/runs.py's
    # internal LLM-broker endpoint returns a deterministic canned review
    # plus fabricated-but-plausible token/cost figures when this is True.
    # The real swap-in point, per the existing architecture already encoded
    # in app/db/models/run.py's docstrings, is APIM as the credential
    # broker in front of the actual model deployment, with its access logs
    # shipped to Event Hub and consumed into run_usage -- both explicitly
    # deferred to the Cloud-wiring step (step 15+), same DWG-style
    # documented-not-built pattern as everywhere else in this project.
    local_dev_llm_stub: bool = True

    # --- secret-backed fields, populated by load_secrets() below ---
    # NOTE on this one: "acb-msak-pg-app-password" does NOT exist in Key
    # Vault yet as of this pass -- only the admin password
    # (acb-msak-pg-admin-password) has ever been created. This is the name
    # this code expects once the acb_app runtime role + its password are
    # actually created for real (see the walkthrough delivered alongside
    # this change, and DEFERRED_ITEMS.md). Until then, real (non-local-dev)
    # startup will fail here with a 404 from Key Vault -- loudly, not
    # silently, which is correct until that secret is real.
    postgres_app_password: str = ""
    # NOT verified against a real secret name -- unlike Postgres/Service
    # Bus/Redis, no one has confirmed whether/where a JWT signing key was
    # ever provisioned in acb-msak-kvault. Same treatment: flagged rather
    # than assumed correct.
    jwt_secret_key: str = ""
    # Send-only SAS for the control plane's one allowed action against the
    # real queue (acb-msak-sbq-agent-runs): enqueueing a run request. This
    # identity cannot receive/listen at all -- confirmed by the real SAS
    # policy name (...-cplane-send).
    service_bus_send_connection_string: str = ""
    # The Redis primary key (acb-msak-redis-pkey) -- not yet consumed by any
    # code in this pass; see redis_host/redis_ssl_port above.
    redis_password: str = ""

    @property
    def redis_url(self) -> str:
        # rediss:// (double s) = TLS, required since the real cache has
        # non-SSL access disabled.
        return f"rediss://:{self.redis_password}@{self.redis_host}:{self.redis_ssl_port}/0"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_app_user}:"
            f"{self.postgres_app_password}@{self.postgres_host}:"
            f"{self.postgres_port}/{self.postgres_db}"
        )


def _load_secrets_from_keyvault(settings: Settings) -> None:
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    credential = DefaultAzureCredential(
        managed_identity_client_id=settings.azure_client_id
    )
    client = SecretClient(
        vault_url=f"https://{settings.key_vault_name}.vault.azure.net",
        credential=credential,
    )
    # "acb-msak-pg-app-password" does not exist yet -- see this field's
    # comment above and DEFERRED_ITEMS.md. This will 404 against the real
    # vault until that secret is created; that's correct, not a bug to
    # silence with a fallback.
    settings.postgres_app_password = client.get_secret("acb-msak-pg-app-password").value
    # Not a confirmed real secret name -- see this field's comment above.
    settings.jwt_secret_key = client.get_secret("jwt-signing-key").value
    settings.service_bus_send_connection_string = client.get_secret(
        "sas-arp-sbq-agent-runs-cplane-send-strb-pconnstr"
    ).value
    settings.redis_password = client.get_secret("acb-msak-redis-pkey").value


def _load_secrets_from_env(settings: Settings) -> None:
    import os

    settings.postgres_app_password = os.environ.get("POSTGRES_APP_PASSWORD", "")
    settings.jwt_secret_key = os.environ.get("JWT_SECRET_KEY", "")
    settings.service_bus_send_connection_string = os.environ.get(
        "SERVICE_BUS_SEND_CONNECTION_STRING", ""
    )
    settings.redis_password = os.environ.get("REDIS_PASSWORD", "")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.local_dev_secrets:
        _load_secrets_from_env(settings)
    else:
        _load_secrets_from_keyvault(settings)
    return settings
