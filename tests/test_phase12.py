from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from datagraph.core.ids import now_iso
from datagraph.db import connect
from datagraph.main import READ_ONLY_MESSAGE, create_app
from datagraph.settings import Settings
from scripts.bench_scale import run_benchmark
from tests.test_phase1 import _minimal_record


def test_read_only_mode_blocks_mutations_but_allows_gets_static_and_evidence(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    with TestClient(create_app(Settings(data_dir=data_dir, port=0))) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = graph["views"][0]["id"]
        run_id = _insert_run(client, graph_id=graph_id, view_id=view_id, status="queued")

    with TestClient(create_app(Settings(data_dir=data_dir, port=0, read_only=True))) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get(f"/api/graphs/{graph_id}").status_code == 200

        mutations = [
            (
                "post",
                "/api/graphs",
                {
                    "name": "blocked",
                    "config": {"embedding": {"textFields": ["customerText"]}},
                },
            ),
            ("patch", f"/api/graphs/{graph_id}", {"config": {"time": {"bucket": "day"}}}),
            ("delete", f"/api/graphs/{graph_id}", None),
            ("post", f"/api/graphs/{graph_id}/records", {"records": [_minimal_record("blocked")]}),
            ("post", f"/api/graphs/{graph_id}/embeddings", {}),
            ("post", f"/api/graphs/{graph_id}/views", {"name": "blocked", "scope": {}}),
            ("delete", f"/api/graphs/{graph_id}/views/{view_id}", None),
            ("post", f"/api/graphs/{graph_id}/views/{view_id}/cluster", {}),
            ("post", f"/api/graphs/{graph_id}/views/{view_id}/layout", {}),
            ("post", f"/api/graphs/{graph_id}/views/{view_id}/label", {}),
            ("post", f"/api/graphs/{graph_id}/views/{view_id}/trends", {}),
            ("post", f"/api/graphs/{graph_id}/runs/{run_id}/cancel", {}),
            ("delete", f"/api/graphs/{graph_id}/runs/{run_id}", None),
        ]
        for method, path, body in mutations:
            caller = getattr(client, method)
            response = caller(path, json=body) if body is not None else caller(path)
            assert response.status_code == 403, (method, path, response.text)
            assert response.json()["detail"] == READ_ONLY_MESSAGE

        evidence = client.post(
            f"/api/graphs/{graph_id}/evidence",
            json={"viewId": view_id, "recipe": "surprising_topics"},
        )
        assert evidence.status_code != 403


def test_delete_view_removes_only_view_scoped_runs_and_outputs(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path / "data", port=0))) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        all_records_view_id = graph["views"][0]["id"]
        record_id = _insert_record(client, graph_id)
        scoped_view = client.post(
            f"/api/graphs/{graph_id}/views",
            json={"name": "scoped", "scope": {"sourceTypes": ["support_ticket"]}},
        ).json()
        scoped_run_id = _insert_run(
            client,
            graph_id=graph_id,
            view_id=scoped_view["id"],
            status="succeeded",
            run_type="cluster",
        )
        graph_run_id = _insert_run(
            client,
            graph_id=graph_id,
            view_id=None,
            status="succeeded",
            run_type="embed",
        )
        _insert_cluster_summary(client, scoped_run_id)

        all_records_delete = client.delete(f"/api/graphs/{graph_id}/views/{all_records_view_id}")
        assert all_records_delete.status_code == 409
        assert "all_records" in all_records_delete.text

        response = client.delete(f"/api/graphs/{graph_id}/views/{scoped_view['id']}")
        assert response.status_code == 204, response.text

        with connect(client.app.state.settings.db_path) as conn:
            assert _count_rows(conn, "views", "id", scoped_view["id"]) == 0
            assert _count_rows(conn, "runs", "id", scoped_run_id) == 0
            assert _count_rows(conn, "cluster_summaries", "run_id", scoped_run_id) == 0
            assert _count_rows(conn, "runs", "id", graph_run_id) == 1
            assert _count_rows(conn, "records", "id", record_id) == 1


