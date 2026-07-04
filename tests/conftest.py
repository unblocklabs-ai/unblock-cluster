from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from datagraph.core.ids import now_iso
from datagraph.db import connect
from scripts.gen_synthetic import generate_records


# The session built graph is shared only by read-only tests. Any test that mutates
# graph/run/output state must build its own isolated tmp_path graph.
@dataclass
class BuiltGraph:
    client: TestClient
    graph_id: str
    view_id: str
    records: list[dict[str, Any]]
    embedding_run_id: str
    cluster_run_id: str
    layout_run_id: str
    label_run_id: str
    trend_run_id: str


@pytest.fixture(scope="session")
def built_graph(tmp_path_factory: pytest.TempPathFactory) -> BuiltGraph:
    from tests.test_artifact_contract import (
        _all_records_view_id,
        _create_graph,
        _enqueue_cluster,
        _phase7_client,
        _poll_run,
        _post_records,
    )
    from tests.test_labeling import ScriptedLabelProvider

    data_root = tmp_path_factory.mktemp("built-graph")
    records = generate_records(5000, 42)[:500]
    records[0] = {**records[0], "customerText": "x" * 500}
    label_provider = ScriptedLabelProvider()
    with _phase7_client(data_root, records, label_provider) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)

        embedding_run_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        assert _poll_run(client, graph_id, embedding_run_id, timeout=60)["status"] == "succeeded"
        cluster_run_id = _enqueue_cluster(client, graph_id, view_id)
        assert _poll_run(client, graph_id, cluster_run_id, timeout=120)["status"] == "succeeded"
        layout_run_id = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/layout",
            json={},
        ).json()["id"]
        assert _poll_run(client, graph_id, layout_run_id, timeout=120)["status"] == "succeeded"
        label_run_id = client.post(f"/api/graphs/{graph_id}/views/{view_id}/label", json={}).json()[
            "id"
        ]
        assert _poll_run(client, graph_id, label_run_id, timeout=120)["status"] == "succeeded"
        trend_run_id = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/trends",
            json={
                "time": {"bucket": "week"},
                "window": {
                    "start": "2025-12-01T00:00:00Z",
                    "end": "2025-12-31T23:59:59Z",
                },
            },
        ).json()["id"]
        assert _poll_run(client, graph_id, trend_run_id, timeout=60)["status"] == "succeeded"
        yield BuiltGraph(
            client=client,
            graph_id=graph_id,
            view_id=view_id,
            records=records,
            embedding_run_id=embedding_run_id,
            cluster_run_id=cluster_run_id,
            layout_run_id=layout_run_id,
            label_run_id=label_run_id,
            trend_run_id=trend_run_id,
        )


def insert_graph_for_tests(db_path: Path | str, graph_id: str, *, name: str = "Test Graph") -> None:
    now = now_iso()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO graphs (id, name, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                graph_id,
                name,
                json.dumps({"embedding": {"textFields": ["customerText"]}}),
                now,
                now,
            ),
        )
        conn.commit()
