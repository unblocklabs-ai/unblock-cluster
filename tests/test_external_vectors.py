from __future__ import annotations

import hashlib
import json
import shutil
import struct
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from datagraph.db import connect, initialize_database
from datagraph.external_vectors import import_external_vectors
from datagraph.external_vectors.models import BundleValidationError, SnapshotConflictError
from datagraph.main import create_app
from tests.helpers import test_settings
from tests.test_labeling import ScriptedLabelProvider

FIXTURE = Path(__file__).parent / "fixtures" / "qmd-memory-v1"


def test_valid_import_preserves_exact_vectors_text_provenance_and_is_idempotent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "data" / "datagraph.sqlite3"
    initialize_database(db_path)
    result = _import(db_path)

    assert result.stats["origin"] == "external"
    assert result.stats["providerRequests"] == 0
    assert result.stats["records"] == 8
    chunks = _read_chunks(FIXTURE)
    payload = (FIXTURE / "vectors.f32").read_bytes()
    expected_vectors = {
        hashlib.sha256(
            payload[
                chunk["vector"]["offset"] : chunk["vector"]["offset"] + chunk["vector"]["length"]
            ]
        ).hexdigest(): payload[
            chunk["vector"]["offset"] : chunk["vector"]["offset"] + chunk["vector"]["length"]
        ]
        for chunk in chunks
    }
    with connect(db_path) as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (result.embedding_run_id,)).fetchone()
        assert run["type"] == "embed"
        assert run["status"] == "succeeded"
        assert json.loads(run["params_json"])["embedding"]["provider"] == "external"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM embedding_items WHERE run_id = ? AND status = 'imported'",
                (result.embedding_run_id,),
            ).fetchone()[0]
            == 8
        )
        vectors = conn.execute(
            "SELECT vector_sha256, original_vector FROM external_vectors"
        ).fetchall()
        assert {row["vector_sha256"]: row["original_vector"] for row in vectors} == expected_vectors
        texts = {
            row["source_record_id"]: row["customer_text"]
            for row in conn.execute("SELECT source_record_id, customer_text FROM records")
        }
        assert texts == {chunk["externalId"]: chunk["text"] for chunk in chunks}

    settings = test_settings(tmp_path / "data")
    with TestClient(create_app(settings)) as client:
        record_id = client.get(f"/api/graphs/{result.graph_id}/records").json()["records"][0]["id"]
        record = client.get(f"/api/graphs/{result.graph_id}/records/{record_id}").json()
        assert record["customerText"] in {chunk["text"] for chunk in chunks}
        assert record["provenance"]["embedding"] == {
            "model": "qmd-synthetic-4d",
            "fingerprint": "sha256:qmd-synthetic-space-v1",
            "dimensions": 4,
            "dtype": "float32-le",
            "distanceMetric": "cosine",
            "normalization": "normalized",
            "spaceId": result.stats["embeddingSpaceId"],
            "vectorSha256": record["provenance"]["embedding"]["vectorSha256"],
        }
        assert record["provenance"]["bundle"]["exportId"] == "qmd-fixture-export-001"

    again = _import(db_path)
    assert again.idempotent is True
    assert again.import_id == result.import_id
    assert again.embedding_run_id == result.embedding_run_id
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM external_imports").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM embedding_vectors").fetchone()[0] == 8

    conflicting = _copy_bundle(tmp_path / "conflicting-export")
    chunks, vectors = _bundle_records_and_vectors(conflicting)
    chunks[0]["text"] = "Different bytes under the same export identity are invalid."
    _write_bundle(conflicting, chunks, vectors)
    with pytest.raises(SnapshotConflictError, match="different bytes"):
        _import(db_path, conflicting)


