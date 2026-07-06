from __future__ import annotations

import json
from typing import Any

from datagraph.core.trend_math import TREND_MATH_VERSION
from datagraph.db import fetch_one


def resolved_run_warnings(
    conn: Any,
    *,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
) -> list[str]:
    warnings: list[str] = []
    label_count = conn.execute(
        "SELECT COUNT(*) FROM cluster_labels WHERE cluster_run_id = ?",
        (cluster_run_id,),
    ).fetchone()[0]
    if label_count == 0:
        warnings.append(
            f"cluster run {cluster_run_id} has no labels; "
            f"POST /api/graphs/{graph_id}/views/{view_id}/label to label topics"
        )

    view = fetch_one(
        conn,
        """
        SELECT default_label_run_id, default_trend_run_id
          FROM views
         WHERE id = ? AND graph_id = ?
        """,
        (view_id, graph_id),
    )
    if view is None:
        return warnings

    if view["default_label_run_id"] is not None:
        label_run = fetch_one(
            conn,
            """
            SELECT input_refs_json
              FROM runs
             WHERE id = ? AND graph_id = ? AND view_id = ?
               AND type = 'label' AND status = 'succeeded'
            """,
            (view["default_label_run_id"], graph_id, view_id),
        )
        if label_run is not None:
            labeled_cluster_run_id = json.loads(label_run["input_refs_json"]).get("clusterRunId")
            if labeled_cluster_run_id != cluster_run_id:
                warnings.append(
                    f"default label run {view['default_label_run_id']} labels cluster run "
                    f"{labeled_cluster_run_id}; resolved cluster run is {cluster_run_id}, "
                    "so labels may be absent"
                )

    if view["default_trend_run_id"] is not None:
        trend_run = fetch_one(
            conn,
            """
            SELECT id, input_refs_json, stats_json
              FROM runs
             WHERE id = ? AND graph_id = ? AND view_id = ?
               AND type = 'trend' AND status = 'succeeded'
            """,
            (view["default_trend_run_id"], graph_id, view_id),
        )
        if trend_run is not None:
            trended_cluster_run_id = json.loads(trend_run["input_refs_json"]).get("clusterRunId")
            if trended_cluster_run_id != cluster_run_id:
                warnings.append(
                    f"default trend run {view['default_trend_run_id']} trends cluster run "
                    f"{trended_cluster_run_id}; resolved cluster run is {cluster_run_id}, "
                    "so trend snapshots are absent"
                )
            warnings.extend(trend_math_version_warnings(trend_run, graph_id, view_id))

    return warnings


def trend_math_version_warnings(
    trend_run: dict[str, Any],
    graph_id: str,
    view_id: str,
) -> list[str]:
    stats = json.loads(trend_run["stats_json"] or "{}")
    math_version = stats.get("mathVersion")
    old_version = math_version if isinstance(math_version, int) else 1
    if old_version >= TREND_MATH_VERSION:
        return []
    return [
        f"trend run {trend_run['id']} was computed with older trend math "
        f"(v{old_version} < v{TREND_MATH_VERSION}); "
        f"POST /api/graphs/{graph_id}/views/{view_id}/trends to recompute spike scores"
    ]
