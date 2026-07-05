from __future__ import annotations

import asyncio
import json
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
from scripts.gen_synthetic import generate_records
from tests.helpers import test_settings
from tests.test_clustering_layout import StructuredTopicProvider
from tests.test_summarize import ScriptedSummaryProvider, StructuredSummaryEmbeddingProvider


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
    assert effective_label_prompt({"prompt": "  Custom prompt  "}) == "  Custom prompt  "
    assert effective_label_prompt(
        {"prompt": "Custom prompt", "promptAppend": "Use brand taxonomy."}
    ) == "Custom prompt\n\nAdditional brand instructions:\nUse brand taxonomy."


@pytest.mark.slow
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
        assert label_run["stats"]["textSource"] == "raw_customer_text"
        assert label_run["stats"]["fallbackRawCount"] == 0
        assert label_run["stats"]["exampleTextLimit"] == 700
        assert all(
            "junkType:" not in block
            for _, blocks in label_provider.calls
            for block in blocks
        )
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


@pytest.mark.slow
def test_label_text_source_lineage_fallback_report_and_quality(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:260]
    summary_provider = ScriptedSummaryProvider()
    embedding_provider = StructuredSummaryEmbeddingProvider()
    label_provider = ScriptedLabelProvider()
    with _phase20_client(
        tmp_path,
        summary_provider,
        embedding_provider,
        label_provider,
    ) as client:
        graph = _create_summary_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)
        summarize_run_id = client.post(f"/api/graphs/{graph_id}/summarize", json={}).json()["id"]
        assert _poll_run(client, graph_id, summarize_run_id, timeout=120)["status"] == "succeeded"
        embed_run_id = client.post(
            f"/api/graphs/{graph_id}/embeddings",
            json={"representation": "summary"},
        ).json()["id"]
        assert _poll_run(client, graph_id, embed_run_id, timeout=60)["status"] == "succeeded"
        cluster_run_id = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/cluster",
            json={
                "embeddingRunId": embed_run_id,
                "cluster": {"space": {"method": "none"}, "hdbscan": {"minClusterSize": 8}},
            },
        ).json()["id"]
        assert _poll_run(client, graph_id, cluster_run_id, timeout=120)["status"] == "succeeded"
        cluster_ids = _cluster_ids(client, cluster_run_id)
        representative_id = _first_representative_id(client, cluster_run_id, cluster_ids[0])
        fallback_raw_text = "I still need help with a long raw fallback example. " * 8
        with connect(client.app.state.settings.db_path) as conn:
            conn.execute(
                "DELETE FROM summary_items WHERE run_id = ? AND record_id = ?",
                (summarize_run_id, representative_id),
            )
            conn.execute(
                "UPDATE records SET customer_text = ? WHERE id = ?",
                (fallback_raw_text, representative_id),
            )
            conn.commit()

        label_provider.results = [
            LabelResult(
                label="Duplicate Label",
                summary="Repeated label for duplicate detection.",
                key_signals=["duplicate"],
                tags=["duplicate"],
                coherent=True,
            )
            for _ in cluster_ids
        ]
        prompt_append = "Use lifecycle-stage naming when it is visible."
        run_id = _enqueue_label(
            client,
            graph_id,
            view_id,
            body={
                "labeling": {
                    "promptAppend": prompt_append,
                    "exampleTextLimit": 220,
                    "topK": 4,
                }
            },
        )
        run = _poll_run(client, graph_id, run_id, timeout=120)
        assert run["status"] == "succeeded", run
        assert run["stats"]["textSource"] == "summary_rendered_text"
        assert run["stats"]["fallbackRawCount"] == 1
        assert run["stats"]["exampleTextLimit"] == 220
        assert run["stats"]["promptHash"] == prompt_sha256(
            effective_label_prompt(
                {
                    "prompt": None,
                    "promptAppend": prompt_append,
                    "topK": 4,
                    "exampleTextLimit": 220,
                    "textSource": "auto",
                }
            )
        )
        assert prompt_append in label_provider.calls[0][0]
        sent_blocks = [block for _, blocks in label_provider.calls for block in blocks]
        assert any("junkType:" in block for block in sent_blocks)
        assert any("junkType:" not in block for block in sent_blocks)

        report = client.get(f"/api/graphs/{graph_id}/label-runs/{run_id}/report")
        assert report.status_code == 200, report.text
        body = report.json()
        first_report_blocks = [
            item["block"] for item in body["clusters"][0]["representatives"]
        ]
        assert first_report_blocks == label_provider.calls[0][1]
        assert body["clusters"][0]["representatives"][0]["id"] == representative_id
        assert body["clusters"][0]["representatives"][0]["recordId"]
        assert body["clusters"][0]["representatives"][0]["textSource"] == "raw_customer_text"
        assert body["clusters"][0]["representatives"][0]["truncationApplied"] is True
        assert body["labelQuality"]["exactDuplicateGroups"][0]["label"] == "Duplicate Label"
        assert sorted(body["labelQuality"]["exactDuplicateGroups"][0]["clusterIds"]) == cluster_ids
        assert body["reportNote"].startswith("Representative blocks are recomputed")

        raw_graph = _create_graph(client)
        raw_graph_id, raw_view_id, _raw_cluster_run_id = _cluster_fixture(client, graph=raw_graph)
        missing_lineage = client.post(
            f"/api/graphs/{raw_graph_id}/views/{raw_view_id}/label",
            json={"labeling": {"textSource": "summary"}},
        )
        assert missing_lineage.status_code == 422
        assert "/summarize" in missing_lineage.text


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


