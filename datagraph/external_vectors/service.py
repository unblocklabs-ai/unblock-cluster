from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from datagraph.core.ids import new_id, now_iso
from datagraph.core.time import parse_timestamp
from datagraph.db import connect, fetch_one
from datagraph.external_vectors.models import (
    ExternalChunk,
    SnapshotConflictError,
    ValidatedExternalBundle,
)
from datagraph.external_vectors.qmd_memory_v1 import FORMAT_NAME, validate_bundle

ADAPTERS = {FORMAT_NAME: validate_bundle}


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    graph_id: str
    view_id: str
    embedding_run_id: str
    dataset: str
    export_id: str
    idempotent: bool
    stats: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "importId": self.import_id,
            "graphId": self.graph_id,
            "viewId": self.view_id,
            "embeddingRunId": self.embedding_run_id,
            "dataset": self.dataset,
            "exportId": self.export_id,
            "idempotent": self.idempotent,
            "stats": self.stats,
        }


def import_external_vectors(
    db_path: Path | str,
    *,
    format_name: str,
    input_path: Path | str,
    dataset: str,
) -> ImportResult:
    dataset = dataset.strip()
    if not dataset:
        raise ValueError("dataset must be a non-empty string")
    adapter = ADAPTERS.get(format_name)
    if adapter is None:
        raise ValueError(
            f"unsupported external vector format {format_name!r}; "
            f"supported formats: {', '.join(sorted(ADAPTERS))}"
        )
    bundle = adapter(Path(input_path))
    return _persist_bundle(Path(db_path), dataset, bundle)