def test_incremental_snapshot_adds_versions_and_tombstones_absent_chunks(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "datagraph.sqlite3"
    initialize_database(db_path)
    first = _import(db_path)
    old_chunks = _read_chunks(FIXTURE)
    old_changed_id = old_chunks[0]["externalId"]
    removed_ids = {chunk["externalId"] for chunk in old_chunks if chunk["documentHash"] == "d" * 64}
    bundle = _copy_bundle(tmp_path / "snapshot-2")
    chunks, vectors = _bundle_records_and_vectors(bundle)
    chunks[0]["text"] = "The deployment queue now records an exact retry reason."
    vectors[0] = struct.pack("<4f", 0.8, 0.6, 0.0, 0.0)
    kept = [
        (chunk, vector)
        for chunk, vector in zip(chunks, vectors, strict=True)
        if chunk["externalId"] not in removed_ids
    ]
    added = {
        "externalId": "new-synthetic-chunk",
        "documentHash": "e" * 64,
        "sequence": 0,
        "text": "A portable vector bundle keeps source evidence attached.",
        "characterStart": 0,
        "characterEnd": 56,
        "totalChunks": 1,
        "collection": "work",
        "path": "memory/vector-import.md",
        "title": "Vector import plan",
        "documentCreatedAt": "2026-05-10T00:00:00Z",
        "documentModifiedAt": "2026-05-10T00:00:00Z",
        "active": True,
        "embeddedAt": "2026-05-11T00:00:00Z",
        "embeddingModel": "qmd-synthetic-4d",
        "embeddingFingerprint": "sha256:qmd-synthetic-space-v1",
        "vector": {"offset": 0, "length": 16},
        "metadata": {"fixtureTopic": "operations"},
    }
    kept.append((added, struct.pack("<4f", 0.0, 0.0, 1.0, 0.0)))
    _write_bundle(
        bundle,
        [chunk for chunk, _ in kept],
        [vector for _, vector in kept],
        export_id="qmd-fixture-export-002",
        exported_at="2026-06-01T00:00:00Z",
    )

    second = _import(db_path, bundle)
    assert second.stats["added"] == 1
    assert second.stats["changed"] == 1
    assert second.stats["deleted"] == 2
    assert second.stats["unchanged"] == 5
    assert second.stats["records"] == 7
    with connect(db_path) as conn:
        changed_versions = conn.execute(
            "SELECT record_id, is_current FROM external_chunk_versions WHERE external_id = ?",
            (old_changed_id,),
        ).fetchall()
        assert len(changed_versions) == 2
        assert sum(row["is_current"] for row in changed_versions) == 1
        assert conn.execute("SELECT COUNT(*) FROM records WHERE is_active = 1").fetchone()[0] == 7
        removed_states = {
            row["external_id"]: row["state"]
            for row in conn.execute(
                "SELECT external_id, state FROM external_import_items WHERE import_id = ?",
                (second.import_id,),
            )
            if row["external_id"] in removed_ids
        }
        assert removed_states == {external_id: "inactive" for external_id in removed_ids}
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM embedding_items WHERE run_id = ?",
                (first.embedding_run_id,),
            ).fetchone()[0]
            == 8
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM embedding_items WHERE run_id = ?",
                (second.embedding_run_id,),
            ).fetchone()[0]
            == 7
        )

    older = _copy_bundle(tmp_path / "older")
    chunks, vectors = _bundle_records_and_vectors(older)
    _write_bundle(
        older,
        chunks,
        vectors,
        export_id="qmd-fixture-export-old",
        exported_at="2026-05-15T00:00:00Z",
    )
    with pytest.raises(SnapshotConflictError, match="not newer"):
        _import(db_path, older)

    other_source = _copy_bundle(tmp_path / "other-source")
    manifest = _read_json(other_source / "manifest.json")
    manifest["sourceIdentity"]["id"] = "another-qmd-installation"
    manifest["exportId"] = "qmd-fixture-export-other-source"
    manifest["exportedAt"] = "2026-07-01T00:00:00Z"
    _write_manifest(other_source, manifest)
    with pytest.raises(SnapshotConflictError, match="different source identity"):
        _import(db_path, other_source)

    with TestClient(create_app(test_settings(tmp_path / "data"))) as client:
        current_cluster = client.post(
            f"/api/graphs/{first.graph_id}/views/{first.view_id}/cluster",
            json={
                "setDefault": False,
                "cluster": {
                    "space": {"method": "none"},
                    "hdbscan": {"minClusterSize": 2, "minSamples": 1},
                },
            },
        )
        assert current_cluster.status_code == 201, current_cluster.text
        assert current_cluster.json()["params"]["embeddingRunId"] == second.embedding_run_id
        current_cluster_run = _poll_run(
            client,
            first.graph_id,
            current_cluster.json()["id"],
        )
        assert current_cluster_run["stats"]["population"] == 7
        immutable = client.delete(f"/api/graphs/{first.graph_id}/runs/{first.embedding_run_id}")
        assert immutable.status_code == 409
        assert "immutable import history" in immutable.text
        deleted = client.delete(f"/api/graphs/{first.graph_id}")
        assert deleted.status_code == 204, deleted.text
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM external_imports").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM external_chunk_versions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM external_vectors").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM embedding_vectors").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM embedding_spaces").fetchone()[0] == 0


