from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, status

from datagraph.db import connect, fetch_one
from datagraph.models import RunResponse

router = APIRouter(prefix="/api/graphs/{graph_id}/runs", tags=["runs"])
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
DEFAULT_RUN_COLUMNS = (
    ("default_embedding_run_id", "defaultEmbeddingRunId"),
    ("default_cluster_run_id", "defaultClusterRunId"),
    ("default_layout_run_id", "defaultLayoutRunId"),
    ("default_label_run_id", "defaultLabelRunId"),
    ("default_trend_run_id", "defaultTrendRunId"),
)


def _to_response(row: dict) -> RunResponse:
    return RunResponse(
        id=row["id"],
        graphId=row["graph_id"],
        viewId=row["view_id"],
        type=row["type"],
        status=row["status"],
        params=json.loads(row["params_json"]),
        progress=json.loads(row["progress_json"]),
        errorText=row["error_text"],
        inputRefs=json.loads(row["input_refs_json"]),
        stats=json.loads(row["stats_json"]),
        createdAt=row["created_at"],
        startedAt=row["started_at"],
        completedAt=row["completed_at"],
    )


@router.get("", response_model=list[RunResponse])
async def list_runs(
    request: Request,
    graph_id: str,
    type: str | None = None,
    status: str | None = None,
) -> list[RunResponse]:
    rows = request.app.state.run_executor.list_runs(graph_id, run_type=type, status=status)
    return [_to_response(row) for row in rows]


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(request: Request, graph_id: str, run_id: str) -> RunResponse:
    row = request.app.state.run_executor.get_run(graph_id, run_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return _to_response(row)


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(request: Request, graph_id: str, run_id: str) -> RunResponse:
    row = request.app.state.run_executor.cancel_run(graph_id, run_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return _to_response(row)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(request: Request, graph_id: str, run_id: str) -> None:
    with connect(request.app.state.settings.db_path) as conn:
        row = fetch_one(
            conn,
            "SELECT * FROM runs WHERE graph_id = ? AND id = ?",
            (graph_id, run_id),
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        if row["status"] not in TERMINAL_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="run is not terminal; cancel queued/running runs before deleting",
            )
        default_ref = _default_run_reference(conn, graph_id, run_id)
        if default_ref is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"run is {default_ref['field']} for view {default_ref['viewName']}; "
                    "repoint defaults or delete the view first"
                ),
            )
        external_import = fetch_one(
            conn,
            "SELECT id FROM external_imports WHERE embedding_run_id = ?",
            (run_id,),
        )
        if external_import is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "external embedding runs are immutable import history; "
                    "delete the dataset graph to remove them"
                ),
            )
        conn.execute("DELETE FROM runs WHERE graph_id = ? AND id = ?", (graph_id, run_id))
        conn.commit()


def _default_run_reference(conn: object, graph_id: str, run_id: str) -> dict[str, str] | None:
    row = fetch_one(
        conn,
        """
        SELECT *
          FROM views
         WHERE graph_id = ?
           AND (
             default_embedding_run_id = ?
             OR default_cluster_run_id = ?
             OR default_layout_run_id = ?
             OR default_label_run_id = ?
             OR default_trend_run_id = ?
           )
         ORDER BY created_at ASC, id ASC
         LIMIT 1
        """,
        (graph_id, run_id, run_id, run_id, run_id, run_id),
    )
    if row is None:
        return None
    for column, field in DEFAULT_RUN_COLUMNS:
        if row[column] == run_id:
            return {"field": field, "viewName": row["name"]}
    return None
