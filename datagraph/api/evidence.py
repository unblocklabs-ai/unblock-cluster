from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from datagraph.api.facets import facet_counts_by_cluster, validate_facet_by
from datagraph.core.ids import new_id, now_iso
from datagraph.core.scope import ScopeValidationError, compile_scope
from datagraph.core.time import TimestampValidationError, parse_timestamp
from datagraph.core.trend_math import bucket_start, compute_trends
from datagraph.db import connect, fetch_all, fetch_one

router = APIRouter(prefix="/api/graphs/{graph_id}/evidence", tags=["evidence"])

VALID_RECIPES = {
    "surprising_topics",
    "new_topics",
    "vanishing_topics",
    "rising_topics",
    "topic_evidence",
    "compare_periods",
}
TEMPORAL_RECIPES = {
    "surprising_topics",
    "new_topics",
    "vanishing_topics",
    "rising_topics",
    "compare_periods",
}
SUMMARY_SECTION_BY_RECIPE = {
    "surprising_topics": "surprisingTopics",
    "new_topics": "newTopics",
    "vanishing_topics": "vanishingTopics",
    "rising_topics": "risingTopics",
}


@router.post("")
async def create_evidence(request: Request, graph_id: str, body: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_body(
        body,
        {"viewId", "recipe", "timeRange", "periods", "topicId", "topK", "facetBy"},
    )
    view_id = _required_string(body.get("viewId"), "viewId")
    recipe = _required_string(body.get("recipe"), "recipe")
    if recipe not in VALID_RECIPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {
                    "field": "recipe",
                    "message": "must be one of " + ", ".join(sorted(VALID_RECIPES)),
                }
            ],
        )
    if recipe == "topic_evidence":
        _validate_topic_id_shape(body.get("topicId"))
    if recipe == "compare_periods":
        _validate_periods_shape(body.get("periods"))
    facet_by = validate_facet_by(body.get("facetBy"))
    top_k = _validate_top_k(body.get("topK", 10))
    db_path = request.app.state.settings.db_path
    cluster_run = _resolve_cluster_run(db_path, graph_id, view_id)
    cluster_run_id = cluster_run["id"]
    trend_run = None
    if recipe in TEMPORAL_RECIPES:
        trend_run = _resolve_matching_trend_run(db_path, graph_id, view_id, cluster_run_id)

    labels: dict[int, dict[str, Any]]
    with connect(db_path) as conn:
        summaries = _load_cluster_summaries(conn, cluster_run_id)
        labels = _latest_labels_by_cluster(conn, cluster_run_id)
        facets = facet_counts_by_cluster(conn, cluster_run_id, facet_by)
        label_run_id = _matching_default_label_run_id(conn, graph_id, view_id, cluster_run_id)
        input_refs = json.loads(cluster_run["input_refs_json"])
        run_refs = {
            "embeddingRunId": input_refs.get("embeddingRunId"),
            "clusterRunId": cluster_run_id,
        }
        if label_run_id is not None:
            run_refs["labelRunId"] = label_run_id
        if trend_run is not None:
            run_refs["trendRunId"] = trend_run["id"]

        if recipe == "topic_evidence":
            evidence = _topic_evidence(
                conn,
                graph_id,
                view_id,
                cluster_run_id,
                body.get("topicId"),
                top_k,
                summaries,
                labels,
                facets,
            )
            topic_trend = _topic_persisted_trend(
                conn,
                graph_id,
                view_id,
                cluster_run_id,
                evidence["clusterId"],
            )
            evidence["trend"] = topic_trend
            if topic_trend is not None:
                run_refs["trendRunId"] = topic_trend["trendRunId"]
        elif recipe == "compare_periods":
            evidence = _compare_periods(
                conn,
                cluster_run_id,
                trend_run,
                summaries,
                labels,
                facets,
                body.get("periods"),
                top_k,
            )
        else:
            evidence = _summary_recipe(
                conn,
                cluster_run_id,
                trend_run,
                summaries,
                labels,
                facets,
                recipe,
                body.get("timeRange"),
                top_k,
            )

        freshness = _freshness(conn, graph_id, view_id, cluster_run)

    response = {
        "viewId": view_id,
        "recipe": recipe,
        "evidence": evidence,
        "runRefs": run_refs,
        "freshness": freshness,
        "vizUrl": f"http://127.0.0.1:{request.app.state.settings.port}/?graphId={graph_id}&viewId={view_id}",
    }
    _persist_analysis_event(db_path, graph_id, view_id, recipe, body, run_refs, evidence)
    return response


