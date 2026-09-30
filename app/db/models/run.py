from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import TimestampMixin, UUIDPkMixin


class AgentRun(UUIDPkMixin, Base):
    """One row per sandbox Container Apps Job execution (one agent run)."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','succeeded','failed','cancelled')",
            name="ck_agent_runs_status",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    uploaded_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    # Container Apps Job execution name (acb-msak-acapps-env-sbxjb2's
    # per-run execution id), useful for correlating a row back to
    # Azure-side logs. Not populated yet -- nothing in this codebase talks
    # to that real Container Apps Job (its subnet, acb-msak-snet-acapps-
    # sbxjb2, isn't decommissioned/rebuilt yet); this column is written once
    # step 15+ actually deploys sandbox_worker/ there for real.
    container_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunUsage(UUIDPkMixin, TimestampMixin, Base):
    """
    Per-run token/cost accounting -- the landing table for the small
    Event Hub consumer that reads APIM's access logs (the credential-broker
    hot path) and turns them into per-run cost rows.
    """

    __tablename__ = "run_usage"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalized for the same reason as messages.user_id: flat RLS policy,
    # and cheap "my total spend" queries without joining through agent_runs.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)
