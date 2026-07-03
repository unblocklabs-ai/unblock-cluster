from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from datagraph.core.embedding_text import render_embedding_text
from datagraph.core.time import parse_timestamp
from datagraph.core.trend_math import bucket_start, compute_trends
from datagraph.db import connect
from datagraph.main import create_app
from datagraph.settings import Settings
from scripts.gen_synthetic import generate_records
from tests.test_phase3 import StructuredTopicProvider


def test_trend_math_buckets_zero_fill_baselines_and_scores() -> None:
    assert bucket_start(_ms("2025-01-01T02:03:04Z"), "day") == "2025-01-01"
    assert bucket_start(_ms("2025-01-01T02:03:04Z"), "week") == "2024-12-30"
    assert bucket_start(_ms("2025-12-31T23:59:59Z"), "month") == "2025-12-01"

    rows = _trend_rows(
        {
            1: {"2025-01-01": 1, "2025-01-08": 1, "2025-01-15": 5},
            2: {"2025-01-15": 5},
            -1: {"2025-01-01": 1, "2025-01-15": 5},
        }
    )
    result = compute_trends(rows, bucket="week", window_start="2025-01-13", window_end="2025-01-13")
    assert result.buckets == ["2024-12-30", "2025-01-06", "2025-01-13"]
    cluster_one = [point for point in result.points if point.cluster_id == 1]
    assert [point.count for point in cluster_one] == [1, 1, 5]
    assert [round(point.share, 6) for point in cluster_one] == [0.5, 1.0, 0.333333]
    assert cluster_one[-1].baseline_mean == 1
    assert cluster_one[-1].baseline_std == 0
    assert cluster_one[-1].spike_score == 4
    assert result.summary["newTopics"] == [
        {"clusterId": 2, "firstBucket": "2025-01-13", "count": 5}
    ]


def test_trend_math_spike_floors_and_window_sections() -> None:
    counts = {
        1: {
            "2025-01-01": 4,
            "2025-01-08": 4,
            "2025-01-15": 8,
            "2025-01-22": 0,
        },
        2: {
            "2025-01-01": 0,
            "2025-01-08": 8,
            "2025-01-15": 12,
            "2025-01-22": 0,
        },
        3: {"2025-01-01": 2, "2025-01-08": 2, "2025-01-15": 0, "2025-01-22": 0},
        4: {"2025-01-01": 0, "2025-01-08": 0, "2025-01-15": 0, "2025-01-22": 0},
        -1: {"2025-01-22": 1},
    }
    result = compute_trends(
        _trend_rows(counts),
        bucket="week",
        window_start="2025-01-20",
        window_end="2025-01-20",
    )
    by_cluster_bucket = {
        (point.cluster_id, point.bucket_start): point for point in result.points
    }
    assert by_cluster_bucket[(1, "2025-01-13")].spike_score == 2
    assert by_cluster_bucket[(2, "2025-01-13")].spike_score == 2
    vanishing_ids = {row["clusterId"] for row in result.summary["vanishingTopics"]}
    assert 3 in vanishing_ids
    assert 4 not in vanishing_ids
    assert result.summary["fallingTopics"][0]["clusterId"] in {1, 2, 3}

    start_window = compute_trends(
        _trend_rows(counts),
        bucket="week",
        window_start="2024-12-30",
        window_end="2024-12-30",
    )
    assert start_window.summary["vanishingTopics"] == []
    assert start_window.summary["risingTopics"] == []
    assert start_window.summary["fallingTopics"] == []


