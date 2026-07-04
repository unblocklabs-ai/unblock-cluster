from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from datagraph.core.summarization import (
    OpenAIChatSummaryProvider,
    SummaryResult,
    SummaryValidationError,
    effective_summarization_prompt,
    summarize_with_retry,
)
from datagraph.core.vectors import normalize_l2
from datagraph.db import connect
from datagraph.main import create_app
from datagraph.settings import Settings
from scripts.gen_synthetic import generate_records


class ScriptedSummaryProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.prompt_hashes: list[str] = []

    async def summarize_record(self, prompt: str, record_text: str) -> SummaryResult:
        self.calls += 1
        self.prompt_hashes.append(hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        return scripted_summary(record_text)


class StructuredSummaryEmbeddingProvider:
    def __init__(self, dimensions: int = 32) -> None:
        self.dimensions = dimensions
        self.request_count = 0
        self.centroids: dict[str, np.ndarray] = {}

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        self.request_count += 1
        return [self.embed_text(text) for text in texts]

    def embed_text(self, text: str) -> np.ndarray:
        topic = self._topic(text)
        if topic not in self.centroids:
            self.centroids[topic] = self._centroid(topic)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
        noise = rng.normal(0, 0.01, self.dimensions).astype(np.float32)
        return normalize_l2(self.centroids[topic] + noise)

    def _topic(self, text: str) -> str:
        for line in text.splitlines():
            if line.startswith("issue: "):
                return line.removeprefix("issue: ")
        return scripted_summary(text).issue

    def _centroid(self, topic: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(topic.encode("utf-8")).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        return normalize_l2(rng.normal(0, 1, self.dimensions).astype(np.float32))


class FlakySummaryProvider:
    def __init__(self, fail_records: set[str], *, fail_all: bool = False) -> None:
        self.fail_records = fail_records
        self.fail_all = fail_all
        self.calls: Counter[str] = Counter()

    async def summarize_record(self, prompt: str, record_text: str) -> SummaryResult:
        key = _source_record_id(record_text)
        self.calls[key] += 1
        if self.fail_all or key in self.fail_records:
            raise SummaryValidationError("scripted invalid schema")
        return scripted_summary(record_text)


def test_summarize_then_embed_flow_reuse_facets_ab_and_receipts(tmp_path: Path) -> None:
    records = generate_records(5000, 42)
    records[0] = {
        **records[0],
        "customerText": "Out of office until Monday. Please contact support@example.com.",
        "sourceRecordId": "phase14-ooo",
    }
    records[1] = {
        **records[1],
        "customerText": "We sell miracle lead lists and want a partnership with your CEO.",
        "sourceRecordId": "phase14-vendor",
    }
    records[2] = {
        **records[2],
        "customerText": "This newsletter contains winter recipes and sale links only.",
        "sourceRecordId": "phase14-newsletter",
    }
    summary_provider = ScriptedSummaryProvider()
    embedding_provider = StructuredSummaryEmbeddingProvider()

    with _phase14_client(tmp_path, summary_provider, embedding_provider) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)

        no_summary = client.post(
            f"/api/graphs/{graph_id}/embeddings",
            json={"representation": "summary"},
        )
        assert no_summary.status_code == 409
        assert "/summarize" in no_summary.text

        summarize_run_id = client.post(f"/api/graphs/{graph_id}/summarize", json={}).json()["id"]
        summarize_run = _poll_run(client, graph_id, summarize_run_id, timeout=120)
        assert summarize_run["status"] == "succeeded", summarize_run
        assert summarize_run["stats"]["records"] == len(records)
        assert summarize_run["stats"]["summarized"] == len(records)
        assert summarize_run["stats"]["providerRetries"] == 0
        assert summary_provider.calls == len(records)

        report = client.get(
            f"/api/graphs/{graph_id}/summarize-runs/{summarize_run_id}/report"
        )
        assert report.status_code == 200, report.text
        report_body = report.json()
        assert report_body["junkCounts"]["ooo"] == 1
        assert report_body["junkCounts"]["vendor_pitch"] == 1
        assert report_body["junkCounts"]["newsletter"] == 1
        assert any(row["summary"]["issue"] for row in report_body["records"])

        reused_run_id = client.post(f"/api/graphs/{graph_id}/summarize", json={}).json()["id"]
        reused_run = _poll_run(client, graph_id, reused_run_id, timeout=120)
        assert reused_run["status"] == "succeeded", reused_run
        assert reused_run["stats"]["reused"] == len(records)
        assert reused_run["stats"]["providerRequests"] == 0
        assert summary_provider.calls == len(records)

        mutated = {
            **records[3],
            "customerText": records[3]["customerText"] + " I now also need refund help.",
        }
        _post_records(client, graph_id, [mutated])
        incremental_run_id = client.post(f"/api/graphs/{graph_id}/summarize", json={}).json()["id"]
        incremental_run = _poll_run(client, graph_id, incremental_run_id, timeout=120)
        assert incremental_run["status"] == "succeeded", incremental_run
        assert incremental_run["stats"]["summarized"] == 1
        assert incremental_run["stats"]["reused"] == len(records) - 1

        context_run_id = client.post(
            f"/api/graphs/{graph_id}/summarize",
            json={"summarization": {"context": "Sells supplements and meal delivery."}},
        ).json()["id"]
        context_run = _poll_run(client, graph_id, context_run_id, timeout=120)
        assert context_run["status"] == "succeeded", context_run
        assert context_run["stats"]["summarized"] == len(records)
        assert context_run["stats"]["promptHash"] != incremental_run["stats"]["promptHash"]

        prompt_run_id = client.post(
            f"/api/graphs/{graph_id}/summarize",
            json={"summarization": {"prompt": "Extract strict support summaries."}},
        ).json()["id"]
        prompt_run = _poll_run(client, graph_id, prompt_run_id, timeout=120)
        assert prompt_run["status"] == "succeeded", prompt_run
        assert prompt_run["stats"]["promptHash"] not in {
            incremental_run["stats"]["promptHash"],
            context_run["stats"]["promptHash"],
        }

        summary_embed_id = client.post(
            f"/api/graphs/{graph_id}/embeddings",
            json={"representation": "summary"},
        ).json()["id"]
        summary_embed = _poll_run(client, graph_id, summary_embed_id, timeout=120)
        assert summary_embed["status"] == "succeeded", summary_embed
        assert summary_embed["inputRefs"]["summarizeRunId"] == prompt_run_id
        assert summary_embed["stats"]["representation"] == "summary"
        assert summary_embed["stats"]["skippedJunk"] == 3
        assert summary_embed["stats"]["missingSummaries"] == 0
        assert summary_embed["stats"]["records"] == len(records) - 3

        include_junk_embed_id = client.post(
            f"/api/graphs/{graph_id}/embeddings",
            json={"representation": "summary", "includeJunk": True},
        ).json()["id"]
        include_junk_embed = _poll_run(client, graph_id, include_junk_embed_id, timeout=120)
        assert include_junk_embed["status"] == "succeeded", include_junk_embed
        assert include_junk_embed["stats"]["records"] == len(records)
        assert include_junk_embed["stats"]["skippedJunk"] == 0

        cluster_run_id = _enqueue_cluster(
            client,
            graph_id,
            view_id,
            {"embeddingRunId": summary_embed_id},
        )
        cluster_run = _poll_run(client, graph_id, cluster_run_id, timeout=180)
        assert cluster_run["status"] == "succeeded", cluster_run
        assert cluster_run["stats"]["population"] == len(records) - 3
        assert cluster_run["inputRefs"]["embeddingRunId"] == summary_embed_id

        layout_run_id = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/layout",
            json={"embeddingRunId": summary_embed_id},
        ).json()["id"]
        layout_run = _poll_run(client, graph_id, layout_run_id, timeout=180)
        assert layout_run["status"] == "succeeded", layout_run
        artifact = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact")
        assert artifact.status_code == 200, artifact.text
        artifact_body = artifact.json()
        assert artifact_body["representation"] == "summary"
        assert artifact_body["runRefs"]["embeddingRunId"] == summary_embed_id
        assert artifact_body["runRefs"]["clusterRunId"] == cluster_run_id
        assert artifact_body["runRefs"]["layoutRunId"] == layout_run_id
        assert artifact_body["runRefs"]["summarizeRunId"] == prompt_run_id

        raw_embed_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        raw_embed = _poll_run(client, graph_id, raw_embed_id, timeout=120)
        assert raw_embed["status"] == "succeeded", raw_embed
        raw_cluster_id = _enqueue_cluster(
            client,
            graph_id,
            view_id,
            {"embeddingRunId": raw_embed_id, "setDefault": False},
        )
        raw_cluster = _poll_run(client, graph_id, raw_cluster_id, timeout=180)
        assert raw_cluster["status"] == "succeeded", raw_cluster

        topics = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics",
            params={"clusterRunId": cluster_run_id, "facetBy": "summary.product"},
        )
        assert topics.status_code == 200, topics.text
        topics_body = topics.json()
        assert len(topics_body["topics"]) >= 5
        assert topics_body["embeddingRunId"] == summary_embed_id
        _assert_summary_product_facets_match_db(
            client.app.state.settings.db_path,
            cluster_run_id,
            prompt_run_id,
            topics_body["topics"],
        )

        raw_topics = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics",
            params={"clusterRunId": raw_cluster_id},
        )
        assert raw_topics.status_code == 200
        assert raw_topics.json()["embeddingRunId"] == raw_embed_id

        first_topic_id = topics_body["topics"][0]["clusterId"]
        topic_records = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics/{first_topic_id}/records",
            params={"clusterRunId": cluster_run_id},
        ).json()["records"]
        assert topic_records
        assert all("issue:" not in row["customerText"] for row in topic_records)
        assert any("I need help" in row["customerText"] for row in topic_records)

        evidence = client.post(
            f"/api/graphs/{graph_id}/evidence",
            json={
                "viewId": view_id,
                "recipe": "topic_evidence",
                "topicId": first_topic_id,
                "facetBy": "summary.product",
            },
        )
        assert evidence.status_code == 200, evidence.text
        representatives = evidence.json()["evidence"]["representatives"]
        assert representatives
        assert all("issue:" not in row["customerText"] for row in representatives)