def test_delete_run_requires_terminal_and_not_view_default(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path / "data", port=0))) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = graph["views"][0]["id"]
        queued_run_id = _insert_run(client, graph_id=graph_id, view_id=view_id, status="queued")
        terminal_run_id = _insert_run(
            client,
            graph_id=graph_id,
            view_id=view_id,
            status="succeeded",
            run_type="cluster",
        )
        _insert_cluster_summary(client, terminal_run_id)

        queued_delete = client.delete(f"/api/graphs/{graph_id}/runs/{queued_run_id}")
        assert queued_delete.status_code == 409
        assert "cancel" in queued_delete.text

        _set_view_default(client, view_id, "default_cluster_run_id", terminal_run_id)
        default_delete = client.delete(f"/api/graphs/{graph_id}/runs/{terminal_run_id}")
        assert default_delete.status_code == 409
        assert "defaultClusterRunId" in default_delete.text
        assert "all_records" in default_delete.text

        _set_view_default(client, view_id, "default_cluster_run_id", None)
        response = client.delete(f"/api/graphs/{graph_id}/runs/{terminal_run_id}")
        assert response.status_code == 204, response.text
        with connect(client.app.state.settings.db_path) as conn:
            assert _count_rows(conn, "runs", "id", terminal_run_id) == 0
            assert _count_rows(conn, "cluster_summaries", "run_id", terminal_run_id) == 0


def test_scale_benchmark_smoke_path(tmp_path: Path) -> None:
    metrics = run_benchmark(data_dir=tmp_path / "bench", size=500, port=0)

    assert metrics["recordCount"] == 500
    assert metrics["embeddingDimensions"] == 1536
    assert metrics["upload"]["batches"] == 1
    assert metrics["upload"]["created"] == 500
    assert metrics["artifact"]["records"] == 500
    assert metrics["artifact"]["gzipWireBytes"] > 0
    assert metrics["runs"]["cluster"]["status"] == "succeeded"
    assert metrics["runs"]["layout"]["status"] == "succeeded"
    assert set(metrics["phaseDurations"]["cluster"]) == {
        "loading",
        "reducing",
        "clustering",
        "persisting",
    }


def _create_graph(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/graphs",
        json={
            "name": "Phase 12",
            "config": {"embedding": {"textFields": ["customerText"]}},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _insert_run(
    client: TestClient,
    *,
    graph_id: str,
    view_id: str | None,
    status: str,
    run_type: str = "noop",
) -> str:
    run_id = f"run_phase12_{status}_{run_type}_{view_id or 'graph'}"
    now = now_iso()
    with connect(client.app.state.settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (
              id, graph_id, view_id, type, status, params_json, progress_json,
              error_text, input_refs_json, stats_json, created_at, started_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                graph_id,
                view_id,
                run_type,
                status,
                json.dumps({}),
                json.dumps({"state": status}),
                json.dumps({}),
                json.dumps({}),
                now,
                now if status != "queued" else None,
                now if status in {"succeeded", "failed", "cancelled"} else None,
            ),
        )
        conn.commit()
    return run_id


def _insert_record(client: TestClient, graph_id: str) -> str:
    response = client.post(
        f"/api/graphs/{graph_id}/records",
        json={"records": [_minimal_record("phase12-record")]},
    )
    assert response.status_code == 200, response.text
    with connect(client.app.state.settings.db_path) as conn:
        return conn.execute("SELECT id FROM records WHERE graph_id = ?", (graph_id,)).fetchone()[0]


def _insert_cluster_summary(client: TestClient, run_id: str) -> None:
    with connect(client.app.state.settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO cluster_summaries (
              run_id, cluster_id, size, mean_probability,
              representative_record_ids_json, source_mix_json
            )
            VALUES (?, 0, 1, 1.0, '[]', '{}')
            """,
            (run_id,),
        )
        conn.commit()


def _set_view_default(
    client: TestClient,
    view_id: str,
    column: str,
    run_id: str | None,
) -> None:
    with connect(client.app.state.settings.db_path) as conn:
        conn.execute(f"UPDATE views SET {column} = ? WHERE id = ?", (run_id, view_id))
        conn.commit()


def _count_rows(conn: Any, table: str, column: str, value: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (value,)).fetchone()[0]
