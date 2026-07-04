from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from datagraph.api.warnings import resolved_run_warnings
from datagraph.core.config import load_graph_config
from datagraph.db import connect, fetch_all, fetch_one

router = APIRouter(prefix="/api/graphs/{graph_id}/views/{view_id}", tags=["artifact"])
ARTIFACT_CACHE_LIMIT = 4


@router.get("/artifact")
async def get_view_artifact(request: Request, graph_id: str, view_id: str) -> Response:
    db_path = request.app.state.settings.db_path
    with connect(db_path) as conn:
        context = _artifact_context(conn, graph_id, view_id)
        etag = _artifact_etag(context)
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

        cache = _artifact_cache(request)
        cached = cache.get(etag)
        if cached is not None:
            cache.move_to_end(etag)
            return JSONResponse(cached, headers=headers)

        artifact = _compose_artifact(conn, context)
        _increment_composition_count(request)
        cache[etag] = artifact
        while len(cache) > ARTIFACT_CACHE_LIMIT:
            cache.popitem(last=False)
        return JSONResponse(artifact, headers=headers)


def _artifact_context(conn: Any, graph_id: str, view_id: str) -> dict[str, Any]:
    graph = fetch_one(conn, "SELECT * FROM graphs WHERE id = ?", (graph_id,))
    if graph is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")
    view = fetch_one(
        conn,
        "SELECT * FROM views WHERE graph_id = ? AND id = ?",
        (graph_id, view_id),
    )
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="view not found")
    cluster_run = _required_default_run(
        conn,
        graph_id,
        view_id,
        view["default_cluster_run_id"],
        "cluster",
        "view has no cluster run; POST /api/graphs/{gid}/views/{vid}/cluster first",
    )
    layout_run = _required_default_run(
        conn,
        graph_id,
        view_id,
        view["default_layout_run_id"],
        "layout",
        "view has no layout run; POST /api/graphs/{gid}/views/{vid}/layout first",
    )
    cluster_run_id = cluster_run["id"]
    layout_run_id = layout_run["id"]
    label_run_id = _matching_default_label_run_id(conn, graph_id, view_id, cluster_run_id)
    label_freshness = _artifact_label_freshness(conn, cluster_run_id)
    # Trend snapshots are intentionally scoped to the matching default trend run;
    # that run id is already part of the ETag inputs.
    _, trend_run_id = _trend_snapshots(conn, graph_id, view_id, cluster_run_id, snapshots=False)
    freshness = _artifact_record_freshness(conn, cluster_run_id, layout_run_id)
    return {
        "graph": graph,
        "view": view,
        "clusterRun": cluster_run,
        "layoutRun": layout_run,
        "labelRunId": label_run_id,
        "labelCount": label_freshness["labelCount"],
        "maxLabelCreatedAt": label_freshness["maxLabelCreatedAt"],
        "trendRunId": trend_run_id,
        "recordCount": freshness["recordCount"],
        "maxUpdatedAt": freshness["maxUpdatedAt"],
    }


def _compose_artifact(conn: Any, context: dict[str, Any]) -> dict[str, Any]:
    graph = context["graph"]
    graph_id = graph["id"]
    view_id = context["view"]["id"]
    cluster_run = context["clusterRun"]
    layout_run = context["layoutRun"]
    label_run_id = context["labelRunId"]
    cluster_run_id = cluster_run["id"]
    layout_run_id = layout_run["id"]
    cluster_refs = json.loads(cluster_run["input_refs_json"])
    layout_refs = json.loads(layout_run["input_refs_json"])
    embedding_run_id = cluster_refs.get("embeddingRunId") or layout_refs.get("embeddingRunId")
    representation = _embedding_representation(conn, embedding_run_id)
    labels = _latest_labels_by_cluster(conn, cluster_run_id)
    trend_snapshots, trend_run_id = _trend_snapshots(conn, graph_id, view_id, cluster_run_id)
    warnings = resolved_run_warnings(
        conn,
        graph_id=graph_id,
        view_id=view_id,
        cluster_run_id=cluster_run_id,
    )
    topics = _topics(conn, cluster_run_id, labels, trend_snapshots)
    data = _data_rows(conn, cluster_run_id, layout_run_id)

    cluster_stats = json.loads(cluster_run["stats_json"])
    layout_params = json.loads(layout_run["params_json"]).get("layout", {})
    graph_config = load_graph_config(graph)
    embedding_config = graph_config.get("embedding", {})
    run_refs = {
        "embeddingRunId": embedding_run_id,
        "clusterRunId": cluster_run_id,
        "layoutRunId": layout_run_id,
    }
    if representation["summarizeRunId"] is not None:
        run_refs["summarizeRunId"] = representation["summarizeRunId"]
    if label_run_id is not None:
        run_refs["labelRunId"] = label_run_id
    if trend_run_id is not None:
        run_refs["trendRunId"] = trend_run_id
    return {
        "graphId": graph_id,
        "viewId": view_id,
        "config": {
            "embedding": {
                "model": embedding_config.get("model"),
                "dimensions": embedding_config.get("dimensions"),
            }
        },
        "runRefs": run_refs,
        "representation": representation["representation"],
        "warnings": warnings,
        "layout": {
            "method": layout_params.get("method", "umap"),
            "params": layout_params,
        },
        "noise": {
            "noiseCount": cluster_stats.get("noiseCount", 0),
            "noiseRatio": cluster_stats.get("noiseRatio", 0),
        },
        "topics": topics,
        "data": data,
    }


