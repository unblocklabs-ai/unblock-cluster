from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from datagraph.core.ids import now_iso
from datagraph.core.trend_math import compute_trends
from datagraph.db import connect, fetch_all, fetch_one

ProgressCallback = Callable[[str, dict[str, Any]], None]


def execute_trend_run(
    *,
    db_path: Path,
    run_id: str,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
    time_config: dict[str, Any],
    window: dict[str, str] | None,
    update_progress: ProgressCallback,
    set_default: bool = True,
) -> dict[str, Any]:
    update_progress(run_id, {"state": "running", "phase": "loading"})
    _load_cluster_run(db_path, graph_id, view_id, cluster_run_id)
    memberships = _load_memberships(db_path, cluster_run_id)
    if not memberships:
        raise RuntimeError("trend run has no cluster memberships to analyze")

    update_progress(run_id, {"state": "running", "phase": "computing"})
    computation = compute_trends(
        memberships,
        bucket=time_config["bucket"],
        window_start=window["start"] if window else None,
        window_end=window["end"] if window else None,
    )
    summary = computation.summary

    update_progress(run_id, {"state": "running", "phase": "persisting"})
    with connect(db_path) as conn:
        conn.execute("DELETE FROM trend_results WHERE run_id = ?", (run_id,))
        conn.executemany(
            """
            INSERT INTO trend_results (run_id, cluster_id, bucket_start, count, share, spike_score)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    point.cluster_id,
                    point.bucket_start,
                    point.count,
                    point.share,
                    point.spike_score,
                )
                for point in computation.points
            ],
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO trend_summaries (run_id, summary_json)
            VALUES (?, ?)
            """,
            (run_id, json.dumps(summary, sort_keys=True)),
        )
        if set_default:
            conn.execute(
                """
                UPDATE views
                   SET default_trend_run_id = ?, updated_at = ?
                 WHERE id = ? AND graph_id = ?
                """,
                (run_id, now_iso(), view_id, graph_id),
            )
        conn.commit()

    return {
        "population": len(memberships),
        "bucket": computation.bucket,
        "bucketCount": len(computation.buckets),
        "clusterCount": len({point.cluster_id for point in computation.points}),
        "window": summary["window"],
    }


def _load_cluster_run(
    db_path: Path,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
) -> dict:
    with connect(db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT *
              FROM runs
             WHERE id = ? AND graph_id = ? AND view_id = ?
               AND type = 'cluster' AND status = 'succeeded'
            """,
            (cluster_run_id, graph_id, view_id),
        )
    if row is None:
        raise RuntimeError("clusterRunId must reference a succeeded cluster run for this view")
    return row


def _load_memberships(db_path: Path, cluster_run_id: str) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
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