def _persist_bundle(
    db_path: Path,
    dataset_key: str,
    bundle: ValidatedExternalBundle,
) -> ImportResult:
    manifest = bundle.manifest
    now = now_iso()
    source_json = _canonical_json(manifest.source_identity)
    space_id = _embedding_space_id(bundle)
    storage_model = f"external/{space_id}"

    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        dataset_row = fetch_one(
            conn,
            "SELECT * FROM external_datasets WHERE dataset_key = ?",
            (dataset_key,),
        )
        if dataset_row is not None:
            if dataset_row["format"] != manifest.format:
                raise SnapshotConflictError(
                    f"dataset {dataset_key!r} is already bound to format {dataset_row['format']!r}"
                )
            if dataset_row["source_identity_json"] != source_json:
                raise SnapshotConflictError(
                    f"dataset {dataset_key!r} is already bound to a different source identity"
                )
            existing_import = fetch_one(
                conn,
                "SELECT * FROM external_imports WHERE dataset_id = ? AND export_id = ?",
                (dataset_row["id"], manifest.export_id),
            )
            if existing_import is not None:
                if existing_import["bundle_digest"] != manifest.bundle_digest:
                    raise SnapshotConflictError(
                        f"exportId {manifest.export_id!r} was already imported with different bytes"
                    )
                return _existing_result(conn, dataset_key, dataset_row, existing_import)
            latest = dataset_row["latest_exported_at"]
            if latest is not None and manifest.exported_at <= latest:
                raise SnapshotConflictError(
                    f"snapshot {manifest.exported_at} is not newer than imported snapshot {latest}"
                )
            graph_id = dataset_row["graph_id"]
            dataset_id = dataset_row["id"]
            view_id = _all_records_view_id(conn, graph_id)
        else:
            graph_id, view_id = _create_graph(conn, dataset_key, now)
            dataset_id = new_id("extds")
            conn.execute(
                """
                INSERT INTO external_datasets (
                  id, graph_id, dataset_key, format, source_identity_json,
                  latest_export_id, latest_exported_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    dataset_id,
                    graph_id,
                    dataset_key,
                    manifest.format,
                    source_json,
                    now,
                    now,
                ),
            )

        _insert_embedding_space(conn, space_id, bundle, now)
        run_id = new_id("run")
        import_id = new_id("imp")
        input_refs = {
            "externalImportId": import_id,
            "datasetId": dataset_id,
            "exportId": manifest.export_id,
        }
        params = {
            "representation": "external",
            "embedding": {
                "provider": "external",
                "model": manifest.embedding_space.model,
                "fingerprint": manifest.embedding_space.fingerprint,
                "dimensions": manifest.embedding_space.dimensions,
                "dtype": manifest.embedding_space.dtype,
                "distanceMetric": manifest.embedding_space.distance_metric,
                "normalization": manifest.embedding_space.normalization,
            },
            "format": manifest.format,
            "dataset": dataset_key,
        }
        conn.execute(
            """
            INSERT INTO runs (
              id, graph_id, view_id, type, status, params_json, progress_json,
              error_text, input_refs_json, stats_json, created_at, started_at, completed_at
            )
            VALUES (?, ?, NULL, 'embed', 'succeeded', ?, ?, NULL, ?, '{}', ?, ?, ?)
            """,
            (
                run_id,
                graph_id,
                _canonical_json(params),
                _canonical_json(
                    {"state": "succeeded", "imported": 0, "total": manifest.chunk_count}
                ),
                _canonical_json(input_refs),
                now,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO external_imports (
              id, dataset_id, embedding_run_id, format, schema_name, schema_version,
              export_id, exported_at, exporter_name, exporter_version, bundle_digest,
              manifest_json, stats_json, created_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
            """,
            (
                import_id,
                dataset_id,
                run_id,
                manifest.format,
                manifest.schema_name,
                manifest.schema_version,
                manifest.export_id,
                manifest.exported_at,
                manifest.exporter_name,
                manifest.exporter_version,
                manifest.bundle_digest,
                _canonical_json(manifest.raw),
                now,
                now,
            ),
        )

        current_rows = {
            row["external_id"]: dict(row)
            for row in conn.execute(
                """
                SELECT ecv.*
                  FROM external_chunk_versions ecv
                 WHERE ecv.dataset_id = ? AND ecv.is_current = 1
                """,
                (dataset_id,),
            ).fetchall()
        }
        seen: set[str] = set()
        counts = {"added": 0, "changed": 0, "unchanged": 0, "inactive": 0, "deleted": 0}
        active_count = 0
        for chunk in bundle.iter_chunks():
            seen.add(chunk.external_id)
            current = current_rows.get(chunk.external_id)
            record_id, change = _persist_chunk(
                conn,
                dataset_id=dataset_id,
                graph_id=graph_id,
                import_id=import_id,
                space_id=space_id,
                storage_model=storage_model,
                bundle=bundle,
                chunk=chunk,
                current=current,
                now=now,
            )
            counts[change] += 1
            state = "active" if chunk.active else "inactive"
            conn.execute(
                """
                INSERT INTO external_import_items (import_id, record_id, external_id, state)
                VALUES (?, ?, ?, ?)
                """,
                (import_id, record_id, chunk.external_id, state),
            )
            if chunk.active:
                active_count += 1
                vector_sha = hashlib.sha256(chunk.vector_bytes).hexdigest()
                vector_key = _vector_key(space_id, vector_sha)
                conn.execute(
                    """
                    INSERT INTO embedding_items (run_id, record_id, text_hash, status)
                    VALUES (?, ?, ?, 'imported')
                    """,
                    (run_id, record_id, vector_key),
                )
            else:
                counts["inactive"] += 1

        for external_id, current in current_rows.items():
            if external_id in seen:
                continue
            counts["deleted"] += 1
            conn.execute(
                """
                UPDATE external_chunk_versions
                   SET is_current = 0, superseded_at = ?
                 WHERE dataset_id = ? AND external_id = ? AND is_current = 1
                """,
                (manifest.exported_at, dataset_id, external_id),
            )
            conn.execute(
                "UPDATE records SET is_active = 0, updated_at = ? WHERE id = ?",
                (now, current["record_id"]),
            )
            conn.execute(
                """
                INSERT INTO external_import_items (import_id, record_id, external_id, state)
                VALUES (?, ?, ?, 'inactive')
                """,
                (import_id, current["record_id"], external_id),
            )

        # The parser intentionally makes a second streaming pass during persistence.
        # Recheck the original digest contract before commit so a concurrently modified
        # bundle cannot turn the validation/persistence split into a TOCTOU import.
        bundle.verify_integrity()

        stats = {
            "origin": "external",
            "provider": "external",
            "format": manifest.format,
            "dataset": dataset_key,
            "externalImportId": import_id,
            "exportId": manifest.export_id,
            "records": active_count,
            "chunks": manifest.chunk_count,
            "documents": manifest.document_count,
            "model": manifest.embedding_space.model,
            "storageModel": storage_model,
            "embeddingFingerprint": manifest.embedding_space.fingerprint,
            "embeddingSpaceId": space_id,
            "dimensions": manifest.embedding_space.dimensions,
            "dtype": manifest.embedding_space.dtype,
            "distanceMetric": manifest.embedding_space.distance_metric,
            "sourceNormalization": manifest.embedding_space.normalization,
            "storedRepresentation": (
                "original-normalized"
                if manifest.embedding_space.normalization == "normalized"
                else "derived-l2-normalized"
            ),
            "originalVectorsPreserved": True,
            "providerRequests": 0,
            "providerRetries": 0,
            "tokenUsage": {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0},
            **counts,
        }
        conn.execute(
            "UPDATE runs SET progress_json = ?, stats_json = ? WHERE id = ?",
            (
                _canonical_json(
                    {"state": "succeeded", "imported": active_count, "total": manifest.chunk_count}
                ),
                _canonical_json(stats),
                run_id,
            ),
        )
        conn.execute(
            "UPDATE external_imports SET stats_json = ? WHERE id = ?",
            (_canonical_json(stats), import_id),
        )
        conn.execute(
            """
            UPDATE external_datasets
               SET latest_export_id = ?, latest_exported_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (manifest.export_id, manifest.exported_at, now, dataset_id),
        )
        conn.execute(
            """
            UPDATE views
               SET default_embedding_run_id = ?,
                   default_cluster_run_id = NULL,
                   default_layout_run_id = NULL,
                   default_label_run_id = NULL,
                   default_trend_run_id = NULL,
                   updated_at = ?
             WHERE id = ? AND graph_id = ?
            """,
            (run_id, now, view_id, graph_id),
        )
        conn.execute("UPDATE graphs SET updated_at = ? WHERE id = ?", (now, graph_id))
        conn.commit()
    return ImportResult(
        import_id=import_id,
        graph_id=graph_id,
        view_id=view_id,
        embedding_run_id=run_id,
        dataset=dataset_key,
        export_id=manifest.export_id,
        idempotent=False,
        stats=stats,
    )


def _persist_chunk(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
    graph_id: str,
    import_id: str,
    space_id: str,
    storage_model: str,
    bundle: ValidatedExternalBundle,
    chunk: ExternalChunk,
    current: dict[str, Any] | None,
    now: str,
) -> tuple[str, str]:
    manifest = bundle.manifest
    vector_sha = hashlib.sha256(chunk.vector_bytes).hexdigest()
    text_sha = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
    version_payload = {
        "externalId": chunk.external_id,
        "documentHash": chunk.document_hash,
        "sequence": chunk.sequence,
        "textSha256": text_sha,
        "characterStart": chunk.character_start,
        "characterEnd": chunk.character_end,
        "totalChunks": chunk.total_chunks,
        "collection": chunk.collection,
        "path": chunk.source_path,
        "title": chunk.title,
        "documentCreatedAt": chunk.document_created_at,
        "documentModifiedAt": chunk.document_modified_at,
        "active": chunk.active,
        "embeddedAt": chunk.embedded_at,
        "metadata": chunk.metadata,
        "embeddingSpaceId": space_id,
        "vectorSha256": vector_sha,
    }
    version_hash = hashlib.sha256(_canonical_json(version_payload).encode()).hexdigest()
    if current is not None and current["version_hash"] == version_hash:
        record_id = current["record_id"]
        conn.execute(
            "UPDATE records SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if chunk.active else 0, now, record_id),
        )
        _store_vector(
            conn,
            space_id=space_id,
            storage_model=storage_model,
            dimensions=manifest.embedding_space.dimensions,
            normalization=manifest.embedding_space.normalization,
            vector_sha=vector_sha,
            original=chunk.vector_bytes,
            now=now,
        )
        return record_id, "unchanged"

    historical = fetch_one(
        conn,
        """
        SELECT record_id
          FROM external_chunk_versions
         WHERE dataset_id = ? AND external_id = ? AND version_hash = ?
        """,
        (dataset_id, chunk.external_id, version_hash),
    )
    change = "added" if current is None and historical is None else "changed"
    if current is not None:
        conn.execute(
            """
            UPDATE external_chunk_versions
               SET is_current = 0, superseded_at = ?
             WHERE dataset_id = ? AND external_id = ? AND is_current = 1
            """,
            (manifest.exported_at, dataset_id, chunk.external_id),
        )
        conn.execute(
            "UPDATE records SET is_active = 0, updated_at = ? WHERE id = ?",
            (now, current["record_id"]),
        )

    if historical is not None:
        record_id = historical["record_id"]
        conn.execute(
            """
            UPDATE external_chunk_versions
               SET is_current = 1, superseded_at = NULL
             WHERE dataset_id = ? AND external_id = ? AND version_hash = ?
            """,
            (dataset_id, chunk.external_id, version_hash),
        )
        conn.execute(
            "UPDATE records SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if chunk.active else 0, now, record_id),
        )
        _store_vector(
            conn,
            space_id=space_id,
            storage_model=storage_model,
            dimensions=manifest.embedding_space.dimensions,
            normalization=manifest.embedding_space.normalization,
            vector_sha=vector_sha,
            original=chunk.vector_bytes,
            now=now,
        )
        return record_id, change

    record_identity = f"{dataset_id}:{chunk.external_id}:{version_hash}"
    record_id = f"rec_ext_{hashlib.sha256(record_identity.encode()).hexdigest()[:32]}"
    record_key = f"external:{dataset_id}:{chunk.external_id}:{version_hash[:16]}"
    _, timestamp_ms = parse_timestamp(chunk.document_modified_at)
    record_metadata = {
        **chunk.metadata,
        "externalVector": {
            "externalId": chunk.external_id,
            "datasetId": dataset_id,
            "documentHash": chunk.document_hash,
            "chunkSequence": chunk.sequence,
            "characterStart": chunk.character_start,
            "characterEnd": chunk.character_end,
            "totalChunks": chunk.total_chunks,
            "collection": chunk.collection,
            "path": chunk.source_path,
        },
    }
    normalized = {
        "recordId": record_key,
        "sourceType": "qmd_memory",
        "sourceName": chunk.collection,
        "sourceRecordId": chunk.external_id,
        "title": chunk.title,
        "customerText": chunk.text,
        "timestamp": chunk.document_modified_at,
        "metadata": record_metadata,
    }
    conn.execute(
        """
        INSERT INTO records (
          id, graph_id, record_key, source_type, source_name, source_record_id,
          title, customer_text, record_url, product, sku, rating, sentiment,
          tags_json, timestamp_utc, timestamp_ms, metadata_json, normalized_json,
          created_at, updated_at, is_active
        )
        VALUES (?, ?, ?, 'qmd_memory', ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL,
                NULL, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            graph_id,
            record_key,
            chunk.collection,
            chunk.external_id,
            chunk.title,
            chunk.text,
            chunk.document_modified_at,
            timestamp_ms,
            _canonical_json(record_metadata),
            _canonical_json(normalized),
            now,
            now,
            1 if chunk.active else 0,
        ),
    )
    conn.execute(
        """
        INSERT INTO external_chunk_versions (
          dataset_id, external_id, version_hash, record_id, introduced_import_id,
          embedding_space_id, vector_sha256, text_sha256, document_hash,
          chunk_sequence, character_start, character_end, total_chunks, collection,
          source_path, source_title, document_created_at, document_modified_at,
          embedded_at, source_active, is_current, metadata_json, created_at, superseded_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
        """,
        (
            dataset_id,
            chunk.external_id,
            version_hash,
            record_id,
            import_id,
            space_id,
            vector_sha,
            text_sha,
            chunk.document_hash,
            chunk.sequence,
            chunk.character_start,
            chunk.character_end,
            chunk.total_chunks,
            chunk.collection,
            chunk.source_path,
            chunk.title,
            chunk.document_created_at,
            chunk.document_modified_at,
            chunk.embedded_at,
            1 if chunk.active else 0,
            _canonical_json(chunk.metadata),
            now,
        ),
    )
    _store_vector(
        conn,
        space_id=space_id,
        storage_model=storage_model,
        dimensions=manifest.embedding_space.dimensions,
        normalization=manifest.embedding_space.normalization,
        vector_sha=vector_sha,
        original=chunk.vector_bytes,
        now=now,
    )
    return record_id, change


