# Single image serves three purposes, selected by the command it's run
# with -- no separate images to keep in sync:
#   1. The Control Plane API itself (Container App): default CMD below.
#   2. The one-shot schema-migration runner (manual-trigger Container Apps
#      Job): override the command to `alembic upgrade head` when creating
#      the Job (see the `az containerapp job create --command` example in
#      the accompanying notes).
#   3. The local Docker Compose `smoketest` service: overrides the
#      entrypoint to run scripts/smoke_test.py, which is why scripts/ is
#      copied in below alongside app/ and alembic/ -- it's not used by the
#      other two roles, but they all share this one image, so it comes
#      along for the ride (a few KB, no real cost).
# static/ is the Option A auth UI (plain HTML/CSS/JS) that app/main.py
# mounts and serves same-origin -- same reasoning as scripts/ above: only
# the API-server role actually serves it, but it's cheap to carry in every
# role's copy of this one image.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY scripts ./scripts
COPY static ./static

# Runs as a non-root user in both modes. /app/data/uploads is the local
# stand-in for Blob Storage (see app/core/storage.py) -- COPY above ran as
# root, so /app is root-owned by default; appuser needs write access to
# this one subtree, nowhere else.
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/data/uploads \
    && chown -R appuser:appuser /app/data
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
