from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import sqlite_vec
import umap

from datagraph.core.config import DEFAULT_GRAPH_CONFIG
from datagraph.runs.cluster import (
    _canonicalize_labels,
    _clustering_space,
    _hdbscan_labels,
    effective_hdbscan_params,
    select_representatives,
)

RUN_COLUMNS = {
    "id",
    "created_at",
    "completed_at",
    "input_digest",
    "model",
    "embedding_fingerprint",
    "dimensions",
    "params_json",
    "stale_at",
}
CLUSTER_COLUMNS = {"run_id", "cluster_id", "size", "mean_probability"}
MEMBERSHIP_COLUMNS = {
    "run_id",
    "hash",
    "seq",
    "cluster_id",
    "probability",
    "outlier_score",
    "x",
    "y",
    "representative_rank",
}
DUPLICATE_OCCURRENCE_COLUMNS = {
    "run_id",
    "content_fingerprint",
    "canonical_hash",
    "canonical_seq",
    "duplicate_hash",
    "duplicate_seq",
}


@dataclass(frozen=True)
class DuplicateOccurrence:
    content_fingerprint: str
    canonical_key: tuple[str, int]
    duplicate_key: tuple[str, int]


@dataclass(frozen=True)
class Population:
    keys: list[tuple[str, int]]
    matrix: np.ndarray
    duplicate_occurrences: list[DuplicateOccurrence]
    model: str
    fingerprint: str
    dimensions: int
    digest: str


@dataclass(frozen=True)
class Analysis:
    labels: np.ndarray
    probabilities: np.ndarray
    outlier_scores: np.ndarray
    points: np.ndarray
    representative_ranks: dict[tuple[str, int], int]


@dataclass(frozen=True)
class AnalysisConfig:
    requested: dict[str, Any]
    resolved: dict[str, Any]
    effective: dict[str, Any]


def _config_error(field: str, message: str) -> ValueError:
    return ValueError(f"{field} {message}")


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _config_error(field, "must be an object")
    return value


