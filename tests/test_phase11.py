from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

from datagraph.core.embedding_text import render_embedding_text
from datagraph.db import connect
from datagraph.main import create_app
from datagraph.runs import cluster as cluster_mod
from datagraph.runs.cluster import _clustering_space
from datagraph.settings import Settings
from scripts.gen_synthetic import generate_records
from tests.test_phase3 import (
    StructuredTopicProvider,
    _all_records_view,
    _create_graph,
    _enqueue_cluster,
    _enqueue_embedding,
    _enqueue_layout,
    _membership_rows,
    _poll_run,
    _post_records,
)
from tests.test_phase4 import ScriptedLabelProvider
from tests.test_phase6 import _enqueue_trend, _post_evidence


def test_cluster_min_dist_default_validation_and_umap_pass_through(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path / "data", port=0))) as client:
        valid = client.post(
            "/api/graphs",
            json={
                "name": "Phase 11 minDist",
                "config": {"embedding": {"textFields": ["customerText"]}},
            },
        )
        assert valid.status_code == 201, valid.text
        config = valid.json()["config"]
        assert config["cluster"]["space"]["minDist"] == 0.1
        assert config["layout"]["minDist"] == 0.1

        for value in (-0.1, "0.1"):
            invalid = client.post(
                "/api/graphs",
                json={
                    "name": f"bad {value}",
                    "config": {
                        "embedding": {"textFields": ["customerText"]},
                        "cluster": {"space": {"minDist": value}},
                    },
                },
            )
            assert invalid.status_code == 422
            assert "config.cluster.space.minDist" in invalid.text

    captured: dict[str, Any] = {}

    class FakeUMAP:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def fit_transform(self, matrix: np.ndarray) -> np.ndarray:
            return np.zeros((len(matrix), captured["n_components"]), dtype=np.float32)

    monkeypatch.setattr(cluster_mod.umap, "UMAP", FakeUMAP)
    _clustering_space(
        np.ones((8, 6), dtype=np.float32),
        {
            "space": {
                "method": "umap",
                "nComponents": 3,
                "nNeighbors": 4,
                "metric": "cosine",
                "minDist": 0.25,
            },
            "seed": 17,
        },
    )
    assert captured["min_dist"] == 0.25


def test_focus_recluster_lineage_defaults_and_drill_down_labeling(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:600]
    label_provider = ScriptedLabelProvider()
    with _phase11_client(tmp_path, records, label_provider) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view(client, graph_id)["id"]

        no_default = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/cluster",
            json={"focus": {"clusterId": 0}},
        )
        assert no_default.status_code == 409
        assert "/cluster" in no_default.text

        _post_records(client, graph_id, records)
        embed_run = _poll_run(client, graph_id, _enqueue_embedding(client, graph_id), timeout=60)
        assert embed_run["status"] == "succeeded"
        layout_run = _poll_run(
            client,
            graph_id,
            _enqueue_layout(client, graph_id, view_id),
            timeout=120,
        )
        assert layout_run["params"]["layout"]["minDist"] == 0.1
        base_run = _poll_run(client, graph_id, _enqueue_cluster(client, graph_id, view_id))
        assert base_run["params"]["cluster"]["space"]["minDist"] == 0.1
        focus_cluster_id, expected_member_ids = _largest_cluster_members(client, base_run["id"])

        unknown_run = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/cluster",
            json={"focus": {"clusterRunId": "run_missing", "clusterId": focus_cluster_id}},
        )
        assert unknown_run.status_code == 404

        unknown_cluster = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/cluster",
            json={"focus": {"clusterId": 999999}},
        )
        assert unknown_cluster.status_code == 404
        assert "valid ids" in unknown_cluster.text

        promoted_focus = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/cluster",
            json={"focus": {"clusterId": focus_cluster_id}, "setDefault": True},
        )
        assert promoted_focus.status_code == 422
        assert "cannot become the view default" in promoted_focus.text

        focus_run_id = _enqueue_cluster(
            client,
            graph_id,
            view_id,
            {
                "focus": {"clusterId": focus_cluster_id},
                "cluster": {
                    "space": {"nComponents": 5, "nNeighbors": 10},
                    "hdbscan": {
                        "minClusterSize": 5,
                        "minSamples": 2,
                        "allowSingleCluster": True,
                    },
                },
            },
        )
        focus_run = _poll_run(client, graph_id, focus_run_id, timeout=120)
        assert focus_run["status"] == "succeeded", focus_run
        assert focus_run["params"]["setDefault"] is False
        assert focus_run["inputRefs"]["focusClusterRunId"] == base_run["id"]
        assert focus_run["inputRefs"]["focusClusterId"] == focus_cluster_id
        assert focus_run["stats"]["population"] == len(expected_member_ids)

        focus_rows = _membership_rows(client, focus_run_id)
        assert {row["record_id"] for row in focus_rows} == expected_member_ids
        _assert_focus_summaries(client, focus_run_id, focus_rows)
        refreshed_view = client.get(f"/api/graphs/{graph_id}/views/{view_id}").json()
        assert refreshed_view["defaultClusterRunId"] == base_run["id"]

        label_run_id = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/label",
            json={"clusterRunId": focus_run_id, "setDefault": False},
        ).json()["id"]
        label_run = _poll_run(client, graph_id, label_run_id, timeout=120)
        assert label_run["status"] == "succeeded", label_run
        focused_topics = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics",
            params={"clusterRunId": focus_run_id},
        )
        assert focused_topics.status_code == 200, focused_topics.text
        assert any(topic["label"] is not None for topic in focused_topics.json()["topics"])


