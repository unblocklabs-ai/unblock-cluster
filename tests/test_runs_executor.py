from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from datagraph.core.embeddings import MockEmbeddingProvider
from datagraph.core.ids import now_iso
from datagraph.db import connect, initialize_database
from datagraph.main import create_app
from scripts.gen_synthetic import generate_records, write_records
from tests.conftest import insert_graph_for_tests
from tests.helpers import test_settings


def test_migrations_apply_fresh_and_are_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh" / "datagraph.sqlite3"
    initialize_database(db_path)
    initialize_database(db_path)

    with connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "graphs",
            "records",
            "views",
            "runs",
            "embedding_vectors",
            "embedding_items",
            "cluster_memberships",
            "cluster_summaries",
            "cluster_labels",
            "layout_points",
            "trend_results",
            "trend_summaries",
            "analysis_events",
            "record_summaries",
            "summary_items",
            "embedding_spaces",
            "external_datasets",
            "external_imports",
            "external_chunk_versions",
            "external_import_items",
            "external_vectors",
        } <= tables
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert {
            "idx_records_graph_timestamp_ms",
            "idx_records_graph_source_type",
            "idx_cluster_memberships_run_cluster",
            "idx_trend_results_run_cluster",
            "idx_summary_items_run_status",
            "idx_external_datasets_graph",
            "idx_external_imports_dataset_exported",
            "idx_external_chunk_current",
        } <= indexes
        record_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(records)")
        }
        assert "is_active" in record_columns
        chunk_version_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(external_chunk_versions)")
        }
        vector_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(external_vectors)")
        }
        assert "vector_id" in chunk_version_columns
        assert "vector_id" in vector_columns


