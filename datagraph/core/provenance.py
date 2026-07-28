from __future__ import annotations

import json
import sqlite3
from typing import Any

from datagraph.core.vectors import chunked
from datagraph.db import fetch_one


def external_provenance_by_record(
    conn: sqlite3.Connection,
    record_ids: list[str],
    *,
    embedding_run_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    if not record_ids:
        return {}
    selected_import_id = None
    if embedding_run_id is not None:
        row = fetch_one(
            conn,
            "SELECT id FROM external_imports WHERE embedding_run_id = ?",
            (embedding_run_id,),
        )
        selected_import_id = row["id"] if row else None

    result: dict[str, dict[str, Any]] = {}
    for record_chunk in chunked(record_ids):
        placeholders = ", ".join("?" for _ in record_chunk)
        rows = conn.execute(
            f"""
            SELECT ecv.*, ed.dataset_key, ed.format, ed.source_identity_json,
                   es.model, es.fingerprint, es.dimensions, es.dtype,
                   es.distance_metric, es.normalization,
                   ei.id AS bundle_import_id, ei.export_id, ei.exported_at,
                   ei.schema_name, ei.schema_version, ei.exporter_name, ei.exporter_version
              FROM external_chunk_versions ecv
              JOIN external_datasets ed ON ed.id = ecv.dataset_id
              JOIN embedding_spaces es ON es.id = ecv.embedding_space_id
              JOIN external_imports ei
                ON ei.id = CASE
                  WHEN ? IS NOT NULL AND EXISTS (
                    SELECT 1 FROM external_import_items eii
                     WHERE eii.import_id = ? AND eii.record_id = ecv.record_id
                  ) THEN ?
                  ELSE ecv.introduced_import_id
                END
             WHERE ecv.record_id IN ({placeholders})
            """,
            (selected_import_id, selected_import_id, selected_import_id, *record_chunk),
        ).fetchall()
        for row in rows:
            result[row["record_id"]] = {
                "provider": "external",
                "format": row["format"],
                "dataset": row["dataset_key"],
                "sourceIdentity": json.loads(row["source_identity_json"]),
                "externalId": row["external_id"],
                "stableChunkId": row["external_id"],
                "versionHash": row["version_hash"],
                "documentHash": row["document_hash"],
                "chunkSequence": row["chunk_sequence"],
                "characterStart": row["character_start"],
                "characterEnd": row["character_end"],
                "totalChunks": row["total_chunks"],
                "collection": row["collection"],
                "path": row["source_path"],
                "title": row["source_title"],
                "documentCreatedAt": row["document_created_at"],
                "documentModifiedAt": row["document_modified_at"],
                "active": bool(row["source_active"]),
                "embeddedAt": row["embedded_at"],
                "embedding": {
                    "model": row["model"],
                    "fingerprint": row["fingerprint"],
                    "dimensions": row["dimensions"],
                    "dtype": row["dtype"],
                    "distanceMetric": row["distance_metric"],
                    "normalization": row["normalization"],
                    "spaceId": row["embedding_space_id"],
                    "vectorSha256": row["vector_sha256"],
                },
                "bundle": {
                    "importId": row["bundle_import_id"],
                    "exportId": row["export_id"],
                    "exportedAt": row["exported_at"],
                    "schema": {"name": row["schema_name"], "version": row["schema_version"]},
                    "exporter": {
                        "name": row["exporter_name"],
                        "version": row["exporter_version"],
                    },
                },
                "metadata": json.loads(row["metadata_json"]),
            }
    return result
