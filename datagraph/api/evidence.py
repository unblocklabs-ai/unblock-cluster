from __future__ import annotations

import json
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Request, status

from datagraph.api.facets import (
    facet_counts_by_cluster,
    summarize_run_id_for_cluster_run,
    validate_facet_by,
)
from datagraph.core.ids import new_id, now_iso
from datagraph.core.openai_client import EmbeddingBatchResult
from datagraph.core.scope import ScopeValidationError, compile_scope
from datagraph.core.time import TimestampValidationError, parse_timestamp
from datagraph.core.trend_math import bucket_start, compute_trends
from datagraph.core.vectors import load_vectors_for_records, normalize_l2
from datagraph.db import connect, fetch_all, fetch_one

router = APIRouter(prefix="/api/graphs/{graph_id}/evidence", tags=["evidence"])

VALID_RECIPES = {
    "surprising_topics",
    "new_topics",
    "vanishing_topics",
    "rising_topics",
    "topic_evidence",
    "topic_search",
    "question_evidence",
    "compare_periods",
}
TEMPORAL_RECIPES = {
    "surprising_topics",
    "new_topics",
    "vanishing_topics",
    "rising_topics",
    "compare_periods",
}
QUESTION_RECIPES = {"topic_search", "question_evidence"}
QUESTION_EVIDENCE_SIMILARITY_FLOOR = 0.2
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
        {"viewId", "recipe", "timeRange", "periods", "topicId", "topK", "facetBy", "question"},
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
    if recipe in QUESTION_RECIPES:
        question = _required_string(body.get("question"), "question")
    else:
        question = None
    if recipe == "compare_periods":
        _validate_periods_shape(body.get("periods"))
    facet_by = validate_facet_by(body.get("facetBy"))
    top_k = _validate_top_k(
        body.get("topK", 5 if recipe in QUESTION_RECIPES else 10),
        maximum=20 if recipe in QUESTION_RECIPES else 50,
    )
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
        facets = facet_counts_by_cluster(
            conn,
            cluster_run_id,
            facet_by,
            summarize_run_id=summarize_run_id_for_cluster_run(conn, cluster_run),
        )
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
        elif recipe == "topic_search":
            evidence = await _topic_search(
                request,
                conn,
                graph_id,
                input_refs.get("embeddingRunId"),
                cluster_run_id,
                question,
                top_k,
                summaries,
                labels,
            )
        elif recipe == "question_evidence":
            ranked_topics = await _topic_search(
                request,
                conn,
                graph_id,
                input_refs.get("embeddingRunId"),
                cluster_run_id,
                question,
                top_k,
                summaries,
                labels,
            )
            top_match = ranked_topics[0] if ranked_topics else None
            if top_match is None or top_match["similarity"] < QUESTION_EVIDENCE_SIMILARITY_FLOOR:
                evidence = {
                    "question": question,
                    "similarityFloor": QUESTION_EVIDENCE_SIMILARITY_FLOOR,
                    "candidates": ranked_topics,
                }
                freshness = _freshness(conn, graph_id, view_id, cluster_run)
                _persist_analysis_event(
                    db_path,
                    graph_id,
                    view_id,
                    recipe,
                    body,
                    run_refs,
                    evidence,
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "message": "no topic matches; closest were returned as candidates",
                        "similarityFloor": QUESTION_EVIDENCE_SIMILARITY_FLOOR,
                        "candidates": ranked_topics,
                        "freshness": freshness,
                        "runRefs": run_refs,
                    },
                )
            topic_evidence = _topic_evidence(
                conn,
                graph_id,
                view_id,
                cluster_run_id,
                top_match["clusterId"],
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
                topic_evidence["clusterId"],
            )
            topic_evidence["trend"] = topic_trend
            if topic_trend is not None:
                run_refs["trendRunId"] = topic_trend["trendRunId"]
            evidence = {
                "question": question,
                "similarityFloor": QUESTION_EVIDENCE_SIMILARITY_FLOOR,
                "match": top_match,
                "topicEvidence": topic_evidence,
                "runnerUpTopics": ranked_topics[1:],
            }
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


