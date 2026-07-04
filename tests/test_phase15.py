from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

from datagraph.core.ids import new_id, now_iso
from datagraph.core.labeling import LabelResult
from datagraph.core.openai_client import EmbeddingBatchResult, TokenUsage
from datagraph.core.summarization import SummaryResult
from datagraph.core.vectors import normalize_l2
from datagraph.db import connect
from datagraph.main import create_app
from datagraph.settings import Settings


class UsageEmbeddingProvider:
    def __init__(self, *, dimensions: int = 8, prompt_tokens: int = 123) -> None:
        self.dimensions = dimensions
        self.prompt_tokens = prompt_tokens

    async def embed_batch(self, texts: list[str]) -> EmbeddingBatchResult:
        return EmbeddingBatchResult(
            vectors=[self._vector(text) for text in texts],
            prompt_tokens=self.prompt_tokens,
        )

    def _vector(self, text: str) -> np.ndarray:
        seed = int.from_bytes(text.encode("utf-8")[:8].ljust(8, b"0"), "big")
        rng = np.random.default_rng(seed)
        return normalize_l2(rng.normal(0, 1, self.dimensions).astype(np.float32))


class UsageSummaryProvider:
    async def summarize_record(self, prompt: str, record_text: str) -> SummaryResult:
        return SummaryResult(
            issue="support issue",
            product="Canonical Product",
            desired_resolution="reply",
            sentiment="negative",
            key_customer_phrases=[record_text.split("customerText: ", 1)[-1].split(".", 1)[0]],
            junk_type="none",
            token_usage=TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        )


class ZeroSummaryProvider:
    async def summarize_record(self, prompt: str, record_text: str) -> SummaryResult:
        return SummaryResult(
            issue="support issue",
            product=None,
            desired_resolution=None,
            sentiment=None,
            key_customer_phrases=["support"],
            junk_type="none",
        )


class UsageLabelProvider:
    async def label_cluster(self, prompt: str, representatives: list[str]) -> LabelResult:
        return LabelResult(
            label="Usage label",
            summary="Usage summary",
            key_signals=["usage"],
            tags=["usage"],
            coherent=True,
            token_usage=TokenUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )


