from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from datagraph.api.facets import (
    facet_counts_by_cluster,
    summarize_run_id_for_cluster_run,
    validate_facet_by,
)
from datagraph.api.records import include_normalized, list_records_for_graph
from datagraph.api.runs import _to_response
from datagraph.api.warnings import resolved_run_warnings
from datagraph.core.config import (
    ConfigValidationError,
    apply_cluster_overrides,
    apply_labeling_overrides,
    apply_layout_overrides,
    apply_time_overrides,
    load_graph_config,
)
from datagraph.core.ids import new_id, now_iso
from datagraph.core.scope import ScopeValidationError, compile_scope, validate_scope
from datagraph.core.time import TimestampValidationError, parse_timestamp
from datagraph.core.trend_math import bucket_start
from datagraph.db import connect, fetch_all, fetch_one

router = APIRouter(prefix="/api/graphs/{graph_id}/views", tags=["views"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_view(request: Request, graph_id: str, body: dict[str, Any]) -> dict[str, Any]:
    _ensure_graph(request.app.state.settings.db_path, graph_id)
    _reject_unknown_body(body, {"name", "description", "scope"})
    name = _validate_name(body.get("name"))
    description = body.get("description")
    if description is not None and not isinstance(description, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "description", "message": "must be a string when provided"}],
        )
    if "scope" not in body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "scope", "message": "is required"}],
        )
    try:
        scope = validate_scope(body["scope"])
    except ScopeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc

    now = now_iso()
    view_id = new_id("view")
    try:
        with connect(request.app.state.settings.db_path) as conn:
            conn.execute(
                """
                INSERT INTO views (
                  id, graph_id, name, description, scope_json,
                  default_embedding_run_id, default_cluster_run_id, default_layout_run_id,
                  default_label_run_id, default_trend_run_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (view_id, graph_id, name, description, json.dumps(scope, sort_keys=True), now, now),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="view name already exists",
        ) from exc
    return _get_view_response(request.app.state.settings.db_path, graph_id, view_id)


@router.get("")
async def list_views(request: Request, graph_id: str) -> dict[str, Any]:
    _ensure_graph(request.app.state.settings.db_path, graph_id)
    with connect(request.app.state.settings.db_path) as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM views WHERE graph_id = ? ORDER BY created_at ASC, id ASC",
            (graph_id,),
        )
    return {"views": [_view_response(request.app.state.settings.db_path, row) for row in rows]}


@router.get("/{view_id}")
async def get_view(request: Request, graph_id: str, view_id: str) -> dict[str, Any]:
    return _get_view_response(request.app.state.settings.db_path, graph_id, view_id)


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_view(request: Request, graph_id: str, view_id: str) -> None:
    view = _get_view_row(request.app.state.settings.db_path, graph_id, view_id)
    if view["name"] == "all_records":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="all_records view cannot be deleted; delete the graph instead",
        )
    with connect(request.app.state.settings.db_path) as conn:
        run_rows = fetch_all(
            conn,
            "SELECT id FROM runs WHERE graph_id = ? AND view_id = ? ORDER BY created_at ASC",
            (graph_id, view_id),
        )
        run_ids = [row["id"] for row in run_rows]
        conn.execute("DELETE FROM views WHERE graph_id = ? AND id = ?", (graph_id, view_id))
        if run_ids:
            placeholders = ", ".join("?" for _ in run_ids)
            conn.execute(
                f"DELETE FROM runs WHERE graph_id = ? AND id IN ({placeholders})",
                (graph_id, *run_ids),
            )
        conn.commit()


@router.get("/{view_id}/records")
async def list_view_records(
    request: Request,
    graph_id: str,
    view_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    include: str | None = None,
    sourceType: str | None = None,
    product: str | None = None,
    sentiment: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    row = _get_view_row(request.app.state.settings.db_path, graph_id, view_id)
    try:
        where, params = compile_scope(json.loads(row["scope_json"]), alias="r")
    except ScopeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc
    return list_records_for_graph(
        request.app.state.settings.db_path,
        graph_id,
        limit=limit,
        offset=offset,
        include_normalized=include_normalized(include),
        filters={
            "sourceType": sourceType,
            "product": product,
            "sentiment": sentiment,
            "start": start,
            "end": end,
        },
        extra_where=where,
        extra_params=params,
    )


@router.post("/{view_id}/cluster", status_code=status.HTTP_201_CREATED)
async def create_cluster_run(
    request: Request,
    graph_id: str,
    view_id: str,
    body: dict[str, Any] | None = None,
    set_default_param: bool | None = Query(default=None, alias="setDefault"),
) -> dict[str, Any]:
    body = body or {}
    _reject_unknown_body(body, {"embeddingRunId", "cluster", "setDefault", "focus"})
    graph = _get_graph_row(request.app.state.settings.db_path, graph_id)
    _get_view_row(request.app.state.settings.db_path, graph_id, view_id)
    focus = _resolve_focus(
        request.app.state.settings.db_path,
        graph_id,
        view_id,
        body.get("focus"),
    )
    if focus is not None and (body.get("setDefault") is True or set_default_param is True):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {
                    "field": "setDefault",
                    "message": (
                        "focus drill-down cluster runs cannot become the view default; "
                        "their population is a subset of the full view artifact/layout"
                    ),
                }
            ],
        )
    set_default = False if focus is not None else _set_default_from_request(body, set_default_param)
    embedding_run_id = body.get("embeddingRunId") or _latest_succeeded_embedding_run(
        request.app.state.settings.db_path,
        graph_id,
    )
    if embedding_run_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="view has no succeeded embedding run; POST /api/graphs/{gid}/embeddings first",
        )
    config = load_graph_config(graph)
    try:
        merged = apply_cluster_overrides(config, body.get("cluster", {}))
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc
    if merged["cluster"]["space"]["method"] == "none":
        population = (
            _focus_view_member_count(
                request.app.state.settings.db_path,
                graph_id,
                view_id,
                focus,
            )
            if focus is not None
            else _view_record_count(request.app.state.settings.db_path, graph_id, view_id)
        )
        validate_none_space_population(population)
    input_refs = {"embeddingRunId": embedding_run_id, "viewId": view_id}
    if focus is not None:
        input_refs["focusClusterRunId"] = focus["clusterRunId"]
        input_refs["focusClusterId"] = focus["clusterId"]
    run_id = request.app.state.run_executor.enqueue_run(
        graph_id,
        run_type="cluster",
        view_id=view_id,
        params={
            "embeddingRunId": embedding_run_id,
            "cluster": merged["cluster"],
            "setDefault": set_default,
            **({"focus": focus} if focus is not None else {}),
        },
        input_refs=input_refs,
    )
    row = request.app.state.run_executor.get_run(graph_id, run_id)
    return _to_response(row).model_dump()


@router.post("/{view_id}/layout", status_code=status.HTTP_201_CREATED)
async def create_layout_run(
    request: Request,
    graph_id: str,
    view_id: str,
    body: dict[str, Any] | None = None,
    set_default_param: bool | None = Query(default=None, alias="setDefault"),
) -> dict[str, Any]:
    body = body or {}
    _reject_unknown_body(body, {"embeddingRunId", "layout", "setDefault"})
    set_default = _set_default_from_request(body, set_default_param)
    graph = _get_graph_row(request.app.state.settings.db_path, graph_id)
    _get_view_row(request.app.state.settings.db_path, graph_id, view_id)
    embedding_run_id = body.get("embeddingRunId") or _latest_succeeded_embedding_run(
        request.app.state.settings.db_path,
        graph_id,
    )
    if embedding_run_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="view has no succeeded embedding run; POST /api/graphs/{gid}/embeddings first",
        )
    config = load_graph_config(graph)
    try:
        merged = apply_layout_overrides(config, body.get("layout", {}))
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc
    run_id = request.app.state.run_executor.enqueue_run(
        graph_id,
        run_type="layout",
        view_id=view_id,
        params={
            "embeddingRunId": embedding_run_id,
            "layout": {**merged["layout"], "seed": merged["cluster"]["seed"]},
            "setDefault": set_default,
        },
        input_refs={"embeddingRunId": embedding_run_id, "viewId": view_id},
    )
    row = request.app.state.run_executor.get_run(graph_id, run_id)
    return _to_response(row).model_dump()


@router.post("/{view_id}/label", status_code=status.HTTP_201_CREATED)
async def create_label_run(
    request: Request,
    graph_id: str,
    view_id: str,
    body: dict[str, Any] | None = None,
    set_default_param: bool | None = Query(default=None, alias="setDefault"),
) -> dict[str, Any]:
    body = body or {}
    _reject_unknown_body(body, {"clusterRunId", "clusterIds", "labeling", "setDefault"})
    set_default = _set_default_from_request(body, set_default_param)
    graph = _get_graph_row(request.app.state.settings.db_path, graph_id)
    cluster_run = _resolve_cluster_run(
        request.app.state.settings.db_path,
        graph_id,
        view_id,
        body.get("clusterRunId"),
    )
    cluster_ids = _validate_cluster_ids(body.get("clusterIds"))
    config = load_graph_config(graph)
    try:
        merged = apply_labeling_overrides(config, body.get("labeling", {}))
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc
    run_id = request.app.state.run_executor.enqueue_run(
        graph_id,
        run_type="label",
        view_id=view_id,
        params={
            "clusterRunId": cluster_run["id"],
            "clusterIds": cluster_ids,
            "labeling": merged["labeling"],
            "setDefault": set_default,
        },
        input_refs={"clusterRunId": cluster_run["id"], "viewId": view_id},
    )
    row = request.app.state.run_executor.get_run(graph_id, run_id)
    return _to_response(row).model_dump()


@router.post("/{view_id}/trends", status_code=status.HTTP_201_CREATED)
async def create_trend_run(
    request: Request,
    graph_id: str,
    view_id: str,
    body: dict[str, Any] | None = None,
    set_default_param: bool | None = Query(default=None, alias="setDefault"),
) -> dict[str, Any]:
    body = body or {}
    _reject_unknown_body(body, {"clusterRunId", "window", "time", "setDefault"})
    set_default = _set_default_from_request(body, set_default_param)
    graph = _get_graph_row(request.app.state.settings.db_path, graph_id)
    cluster_run = _resolve_cluster_run(
        request.app.state.settings.db_path,
        graph_id,
        view_id,
        body.get("clusterRunId"),
    )
    config = load_graph_config(graph)
    try:
        merged = apply_time_overrides(config, body.get("time", {}))
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc
    window = _validate_trend_window(body.get("window"), bucket=merged["time"]["bucket"])
    run_id = request.app.state.run_executor.enqueue_run(
        graph_id,
        run_type="trend",
        view_id=view_id,
        params={
            "clusterRunId": cluster_run["id"],
            "time": merged["time"],
            "window": window,
            "setDefault": set_default,
        },
        input_refs={"clusterRunId": cluster_run["id"], "viewId": view_id},
    )
    row = request.app.state.run_executor.get_run(graph_id, run_id)
    return _to_response(row).model_dump()


@router.get("/{view_id}/trends")
async def get_trends(
    request: Request,
    graph_id: str,
    view_id: str,
    trendRunId: str | None = None,
) -> dict[str, Any]:
    run = _resolve_trend_run(
        request.app.state.settings.db_path,
        graph_id,
        view_id,
        trendRunId,
    )
    refs = json.loads(run["input_refs_json"])
    cluster_run_id = refs["clusterRunId"]
    with connect(request.app.state.settings.db_path) as conn:
        summary_row = fetch_one(
            conn,
            "SELECT summary_json FROM trend_summaries WHERE run_id = ?",
            (run["id"],),
        )
        rows = fetch_all(
            conn,
            """
            SELECT *
              FROM trend_results
             WHERE run_id = ?
             ORDER BY cluster_id ASC, bucket_start ASC
            """,
            (run["id"],),
        )
        label_text = _latest_label_text_by_cluster(conn, cluster_run_id)
    if summary_row is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="trend results missing")
    summary = json.loads(summary_row["summary_json"])
    series: list[dict[str, Any]] = []
    current_cluster_id: int | None = None
    current: dict[str, Any] | None = None
    for row in rows:
        cluster_id = int(row["cluster_id"])
        if cluster_id != current_cluster_id:
            current = {
                "clusterId": cluster_id,
                "label": label_text.get(cluster_id),
                "buckets": [],
            }
            series.append(current)
            current_cluster_id = cluster_id
        assert current is not None
        current["buckets"].append(
            {
                "bucketStart": row["bucket_start"],
                "count": row["count"],
                "share": row["share"],
                "spikeScore": row["spike_score"],
            }
        )
    return {
        "trendRunId": run["id"],
        "clusterRunId": cluster_run_id,
        "bucket": summary["bucket"],
        "window": summary["window"],
        "summary": summary,
        "series": series,
    }


@router.get("/{view_id}/topics")
async def list_topics(
    request: Request,
    graph_id: str,
    view_id: str,
    clusterRunId: str | None = None,
    facetBy: str | None = None,
) -> dict[str, Any]:
    facet_by = validate_facet_by(facetBy)
    run = _resolve_cluster_run(
        request.app.state.settings.db_path,
        graph_id,
        view_id,
        clusterRunId,
    )
    run_id = run["id"]
    with connect(request.app.state.settings.db_path) as conn:
        rows = fetch_all(
            conn,
            """
            SELECT *
              FROM cluster_summaries
             WHERE run_id = ?
             ORDER BY size DESC, cluster_id ASC
            """,
            (run_id,),
        )
        labels = _latest_labels_by_cluster(conn, run_id)
        trends = _trend_snapshots_by_cluster(conn, graph_id, view_id, run_id)
        facets = facet_counts_by_cluster(
            conn,
            run_id,
            facet_by,
            cluster_ids=[int(row["cluster_id"]) for row in rows],
            summarize_run_id=summarize_run_id_for_cluster_run(conn, run),
        )
        warnings = resolved_run_warnings(
            conn,
            graph_id=graph_id,
            view_id=view_id,
            cluster_run_id=run_id,
        )
    refs = json.loads(run["input_refs_json"])
    stats = json.loads(run["stats_json"])
    return {
        "clusterRunId": run_id,
        "embeddingRunId": refs.get("embeddingRunId"),
        "noise": {
            "noiseCount": stats.get("noiseCount", 0),
            "noiseRatio": stats.get("noiseRatio", 0),
        },
        "warnings": warnings,
        "topics": [
            _topic_response(
                row,
                labels.get(row["cluster_id"]),
                trends.get(row["cluster_id"]),
                facets.get(int(row["cluster_id"])) if facet_by else None,
            )
            for row in rows
        ],
    }


@router.get("/{view_id}/topics/{cluster_id}")
async def get_topic(
    request: Request,
    graph_id: str,
    view_id: str,
    cluster_id: int,
    clusterRunId: str | None = None,
    facetBy: str | None = None,
) -> dict[str, Any]:
    facet_by = validate_facet_by(facetBy)
    run = _resolve_cluster_run(
        request.app.state.settings.db_path,
        graph_id,
        view_id,
        clusterRunId,
    )
    run_id = run["id"]
    with connect(request.app.state.settings.db_path) as conn:
        row = fetch_one(
            conn,
            "SELECT * FROM cluster_summaries WHERE run_id = ? AND cluster_id = ?",
            (run_id, cluster_id),
        )
        labels = _latest_labels_by_cluster(conn, run_id)
        trends = _trend_snapshots_by_cluster(conn, graph_id, view_id, run_id)
        facets = facet_counts_by_cluster(
            conn,
            run_id,
            facet_by,
            cluster_ids=[cluster_id],
            summarize_run_id=summarize_run_id_for_cluster_run(conn, run),
        )
        warnings = resolved_run_warnings(
            conn,
            graph_id=graph_id,
            view_id=view_id,
            cluster_run_id=run_id,
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="topic not found")
    refs = json.loads(run["input_refs_json"])
    return {
        "clusterRunId": run_id,
        "embeddingRunId": refs.get("embeddingRunId"),
        "warnings": warnings,
        "topic": _topic_response(
            row,
            labels.get(cluster_id),
            trends.get(cluster_id),
            facets.get(cluster_id) if facet_by else None,
        ),
    }


@router.get("/{view_id}/topics/{cluster_id}/records")
async def list_topic_records(
    request: Request,
    graph_id: str,
    view_id: str,
    cluster_id: int,
    clusterRunId: str | None = None,
    topK: int = Query(default=12, ge=1, le=50),
) -> dict[str, Any]:
    run = _resolve_cluster_run(
        request.app.state.settings.db_path,
        graph_id,
        view_id,
        clusterRunId,
    )
    run_id = run["id"]
    with connect(request.app.state.settings.db_path) as conn:
        summary = fetch_one(
            conn,
            """
            SELECT representative_record_ids_json
              FROM cluster_summaries
             WHERE run_id = ? AND cluster_id = ?
            """,
            (run_id, cluster_id),
        )
        if summary is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="topic not found")
        representative_ids = json.loads(summary["representative_record_ids_json"])[:topK]
        rows = []
        if representative_ids:
            placeholders = ", ".join("?" for _ in representative_ids)
            rows = fetch_all(
                conn,
                f"""
                SELECT r.*, cm.cluster_id, cm.probability, cm.outlier_score, cm.is_noise
                  FROM cluster_memberships cm
                  JOIN records r ON r.id = cm.record_id
                 WHERE cm.run_id = ? AND cm.cluster_id = ? AND cm.record_id IN ({placeholders})
                """,
                (run_id, cluster_id, *representative_ids),
            )
    rows_by_id = {row["id"]: row for row in rows}
    ordered_rows = [
        rows_by_id[record_id] for record_id in representative_ids if record_id in rows_by_id
    ]
    refs = json.loads(run["input_refs_json"])
    return {
        "clusterRunId": run_id,
        "embeddingRunId": refs.get("embeddingRunId"),
        "clusterId": cluster_id,
        "records": [_topic_record_response(row) for row in ordered_rows],
    }


@router.get("/{view_id}/outliers")
async def list_outliers(
    request: Request,
    graph_id: str,
    view_id: str,
    clusterRunId: str | None = None,
    topK: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    run = _resolve_cluster_run(
        request.app.state.settings.db_path,
        graph_id,
        view_id,
        clusterRunId,
    )
    run_id = run["id"]
    with connect(request.app.state.settings.db_path) as conn:
        rows = fetch_all(
            conn,
            """
            SELECT r.*, cm.cluster_id, cm.probability, cm.outlier_score, cm.is_noise
              FROM cluster_memberships cm
              JOIN records r ON r.id = cm.record_id
             WHERE cm.run_id = ?
             ORDER BY cm.is_noise DESC, cm.outlier_score DESC, r.id ASC
             LIMIT ?
            """,
            (run_id, topK),
        )
    refs = json.loads(run["input_refs_json"])
    return {
        "clusterRunId": run_id,
        "embeddingRunId": refs.get("embeddingRunId"),
        "records": [_topic_record_response(row) for row in rows],
    }


def _get_view_response(db_path: str, graph_id: str, view_id: str) -> dict[str, Any]:
    return _view_response(db_path, _get_view_row(db_path, graph_id, view_id))


def _get_graph_row(db_path: str, graph_id: str) -> dict:
    with connect(db_path) as conn:
        row = fetch_one(conn, "SELECT * FROM graphs WHERE id = ?", (graph_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")
    return row


def _get_view_row(db_path: str, graph_id: str, view_id: str) -> dict:
    _ensure_graph(db_path, graph_id)
    with connect(db_path) as conn:
        row = fetch_one(
            conn,
            "SELECT * FROM views WHERE graph_id = ? AND id = ?",
            (graph_id, view_id),
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="view not found")
    return row


def _view_response(db_path: str, row: dict) -> dict[str, Any]:
    scope = json.loads(row["scope_json"])
    try:
        where, params = compile_scope(scope, alias="r")
    except ScopeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc
    with connect(db_path) as conn:
        count = conn.execute(
            f"SELECT COUNT(*) FROM records r WHERE r.graph_id = ? AND {where}",
            (row["graph_id"], *params),
        ).fetchone()[0]
    return {
        "id": row["id"],
        "graphId": row["graph_id"],
        "name": row["name"],
        "description": row["description"],
        "scope": scope,
        "defaultEmbeddingRunId": row["default_embedding_run_id"],
        "defaultClusterRunId": row["default_cluster_run_id"],
        "defaultLayoutRunId": row["default_layout_run_id"],
        "defaultLabelRunId": row["default_label_run_id"],
        "defaultTrendRunId": row["default_trend_run_id"],
        "recordCount": count,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _ensure_graph(db_path: str, graph_id: str) -> None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM graphs WHERE id = ?", (graph_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")


def _latest_succeeded_embedding_run(db_path: str, graph_id: str) -> str | None:
    with connect(db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT id
              FROM runs
             WHERE graph_id = ? AND type = 'embed' AND status = 'succeeded'
             ORDER BY completed_at DESC, created_at DESC, id DESC
             LIMIT 1
            """,
            (graph_id,),
        )
    return row["id"] if row else None


