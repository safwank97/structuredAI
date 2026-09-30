import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    # Optional on purpose: the UI's "New chat" button posts an empty body,
    # and the conversation gets a title later (either the user renames it,
    # or a future summarization step fills one in) -- same pattern as most
    # chat products, where a thread exists before it has a name.
    title: str | None = Field(default=None, max_length=255)


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationRename(BaseModel):
    # Required, unlike ConversationCreate.title -- this endpoint's whole job
    # is setting the title, so an empty string is a valid, meaningful input
    # (see the endpoint: it clears the title back to None/"Untitled
    # project"), not "field omitted."
    title: str = Field(max_length=255)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class UploadedFileOut(BaseModel):
    id: uuid.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