def test_summarize_failures_config_validation_and_summary_facet_409s(tmp_path: Path) -> None:
    records = generate_records(5000, 7)[:8]
    provider = FlakySummaryProvider({records[0]["sourceRecordId"]})
    with _phase14_client(tmp_path, provider, StructuredSummaryEmbeddingProvider()) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)

        too_long = client.post(
            f"/api/graphs/{graph_id}/summarize",
            json={"summarization": {"context": "x" * 4001}},
        )
        assert too_long.status_code == 422
        unknown = client.post(
            f"/api/graphs/{graph_id}/summarize",
            json={"summarization": {"extra": True}},
        )
        assert unknown.status_code == 422

        partial_run_id = client.post(f"/api/graphs/{graph_id}/summarize", json={}).json()["id"]
        partial_run = _poll_run(client, graph_id, partial_run_id)
        assert partial_run["status"] == "succeeded", partial_run
        assert partial_run["stats"]["failed"] == 1
        assert partial_run["stats"]["providerRetries"] == 1
        assert partial_run["stats"]["failedRecordIds"]
        assert provider.calls[records[0]["sourceRecordId"]] == 2

        summary_embed_id = client.post(
            f"/api/graphs/{graph_id}/embeddings",
            json={"representation": "summary"},
        ).json()["id"]
        summary_embed = _poll_run(client, graph_id, summary_embed_id)
        assert summary_embed["status"] == "succeeded", summary_embed
        assert summary_embed["stats"]["missingSummaries"] == 1

        all_fail_provider = FlakySummaryProvider(set(), fail_all=True)
    with _phase14_client(
        tmp_path / "all-fail",
        all_fail_provider,
        StructuredSummaryEmbeddingProvider(),
    ) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        _post_records(client, graph_id, records[:2])
        failed_run_id = client.post(f"/api/graphs/{graph_id}/summarize", json={}).json()["id"]
        failed_run = _poll_run(client, graph_id, failed_run_id)
        assert failed_run["status"] == "failed"
        assert "all records failed" in failed_run["errorText"]

    with _phase14_client(
        tmp_path / "raw-lineage",
        ScriptedSummaryProvider(),
        StructuredSummaryEmbeddingProvider(),
    ) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)
        raw_embed_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        assert _poll_run(client, graph_id, raw_embed_id)["status"] == "succeeded"
        raw_cluster_id = _enqueue_cluster(
            client,
            graph_id,
            view_id,
            {"embeddingRunId": raw_embed_id},
        )
        assert _poll_run(client, graph_id, raw_cluster_id)["status"] == "succeeded"
        summary_facet = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics",
            params={"facetBy": "summary.product"},
        )
        assert summary_facet.status_code == 422
        assert "summary representation lineage" in summary_facet.text


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
def test_real_openai_summarizer_schema_and_verbatim_phrases() -> None:
    records = generate_records(5000, 42)[:10]
    provider = OpenAIChatSummaryProvider(
        api_key=os.environ.get("OPENAI_API_KEY"),
        model="gpt-5.4-nano",
    )
    prompt = effective_summarization_prompt(
        {"prompt": None, "context": "A consumer supplement and meal delivery brand."}
    )
    for record in records:
        text = record["customerText"]
        result, _attempts = __import__("asyncio").run(
            summarize_with_retry(provider, prompt, text)
        )
        assert result.issue
        assert result.key_customer_phrases
        assert any(phrase in text for phrase in result.key_customer_phrases)


