from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from datagraph.core.embedding_text import render_embedding_text
from datagraph.core.trend_math import TREND_MATH_VERSION
from datagraph.db import connect
from datagraph.main import create_app
from scripts.gen_synthetic import generate_records
from tests.helpers import test_settings
from tests.test_clustering_layout import StructuredTopicProvider
from tests.test_labeling import ScriptedLabelProvider

RUN_KEYS = {
    "id",
    "graphId",
    "viewId",
    "type",
    "status",
    "params",
    "progress",
    "errorText",
    "inputRefs",
    "stats",
    "createdAt",
    "startedAt",
    "completedAt",
}


@pytest.mark.slow
def test_set_default_false_persists_outputs_without_promoting_defaults(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:180]
    with _phase8_client(tmp_path, records) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)

        embed_run_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        embed_run = _poll_run(client, graph_id, embed_run_id)
        assert embed_run["status"] == "succeeded"
        assert _snake_keys(embed_run) == []

        cluster_initial = _run_view_action(client, graph_id, view_id, "cluster", {})
        layout_initial = _run_view_action(client, graph_id, view_id, "layout", {})
        label_initial = _run_view_action(client, graph_id, view_id, "label", {})
        trend_initial = _run_view_action(
            client,
            graph_id,
            view_id,
            "trends",
            {
                "time": {"bucket": "week"},
                "window": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
            },
        )
        defaults = _view_defaults(client, graph_id, view_id)
        assert defaults == {
            "defaultEmbeddingRunId": embed_run_id,
            "defaultClusterRunId": cluster_initial["id"],
            "defaultLayoutRunId": layout_initial["id"],
            "defaultLabelRunId": label_initial["id"],
            "defaultTrendRunId": trend_initial["id"],
        }

        cluster_false = _run_view_action(
            client,
            graph_id,
            view_id,
            "cluster",
            {"setDefault": False},
        )
        assert cluster_false["params"]["setDefault"] is False
        assert _table_count(client, "cluster_memberships", cluster_false["id"]) > 0
        assert _view_defaults(client, graph_id, view_id) == defaults
        cluster_true = _run_view_action(client, graph_id, view_id, "cluster", {"setDefault": True})
        assert cluster_true["params"]["setDefault"] is True
        defaults = {**defaults, "defaultClusterRunId": cluster_true["id"]}
        assert _view_defaults(client, graph_id, view_id) == defaults

        layout_false = _run_view_action(client, graph_id, view_id, "layout", {"setDefault": False})
        assert layout_false["params"]["setDefault"] is False
        assert _table_count(client, "layout_points", layout_false["id"]) > 0
        assert _view_defaults(client, graph_id, view_id) == defaults
        layout_true = _run_view_action(client, graph_id, view_id, "layout", {"setDefault": True})
        defaults = {**defaults, "defaultLayoutRunId": layout_true["id"]}
        assert _view_defaults(client, graph_id, view_id) == defaults

        label_false = _run_view_action(client, graph_id, view_id, "label", {"setDefault": False})
        assert label_false["params"]["setDefault"] is False
        assert _table_count(client, "cluster_labels", label_false["id"], column="label_run_id") > 0
        assert _view_defaults(client, graph_id, view_id) == defaults
        label_true = _run_view_action(client, graph_id, view_id, "label", {"setDefault": True})
        defaults = {**defaults, "defaultLabelRunId": label_true["id"]}
        assert _view_defaults(client, graph_id, view_id) == defaults

        trend_body = {
            "time": {"bucket": "week"},
            "window": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
        }
        trend_false = _run_view_action(
            client,
            graph_id,
            view_id,
            "trends",
            {**trend_body, "setDefault": False},
        )
        assert trend_false["params"]["setDefault"] is False
        assert _table_count(client, "trend_results", trend_false["id"]) > 0
        assert _view_defaults(client, graph_id, view_id) == defaults
        trend_true = _run_view_action(
            client,
            graph_id,
            view_id,
            "trends",
            {**trend_body, "setDefault": True},
        )
        defaults = {**defaults, "defaultTrendRunId": trend_true["id"]}
        assert _view_defaults(client, graph_id, view_id) == defaults

        for action in ("cluster", "layout", "label", "trends"):
            response = client.post(
                f"/api/graphs/{graph_id}/views/{view_id}/{action}",
                json={"setDefault": "false"},
            )
            assert response.status_code == 422
            assert "setDefault" in response.text


