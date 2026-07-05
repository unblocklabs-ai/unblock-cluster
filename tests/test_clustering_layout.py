from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sklearn.metrics import adjusted_rand_score

from datagraph.api.views import validate_none_space_population
from datagraph.core.embedding_text import render_embedding_text
from datagraph.core.openai_client import MockEmbeddingProvider
from datagraph.core.vectors import normalize_l2
from datagraph.db import connect
from datagraph.main import create_app
from datagraph.runs.cluster import effective_hdbscan_params, select_representatives
from scripts.gen_synthetic import generate_records
from tests.helpers import test_settings


class StructuredTopicProvider(MockEmbeddingProvider):
    def __init__(self, text_to_topic: dict[str, str], *, dimensions: int = 32) -> None:
        super().__init__(dimensions=dimensions)
        self.text_to_topic = text_to_topic
        self.centroids = {
            topic: self._centroid(topic) for topic in sorted(set(text_to_topic.values()))
        }

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed_text(text) for text in texts]

    def embed_text(self, text: str) -> np.ndarray:
        topic = self.text_to_topic[text]
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
        noise = rng.normal(0, 0.01, self.dimensions).astype(np.float32)
        return normalize_l2(self.centroids[topic] + noise)

    def _centroid(self, topic: str) -> np.ndarray:
        seed = int.from_bytes(topic.encode("utf-8")[:8].ljust(8, b"0"), "big")
        rng = np.random.default_rng(seed)
        return normalize_l2(rng.normal(0, 1, self.dimensions).astype(np.float32))


def _client(tmp_path: Path, provider: Any | None = None) -> TestClient:
    factory = (lambda _config: provider) if provider is not None else None
    return TestClient(
        create_app(
            test_settings(tmp_path / "data"),
            embedding_provider_factory=factory,
        )
    )


def _create_graph(client: TestClient, *, embedding: dict[str, Any] | None = None) -> dict[str, Any]:
    config = {
        "embedding": {
            "provider": "mock",
            "model": "structured-mock",
            "dimensions": 32,
            "textFields": ["title", "customerText", "product", "tags"],
            "requestsPerMinute": 1000,
            "maxConcurrency": 4,
            **(embedding or {}),
        },
        "cluster": {
            "space": {"method": "umap", "nComponents": 10, "nNeighbors": 15, "metric": "cosine"},
            "hdbscan": {"minClusterSize": 15, "minSamples": 5},
            "seed": 42,
        },
    }
    response = client.post("/api/graphs", json={"name": "Phase 3", "config": config})
    assert response.status_code == 201, response.text
    return response.json()