def _store_vector(
    conn: sqlite3.Connection,
    *,
    space_id: str,
    storage_model: str,
    dimensions: int,
    normalization: str,
    vector_sha: str,
    original: bytes,
    now: str,
) -> None:
    values = np.frombuffer(original, dtype="<f4")
    if normalization == "normalized":
        derived = bytes(original)
        transformation = "none"
    else:
        derived = (values / np.linalg.norm(values)).astype("<f4", copy=False).tobytes()
        transformation = "l2-normalize"
    conn.execute(
        """
        INSERT OR IGNORE INTO external_vectors (
          embedding_space_id, vector_sha256, original_vector, derived_vector,
          transformation, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (space_id, vector_sha, original, derived, transformation, now),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO embedding_vectors (
          model, dimensions, text_hash, vector, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (storage_model, dimensions, _vector_key(space_id, vector_sha), derived, now),
    )


def _create_graph(conn: sqlite3.Connection, dataset_key: str, now: str) -> tuple[str, str]:
    graph_id = new_id("grf")
    view_id = new_id("view")
    config = {"embedding": {"textFields": ["customerText"]}}
    conn.execute(
        "INSERT INTO graphs (id, name, config_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (graph_id, dataset_key, _canonical_json(config), now, now),
    )
    conn.execute(
        """
        INSERT INTO views (
          id, graph_id, name, description, scope_json,
          default_embedding_run_id, default_cluster_run_id, default_layout_run_id,
          default_label_run_id, default_trend_run_id, created_at, updated_at
        )
        VALUES (?, ?, 'all_records', NULL, '{}', NULL, NULL, NULL, NULL, NULL, ?, ?)
        """,
        (view_id, graph_id, now, now),
    )
    return graph_id, view_id


def _insert_embedding_space(
    conn: sqlite3.Connection,
    space_id: str,
    bundle: ValidatedExternalBundle,
    now: str,
) -> None:
    space = bundle.manifest.embedding_space
    conn.execute(
        """
        INSERT OR IGNORE INTO embedding_spaces (
          id, origin, model, fingerprint, dimensions, dtype, distance_metric,
          normalization, metadata_json, created_at
        )
        VALUES (?, 'external', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            space_id,
            space.model,
            space.fingerprint,
            space.dimensions,
            space.dtype,
            space.distance_metric,
            space.normalization,
            _canonical_json({"format": bundle.manifest.format}),
            now,
        ),
    )


