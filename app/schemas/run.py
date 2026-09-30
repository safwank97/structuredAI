import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class RunCreate(BaseModel):
    uploaded_file_id: uuid.UUID
    # The optional chat-prompt question accompanying the uploaded drawing --
    # step (c) of the four-step pipeline (upload -> parse -> LLM call,
    # optionally with a user question -> write result to chat). Blank is
    # valid: "just review this drawing" with no specific question is a
    # normal request.
    question: str = Field(default="", max_length=4000)


class RunOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID | None
    uploaded_file_id: uuid.UUID | None
    status: str
    error_message: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class RunUsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float

    model_config = {"from_attributes": True}


# --- internal broker schemas (sandbox worker <-> control plane only) ---


class BrokerFileOut(BaseModel):
    """Response shape for GET /internal/runs/{run_id}/file. Bytes are
    base64-encoded so this can be a normal JSON response like every other
    endpoint in this API rather than introducing a second response
    convention just for this one broker call."""

    original_filename: str
    content_type: str
    data_base64: str


class BrokerLlmRequest(BaseModel):
    question: str = Field(default="", max_length=4000)
    # Plain text extracted from the file by the sandbox worker (e.g. a DXF's
    # entity summary) -- the control plane never parses the file itself, it
    # only brokers the LLM call. Bounded generously but not unbounded, so a
    # worker bug can't turn this into an unbounded-body DoS against its own
    # control plane.
    file_excerpt: str = Field(default="", max_length=100_000)
    original_filename: str = ""


class BrokerLlmResponse(BaseModel):
    review_text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
