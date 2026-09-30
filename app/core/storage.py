"""
Local-disk stand-in for the real Blob Storage broker.

The target architecture has the Control Plane API write uploads to Azure
Blob Storage via its own managed identity (acb-msak-iam-id-cplane), never a
shared account key, with the resulting blob_path recorded on the
uploaded_files row (see migration 0001 and app/db/models/file.py). Standing
up a real Blob Storage account -- or even Azurite -- is out of scope for
this throwaway local smoke-test stack, so this module writes to a local
directory inside the container instead, behind the same narrow interface
(save() takes raw bytes + a user id, returns the path to record) so that
swapping this out for an azure-storage-blob-backed implementation later is
a one-file change, not a rewrite of the API layer that calls it.

Deliberately NOT wired into docker-compose.yml as a named volume: this
whole stack is meant to be built, tested, and binned (see that file's own
top comment), so uploaded bytes are exactly as throwaway as everything
else here -- gone the moment `docker compose down` removes the container.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from app.config import get_settings


def _user_upload_dir(user_id: uuid.UUID) -> Path:
    settings = get_settings()
    directory = Path(settings.upload_dir) / str(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_upload(user_id: uuid.UUID, original_filename: str, data: bytes) -> str:
    """Writes `data` under a per-user directory using a random filename (never
    the client-supplied original_filename -- that string is untrusted and is
    kept only as metadata, never as a path component, so there's no path
    traversal surface here regardless of what a client sends as a filename).
    Returns the path to store in uploaded_files.blob_path.
    """
    directory = _user_upload_dir(user_id)
    stored_name = uuid.uuid4().hex
    target = directory / stored_name
    target.write_bytes(data)
    return str(target)


def read_upload(blob_path: str) -> bytes:
    """Reads back bytes previously written by save_upload(). Used by the
    sandbox-job file-download broker endpoint (app/api/v1/runs.py) -- the
    sandbox worker never gets a path or credential to this storage directly,
    it only ever gets bytes handed back through that authenticated endpoint.
    The real-Blob-Storage equivalent of this call is generating a short-lived
    read-only SAS URL and handing that back instead of proxying bytes through
    the API process; deferred along with the rest of the real Blob wiring
    (see this module's top docstring)."""
    return Path(blob_path).read_bytes()
