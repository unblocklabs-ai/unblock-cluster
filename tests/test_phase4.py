from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from datagraph.core.embedding_text import render_embedding_text
from datagraph.core.labeling import LabelResult, LabelValidationError
from datagraph.db import connect
from datagraph.main import create_app
from datagraph.runs.label import (
    DEFAULT_LABEL_PROMPT,
    build_representative_blocks,
    effective_label_prompt,
    prompt_sha256,
)
from datagraph.settings import Settings
from scripts.gen_synthetic import generate_records
from tests.test_phase3 import StructuredTopicProvider


class ScriptedLabelProvider:
    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.calls: list[tuple[str, list[str]]] = []
        self.results: list[LabelResult] = []
        self.fail_calls: set[int] = set()
        self.invalid_failures_remaining = 0
        self.always_fail = False

    async def label_cluster(self, prompt: str, representatives: list[str]) -> LabelResult:
        call_index = len(self.calls)
        self.calls.append((prompt, representatives))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.invalid_failures_remaining:
            self.invalid_failures_remaining -= 1
            raise LabelValidationError("scripted invalid schema")
        if self.always_fail or call_index in self.fail_calls:
            raise RuntimeError("scripted cluster failure")
        if self.results:
            return self.results.pop(0)
        return LabelResult(
            label=f"Scripted Topic {call_index}",
            summary=f"Summary for scripted topic {call_index}",
            key_signals=[f"signal-{call_index}"],
            tags=[f"tag-{call_index}"],
            coherent=call_index != 0,
        )


def test_prompt_assembly_topk_truncation_absent_title_and_stable_format() -> None:
    records = [
        {
            "source_type": "support_ticket",
            "title": "Refund request",
            "customer_text": "x" * 760,
        },
        {
            "source_type": "social_comment",
            "title": None,
            "customer_text": "No title here",
        },
        {
            "source_type": "review",
            "title": "Ignored by topK",
            "customer_text": "Ignored",
        },
    ]

    blocks = build_representative_blocks(records, top_k=2)

    assert len(blocks) == 2
    assert blocks[0].startswith("Record 1\nsourceType: support_ticket\ntitle: Refund request\n")
    assert "customerText: " + ("x" * 697) + "..." in blocks[0]
    assert blocks[1] == "Record 2\nsourceType: social_comment\ncustomerText: No title here"
    assert all("Ignored" not in block for block in blocks)
    assert effective_label_prompt({"prompt": None}) == DEFAULT_LABEL_PROMPT
    assert effective_label_prompt({"prompt": "   "}) == DEFAULT_LABEL_PROMPT
    assert effective_label_prompt({"prompt": "Custom prompt"}) == "Custom prompt"


def test_label_run_persists_merges_and_relabels_subset(tmp_path: Path) -> None:
    label_provider = ScriptedLabelProvider(delay_seconds=0.02)
    with _phase4_client(tmp_path, label_provider) as client:
        graph_id, view_id, cluster_run_id = _cluster_fixture(client)
        cluster_ids = _cluster_ids(client, cluster_run_id)

        before = client.get(f"/api/graphs/{graph_id}/views/{view_id}/topics").json()
        assert before["topics"]
        assert all(topic["label"] is None for topic in before["topics"])

        label_run_id = _enqueue_label(client, graph_id, view_id)
        observed = _wait_for_label_progress(client, graph_id, label_run_id)
        assert observed["progress"]["total"] == len(cluster_ids)
        label_run = _poll_run(client, graph_id, label_run_id)

        assert label_run["status"] == "succeeded", label_run
        assert label_run["stats"]["targets"] == len(cluster_ids)
        assert label_run["stats"]["labeled"] == len(cluster_ids)
        assert label_run["stats"]["failed"] == 0
        assert label_run["stats"]["providerRequests"] == len(cluster_ids)
        assert label_run["stats"]["promptHash"] == prompt_sha256(DEFAULT_LABEL_PROMPT)
        refreshed_view = client.get(f"/api/graphs/{graph_id}/views/{view_id}").json()
        assert refreshed_view["defaultLabelRunId"] == label_run_id

        with connect(client.app.state.settings.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM cluster_labels WHERE label_run_id = ?",
                (label_run_id,),
            ).fetchall()
        assert len(rows) == len(cluster_ids)
        assert {row["model"] for row in rows} == {"gpt-5.4-mini"}
        assert {row["prompt_hash"] for row in rows} == {prompt_sha256(DEFAULT_LABEL_PROMPT)}
        assert {row["top_k"] for row in rows} == {12}

        topics = client.get(f"/api/graphs/{graph_id}/views/{view_id}/topics").json()
        labels_by_cluster = {topic["clusterId"]: topic["label"] for topic in topics["topics"]}
        first_cluster_id = cluster_ids[0]
        assert labels_by_cluster[first_cluster_id]["coherent"] is False
        assert labels_by_cluster[first_cluster_id]["labelRunId"] == label_run_id

        label_provider.results = [
            LabelResult(
                label="Updated Single Topic",
                summary="A new summary for one topic.",
                key_signals=["updated"],
                tags=["single"],
                coherent=True,
            )
        ]
        override_prompt = "Custom prompt for a single topic."
        relabel_run_id = _enqueue_label(
            client,
            graph_id,
            view_id,
            body={
                "clusterIds": [first_cluster_id],
                "labeling": {"prompt": override_prompt, "topK": 3},
            },
        )
        relabel_run = _poll_run(client, graph_id, relabel_run_id)
        assert relabel_run["status"] == "succeeded"
        assert relabel_run["stats"]["promptHash"] == prompt_sha256(override_prompt)

        relabeled = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics/{first_cluster_id}"
        ).json()
        assert relabeled["topic"]["label"]["label"] == "Updated Single Topic"
        assert relabeled["topic"]["label"]["labelRunId"] == relabel_run_id

        with connect(client.app.state.settings.db_path) as conn:
            history_count = conn.execute(
                """
                SELECT COUNT(*)
                  FROM cluster_labels
                 WHERE cluster_run_id = ? AND cluster_id = ?
                """,
                (cluster_run_id, first_cluster_id),
            ).fetchone()[0]
            newest = conn.execute(
                """
                SELECT top_k, prompt_hash
                  FROM cluster_labels
                 WHERE label_run_id = ?
                """,
                (relabel_run_id,),
            ).fetchone()
        assert history_count == 2
        assert newest["top_k"] == 3
        assert newest["prompt_hash"] == prompt_sha256(override_prompt)