def _summary_recipe(
    conn: Any,
    cluster_run_id: str,
    trend_run: dict[str, Any],
    summaries: dict[int, dict[str, Any]],
    labels: dict[int, dict[str, Any]],
    facets: dict[int, dict[str, int]],
    recipe: str,
    time_range: Any,
    top_k: int,
) -> list[dict[str, Any]]:
    bucket = _trend_bucket(trend_run)
    window = _validate_optional_range(time_range, bucket=bucket)
    if window is None:
        window = _trend_window(conn, trend_run)
    computation = compute_trends(
        _load_memberships(conn, cluster_run_id),
        bucket=bucket,
        window_start=window["start"] if window else None,
        window_end=window["end"] if window else None,
    )
    section = computation.summary[SUMMARY_SECTION_BY_RECIPE[recipe]]
    return [
        _enrich_topic_entry(row, summaries, labels)
        | ({"facets": facets[row["clusterId"]]} if row["clusterId"] in facets else {})
        for row in section[:top_k]
        if row["clusterId"] in summaries
    ]


def _topic_evidence(
    conn: Any,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
    topic_id: Any,
    top_k: int,
    summaries: dict[int, dict[str, Any]],
    labels: dict[int, dict[str, Any]],
    facets: dict[int, dict[str, int]],
) -> dict[str, Any]:
    _validate_topic_id_shape(topic_id)
    if topic_id not in summaries:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="topic not found")
    summary = summaries[topic_id]
    representative_ids = summary["representativeRecordIds"][:top_k]
    evidence = {
        "clusterId": topic_id,
        "label": labels.get(topic_id),
        "size": summary["size"],
        "meanProbability": summary["meanProbability"],
        "sourceMix": summary["sourceMix"],
        "representativeRecordIds": representative_ids,
        "representatives": _load_representatives(
            conn,
            cluster_run_id,
            topic_id,
            representative_ids,
        ),
        "trend": None,
        "viewId": view_id,
        "graphId": graph_id,
    }
    if topic_id in facets:
        evidence["facets"] = facets[topic_id]
    return evidence


def _compare_periods(
    conn: Any,
    cluster_run_id: str,
    trend_run: dict[str, Any],
    summaries: dict[int, dict[str, Any]],
    labels: dict[int, dict[str, Any]],
    facets: dict[int, dict[str, int]],
    periods: Any,
    top_k: int,
) -> list[dict[str, Any]]:
    bucket = _trend_bucket(trend_run)
    parsed = _validate_periods(periods, bucket=bucket)
    computation = compute_trends(_load_memberships(conn, cluster_run_id), bucket=bucket)
    points_by_cluster: dict[int, list[Any]] = {}
    for point in computation.points:
        points_by_cluster.setdefault(point.cluster_id, []).append(point)

    rows = []
    for cluster_id, points in points_by_cluster.items():
        if cluster_id not in summaries:
            continue
        a_points = _points_in_range(points, parsed["a"])
        b_points = _points_in_range(points, parsed["b"])
        a_count = sum(point.count for point in a_points)
        b_count = sum(point.count for point in b_points)
        a_mean_share = _mean_share(a_points)
        b_mean_share = _mean_share(b_points)
        row = _enrich_topic_entry(
            {
                "clusterId": cluster_id,
                "periodA": {"count": a_count, "meanShare": a_mean_share},
                "periodB": {"count": b_count, "meanShare": b_mean_share},
                "deltaCount": b_count - a_count,
                "deltaShare": b_mean_share - a_mean_share,
            },
            summaries,
            labels,
        )
        if cluster_id in facets:
            row["facets"] = facets[cluster_id]
        rows.append(row)
    return sorted(rows, key=lambda row: (-abs(row["deltaShare"]), row["clusterId"]))[:top_k]


def _points_in_range(points: list[Any], period: dict[str, str]) -> list[Any]:
    return [
        point
        for point in points
        if period["start"] <= point.bucket_start <= period["end"]
    ]


def _mean_share(points: list[Any]) -> float:
    return sum(point.share for point in points) / len(points) if points else 0.0