def _artifact_record_freshness(
    conn: Any,
    cluster_run_id: str,
    layout_run_id: str,
) -> dict[str, Any]:
    row = fetch_one(
        conn,
        """
        SELECT COUNT(*) AS record_count, MAX(r.updated_at) AS max_updated_at
          FROM layout_points lp
          JOIN cluster_memberships cm
            ON cm.record_id = lp.record_id AND cm.run_id = ?
          JOIN records r ON r.id = lp.record_id
         WHERE lp.run_id = ?
        """,
        (cluster_run_id, layout_run_id),
    )
    return {
        "recordCount": row["record_count"] if row else 0,
        "maxUpdatedAt": row["max_updated_at"] if row else None,
    }


def _artifact_label_freshness(conn: Any, cluster_run_id: str) -> dict[str, Any]:
    row = fetch_one(
        conn,
        """
        SELECT COUNT(*) AS label_count, MAX(created_at) AS max_label_created_at
          FROM cluster_labels
         WHERE cluster_run_id = ?
        """,
        (cluster_run_id,),
    )
    return {
        "labelCount": row["label_count"] if row else 0,
        "maxLabelCreatedAt": row["max_label_created_at"] if row else None,
    }


def _artifact_etag(context: dict[str, Any]) -> str:
    cluster_run = context["clusterRun"]
    layout_run = context["layoutRun"]
    cluster_refs = json.loads(cluster_run["input_refs_json"])
    layout_refs = json.loads(layout_run["input_refs_json"])
    inputs = {
        "graphId": context["graph"]["id"],
        "viewId": context["view"]["id"],
        "runRefs": {
            "embeddingRunId": (
                cluster_refs.get("embeddingRunId") or layout_refs.get("embeddingRunId")
            ),
            "clusterRunId": cluster_run["id"],
            "layoutRunId": layout_run["id"],
            "labelRunId": context["labelRunId"],
            "trendRunId": context["trendRunId"],
        },
        "recordCount": context["recordCount"],
        "maxUpdatedAt": context["maxUpdatedAt"],
        "labelCount": context["labelCount"],
        "maxLabelCreatedAt": context["maxLabelCreatedAt"],
    }
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode("utf-8")).hexdigest()
    return f'"artifact-{digest}"'


def _embedding_representation(conn: Any, embedding_run_id: str | None) -> dict[str, Any]:
    if embedding_run_id is None:
        return {"representation": "raw", "summarizeRunId": None}
    row = fetch_one(
        conn,
        "SELECT params_json, input_refs_json FROM runs WHERE id = ? AND type = 'embed'",
        (embedding_run_id,),
    )
    if row is None:
        return {"representation": "raw", "summarizeRunId": None}
    params = json.loads(row["params_json"])
    refs = json.loads(row["input_refs_json"])
    representation = params.get("representation", "raw")
    if representation != "summary":
        return {"representation": "raw", "summarizeRunId": None}
    return {
        "representation": "summary",
        "summarizeRunId": params.get("summarizeRunId") or refs.get("summarizeRunId"),
    }


def _artifact_cache(request: Request) -> OrderedDict[str, dict[str, Any]]:
    cache = getattr(request.app.state, "artifact_cache", None)
    if cache is None:
        cache = OrderedDict()
        request.app.state.artifact_cache = cache
    return cache


def _increment_composition_count(request: Request) -> None:
    request.app.state.artifact_compositions = (
        getattr(request.app.state, "artifact_compositions", 0) + 1
    )