def test_trend_api_validation_snapshots_integrity_and_determinism(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:360]
    with _phase5_client(tmp_path, records) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)

        no_cluster = client.post(f"/api/graphs/{graph_id}/views/{view_id}/trends", json={})
        assert no_cluster.status_code == 409
        assert "/cluster" in no_cluster.text

        no_trend = client.get(f"/api/graphs/{graph_id}/views/{view_id}/trends")
        assert no_trend.status_code == 409
        assert "/trends" in no_trend.text

        _post_records(client, graph_id, records)
        embed_run_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        assert _poll_run(client, graph_id, embed_run_id, timeout=60)["status"] == "succeeded"
        cluster_run_id = _enqueue_cluster(client, graph_id, view_id)
        cluster_run = _poll_run(client, graph_id, cluster_run_id, timeout=120)
        assert cluster_run["status"] == "succeeded", cluster_run

        before_topics = client.get(f"/api/graphs/{graph_id}/views/{view_id}/topics").json()
        assert all(topic["trend"] is None for topic in before_topics["topics"])

        for body in (
            {"window": {"start": "not-a-date", "end": "2025-12-31T23:59:59Z"}},
            {
                "window": {
                    "start": "2025-12-31T23:59:59Z",
                    "end": "2025-12-01T00:00:00Z",
                }
            },
            {"time": {"bucket": "hour"}},
            {"time": {"timestampField": "createdAt"}},
        ):
            response = client.post(
                f"/api/graphs/{graph_id}/views/{view_id}/trends",
                json=body,
            )
            assert response.status_code == 422

        body = {
            "time": {"bucket": "week"},
            "window": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
        }
        first_run = _poll_run(
            client,
            graph_id,
            client.post(f"/api/graphs/{graph_id}/views/{view_id}/trends", json=body).json()["id"],
            timeout=60,
        )
        second_run = _poll_run(
            client,
            graph_id,
            client.post(f"/api/graphs/{graph_id}/views/{view_id}/trends", json=body).json()["id"],
            timeout=60,
        )
        assert first_run["status"] == "succeeded"
        assert second_run["status"] == "succeeded"
        assert first_run["stats"]["bucket"] == "week"
        assert first_run["stats"]["window"] == {"start": "2025-12-01", "end": "2025-12-29"}

        view = client.get(f"/api/graphs/{graph_id}/views/{view_id}").json()
        assert view["defaultTrendRunId"] == second_run["id"]
        trends = client.get(f"/api/graphs/{graph_id}/views/{view_id}/trends").json()
        explicit = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/trends",
            params={"trendRunId": first_run["id"]},
        ).json()
        assert trends["trendRunId"] == second_run["id"]
        assert trends["clusterRunId"] == cluster_run_id
        assert trends["bucket"] == "week"
        assert trends["series"]
        assert all("label" in series for series in trends["series"])
        assert _comparable_trends(trends) == _comparable_trends(
            client.get(
                f"/api/graphs/{graph_id}/views/{view_id}/trends",
                params={"trendRunId": second_run["id"]},
            ).json()
        )
        assert explicit["summary"] == trends["summary"]

        _assert_trend_integrity(client, second_run["id"], cluster_run_id)
        after_topics = client.get(f"/api/graphs/{graph_id}/views/{view_id}/topics").json()
        assert any(topic["trend"] is not None for topic in after_topics["topics"])
        trended_topic = next(
            topic for topic in after_topics["topics"] if topic["trend"] is not None
        )
        detail = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics/{trended_topic['clusterId']}"
        ).json()
        assert detail["topic"]["trend"] == trended_topic["trend"]