def test_external_vector_migration_upgrades_existing_v2_records_as_active(tmp_path: Path) -> None:
    db_path = tmp_path / "upgrade" / "datagraph.sqlite3"
    db_path.parent.mkdir(parents=True)
    migrations = Path(__file__).parents[1] / "datagraph" / "migrations"
    with connect(db_path) as conn:
        conn.executescript((migrations / "001_initial.sql").read_text())
        conn.executescript((migrations / "002_record_summaries.sql").read_text())
        now = now_iso()
        conn.execute(
            "INSERT INTO graphs VALUES ('grf_existing', 'Existing', '{}', ?, ?)",
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO records (
              id, graph_id, record_key, source_type, source_name, source_record_id,
              title, customer_text, record_url, product, sku, rating, sentiment,
              tags_json, timestamp_utc, timestamp_ms, metadata_json, normalized_json,
              created_at, updated_at
            )
            VALUES (
              'rec_existing', 'grf_existing', 'existing', 'note', 'fixture', 'existing',
              NULL, 'existing text', NULL, NULL, NULL, NULL, NULL, NULL,
              '2026-01-01T00:00:00Z', 1767225600000, NULL, '{}', ?, ?
            )
            """,
            (now, now),
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()

    initialize_database(db_path)
    with connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute(
            "SELECT is_active FROM records WHERE id = 'rec_existing'"
        ).fetchone()[0] == 1


def _client(tmp_path: Path) -> TestClient:
    settings = test_settings(tmp_path / "data")
    return TestClient(create_app(settings))


def _poll_status(client: TestClient, graph_id: str, run_id: str, status: str) -> dict:
    deadline = time.monotonic() + 3
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/graphs/{graph_id}/runs/{run_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] == status:
            return last
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {status}; last={last}")


def test_noop_run_moves_queued_running_succeeded_through_polling_api(tmp_path: Path) -> None:
    graph_id = "grf_test_noop"
    with _client(tmp_path) as client:
        insert_graph_for_tests(client.app.state.settings.db_path, graph_id)
        run_id = client.app.state.run_executor.enqueue_noop(
            graph_id,
            queue_delay_seconds=0.2,
            delay_seconds=0.25,
        )

        queued = client.get(f"/api/graphs/{graph_id}/runs/{run_id}").json()
        assert queued["status"] == "queued"

        running = _poll_status(client, graph_id, run_id, "running")
        assert running["progress"]["state"] == "running"

        succeeded = _poll_status(client, graph_id, run_id, "succeeded")
        assert succeeded["completedAt"] is not None

        listed = client.get(
            f"/api/graphs/{graph_id}/runs",
            params={"type": "noop", "status": "succeeded"},
        )
        assert [row["id"] for row in listed.json()] == [run_id]


def test_cancel_queued_run_yields_cancelled(tmp_path: Path) -> None:
    graph_id = "grf_test_cancel"
    with _client(tmp_path) as client:
        insert_graph_for_tests(client.app.state.settings.db_path, graph_id)
        client.app.state.run_executor.enqueue_noop(graph_id, delay_seconds=0.4)
        queued_run_id = client.app.state.run_executor.enqueue_noop(graph_id, delay_seconds=0.01)

        response = client.post(f"/api/graphs/{graph_id}/runs/{queued_run_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"


def test_startup_recovery_marks_running_run_failed(tmp_path: Path) -> None:
    settings = test_settings(tmp_path / "data")
    initialize_database(settings.db_path)
    graph_id = "grf_recovery"
    run_id = "run_recovery"
    insert_graph_for_tests(settings.db_path, graph_id)
    with connect(settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (
              id, graph_id, view_id, type, status, params_json, progress_json,
              error_text, input_refs_json, stats_json, created_at, started_at, completed_at
            )
            VALUES (?, ?, NULL, 'noop', 'running', '{}', '{}', NULL, '{}', '{}', ?, ?, NULL)
            """,
            (run_id, graph_id, now_iso(), now_iso()),
        )
        conn.commit()

    with TestClient(create_app(settings)) as client:
        response = client.get(f"/api/graphs/{graph_id}/runs/{run_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["errorText"] == "interrupted by restart"


def test_mock_embedding_provider_is_deterministic_and_unit_norm() -> None:
    provider = MockEmbeddingProvider(dimensions=64)
    first = asyncio.run(provider.embed_texts(["same text", "same text", "different"]))[0]
    second = provider.embed_text("same text")
    third = provider.embed_text("different")

    assert first.dtype == np.float32
    assert np.array_equal(first, second)
    assert not np.array_equal(first, third)
    assert math.isclose(float(np.linalg.norm(first)), 1.0, rel_tol=1e-6)


def test_synthetic_generator_is_deterministic_and_has_temporal_patterns(tmp_path: Path) -> None:
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    records = generate_records(5000, 42)
    write_records(records, first_path)
    write_records(generate_records(5000, 42), second_path)

    assert first_path.read_bytes() == second_path.read_bytes()
    assert len(records) == 5000
    assert all(record["metadata"]["groundTruthTopicId"] for record in records)
    assert all(
        record["title"] is None and record["rating"] is None
        for record in records
        if record["sourceType"] == "social_comment"
    )

    by_topic_month: dict[str, Counter[int]] = defaultdict(Counter)
    for record in records:
        topic = record["metadata"]["groundTruthTopicId"]
        month = int(record["timestamp"][5:7])
        by_topic_month[topic][month] += 1

    december_spike = by_topic_month["december_energy_crash_spike"]
    previous_month_peak = max(december_spike[month] for month in range(1, 12))
    assert december_spike[12] >= previous_month_peak * 4

    november_first = by_topic_month["november_creatine_questions"]
    assert sum(november_first[month] for month in range(1, 11)) == 0
    assert november_first[11] > 0

    vanishing = by_topic_month["midyear_vanishing_packaging"]
    assert sum(vanishing[month] for month in range(1, 7)) > 0
    assert sum(vanishing[month] for month in range(7, 13)) == 0


def test_synthetic_generator_cli_writes_exact_record_count(tmp_path: Path) -> None:
    out = tmp_path / "synthetic-5k.json"
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "gen_synthetic.py"
    subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--size",
            "5000",
            "--seed",
            "42",
            "--out",
            str(out),
        ],
        check=True,
    )
    records = json.loads(out.read_text())
    assert len(records) == 5000