def _create_view(
    client: TestClient,
    graph_id: str,
    name: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(f"/api/graphs/{graph_id}/views", json={"name": name, "scope": scope})
    assert response.status_code == 201, response.text
    return response.json()


def _post_records(client: TestClient, graph_id: str, records: list[dict[str, Any]]) -> None:
    for start in range(0, len(records), 1000):
        response = client.post(
            f"/api/graphs/{graph_id}/records",
            json={"records": records[start : start + 1000]},
        )
        assert response.status_code == 200, response.text


def _enqueue_embedding(client: TestClient, graph_id: str) -> str:
    response = client.post(f"/api/graphs/{graph_id}/embeddings", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _enqueue_cluster(
    client: TestClient,
    graph_id: str,
    view_id: str,
    body: dict[str, Any] | None = None,
) -> str:
    response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/cluster", json=body or {})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _enqueue_layout(
    client: TestClient,
    graph_id: str,
    view_id: str,
    body: dict[str, Any] | None = None,
) -> str:
    response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/layout", json=body or {})
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


def _all_records_view(client: TestClient, graph_id: str) -> dict[str, Any]:
    graph = client.get(f"/api/graphs/{graph_id}").json()
    return next(view for view in graph["views"] if view["name"] == "all_records")


def _membership_rows(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    with connect(client.app.state.settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT cm.*, r.normalized_json, r.source_type
              FROM cluster_memberships cm
              JOIN records r ON r.id = cm.record_id
             WHERE cm.run_id = ?
             ORDER BY cm.record_id ASC
            """,
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@pytest.mark.slow
def test_planted_structure_cluster_layout_and_read_apis(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:2000]
    text_config = {
        "textFields": ["title", "customerText", "product", "tags"],
        "maxInputTokens": 8000,
    }
    text_to_topic = {
        render_embedding_text(record, text_config).text: record["metadata"]["groundTruthTopicId"]
        for record in records
    }
    provider = StructuredTopicProvider(text_to_topic)
    with _client(tmp_path, provider) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        _post_records(client, graph_id, records)
        view = _all_records_view(client, graph_id)

        embed_run = _poll_run(client, graph_id, _enqueue_embedding(client, graph_id), timeout=60)
        assert embed_run["status"] == "succeeded"

        layout_run_id = _enqueue_layout(client, graph_id, view["id"])
        layout_run = _poll_run(client, graph_id, layout_run_id, timeout=120)
        assert layout_run["status"] == "succeeded"
        assert layout_run["progress"]["phase"] == "persisting"
        assert set(layout_run["stats"]["phaseDurations"]) == {"loading", "reducing", "persisting"}
        with connect(client.app.state.settings.db_path) as conn:
            layout_points = conn.execute(
                "SELECT x, y FROM layout_points WHERE run_id = ?",
                (layout_run_id,),
            ).fetchall()
            memberships_before_cluster = conn.execute(
                "SELECT COUNT(*) FROM cluster_memberships"
            ).fetchone()[0]
        assert len(layout_points) == 2000
        assert all(math.isfinite(row["x"]) and math.isfinite(row["y"]) for row in layout_points)
        assert memberships_before_cluster == 0
        view_after_layout = client.get(f"/api/graphs/{graph_id}/views/{view['id']}").json()
        assert view_after_layout["defaultEmbeddingRunId"] == embed_run["id"]
        assert view_after_layout["defaultLayoutRunId"] == layout_run_id

        cluster_run_id = _enqueue_cluster(client, graph_id, view["id"])
        cluster_run = _poll_run(client, graph_id, cluster_run_id, timeout=180)
        assert cluster_run["status"] == "succeeded", cluster_run
        assert cluster_run["progress"]["phase"] == "persisting"
        assert set(cluster_run["stats"]["phaseDurations"]) == {
            "loading",
            "reducing",
            "clustering",
            "persisting",
        }
        assert cluster_run["stats"]["population"] == 2000
        assert cluster_run["stats"]["missingEmbeddings"] == 0

        rows = _membership_rows(client, cluster_run_id)
        assert len(rows) == 2000
        labels = [row["cluster_id"] for row in rows if row["cluster_id"] != -1]
        truth = [
            json.loads(row["normalized_json"])["metadata"]["groundTruthTopicId"]
            for row in rows
            if row["cluster_id"] != -1
        ]
        ari = adjusted_rand_score(truth, labels)
        assert ari >= 0.8

        _assert_membership_integrity(client, cluster_run_id, rows)
        first_memberships = [(row["record_id"], row["cluster_id"]) for row in rows]
        second_cluster_run = _poll_run(
            client,
            graph_id,
            _enqueue_cluster(client, graph_id, view["id"]),
            timeout=180,
        )
        assert second_cluster_run["status"] == "succeeded"
        second_rows = _membership_rows(client, second_cluster_run["id"])
        assert [(row["record_id"], row["cluster_id"]) for row in second_rows] == first_memberships

        refreshed_view = client.get(f"/api/graphs/{graph_id}/views/{view['id']}").json()
        assert refreshed_view["defaultEmbeddingRunId"] == embed_run["id"]
        assert refreshed_view["defaultLayoutRunId"] == layout_run_id
        assert refreshed_view["defaultClusterRunId"] == second_cluster_run["id"]

        topics = client.get(f"/api/graphs/{graph_id}/views/{view['id']}/topics").json()
        assert topics["clusterRunId"] == second_cluster_run["id"]
        assert topics["embeddingRunId"] == embed_run["id"]
        assert topics["noise"] == {
            "noiseCount": second_cluster_run["stats"]["noiseCount"],
            "noiseRatio": second_cluster_run["stats"]["noiseRatio"],
        }
        assert topics["topics"]
        topic_id = topics["topics"][0]["clusterId"]
        assert topics["topics"][0]["label"] is None
        topic = client.get(f"/api/graphs/{graph_id}/views/{view['id']}/topics/{topic_id}").json()
        assert topic["embeddingRunId"] == embed_run["id"]
        assert topic["topic"]["clusterId"] == topic_id
        assert topic["topic"]["label"] is None

        with connect(client.app.state.settings.db_path) as conn:
            summary = conn.execute(
                """
                SELECT representative_record_ids_json
                  FROM cluster_summaries
                 WHERE run_id = ? AND cluster_id = ?
                """,
                (second_cluster_run["id"], topic_id),
            ).fetchone()
        representative_ids = json.loads(summary["representative_record_ids_json"])
        topic_records = client.get(
            f"/api/graphs/{graph_id}/views/{view['id']}/topics/{topic_id}/records",
            params={"clusterRunId": second_cluster_run["id"]},
        ).json()
        assert topic_records["embeddingRunId"] == embed_run["id"]
        assert [record["id"] for record in topic_records["records"]] == representative_ids[:12]
        top_five_records = client.get(
            f"/api/graphs/{graph_id}/views/{view['id']}/topics/{topic_id}/records",
            params={"topK": 5, "clusterRunId": second_cluster_run["id"]},
        ).json()
        assert [record["id"] for record in top_five_records["records"]] == representative_ids[:5]
        too_many_records = client.get(
            f"/api/graphs/{graph_id}/views/{view['id']}/topics/{topic_id}/records",
            params={"topK": 51, "clusterRunId": second_cluster_run["id"]},
        )
        assert too_many_records.status_code == 422
        outliers = client.get(f"/api/graphs/{graph_id}/views/{view['id']}/outliers").json()
        assert outliers["embeddingRunId"] == embed_run["id"]
        assert outliers["records"]


@pytest.mark.slow
def test_scoped_cluster_missing_embeddings_and_none_space_guard(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:240]
    text_config = {
        "textFields": ["title", "customerText", "product", "tags"],
        "maxInputTokens": 8000,
    }
    text_to_topic = {
        render_embedding_text(record, text_config).text: record["metadata"]["groundTruthTopicId"]
        for record in records
    }
    provider = StructuredTopicProvider(text_to_topic)
    with _client(tmp_path, provider) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        _post_records(client, graph_id, records)
        social_view = _create_view(
            client,
            graph_id,
            "social",
            {"sourceTypes": ["social_comment"]},
        )
        embed_run = _poll_run(client, graph_id, _enqueue_embedding(client, graph_id), timeout=60)
        assert embed_run["status"] == "succeeded"

        new_social = dict(
            next(record for record in records if record["sourceType"] == "social_comment")
        )
        new_social["recordId"] = "after-embed"
        new_social["sourceRecordId"] = "after-embed"
        new_social["customerText"] = "A new social comment after the embedding run."
        new_social["metadata"] = {"groundTruthTopicId": "after_embed"}
        _post_records(client, graph_id, [new_social])

        cluster_run_id = _enqueue_cluster(client, graph_id, social_view["id"])
        cluster_run = _poll_run(client, graph_id, cluster_run_id, timeout=120)
        assert cluster_run["status"] == "succeeded"
        expected_population = sum(
            1 for record in records if record["sourceType"] == "social_comment"
        )
        assert cluster_run["stats"]["population"] == expected_population
        assert cluster_run["stats"]["missingEmbeddings"] == 1
        rows = _membership_rows(client, cluster_run_id)
        assert {json.loads(row["normalized_json"])["sourceType"] for row in rows} == {
            "social_comment"
        }
        refreshed_social = client.get(f"/api/graphs/{graph_id}/views/{social_view['id']}").json()
        assert refreshed_social["defaultEmbeddingRunId"] == embed_run["id"]
        assert refreshed_social["defaultClusterRunId"] == cluster_run_id

        small_view = _create_view(
            client,
            graph_id,
            "small-none",
            {"sourceTypes": ["support_ticket"]},
        )
        none_run = _poll_run(
            client,
            graph_id,
            _enqueue_cluster(
                client,
                graph_id,
                small_view["id"],
                {"cluster": {"space": {"method": "none"}, "hdbscan": {"minClusterSize": 2}}},
            ),
            timeout=120,
        )
        assert none_run["status"] == "succeeded"

    with pytest.raises(HTTPException) as exc:
        validate_none_space_population(20_001)
    assert exc.value.status_code == 422


def test_cluster_and_topic_409s_and_representative_fallback(tmp_path: Path) -> None:
    member_ids = ["a", "b", "c"]
    vectors = np.eye(3, dtype=np.float32)
    reps = select_representatives(member_ids, vectors, np.array([0.1, 0.2, 0.3], dtype=np.float32))
    assert set(reps) == set(member_ids)

    with _client(tmp_path, StructuredTopicProvider({})) as client:
        invalid_layout = client.post(
            "/api/graphs",
            json={
                "name": "Invalid layout",
                "config": {
                    "embedding": {"textFields": ["customerText"]},
                    "layout": {"method": "pacmap"},
                },
            },
        )
        assert invalid_layout.status_code == 422
        assert "config.layout.method" in invalid_layout.text

        graph = _create_graph(client)
        graph_id = graph["id"]
        _post_records(client, graph_id, generate_records(5000, 42)[:10])
        view = _all_records_view(client, graph_id)
        no_embed = client.post(f"/api/graphs/{graph_id}/views/{view['id']}/cluster", json={})
        assert no_embed.status_code == 409
        assert "/embeddings" in no_embed.text

        no_cluster = client.get(f"/api/graphs/{graph_id}/views/{view['id']}/topics")
        assert no_cluster.status_code == 409
        assert "/cluster" in no_cluster.text


def _assert_membership_integrity(
    client: TestClient,
    run_id: str,
    rows: list[dict[str, Any]],
) -> None:
    seen = Counter(row["record_id"] for row in rows)
    assert all(count == 1 for count in seen.values())
    assert all(0 <= row["probability"] <= 1 for row in rows)
    assert all(math.isfinite(row["outlier_score"]) for row in rows)
    assert all((row["cluster_id"] == -1) == bool(row["is_noise"]) for row in rows)

    with connect(client.app.state.settings.db_path) as conn:
        summaries = conn.execute(
            "SELECT * FROM cluster_summaries WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    summary_size = sum(row["size"] for row in summaries)
    noise_count = sum(1 for row in rows if row["cluster_id"] == -1)
    assert summary_size + noise_count == len(rows)

    rows_by_cluster: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_cluster[row["cluster_id"]].append(row)
    for summary in summaries:
        cluster_id = summary["cluster_id"]
        members = rows_by_cluster[cluster_id]
        assert summary["size"] == len(members)
        reps = json.loads(summary["representative_record_ids_json"])
        assert len(reps) <= 20
        member_ids = {row["record_id"] for row in members}
        assert set(reps) <= member_ids
        high_probability_ids = {row["record_id"] for row in members if row["probability"] >= 0.7}
        if high_probability_ids:
            assert set(reps) <= high_probability_ids
        source_mix = json.loads(summary["source_mix_json"])
        assert source_mix == dict(Counter(row["source_type"] for row in members))


def test_effective_hdbscan_params_defaults_and_overrides() -> None:
    def config(min_cluster_size=None, min_samples=None):
        return {"hdbscan": {"minClusterSize": min_cluster_size, "minSamples": min_samples}}

    assert effective_hdbscan_params(config(), 1000) == (15, 10)
    assert effective_hdbscan_params(config(), 5000) == (25, 10)
    assert effective_hdbscan_params(config(), 20000) == (100, 10)
    assert effective_hdbscan_params(config(), 100000) == (150, 10)
    assert effective_hdbscan_params(config(min_cluster_size=40), 5000) == (40, 10)
    assert effective_hdbscan_params(config(min_samples=25), 5000) == (25, 25)
    assert effective_hdbscan_params(config(min_cluster_size=8), 100000) == (8, 8)