def test_label_failure_policies_and_invalid_schema_retry(tmp_path: Path) -> None:
    label_provider = ScriptedLabelProvider()
    with _phase4_client(tmp_path, label_provider) as client:
        graph_id, view_id, cluster_run_id = _cluster_fixture(client)
        cluster_ids = _cluster_ids(client, cluster_run_id)

        label_provider.fail_calls = {0}
        partial_run_id = _enqueue_label(client, graph_id, view_id)
        partial_run = _poll_run(client, graph_id, partial_run_id)
        assert partial_run["status"] == "succeeded"
        assert partial_run["stats"]["failedClusterIds"] == [cluster_ids[0]]
        assert partial_run["stats"]["labeled"] == len(cluster_ids) - 1

        label_provider.fail_calls = set()
        label_provider.always_fail = True
        failed_run_id = _enqueue_label(
            client,
            graph_id,
            view_id,
            body={"clusterIds": [cluster_ids[0]]},
        )
        failed_run = _poll_run(client, graph_id, failed_run_id)
        assert failed_run["status"] == "failed"
        assert "all target clusters failed labeling" in failed_run["errorText"]

        label_provider.always_fail = False
        before_invalid_calls = len(label_provider.calls)
        label_provider.invalid_failures_remaining = 2
        invalid_run_id = _enqueue_label(
            client,
            graph_id,
            view_id,
            body={"clusterIds": [cluster_ids[1]]},
        )
        invalid_run = _poll_run(client, graph_id, invalid_run_id)
        assert invalid_run["status"] == "failed"
        assert invalid_run["stats"]["failedClusterIds"] == [cluster_ids[1]]
        assert len(label_provider.calls) - before_invalid_calls == 2


