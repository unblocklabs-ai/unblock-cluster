from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from datagraph.api.warnings import resolved_run_warnings
from datagraph.db import connect, fetch_all, fetch_one

router = APIRouter(prefix="/api/graphs/{graph_id}/views/{view_id}", tags=["artifact"])


@router.get("/artifact")
async def get_view_artifact(request: Request, graph_id: str, view_id: str) -> dict[str, Any]:
    db_path = request.app.state.settings.db_path
    with connect(db_path) as conn:
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
        cluster_refs = json.loads(cluster_run["input_refs_json"])
        layout_refs = json.loads(layout_run["input_refs_json"])
        labels = _latest_labels_by_cluster(conn, cluster_run_id)
        label_run_id = _matching_default_label_run_id(conn, graph_id, view_id, cluster_run_id)
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
    graph_config = json.loads(graph["config_json"])
    embedding_config = graph_config.get("embedding", {})
    run_refs = {
        "embeddingRunId": cluster_refs.get("embeddingRunId") or layout_refs.get("embeddingRunId"),
        "clusterRunId": cluster_run_id,
        "layoutRunId": layout_run_id,
    }
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
            "spikeScore": row["spike_score"],
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
                "meanProbability": row["mean_probability"],
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
            "x": row["x"],
            "y": row["y"],
            "clusterId": row["cluster_id"],
            "clusterProbability": row["probability"],
            "outlierScore": row["outlier_score"],
            "isNoise": bool(row["is_noise"]),
        }
        for row in rows
    ]


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
