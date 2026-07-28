from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _VectorReference:
    vector_id: str
    document_hash: str
    sequence: int
    text_sha256: str
    offset: int
    length: int
    vector_sha256: str


def vector_identity(document_hash: str, sequence: int, fingerprint: str) -> str:
    return _identity_hash(["qmd-vector-v1", document_hash, sequence, fingerprint])


def record_identity(collection: str, source_path: str, vector_id: str) -> str:
    return _identity_hash(["qmd-record-v1", collection, source_path, vector_id])


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
    chunks_path, vector_index_path, vectors_path, checksums_path = _payload_paths(
        root, manifest_raw
    )
    _validate_checksums(
        manifest_path,
        chunks_path,
        vector_index_path,
        vectors_path,
        checksums_path,
        manifest_raw,
    )

    vector_bytes_expected = manifest.vector_count * manifest.embedding_space.dimensions * 4
    vector_bytes_actual = vectors_path.stat().st_size
    if vector_bytes_actual != vector_bytes_expected:
        raise BundleValidationError(
            "vector payload length mismatch: "
            f"expected {vector_bytes_expected} bytes, found {vector_bytes_actual}"
        )
    vector_index = _load_vector_index(vector_index_path, vectors_path, manifest)

    seen_ids: set[str] = set()
    referenced_vectors: set[str] = set()
    document_sequences: dict[tuple[str, str], set[int]] = {}
    document_facts: dict[tuple[str, str], tuple[Any, ...]] = {}
    count = 0
    for chunk in _iter_chunks(chunks_path, vectors_path, manifest, vector_index):
        if chunk.external_id in seen_ids:
            raise BundleValidationError(f"duplicate externalId: {chunk.external_id}")
        seen_ids.add(chunk.external_id)
        referenced_vectors.add(chunk.vector_id)
        source_key = (chunk.collection, chunk.source_path)
        sequences = document_sequences.setdefault(source_key, set())
        if chunk.sequence in sequences:
            raise BundleValidationError(
                "duplicate sequence for logical source document "
                f"{chunk.collection!r} + {chunk.source_path!r}: {chunk.sequence}"
            )
        sequences.add(chunk.sequence)
        facts = (
            chunk.document_hash,
            chunk.total_chunks,
            chunk.active,
            chunk.title,
            chunk.document_created_at,
            chunk.document_modified_at,
        )
        prior_facts = document_facts.setdefault(source_key, facts)
        if prior_facts != facts:
            raise BundleValidationError(
                "inconsistent document metadata for logical source document "
                f"{chunk.collection!r} + {chunk.source_path!r}"
            )
        count += 1

    if count != manifest.chunk_count:
        raise BundleValidationError(
            f"chunk count mismatch: manifest declares {manifest.chunk_count}, parsed {count}"
        )
    if len(document_sequences) != manifest.document_count:
        raise BundleValidationError(
            "document count mismatch: manifest declares "
            f"{manifest.document_count}, parsed {len(document_sequences)} logical source documents"
        )
    for source_key, sequences in document_sequences.items():
        total_chunks = int(document_facts[source_key][1])
        expected = set(range(total_chunks))
        if sequences != expected:
            raise BundleValidationError(
                "logical source document "
                f"{source_key[0]!r} + {source_key[1]!r} does not contain sequences "
                f"0 through {total_chunks - 1}"
            )
    unreferenced = set(vector_index) - referenced_vectors
    if unreferenced:
        raise BundleValidationError(
            f"vector index contains {len(unreferenced)} unreferenced vector object(s)"
        )

    return ValidatedExternalBundle(
        root=root,
        manifest=manifest,
        iter_chunks=lambda: _iter_chunks(chunks_path, vectors_path, manifest, vector_index),
        verify_integrity=lambda: _validate_checksums(
            manifest_path,
            chunks_path,
            vector_index_path,
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
    vector_count = _integer(raw, "vectorCount", minimum=0)
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
        vector_count=vector_count,
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
        bundle_digest=_sha256(root / MANIFEST_FILENAME),
        raw=raw,
    )


def _payload_paths(root: Path, raw: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    payloads = _object(raw, "payloads")
    return tuple(
        _safe_payload_path(root, _string(payloads, key, f"payloads.{key}"))
        for key in ("chunks", "vectorIndex", "vectors", "checksums")
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
    vector_index_path: Path,
    vectors_path: Path,
    checksums_path: Path,
    manifest_raw: dict[str, Any],
) -> None:
    checksums = _read_json(checksums_path, "checksums")
    if checksums.get("algorithm") != "sha256":
        raise BundleValidationError("checksums.algorithm must be sha256")
    files = _object(checksums, "files", "checksums.files")
    paths = (manifest_path, chunks_path, vector_index_path, vectors_path)
    for path in paths:
        entry = files.get(path.name)
        if not isinstance(entry, dict):
            raise BundleValidationError(f"checksums.files is missing {path.name}")
        declared = _string(entry, "sha256", f"checksums.files.{path.name}.sha256")
        declared_bytes = _integer(entry, "bytes", f"checksums.files.{path.name}.bytes", minimum=0)
        if declared != _sha256(path) or declared_bytes != path.stat().st_size:
            raise BundleValidationError(f"checksum failure for {path.name}")

    inline = _object(manifest_raw, "checksums")
    for path in (chunks_path, vector_index_path, vectors_path):
        entry = inline.get(path.name)
        if not isinstance(entry, dict):
            raise BundleValidationError(f"manifest.checksums is missing {path.name}")
        if entry != files[path.name]:
            raise BundleValidationError(
                f"manifest.checksums.{path.name} does not match checksums payload"
            )


def _load_vector_index(
    index_path: Path,
    vectors_path: Path,
    manifest: ExternalBundleManifest,
) -> dict[str, _VectorReference]:
    vector_length = manifest.embedding_space.dimensions * 4
    by_id: dict[str, _VectorReference] = {}
    by_content_position: set[tuple[str, int]] = set()
    expected_offset = 0
    with (
        index_path.open("r", encoding="utf-8", newline="") as index_file,
        vectors_path.open("rb") as vectors_file,
    ):
        for line_number, line in enumerate(index_file, start=1):
            raw = _ndjson_object(line, line_number, "vectors.ndjson")
            document_hash = _string(raw, "documentHash")
            sequence = _integer(raw, "sequence", minimum=0)
            fingerprint = _string(raw, "embeddingFingerprint")
            if fingerprint != manifest.embedding_space.fingerprint:
                raise BundleValidationError(
                    f"mixed embedding fingerprint on vectors.ndjson line {line_number}"
                )
            expected_id = vector_identity(document_hash, sequence, fingerprint)
            vector_id = _string(raw, "vectorId")
            if vector_id != expected_id:
                raise BundleValidationError(
                    f"vectors.ndjson line {line_number} vectorId does not match "
                    "documentHash + sequence + embeddingFingerprint"
                )
            if vector_id in by_id:
                raise BundleValidationError(f"duplicate vectorId: {vector_id}")
            content_position = (document_hash, sequence)
            if content_position in by_content_position:
                raise BundleValidationError(
                    f"duplicate vector identity: {document_hash} + {sequence}"
                )
            by_content_position.add(content_position)
            offset = _integer(raw, "offset", minimum=0)
            length = _integer(raw, "length", minimum=1)
            if length != vector_length:
                raise BundleValidationError(
                    f"vectors.ndjson line {line_number}.length must be {vector_length}; "
                    f"found {length}"
                )
            if offset != expected_offset:
                raise BundleValidationError(
                    f"vectors.ndjson line {line_number} offset must be {expected_offset}; "
                    f"found {offset}"
                )
            expected_offset += length
            vectors_file.seek(offset)
            vector_bytes = vectors_file.read(length)
            _validate_vector(vector_bytes, manifest, line_number)
            by_id[vector_id] = _VectorReference(
                vector_id=vector_id,
                document_hash=document_hash,
                sequence=sequence,
                text_sha256=_sha256_string(raw, "textSha256", line_number),
                offset=offset,
                length=length,
                vector_sha256=hashlib.sha256(vector_bytes).hexdigest(),
            )
    if len(by_id) != manifest.vector_count:
        raise BundleValidationError(
            f"vector count mismatch: manifest declares {manifest.vector_count}, parsed {len(by_id)}"
        )
    return by_id


def _iter_chunks(
    chunks_path: Path,
    vectors_path: Path,
    manifest: ExternalBundleManifest,
    vector_index: dict[str, _VectorReference],
) -> Iterator[ExternalChunk]:
    with (
        chunks_path.open("r", encoding="utf-8", newline="") as chunks_file,
        vectors_path.open("rb") as vectors_file,
    ):
        for line_number, line in enumerate(chunks_file, start=1):
            raw = _ndjson_object(line, line_number, "chunks.ndjson")
            vector_id = _string(raw, "vectorId")
            vector = vector_index.get(vector_id)
            if vector is None:
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} references unknown vectorId {vector_id!r}"
                )
            document_hash = _string(raw, "documentHash")
            sequence = _integer(raw, "sequence", minimum=0)
            if (document_hash, sequence) != (vector.document_hash, vector.sequence):
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} vectorId does not match its "
                    "documentHash + sequence"
                )
            text = _string(raw, "text")
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != vector.text_sha256:
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} text does not match vector textSha256"
                )
            collection = _string(raw, "collection")
            source_path = _string(raw, "path")
            external_id = _string(raw, "externalId")
            expected_external_id = record_identity(collection, source_path, vector_id)
            if external_id != expected_external_id:
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} externalId does not include stable "
                    "collection + path + vector identity"
                )
            character_start = _integer(raw, "characterStart", minimum=0)
            character_end = _optional_integer(raw, "characterEnd", minimum=0)
            if character_end is not None and character_end < character_start:
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} characterEnd precedes characterStart"
                )
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
            metadata = raw.get("metadata", {})
            if not isinstance(metadata, dict):
                raise BundleValidationError(
                    f"chunks.ndjson line {line_number} metadata must be an object"
                )
            vectors_file.seek(vector.offset)
            vector_bytes = vectors_file.read(vector.length)
            if hashlib.sha256(vector_bytes).hexdigest() != vector.vector_sha256:
                raise BundleValidationError(
                    f"vector payload changed while reading chunks.ndjson line {line_number}"
                )
            yield ExternalChunk(
                external_id=external_id,
                vector_id=vector_id,
                document_hash=document_hash,
                sequence=sequence,
                text=text,
                character_start=character_start,
                character_end=character_end,
                total_chunks=total_chunks,
                collection=collection,
                source_path=source_path,
                title=_string(raw, "title"),
                document_created_at=_timestamp(raw, "documentCreatedAt"),
                document_modified_at=_timestamp(raw, "documentModifiedAt"),
                active=active,
                embedded_at=_timestamp(raw, "embeddedAt"),
                vector_bytes=vector_bytes,
                metadata=metadata,
            )


