from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from datagraph.core.embedding_text import render_embedding_text
from datagraph.db import connect
from datagraph.main import create_app
from scripts.gen_synthetic import generate_records
from tests.helpers import test_settings
from tests.test_clustering_layout import StructuredTopicProvider
from tests.test_labeling import ScriptedLabelProvider


@pytest.mark.slow
def test_view_artifact_shape_labels_trends_and_truncation(built_graph: Any) -> None:
    client = built_graph.client
    graph_id = built_graph.graph_id
    view_id = built_graph.view_id

    run_count_before = _run_count(client, graph_id)
    response = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact")
    assert response.status_code == 200, response.text
    assert _run_count(client, graph_id) == run_count_before
    artifact = response.json()
    assert artifact["graphId"] == graph_id
    assert artifact["viewId"] == view_id
    assert artifact["runRefs"] == {
        "embeddingRunId": built_graph.embedding_run_id,
        "clusterRunId": built_graph.cluster_run_id,
        "layoutRunId": built_graph.layout_run_id,
        "labelRunId": built_graph.label_run_id,
        "trendRunId": built_graph.trend_run_id,
    }
    assert artifact["layout"]["method"] == "umap"
    assert artifact["noise"]["noiseCount"] >= 0
    assert len(artifact["data"]) == len(built_graph.records)
    required_record_fields = {"x", "y", "clusterProbability", "outlierScore"}
    assert all(required_record_fields <= set(row) for row in artifact["data"])
    assert any(
        row["customerText"].endswith("...") and len(row["customerText"]) == 300
        for row in artifact["data"]
    )
    assert artifact["topics"]
    assert any(
        topic["label"] is not None and topic["summary"] is not None for topic in artifact["topics"]
    )
    assert any(topic["trend"] is not None for topic in artifact["topics"])


@pytest.mark.slow
def test_view_artifact_409_matrix_and_optional_label_trend(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:160]
    with _phase7_client(tmp_path, records, ScriptedLabelProvider()) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        missing_cluster = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact")
        assert missing_cluster.status_code == 409
        assert "/cluster" in missing_cluster.text

        _post_records(client, graph_id, records)
        embed_run_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        assert _poll_run(client, graph_id, embed_run_id, timeout=60)["status"] == "succeeded"
        cluster_run_id = _enqueue_cluster(client, graph_id, view_id)
        assert _poll_run(client, graph_id, cluster_run_id, timeout=120)["status"] == "succeeded"
        missing_layout = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact")
        assert missing_layout.status_code == 409
        assert "/layout" in missing_layout.text

        layout_response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/layout", json={})
        layout_run_id = layout_response.json()["id"]
        assert _poll_run(client, graph_id, layout_run_id, timeout=120)["status"] == "succeeded"
        response = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact")
        assert response.status_code == 200, response.text
        artifact = response.json()
        assert artifact["runRefs"] == {
            "embeddingRunId": embed_run_id,
            "clusterRunId": cluster_run_id,
            "layoutRunId": layout_run_id,
        }
        assert all(topic["label"] is None for topic in artifact["topics"])
        assert all(topic["trend"] is None for topic in artifact["topics"])


def _phase7_client(
    tmp_path: Path,
    records: list[dict[str, Any]],
    label_provider: ScriptedLabelProvider,
) -> TestClient:
    text_config = {
        "textFields": ["title", "customerText", "product", "tags"],
        "maxInputTokens": 8000,
    }
    text_to_topic = {
        render_embedding_text(record, text_config).text: record["metadata"]["groundTruthTopicId"]
        for record in records
    }
    provider = StructuredTopicProvider(text_to_topic)
    return TestClient(
        create_app(
            test_settings(tmp_path / "data"),
            embedding_provider_factory=lambda _config: provider,
            label_provider_factory=lambda _config: label_provider,
        )
    )


def _create_graph(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/graphs",
        json={
            "name": f"Phase 7 {time.monotonic_ns()}",
            "config": {
                "embedding": {
                    "provider": "mock",
                    "model": "structured-mock",
                    "dimensions": 32,
                    "textFields": ["title", "customerText", "product", "tags"],
                },
                "cluster": {
                    "space": {"method": "none"},
                    "hdbscan": {"minClusterSize": 10, "minSamples": 3},
                    "seed": 42,
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _post_records(client: TestClient, graph_id: str, records: list[dict[str, Any]]) -> None:
    for start in range(0, len(records), 1000):
        response = client.post(
            f"/api/graphs/{graph_id}/records",
            json={"records": records[start : start + 1000]},
        )
        assert response.status_code == 200, response.text


def _all_records_view_id(graph: dict[str, Any]) -> str:
    return next(view["id"] for view in graph["views"] if view["name"] == "all_records")


def _enqueue_cluster(client: TestClient, graph_id: str, view_id: str) -> str:
    response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/cluster", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _poll_run(
    client: TestClient,
    graph_id: str,
    run_id: str,
    timeout: float = 120,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/graphs/{graph_id}/runs/{run_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] in {"succeeded", "failed", "cancelled"}:
            return last
        time.sleep(0.01)
    raise AssertionError(f"run did not finish; last={last}")


def _run_count(client: TestClient, graph_id: str) -> int:
    with connect(client.app.state.settings.db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM runs WHERE graph_id = ?",
            (graph_id,),
        ).fetchone()[0]
