-- Hand-validated source of truth for Alembic migration 0002, same discipline
-- as scripts/0001_initial_schema.sql: run once by hand against the local
-- throwaway Postgres before wrapping these statements in op.execute() calls
-- inside the actual Alembic revision file.
--
-- Adds email verification: a new user cannot sign in until they've followed
-- a one-time link proving they own the address they registered with.

-- ============================================================
-- 1. users.email_verified
-- ============================================================
-- DEFAULT TRUE at the column level, not FALSE -- this migration runs against
-- a database that may already have real user rows in it (it does, in the
-- local throwaway stack: every account created before this feature shipped).
-- A DEFAULT FALSE here would retroactively lock every existing account out
-- at their next login with no way back in, which is a much worse outcome
-- than "email verification wasn't required yet when you signed up".
-- Grandfather existing rows as verified; the application layer (not this
-- migration) is what makes NEW registrations start out unverified, by
-- passing email_verified=False explicitly when it inserts the row.
ALTER TABLE users ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT TRUE;

-- ============================================================
-- 2. email_verification_tokens
-- ============================================================
-- Same shape and reasoning as refresh_tokens: never store the raw token,
-- only a SHA-256 hash, so a DB leak can't be replayed as a valid link.
-- used_at (rather than deleting the row on use) keeps an audit trail and
-- lets a second click on the same link get a clear "already used" instead
-- of a confusing "not found".
CREATE TABLE email_verification_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash VARCHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_email_verification_tokens_token_hash UNIQUE (token_hash)
);
CREATE INDEX ix_email_verification_tokens_user_id ON email_verification_tokens (user_id);
CREATE INDEX ix_email_verification_tokens_expires_at ON email_verification_tokens (expires_at);

-- email_verification_tokens: deliberately NOT RLS-restricted, for the exact
-- same reason as refresh_tokens (see scripts/0001_initial_schema.sql) --
-- /auth/verify-email looks this row up BY ITS HASH before any user_id
-- context exists; that lookup is what *establishes* which user this is.
-- The raw token's entropy (not RLS) is the access-control mechanism here.

-- ============================================================
-- 3. Grants
-- ============================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON email_verification_tokens TO acb_app;
