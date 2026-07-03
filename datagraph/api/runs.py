from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, status

from datagraph.models import RunResponse

router = APIRouter(prefix="/api/graphs/{graph_id}/runs", tags=["runs"])


def _to_response(row: dict) -> RunResponse:
    return RunResponse(
        id=row["id"],
        graph_id=row["graph_id"],
        view_id=row["view_id"],
        type=row["type"],
        status=row["status"],
        params=json.loads(row["params_json"]),
        progress=json.loads(row["progress_json"]),
        error_text=row["error_text"],
        input_refs=json.loads(row["input_refs_json"]),
        stats=json.loads(row["stats_json"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
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

