from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

import hdbscan
import numpy as np
import umap

from datagraph.core.scope import compile_scope
from datagraph.core.vectors import load_vectors_for_records, normalize_l2
from datagraph.db import connect, fetch_all, fetch_one


def execute_cluster_job(
    db_path: str,
    run_id: str,
    graph_id: str,
    view_id: str,
    embedding_run_id: str,
    cluster_config: dict[str, Any],
    set_default: bool = True,
    focus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    phase_started = _start_phase(db_path, run_id, "loading")
    phase_durations: dict[str, float] = {}
    embedding_run = _load_embedding_run(db_path, graph_id, embedding_run_id)
    model = embedding_run["stats"]["model"]
    dimensions = int(embedding_run["stats"]["dimensions"])
    scoped_records = _load_scoped_records(db_path, graph_id, view_id, focus=focus)
    scoped_ids = [record["id"] for record in scoped_records]
    vector_record_ids, matrix = load_vectors_for_records(
        db_path,
        embedding_run_id=embedding_run_id,
        model=model,
        dimensions=dimensions,
        record_ids=scoped_ids,
    )
    missing_embeddings = len(scoped_ids) - len(vector_record_ids)
    population = len(vector_record_ids)
    if population == 0:
        raise RuntimeError(
            "cluster run has no records with embeddings; POST /api/graphs/{gid}/embeddings first"
        )
    if cluster_config["space"]["method"] == "umap" and population < 5:
        raise RuntimeError("cluster run requires at least 5 embedded records when using UMAP space")

    vector_records = {
        record["id"]: record for record in scoped_records if record["id"] in vector_record_ids
    }
    _finish_phase(phase_durations, "loading", phase_started)
    phase_started = _start_phase(db_path, run_id, "reducing")
    clustering_space = _clustering_space(matrix, cluster_config)
    _finish_phase(phase_durations, "reducing", phase_started)
    phase_started = _start_phase(db_path, run_id, "clustering")
    labels, probabilities, outlier_scores = _hdbscan_labels(
        clustering_space,
        cluster_config,
        population,
    )
    labels = _canonicalize_labels(vector_record_ids, labels)
    _finish_phase(phase_durations, "clustering", phase_started)
    phase_started = _start_phase(db_path, run_id, "persisting")
    _persist_memberships(db_path, run_id, vector_record_ids, labels, probabilities, outlier_scores)
    summaries = _summaries(
        vector_record_ids,
        matrix,
        labels,
        probabilities,
        vector_records,
    )
    _persist_summaries(db_path, run_id, summaries)

    effective_min_cluster_size, effective_min_samples = effective_hdbscan_params(
        cluster_config,
        population,
    )
    noise_count = sum(1 for label in labels if int(label) == -1)
    cluster_count = len({int(label) for label in labels if int(label) != -1})
    if set_default:
        with connect(db_path) as conn:
            conn.execute(
                """
                UPDATE views
                   SET default_embedding_run_id = ?,
                       default_cluster_run_id = ?,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                 WHERE id = ? AND graph_id = ?
                """,
                (embedding_run_id, run_id, view_id, graph_id),
            )
            conn.commit()
    _finish_phase(phase_durations, "persisting", phase_started)
    stats = {
        "records": len(scoped_ids),
        "population": population,
        "missingEmbeddings": missing_embeddings,
        "clusterCount": cluster_count,
        "noiseCount": noise_count,
        "noiseRatio": noise_count / population if population else 0,
        "embeddingRunId": embedding_run_id,
        "effectiveHdbscan": {
            "minClusterSize": effective_min_cluster_size,
            "minSamples": effective_min_samples,
        },
        "params": {
            "embeddingRunId": embedding_run_id,
            "cluster": cluster_config,
            "setDefault": set_default,
            **({"focus": focus} if focus is not None else {}),
        },
        "phaseDurations": phase_durations,
        "durationSeconds": round(time.monotonic() - started, 6),
    }
    return stats


def select_representatives(
    member_ids: list[str],
    member_vectors: np.ndarray,
    probabilities: np.ndarray,
    *,
    top_k: int = 20,
    probability_floor: float = 0.7,
) -> list[str]:
    if not member_ids:
        return []
    mask = probabilities >= probability_floor
    candidate_indices = np.flatnonzero(mask)
    if len(candidate_indices) == 0:
        candidate_indices = np.arange(len(member_ids))
    centroid = normalize_l2(member_vectors[candidate_indices].mean(axis=0))
    scores = member_vectors[candidate_indices] @ centroid
    ordered = candidate_indices[np.argsort(-scores)]
    return [member_ids[int(index)] for index in ordered[:top_k]]


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


def _load_scoped_records(
    db_path: str,
    graph_id: str,
    view_id: str,
    *,
    focus: dict[str, Any] | None = None,
) -> list[dict]:
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
            SELECT *
              FROM records r
             WHERE r.graph_id = ? AND {where}
             ORDER BY r.timestamp_ms ASC, r.id ASC
            """,
            (graph_id, *params),
        )
        if focus is None:
            return rows
        focus_rows = fetch_all(
            conn,
            """
            SELECT record_id
              FROM cluster_memberships
             WHERE run_id = ? AND cluster_id = ?
            """,
            (focus["clusterRunId"], int(focus["clusterId"])),
        )
        focus_ids = {row["record_id"] for row in focus_rows}
        return [row for row in rows if row["id"] in focus_ids]


def _clustering_space(matrix: np.ndarray, config: dict[str, Any]) -> np.ndarray:
    if config["space"]["method"] == "none":
        return matrix
    n_neighbors = min(int(config["space"]["nNeighbors"]), len(matrix) - 1)
    reducer = umap.UMAP(
        n_components=int(config["space"]["nComponents"]),
        n_neighbors=max(n_neighbors, 2),
        min_dist=float(config["space"]["minDist"]),
        metric=config["space"]["metric"],
        random_state=config.get("seed"),
    )
    return reducer.fit_transform(matrix).astype(np.float32, copy=False)


def effective_hdbscan_params(config: dict[str, Any], population: int) -> tuple[int, int]:
    """Resolve null minClusterSize/minSamples to population-scaled defaults.

    Defaults retuned on the real-embedding quality eval (2026-07-03): the old
    0.2% floor-10 default, with minSamples following minClusterSize,
    over-split 20 planted topics into 80 pure fragments at 5k. 0.5% (floored
    at 15, capped at 150 so 100k-scale datasets keep emerging-topic
    detection) plus a decoupled, smaller minSamples smooths the density
    estimate without erasing small topics.
    """
    min_cluster_size = config["hdbscan"]["minClusterSize"]
    min_samples = config["hdbscan"]["minSamples"]
    effective_size = int(min_cluster_size or min(150, max(15, round(0.005 * population))))
    effective_samples = min(10, effective_size) if min_samples is None else int(min_samples)
    return effective_size, effective_samples


def _hdbscan_labels(
    clustering_space: np.ndarray,
    config: dict[str, Any],
    population: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    min_cluster_size, min_samples = effective_hdbscan_params(config, population)
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
        cluster_selection_method=config["hdbscan"]["clusterSelectionMethod"],
        cluster_selection_epsilon=float(config["hdbscan"]["clusterSelectionEpsilon"]),
        allow_single_cluster=bool(config["hdbscan"]["allowSingleCluster"]),
    )
    labels = clusterer.fit_predict(clustering_space)
    probabilities = np.asarray(
        getattr(clusterer, "probabilities_", np.ones(population)),
        dtype=np.float32,
    )
    outlier_scores = np.asarray(
        getattr(clusterer, "outlier_scores_", np.zeros(population)),
        dtype=np.float32,
    )
    outlier_scores = np.nan_to_num(outlier_scores, nan=0.0, posinf=0.0, neginf=0.0)
    probabilities = np.clip(np.nan_to_num(probabilities, nan=0.0), 0.0, 1.0)
    return labels, probabilities, outlier_scores


def _canonicalize_labels(record_ids: list[str], labels: np.ndarray) -> np.ndarray:
    cluster_keys: list[tuple[str, int]] = []
    for label in sorted({int(label) for label in labels if int(label) != -1}):
        member_ids = [
            record_id
            for record_id, candidate_label in zip(record_ids, labels, strict=True)
            if int(candidate_label) == label
        ]
        cluster_keys.append((min(member_ids), label))
    mapping = {
        old_label: new_label
        for new_label, (_, old_label) in enumerate(sorted(cluster_keys))
    }
    return np.asarray([mapping.get(int(label), -1) for label in labels], dtype=np.int64)


def _persist_memberships(
    db_path: str,
    run_id: str,
    record_ids: list[str],
    labels: np.ndarray,
    probabilities: np.ndarray,
    outlier_scores: np.ndarray,
) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO cluster_memberships (
              run_id, record_id, cluster_id, probability, outlier_score, is_noise
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    record_id,
                    int(label),
                    float(probability),
                    float(outlier_score),
                    1 if int(label) == -1 else 0,
                )
                for record_id, label, probability, outlier_score in zip(
                    record_ids,
                    labels,
                    probabilities,
                    outlier_scores,
                    strict=True,
                )
            ],
        )
        conn.commit()


def _summaries(
    record_ids: list[str],
    matrix: np.ndarray,
    labels: np.ndarray,
    probabilities: np.ndarray,
    records_by_id: dict[str, dict],
) -> list[dict[str, Any]]:
    summaries = []
    for cluster_id in sorted({int(label) for label in labels if int(label) != -1}):
        indices = np.flatnonzero(labels == cluster_id)
        member_ids = [record_ids[int(index)] for index in indices]
        member_probabilities = probabilities[indices]
        representative_ids = select_representatives(
            member_ids,
            matrix[indices],
            member_probabilities,
            top_k=20,
        )
        source_mix = Counter(records_by_id[record_id]["source_type"] for record_id in member_ids)
        mean_probability = float(member_probabilities.mean()) if len(member_probabilities) else 0
        summaries.append(
            {
                "clusterId": cluster_id,
                "size": len(member_ids),
                "meanProbability": mean_probability,
                "representativeRecordIds": representative_ids,
                "sourceMix": dict(sorted(source_mix.items())),
            }
        )
    return summaries


def _persist_summaries(db_path: str, run_id: str, summaries: list[dict[str, Any]]) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO cluster_summaries (
              run_id, cluster_id, size, mean_probability,
              representative_record_ids_json, source_mix_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    summary["clusterId"],
                    summary["size"],
                    summary["meanProbability"],
                    json.dumps(summary["representativeRecordIds"]),
                    json.dumps(summary["sourceMix"], sort_keys=True),
                )
                for summary in summaries
            ],
        )
        conn.commit()