def scripted_summary(record_text: str) -> SummaryResult:
    lowered = record_text.lower()
    if "out of office" in lowered:
        return _summary("out of office auto reply", None, "ooo", "Out of office")
    if "lead lists" in lowered or "partnership with your ceo" in lowered:
        return _summary("vendor sales pitch", None, "vendor_pitch", "sell miracle lead lists")
    if "newsletter" in lowered:
        return _summary("newsletter or promotional send", None, "newsletter", "newsletter")
    if "system generated" in lowered or "payout notice" in lowered:
        return _summary(
            "platform notification",
            None,
            "platform_notification",
            "system generated",
        )
    if "packaging" in lowered or "zipper" in lowered or "pouch" in lowered:
        return _summary("packaging failure", "Greens Powder", "none", _phrase(record_text))
    if "shipping" in lowered or "customs" in lowered or "delivery" in lowered:
        return _summary(
            "delivery and shipping issue",
            "Electrolyte Mix",
            "none",
            _phrase(record_text),
        )
    if "creatine" in lowered or "loading guidance" in lowered:
        return _summary(
            "creatine guidance question",
            "Strength Stack",
            "none",
            _phrase(record_text),
        )
    if "energy" in lowered or "crash" in lowered:
        return _summary(
            "energy crash complaint",
            "Holiday Energy Bundle",
            "none",
            _phrase(record_text),
        )
    if "refund" in lowered or "cancel" in lowered:
        return _summary("refund or cancellation request", None, "none", _phrase(record_text))
    if "sleep" in lowered:
        return _summary("sleep product concern", "Sleep Drops", "none", _phrase(record_text))
    return _summary("general support request", None, "none", _phrase(record_text))