def _view_record_count(db_path: str, graph_id: str, view_id: str) -> int:
    view = _get_view_row(db_path, graph_id, view_id)
    where, params = compile_scope(json.loads(view["scope_json"]), alias="r")
    with connect(db_path) as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM records r WHERE r.graph_id = ? AND {where}",
            (graph_id, *params),
        ).fetchone()[0]


def _focus_view_member_count(
    db_path: str,
    graph_id: str,
    view_id: str,
    focus: dict[str, Any],
) -> int:
    view = _get_view_row(db_path, graph_id, view_id)
    where, params = compile_scope(json.loads(view["scope_json"]), alias="r")
    with connect(db_path) as conn:
        return conn.execute(
            f"""
            SELECT COUNT(*)
              FROM cluster_memberships cm
              JOIN records r ON r.id = cm.record_id
             WHERE cm.run_id = ? AND cm.cluster_id = ?
               AND r.graph_id = ? AND {where}
            """,
            (focus["clusterRunId"], focus["clusterId"], graph_id, *params),
        ).fetchone()[0]


def _resolve_focus(
    db_path: str,
    graph_id: str,
    view_id: str,
    value: Any,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "focus", "message": "must be an object"}],
        )
    unknown = sorted(set(value) - {"clusterRunId", "clusterId"})
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {"field": f"focus.{key}", "message": "unknown request key"}
                for key in unknown
            ],
        )
    cluster_id = value.get("clusterId")
    if not isinstance(cluster_id, int) or isinstance(cluster_id, bool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "focus.clusterId", "message": "must be an integer"}],
        )
    cluster_run = _resolve_cluster_run(db_path, graph_id, view_id, value.get("clusterRunId"))
    with connect(db_path) as conn:
        rows = fetch_all(
            conn,
            """
            SELECT cluster_id
              FROM cluster_summaries
             WHERE run_id = ?
             ORDER BY cluster_id ASC
            """,
            (cluster_run["id"],),
        )
    valid_ids = [int(row["cluster_id"]) for row in rows]
    if cluster_id not in valid_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"focus clusterId not found; valid ids: {valid_ids}",
        )
    return {"clusterRunId": cluster_run["id"], "clusterId": cluster_id}