def _topic_persisted_trend(
    conn: Any,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
    cluster_id: int,
) -> dict[str, Any] | None:
    trend_run = _matching_trend_run_row(conn, graph_id, view_id, cluster_run_id)
    if trend_run is None:
        return None
    summary_row = fetch_one(
        conn,
        "SELECT summary_json FROM trend_summaries WHERE run_id = ?",
        (trend_run["id"],),
    )
    if summary_row is None:
        return None
    summary = json.loads(summary_row["summary_json"])
    rows = fetch_all(
        conn,
        """
        SELECT bucket_start, count, share, spike_score
          FROM trend_results
         WHERE run_id = ? AND cluster_id = ?
         ORDER BY bucket_start ASC
        """,
        (trend_run["id"], cluster_id),
    )
    if not rows:
        return None
    top = max(rows, key=lambda row: (row["spike_score"], row["count"], row["bucket_start"]))
    return {
        "trendRunId": trend_run["id"],
        "bucket": summary["bucket"],
        "snapshot": {
            "bucket": summary["bucket"],
            "spikeScore": top["spike_score"],
            "topBucket": top["bucket_start"],
        },
        "series": [
            {
                "bucketStart": row["bucket_start"],
                "count": row["count"],
                "share": row["share"],
                "spikeScore": row["spike_score"],
            }
            for row in rows
        ],
    }


def _enrich_topic_entry(
    row: dict[str, Any],
    summaries: dict[int, dict[str, Any]],
    labels: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    cluster_id = int(row["clusterId"])
    summary = summaries[cluster_id]
    enriched = {
        **row,
        "clusterId": cluster_id,
        "label": labels.get(cluster_id, {}).get("label"),
        "sourceMix": summary["sourceMix"],
        "representativeRecordIds": summary["representativeRecordIds"],
    }
    return enriched


def _load_cluster_summaries(conn: Any, cluster_run_id: str) -> dict[int, dict[str, Any]]:
    rows = fetch_all(
        conn,
        """
        SELECT *
          FROM cluster_summaries
         WHERE run_id = ?
         ORDER BY cluster_id ASC
        """,
        (cluster_run_id,),
    )
    return {
        int(row["cluster_id"]): {
            "clusterId": int(row["cluster_id"]),
            "size": row["size"],
            "meanProbability": row["mean_probability"],
            "representativeRecordIds": json.loads(row["representative_record_ids_json"]),
            "sourceMix": json.loads(row["source_mix_json"]),
        }
        for row in rows
    }


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
                "keySignals": json.loads(row["key_signals_json"]),
                "tags": json.loads(row["tags_json"]),
                "coherent": bool(row["coherent"]),
                "labelRunId": row["label_run_id"],
                "model": row["model"],
            }
    return labels


def _load_representatives(
    conn: Any,
    cluster_run_id: str,
    cluster_id: int,
    representative_ids: list[str],
) -> list[dict[str, Any]]:
    if not representative_ids:
        return []
    placeholders = ", ".join("?" for _ in representative_ids)
    rows = fetch_all(
        conn,
        f"""
        SELECT r.id, r.record_key, r.source_type, r.title, r.customer_text,
               r.record_url, r.timestamp_utc
          FROM cluster_memberships cm
          JOIN records r ON r.id = cm.record_id
         WHERE cm.run_id = ? AND cm.cluster_id = ? AND cm.record_id IN ({placeholders})
        """,
        (cluster_run_id, cluster_id, *representative_ids),
    )
    by_id = {row["id"]: row for row in rows}
    return [
        {
            "id": row["id"],
            "recordId": row["record_key"],
            "sourceType": row["source_type"],
            "title": row["title"],
            "customerText": row["customer_text"],
            "recordUrl": row["record_url"],
            "timestamp": row["timestamp_utc"],
        }
        for row in (by_id[record_id] for record_id in representative_ids if record_id in by_id)
    ]


def _load_memberships(conn: Any, cluster_run_id: str) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """
        SELECT cm.cluster_id, cm.is_noise, r.timestamp_ms
          FROM cluster_memberships cm
          JOIN records r ON r.id = cm.record_id
         WHERE cm.run_id = ?
         ORDER BY r.timestamp_ms ASC, r.id ASC
        """,
        (cluster_run_id,),
    )


def _resolve_cluster_run(db_path: str, graph_id: str, view_id: str) -> dict:
    with connect(db_path) as conn:
        view = fetch_one(
            conn,
            "SELECT default_cluster_run_id FROM views WHERE id = ? AND graph_id = ?",
            (view_id, graph_id),
        )
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="view not found")
    if view["default_cluster_run_id"] is None:
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


def _resolve_matching_trend_run(
    db_path: str,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
) -> dict:
    with connect(db_path) as conn:
        row = _matching_trend_run_row(conn, graph_id, view_id, cluster_run_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "view has no trend run for this cluster run; "
                "POST /api/graphs/{gid}/views/{vid}/trends first"
            ),
        )
    return row