@pytest.mark.slow
def test_artifact_and_topics_warn_on_label_and_trend_mismatches(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:180]
    with _phase8_client(tmp_path, records) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)

        embed_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        assert _poll_run(client, graph_id, embed_id)["status"] == "succeeded"
        cluster_a = _run_view_action(client, graph_id, view_id, "cluster", {})
        _run_view_action(client, graph_id, view_id, "layout", {})
        _run_view_action(client, graph_id, view_id, "label", {})
        trend_initial = _run_view_action(
            client,
            graph_id,
            view_id,
            "trends",
            {
                "time": {"bucket": "week"},
                "window": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
            },
        )

        healthy_artifact = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact").json()
        healthy_topics = client.get(f"/api/graphs/{graph_id}/views/{view_id}/topics").json()
        healthy_trends = client.get(f"/api/graphs/{graph_id}/views/{view_id}/trends").json()
        assert healthy_artifact["warnings"] == []
        assert healthy_topics["warnings"] == []
        assert healthy_trends["warnings"] == []
        assert healthy_artifact["runRefs"]["clusterRunId"] == cluster_a["id"]
        assert any(topic["label"] is not None for topic in healthy_artifact["topics"])
        assert any(topic["trend"] is not None for topic in healthy_artifact["topics"])
        assert trend_initial["stats"]["mathVersion"] == TREND_MATH_VERSION

        _set_trend_math_version(client, trend_initial["id"], None)
        stale_artifact = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact").json()
        stale_topics = client.get(f"/api/graphs/{graph_id}/views/{view_id}/topics").json()
        stale_trends = client.get(f"/api/graphs/{graph_id}/views/{view_id}/trends").json()
        stale_topic = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics/"
            f"{stale_topics['topics'][0]['clusterId']}"
        ).json()
        for payload in (stale_artifact, stale_topics, stale_topic, stale_trends):
            assert _stale_trend_warning(payload["warnings"], trend_initial["id"])

        _set_trend_math_version(client, trend_initial["id"], 1)
        older_topics = client.get(f"/api/graphs/{graph_id}/views/{view_id}/topics").json()
        assert _stale_trend_warning(older_topics["warnings"], trend_initial["id"])

        _set_trend_math_version(client, trend_initial["id"], TREND_MATH_VERSION)
        refreshed_artifact = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact").json()
        refreshed_topics = client.get(f"/api/graphs/{graph_id}/views/{view_id}/topics").json()
        refreshed_trends = client.get(f"/api/graphs/{graph_id}/views/{view_id}/trends").json()
        assert refreshed_artifact["warnings"] == []
        assert refreshed_topics["warnings"] == []
        assert refreshed_trends["warnings"] == []

        tuning_false = _run_view_action(
            client,
            graph_id,
            view_id,
            "cluster",
            {"setDefault": False},
        )
        assert tuning_false["status"] == "succeeded"
        intact_artifact = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact").json()
        assert intact_artifact["runRefs"]["clusterRunId"] == cluster_a["id"]
        assert intact_artifact["warnings"] == []
        assert any(topic["label"] is not None for topic in intact_artifact["topics"])

        tuning_true = _run_view_action(
            client,
            graph_id,
            view_id,
            "cluster",
            {"setDefault": True},
        )
        assert tuning_true["status"] == "succeeded"
        warned_artifact = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact").json()
        warned_topics = client.get(f"/api/graphs/{graph_id}/views/{view_id}/topics").json()
        first_topic_id = warned_topics["topics"][0]["clusterId"]
        warned_topic = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics/{first_topic_id}"
        ).json()

        for payload in (warned_artifact, warned_topics, warned_topic):
            warnings = payload["warnings"]
            assert any("has no labels" in warning and "/label" in warning for warning in warnings)
            assert any(
                "default label run" in warning and "resolved cluster run" in warning
                for warning in warnings
            )
            assert any(
                "default trend run" in warning and "trend snapshots are absent" in warning
                for warning in warnings
            )

        assert warned_artifact["runRefs"]["clusterRunId"] == tuning_true["id"]
        assert all(topic["label"] is None for topic in warned_artifact["topics"])
        assert all(topic["trend"] is None for topic in warned_artifact["topics"])


