from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Phase 3 real-embedding quality gate.")
    parser.add_argument("--size", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is required for scripts/quality_eval.py", file=sys.stderr)
        return 2

    if args.data_dir is None:
        with tempfile.TemporaryDirectory(prefix="datagraph-quality-") as temp_dir:
            return _run_quality_eval(Path(temp_dir), args.size, args.seed, args.timeout, api_key)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    return _run_quality_eval(args.data_dir, args.size, args.seed, args.timeout, api_key)


def _run_quality_eval(
    data_dir: Path,
    size: int,
    seed: int,
    timeout: float,
    api_key: str,
) -> int:
    from fastapi.testclient import TestClient

    from datagraph.main import create_app
    from datagraph.settings import Settings
    from scripts.gen_synthetic import generate_records

    records = generate_records(size, seed)
    settings = Settings(data_dir=data_dir, port=0, openai_api_key=api_key)
    with TestClient(create_app(settings)) as client:
        graph_id = _create_graph(client)
        _post_records(client, graph_id, records)
        view_id = _all_records_view_id(client, graph_id)

        embed_run = _poll_run(
            client,
            graph_id,
            _enqueue_embedding(client, graph_id),
            timeout=timeout,
        )
        if embed_run["status"] != "succeeded":
            return _fail("embedding run failed", embed_run)

        cluster_run = _poll_run(
            client,
            graph_id,
            _enqueue_cluster(client, graph_id, view_id),
            timeout=timeout,
        )
        if cluster_run["status"] != "succeeded":
            return _fail("cluster run failed", cluster_run)

        label_run = _poll_run(
            client,
            graph_id,
            _enqueue_label(client, graph_id, view_id),
            timeout=timeout,
        )
        if label_run["status"] != "succeeded":
            return _fail("label run failed", label_run)

        trend_run = _poll_run(
            client,
            graph_id,
            _enqueue_trend(client, graph_id, view_id),
            timeout=timeout,
        )
        if trend_run["status"] != "succeeded":
            return _fail("trend run failed", trend_run)
        trend_response = _get_trends(client, graph_id, view_id, trend_run["id"])

        layout_run = _poll_run(
            client,
            graph_id,
            _enqueue_layout(client, graph_id, view_id),
            timeout=timeout,
        )
        if layout_run["status"] != "succeeded":
            return _fail("layout run failed", layout_run)

        scores = _score_memberships(settings.db_path, cluster_run["id"])
        report = {
            "size": size,
            "seed": seed,
            "dataDir": str(data_dir),
            "embeddingRunId": embed_run["id"],
            "clusterRunId": cluster_run["id"],
            "labelRunId": label_run["id"],
            "trendRunId": trend_run["id"],
            "layoutRunId": layout_run["id"],
            "clusterStats": cluster_run["stats"],
            "labelStats": label_run["stats"],
            "trendStats": trend_run["stats"],
            "decemberSurprisingTopics": trend_response["summary"]["surprisingTopics"],
            **scores,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if scores["adjustedRandIndex"] >= 0.5 else 1


def _create_graph(client: Any) -> str:
    response = client.post(
        "/api/graphs",
        json={
            "name": "Quality Eval",
            "config": {
                "embedding": {
                    "provider": "openai",
                    "model": "text-embedding-3-small",
                    "dimensions": 1536,
                    "textFields": ["title", "customerText", "product", "tags"],
                    "requestsPerMinute": 500,
                    "maxConcurrency": 4,
                    "maxInputTokens": 8000,
                }
            },
        },
    )
    response.raise_for_status()
    return response.json()["id"]


def _post_records(client: Any, graph_id: str, records: list[dict[str, Any]]) -> None:
    for start in range(0, len(records), 1000):
        response = client.post(
            f"/api/graphs/{graph_id}/records",
            json={"records": records[start : start + 1000]},
        )
        response.raise_for_status()


def _all_records_view_id(client: Any, graph_id: str) -> str:
    response = client.get(f"/api/graphs/{graph_id}")
    response.raise_for_status()
    graph = response.json()
    return next(view["id"] for view in graph["views"] if view["name"] == "all_records")


def _enqueue_embedding(client: Any, graph_id: str) -> str:
    response = client.post(f"/api/graphs/{graph_id}/embeddings", json={})
    response.raise_for_status()
    return response.json()["id"]


def _enqueue_cluster(client: Any, graph_id: str, view_id: str) -> str:
    response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/cluster", json={})
    response.raise_for_status()
    return response.json()["id"]


def _enqueue_layout(client: Any, graph_id: str, view_id: str) -> str:
    response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/layout", json={})
    response.raise_for_status()
    return response.json()["id"]


def _enqueue_label(client: Any, graph_id: str, view_id: str) -> str:
    response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/label", json={})
    response.raise_for_status()
    return response.json()["id"]


def _enqueue_trend(client: Any, graph_id: str, view_id: str) -> str:
    response = client.post(
        f"/api/graphs/{graph_id}/views/{view_id}/trends",
        json={
            "time": {"bucket": "week"},
            "window": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
        },
    )
    response.raise_for_status()
    return response.json()["id"]


def _get_trends(client: Any, graph_id: str, view_id: str, trend_run_id: str) -> dict[str, Any]:
    response = client.get(
        f"/api/graphs/{graph_id}/views/{view_id}/trends",
        params={"trendRunId": trend_run_id},
    )
    response.raise_for_status()
    return response.json()


def _poll_run(client: Any, graph_id: str, run_id: str, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/graphs/{graph_id}/runs/{run_id}")
        response.raise_for_status()
        last = response.json()
        print(
            f"{last['type']} {run_id}: {last['status']} "
            f"progress={json.dumps(last['progress'], sort_keys=True)}",
            flush=True,
        )
        if last["status"] in {"succeeded", "failed", "cancelled"}:
            return last
        time.sleep(2.0)
    raise TimeoutError(f"run {run_id} did not finish before timeout; last={last}")


def _score_memberships(db_path: Path, cluster_run_id: str) -> dict[str, Any]:
    from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score

    from datagraph.db import connect

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT cm.cluster_id, r.normalized_json
              FROM cluster_memberships cm
              JOIN records r ON r.id = cm.record_id
             WHERE cm.run_id = ?
             ORDER BY r.id ASC
            """,
            (cluster_run_id,),
        ).fetchall()

    truth: list[str] = []
    labels: list[int] = []
    noise_count = 0
    for row in rows:
        if int(row["cluster_id"]) == -1:
            noise_count += 1
            continue
        normalized = json.loads(row["normalized_json"])
        truth.append(normalized["metadata"]["groundTruthTopicId"])
        labels.append(int(row["cluster_id"]))

    if not labels:
        return {
            "adjustedRandIndex": 0.0,
            "normalizedMutualInfo": 0.0,
            "scoredRecords": 0,
            "noiseRecords": noise_count,
            "totalMemberships": len(rows),
        }
    return {
        "adjustedRandIndex": adjusted_rand_score(truth, labels),
        "normalizedMutualInfo": normalized_mutual_info_score(truth, labels),
        "scoredRecords": len(labels),
        "noiseRecords": noise_count,
        "totalMemberships": len(rows),
    }


def _fail(message: str, run: dict[str, Any]) -> int:
    print(message, file=sys.stderr)
    print(json.dumps(run, indent=2, sort_keys=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