def _resolve_cluster_run(
    db_path: str,
    graph_id: str,
    view_id: str,
    requested_run_id: str | None,
) -> dict:
    _get_view_row(db_path, graph_id, view_id)
    if requested_run_id is not None:
        with connect(db_path) as conn:
            row = fetch_one(
                conn,
                """
                SELECT *
                  FROM runs
                 WHERE id = ? AND graph_id = ? AND view_id = ?
                   AND type = 'cluster' AND status = 'succeeded'
                """,
                (requested_run_id, graph_id, view_id),
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="cluster run not found",
            )
        return row
    with connect(db_path) as conn:
        view = fetch_one(
            conn,
            "SELECT default_cluster_run_id FROM views WHERE id = ? AND graph_id = ?",
            (view_id, graph_id),
        )
    if view is None or view["default_cluster_run_id"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="view has no cluster run; POST /api/graphs/{gid}/views/{vid}/cluster first",
        )
    with connect(db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT *
              FROM runs
             WHERE id = ? AND graph_id = ? AND view_id = ?
               AND type = 'cluster' AND status = 'succeeded'
            """,
            (view["default_cluster_run_id"], graph_id, view_id),
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "view default cluster run is unavailable; "
                "POST /api/graphs/{gid}/views/{vid}/cluster first"
            ),
        )
    return row


def _resolve_trend_run(
    db_path: str,
    graph_id: str,
    view_id: str,
    requested_run_id: str | None,
) -> dict:
    _get_view_row(db_path, graph_id, view_id)
    if requested_run_id is not None:
        with connect(db_path) as conn:
            row = fetch_one(
                conn,
                """
                SELECT *
                  FROM runs
                 WHERE id = ? AND graph_id = ? AND view_id = ?
                   AND type = 'trend' AND status = 'succeeded'
                """,
                (requested_run_id, graph_id, view_id),
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="trend run not found",
            )
        return row
    with connect(db_path) as conn:
        view = fetch_one(
            conn,
            "SELECT default_trend_run_id FROM views WHERE id = ? AND graph_id = ?",
            (view_id, graph_id),
        )
    if view is None or view["default_trend_run_id"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "view has no trend run for this window; "
                "POST /api/graphs/{gid}/views/{vid}/trends"
            ),
        )
    with connect(db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT *
              FROM runs
             WHERE id = ? AND graph_id = ? AND view_id = ?
               AND type = 'trend' AND status = 'succeeded'
            """,
            (view["default_trend_run_id"], graph_id, view_id),
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "view default trend run is unavailable; "
                "POST /api/graphs/{gid}/views/{vid}/trends"
            ),
        )
    return row