def test_legacy_config_without_summarization_hydrates_for_summarize(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", port=0)
    graph_id = "grf_legacy_summarize"
    legacy_config = {
        "embedding": {
            "provider": "mock",
            "model": "legacy-mock",
            "dimensions": 8,
            "textFields": ["customerText"],
        }
    }
    with TestClient(
        create_app(settings, summary_provider_factory=lambda _config: ZeroSummaryProvider())
    ) as client:
        _insert_graph(client, graph_id, legacy_config)
        stored_before = _stored_config(settings.db_path, graph_id)
        response = client.post(f"/api/graphs/{graph_id}/summarize", json={})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["params"]["summarization"] == {
            "provider": "openai",
            "model": "gpt-5.4-nano",
            "context": None,
            "requestsPerMinute": 500,
            "maxConcurrency": 4,
            "prompt": None,
        }
        assert _stored_config(settings.db_path, graph_id) == stored_before


def test_legacy_config_without_cluster_min_dist_hydrates_for_cluster(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", port=0)
    graph_id = "grf_legacy_cluster"
    legacy_config = {
        "embedding": {
            "provider": "mock",
            "model": "legacy-cluster-mock",
            "dimensions": 8,
            "textFields": ["customerText"],
            "requestsPerMinute": 100000,
            "maxConcurrency": 2,
        },
        "cluster": {
            "space": {"method": "none"},
            "hdbscan": {"minClusterSize": 2, "minSamples": 1},
            "seed": 42,
        },
    }
    with TestClient(
        create_app(
            settings,
            embedding_provider_factory=lambda _config: UsageEmbeddingProvider(dimensions=8),
        )
    ) as client:
        view_id = _insert_graph(client, graph_id, legacy_config)
        _post_records(client, graph_id, [_record("one"), _record("two"), _record("three")])
        embed_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        embed_run = _poll_run(client, graph_id, embed_id)
        assert embed_run["status"] == "succeeded", embed_run

        response = client.post(f"/api/graphs/{graph_id}/views/{view_id}/cluster", json={})
        assert response.status_code == 201, response.text
        cluster_run = _poll_run(client, graph_id, response.json()["id"])
        assert cluster_run["status"] == "succeeded", cluster_run
        assert cluster_run["params"]["cluster"]["space"]["minDist"] == 0.1


def test_unknown_legacy_config_keys_are_dropped_on_reads_but_patch_is_strict(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", port=0)
    graph_id = "grf_legacy_unknown"
    legacy_config = {
        "legacyTop": True,
        "embedding": {
            "provider": "mock",
            "model": "legacy-unknown-mock",
            "dimensions": 8,
            "textFields": ["customerText"],
            "legacyEmbedding": True,
        },
    }
    with TestClient(create_app(settings)) as client:
        _insert_graph(client, graph_id, legacy_config)
        stored_before = _stored_config(settings.db_path, graph_id)
        response = client.get(f"/api/graphs/{graph_id}")
        assert response.status_code == 200, response.text
        hydrated = response.json()["config"]
        assert "legacyTop" not in hydrated
        assert "legacyEmbedding" not in hydrated["embedding"]
        assert "summarization" in hydrated
        assert _stored_config(settings.db_path, graph_id) == stored_before

        strict_patch = client.patch(
            f"/api/graphs/{graph_id}",
            json={"config": {"embedding": {"legacyEmbedding": True}}},
        )
        assert strict_patch.status_code == 422


def test_no_direct_config_json_loads_outside_shared_loader() -> None:
    offenders = []
    root = Path(__file__).resolve().parents[1]
    for directory in (root / "datagraph" / "api", root / "datagraph" / "runs"):
        for path in directory.rglob("*.py"):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "json.loads" in line and "config_json" in line:
                    offenders.append(f"{path.relative_to(root)}:{line_number}:{line.strip()}")
    assert offenders == []


def test_token_usage_in_summarize_embed_label_and_report(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", port=0)
    embedding_provider = UsageEmbeddingProvider(dimensions=8, prompt_tokens=123)
    with TestClient(
        create_app(
            settings,
            embedding_provider_factory=lambda _config: embedding_provider,
            summary_provider_factory=lambda _config: UsageSummaryProvider(),
            label_provider_factory=lambda _config: UsageLabelProvider(),
        )
    ) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, [_record("usage-one"), _record("usage-two")])

        summarize_id = client.post(f"/api/graphs/{graph_id}/summarize", json={}).json()["id"]
        summarize_run = _poll_run(client, graph_id, summarize_id)
        assert summarize_run["stats"]["tokenUsage"] == {
            "promptTokens": 6,
            "completionTokens": 4,
            "totalTokens": 10,
        }
        report = client.get(f"/api/graphs/{graph_id}/summarize-runs/{summarize_id}/report")
        assert report.status_code == 200, report.text
        assert report.json()["tokenUsage"] == summarize_run["stats"]["tokenUsage"]

        embed_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        embed_run = _poll_run(client, graph_id, embed_id)
        assert embed_run["stats"]["tokenUsage"] == {
            "promptTokens": 123,
            "completionTokens": 0,
            "totalTokens": 123,
        }

        cluster_run_id = _insert_cluster_run_for_label(client, graph_id, view_id)
        label_id = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/label",
            json={"clusterRunId": cluster_run_id, "setDefault": False},
        ).json()["id"]
        label_run = _poll_run(client, graph_id, label_id)
        assert label_run["stats"]["tokenUsage"] == {
            "promptTokens": 11,
            "completionTokens": 7,
            "totalTokens": 18,
        }


def test_plain_mock_providers_emit_zero_token_usage(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", port=0)
    with TestClient(
        create_app(
            settings,
            embedding_provider_factory=lambda _config: UsageEmbeddingProvider(
                dimensions=8,
                prompt_tokens=0,
            ),
            summary_provider_factory=lambda _config: ZeroSummaryProvider(),
        )
    ) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        _post_records(client, graph_id, [_record("zero-one")])
        summarize_id = client.post(f"/api/graphs/{graph_id}/summarize", json={}).json()["id"]
        summarize_run = _poll_run(client, graph_id, summarize_id)
        assert summarize_run["stats"]["tokenUsage"] == {
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
        }


def _create_graph(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/graphs",
        json={
            "name": f"Phase 15 {time.monotonic_ns()}",
            "config": {
                "embedding": {
                    "provider": "mock",
                    "model": "phase15-mock",
                    "dimensions": 8,
                    "textFields": ["customerText"],
                    "requestsPerMinute": 100000,
                    "maxConcurrency": 2,
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


def _insert_graph(client: TestClient, graph_id: str, config: dict[str, Any]) -> str:
    view_id = new_id("view")
    now = now_iso()
    with connect(client.app.state.settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO graphs (id, name, config_json, created_at, updated_at)
            VALUES (?, 'Legacy', ?, ?, ?)
            """,
            (graph_id, json.dumps(config, sort_keys=True), now, now),
        )
        conn.execute(
            """
            INSERT INTO views (
              id, graph_id, name, description, scope_json,
              default_embedding_run_id, default_cluster_run_id, default_layout_run_id,
              default_label_run_id, default_trend_run_id, created_at, updated_at
            )
            VALUES (?, ?, 'all_records', NULL, '{}', NULL, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (view_id, graph_id, now, now),
        )
        conn.commit()
    return view_id


def _stored_config(db_path: Path, graph_id: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT config_json FROM graphs WHERE id = ?", (graph_id,)).fetchone()
    return json.loads(row["config_json"])


def _record(suffix: str) -> dict[str, Any]:
    return {
        "recordId": f"phase15-{suffix}",
        "sourceType": "support_ticket",
        "sourceName": "phase15",
        "sourceRecordId": f"src-{suffix}",
        "customerText": f"Customer needs help with {suffix}.",
        "timestamp": "2025-12-01T00:00:00Z",
    }


def _post_records(client: TestClient, graph_id: str, records: list[dict[str, Any]]) -> None:
    response = client.post(f"/api/graphs/{graph_id}/records", json={"records": records})
    assert response.status_code == 200, response.text


def _all_records_view_id(graph: dict[str, Any]) -> str:
    return next(view["id"] for view in graph["views"] if view["name"] == "all_records")


def _poll_run(
    client: TestClient,
    graph_id: str,
    run_id: str,
    timeout: float = 30,
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


def _insert_cluster_run_for_label(client: TestClient, graph_id: str, view_id: str) -> str:
    run_id = new_id("run")
    now = now_iso()
    with connect(client.app.state.settings.db_path) as conn:
        record_rows = conn.execute(
            "SELECT id FROM records WHERE graph_id = ? ORDER BY id ASC",
            (graph_id,),
        ).fetchall()
        record_ids = [row["id"] for row in record_rows]
        conn.execute(
            """
            INSERT INTO runs (
              id, graph_id, view_id, type, status, params_json, progress_json,
              error_text, input_refs_json, stats_json, created_at, started_at, completed_at
            )
            VALUES (?, ?, ?, 'cluster', 'succeeded', '{}', '{}', NULL, '{}', '{}', ?, ?, ?)
            """,
            (run_id, graph_id, view_id, now, now, now),
        )
        conn.executemany(
            """
            INSERT INTO cluster_memberships (
              run_id, record_id, cluster_id, probability, outlier_score, is_noise
            )
            VALUES (?, ?, 0, 1.0, 0.0, 0)
            """,
            [(run_id, record_id) for record_id in record_ids],
        )
        conn.execute(
            """
            INSERT INTO cluster_summaries (
              run_id, cluster_id, size, mean_probability,
              representative_record_ids_json, source_mix_json
            )
            VALUES (?, 0, ?, 1.0, ?, '{}')
            """,
            (run_id, len(record_ids), json.dumps(record_ids)),
        )
        conn.commit()
    return run_id