def _required_default_run(
    conn: Any,
    graph_id: str,
    view_id: str,
    run_id: str | None,
    run_type: str,
    missing_detail: str,
) -> dict:
    if run_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=missing_detail)
    row = fetch_one(
        conn,
        """
        SELECT *
          FROM runs
         WHERE id = ? AND graph_id = ? AND view_id = ?
           AND type = ? AND status = 'succeeded'
        """,
        (run_id, graph_id, view_id, run_type),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=missing_detail)
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
        cluster_id = int(row["cluster_id"])
        if cluster_id not in labels:
            labels[cluster_id] = {
                "label": row["label"],
                "summary": row["summary"],
                "coherent": bool(row["coherent"]),
                "labelRunId": row["label_run_id"],
            }
    return labels


def _matching_default_label_run_id(
    conn: Any,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
) -> str | None:
    view = fetch_one(
        conn,
        "SELECT default_label_run_id FROM views WHERE id = ? AND graph_id = ?",
        (view_id, graph_id),
    )
    if view is None or view["default_label_run_id"] is None:
        return None
    row = fetch_one(
        conn,
        """
        SELECT input_refs_json
          FROM runs
         WHERE id = ? AND graph_id = ? AND view_id = ?
           AND type = 'label' AND status = 'succeeded'
        """,
        (view["default_label_run_id"], graph_id, view_id),
    )
    if row is None:
        return None
    refs = json.loads(row["input_refs_json"])
    return view["default_label_run_id"] if refs.get("clusterRunId") == cluster_run_id else None


def _trend_snapshots(
    conn: Any,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
    *,
    snapshots: bool = True,
) -> tuple[dict[int, dict[str, Any]], str | None]:
    view = fetch_one(
        conn,
        "SELECT default_trend_run_id FROM views WHERE id = ? AND graph_id = ?",
        (view_id, graph_id),
    )
    if view is None or view["default_trend_run_id"] is None:
        return {}, None
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
        return {}, None
    refs = json.loads(trend_run["input_refs_json"])
    if refs.get("clusterRunId") != cluster_run_id:
        return {}, None
    if not snapshots:
        return {}, trend_run["id"]
    summary_row = fetch_one(
        conn,
        "SELECT summary_json FROM trend_summaries WHERE run_id = ?",
        (trend_run["id"],),
    )
    if summary_row is None:
        return {}, None
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
            "spikeScore": _round4(row["spike_score"]),
            "topBucket": row["bucket_start"],
        }
    return snapshots, trend_run["id"]


def _topics(
    conn: Any,
    cluster_run_id: str,
    labels: dict[int, dict[str, Any]],
    trends: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = fetch_all(
        conn,
        """
        SELECT *
          FROM cluster_summaries
         WHERE run_id = ?
         ORDER BY size DESC, cluster_id ASC
        """,
        (cluster_run_id,),
    )
    topics = []
    for row in rows:
        cluster_id = int(row["cluster_id"])
        label = labels.get(cluster_id)
        topics.append(
            {
                "clusterId": cluster_id,
                "label": label["label"] if label else None,
                "summary": label["summary"] if label else None,
                "coherent": label["coherent"] if label else None,
                "size": row["size"],
                "meanProbability": _round4(row["mean_probability"]),
                "sourceMix": json.loads(row["source_mix_json"]),
                "representativeRecordIds": json.loads(row["representative_record_ids_json"]),
                "trend": trends.get(cluster_id),
            }
        )
    return topics


def _data_rows(conn: Any, cluster_run_id: str, layout_run_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        conn,
        """
        SELECT r.*, lp.x, lp.y, cm.cluster_id, cm.probability, cm.outlier_score, cm.is_noise
          FROM layout_points lp
          JOIN cluster_memberships cm
            ON cm.record_id = lp.record_id AND cm.run_id = ?
          JOIN records r ON r.id = lp.record_id
         WHERE lp.run_id = ?
         ORDER BY r.timestamp_ms ASC, r.id ASC
        """,
        (cluster_run_id, layout_run_id),
    )
    return [
        {
            "id": row["id"],
            "recordId": row["record_key"],
            "title": row["title"],
            "customerText": _truncate(row["customer_text"], 300),
            "sourceType": row["source_type"],
            "sourceName": row["source_name"],
            "product": row["product"],
            "sentiment": row["sentiment"],
            "rating": row["rating"],
            "tags": json.loads(row["tags_json"]) if row["tags_json"] else None,
            "timestamp": row["timestamp_utc"],
            "recordUrl": row["record_url"],
            "x": _round4(row["x"]),
            "y": _round4(row["y"]),
            "clusterId": row["cluster_id"],
            "clusterProbability": _round4(row["probability"]),
            "outlierScore": _round4(row["outlier_score"]),
            "isNoise": bool(row["is_noise"]),
        }
        for row in rows
    ]


def _round4(value: Any) -> Any:
    return round(float(value), 4) if value is not None else None


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
