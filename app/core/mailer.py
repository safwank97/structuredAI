"""
Transactional email -- currently a placeholder, exactly like
app/core/storage.py stands in for real Blob Storage.

There's no email provider account wired up for this local docker-compose
stack (no SMTP relay, no Azure Communication Services / SendGrid API key),
so `settings.local_dev_email_stub` (see app/config.py) gates a stand-in that
just logs the message a real send would contain. The caller (app/api/v1/
auth.py's register()) also hands the raw verification link back in the API
response itself while that flag is on, purely so this can be exercised
end-to-end (by a human clicking it, or by scripts/smoke_test.py) without a
real inbox to check. That response field -- not this module -- is the part
that MUST be switched off before this is reachable by anyone other than the
account holder; this module's job is only ever to attempt sending.

Wiring a real provider later means replacing the body of `send_verification_
email` with an actual API call, using a secret pulled from Key Vault the
same way jwt_secret_key is loaded in app/config.py -- the function signature
here is written to make that swap a one-function change, not a call-site
rewrite.
"""
import logging

from app.config import get_settings

logger = logging.getLogger("app.mailer")


def send_verification_email(to_email: str, display_name: str, verification_url: str) -> None:
    settings = get_settings()
    if settings.local_dev_email_stub:
        # Printed (not just logged at DEBUG) so it's visible in plain
        # `docker compose logs api` output without adjusting log levels --
        # this is the only place a human can actually see the link in this
        # local stack short of querying Postgres directly.
        print(
            "\n"
            "==================== [local-dev email stub] ====================\n"
            f"To:      {to_email}\n"
            f"Subject: Confirm your Construction Plan Review account\n"
            f"\n"
            f"Hi {display_name},\n"
            f"\n"
            f"Confirm your email to start using Construction Plan Review:\n"
            f"  {verification_url}\n"
            f"\n"
            f"This link expires in {settings.email_verification_ttl_hours} hours "
            f"and can only be used once.\n"
            "==================================================================\n"
        )
        return

    # No real provider wired up yet -- fail loudly rather than silently
    # pretending an email went out when local_dev_email_stub is turned off
    # without anything having replaced this branch.
    raise NotImplementedError(
        "local_dev_email_stub is False but no real email provider is configured "
        "in app/core/mailer.py yet"
    )
