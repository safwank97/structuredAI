# Deliberately a separate, minimal image from the control plane's root
# Dockerfile -- this is meant to be the actual container that eventually
# runs inside acb-msak-acapps-env-sbxjb2. It carries none of the control
# plane's code (no FastAPI, no SQLAlchemy, no DB models, no Key Vault
# client): see queue_client.py's module docstring for why that separation
# is the point, not an oversight.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /worker

COPY sandbox_worker/requirements.txt .
RUN pip install -r requirements.txt

COPY sandbox_worker ./sandbox_worker

RUN useradd --create-home --shell /bin/bash sandboxuser
USER sandboxuser

CMD ["python", "-m", "sandbox_worker.worker"]