def test_planted_temporal_patterns_surface_expected_topics(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:2500]
    with _phase5_client(tmp_path, records) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)
        embed_run_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        assert _poll_run(client, graph_id, embed_run_id, timeout=60)["status"] == "succeeded"
        cluster_run_id = _enqueue_cluster(client, graph_id, view_id)
        cluster_run = _poll_run(client, graph_id, cluster_run_id, timeout=180)
        assert cluster_run["status"] == "succeeded", cluster_run
        cluster_truth = _majority_truth_by_cluster(client, cluster_run_id)

        december = _run_trend(
            client,
            graph_id,
            view_id,
            {
                "time": {"bucket": "week"},
                "window": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
            },
        )
        surprising_truth = [
            cluster_truth[row["clusterId"]]
            for row in december["summary"]["surprisingTopics"]
            if row["clusterId"] in cluster_truth
        ]
        assert surprising_truth[0] == "december_energy_crash_spike"
        december_top = december["summary"]["surprisingTopics"][0]["topBucket"]
        assert december_top.startswith("2025-12")

        nov_dec = _run_trend(
            client,
            graph_id,
            view_id,
            {
                "time": {"bucket": "week"},
                "window": {"start": "2025-11-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
            },
        )
        new_truth = {
            cluster_truth[row["clusterId"]]
            for row in nov_dec["summary"]["newTopics"]
            if row["clusterId"] in cluster_truth
        }
        assert "november_creatine_questions" in new_truth

        july_dec = _run_trend(
            client,
            graph_id,
            view_id,
            {
                "time": {"bucket": "week"},
                "window": {"start": "2025-07-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
            },
        )
        vanishing_truth = {
            cluster_truth[row["clusterId"]]
            for row in july_dec["summary"]["vanishingTopics"]
            if row["clusterId"] in cluster_truth
        }
        assert "midyear_vanishing_packaging" in vanishing_truth


def _phase5_client(tmp_path: Path, records: list[dict[str, Any]]) -> TestClient:
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
        )
    )


def _create_graph(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/graphs",
        json={
            "name": f"Phase 5 {time.monotonic_ns()}",
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


def _run_trend(
    client: TestClient,
    graph_id: str,
    view_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/trends", json=body)
    assert response.status_code == 201, response.text
    run = _poll_run(client, graph_id, response.json()["id"], timeout=60)
    assert run["status"] == "succeeded", run
    trends = client.get(
        f"/api/graphs/{graph_id}/views/{view_id}/trends",
        params={"trendRunId": run["id"]},
    )
    assert trends.status_code == 200, trends.text
    return trends.json()


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


def _assert_trend_integrity(client: TestClient, trend_run_id: str, cluster_run_id: str) -> None:
    with connect(client.app.state.settings.db_path) as conn:
        trend_rows = conn.execute(
            "SELECT * FROM trend_results WHERE run_id = ?",
            (trend_run_id,),
        ).fetchall()
        noise_rows = conn.execute(
            """
            SELECT COUNT(*)
              FROM cluster_memberships
             WHERE run_id = ? AND cluster_id = -1
            """,
            (cluster_run_id,),
        ).fetchone()[0]
        topic_counts = conn.execute(
            """
            SELECT bucket_start, SUM(count) AS count_sum
              FROM trend_results
             WHERE run_id = ?
             GROUP BY bucket_start
            """,
            (trend_run_id,),
        ).fetchall()
        memberships = conn.execute(
            """
            SELECT r.timestamp_ms
              FROM cluster_memberships cm
              JOIN records r ON r.id = cm.record_id
             WHERE cm.run_id = ?
            """,
            (cluster_run_id,),
        ).fetchall()
    assert trend_rows
    assert all(row["cluster_id"] != -1 for row in trend_rows)
    assert all(0 <= row["share"] <= 1 for row in trend_rows)
    assert noise_rows >= 0
    assert len({row["bucket_start"] for row in trend_rows}) * len(
        {row["cluster_id"] for row in trend_rows}
    ) == len(trend_rows)
    totals = Counter(bucket_start(row["timestamp_ms"], "week") for row in memberships)
    assert topic_counts
    for row in topic_counts:
        assert row["count_sum"] <= totals[row["bucket_start"]]


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


def _comparable_trends(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "clusterRunId": response["clusterRunId"],
        "bucket": response["bucket"],
        "window": response["window"],
        "summary": response["summary"],
        "series": response["series"],
    }


def _trend_rows(counts_by_cluster: dict[int, dict[str, int]]) -> list[dict[str, Any]]:
    rows = []
    for cluster_id, counts in counts_by_cluster.items():
        for bucket, count in counts.items():
            for index in range(count):
                rows.append(
                    {
                        "cluster_id": cluster_id,
                        "is_noise": cluster_id == -1,
                        "timestamp_ms": _ms(f"{bucket}T12:{index % 60:02d}:00Z"),
                    }
                )
    return rows


def _ms(value: str) -> int:
    return parse_timestamp(value)[1]
