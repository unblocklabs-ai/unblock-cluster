from __future__ import annotations

import json
import time
from typing import Any

import umap

from datagraph.core.scope import compile_scope
from datagraph.core.vectors import load_vectors_for_records
from datagraph.db import connect, fetch_all, fetch_one


def execute_layout_job(
    db_path: str,
    run_id: str,
    graph_id: str,
    view_id: str,
    embedding_run_id: str,
    layout_config: dict[str, Any],
    set_default: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    phase_started = _start_phase(db_path, run_id, "loading")
    phase_durations: dict[str, float] = {}
    embedding_run = _load_embedding_run(db_path, graph_id, embedding_run_id)
    model = embedding_run["stats"].get("storageModel", embedding_run["stats"]["model"])
    dimensions = int(embedding_run["stats"]["dimensions"])
    scoped_ids = _load_scoped_record_ids(db_path, graph_id, view_id)
    record_ids, matrix = load_vectors_for_records(
        db_path,
        embedding_run_id=embedding_run_id,
        model=model,
        dimensions=dimensions,
        record_ids=scoped_ids,
    )
    missing_embeddings = len(scoped_ids) - len(record_ids)
    if not record_ids:
        raise RuntimeError(
            "layout run has no records with embeddings; POST /api/graphs/{gid}/embeddings first"
        )
    _finish_phase(phase_durations, "loading", phase_started)
    phase_started = _start_phase(db_path, run_id, "reducing")
    if len(record_ids) == 1:
        points = [(0.0, 0.0)]
    elif len(record_ids) == 2:
        points = [(-1.0, 0.0), (1.0, 0.0)]
    else:
        n_neighbors = min(int(layout_config["nNeighbors"]), len(record_ids) - 1)
        reducer = umap.UMAP(
            n_components=2,
            n_neighbors=max(n_neighbors, 2),
            min_dist=float(layout_config["minDist"]),
            metric="cosine",
            random_state=layout_config.get("seed"),
        )
        embedding = reducer.fit_transform(matrix)
        points = [(float(row[0]), float(row[1])) for row in embedding]

    _finish_phase(phase_durations, "reducing", phase_started)
    phase_started = _start_phase(db_path, run_id, "persisting")
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO layout_points (run_id, record_id, x, y)
            VALUES (?, ?, ?, ?)
            """,
            [
                (run_id, record_id, float(x), float(y))
                for record_id, (x, y) in zip(record_ids, points, strict=True)
            ],
        )
        if set_default:
            conn.execute(
                """
                UPDATE views
                   SET default_embedding_run_id = COALESCE(default_embedding_run_id, ?),
                       default_layout_run_id = ?,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                 WHERE id = ? AND graph_id = ?
                """,
                (embedding_run_id, run_id, view_id, graph_id),
            )
        conn.commit()
    _finish_phase(phase_durations, "persisting", phase_started)
    return {
        "records": len(scoped_ids),
        "population": len(record_ids),
        "missingEmbeddings": missing_embeddings,
        "embeddingRunId": embedding_run_id,
        "phaseDurations": phase_durations,
        "durationSeconds": round(time.monotonic() - started, 6),
    }


def _start_phase(db_path: str, run_id: str, phase: str) -> float:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE runs SET progress_json = ? WHERE id = ?",
            (json.dumps({"state": "running", "phase": phase}, sort_keys=True), run_id),
        )
        conn.commit()
    return time.monotonic()


def _finish_phase(durations: dict[str, float], phase: str, started: float) -> None:
    durations[phase] = round(time.monotonic() - started, 6)


def _load_embedding_run(db_path: str, graph_id: str, embedding_run_id: str) -> dict[str, Any]:
    with connect(db_path) as conn:
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
        raise RuntimeError("embeddingRunId must reference a succeeded embed run")
    row["stats"] = json.loads(row["stats_json"])
    return row


def _load_scoped_record_ids(db_path: str, graph_id: str, view_id: str) -> list[str]:
    with connect(db_path) as conn:
        view = fetch_one(
            conn,
            "SELECT * FROM views WHERE id = ? AND graph_id = ?",
            (view_id, graph_id),
        )
        if view is None:
            raise RuntimeError("view not found")
        where, params = compile_scope(json.loads(view["scope_json"]), alias="r")
        rows = fetch_all(
            conn,
            f"""
            SELECT r.id
              FROM records r
             WHERE r.graph_id = ? AND {where}
             ORDER BY r.timestamp_ms ASC, r.id ASC
            """,
            (graph_id, *params),
        )
    return [row["id"] for row in rows]