def test_unnormalized_bundle_preserves_original_and_records_derived_transform(
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(tmp_path / "unnormalized")
    chunks, vectors = _bundle_records_and_vectors(bundle)
    scaled = [
        (np.frombuffer(vector, dtype="<f4") * 3.0).astype("<f4").tobytes() for vector in vectors
    ]
    _write_bundle(bundle, chunks, scaled)
    manifest = _read_json(bundle / "manifest.json")
    manifest["embedding"]["normalization"] = "unnormalized"
    _write_manifest(bundle, manifest)
    db_path = tmp_path / "data" / "datagraph.sqlite3"
    initialize_database(db_path)
    result = _import(db_path, bundle)
    assert result.stats["storedRepresentation"] == "derived-l2-normalized"
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT original_vector, derived_vector, transformation FROM external_vectors LIMIT 1"
        ).fetchone()
    assert row["original_vector"] in scaled
    assert row["transformation"] == "l2-normalize"
    assert np.isclose(np.linalg.norm(np.frombuffer(row["derived_vector"], dtype="<f4")), 1.0)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("checksum", "checksum failure"),
        ("truncated", "vector payload length mismatch"),
        ("dimensions", "vector payload length mismatch"),
        ("duplicate", "duplicate externalId"),
        ("duplicate-sequence", r"duplicate documentHash \+ sequence"),
        ("offset", "vector offset must be"),
        ("timestamp", "must include a timezone"),
        ("normalization", "declared normalized"),
        ("schema", "unsupported schema"),
        ("mixed", "mixed embedding fingerprint"),
    ],
)
def test_bundle_validation_rejects_invalid_payloads(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    bundle = _copy_bundle(tmp_path / mutation)
    if mutation == "checksum":
        with (bundle / "chunks.ndjson").open("a", encoding="utf-8") as file:
            file.write(" ")
    elif mutation == "truncated":
        (bundle / "vectors.f32").write_bytes((bundle / "vectors.f32").read_bytes()[:-1])
        _refresh_checksums(bundle)
    elif mutation == "dimensions":
        manifest = _read_json(bundle / "manifest.json")
        manifest["embedding"]["dimensions"] = 5
        _write_manifest(bundle, manifest)
    elif mutation == "duplicate":
        chunks, vectors = _bundle_records_and_vectors(bundle)
        chunks[1]["externalId"] = chunks[0]["externalId"]
        _write_bundle(bundle, chunks, vectors)
    elif mutation == "duplicate-sequence":
        chunks, vectors = _bundle_records_and_vectors(bundle)
        chunks[1]["sequence"] = chunks[0]["sequence"]
        _write_bundle(bundle, chunks, vectors)
    elif mutation == "offset":
        chunks = _read_chunks(bundle)
        chunks[1]["vector"]["offset"] += 1
        _write_chunks(bundle, chunks)
        _refresh_checksums(bundle)
    elif mutation == "timestamp":
        chunks, vectors = _bundle_records_and_vectors(bundle)
        chunks[0]["embeddedAt"] = "2026-05-01T12:00:00"
        _write_bundle(bundle, chunks, vectors)
    elif mutation == "normalization":
        chunks, vectors = _bundle_records_and_vectors(bundle)
        vectors[0] = struct.pack("<4f", 2.0, 0.0, 0.0, 0.0)
        _write_bundle(bundle, chunks, vectors)
    elif mutation == "schema":
        manifest = _read_json(bundle / "manifest.json")
        manifest["schema"]["version"] = 2
        _write_manifest(bundle, manifest)
    elif mutation == "mixed":
        chunks, vectors = _bundle_records_and_vectors(bundle)
        chunks[1]["embeddingFingerprint"] = "sha256:different-space"
        _write_bundle(bundle, chunks, vectors)

    db_path = tmp_path / "data" / "datagraph.sqlite3"
    initialize_database(db_path)
    with pytest.raises(BundleValidationError, match=message):
        _import(db_path, bundle)
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graphs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM external_imports").fetchone()[0] == 0


def test_import_transaction_rolls_back_on_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datagraph.external_vectors import service

    db_path = tmp_path / "data" / "datagraph.sqlite3"
    initialize_database(db_path)
    original = service._persist_chunk
    calls = 0

    def fail_after_first(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic persistence failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_persist_chunk", fail_after_first)
    with pytest.raises(RuntimeError, match="synthetic persistence failure"):
        _import(db_path)
    with connect(db_path) as conn:
        for table in (
            "graphs",
            "records",
            "runs",
            "external_datasets",
            "external_imports",
            "external_vectors",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_clustering_rejects_a_mixed_external_embedding_run(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    db_path = data_dir / "datagraph.sqlite3"
    initialize_database(db_path)
    imported = _import(db_path)
    with connect(db_path) as conn:
        source_space = conn.execute("SELECT * FROM embedding_spaces LIMIT 1").fetchone()
        conn.execute(
            """
            INSERT INTO embedding_spaces (
              id, origin, model, fingerprint, dimensions, dtype, distance_metric,
              normalization, metadata_json, created_at
            )
            VALUES ('esp_mixed', 'external', ?, 'different-fingerprint', ?, ?, ?, ?, '{}', ?)
            """,
            (
                source_space["model"],
                source_space["dimensions"],
                source_space["dtype"],
                source_space["distance_metric"],
                source_space["normalization"],
                source_space["created_at"],
            ),
        )
        record_id = conn.execute(
            "SELECT record_id FROM embedding_items WHERE run_id = ? LIMIT 1",
            (imported.embedding_run_id,),
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE external_chunk_versions
               SET embedding_space_id = 'esp_mixed'
             WHERE record_id = ?
            """,
            (record_id,),
        )
        conn.commit()

    with TestClient(create_app(test_settings(data_dir))) as client:
        response = client.post(
            f"/api/graphs/{imported.graph_id}/views/{imported.view_id}/cluster",
            json={
                "cluster": {
                    "space": {"method": "none"},
                    "hdbscan": {"minClusterSize": 2, "minSamples": 1},
                }
            },
        )
        assert response.status_code == 201
        run = _wait_terminal(client, imported.graph_id, response.json()["id"])
        assert run["status"] == "failed"
        assert "mixed or untraceable embedding spaces" in run["errorText"]


def test_fixture_end_to_end_import_cluster_layout_label_trend_and_evidence(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    db_path = data_dir / "datagraph.sqlite3"
    initialize_database(db_path)
    imported = _import(db_path)
    label_provider = ScriptedLabelProvider()

    def embedding_provider_must_not_run(_config: dict[str, Any]) -> Any:
        raise AssertionError("external-vector flow must not invoke an embedding provider")

    with TestClient(
        create_app(
            test_settings(data_dir),
            embedding_provider_factory=embedding_provider_must_not_run,
            label_provider_factory=lambda _config: label_provider,
        )
    ) as client:
        cluster_id = _enqueue_and_poll(
            client,
            imported.graph_id,
            f"/api/graphs/{imported.graph_id}/views/{imported.view_id}/cluster",
            {
                "cluster": {
                    "space": {"method": "none"},
                    "hdbscan": {"minClusterSize": 2, "minSamples": 1},
                }
            },
        )
        layout_id = _enqueue_and_poll(
            client,
            imported.graph_id,
            f"/api/graphs/{imported.graph_id}/views/{imported.view_id}/layout",
            {"layout": {"nNeighbors": 3}},
        )
        label_id = _enqueue_and_poll(
            client,
            imported.graph_id,
            f"/api/graphs/{imported.graph_id}/views/{imported.view_id}/label",
            {},
        )
        trend_id = _enqueue_and_poll(
            client,
            imported.graph_id,
            f"/api/graphs/{imported.graph_id}/views/{imported.view_id}/trends",
            {"time": {"bucket": "month"}},
        )
        with connect(db_path) as conn:
            topic_id = conn.execute(
                """
                SELECT cluster_id
                  FROM cluster_summaries
                 WHERE run_id = ?
                 ORDER BY cluster_id
                 LIMIT 1
                """,
                (cluster_id,),
            ).fetchone()[0]

        artifact_response = client.get(
            f"/api/graphs/{imported.graph_id}/views/{imported.view_id}/artifact"
        )
        assert artifact_response.status_code == 200, artifact_response.text
        artifact = artifact_response.json()
        assert artifact["runRefs"] == {
            "embeddingRunId": imported.embedding_run_id,
            "clusterRunId": cluster_id,
            "layoutRunId": layout_id,
            "labelRunId": label_id,
            "trendRunId": trend_id,
        }
        assert artifact["representation"] == "external"
        assert artifact["config"]["embedding"]["fingerprint"] == ("sha256:qmd-synthetic-space-v1")
        point = artifact["data"][0]
        assert point["provenance"]["path"].startswith("memory/")

        evidence_response = client.post(
            f"/api/graphs/{imported.graph_id}/evidence",
            json={
                "viewId": imported.view_id,
                "recipe": "topic_evidence",
                "topicId": topic_id,
            },
        )
        assert evidence_response.status_code == 200, evidence_response.text
        representative = evidence_response.json()["evidence"]["representatives"][0]
        fixture_by_id = {chunk["externalId"]: chunk for chunk in _read_chunks(FIXTURE)}
        source = fixture_by_id[representative["provenance"]["externalId"]]
        assert representative["customerText"] == source["text"]
        assert representative["provenance"]["documentHash"] == source["documentHash"]
        assert representative["provenance"]["characterStart"] == source["characterStart"]
        assert representative["provenance"]["bundle"]["exportId"] == ("qmd-fixture-export-001")

        outliers = client.get(f"/api/graphs/{imported.graph_id}/views/{imported.view_id}/outliers")
        assert outliers.status_code == 200
        assert outliers.json()["records"]

        mixed_query = client.post(
            f"/api/graphs/{imported.graph_id}/evidence",
            json={
                "viewId": imported.view_id,
                "recipe": "topic_search",
                "question": "Which notes discuss deployment retries?",
            },
        )
        assert mixed_query.status_code == 422
        assert "proven compatible" in mixed_query.text


def _import(db_path: Path, bundle: Path = FIXTURE) -> Any:
    return import_external_vectors(
        db_path,
        format_name="qmd-memory-v1",
        input_path=bundle,
        dataset="synthetic-qmd",
    )


def _enqueue_and_poll(
    client: TestClient,
    graph_id: str,
    path: str,
    body: dict[str, Any],
) -> str:
    response = client.post(path, json=body)
    assert response.status_code == 201, response.text
    return _poll_run(client, graph_id, response.json()["id"])["id"]


def _poll_run(client: TestClient, graph_id: str, run_id: str) -> dict[str, Any]:
    run = _wait_terminal(client, graph_id, run_id)
    assert run["status"] == "succeeded", run
    return run


def _wait_terminal(client: TestClient, graph_id: str, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = client.get(f"/api/graphs/{graph_id}/runs/{run_id}").json()
        if run["status"] in {"succeeded", "failed", "cancelled"}:
            return run
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not complete")


def _copy_bundle(destination: Path) -> Path:
    shutil.copytree(FIXTURE, destination)
    return destination


def _read_chunks(bundle: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (bundle / "chunks.ndjson").read_text().splitlines()]


def _bundle_records_and_vectors(bundle: Path) -> tuple[list[dict[str, Any]], list[bytes]]:
    chunks = _read_chunks(bundle)
    payload = (bundle / "vectors.f32").read_bytes()
    vectors = [
        payload[chunk["vector"]["offset"] : chunk["vector"]["offset"] + chunk["vector"]["length"]]
        for chunk in chunks
    ]
    return chunks, vectors


def _write_bundle(
    bundle: Path,
    chunks: list[dict[str, Any]],
    vectors: list[bytes],
    *,
    export_id: str | None = None,
    exported_at: str | None = None,
) -> None:
    offset = 0
    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk["vector"] = {"offset": offset, "length": len(vector)}
        offset += len(vector)
    _write_chunks(bundle, chunks)
    (bundle / "vectors.f32").write_bytes(b"".join(vectors))
    manifest = _read_json(bundle / "manifest.json")
    manifest["chunkCount"] = len(chunks)
    manifest["documentCount"] = len({chunk["documentHash"] for chunk in chunks})
    if export_id is not None:
        manifest["exportId"] = export_id
    if exported_at is not None:
        manifest["exportedAt"] = exported_at
    _write_manifest(bundle, manifest)


def _write_chunks(bundle: Path, chunks: list[dict[str, Any]]) -> None:
    (bundle / "chunks.ndjson").write_text(
        "".join(
            json.dumps(chunk, sort_keys=True, separators=(",", ":")) + "\n" for chunk in chunks
        ),
        encoding="utf-8",
    )


def _write_manifest(bundle: Path, manifest: dict[str, Any]) -> None:
    manifest["checksums"] = {
        filename: _checksum_entry(bundle / filename)
        for filename in ("chunks.ndjson", "vectors.f32")
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _refresh_checksums(bundle)


def _refresh_checksums(bundle: Path) -> None:
    manifest_path = bundle / "manifest.json"
    manifest = _read_json(manifest_path)
    manifest["checksums"] = {
        filename: _checksum_entry(bundle / filename)
        for filename in ("chunks.ndjson", "vectors.f32")
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksums = {
        "algorithm": "sha256",
        "files": {
            filename: _checksum_entry(bundle / filename)
            for filename in ("manifest.json", "chunks.ndjson", "vectors.f32")
        },
    }
    (bundle / "checksums.json").write_text(
        json.dumps(checksums, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _checksum_entry(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
