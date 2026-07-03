from __future__ import annotations

import asyncio
import math
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from datagraph.core.embedding_text import render_embedding_text
from datagraph.core.openai_client import (
    MockEmbeddingProvider,
    ProviderRetryError,
    TokenBucketRateLimiter,
    pack_embedding_batches,
)
from datagraph.core.vectors import unpack_vector
from datagraph.db import connect
from datagraph.main import create_app
from datagraph.settings import Settings
from scripts.gen_synthetic import generate_records


class CountingMockProvider(MockEmbeddingProvider):
    def __init__(self, dimensions: int = 32, *, delay_seconds: float = 0.0) -> None:
        super().__init__(dimensions=dimensions)
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            else:
                await asyncio.sleep(0)
            return await super().embed_batch(texts)
        finally:
            self.in_flight -= 1


class FlakyProvider(MockEmbeddingProvider):
    def __init__(self, *, failures_before_success: int | None, dimensions: int = 16) -> None:
        super().__init__(dimensions=dimensions)
        self.failures_before_success = failures_before_success
        self.attempts = 0

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        self.attempts += 1
        if self.failures_before_success is None or self.attempts <= self.failures_before_success:
            raise ProviderRetryError("429 rate limited", retry_after=0)
        return await super().embed_batch(texts)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def _client(
    tmp_path: Path,
    *,
    provider: Any | None = None,
    clock: FakeClock | None = None,
) -> TestClient:
    factory = (lambda _config: provider) if provider is not None else None
    return TestClient(
        create_app(
            Settings(data_dir=tmp_path / "data", port=0),
            embedding_provider_factory=factory,
            clock=clock,
            sleep=clock.sleep if clock is not None else None,
        )
    )


def _create_graph(client: TestClient, *, embedding: dict[str, Any] | None = None) -> dict[str, Any]:
    config = {
        "embedding": {
            "provider": "mock",
            "model": "mock-model",
            "dimensions": 32,
            "textFields": ["title", "customerText", "product", "tags"],
            "requestsPerMinute": 1000,
            "maxConcurrency": 2,
            **(embedding or {}),
        }
    }
    response = client.post("/api/graphs", json={"name": "Embedding Graph", "config": config})
    assert response.status_code == 201, response.text
    return response.json()


def _post_records(client: TestClient, graph_id: str, records: list[dict[str, Any]]) -> None:
    for start in range(0, len(records), 1000):
        response = client.post(
            f"/api/graphs/{graph_id}/records",
            json={"records": records[start : start + 1000]},
        )
        assert response.status_code == 200, response.text


def _minimal_record(record_id: str, **overrides: Any) -> dict[str, Any]:
    record = {
        "recordId": record_id,
        "sourceType": "support_ticket",
        "sourceName": "zendesk",
        "sourceRecordId": f"ticket-{record_id}",
        "title": "Refund request",
        "customerText": "Please help with the refund.",
        "product": "Sleep Drops",
        "tags": ["refund"],
        "timestamp": "2025-01-02T03:04:05Z",
        "metadata": {"groundTruthTopicId": "refund_friction"},
    }
    record.update(overrides)
    return record


def _enqueue_embedding(
    client: TestClient,
    graph_id: str,
    body: dict[str, Any] | None = None,
) -> str:
    response = client.post(f"/api/graphs/{graph_id}/embeddings", json=body or {})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _poll_run(
    client: TestClient,
    graph_id: str,
    run_id: str,
    terminal: bool = True,
) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/graphs/{graph_id}/runs/{run_id}")
        assert response.status_code == 200
        last = response.json()
        if terminal and last["status"] in {"succeeded", "failed", "cancelled"}:
            return last
        if not terminal and last["status"] == "running":
            return last
        time.sleep(0.01)
    raise AssertionError(f"run did not reach expected state; last={last}")


