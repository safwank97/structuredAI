"""initial schema: users, auth, conversations, messages, files, runs, usage
   + restricted acb_app role + row-level security for tenant isolation

Revision ID: 0001
Revises:
Create Date: 2026-09-27

This migration's DDL was hand-validated against a real local Postgres 16
instance (matching acb-msak-postgre-sql's engine version) before being
transcribed here, including a live test proving RLS actually isolates two
different users' rows and fails closed (zero rows) when no session context
is set. See scripts/0001_initial_schema.sql for the exact validated SQL this
migration executes verbatim via op.execute().
"""
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    acb_app_password = os.environ.get("ACB_APP_ROLE_PASSWORD")
    if not acb_app_password:
        raise RuntimeError(
            "ACB_APP_ROLE_PASSWORD env var is required to run this migration "
            "(the password the restricted runtime role 'acb_app' gets -- "
            "generate one, store it in Key Vault as 'acb-msak-pg-app-password' "
            "(matching the acb-msak-pg-admin-password naming convention), "
            "and pass it to whatever runs `alembic upgrade head`)."
        )

    bind = op.get_bind()
    dbname = bind.engine.url.database

    # psql's \gexec (used in scripts/0001_initial_schema.sql to hand-validate
    # this exact DDL) has no Alembic/SQLAlchemy equivalent -- same
    # conditional-create effect done directly here instead: check, then
    # create only if missing, so re-running against an environment that
    # already has the role is a safe no-op.
    #
    # IMPORTANT: CREATE ROLE ... PASSWORD is DDL, and Postgres's grammar for
    # that clause requires a literal string token right there in the
    # statement -- it does NOT accept a protocol-level bind parameter ($1)
    # in that position the way an INSERT/UPDATE/SELECT would. An earlier
    # version of this migration passed the password via .bindparams(), which
    # asyncpg sends as a real parameter -- Postgres rejected it outright
    # ("syntax error at or near $1"), caught only once this was actually run
    # against a live database rather than just syntax-checked. Fix: fetch a
    # properly-escaped literal from Postgres itself via quote_literal() (a
    # normal function call, which *can* take a bind parameter fine since
    # it's plain SELECT, not DDL), then splice that already-safe literal
    # into the CREATE ROLE text -- exactly what \gexec does implicitly in
    # the hand-validated SQL script.
    role_missing = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'acb_app'")
    ).first()
    if role_missing is None:
        quoted_password = bind.execute(
            sa.text("SELECT quote_literal(:pw)"), {"pw": acb_app_password}
        ).scalar_one()
        bind.execute(sa.text(f"CREATE ROLE acb_app WITH LOGIN PASSWORD {quoted_password}"))

    op.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO acb_app')
    op.execute("GRANT USAGE ON SCHEMA public TO acb_app")

    op.create_table(
        "users",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"])

    op.create_table(
        "conversations",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    op.create_table(
        "messages",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("conversation_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role in ('user','assistant','system','tool')", name="ck_messages_role"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_user_id", "messages", ["user_id"])

    op.create_table(
        "uploaded_files",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("blob_path", sa.String(1024), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending_scan"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("blob_path", name="uq_uploaded_files_blob_path"),
        sa.CheckConstraint(
            "status in ('pending_scan','clean','quarantined','failed')",
            name="ck_uploaded_files_status",
        ),
    )
    op.create_index("ix_uploaded_files_user_id", "uploaded_files", ["user_id"])

    op.create_table(
        "agent_runs",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("uploaded_file_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("uploaded_files.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("container_name", sa.String(255), nullable=True),
        sa.Column("error_message", sa.String(2000), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('queued','running','succeeded','failed','cancelled')",
            name="ck_agent_runs_status",
        ),
    )
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_conversation_id", "agent_runs", ["conversation_id"])

    op.create_table(
        "run_usage",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_run_usage_run_id", "run_usage", ["run_id"])
    op.create_index("ix_run_usage_user_id", "run_usage", ["user_id"])

    # --- Row-Level Security: the hard multi-tenancy isolation requirement ---
    # refresh_tokens is deliberately excluded: /auth/refresh must look a row
    # up BY ITS HASH before any user_id context exists (that lookup is what
    # *establishes* identity), so a user_id-keyed policy would hide the very
    # row the query needs to find. See scripts/0001_initial_schema.sql for
    # the full reasoning -- this was caught and fixed while building
    # app/api/v1/auth.py, not part of the original hand-validated draft.
    for table in ("conversations", "messages", "uploaded_files", "agent_runs", "run_usage"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (user_id = current_setting('app.current_user_id', true)::uuid)"
        )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "users, refresh_tokens, conversations, messages, "
        "uploaded_files, agent_runs, run_usage TO acb_app"
    )


def downgrade() -> None:
    for table in ("run_usage", "agent_runs", "uploaded_files", "messages", "conversations"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_table("run_usage")
    op.drop_table("agent_runs")
    op.drop_table("uploaded_files")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
    op.execute("REVOKE ALL ON SCHEMA public FROM acb_app")
    op.execute("DROP ROLE IF EXISTS acb_app")
