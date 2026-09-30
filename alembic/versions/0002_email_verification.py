"""add users.email_verified + email_verification_tokens

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29

This migration's DDL was hand-validated against a real local Postgres 16
instance before being transcribed here, including a live INSERT/UPDATE walk
through the whole register -> issue token -> verify -> reject-reuse sequence
as the restricted acb_app role. See scripts/0002_email_verification.sql for
the exact validated SQL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # DEFAULT TRUE, not FALSE -- see scripts/0002_email_verification.sql for
    # why: this migration may run against a database that already has real
    # user rows (accounts created before this feature shipped), and a
    # DEFAULT FALSE would retroactively lock every one of them out. The
    # application layer is what makes *new* registrations start out
    # unverified, by passing email_verified=False explicitly on insert --
    # this column's default only grandfathers whatever already exists.
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean, nullable=False, server_default=sa.true()),
    )

    op.create_table(
        "email_verification_tokens",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                   sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("token_hash", name="uq_email_verification_tokens_token_hash"),
    )
    op.create_index(
        "ix_email_verification_tokens_user_id", "email_verification_tokens", ["user_id"]
    )
    op.create_index(
        "ix_email_verification_tokens_expires_at", "email_verification_tokens", ["expires_at"]
    )

    # Deliberately NOT added to RLS's tenant_isolation loop -- same reasoning
    # as refresh_tokens (migration 0001): this table is looked up BY ITS
    # HASH before any user_id context exists, since that lookup is what
    # establishes which user this is in the first place.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON email_verification_tokens TO acb_app")


def downgrade() -> None:
    op.drop_table("email_verification_tokens")
    op.drop_column("users", "email_verified")
