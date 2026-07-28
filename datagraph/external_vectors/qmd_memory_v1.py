from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from datagraph.external_vectors.models import (
    BundleValidationError,
    ExternalBundleManifest,
    ExternalChunk,
    ExternalEmbeddingSpace,
    ValidatedExternalBundle,
)

FORMAT_NAME = "qmd-memory-v1"
SCHEMA_NAME = "qmd-memory-bundle"
SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
SUPPORTED_DTYPE = "float32-le"
SUPPORTED_METRIC = "cosine"
SUPPORTED_SNAPSHOT_MODE = "full"
SUPPORTED_DELETION_POLICY = "tombstone-absent"
READ_SIZE = 1024 * 1024


def validate_bundle(root: Path) -> ValidatedExternalBundle:
    root = root.resolve()
    if not root.is_dir():
        raise BundleValidationError(f"bundle input is not a directory: {root}")
    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.is_file() or manifest_path.resolve().parent != root:
        raise BundleValidationError(
            f"manifest file is missing or escapes bundle: {MANIFEST_FILENAME}"
        )
    manifest_path = manifest_path.resolve()
    manifest_raw = _read_json(manifest_path, "manifest")
    manifest = _parse_manifest(root, manifest_raw)
    chunks_path, vectors_path, checksums_path = _payload_paths(root, manifest_raw)
    _validate_checksums(manifest_path, chunks_path, vectors_path, checksums_path, manifest_raw)

    vector_bytes_expected = manifest.chunk_count * manifest.embedding_space.dimensions * 4
    vector_bytes_actual = vectors_path.stat().st_size
    if vector_bytes_actual != vector_bytes_expected:
        raise BundleValidationError(
            "vector payload length mismatch: "
            f"expected {vector_bytes_expected} bytes, found {vector_bytes_actual}"
        )

    seen_ids: set[str] = set()
    document_hashes: set[str] = set()
    document_chunks: dict[str, set[int]] = {}
    document_totals: dict[str, int] = {}
    document_active: dict[str, bool] = {}
    count = 0
    expected_offset = 0
    for chunk in _iter_chunks(chunks_path, vectors_path, manifest):
        if chunk.external_id in seen_ids:
            raise BundleValidationError(f"duplicate externalId: {chunk.external_id}")
        seen_ids.add(chunk.external_id)
        document_hashes.add(chunk.document_hash)
        if chunk.sequence in document_chunks.setdefault(chunk.document_hash, set()):
            raise BundleValidationError(
                f"duplicate documentHash + sequence: {chunk.document_hash} + {chunk.sequence}"
            )
        document_chunks[chunk.document_hash].add(chunk.sequence)
        prior_total = document_totals.setdefault(chunk.document_hash, chunk.total_chunks)
        if prior_total != chunk.total_chunks:
            raise BundleValidationError(
                f"inconsistent totalChunks for documentHash {chunk.document_hash}"
            )
        prior_active = document_active.setdefault(chunk.document_hash, chunk.active)
        if prior_active != chunk.active:
            raise BundleValidationError(
                f"inconsistent active state for documentHash {chunk.document_hash}"
            )
        if chunk.vector_offset != expected_offset:
            raise BundleValidationError(
                f"chunk {chunk.external_id} vector offset must be {expected_offset}, "
                f"found {chunk.vector_offset}"
            )
        expected_offset += len(chunk.vector_bytes)
        count += 1
    if count != manifest.chunk_count:
        raise BundleValidationError(
            f"chunk count mismatch: manifest declares {manifest.chunk_count}, parsed {count}"
        )
    if len(document_hashes) != manifest.document_count:
        raise BundleValidationError(
            "document count mismatch: "
            f"manifest declares {manifest.document_count}, parsed {len(document_hashes)}"
        )
    for document_hash, sequences in document_chunks.items():
        expected = set(range(document_totals[document_hash]))
        if sequences != expected:
            raise BundleValidationError(
                f"documentHash {document_hash} does not contain sequences 0 through "
                f"{document_totals[document_hash] - 1}"
            )

    return ValidatedExternalBundle(
        root=root,
        manifest=manifest,
        iter_chunks=lambda: _iter_chunks(chunks_path, vectors_path, manifest),
        verify_integrity=lambda: _validate_checksums(
            manifest_path,
            chunks_path,
            vectors_path,
            checksums_path,
            manifest_raw,
        ),
    )