def _reject_unknown_keys(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _config_error(field, f"has unknown properties: {', '.join(unknown)}")


def _require_integer(value: Any, field: str, minimum: int, maximum: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise _config_error(field, f"must be an integer from {minimum} to {maximum}")


def _require_number(value: Any, field: str, minimum: float, maximum: float | None) -> None:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < minimum
        or (maximum is not None and float(value) > maximum)
    ):
        upper = f" to {maximum:g}" if maximum is not None else " or greater"
        raise _config_error(field, f"must be a finite number from {minimum:g}{upper}")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid numeric constant {value}")


def _parse_collections_json(collections_json: str | None) -> tuple[str, ...] | None:
    if collections_json is None:
        return None
    try:
        parsed = json.loads(collections_json)
    except json.JSONDecodeError as error:
        raise ValueError(f"--collections-json must be valid JSON: {error}") from error
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("--collections-json must be a non-empty array")
    if any(not isinstance(value, str) or not value.strip() for value in parsed):
        raise ValueError("--collections-json values must be non-empty strings")
    collections = tuple(value.strip() for value in parsed)
    if len(set(collections)) != len(collections):
        raise ValueError("--collections-json values must be unique")
    return collections


def _parse_config_json(config_json: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if config_json is None:
        requested: dict[str, Any] = {}
    else:
        try:
            parsed = json.loads(config_json, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"--config-json must be valid JSON: {error}") from error
        requested = _require_object(parsed, "--config-json")

    _reject_unknown_keys(requested, {"space", "hdbscan", "seed"}, "config")
    space = _require_object(requested.get("space", {}), "config.space")
    hdbscan = _require_object(requested.get("hdbscan", {}), "config.hdbscan")
    _reject_unknown_keys(
        space,
        {"method", "nComponents", "nNeighbors", "minDist"},
        "config.space",
    )
    _reject_unknown_keys(
        hdbscan,
        {
            "minClusterSize",
            "minSamples",
            "clusterSelectionMethod",
            "clusterSelectionEpsilon",
            "allowSingleCluster",
        },
        "config.hdbscan",
    )

    if "method" in space and space["method"] not in {"umap", "none"}:
        raise _config_error('config.space.method', 'must be "umap" or "none"')
    if "nComponents" in space:
        _require_integer(space["nComponents"], "config.space.nComponents", 2, 100)
    if "nNeighbors" in space:
        _require_integer(space["nNeighbors"], "config.space.nNeighbors", 2, 200)
    if "minDist" in space:
        _require_number(space["minDist"], "config.space.minDist", 0, 1)
    if "minClusterSize" in hdbscan:
        _require_integer(
            hdbscan["minClusterSize"], "config.hdbscan.minClusterSize", 2, 100_000
        )
    if "minSamples" in hdbscan:
        _require_integer(hdbscan["minSamples"], "config.hdbscan.minSamples", 1, 100_000)
    if (
        "clusterSelectionMethod" in hdbscan
        and hdbscan["clusterSelectionMethod"] not in {"eom", "leaf"}
    ):
        raise _config_error(
            "config.hdbscan.clusterSelectionMethod", 'must be "eom" or "leaf"'
        )
    if "clusterSelectionEpsilon" in hdbscan:
        _require_number(
            hdbscan["clusterSelectionEpsilon"],
            "config.hdbscan.clusterSelectionEpsilon",
            0,
            None,
        )
    if "allowSingleCluster" in hdbscan and not isinstance(
        hdbscan["allowSingleCluster"], bool
    ):
        raise _config_error("config.hdbscan.allowSingleCluster", "must be a boolean")
    if "seed" in requested:
        _require_integer(requested["seed"], "config.seed", 0, 4_294_967_295)

    resolved = deepcopy(DEFAULT_GRAPH_CONFIG["cluster"])
    resolved["space"].update(space)
    resolved["hdbscan"].update(hdbscan)
    if "seed" in requested:
        resolved["seed"] = requested["seed"]
    resolved["space"].pop("metric", None)
    return deepcopy(requested), resolved


def _resolve_config(
    requested: dict[str, Any], resolved: dict[str, Any], population: int
) -> AnalysisConfig:
    for field in ("minClusterSize", "minSamples"):
        value = resolved["hdbscan"][field]
        if value is not None and value > population:
            raise _config_error(
                f"config.hdbscan.{field}", f"must not exceed population {population}"
            )

    effective = deepcopy(resolved)
    if effective["space"]["method"] == "umap":
        effective["space"]["nComponents"] = min(
            int(effective["space"]["nComponents"]), population - 2
        )
        effective["space"]["nNeighbors"] = min(
            int(effective["space"]["nNeighbors"]), population - 1
        )
    effective_size, effective_samples = effective_hdbscan_params(effective, population)
    effective["hdbscan"]["minClusterSize"] = min(effective_size, population)
    effective["hdbscan"]["minSamples"] = min(effective_samples, population)
    effective["space"]["metric"] = "cosine"
    return AnalysisConfig(requested=requested, resolved=resolved, effective=effective)


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _active_vector_rows(
    conn: sqlite3.Connection, collections: tuple[str, ...] | None = None
) -> list[sqlite3.Row]:
    required = {"content", "documents", "content_vectors", "vectors_vec"}
    actual = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')"
        )
    }
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(f"not a supported QMD index; missing tables: {', '.join(missing)}")
    collection_filter = ""
    parameters: tuple[str, ...] = ()
    if collections is not None:
        placeholders = ", ".join("?" for _ in collections)
        collection_filter = f" AND collection IN ({placeholders})"
        parameters = collections
    rows = conn.execute(
        f"""
        WITH active_hashes AS (
          SELECT DISTINCT hash
          FROM documents
          WHERE active = 1{collection_filter}
        )
        SELECT
          ah.hash AS active_hash,
          cv.hash,
          cv.seq,
          cv.pos,
          cv.chunk_len,
          cv.total_chunks,
          cv.model,
          cv.embed_fingerprint,
          cv.embedded_at,
          c.hash AS content_hash,
          vv.embedding
        FROM active_hashes ah
        LEFT JOIN content c ON c.hash = ah.hash
        LEFT JOIN content_vectors cv ON cv.hash = ah.hash
        LEFT JOIN vectors_vec vv ON vv.hash_seq = cv.hash || '_' || cv.seq
        ORDER BY ah.hash, cv.seq
        """,
        parameters,
    ).fetchall()

    validated: list[sqlite3.Row] = []
    start = 0
    while start < len(rows):
        active_hash = str(rows[start]["active_hash"])
        end = start + 1
        while end < len(rows) and rows[end]["active_hash"] == active_hash:
            end += 1
        hash_rows = rows[start:end]

        if hash_rows[0]["seq"] is None:
            raise RuntimeError(f"active QMD content hash {active_hash} has no embedded chunks")
        if hash_rows[0]["content_hash"] is None:
            raise RuntimeError(f"active QMD content hash {active_hash} has no source content")

        total_chunks = {int(row["total_chunks"]) for row in hash_rows}
        if len(total_chunks) != 1 or next(iter(total_chunks)) <= 0:
            raise RuntimeError(
                f"active QMD content hash {active_hash} has inconsistent or invalid total_chunks"
            )
        expected_sequences = list(range(next(iter(total_chunks))))
        actual_sequences = [int(row["seq"]) for row in hash_rows]
        if actual_sequences != expected_sequences:
            raise RuntimeError(
                f"active QMD content hash {active_hash} has incomplete chunk sequences; "
                f"expected {expected_sequences}, found {actual_sequences}"
            )

        missing_vector = next(
            (row for row in hash_rows if row["embedding"] is None),
            None,
        )
        if missing_vector is not None:
            raise RuntimeError(
                "active QMD content vector "
                f"{active_hash}:{missing_vector['seq']} has no vectors_vec embedding"
            )

        validated.extend(hash_rows)
        start = end
    return validated