async def _topic_search(
    request: Request,
    conn: Any,
    graph_id: str,
    embedding_run_id: str | None,
    cluster_run_id: str,
    question: str,
    top_k: int,
    summaries: dict[int, dict[str, Any]],
    labels: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    if embedding_run_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cluster run has no embedding run reference",
        )
    embedding_run = _resolve_embedding_run(conn, graph_id, embedding_run_id)
    embedding_params = json.loads(embedding_run["params_json"])
    embedding_config = embedding_params["embedding"]
    dimensions = int(embedding_config.get("dimensions") or 1536)
    provider = request.app.state.run_executor.embedding_provider(embedding_config)
    result = await provider.embed_batch([question])
    vectors = result.vectors if isinstance(result, EmbeddingBatchResult) else result
    if len(vectors) != 1:
        raise RuntimeError("embedding provider returned an unexpected question vector count")
    question_vector = normalize_l2(vectors[0])
    centroids = _topic_centroids(
        request.app.state.settings.db_path,
        conn,
        cluster_run_id,
        embedding_run_id,
        embedding_config["model"],
        dimensions,
        summaries,
    )
    rows = []
    for cluster_id, centroid in centroids.items():
        summary = summaries[cluster_id]
        rows.append(
            {
                "clusterId": cluster_id,
                "label": labels.get(cluster_id),
                "similarity": round(float(np.dot(question_vector, centroid)), 6),
                "size": summary["size"],
                "sourceMix": summary["sourceMix"],
                "representativeRecordIds": summary["representativeRecordIds"],
            }
        )
    return sorted(rows, key=lambda row: (-row["similarity"], row["clusterId"]))[:top_k]


def _resolve_embedding_run(conn: Any, graph_id: str, embedding_run_id: str) -> dict[str, Any]:
    row = fetch_one(
        conn,
        """
        SELECT *
          FROM runs
         WHERE id = ? AND graph_id = ? AND type = 'embed' AND status = 'succeeded'
        """,
        (embedding_run_id, graph_id),
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cluster run embedding reference is unavailable",
        )
    return row


def _topic_centroids(
    db_path: str,
    conn: Any,
    cluster_run_id: str,
    embedding_run_id: str,
    model: str,
    dimensions: int,
    summaries: dict[int, dict[str, Any]],
) -> dict[int, np.ndarray]:
    centroids = {}
    for cluster_id, summary in summaries.items():
        record_ids = summary["representativeRecordIds"]
        _, matrix = load_vectors_for_records(
            db_path,
            embedding_run_id=embedding_run_id,
            model=model,
            dimensions=dimensions,
            record_ids=record_ids,
        )
        if not len(matrix):
            fallback_ids = _member_record_ids(conn, cluster_run_id, cluster_id)
            _, matrix = load_vectors_for_records(
                db_path,
                embedding_run_id=embedding_run_id,
                model=model,
                dimensions=dimensions,
                record_ids=fallback_ids,
            )
        if len(matrix):
            centroids[cluster_id] = normalize_l2(matrix.mean(axis=0))
    return centroids


def _member_record_ids(conn: Any, cluster_run_id: str, cluster_id: int) -> list[str]:
    rows = fetch_all(
        conn,
        """
        SELECT record_id
          FROM cluster_memberships
         WHERE run_id = ? AND cluster_id = ?
         ORDER BY record_id ASC
        """,
        (cluster_run_id, cluster_id),
    )
    return [row["record_id"] for row in rows]


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
    spike_score = top["spike_score"] if top["spike_score"] > 0 else 0.0
    return {
        "trendRunId": trend_run["id"],
        "bucket": summary["bucket"],
        "snapshot": {
            "bucket": summary["bucket"],
            "spikeScore": spike_score,
            "topBucket": top["bucket_start"] if spike_score > 0 else None,
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


def _validate_top_k(value: Any, *, maximum: int = 50) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {"field": "topK", "message": f"must be an integer from 1 to {maximum}"}
            ],
        )
    return value