def test_topics_and_evidence_facet_by_counts_and_validation(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:700]
    with _phase11_client(tmp_path, records, ScriptedLabelProvider()) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view(client, graph_id)["id"]
        _post_records(client, graph_id, records)
        assert _poll_run(client, graph_id, _enqueue_embedding(client, graph_id))["status"] == (
            "succeeded"
        )
        cluster_run = _poll_run(client, graph_id, _enqueue_cluster(client, graph_id, view_id))
        assert cluster_run["status"] == "succeeded"
        trend_run = _poll_run(
            client,
            graph_id,
            _enqueue_trend(
                client,
                graph_id,
                view_id,
                "2025-12-01T00:00:00Z",
                "2025-12-31T23:59:59Z",
            ),
        )
        assert trend_run["status"] == "succeeded"
        cluster_id, _ = _largest_cluster_members(client, cluster_run["id"])
        _write_high_cardinality_metadata(client, cluster_run["id"], cluster_id)

        source_topics = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics",
            params={"facetBy": "sourceType"},
        )
        assert source_topics.status_code == 200, source_topics.text
        source_topic = _topic_by_id(source_topics.json()["topics"], cluster_id)
        assert source_topic["facets"] == _source_type_counts(client, cluster_run["id"], cluster_id)

        metadata_topics = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics",
            params={"facetBy": "metadata.groundTruthTopicId"},
        )
        assert metadata_topics.status_code == 200, metadata_topics.text
        metadata_topic = _topic_by_id(metadata_topics.json()["topics"], cluster_id)
        assert metadata_topic["facets"] == _metadata_counts(
            client,
            cluster_run["id"],
            cluster_id,
            "groundTruthTopicId",
        )

        high_card_topics = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics",
            params={"facetBy": "metadata.phase11HighCard"},
        ).json()
        high_card_facets = _topic_by_id(high_card_topics["topics"], cluster_id)["facets"]
        assert "(none)" in high_card_facets
        assert "(other)" in high_card_facets
        assert len(high_card_facets) <= 21

        invalid_topics = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics",
            params={"facetBy": "metadata.nested.key"},
        )
        assert invalid_topics.status_code == 422
        assert "metadata.<key>" in invalid_topics.text

        surprising = _post_evidence(
            client,
            graph_id,
            {
                "viewId": view_id,
                "recipe": "surprising_topics",
                "timeRange": {
                    "start": "2025-12-01T00:00:00Z",
                    "end": "2025-12-31T23:59:59Z",
                },
                "facetBy": "sourceType",
            },
        )
        assert all("facets" in row for row in surprising["evidence"])

        topic_evidence = _post_evidence(
            client,
            graph_id,
            {
                "viewId": view_id,
                "recipe": "topic_evidence",
                "topicId": cluster_id,
                "facetBy": "metadata.groundTruthTopicId",
            },
        )
        assert topic_evidence["evidence"]["facets"] == metadata_topic["facets"]

        invalid_evidence = client.post(
            f"/api/graphs/{graph_id}/evidence",
            json={
                "viewId": view_id,
                "recipe": "topic_evidence",
                "topicId": cluster_id,
                "facetBy": "owner",
            },
        )
        assert invalid_evidence.status_code == 422