def _summary(issue: str, product: str | None, junk_type: str, phrase: str) -> SummaryResult:
    return SummaryResult(
        issue=issue,
        product=product,
        desired_resolution="support follow-up" if junk_type == "none" else None,
        sentiment="negative" if junk_type == "none" else None,
        key_customer_phrases=[phrase],
        junk_type=junk_type,
    )


def _phrase(text: str) -> str:
    value = text.split("customerText:", 1)[-1].strip()
    return value.split(".", 1)[0].strip()[:120] or value[:120] or "customer"


def _source_record_id(record_text: str) -> str:
    for line in record_text.splitlines():
        if line.startswith("sourceRecordId: "):
            return line.removeprefix("sourceRecordId: ").strip()
    return "unknown"


def _phase14_client(
    tmp_path: Path,
    summary_provider: Any,
    embedding_provider: Any,
) -> TestClient:
    return TestClient(
        create_app(
            Settings(data_dir=tmp_path / "data", port=0),
            embedding_provider_factory=lambda _config: embedding_provider,
            summary_provider_factory=lambda _config: summary_provider,
        )
    )


def _create_graph(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/graphs",
        json={
            "name": f"Phase 14 {time.monotonic_ns()}",
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
                    "hdbscan": {"minClusterSize": 15, "minSamples": 5},
                    "seed": 42,
                },
                "summarization": {"requestsPerMinute": 100000, "maxConcurrency": 8},
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _all_records_view_id(graph: dict[str, Any]) -> str:
    return next(view["id"] for view in graph["views"] if view["name"] == "all_records")


def _post_records(client: TestClient, graph_id: str, records: list[dict[str, Any]]) -> None:
    for start in range(0, len(records), 1000):
        response = client.post(
            f"/api/graphs/{graph_id}/records",
            json={"records": records[start : start + 1000]},
        )
        assert response.status_code == 200, response.text


def _enqueue_cluster(
    client: TestClient,
    graph_id: str,
    view_id: str,
    body: dict[str, Any],
) -> str:
    response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/cluster", json=body)
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


def _assert_summary_product_facets_match_db(
    db_path: Path,
    cluster_run_id: str,
    summarize_run_id: str,
    topics: list[dict[str, Any]],
) -> None:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT cm.cluster_id, rs.summary_json
              FROM cluster_memberships cm
              JOIN summary_items si ON si.run_id = ? AND si.record_id = cm.record_id
              JOIN runs sr ON sr.id = si.run_id
              JOIN record_summaries rs
                ON rs.model = json_extract(sr.params_json, '$.summarization.model')
               AND rs.prompt_hash = json_extract(sr.stats_json, '$.promptHash')
               AND rs.text_hash = si.text_hash
             WHERE cm.run_id = ? AND cm.cluster_id != -1
            """,
            (summarize_run_id, cluster_run_id),
        ).fetchall()
    expected: dict[int, Counter[str]] = {}
    for row in rows:
        product = json.loads(row["summary_json"]).get("product") or "(none)"
        expected.setdefault(int(row["cluster_id"]), Counter())[product] += 1
    by_cluster = {topic["clusterId"]: topic for topic in topics}
    for cluster_id, counts in expected.items():
        assert by_cluster[cluster_id]["facets"] == dict(counts)
