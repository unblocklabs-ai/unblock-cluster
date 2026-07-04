from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from datagraph.api.runs import _to_response
from datagraph.core.config import ConfigValidationError, apply_summarization_overrides
from datagraph.db import connect, fetch_one
from datagraph.runs.summarize import summarize_run_report

router = APIRouter(prefix="/api/graphs/{graph_id}", tags=["summarize"])


@router.post("/summarize", status_code=status.HTTP_201_CREATED)
async def create_summarize_run(
    request: Request,
    graph_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = body or {}
    _reject_unknown_body(body, {"summarization"})
    with connect(request.app.state.settings.db_path) as conn:
        graph = fetch_one(conn, "SELECT * FROM graphs WHERE id = ?", (graph_id,))
    if graph is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")
    config = json.loads(graph["config_json"])
    try:
        merged = apply_summarization_overrides(config, body.get("summarization", {}))
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc

    run_id = request.app.state.run_executor.enqueue_run(
        graph_id,
        run_type="summarize",
        params={
            "embedding": merged["embedding"],
            "summarization": merged["summarization"],
        },
    )
    row = request.app.state.run_executor.get_run(graph_id, run_id)
    return _to_response(row).model_dump()


@router.get("/summarize-runs/{run_id}/report")
async def get_summarize_run_report(
    request: Request,
    graph_id: str,
    run_id: str,
) -> dict[str, Any]:
    with connect(request.app.state.settings.db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT *
              FROM runs
             WHERE id = ? AND graph_id = ? AND type = 'summarize'
            """,
            (run_id, graph_id),
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="summarize run not found")
    report = summarize_run_report(request.app.state.settings.db_path, run_id)
    return {
        "graphId": graph_id,
        "runId": run_id,
        "status": row["status"],
        "params": json.loads(row["params_json"]),
        "stats": json.loads(row["stats_json"]),
        **report,
    }


def _reject_unknown_body(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": key, "message": "unknown request key"} for key in unknown],
        )
