from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import TimestampMixin, UUIDPkMixin


class RefreshToken(UUIDPkMixin, Base):
    """
    A refresh token is never stored raw -- only a SHA-256 hash of it, so a
    Postgres dump/leak can't be replayed directly as a valid token. Rotation:
    every /auth/refresh issues a brand-new refresh token and marks the old
    row revoked_at + replaced_by_id, so reuse of an already-rotated token
    (a strong signal of theft) is detectable.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EmailVerificationToken(UUIDPkMixin, TimestampMixin, Base):
    """
    Same trust model as RefreshToken above: only a SHA-256 hash is ever
    stored, so a DB leak can't be replayed as a working verification link.
    `used_at` (rather than deleting the row once consumed) is what turns a
    second click on the same link into a clear "already used" instead of a
    confusing "not found" -- and, unlike refresh tokens, there's no rotation
    concept here: a verification token is single-use by design, not
    single-use-per-chain.
    """

    __tablename__ = "email_verification_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