def _phase4_client(
    tmp_path: Path,
    label_provider: ScriptedLabelProvider | None,
) -> TestClient:
    embedding_provider = _structured_provider()
    label_factory = (lambda _config: label_provider) if label_provider is not None else None
    return TestClient(
        create_app(
            test_settings(
                tmp_path / "data",
                openai_api_key=os.environ.get("OPENAI_API_KEY"),
            ),
            embedding_provider_factory=lambda _config: embedding_provider,
            label_provider_factory=label_factory,
        )
    )


def _phase20_client(
    tmp_path: Path,
    summary_provider: ScriptedSummaryProvider,
    embedding_provider: StructuredSummaryEmbeddingProvider,
    label_provider: ScriptedLabelProvider,
) -> TestClient:
    return TestClient(
        create_app(
            test_settings(tmp_path / "data"),
            embedding_provider_factory=lambda _config: embedding_provider,
            summary_provider_factory=lambda _config: summary_provider,
            label_provider_factory=lambda _config: label_provider,
        )
    )


def _structured_provider(record_count: int = 300) -> StructuredTopicProvider:
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
    record_count: int = 300,
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


def _create_summary_graph(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/graphs",
        json={
            "name": f"Phase 20 {time.monotonic_ns()}",
            "config": {
                "embedding": {
                    "provider": "mock",
                    "model": "structured-summary-mock",
                    "dimensions": 32,
                    "textFields": [
                        "sourceRecordId",
                        "title",
                        "customerText",
                        "product",
                        "tags",
                    ],
                    "requestsPerMinute": 1000,
                    "maxConcurrency": 8,
                },
                "cluster": {
                    "space": {"method": "none"},
                    "hdbscan": {"minClusterSize": 8, "minSamples": 3},
                    "seed": 42,
                },
                "summarization": {"requestsPerMinute": 100000, "maxConcurrency": 8},
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


def _first_representative_id(
    client: TestClient,
    cluster_run_id: str,
    cluster_id: int,
) -> str:
    with connect(client.app.state.settings.db_path) as conn:
        row = conn.execute(
            """
            SELECT representative_record_ids_json
              FROM cluster_summaries
             WHERE run_id = ? AND cluster_id = ?
            """,
            (cluster_run_id, cluster_id),
        ).fetchone()
    assert row is not None
    return json.loads(row["representative_record_ids_json"])[0]


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
        time.sleep(0.01)
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
        time.sleep(0.01)
    raise AssertionError(f"label progress not observed; last={last}")