def _matching_trend_run_row(
    conn: Any,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
) -> dict | None:
    view = fetch_one(
        conn,
        "SELECT default_trend_run_id FROM views WHERE id = ? AND graph_id = ?",
        (view_id, graph_id),
    )
    if view is None or view["default_trend_run_id"] is None:
        return None
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
        return None
    refs = json.loads(row["input_refs_json"])
    return row if refs.get("clusterRunId") == cluster_run_id else None


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


def _trend_bucket(trend_run: dict[str, Any]) -> str:
    stats = json.loads(trend_run["stats_json"])
    return stats["bucket"]


def _trend_window(conn: Any, trend_run: dict[str, Any]) -> dict[str, str] | None:
    summary = _trend_summary_from_run(conn, trend_run)
    return summary.get("window")


def _trend_summary_from_run(conn: Any, trend_run: dict[str, Any]) -> dict[str, Any]:
    row = fetch_one(
        conn,
        "SELECT summary_json FROM trend_summaries WHERE run_id = ?",
        (trend_run["id"],),
    )
    return json.loads(row["summary_json"]) if row else {}


def _validate_optional_range(value: Any, *, bucket: str) -> dict[str, str] | None:
    if value is None:
        return None
    return _validate_range(value, "timeRange", bucket=bucket)


def _validate_periods(value: Any, *, bucket: str) -> dict[str, dict[str, str]]:
    _validate_periods_shape(value)
    return {
        "a": _validate_range(value["a"], "periods.a", bucket=bucket),
        "b": _validate_range(value["b"], "periods.b", bucket=bucket),
    }


def _validate_range(value: Any, field: str, *, bucket: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": field, "message": "must be an object"}],
        )
    unknown = sorted(set(value) - {"start", "end"})
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {"field": f"{field}.{key}", "message": "unknown request key"}
                for key in unknown
            ],
        )
    if "start" not in value or "end" not in value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": field, "message": "must include start and end"}],
        )
    try:
        _, start_ms = parse_timestamp(value["start"], field=f"{field}.start")
        _, end_ms = parse_timestamp(value["end"], field=f"{field}.end")
    except TimestampValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": field, "message": str(exc)}],
        ) from exc
    if start_ms > end_ms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": field, "message": "start must be before or equal to end"}],
        )
    return {"start": bucket_start(start_ms, bucket), "end": bucket_start(end_ms, bucket)}


def _freshness(conn: Any, graph_id: str, view_id: str, cluster_run: dict) -> dict[str, Any]:
    view = fetch_one(
        conn,
        "SELECT scope_json FROM views WHERE id = ? AND graph_id = ?",
        (view_id, graph_id),
    )
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="view not found")
    try:
        where, params = compile_scope(json.loads(view["scope_json"]), alias="r")
    except ScopeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc
    count = conn.execute(
        f"""
        SELECT COUNT(*)
          FROM records r
         WHERE r.graph_id = ? AND ({where}) AND r.created_at > ?
        """,
        (graph_id, *params, cluster_run["created_at"]),
    ).fetchone()[0]
    return {
        "clusterRunCreatedAt": cluster_run["created_at"],
        "recordsAddedSinceClusterRun": count,
    }


def _persist_analysis_event(
    db_path: str,
    graph_id: str,
    view_id: str,
    recipe: str,
    params: dict[str, Any],
    run_refs: dict[str, Any],
    evidence: Any,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO analysis_events (
              id, graph_id, view_id, recipe, params_json, run_refs_json,
              evidence_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("evt"),
                graph_id,
                view_id,
                recipe,
                json.dumps(params, sort_keys=True),
                json.dumps(run_refs, sort_keys=True),
                json.dumps(evidence, sort_keys=True),
                now_iso(),
            ),
        )
        conn.commit()


def _reject_unknown_body(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": key, "message": "unknown request key"} for key in unknown],
        )


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": field, "message": "is required and must be a non-empty string"}],
        )
    return value.strip()


def _validate_topic_id_shape(value: Any) -> None:
    if value is None or not isinstance(value, int) or isinstance(value, bool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "topicId", "message": "is required and must be an integer"}],
        )


def _validate_periods_shape(value: Any) -> None:
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "periods", "message": "is required and must be an object"}],
        )
    if set(value) != {"a", "b"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "periods", "message": "must include periods a and b"}],
        )


def _validate_top_k(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 50:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "topK", "message": "must be an integer from 1 to 50"}],
        )
    return value