def _phase11_client(
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
            Settings(data_dir=tmp_path / f"data-{time.monotonic_ns()}", port=0),
            embedding_provider_factory=lambda _config: provider,
            label_provider_factory=lambda _config: label_provider,
        )
    )


def _largest_cluster_members(client: TestClient, cluster_run_id: str) -> tuple[int, set[str]]:
    rows = _membership_rows(client, cluster_run_id)
    counts: dict[int, set[str]] = {}
    for row in rows:
        cluster_id = int(row["cluster_id"])
        if cluster_id == -1:
            continue
        counts.setdefault(cluster_id, set()).add(row["record_id"])
    assert counts
    return max(counts.items(), key=lambda item: (len(item[1]), -item[0]))


def _assert_focus_summaries(
    client: TestClient,
    focus_run_id: str,
    focus_rows: list[dict[str, Any]],
) -> None:
    with connect(client.app.state.settings.db_path) as conn:
        summaries = conn.execute(
            """
            SELECT *
              FROM cluster_summaries
             WHERE run_id = ?
             ORDER BY cluster_id ASC
            """,
            (focus_run_id,),
        ).fetchall()
    assert summaries
    cluster_ids = [int(row["cluster_id"]) for row in summaries]
    assert cluster_ids == list(range(len(cluster_ids)))
    member_ids = {row["record_id"] for row in focus_rows if int(row["cluster_id"]) != -1}
    for summary in summaries:
        reps = set(json.loads(summary["representative_record_ids_json"]))
        assert reps
        assert reps <= member_ids


def _topic_by_id(topics: list[dict[str, Any]], cluster_id: int) -> dict[str, Any]:
    return next(topic for topic in topics if topic["clusterId"] == cluster_id)


def _source_type_counts(client: TestClient, cluster_run_id: str, cluster_id: int) -> dict[str, int]:
    with connect(client.app.state.settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.source_type
              FROM cluster_memberships cm
              JOIN records r ON r.id = cm.record_id
             WHERE cm.run_id = ? AND cm.cluster_id = ?
            """,
            (cluster_run_id, cluster_id),
        ).fetchall()
    return dict(sorted(Counter(row["source_type"] or "(none)" for row in rows).items()))


def _metadata_counts(
    client: TestClient,
    cluster_run_id: str,
    cluster_id: int,
    key: str,
) -> dict[str, int]:
    with connect(client.app.state.settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.metadata_json
              FROM cluster_memberships cm
              JOIN records r ON r.id = cm.record_id
             WHERE cm.run_id = ? AND cm.cluster_id = ?
            """,
            (cluster_run_id, cluster_id),
        ).fetchall()
    counts = Counter()
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        counts[str(metadata.get(key) or "(none)")] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _write_high_cardinality_metadata(
    client: TestClient,
    cluster_run_id: str,
    cluster_id: int,
) -> None:
    with connect(client.app.state.settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.metadata_json
              FROM cluster_memberships cm
              JOIN records r ON r.id = cm.record_id
             WHERE cm.run_id = ? AND cm.cluster_id = ?
             ORDER BY r.id ASC
            """,
            (cluster_run_id, cluster_id),
        ).fetchall()
        assert len(rows) >= 30
        for index, row in enumerate(rows):
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata.pop("phase11HighCard", None)
            if index < 25:
                metadata["phase11HighCard"] = f"value-{index:02d}"
            conn.execute(
                "UPDATE records SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, sort_keys=True), row["id"]),
            )
        conn.commit()
