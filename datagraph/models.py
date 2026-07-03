from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    graph_id: str
    view_id: str | None
    type: str
    status: str
    params: dict[str, Any]
    progress: dict[str, Any]
    error_text: str | None
    input_refs: dict[str, Any]
    stats: dict[str, Any]
    created_at: str
    started_at: str | None
    completed_at: str | None


class HealthResponse(BaseModel):
    status: str


class MockEmbeddingRequest(BaseModel):
    texts: list[str]