def _parse_manifest(root: Path, raw: dict[str, Any]) -> ExternalBundleManifest:
    schema = _object(raw, "schema")
    schema_name = _string(schema, "name", "schema.name")
    schema_version = _integer(schema, "version", "schema.version", minimum=1)
    if schema_name != SCHEMA_NAME or schema_version != SCHEMA_VERSION:
        raise BundleValidationError(
            f"unsupported schema {schema_name!r} version {schema_version}; "
            f"expected {SCHEMA_NAME!r} version {SCHEMA_VERSION}"
        )
    export_id = _string(raw, "exportId")
    exported_at = _timestamp(raw, "exportedAt")
    exporter = _object(raw, "exporter")
    exporter_name = _string(exporter, "name", "exporter.name")
    exporter_version = _string(exporter, "version", "exporter.version")
    source_identity = _object(raw, "sourceIdentity")
    _string(source_identity, "id", "sourceIdentity.id")
    if set(source_identity) - {"id", "label", "metadata"}:
        raise BundleValidationError(
            "sourceIdentity only supports id, label, and metadata; do not export machine secrets"
        )
    if "label" in source_identity:
        _string(source_identity, "label", "sourceIdentity.label")
    if "metadata" in source_identity and not isinstance(source_identity["metadata"], dict):
        raise BundleValidationError("sourceIdentity.metadata must be an object")

    chunk_count = _integer(raw, "chunkCount", minimum=0)
    document_count = _integer(raw, "documentCount", minimum=0)
    embedding = _object(raw, "embedding")
    dimensions = _integer(embedding, "dimensions", "embedding.dimensions", minimum=1)
    dtype = _string(embedding, "dtype", "embedding.dtype")
    metric = _string(embedding, "distanceMetric", "embedding.distanceMetric")
    normalization = _string(embedding, "normalization", "embedding.normalization")
    if dtype != SUPPORTED_DTYPE:
        raise BundleValidationError(f"embedding.dtype must be {SUPPORTED_DTYPE!r}; found {dtype!r}")
    if metric != SUPPORTED_METRIC:
        raise BundleValidationError(
            f"embedding.distanceMetric must be {SUPPORTED_METRIC!r}; found {metric!r}"
        )
    if normalization not in {"normalized", "unnormalized", "unknown"}:
        raise BundleValidationError(
            "embedding.normalization must be normalized, unnormalized, or unknown"
        )

    snapshot = _object(raw, "snapshot")
    snapshot_mode = _string(snapshot, "mode", "snapshot.mode")
    deletion_policy = _string(snapshot, "deletionPolicy", "snapshot.deletionPolicy")
    if snapshot_mode != SUPPORTED_SNAPSHOT_MODE:
        raise BundleValidationError(
            f"snapshot.mode must be {SUPPORTED_SNAPSHOT_MODE!r} for schema version 1"
        )
    if deletion_policy != SUPPORTED_DELETION_POLICY:
        raise BundleValidationError(
            f"snapshot.deletionPolicy must be {SUPPORTED_DELETION_POLICY!r} for schema version 1"
        )
    chunking = raw.get("chunking")
    if chunking is not None and not isinstance(chunking, dict):
        raise BundleValidationError("chunking must be an object or null")

    manifest_digest = _sha256(root / MANIFEST_FILENAME)
    return ExternalBundleManifest(
        format=FORMAT_NAME,
        schema_name=schema_name,
        schema_version=schema_version,
        export_id=export_id,
        exported_at=exported_at,
        exporter_name=exporter_name,
        exporter_version=exporter_version,
        source_identity=source_identity,
        chunk_count=chunk_count,
        document_count=document_count,
        embedding_space=ExternalEmbeddingSpace(
            model=_string(embedding, "model", "embedding.model"),
            fingerprint=_string(embedding, "fingerprint", "embedding.fingerprint"),
            dimensions=dimensions,
            dtype=dtype,
            distance_metric=metric,
            normalization=normalization,
        ),
        chunking=chunking,
        snapshot_mode=snapshot_mode,
        deletion_policy=deletion_policy,
        bundle_digest=manifest_digest,
        raw=raw,
    )


