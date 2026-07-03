from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from datagraph.core.embedding_text import render_embedding_text
from datagraph.db import connect
from datagraph.main import create_app
from datagraph.settings import Settings
from scripts.gen_synthetic import generate_records
from tests.test_phase3 import StructuredTopicProvider
from tests.test_phase4 import ScriptedLabelProvider


def test_planted_evidence_recipes_events_freshness_and_determinism(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:2500]
    label_provider = ScriptedLabelProvider()
    with _phase6_client(tmp_path, records, label_provider) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)
        embed_run_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        assert _poll_run(client, graph_id, embed_run_id, timeout=60)["status"] == "succeeded"
        cluster_run_id = _enqueue_cluster(client, graph_id, view_id)
        cluster_run = _poll_run(client, graph_id, cluster_run_id, timeout=180)
        assert cluster_run["status"] == "succeeded", cluster_run
        label_run_id = client.post(f"/api/graphs/{graph_id}/views/{view_id}/label", json={}).json()[
            "id"
        ]
        label_run = _poll_run(client, graph_id, label_run_id, timeout=120)
        assert label_run["status"] == "succeeded", label_run
        trend_run_id = _enqueue_trend(
            client,
            graph_id,
            view_id,
            "2025-12-01T00:00:00Z",
            "2025-12-31T23:59:59Z",
        )
        trend_run = _poll_run(client, graph_id, trend_run_id, timeout=60)
        assert trend_run["status"] == "succeeded", trend_run

        late_records = [dict(record) for record in records[:3]]
        for index, record in enumerate(late_records):
            record["recordId"] = f"late-{index}"
            record["sourceRecordId"] = f"late-{index}"
            record["customerText"] = f"Late freshness record {index}"
        _post_records(client, graph_id, late_records)

        cluster_truth = _majority_truth_by_cluster(client, cluster_run_id)
        spike_cluster_id = _cluster_id_for_truth(cluster_truth, "december_energy_crash_spike")
        november_cluster_id = _cluster_id_for_truth(cluster_truth, "november_creatine_questions")
        vanishing_cluster_id = _cluster_id_for_truth(
            cluster_truth,
            "midyear_vanishing_packaging",
        )
        source_mix = _source_mix_by_cluster(client, cluster_run_id)

        run_count_before_evidence = _run_count(client, graph_id)
        label_calls_before_evidence = len(label_provider.calls)
        body = {
            "viewId": view_id,
            "recipe": "surprising_topics",
            "timeRange": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
            "topK": 10,
        }
        surprising = _post_evidence(client, graph_id, body)
        repeat = _post_evidence(client, graph_id, body)
        assert repeat == surprising
        assert surprising["runRefs"] == {
            "embeddingRunId": embed_run_id,
            "clusterRunId": cluster_run_id,
            "labelRunId": label_run_id,
            "trendRunId": trend_run_id,
        }
        assert surprising["freshness"]["recordsAddedSinceClusterRun"] == 3
        assert surprising["vizUrl"] == f"http://127.0.0.1:0/?graphId={graph_id}&viewId={view_id}"
        assert surprising["evidence"][0]["clusterId"] == spike_cluster_id
        assert surprising["evidence"][0]["label"].startswith("Scripted Topic")
        assert surprising["evidence"][0]["sourceMix"] == source_mix[spike_cluster_id]
        assert surprising["evidence"][0]["representativeRecordIds"]
        assert _run_count(client, graph_id) == run_count_before_evidence
        assert len(label_provider.calls) == label_calls_before_evidence
        default_window = _post_evidence(
            client,
            graph_id,
            {"viewId": view_id, "recipe": "surprising_topics"},
        )
        assert default_window == surprising

        nov_dec = {
            "viewId": view_id,
            "timeRange": {"start": "2025-11-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
        }
        new_topics = _post_evidence(client, graph_id, {**nov_dec, "recipe": "new_topics"})
        assert november_cluster_id in {row["clusterId"] for row in new_topics["evidence"]}

        july_dec = {
            "viewId": view_id,
            "timeRange": {"start": "2025-07-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
        }
        vanishing = _post_evidence(client, graph_id, {**july_dec, "recipe": "vanishing_topics"})
        assert vanishing_cluster_id in {row["clusterId"] for row in vanishing["evidence"]}

        july_trend_id = _enqueue_trend(
            client,
            graph_id,
            view_id,
            "2025-07-01T00:00:00Z",
            "2025-12-31T23:59:59Z",
        )
        assert _poll_run(client, graph_id, july_trend_id, timeout=60)["status"] == "succeeded"
        rising = _post_evidence(client, graph_id, {**july_dec, "recipe": "rising_topics"})
        trend_summary = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/trends",
            params={"trendRunId": july_trend_id},
        ).json()["summary"]
        assert [row["clusterId"] for row in rising["evidence"]] == [
            row["clusterId"] for row in trend_summary["risingTopics"][:10]
        ]
        assert all("deltaShare" in row and "sourceMix" in row for row in rising["evidence"])

        topic_evidence = _post_evidence(
            client,
            graph_id,
            {
                "viewId": view_id,
                "recipe": "topic_evidence",
                "topicId": spike_cluster_id,
                "topK": 7,
            },
        )
        topic = topic_evidence["evidence"]
        assert topic["label"]["label"].startswith("Scripted Topic")
        assert len(topic["representatives"]) <= 7
        assert all(record["customerText"] for record in topic["representatives"])
        assert topic["trend"]["series"]
        assert topic["trend"]["snapshot"]["topBucket"].startswith("2025-12")

        no_trend_view = client.post(
            f"/api/graphs/{graph_id}/views",
            json={"name": "no-trend", "scope": {"sourceTypes": ["support_ticket"]}},
        ).json()
        no_trend_cluster_id = _enqueue_cluster(client, graph_id, no_trend_view["id"])
        no_trend_cluster = _poll_run(client, graph_id, no_trend_cluster_id, timeout=120)
        assert no_trend_cluster["status"] == "succeeded"
        no_trend_topic_id = _first_cluster_id(client, no_trend_cluster_id)
        no_trend_topic = _post_evidence(
            client,
            graph_id,
            {
                "viewId": no_trend_view["id"],
                "recipe": "topic_evidence",
                "topicId": no_trend_topic_id,
            },
        )
        assert no_trend_topic["evidence"]["trend"] is None
        assert no_trend_topic["evidence"]["representatives"]

        compare = _post_evidence(
            client,
            graph_id,
            {
                "viewId": view_id,
                "recipe": "compare_periods",
                "periods": {
                    "a": {"start": "2025-01-01T00:00:00Z", "end": "2025-06-30T23:59:59Z"},
                    "b": {"start": "2025-07-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
                },
            },
        )
        deltas = {row["clusterId"]: row["deltaShare"] for row in compare["evidence"]}
        assert deltas[vanishing_cluster_id] < 0
        assert deltas[spike_cluster_id] > 0

        with connect(client.app.state.settings.db_path) as conn:
            events = conn.execute(
                """
                SELECT recipe, params_json, run_refs_json
                  FROM analysis_events
                 WHERE graph_id = ?
                 ORDER BY created_at ASC, id ASC
                """,
                (graph_id,),
            ).fetchall()
        assert len(events) == 9
        assert json.loads(events[0]["params_json"]) == body
        assert json.loads(events[0]["run_refs_json"]) == surprising["runRefs"]
        assert {event["recipe"] for event in events} >= {
            "surprising_topics",
            "new_topics",
            "vanishing_topics",
            "rising_topics",
            "topic_evidence",
            "compare_periods",
        }
        assert len(label_provider.calls) == label_calls_before_evidence


def test_evidence_validation_409s_and_deleted_stub(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:160]
    with _phase6_client(tmp_path, records, ScriptedLabelProvider()) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)

        unknown = client.post(
            f"/api/graphs/{graph_id}/evidence",
            json={"viewId": view_id, "recipe": "unknown"},
        )
        assert unknown.status_code == 422
        assert "surprising_topics" in unknown.text
        assert "compare_periods" in unknown.text

        no_cluster = client.post(
            f"/api/graphs/{graph_id}/evidence",
            json={"viewId": view_id, "recipe": "topic_evidence", "topicId": 0},
        )
        assert no_cluster.status_code == 409
        assert "/cluster" in no_cluster.text

        _post_records(client, graph_id, records)
        embed_run_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        assert _poll_run(client, graph_id, embed_run_id, timeout=60)["status"] == "succeeded"
        cluster_run_id = _enqueue_cluster(client, graph_id, view_id)
        assert _poll_run(client, graph_id, cluster_run_id, timeout=120)["status"] == "succeeded"

        missing_topic = client.post(
            f"/api/graphs/{graph_id}/evidence",
            json={"viewId": view_id, "recipe": "topic_evidence"},
        )
        assert missing_topic.status_code == 422
        missing_periods = client.post(
            f"/api/graphs/{graph_id}/evidence",
            json={"viewId": view_id, "recipe": "compare_periods"},
        )
        assert missing_periods.status_code == 422
        assert "periods" in missing_periods.text

        no_trend = client.post(
            f"/api/graphs/{graph_id}/evidence",
            json={"viewId": view_id, "recipe": "surprising_topics"},
        )
        assert no_trend.status_code == 409
        assert "/trends" in no_trend.text

        trend_run_id = _enqueue_trend(
            client,
            graph_id,
            view_id,
            "2025-12-01T00:00:00Z",
            "2025-12-31T23:59:59Z",
        )
        assert _poll_run(client, graph_id, trend_run_id, timeout=60)["status"] == "succeeded"

        for body in (
            {
                "viewId": view_id,
                "recipe": "surprising_topics",
                "timeRange": {"start": "bad", "end": "2025-12-31T23:59:59Z"},
            },
            {
                "viewId": view_id,
                "recipe": "surprising_topics",
                "timeRange": {
                    "start": "2025-12-31T23:59:59Z",
                    "end": "2025-12-01T00:00:00Z",
                },
            },
            {"viewId": view_id, "recipe": "topic_evidence", "topicId": 0, "extra": True},
            {"viewId": view_id, "recipe": "topic_evidence", "topicId": 0, "topK": 51},
            {
                "viewId": view_id,
                "recipe": "compare_periods",
                "periods": {"a": {"start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z"}},
            },
        ):
            response = client.post(f"/api/graphs/{graph_id}/evidence", json=body)
            assert response.status_code == 422

    assert not (Path(__file__).resolve().parents[1] / "datagraph/runs/trends.py").exists()


def _phase6_client(
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
            Settings(data_dir=tmp_path / "data", port=0),
            embedding_provider_factory=lambda _config: provider,
            label_provider_factory=lambda _config: label_provider,
        )
    )


