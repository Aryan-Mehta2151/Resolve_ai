from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeCitation(BaseModel):
    document_id: UUID
    version_id: UUID
    title: str = Field(max_length=160)
    filename: str = Field(max_length=255)
    excerpt: str | None = Field(default=None, max_length=1200)


class KnowledgeAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=6000)
    citations: list[KnowledgeCitation] = Field(default_factory=list, max_length=10)


class KnowledgeVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version_number: int
    original_filename: str
    content_type: str
    byte_size: int
    status: str
    error_code: str | None
    created_at: datetime
    ingested_at: datetime | None


class KnowledgeDocumentResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    versions: list[KnowledgeVersionResponse]


class KnowledgeUploadResponse(BaseModel):
    document: KnowledgeDocumentResponse
    version: KnowledgeVersionResponse


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=1000)


class KnowledgeSearchResponse(KnowledgeAnswer):
    grounded: bool