def _digest_rows(rows: list[sqlite3.Row]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        vector = bytes(row["embedding"])
        fields = (
            row["hash"],
            row["seq"],
            row["pos"],
            row["chunk_len"],
            row["total_chunks"],
            row["model"],
            row["embed_fingerprint"],
            row["embedded_at"],
            len(vector),
        )
        digest.update(json.dumps(fields, separators=(",", ":")).encode())
        digest.update(b"\0")
        digest.update(vector)
    return digest.hexdigest()


def _slice_utf16(encoded: bytes, pos: int, length: int, identity: str) -> str:
    start = pos * 2
    end = (pos + length) * 2
    if pos < 0 or length <= 0 or end > len(encoded):
        raise RuntimeError(f"active QMD content vector {identity} has invalid chunk bounds")
    try:
        return encoded[start:end].decode("utf-16-le")
    except UnicodeDecodeError as error:
        raise RuntimeError(
            f"active QMD content vector {identity} splits a UTF-16 surrogate pair"
        ) from error


def _load_population(
    conn: sqlite3.Connection, collections: tuple[str, ...] | None = None
) -> Population:
    rows = _active_vector_rows(conn, collections)
    if not rows:
        raise RuntimeError("QMD index has no embedded chunks for active documents")

    spaces = {(row["model"], row["embed_fingerprint"]) for row in rows}
    if len(spaces) != 1:
        raise RuntimeError("active QMD vectors use mixed embedding spaces")
    model, fingerprint = next(iter(spaces))
    if not model or not fingerprint:
        raise RuntimeError("active QMD vectors must have a model and embedding fingerprint")

    vectors: list[np.ndarray] = []
    keys: list[tuple[str, int]] = []
    duplicate_occurrences: list[DuplicateOccurrence] = []
    canonical_by_fingerprint: dict[str, tuple[str, int]] = {}
    encoded_doc = b""
    document_hash: str | None = None
    dimensions: int | None = None
    for row in rows:
        key = (str(row["hash"]), int(row["seq"]))
        identity = f"{key[0]}:{key[1]}"
        if key[0] != document_hash:
            doc = conn.execute(
                "SELECT doc FROM content WHERE hash = ?", (key[0],)
            ).fetchone()
            if doc is None:
                raise RuntimeError(f"active QMD content hash {key[0]} has no source content")
            encoded_doc = str(doc["doc"]).encode("utf-16-le")
            document_hash = key[0]
        blob = bytes(row["embedding"])
        if not blob or len(blob) % np.dtype(np.float32).itemsize:
            raise RuntimeError(f"invalid vector bytes for {identity}")
        vector = np.frombuffer(blob, dtype=np.float32).copy()
        dimensions = dimensions or len(vector)
        if len(vector) != dimensions or not np.all(np.isfinite(vector)):
            raise RuntimeError("active QMD vectors have invalid or mixed dimensions")
        norm = np.linalg.norm(vector)
        if norm == 0:
            raise RuntimeError(f"zero vector for {identity}")

        chunk_text = _slice_utf16(
            encoded_doc, int(row["pos"]), int(row["chunk_len"]), identity
        )
        content_fingerprint = hashlib.sha256(chunk_text.encode()).hexdigest()
        canonical_key = canonical_by_fingerprint.get(content_fingerprint)
        if canonical_key is not None:
            duplicate_occurrences.append(
                DuplicateOccurrence(content_fingerprint, canonical_key, key)
            )
            continue

        canonical_by_fingerprint[content_fingerprint] = key
        keys.append(key)
        vectors.append((vector / norm).astype(np.float32, copy=False))

    if len(keys) < 5:
        raise RuntimeError("memory analysis requires at least 5 unique active embedded chunks")
    return Population(
        keys=keys,
        matrix=np.vstack(vectors).astype(np.float32, copy=False),
        duplicate_occurrences=duplicate_occurrences,
        model=str(model),
        fingerprint=str(fingerprint),
        dimensions=dimensions,
        digest=_digest_rows(rows),
    )


def _layout(matrix: np.ndarray, config: dict[str, Any]) -> np.ndarray:
    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=max(min(int(config["nNeighbors"]), len(matrix) - 1), 2),
        min_dist=float(config["minDist"]),
        metric="cosine",
        random_state=config.get("seed"),
    )
    return reducer.fit_transform(matrix).astype(np.float32, copy=False)


