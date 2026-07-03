from __future__ import annotations

from pathlib import Path

import numpy as np

from datagraph.db import connect

VECTOR_QUERY_CHUNK_SIZE = 500


def normalize_l2(vector: np.ndarray | list[float]) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(array)
    if norm == 0:
        return array.astype(np.float32, copy=False)
    return (array / norm).astype(np.float32, copy=False)


def pack_vector(vector: np.ndarray | list[float]) -> bytes:
    return normalize_l2(vector).astype(np.float32, copy=False).tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def chunked(values: list[str], size: int = VECTOR_QUERY_CHUNK_SIZE) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def existing_vector_hashes(
    db_path: Path | str,
    *,
    model: str,
    dimensions: int,
    text_hashes: list[str],
) -> set[str]:
    if not text_hashes:
        return set()
    existing: set[str] = set()
    with connect(db_path) as conn:
        for chunk in chunked(text_hashes):
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT text_hash
                  FROM embedding_vectors
                 WHERE model = ? AND dimensions = ? AND text_hash IN ({placeholders})
                """,
                (model, dimensions, *chunk),
            ).fetchall()
            existing.update(row["text_hash"] for row in rows)
    return existing


def load_vectors_for_records(
    db_path: Path | str,
    *,
    embedding_run_id: str,
    model: str,
    dimensions: int,
    record_ids: list[str],
) -> tuple[list[str], np.ndarray]:
    if not record_ids:
        return [], np.empty((0, dimensions), dtype=np.float32)

    vectors_by_record: dict[str, np.ndarray] = {}
    with connect(db_path) as conn:
        for chunk in chunked(record_ids):
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT ei.record_id, ev.vector
                  FROM embedding_items ei
                  JOIN embedding_vectors ev
                    ON ev.model = ?
                   AND ev.dimensions = ?
                   AND ev.text_hash = ei.text_hash
                 WHERE ei.run_id = ?
                   AND ei.record_id IN ({placeholders})
                """,
                (model, dimensions, embedding_run_id, *chunk),
            ).fetchall()
            for row in rows:
                vectors_by_record[row["record_id"]] = unpack_vector(row["vector"]).copy()

    aligned_ids = [record_id for record_id in record_ids if record_id in vectors_by_record]
    if not aligned_ids:
        return [], np.empty((0, dimensions), dtype=np.float32)
    matrix = np.vstack([vectors_by_record[record_id] for record_id in aligned_ids]).astype(
        np.float32,
        copy=False,
    )
    return aligned_ids, matrix