def _create_graph(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/graphs",
        json={
            "name": f"Phase 6 {time.monotonic_ns()}",
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


def _enqueue_trend(
    client: TestClient,
    graph_id: str,
    view_id: str,
    start: str,
    end: str,
) -> str:
    response = client.post(
        f"/api/graphs/{graph_id}/views/{view_id}/trends",
        json={"time": {"bucket": "week"}, "window": {"start": start, "end": end}},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _post_evidence(client: TestClient, graph_id: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(f"/api/graphs/{graph_id}/evidence", json=body)
    assert response.status_code == 200, response.text
    return response.json()


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
        time.sleep(0.03)
    raise AssertionError(f"run did not finish; last={last}")


def _majority_truth_by_cluster(client: TestClient, cluster_run_id: str) -> dict[int, str]:
    with connect(client.app.state.settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT cm.cluster_id, r.normalized_json
              FROM cluster_memberships cm
              JOIN records r ON r.id = cm.record_id
             WHERE cm.run_id = ? AND cm.cluster_id != -1
            """,
            (cluster_run_id,),
        ).fetchall()
    counts: dict[int, Counter[str]] = defaultdict(Counter)
    for row in rows:
        normalized = json.loads(row["normalized_json"])
        counts[int(row["cluster_id"])][normalized["metadata"]["groundTruthTopicId"]] += 1
    return {
        cluster_id: counter.most_common(1)[0][0]
        for cluster_id, counter in counts.items()
    }


def _cluster_id_for_truth(cluster_truth: dict[int, str], truth: str) -> int:
    for cluster_id, candidate in cluster_truth.items():
        if candidate == truth:
            return cluster_id
    raise AssertionError(f"missing cluster for {truth}")


def _source_mix_by_cluster(client: TestClient, cluster_run_id: str) -> dict[int, dict[str, int]]:
    with connect(client.app.state.settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT cm.cluster_id, r.source_type
              FROM cluster_memberships cm
              JOIN records r ON r.id = cm.record_id
             WHERE cm.run_id = ? AND cm.cluster_id != -1
            """,
            (cluster_run_id,),
        ).fetchall()
    counts: dict[int, Counter[str]] = defaultdict(Counter)
    for row in rows:
        counts[int(row["cluster_id"])][row["source_type"]] += 1
    return {cluster_id: dict(counter) for cluster_id, counter in counts.items()}


def _first_cluster_id(client: TestClient, cluster_run_id: str) -> int:
    with connect(client.app.state.settings.db_path) as conn:
        row = conn.execute(
            """
            SELECT cluster_id
              FROM cluster_summaries
             WHERE run_id = ?
             ORDER BY cluster_id ASC
             LIMIT 1
            """,
            (cluster_run_id,),
        ).fetchone()
    assert row is not None
    return int(row["cluster_id"])


def _run_count(client: TestClient, graph_id: str) -> int:
    with connect(client.app.state.settings.db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM runs WHERE graph_id = ?",
            (graph_id,),
        ).fetchone()[0]
