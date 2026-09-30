import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import TimestampMixin, UUIDPkMixin


class UploadedFile(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "uploaded_files"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending_scan','clean','quarantined','failed')",
            name="ck_uploaded_files_status",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Control Plane API is the sole Blob Storage broker (managed identity,
    # never a shared account key) -- this is the tenant-scoped path it wrote
    # the blob to, e.g. users/{user_id}/{file_id}/{original_filename}.
    blob_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending_scan")
