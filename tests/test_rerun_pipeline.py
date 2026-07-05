from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from datagraph.core.embedding_text import render_embedding_text
from datagraph.main import create_app
from datagraph.settings import Settings
from scripts.gen_synthetic import generate_records
from scripts.rerun_pipeline import InProcessApiClient, run_rerun_pipeline
from tests.helpers import test_settings
from tests.test_clustering_layout import StructuredTopicProvider
from tests.test_labeling import ScriptedLabelProvider


def test_rerun_pipeline_smoke_api_only_sets_view_defaults(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:120]
    embedding_provider = _structured_provider(records)
    label_provider = ScriptedLabelProvider()
    with TestClient(
        create_app(
            test_settings(tmp_path / "data"),
            embedding_provider_factory=lambda _config: embedding_provider,
            label_provider_factory=lambda _config: label_provider,
        )
    ) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = next(view["id"] for view in graph["views"] if view["name"] == "all_records")
        _post_records(client, graph_id, records)

        output = io.StringIO()
        result = run_rerun_pipeline(
            InProcessApiClient(client),
            graph_id=graph_id,
            view_id=view_id,
            overrides={"cluster": {"space": {"method": "none"}}},
            poll_interval=0.01,
            timeout=120,
            output=output,
        )

        assert result["vizUrl"].endswith(f"/?graphId={graph_id}&viewId={view_id}")
        assert "vizUrl:" in output.getvalue()
        by_stage = {run["stage"]: run["id"] for run in result["runs"]}
        view = client.get(f"/api/graphs/{graph_id}/views/{view_id}").json()
        assert view["defaultEmbeddingRunId"] == by_stage["embed"]
        assert view["defaultClusterRunId"] == by_stage["cluster"]
        assert view["defaultLayoutRunId"] == by_stage["layout"]
        assert view["defaultLabelRunId"] == by_stage["label"]
        assert view["defaultTrendRunId"] == by_stage["trend"]


def test_rerun_pipeline_uses_api_read_only_guard(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    with TestClient(create_app(test_settings(data_dir))) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = next(view["id"] for view in graph["views"] if view["name"] == "all_records")
        _post_records(client, graph_id, generate_records(5000, 42)[:5])

    with TestClient(
        create_app(Settings(data_dir=data_dir, port=0, read_only=True, inline_cpu_runs=True))
    ) as read_only_client:
        with pytest.raises(Exception, match="403 Forbidden"):
            run_rerun_pipeline(
                InProcessApiClient(read_only_client),
                graph_id=graph_id,
                view_id=view_id,
                poll_interval=0.01,
                timeout=10,
                output=io.StringIO(),
            )

        view = read_only_client.get(f"/api/graphs/{graph_id}/views/{view_id}").json()
        assert view["defaultEmbeddingRunId"] is None
        assert view["defaultClusterRunId"] is None
        assert view["defaultLayoutRunId"] is None
        assert view["defaultLabelRunId"] is None
        assert view["defaultTrendRunId"] is None


def _structured_provider(records: list[dict[str, Any]]) -> StructuredTopicProvider:
    text_config = {
        "textFields": ["title", "customerText", "product", "tags"],
        "maxInputTokens": 8000,
    }
    text_to_topic = {
        render_embedding_text(record, text_config).text: record["metadata"]["groundTruthTopicId"]
        for record in records
    }
    return StructuredTopicProvider(text_to_topic)


def _create_graph(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/graphs",
        json={
            "name": f"Phase 20 rerun {time.monotonic_ns()}",
            "config": {
                "embedding": {
                    "provider": "mock",
                    "model": "structured-mock",
                    "dimensions": 32,
                    "textFields": ["title", "customerText", "product", "tags"],
                },
                "cluster": {
                    "space": {"method": "none"},
                    "hdbscan": {"minClusterSize": 2, "minSamples": 1},
                    "seed": 42,
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _post_records(client: TestClient, graph_id: str, records: list[dict[str, Any]]) -> None:
    response = client.post(f"/api/graphs/{graph_id}/records", json={"records": records})
    assert response.status_code == 200, response.text