def _set_trend_math_version(
    client: TestClient,
    run_id: str,
    version: int | None,
) -> None:
    with connect(client.app.state.settings.db_path) as conn:
        row = conn.execute("SELECT stats_json FROM runs WHERE id = ?", (run_id,)).fetchone()
        stats = json.loads(row["stats_json"])
        if version is None:
            stats.pop("mathVersion", None)
        else:
            stats["mathVersion"] = version
        conn.execute(
            "UPDATE runs SET stats_json = ? WHERE id = ?",
            (json.dumps(stats, sort_keys=True), run_id),
        )
        conn.commit()


def _stale_trend_warning(warnings: list[str], run_id: str) -> bool:
    return any(
        f"trend run {run_id} was computed with older trend math" in warning
        and f"v1 < v{TREND_MATH_VERSION}" in warning
        and "/trends" in warning
        for warning in warnings
    )


def _phase8_client(tmp_path: Path, records: list[dict[str, Any]]) -> TestClient:
    text_config = {
        "textFields": ["title", "customerText", "product", "tags"],
        "maxInputTokens": 8000,
    }
    text_to_topic = {
        render_embedding_text(record, text_config).text: record["metadata"]["groundTruthTopicId"]
        for record in records
    }
    provider = StructuredTopicProvider(text_to_topic)
    label_provider = ScriptedLabelProvider()
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
            "name": f"Phase 8 {time.monotonic_ns()}",
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


def _run_view_action(
    client: TestClient,
    graph_id: str,
    view_id: str,
    action: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/{action}", json=body)
    assert response.status_code == 201, response.text
    run = response.json()
    assert _snake_keys(run) == []
    assert set(run) == RUN_KEYS
    return _poll_run(client, graph_id, run["id"])


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
        assert _snake_keys(last) == []
        assert set(last) == RUN_KEYS
        if last["status"] in {"succeeded", "failed", "cancelled"}:
            return last
        time.sleep(0.01)
    raise AssertionError(f"run did not finish; last={last}")


def _snake_keys(payload: dict[str, Any]) -> list[str]:
    return sorted(key for key in payload if "_" in key)


def _view_defaults(client: TestClient, graph_id: str, view_id: str) -> dict[str, Any]:
    view = client.get(f"/api/graphs/{graph_id}/views/{view_id}").json()
    return {
        "defaultEmbeddingRunId": view["defaultEmbeddingRunId"],
        "defaultClusterRunId": view["defaultClusterRunId"],
        "defaultLayoutRunId": view["defaultLayoutRunId"],
        "defaultLabelRunId": view["defaultLabelRunId"],
        "defaultTrendRunId": view["defaultTrendRunId"],
    }


def _table_count(
    client: TestClient,
    table: str,
    run_id: str,
    *,
    column: str = "run_id",
) -> int:
    if table not in {"cluster_memberships", "layout_points", "cluster_labels", "trend_results"}:
        raise AssertionError(f"unexpected table: {table}")
    if column not in {"run_id", "label_run_id"}:
        raise AssertionError(f"unexpected column: {column}")
    with connect(client.app.state.settings.db_path) as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
            (run_id,),
        ).fetchone()[0]