def _embedding_space_id(bundle: ValidatedExternalBundle) -> str:
    space = bundle.manifest.embedding_space
    payload = {
        "model": space.model,
        "fingerprint": space.fingerprint,
        "dimensions": space.dimensions,
        "dtype": space.dtype,
        "distanceMetric": space.distance_metric,
        "normalization": space.normalization,
    }
    return f"esp_{hashlib.sha256(_canonical_json(payload).encode()).hexdigest()[:32]}"


def _vector_key(space_id: str, vector_sha: str) -> str:
    return hashlib.sha256(f"{space_id}:{vector_sha}".encode()).hexdigest()


def _all_records_view_id(conn: sqlite3.Connection, graph_id: str) -> str:
    row = fetch_one(
        conn,
        "SELECT id FROM views WHERE graph_id = ? AND name = 'all_records'",
        (graph_id,),
    )
    if row is None:
        raise RuntimeError("external dataset graph is missing its all_records view")
    return row["id"]


def _existing_result(
    conn: sqlite3.Connection,
    dataset_key: str,
    dataset_row: dict[str, Any],
    import_row: dict[str, Any],
) -> ImportResult:
    return ImportResult(
        import_id=import_row["id"],
        graph_id=dataset_row["graph_id"],
        view_id=_all_records_view_id(conn, dataset_row["graph_id"]),
        embedding_run_id=import_row["embedding_run_id"],
        dataset=dataset_key,
        export_id=import_row["export_id"],
        idempotent=True,
        stats=json.loads(import_row["stats_json"]),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