def _validate_vector(
    vector_bytes: bytes,
    manifest: ExternalBundleManifest,
    line_number: int,
) -> None:
    expected_length = manifest.embedding_space.dimensions * 4
    if len(vector_bytes) != expected_length:
        raise BundleValidationError(
            f"truncated vector payload for vectors.ndjson line {line_number}: "
            f"expected {expected_length} bytes"
        )
    values = np.frombuffer(vector_bytes, dtype="<f4")
    if not np.isfinite(values).all():
        raise BundleValidationError(
            f"vector on vectors.ndjson line {line_number} contains non-finite values"
        )
    norm = float(np.linalg.norm(values))
    if math.isclose(norm, 0.0, abs_tol=1e-12):
        raise BundleValidationError(f"vector on vectors.ndjson line {line_number} has zero norm")
    if manifest.embedding_space.normalization == "normalized" and not math.isclose(
        norm, 1.0, rel_tol=1e-4, abs_tol=1e-4
    ):
        raise BundleValidationError(
            f"vector on vectors.ndjson line {line_number} is declared normalized "
            "but does not have unit norm"
        )


def _ndjson_object(line: str, line_number: int, filename: str) -> dict[str, Any]:
    if not line.strip():
        raise BundleValidationError(f"{filename} line {line_number} is blank")
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise BundleValidationError(f"{filename} line {line_number} is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise BundleValidationError(f"{filename} line {line_number} must be an object")
    return raw


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


def _sha256_string(value: dict[str, Any], key: str, line_number: int) -> str:
    item = _string(value, key)
    if len(item) != 64 or any(character not in "0123456789abcdef" for character in item):
        raise BundleValidationError(
            f"vectors.ndjson line {line_number} {key} must be a lowercase SHA-256 hex digest"
        )
    return item


def _identity_hash(parts: list[object]) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