def _latest_labels_by_cluster(conn: Any, cluster_run_id: str) -> dict[int, dict[str, Any]]:
    rows = fetch_all(
        conn,
        """
        SELECT *
          FROM cluster_labels
         WHERE cluster_run_id = ?
         ORDER BY cluster_id ASC, created_at DESC, id DESC
        """,
        (cluster_run_id,),
    )
    labels = {}
    for row in rows:
        cluster_id = row["cluster_id"]
        if cluster_id not in labels:
            labels[cluster_id] = _label_response(row)
    return labels


def _latest_label_text_by_cluster(conn: Any, cluster_run_id: str) -> dict[int, str]:
    labels = _latest_labels_by_cluster(conn, cluster_run_id)
    return {cluster_id: label["label"] for cluster_id, label in labels.items()}


def _label_response(row: dict) -> dict[str, Any]:
    return {
        "label": row["label"],
        "summary": row["summary"],
        "keySignals": json.loads(row["key_signals_json"]),
        "tags": json.loads(row["tags_json"]),
        "coherent": bool(row["coherent"]),
        "labelRunId": row["label_run_id"],
        "model": row["model"],
    }


def _trend_snapshots_by_cluster(
    conn: Any,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
) -> dict[int, dict[str, Any]]:
    view = fetch_one(
        conn,
        "SELECT default_trend_run_id FROM views WHERE id = ? AND graph_id = ?",
        (view_id, graph_id),
    )
    if view is None or view["default_trend_run_id"] is None:
        return {}
    trend_run = fetch_one(
        conn,
        """
        SELECT *
          FROM runs
         WHERE id = ? AND graph_id = ? AND view_id = ?
           AND type = 'trend' AND status = 'succeeded'
        """,
        (view["default_trend_run_id"], graph_id, view_id),
    )
    if trend_run is None:
        return {}
    refs = json.loads(trend_run["input_refs_json"])
    if refs.get("clusterRunId") != cluster_run_id:
        return {}
    summary_row = fetch_one(
        conn,
        "SELECT summary_json FROM trend_summaries WHERE run_id = ?",
        (trend_run["id"],),
    )
    if summary_row is None:
        return {}
    summary = json.loads(summary_row["summary_json"])
    window = summary["window"]
    rows = fetch_all(
        conn,
        """
        SELECT cluster_id, bucket_start, spike_score
          FROM trend_results
         WHERE run_id = ?
           AND bucket_start >= ?
           AND bucket_start <= ?
         ORDER BY cluster_id ASC, spike_score DESC, bucket_start ASC
        """,
        (trend_run["id"], window["start"], window["end"]),
    )
    snapshots: dict[int, dict[str, Any]] = {}
    for row in rows:
        cluster_id = int(row["cluster_id"])
        if cluster_id in snapshots:
            continue
        snapshots[cluster_id] = {
            "bucket": summary["bucket"],
            "spikeScore": row["spike_score"],
            "topBucket": row["bucket_start"],
        }
    return snapshots


