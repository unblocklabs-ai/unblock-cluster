from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, status

from datagraph.db import connect, fetch_one
from datagraph.runs.label import label_run_report

router = APIRouter(prefix="/api/graphs/{graph_id}/label-runs", tags=["labels"])


@router.get("/{run_id}/report")
async def get_label_run_report(
    request: Request,
    graph_id: str,
    run_id: str,
) -> dict:
    with connect(request.app.state.settings.db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT *
              FROM runs
             WHERE id = ? AND graph_id = ? AND type = 'label'
            """,
            (run_id, graph_id),
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="label run not found")
    try:
        report = label_run_report(request.app.state.settings.db_path, graph_id, run_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    stats = json.loads(row["stats_json"])
    return {
        "graphId": graph_id,
        "runId": run_id,
        "status": row["status"],
        "params": json.loads(row["params_json"]),
        "stats": stats,
        "tokenUsage": stats.get(
            "tokenUsage",
            {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0},
        ),
        **report,
        "reportNote": (
            "Representative blocks are recomputed from this run's recorded params "
            "and current assembly code; prompt inputs are not persisted."
        ),
    }
