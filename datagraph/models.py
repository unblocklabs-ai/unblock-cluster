from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    graphId: str
    viewId: str | None
    type: str
    status: str
    params: dict[str, Any]
    progress: dict[str, Any]
    errorText: str | None
    inputRefs: dict[str, Any]
    stats: dict[str, Any]
    createdAt: str
    startedAt: str | None
    completedAt: str | None


class HealthResponse(BaseModel):
    status: str


class MockEmbeddingRequest(BaseModel):
    texts: list[str]
