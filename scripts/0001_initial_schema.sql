-- Hand-validated source of truth for Alembic migration 0001.
-- Run once by hand against the local throwaway Postgres to confirm every
-- statement is syntactically and semantically correct on real Postgres 16
-- (matching acb-msak-postgre-sql's engine version) before it gets wrapped
-- in op.execute() calls inside the actual Alembic revision file.

-- ============================================================
-- 1. Restricted runtime role. Migrations/admin work always run as
--    acbmsakadmin (the table owner); the app itself never does, so RLS
--    below is enforced without needing FORCE ROW LEVEL SECURITY.
-- ============================================================
-- \gexec runs outside dollar-quoting, so psql's client-side :'var'
-- substitution actually applies here (it is silently skipped inside a
-- DO $$ ... $$ body, which looked like a bug the first time this was
-- tested against the local throwaway DB and is why this form was chosen).
SELECT 'CREATE ROLE acb_app WITH LOGIN PASSWORD ' || quote_literal(:'acb_app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acb_app')
\gexec

GRANT CONNECT ON DATABASE :"dbname" TO acb_app;
GRANT USAGE ON SCHEMA public TO acb_app;

-- ============================================================
-- 2. Tables
-- ============================================================
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(320) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_email UNIQUE (email)
);
CREATE INDEX ix_users_email ON users (email);

CREATE TABLE refresh_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash VARCHAR(64) NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    replaced_by_id UUID REFERENCES refresh_tokens (id) ON DELETE SET NULL,
    user_agent VARCHAR(512),
    ip_address VARCHAR(64),
    CONSTRAINT uq_refresh_tokens_token_hash UNIQUE (token_hash)
);
CREATE INDEX ix_refresh_tokens_user_id ON refresh_tokens (user_id);
CREATE INDEX ix_refresh_tokens_expires_at ON refresh_tokens (expires_at);

CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_conversations_user_id ON conversations (user_id);

CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_messages_role CHECK (role IN ('user', 'assistant', 'system', 'tool'))
);
CREATE INDEX ix_messages_conversation_id ON messages (conversation_id);
CREATE INDEX ix_messages_user_id ON messages (user_id);

CREATE TABLE uploaded_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    blob_path VARCHAR(1024) NOT NULL,
    original_filename VARCHAR(255) NOT NULL,
    content_type VARCHAR(255) NOT NULL,
    size_bytes BIGINT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending_scan',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_uploaded_files_blob_path UNIQUE (blob_path),
    CONSTRAINT ck_uploaded_files_status
        CHECK (status IN ('pending_scan', 'clean', 'quarantined', 'failed'))
);
CREATE INDEX ix_uploaded_files_user_id ON uploaded_files (user_id);

CREATE TABLE agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations (id) ON DELETE SET NULL,
    uploaded_file_id UUID REFERENCES uploaded_files (id) ON DELETE SET NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    container_name VARCHAR(255),
    error_message VARCHAR(2000),
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT ck_agent_runs_status
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'))
);
CREATE INDEX ix_agent_runs_user_id ON agent_runs (user_id);
CREATE INDEX ix_agent_runs_conversation_id ON agent_runs (conversation_id);

CREATE TABLE run_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES agent_runs (id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(10, 4) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_run_usage_run_id ON run_usage (run_id);
CREATE INDEX ix_run_usage_user_id ON run_usage (user_id);

-- ============================================================
-- 3. Row-Level Security -- the hard multi-tenancy-isolation requirement.
--    Every user-owned table is scoped to the Postgres session variable
--    app.current_user_id, set per-request by the API layer inside the
--    same transaction (SET LOCAL, so it can never leak across requests
--    on a pooled connection). current_setting(..., true) returns NULL
--    instead of erroring when unset, so a request with no user context
--    sees zero rows -- fails closed, not open.
-- ============================================================
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON conversations
    USING (user_id = current_setting('app.current_user_id', true)::uuid);

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON messages
    USING (user_id = current_setting('app.current_user_id', true)::uuid);

ALTER TABLE uploaded_files ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON uploaded_files
    USING (user_id = current_setting('app.current_user_id', true)::uuid);

ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON agent_runs
    USING (user_id = current_setting('app.current_user_id', true)::uuid);

ALTER TABLE run_usage ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON run_usage
    USING (user_id = current_setting('app.current_user_id', true)::uuid);
-- (refresh_tokens intentionally excluded from this list -- see below)

-- refresh_tokens: deliberately NOT RLS-restricted, unlike the other
-- user-owned tables. RLS here would be actively wrong: /auth/refresh's
-- whole job is to look a token row up BY ITS HASH before any user_id
-- context exists (that hash lookup is what *establishes* identity) -- a
-- user_id-keyed RLS policy would hide that very row from the query meant
-- to find it, since current_setting(...) is NULL until after we already
-- know whose token it is. The raw refresh token's 256+ bits of entropy is
-- the actual access-control mechanism for this table: knowing a valid raw
-- token already proves the caller is that token's owner, the same trust
-- model refresh tokens use industry-wide. No endpoint lists refresh_tokens
-- in bulk without an explicit WHERE user_id = <already-authenticated user>,
-- so nothing here exposes another user's tokens regardless.

-- users: intentionally NOT row-level-restricted by user_id -- the API
-- needs to look up a row by email during login/registration before any
-- user_id context exists. Table-level GRANTs below are the actual control
-- here (acb_app gets exactly the columns/verbs it needs); no full-table
-- READ of other users' emails is exposed by any endpoint regardless of
-- what RLS would technically allow.

-- ============================================================
-- 4. Grants -- acb_app gets exactly what the app needs, nothing more.
-- ============================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON
    users, refresh_tokens, conversations, messages,
    uploaded_files, agent_runs, run_usage
    TO acb_app;