def _analyze(population: Population, config: AnalysisConfig) -> Analysis:
    cluster_config = config.effective
    layout_config = {**DEFAULT_GRAPH_CONFIG["layout"], "seed": cluster_config["seed"]}

    clustering_space = _clustering_space(population.matrix, cluster_config)

    labels, probabilities, outlier_scores = _hdbscan_labels(
        clustering_space, cluster_config, len(population.keys)
    )
    identities = [f"{hash_}\0{seq}" for hash_, seq in population.keys]
    labels = _canonicalize_labels(identities, labels)

    points = _layout(population.matrix, layout_config)

    representative_ranks: dict[tuple[str, int], int] = {}
    key_by_identity = dict(zip(identities, population.keys, strict=True))
    for cluster_id in sorted({int(label) for label in labels if int(label) != -1}):
        indices = np.flatnonzero(labels == cluster_id)
        member_ids = [identities[int(index)] for index in indices]
        representatives = select_representatives(
            member_ids,
            population.matrix[indices],
            probabilities[indices],
            top_k=50,
        )
        representative_ranks.update(
            {key_by_identity[identity]: rank for rank, identity in enumerate(representatives, 1)}
        )

    return Analysis(
        labels=labels,
        probabilities=probabilities,
        outlier_scores=outlier_scores,
        points=points,
        representative_ranks=representative_ranks,
    )


