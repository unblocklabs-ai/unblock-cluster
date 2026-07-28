from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BundleValidationError(ValueError):
    pass


class SnapshotConflictError(ValueError):
    pass


@dataclass(frozen=True)
class ExternalEmbeddingSpace:
    model: str
    fingerprint: str
    dimensions: int
    dtype: str
    distance_metric: str
    normalization: str


@dataclass(frozen=True)
class ExternalBundleManifest:
    format: str
    schema_name: str
    schema_version: int
    export_id: str
    exported_at: str
    exporter_name: str
    exporter_version: str
    source_identity: dict[str, Any]
    chunk_count: int
    vector_count: int
    document_count: int
    embedding_space: ExternalEmbeddingSpace
    chunking: dict[str, Any] | None
    snapshot_mode: str
    deletion_policy: str
    bundle_digest: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ExternalChunk:
    external_id: str
    vector_id: str
    document_hash: str
    sequence: int
    text: str
    character_start: int
    character_end: int | None
    total_chunks: int
    collection: str
    source_path: str
    title: str
    document_created_at: str
    document_modified_at: str
    active: bool
    embedded_at: str
    vector_bytes: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ValidatedExternalBundle:
    root: Path
    manifest: ExternalBundleManifest
    iter_chunks: Callable[[], Iterator[ExternalChunk]]
    verify_integrity: Callable[[], None]
