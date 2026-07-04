from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from datagraph.api.runs import _to_response
from datagraph.core.config import (
    DEFAULT_GRAPH_CONFIG,
    ConfigValidationError,
    apply_embedding_overrides,
    load_graph_config,
)
from datagraph.db import connect, fetch_one

router = APIRouter(prefix="/api/graphs/{graph_id}/embeddings", tags=["embeddings"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_embedding_run(
    request: Request,
    graph_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    overrides = body or {}
    _reject_unknown_body(
        overrides,
        set(DEFAULT_GRAPH_CONFIG["embedding"]) | {"representation", "includeJunk"},
    )
    representation = _validate_representation(overrides.get("representation", "raw"))
    include_junk = _validate_include_junk(overrides.get("includeJunk", False))
    embedding_overrides = {
        key: value for key, value in overrides.items() if key in DEFAULT_GRAPH_CONFIG["embedding"]
    }
    with connect(request.app.state.settings.db_path) as conn:
        graph = fetch_one(conn, "SELECT * FROM graphs WHERE id = ?", (graph_id,))
    if graph is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")
    config = load_graph_config(graph)
    try:
        merged = apply_embedding_overrides(config, embedding_overrides)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc

    summarize_run_id = None
    input_refs: dict[str, Any] = {}
    if representation == "summary":
        summarize_run_id = _latest_succeeded_summarize_run(
            request.app.state.settings.db_path,
            graph_id,
        )
        if summarize_run_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "summary representation requires a succeeded summarize run; "
                    "POST /api/graphs/{gid}/summarize first"
                ),
            )
        input_refs["summarizeRunId"] = summarize_run_id

    run_id = request.app.state.run_executor.enqueue_run(
        graph_id,
        run_type="embed",
        params={
            "embedding": merged["embedding"],
            "representation": representation,
            "includeJunk": include_junk,
            **({"summarizeRunId": summarize_run_id} if summarize_run_id is not None else {}),
        },
        input_refs=input_refs,
    )
    row = request.app.state.run_executor.get_run(graph_id, run_id)
    return _to_response(row).model_dump()


def _latest_succeeded_summarize_run(db_path: str, graph_id: str) -> str | None:
    with connect(db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT id
              FROM runs
             WHERE graph_id = ? AND type = 'summarize' AND status = 'succeeded'
             ORDER BY completed_at DESC, created_at DESC, id DESC
             LIMIT 1
            """,
            (graph_id,),
        )
    return row["id"] if row else None


def _validate_representation(value: Any) -> str:
    if value not in {"raw", "summary"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {"field": "representation", "message": 'must be "raw" or "summary"'}
            ],
        )
    return value


def _validate_include_junk(value: Any) -> bool:
    if not isinstance(value, bool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "includeJunk", "message": "must be a boolean"}],
        )
    return value


def _reject_unknown_body(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": key, "message": "unknown request key"} for key in unknown],
        )