def _db_counts(client: TestClient, run_id: str | None = None) -> dict[str, int]:
    with connect(client.app.state.settings.db_path) as conn:
        counts = {
            "vectors": conn.execute("SELECT COUNT(*) FROM embedding_vectors").fetchone()[0],
            "items": conn.execute("SELECT COUNT(*) FROM embedding_items").fetchone()[0],
        }
        if run_id is not None:
            counts["run_items"] = conn.execute(
                "SELECT COUNT(*) FROM embedding_items WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
    return counts


def test_embedding_text_rendering_template_tags_skipping_and_truncation() -> None:
    record = {
        "title": "Great gummies",
        "customerText": "They arrived melted",
        "product": None,
        "tags": ["shipping", "melting"],
    }
    config = {"textFields": ["title", "customerText", "product", "tags"], "maxInputTokens": 8000}
    rendered = render_embedding_text(record, config)
    assert rendered.text == (
        "title: Great gummies\ncustomerText: They arrived melted\ntags: shipping, melting"
    )

    templated = render_embedding_text(
        record,
        {
            "textTemplate": "{title} :: {missing} :: {tags}",
            "textFields": ["customerText"],
            "maxInputTokens": 8000,
        },
    )
    assert templated.text == "Great gummies ::  :: shipping, melting"

    truncated = render_embedding_text(
        {"customerText": "hello " * 200},
        {"textFields": ["customerText"], "maxInputTokens": 10},
    )
    assert truncated.token_count <= 10
    assert truncated.text_hash == render_embedding_text(
        {"customerText": truncated.text.removeprefix("customerText: ")},
        {"textFields": ["customerText"], "maxInputTokens": 10},
    ).text_hash


def test_batch_packer_respects_input_and_token_caps() -> None:
    by_input = pack_embedding_batches(["hello"] * 513)
    assert [len(batch.texts) for batch in by_input] == [512, 1]

    by_tokens = pack_embedding_batches(["hello world"] * 4, max_inputs=512, max_tokens=5)
    assert len(by_tokens) == 2
    assert all(batch.token_count <= 5 for batch in by_tokens)


def test_token_bucket_rate_limiter_uses_fake_clock() -> None:
    clock = FakeClock()

    async def run() -> None:
        limiter = TokenBucketRateLimiter(requests_per_minute=2, clock=clock, sleep=clock.sleep)
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()

    asyncio.run(run())
    assert clock.sleeps == [30.0]
    assert clock.now == 30.0


def test_full_synthetic_embedding_run_reuse_and_one_record_mutation(tmp_path: Path) -> None:
    records = generate_records(5000, 42)
    provider = CountingMockProvider(dimensions=32, delay_seconds=0.005)
    with _client(tmp_path, provider=provider) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        _post_records(client, graph_id, records)
        embedding_config = graph["config"]["embedding"]
        unique_hashes = {
            render_embedding_text(record, embedding_config).text_hash for record in records
        }

        run_id = _enqueue_embedding(client, graph_id)
        _poll_embedding_progress(client, graph_id, run_id, total=5000)
        final = _poll_run(client, graph_id, run_id)
        assert final["status"] == "succeeded"
        assert final["progress"]["embedded"] == 5000
        assert final["progress"]["reused"] == 0
        assert final["stats"]["records"] == 5000
        assert final["stats"]["uniqueTexts"] == len(unique_hashes)
        assert final["stats"]["providerRetries"] == 0
        assert provider.max_in_flight <= 2

        counts = _db_counts(client, run_id)
        assert counts["run_items"] == 5000
        assert counts["vectors"] == len(unique_hashes)
        with connect(client.app.state.settings.db_path) as conn:
            rows = conn.execute("SELECT vector, dimensions FROM embedding_vectors").fetchall()
        assert {row["dimensions"] for row in rows} == {32}
        for row in rows:
            vector = unpack_vector(row["vector"])
            assert vector.dtype == np.float32
            assert vector.shape == (32,)
            assert math.isclose(float(np.linalg.norm(vector)), 1.0, rel_tol=1e-6)

        calls_after_first = provider.calls
        rerun_id = _enqueue_embedding(client, graph_id)
        rerun = _poll_run(client, graph_id, rerun_id)
        assert rerun["status"] == "succeeded"
        assert rerun["progress"]["embedded"] == 0
        assert rerun["progress"]["reused"] == 5000
        assert rerun["stats"]["providerRequests"] == 0
        assert rerun["stats"]["providerRetries"] == 0
        assert provider.calls == calls_after_first

        mutated = dict(records[0])
        mutated["customerText"] = f"{mutated['customerText']} Mutated once."
        _post_records(client, graph_id, [mutated])
        mutation_id = _enqueue_embedding(client, graph_id)
        mutation = _poll_run(client, graph_id, mutation_id)
        assert mutation["status"] == "succeeded"
        assert mutation["progress"]["embedded"] == 1
        assert mutation["progress"]["reused"] == 4999
        assert _db_counts(client)["vectors"] == len(unique_hashes) + 1


def test_empty_graph_embedding_succeeds_and_override_sets_vector_key(tmp_path: Path) -> None:
    provider = CountingMockProvider(dimensions=12)
    with _client(tmp_path, provider=provider) as client:
        graph = _create_graph(client, embedding={"dimensions": 24})
        run_id = _enqueue_embedding(client, graph["id"], {"dimensions": 12})
        run = _poll_run(client, graph["id"], run_id)
        assert run["status"] == "succeeded"
        assert run["progress"]["total"] == 0
        assert run["stats"]["dimensions"] == 12
        assert provider.calls == 0

        graph_with_record = _create_graph(client, embedding={"dimensions": 24})
        _post_records(client, graph_with_record["id"], [_minimal_record("override-dim")])
        override_id = _enqueue_embedding(client, graph_with_record["id"], {"dimensions": 12})
        override_run = _poll_run(client, graph_with_record["id"], override_id)
        assert override_run["status"] == "succeeded"
        assert override_run["stats"]["dimensions"] == 12
        with connect(client.app.state.settings.db_path) as conn:
            dimensions = {
                row["dimensions"]
                for row in conn.execute(
                    "SELECT dimensions FROM embedding_vectors WHERE model = 'mock-model'"
                )
            }
        assert dimensions == {12}


def test_retry_success_and_permanent_failure(tmp_path: Path) -> None:
    success_provider = FlakyProvider(failures_before_success=2)
    clock = FakeClock()
    with _client(tmp_path, provider=success_provider, clock=clock) as client:
        graph = _create_graph(client, embedding={"dimensions": 16})
        _post_records(client, graph["id"], [_minimal_record("retry-ok")])
        run_id = _enqueue_embedding(client, graph["id"])
        run = _poll_run(client, graph["id"], run_id)
        assert run["status"] == "succeeded"
        assert success_provider.attempts == 3
        assert run["stats"]["providerRetries"] == 2
        assert clock.sleeps == [0, 0]

    failing_provider = FlakyProvider(failures_before_success=None)
    failing_clock = FakeClock()
    with _client(tmp_path / "failing", provider=failing_provider, clock=failing_clock) as client:
        graph = _create_graph(client, embedding={"dimensions": 16})
        _post_records(client, graph["id"], [_minimal_record("retry-fail")])
        run_id = _enqueue_embedding(client, graph["id"])
        run = _poll_run(client, graph["id"], run_id)
        assert run["status"] == "failed"
        assert "embedding provider failed after" in run["errorText"]
        assert failing_provider.attempts == 4


def test_cancel_between_embedding_batches_keeps_partial_vectors(tmp_path: Path) -> None:
    records = [
        _minimal_record(f"cancel-{index}", customerText=f"text {index}") for index in range(1200)
    ]
    provider = CountingMockProvider(dimensions=16, delay_seconds=0.05)
    with _client(tmp_path, provider=provider) as client:
        graph = _create_graph(
            client,
            embedding={"dimensions": 16, "maxConcurrency": 1, "requestsPerMinute": 1000},
        )
        _post_records(client, graph["id"], records)
        run_id = _enqueue_embedding(client, graph["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = client.get(f"/api/graphs/{graph['id']}/runs/{run_id}").json()
            if run["status"] == "running" and run["progress"].get("embedded", 0) > 0:
                break
            time.sleep(0.01)
        response = client.post(f"/api/graphs/{graph['id']}/runs/{run_id}/cancel")
        assert response.status_code == 200
        cancelled = _poll_run(client, graph["id"], run_id)
        assert cancelled["status"] == "cancelled"
        vector_count = _db_counts(client)["vectors"]
        assert 0 < vector_count < 1200


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set")
def test_real_openai_embedding_integration_opt_in(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        graph = _create_graph(
            client,
            embedding={
                "provider": "openai",
                "model": "text-embedding-3-small",
                "dimensions": 1536,
            },
        )
        _post_records(
            client,
            graph["id"],
            [
                _minimal_record(
                    f"real-{index}",
                    customerText=f"Please help with issue number {index} about my order.",
                )
                for index in range(10)
            ],
        )
        run_id = _enqueue_embedding(client, graph["id"])
        run = _poll_run(client, graph["id"], run_id)
        assert run["status"] == "succeeded"
        with connect(client.app.state.settings.db_path) as conn:
            rows = conn.execute("SELECT vector FROM embedding_vectors").fetchall()
        assert len(rows) == 10
        for row in rows:
            vector = unpack_vector(row["vector"])
            assert vector.shape == (1536,)
            assert math.isclose(float(np.linalg.norm(vector)), 1.0, rel_tol=1e-5)


def _poll_embedding_progress(
    client: TestClient,
    graph_id: str,
    run_id: str,
    *,
    total: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/graphs/{graph_id}/runs/{run_id}")
        assert response.status_code == 200
        last = response.json()
        progress = last["progress"]
        if last["status"] == "running" and progress.get("total") == total:
            return last
        time.sleep(0.005)
    raise AssertionError(f"run did not expose embedding progress; last={last}")