def _payload_paths(root: Path, raw: dict[str, Any]) -> tuple[Path, Path, Path]:
    payloads = _object(raw, "payloads")
    return tuple(
        _safe_payload_path(root, _string(payloads, key, f"payloads.{key}"))
        for key in ("chunks", "vectors", "checksums")
    )


def _safe_payload_path(root: Path, filename: str) -> Path:
    relative = Path(filename)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name != filename:
        raise BundleValidationError(
            f"payload filename must be a single relative filename: {filename}"
        )
    path = root / filename
    if not path.is_file() or path.resolve().parent != root:
        raise BundleValidationError(f"payload file is missing: {filename}")
    return path.resolve()


def _validate_checksums(
    manifest_path: Path,
    chunks_path: Path,
    vectors_path: Path,
    checksums_path: Path,
    manifest_raw: dict[str, Any],
) -> None:
    checksums = _read_json(checksums_path, "checksums")
    if checksums.get("algorithm") != "sha256":
        raise BundleValidationError("checksums.algorithm must be sha256")
    files = _object(checksums, "files", "checksums.files")
    paths = (manifest_path, chunks_path, vectors_path)
    for path in paths:
        entry = files.get(path.name)
        if not isinstance(entry, dict):
            raise BundleValidationError(f"checksums.files is missing {path.name}")
        declared = _string(entry, "sha256", f"checksums.files.{path.name}.sha256")
        declared_bytes = _integer(entry, "bytes", f"checksums.files.{path.name}.bytes", minimum=0)
        actual = _sha256(path)
        actual_bytes = path.stat().st_size
        if declared != actual or declared_bytes != actual_bytes:
            raise BundleValidationError(f"checksum failure for {path.name}")

    inline = _object(manifest_raw, "checksums")
    for path in (chunks_path, vectors_path):
        entry = inline.get(path.name)
        if not isinstance(entry, dict):
            raise BundleValidationError(f"manifest.checksums is missing {path.name}")
        if entry != files[path.name]:
            raise BundleValidationError(
                f"manifest.checksums.{path.name} does not match checksums payload"
            )