def _validate_schema(conn: sqlite3.Connection) -> None:
    required_tables = {
        "memory_analysis_runs",
        "memory_analysis_clusters",
        "memory_analysis_duplicate_occurrences",
        "memory_analysis_memberships",
    }
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = sorted(required_tables - tables)
    if missing:
        raise RuntimeError(
            "Unblock Memory has not initialized its analysis schema; missing tables: "
            + ", ".join(missing)
        )
    expected = {
        "memory_analysis_runs": RUN_COLUMNS,
        "memory_analysis_clusters": CLUSTER_COLUMNS,
        "memory_analysis_duplicate_occurrences": DUPLICATE_OCCURRENCE_COLUMNS,
        "memory_analysis_memberships": MEMBERSHIP_COLUMNS,
    }
    for table, columns in expected.items():
        actual = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing_columns = sorted(columns - actual)
        if missing_columns:
            raise RuntimeError(
                f"incompatible {table} schema; missing columns: "
                + ", ".join(missing_columns)
            )


def _persist(
    conn: sqlite3.Connection,
    population: Population,
    analysis: Analysis,
    config: AnalysisConfig,
    run_id: str,
    created_at: str,
    collections: tuple[str, ...] | None = None,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        params = {
            "requested": config.requested,
            "resolved": config.resolved,
            "effective": config.effective,
        }
        if collections is not None:
            params["collections"] = sorted(collections)
        completed_at = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO memory_analysis_runs (
              id, created_at, completed_at, input_digest, model,
              embedding_fingerprint, dimensions, params_json, stale_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                run_id,
                created_at,
                completed_at,
                population.digest,
                population.model,
                population.fingerprint,
                population.dimensions,
                json.dumps(params, separators=(",", ":"), sort_keys=True),
            ),
        )

        cluster_rows = []
        for cluster_id in sorted(
            {int(label) for label in analysis.labels if int(label) != -1}
        ):
            indices = np.flatnonzero(analysis.labels == cluster_id)
            cluster_rows.append(
                (
                    run_id,
                    cluster_id,
                    len(indices),
                    float(analysis.probabilities[indices].mean()),
                )
            )
        conn.executemany(
            """
            INSERT INTO memory_analysis_clusters
              (run_id, cluster_id, size, mean_probability)
            VALUES (?, ?, ?, ?)
            """,
            cluster_rows,
        )
        conn.executemany(
            """
            INSERT INTO memory_analysis_memberships (
              run_id, hash, seq, cluster_id, probability, outlier_score,
              x, y, representative_rank
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    hash_,
                    seq,
                    int(label),
                    float(probability),
                    float(outlier_score),
                    float(point[0]),
                    float(point[1]),
                    analysis.representative_ranks.get((hash_, seq)),
                )
                for (hash_, seq), label, probability, outlier_score, point in zip(
                    population.keys,
                    analysis.labels,
                    analysis.probabilities,
                    analysis.outlier_scores,
                    analysis.points,
                    strict=True,
                )
            ],
        )
        conn.executemany(
            """
            INSERT INTO memory_analysis_duplicate_occurrences (
              run_id, content_fingerprint, canonical_hash, canonical_seq,
              duplicate_hash, duplicate_seq
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    occurrence.content_fingerprint,
                    *occurrence.canonical_key,
                    *occurrence.duplicate_key,
                )
                for occurrence in population.duplicate_occurrences
            ],
        )
        conn.execute("DELETE FROM memory_analysis_runs WHERE id <> ?", (run_id,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def analyze_database(
    db_path: Path | str,
    config_json: str | None = None,
    collections_json: str | None = None,
) -> None:
    requested, resolved = _parse_config_json(config_json)
    collections = _parse_collections_json(collections_json)
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"QMD index does not exist: {path}")

    created_at = datetime.now(UTC).isoformat()
    run_id = str(uuid.uuid4())
    with _connect(path) as conn:
        _validate_schema(conn)
        population = _load_population(conn, collections)
        config = _resolve_config(requested, resolved, len(population.keys))
        analysis = _analyze(population, config)
        _persist(conn, population, analysis, config, run_id, created_at, collections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unblock-memory-analysis")
    parser.add_argument("--db", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument("--config-json", help=argparse.SUPPRESS)
    parser.add_argument("--collections-json", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        analyze_database(args.db, args.config_json, args.collections_json)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        print(f"unblock-memory-analysis: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