def _topic_response(
    row: dict,
    label: dict[str, Any] | None = None,
    trend: dict[str, Any] | None = None,
    facets: dict[str, int] | None = None,
) -> dict[str, Any]:
    topic = {
        "clusterId": row["cluster_id"],
        "label": label,
        "trend": trend,
        "size": row["size"],
        "meanProbability": row["mean_probability"],
        "representativeRecordIds": json.loads(row["representative_record_ids_json"]),
        "sourceMix": json.loads(row["source_mix_json"]),
    }
    if facets is not None:
        topic["facets"] = facets
    return topic


def _topic_record_response(row: dict) -> dict[str, Any]:
    return {
        "id": row["id"],
        "recordId": row["record_key"],
        "sourceType": row["source_type"],
        "title": row["title"],
        "customerText": row["customer_text"],
        "clusterId": row.get("cluster_id"),
        "probability": row["probability"],
        "outlierScore": row["outlier_score"],
        "isNoise": bool(row["is_noise"]) if "is_noise" in row else False,
    }


def _reject_unknown_body(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": key, "message": "unknown request key"} for key in unknown],
        )


def _validate_trend_window(value: Any, *, bucket: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "window", "message": "must be an object"}],
        )
    unknown = sorted(set(value) - {"start", "end"})
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {"field": f"window.{key}", "message": "unknown request key"}
                for key in unknown
            ],
        )
    if "start" not in value or "end" not in value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "window", "message": "must include start and end"}],
        )
    try:
        _, start_ms = parse_timestamp(value["start"], field="window.start")
        _, end_ms = parse_timestamp(value["end"], field="window.end")
    except TimestampValidationError as exc:
        field = "window.start" if "start" in str(exc) else "window.end"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": field, "message": str(exc)}],
        ) from exc
    if start_ms > end_ms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "window", "message": "start must be before or equal to end"}],
        )
    return {
        "start": bucket_start(start_ms, bucket),
        "end": bucket_start(end_ms, bucket),
    }


def _validate_cluster_ids(value: Any) -> list[int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "clusterIds", "message": "must be a non-empty list of integers"}],
        )
    return sorted(set(value))


def _validate_set_default(value: Any) -> bool:
    if not isinstance(value, bool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "setDefault", "message": "must be a boolean"}],
        )
    return value


def _set_default_from_request(body: dict[str, Any], query_value: bool | None) -> bool:
    if "setDefault" in body:
        return _validate_set_default(body["setDefault"])
    if query_value is not None:
        return query_value
    return True


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "name", "message": "is required and must be a non-empty string"}],
        )
    return value.strip()


def validate_none_space_population(population: int) -> None:
    if population > 20_000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {
                    "field": "cluster.space.method",
                    "message": '"none" is rejected above 20000 scoped records',
                }
            ],
        )