def _iter_chunks(
    chunks_path: Path,
    vectors_path: Path,
    manifest: ExternalBundleManifest,
) -> Iterator[ExternalChunk]:
    vector_length = manifest.embedding_space.dimensions * 4
    expected_offset = 0
    with (
        chunks_path.open("r", encoding="utf-8", newline="") as chunks_file,
        vectors_path.open("rb") as vectors_file,
    ):
        for line_number, line in enumerate(chunks_file, start=1):
            if not line.strip():
                raise BundleValidationError(f"chunks.ndjson line {line_number} is blank")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} is invalid JSON"
                ) from exc
            if not isinstance(raw, dict):
                raise BundleValidationError(f"chunks.ndjson line {line_number} must be an object")
            vector = _object(raw, "vector", f"line {line_number}.vector")
            offset = _integer(vector, "offset", f"line {line_number}.vector.offset", minimum=0)
            length = _integer(vector, "length", f"line {line_number}.vector.length", minimum=1)
            if length != vector_length:
                raise BundleValidationError(
                    f"line {line_number}.vector.length must be {vector_length}; found {length}"
                )
            if offset != expected_offset:
                raise BundleValidationError(
                    f"line {line_number}.vector offset must be {expected_offset}; found {offset}"
                )
            expected_offset += length
            vectors_file.seek(offset)
            vector_bytes = vectors_file.read(length)
            if len(vector_bytes) != length:
                raise BundleValidationError(
                    f"truncated vector payload for line {line_number}: expected {length} bytes"
                )
            values = np.frombuffer(vector_bytes, dtype="<f4")
            if len(values) != manifest.embedding_space.dimensions:
                raise BundleValidationError(
                    f"dimension mismatch on chunks.ndjson line {line_number}"
                )
            if not np.isfinite(values).all():
                raise BundleValidationError(
                    f"vector on chunks.ndjson line {line_number} contains non-finite values"
                )
            if math.isclose(float(np.linalg.norm(values)), 0.0, abs_tol=1e-12):
                raise BundleValidationError(
                    f"vector on chunks.ndjson line {line_number} has zero norm"
                )
            if manifest.embedding_space.normalization == "normalized" and not math.isclose(
                float(np.linalg.norm(values)), 1.0, rel_tol=1e-4, abs_tol=1e-4
            ):
                raise BundleValidationError(
                    f"vector on chunks.ndjson line {line_number} is declared normalized "
                    "but does not have unit norm"
                )
            chunk_model = raw.get("embeddingModel", manifest.embedding_space.model)
            chunk_fingerprint = raw.get(
                "embeddingFingerprint", manifest.embedding_space.fingerprint
            )
            if chunk_model != manifest.embedding_space.model:
                raise BundleValidationError(
                    f"mixed embedding model on chunks.ndjson line {line_number}"
                )
            if chunk_fingerprint != manifest.embedding_space.fingerprint:
                raise BundleValidationError(
                    f"mixed embedding fingerprint on chunks.ndjson line {line_number}"
                )
            metadata = raw.get("metadata", {})
            if not isinstance(metadata, dict):
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} metadata must be an object"
                )
            character_start = _integer(raw, "characterStart", minimum=0)
            character_end = _optional_integer(raw, "characterEnd", minimum=0)
            text = _string(raw, "text")
            if not text:
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} text must be non-empty"
                )
            if character_end is not None and character_end < character_start:
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} characterEnd precedes characterStart"
                )
            sequence = _integer(raw, "sequence", minimum=0)
            total_chunks = _integer(raw, "totalChunks", minimum=1)
            if sequence >= total_chunks:
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} sequence must be less than totalChunks"
                )
            active = raw.get("active")
            if not isinstance(active, bool):
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} active must be a boolean"
                )
            yield ExternalChunk(
                external_id=_string(raw, "externalId"),
                document_hash=_string(raw, "documentHash"),
                sequence=sequence,
                text=text,
                character_start=character_start,
                character_end=character_end,
                total_chunks=total_chunks,
                collection=_string(raw, "collection"),
                source_path=_string(raw, "path"),
                title=_string(raw, "title"),
                document_created_at=_timestamp(raw, "documentCreatedAt"),
                document_modified_at=_timestamp(raw, "documentModifiedAt"),
                active=active,
                embedded_at=_timestamp(raw, "embeddedAt"),
                vector_offset=offset,
                vector_bytes=vector_bytes,
                metadata=metadata,
            )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BundleValidationError(f"{label} file is missing: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise BundleValidationError(f"{label} file is invalid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise BundleValidationError(f"{label} file must contain an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(READ_SIZE):
            digest.update(block)
    return digest.hexdigest()


def _object(value: dict[str, Any], key: str, field: str | None = None) -> dict[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise BundleValidationError(f"{field or key} must be an object")
    return item


def _string(value: dict[str, Any], key: str, field: str | None = None) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise BundleValidationError(f"{field or key} must be a non-empty string")
    return item


def _integer(
    value: dict[str, Any],
    key: str,
    field: str | None = None,
    *,
    minimum: int,
) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
        raise BundleValidationError(
            f"{field or key} must be an integer greater than or equal to {minimum}"
        )
    return item


def _optional_integer(value: dict[str, Any], key: str, *, minimum: int) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
        raise BundleValidationError(
            f"{key} must be null or an integer greater than or equal to {minimum}"
        )
    return item


def _timestamp(value: dict[str, Any], key: str) -> str:
    item = _string(value, key)
    try:
        parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BundleValidationError(f"{key} must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BundleValidationError(f"{key} must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