def test_label_409_and_cancel_preserves_partial_labels(tmp_path: Path) -> None:
    label_provider = ScriptedLabelProvider(delay_seconds=0.15)
    with _phase4_client(tmp_path, label_provider) as client:
        graph = _create_graph(client)
        no_cluster = client.post(
            f"/api/graphs/{graph['id']}/views/{_all_records_view_id(graph)}/label",
            json={},
        )
        assert no_cluster.status_code == 409
        assert "/cluster" in no_cluster.text

        graph_id, view_id, cluster_run_id = _cluster_fixture(client, graph=graph)
        cluster_ids = _cluster_ids(client, cluster_run_id)
        empty_cluster_ids = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/label",
            json={"clusterIds": []},
        )
        assert empty_cluster_ids.status_code == 422

        assert len(cluster_ids) > 4
        run_id = _enqueue_label(client, graph_id, view_id)
        _wait_for_label_progress(client, graph_id, run_id, minimum_labeled=1)

        cancelled = client.post(f"/api/graphs/{graph_id}/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        terminal = _poll_run(client, graph_id, run_id)
        assert terminal["status"] == "cancelled"
        assert terminal["stats"]["labeled"] > 0
        with connect(client.app.state.settings.db_path) as conn:
            persisted = conn.execute(
                "SELECT COUNT(*) FROM cluster_labels WHERE label_run_id = ?",
                (run_id,),
            ).fetchone()[0]
        assert persisted == terminal["stats"]["labeled"]
        assert persisted < len(cluster_ids)


def test_label_cancel_before_first_cluster_is_cancelled_not_failed(tmp_path: Path) -> None:
    label_provider = ScriptedLabelProvider(delay_seconds=0.5)
    with _phase4_client(tmp_path, label_provider) as client:
        graph_id, view_id, _cluster_run_id = _cluster_fixture(client, record_count=160)
        run_id = _enqueue_label(client, graph_id, view_id)
        _wait_for_label_progress(client, graph_id, run_id, minimum_labeled=0)

        cancelled = client.post(f"/api/graphs/{graph_id}/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        terminal = _poll_run(client, graph_id, run_id)
        assert terminal["status"] == "cancelled"
        assert terminal["stats"]["labeled"] == 0
        with connect(client.app.state.settings.db_path) as conn:
            persisted = conn.execute(
                "SELECT COUNT(*) FROM cluster_labels WHERE label_run_id = ?",
                (run_id,),
            ).fetchone()[0]
        assert persisted == 0


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set")
def test_real_openai_label_run_smoke(tmp_path: Path) -> None:
    with _phase4_client(tmp_path, label_provider=None) as client:
        graph_id, view_id, cluster_run_id = _cluster_fixture(client, record_count=160)
        cluster_ids = _cluster_ids(client, cluster_run_id)[:3]
        run_id = _enqueue_label(client, graph_id, view_id, body={"clusterIds": cluster_ids})
        run = _poll_run(client, graph_id, run_id, timeout=240)
        assert run["status"] == "succeeded"
        topics = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics",
            params={"clusterRunId": cluster_run_id},
        ).json()
        labeled = [
            topic["label"]
            for topic in topics["topics"]
            if topic["clusterId"] in set(cluster_ids)
        ]
        assert len(labeled) == len(cluster_ids)
        assert all(label["summary"] for label in labeled)
        assert all(1 <= len(label["label"].split()) <= 10 for label in labeled)


def _phase4_client(
    tmp_path: Path,
    label_provider: ScriptedLabelProvider | None,
) -> TestClient:
    embedding_provider = _structured_provider()
    label_factory = (lambda _config: label_provider) if label_provider is not None else None
    return TestClient(
        create_app(
            Settings(
                data_dir=tmp_path / "data",
                port=0,
                openai_api_key=os.environ.get("OPENAI_API_KEY"),
            ),
            embedding_provider_factory=lambda _config: embedding_provider,
            label_provider_factory=label_factory,
        )
    )


def _structured_provider(record_count: int = 500) -> StructuredTopicProvider:
    records = generate_records(5000, 42)[:record_count]
    text_config = {
        "textFields": ["title", "customerText", "product", "tags"],
        "maxInputTokens": 8000,
    }
    text_to_topic = {
        render_embedding_text(record, text_config).text: record["metadata"]["groundTruthTopicId"]
        for record in records
    }
    return StructuredTopicProvider(text_to_topic)


def _cluster_fixture(
    client: TestClient,
    *,
    graph: dict[str, Any] | None = None,
    record_count: int = 500,
) -> tuple[str, str, str]:
    records = generate_records(5000, 42)[:record_count]
    graph = graph or _create_graph(client)
    graph_id = graph["id"]
    _post_records(client, graph_id, records)
    view_id = _all_records_view_id(graph)
    embed_run_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
    embed_run = _poll_run(client, graph_id, embed_run_id, timeout=60)
    assert embed_run["status"] == "succeeded"
    cluster_run_id = client.post(
        f"/api/graphs/{graph_id}/views/{view_id}/cluster",
        json={"cluster": {"space": {"method": "none"}, "hdbscan": {"minClusterSize": 2}}},
    ).json()["id"]
    cluster_run = _poll_run(client, graph_id, cluster_run_id, timeout=120)
    assert cluster_run["status"] == "succeeded", cluster_run
    return graph_id, view_id, cluster_run_id


def _create_graph(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/graphs",
        json={
            "name": f"Phase 4 {time.monotonic_ns()}",
            "config": {
                "embedding": {
                    "provider": "mock",
                    "model": "structured-mock",
                    "dimensions": 32,
                    "textFields": ["title", "customerText", "product", "tags"],
                }
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


def _cluster_ids(client: TestClient, cluster_run_id: str) -> list[int]:
    with connect(client.app.state.settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT cluster_id
              FROM cluster_summaries
             WHERE run_id = ?
             ORDER BY cluster_id ASC
            """,
            (cluster_run_id,),
        ).fetchall()
    cluster_ids = [row["cluster_id"] for row in rows]
    assert cluster_ids
    return cluster_ids


def _enqueue_label(
    client: TestClient,
    graph_id: str,
    view_id: str,
    body: dict[str, Any] | None = None,
) -> str:
    response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/label", json=body or {})
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
        time.sleep(0.03)
    raise AssertionError(f"run did not finish; last={last}")


def _wait_for_label_progress(
    client: TestClient,
    graph_id: str,
    run_id: str,
    *,
    minimum_labeled: int = 0,
    timeout: float = 10,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/graphs/{graph_id}/runs/{run_id}")
        assert response.status_code == 200
        last = response.json()
        progress = last["progress"]
        if (
            last["status"] == "running"
            and "total" in progress
            and progress.get("labeled", 0) >= minimum_labeled
        ):
            return last
        time.sleep(0.02)
    raise AssertionError(f"label progress not observed; last={last}")
