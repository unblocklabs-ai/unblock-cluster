from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from datagraph.api.runs import _to_response
from datagraph.core.config import ConfigValidationError, apply_embedding_overrides
from datagraph.db import connect, fetch_one

router = APIRouter(prefix="/api/graphs/{graph_id}/embeddings", tags=["embeddings"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_embedding_run(
    request: Request,
    graph_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    overrides = body or {}
    with connect(request.app.state.settings.db_path) as conn:
        graph = fetch_one(conn, "SELECT * FROM graphs WHERE id = ?", (graph_id,))
    if graph is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")
    config = json.loads(graph["config_json"])
    try:
        merged = apply_embedding_overrides(config, overrides)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc

    run_id = request.app.state.run_executor.enqueue_run(
        graph_id,
        run_type="embed",
        params={"embedding": merged["embedding"]},
    )
    row = request.app.state.run_executor.get_run(graph_id, run_id)
    return _to_response(row).model_dump()
