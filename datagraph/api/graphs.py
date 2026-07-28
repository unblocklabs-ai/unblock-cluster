from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from datagraph.core.config import (
    ConfigValidationError,
    apply_config_patch,
    load_graph_config,
    validate_graph_config,
)
from datagraph.core.ids import new_id, now_iso
from datagraph.core.scope import ScopeValidationError, compile_scope
from datagraph.db import connect, fetch_all, fetch_one

router = APIRouter(prefix="/api/graphs", tags=["graphs"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_graph(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_body(body, {"name", "config"})
    name = _validate_name(body.get("name"))
    try:
        config = validate_graph_config(body.get("config", {}), require_text_fields=True)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc

    graph_id = new_id("grf")
    view_id = new_id("view")
    now = now_iso()
    with connect(request.app.state.settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO graphs (id, name, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (graph_id, name, json.dumps(config, sort_keys=True), now, now),
        )
        conn.execute(
            """
            INSERT INTO views (
              id, graph_id, name, description, scope_json,
              default_embedding_run_id, default_cluster_run_id, default_layout_run_id,
              default_label_run_id, default_trend_run_id, created_at, updated_at
            )
            VALUES (?, ?, 'all_records', NULL, '{}', NULL, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (view_id, graph_id, now, now),
        )
        conn.commit()
    return _get_graph_response(request.app.state.settings.db_path, graph_id)


@router.get("")
async def list_graphs(request: Request) -> dict[str, Any]:
    with connect(request.app.state.settings.db_path) as conn:
        rows = fetch_all(conn, "SELECT * FROM graphs ORDER BY created_at ASC, id ASC")
    return {"graphs": [_graph_summary(request.app.state.settings.db_path, row) for row in rows]}


@router.get("/{graph_id}")
async def get_graph(request: Request, graph_id: str) -> dict[str, Any]:
    return _get_graph_response(request.app.state.settings.db_path, graph_id)


@router.patch("/{graph_id}")
async def patch_graph(request: Request, graph_id: str, body: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_body(body, {"config"})
    if "config" not in body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "config", "message": "is required"}],
        )
    with connect(request.app.state.settings.db_path) as conn:
        row = fetch_one(conn, "SELECT * FROM graphs WHERE id = ?", (graph_id,))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")
        current_config = load_graph_config(row)
        try:
            config = apply_config_patch(current_config, body["config"])
        except ConfigValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=exc.errors,
            ) from exc
        conn.execute(
            "UPDATE graphs SET config_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(config, sort_keys=True), now_iso(), graph_id),
        )
        conn.commit()
    return _get_graph_response(request.app.state.settings.db_path, graph_id)


@router.delete("/{graph_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_graph(request: Request, graph_id: str) -> None:
    with connect(request.app.state.settings.db_path) as conn:
        external_space_ids = [
            row["embedding_space_id"]
            for row in conn.execute(
                """
                SELECT DISTINCT ecv.embedding_space_id
                  FROM external_datasets ed
                  JOIN external_chunk_versions ecv ON ecv.dataset_id = ed.id
                 WHERE ed.graph_id = ?
                """,
                (graph_id,),
            ).fetchall()
        ]
        cursor = conn.execute("DELETE FROM graphs WHERE id = ?", (graph_id,))
        for space_id in external_space_ids:
            still_referenced = conn.execute(
                "SELECT 1 FROM external_chunk_versions WHERE embedding_space_id = ? LIMIT 1",
                (space_id,),
            ).fetchone()
            if still_referenced is not None:
                continue
            conn.execute(
                "DELETE FROM embedding_vectors WHERE model = ?",
                (f"external/{space_id}",),
            )
            conn.execute("DELETE FROM embedding_spaces WHERE id = ?", (space_id,))
        conn.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")


def _get_graph_response(db_path: str, graph_id: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        row = fetch_one(conn, "SELECT * FROM graphs WHERE id = ?", (graph_id,))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")
    return _graph_summary(db_path, row, include_views=True)


def _graph_summary(db_path: str, row: dict, *, include_views: bool = False) -> dict[str, Any]:
    with connect(db_path) as conn:
        record_count = conn.execute(
            "SELECT COUNT(*) FROM records WHERE graph_id = ? AND is_active = 1",
            (row["id"],),
        ).fetchone()[0]
        view_rows = fetch_all(
            conn,
            "SELECT * FROM views WHERE graph_id = ? ORDER BY created_at ASC, id ASC",
            (row["id"],),
        )
    response: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "config": load_graph_config(row),
        "recordCount": record_count,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_views:
        response["views"] = [_view_summary(db_path, view) for view in view_rows]
    return response


def _view_summary(db_path: str, row: dict) -> dict[str, Any]:
    return {
        "id": row["id"],
        "graphId": row["graph_id"],
        "name": row["name"],
        "description": row["description"],
        "scope": json.loads(row["scope_json"]),
        "defaultEmbeddingRunId": row["default_embedding_run_id"],
        "defaultClusterRunId": row["default_cluster_run_id"],
        "defaultLayoutRunId": row["default_layout_run_id"],
        "defaultLabelRunId": row["default_label_run_id"],
        "defaultTrendRunId": row["default_trend_run_id"],
        "recordCount": _view_record_count(db_path, row["graph_id"], json.loads(row["scope_json"])),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _view_record_count(db_path: str, graph_id: str, scope: dict[str, Any]) -> int:
    try:
        where, params = compile_scope(scope, alias="r")
    except ScopeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc
    with connect(db_path) as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM records r WHERE r.graph_id = ? AND {where}",
            (graph_id, *params),
        ).fetchone()[0]


def _reject_unknown_body(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": key, "message": "unknown request key"} for key in unknown],
        )


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "name", "message": "is required and must be a non-empty string"}],
        )
    return value.strip()
