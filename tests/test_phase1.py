from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from datagraph.db import connect
from datagraph.main import create_app
from datagraph.settings import Settings
from scripts.gen_synthetic import generate_records


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=tmp_path / "data", port=0)))


def _create_graph(client: TestClient, *, text_fields: list[str] | None = None) -> dict[str, Any]:
    response = client.post(
        "/api/graphs",
        json={
            "name": "Supplement DTC",
            "config": {"embedding": {"textFields": text_fields or ["customerText"]}},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _post_records(
    client: TestClient,
    graph_id: str,
    records: list[dict[str, Any]],
    **extra: Any,
) -> dict:
    response = client.post(f"/api/graphs/{graph_id}/records", json={"records": records, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _minimal_record(record_id: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "recordId": record_id,
        "sourceType": "support_ticket",
        "sourceName": "zendesk",
        "sourceRecordId": f"ticket-{record_id}",
        "title": "Refund request",
        "customerText": "Please help with the refund.",
        "recordUrl": "https://example.test/ticket",
        "product": "Sleep Drops",
        "sku": "SLEEP-DROPS",
        "rating": 2,
        "sentiment": "negative",
        "tags": ["refund", "support_ticket"],
        "timestamp": "2025-01-02T03:04:05Z",
        "metadata": {"groundTruthTopicId": "refund_friction"},
    }
    record.update(overrides)
    return record


def _upload_batches(
    client: TestClient,
    graph_id: str,
    records: list[dict[str, Any]],
) -> dict[str, int]:
    totals = {"created": 0, "updated": 0, "rejected": 0}
    for start in range(0, len(records), 1000):
        result = _post_records(client, graph_id, records[start : start + 1000])
        totals["created"] += result["created"]
        totals["updated"] += result["updated"]
        totals["rejected"] += len(result["rejected"])
    return totals


def _count(records: Iterable[dict[str, Any]], **criteria: Any) -> int:
    return sum(1 for record in records if _matches(record, criteria))


def _matches(record: dict[str, Any], criteria: dict[str, Any]) -> bool:
    if "sourceType" in criteria and record["sourceType"] != criteria["sourceType"]:
        return False
    if "product" in criteria and record["product"] != criteria["product"]:
        return False
    if "sentiment" in criteria and record["sentiment"] != criteria["sentiment"]:
        return False
    if "timestampPrefix" in criteria and not record["timestamp"].startswith(
        criteria["timestampPrefix"]
    ):
        return False
    return not (
        "groundTruthTopicId" in criteria
        and record["metadata"]["groundTruthTopicId"] != criteria["groundTruthTopicId"]
    )


def test_graph_config_validation_defaults_and_patch_revalidation(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        missing = client.post("/api/graphs", json={"name": "Bad", "config": {"embedding": {}}})
        assert missing.status_code == 422
        assert "textFields" in missing.text

        pacmap = client.post(
            "/api/graphs",
            json={
                "name": "Bad",
                "config": {
                    "embedding": {"textFields": ["customerText"]},
                    "cluster": {"space": {"method": "pacmap"}},
                },
            },
        )
        assert pacmap.status_code == 422
        assert "umap" in pacmap.text

        unknown = client.post(
            "/api/graphs",
            json={
                "name": "Bad",
                "config": {"embedding": {"textFields": ["customerText"]}, "extra": True},
            },
        )
        assert unknown.status_code == 422
        assert "unknown config key" in unknown.text

        graph = _create_graph(client, text_fields=["title", "customerText"])
        graph_list = client.get("/api/graphs")
        assert graph_list.status_code == 200
        assert [item["id"] for item in graph_list.json()["graphs"]] == [graph["id"]]

        config = graph["config"]
        assert config["embedding"]["provider"] == "openai"
        assert config["embedding"]["model"] == "text-embedding-3-small"
        assert config["embedding"]["textFields"] == ["title", "customerText"]
        assert config["cluster"]["space"]["method"] == "umap"
        assert config["layout"]["nNeighbors"] == 30
        assert config["labeling"]["model"] == "gpt-5.4-mini"
        assert config["time"]["bucket"] == "week"

        patched = client.patch(
            f"/api/graphs/{graph['id']}",
            json={"config": {"cluster": {"space": {"method": "none"}}}},
        )
        assert patched.status_code == 200, patched.text
        patched_config = patched.json()["config"]
        assert patched_config["cluster"]["space"]["method"] == "none"
        assert patched_config["cluster"]["hdbscan"]["clusterSelectionMethod"] == "eom"
        assert patched_config["embedding"]["textFields"] == ["title", "customerText"]

        invalid_patch = client.patch(
            f"/api/graphs/{graph['id']}",
            json={"config": {"embedding": {"model": "text-embedding-3-small"}}},
        )
        assert invalid_patch.status_code == 422
        assert "textFields" in invalid_patch.text


def test_record_validation_atomic_skip_timestamps_and_null_optionals(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        valid = _minimal_record("valid-1")
        invalid = [
            _minimal_record("missing-record-id") | {"recordId": None},
            _minimal_record("empty-text") | {"customerText": "  "},
            _minimal_record("epoch-time") | {"timestamp": 1735776000},
            _minimal_record("bad-iso") | {"timestamp": "not-a-date"},
        ]

        rejected = client.post(
            f"/api/graphs/{graph_id}/records",
            json={"records": [valid, *invalid]},
        )
        assert rejected.status_code == 422
        body = rejected.json()["detail"]
        assert [item["index"] for item in body["rejected"]] == [1, 2, 3, 4]
        assert client.get(f"/api/graphs/{graph_id}/records").json()["total"] == 0

        skipped = _post_records(
            client,
            graph_id,
            [valid, *invalid],
            onInvalid="skip",
        )
        assert skipped["created"] == 1
        assert skipped["updated"] == 0
        assert len(skipped["rejected"]) == 4

        timestamp_records = [
            _minimal_record(
                "naive-time",
                timestamp="2025-01-02T03:04:05",
                title=None,
                rating=None,
            ),
            _minimal_record(
                "offset-time",
                timestamp="2025-01-02T01:04:05-02:00",
                title=None,
                rating=None,
            ),
        ]
        result = _post_records(client, graph_id, timestamp_records)
        assert result["created"] == 2

        naive = client.get(f"/api/graphs/{graph_id}/records/naive-time").json()
        offset = client.get(f"/api/graphs/{graph_id}/records/offset-time").json()
        assert naive["timestamp"] == "2025-01-02T03:04:05Z"
        assert offset["timestamp"] == "2025-01-02T03:04:05Z"
        assert naive["title"] is None
        assert naive["rating"] is None


def test_records_pagination_filters_and_batch_limit(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        records = [
            _minimal_record(
                "r1",
                sourceType="support_ticket",
                product="Sleep Drops",
                sentiment="negative",
            ),
            _minimal_record(
                "r2",
                sourceType="social_comment",
                product="Focus Gummies",
                sentiment="neutral",
                timestamp="2025-12-02T00:00:00Z",
                title=None,
                rating=None,
            ),
            _minimal_record(
                "r3",
                sourceType="product_review",
                product="Sleep Drops",
                sentiment="positive",
            ),
        ]
        assert _post_records(client, graph_id, records)["created"] == 3

        too_many = client.post(
            f"/api/graphs/{graph_id}/records",
            json={"records": [_minimal_record(f"too-many-{index}") for index in range(1001)]},
        )
        assert too_many.status_code == 422

        first_page = client.get(f"/api/graphs/{graph_id}/records", params={"limit": 2}).json()
        second_page = client.get(
            f"/api/graphs/{graph_id}/records",
            params={"limit": 2, "offset": 2},
        ).json()
        assert first_page["total"] == 3
        assert len(first_page["records"]) == 2
        assert len(second_page["records"]) == 1

        source_filtered = client.get(
            f"/api/graphs/{graph_id}/records",
            params={"sourceType": "social_comment"},
        ).json()
        assert source_filtered["total"] == 1
        assert source_filtered["records"][0]["recordId"] == "r2"

        product_filtered = client.get(
            f"/api/graphs/{graph_id}/records",
            params={"product": "Sleep Drops"},
        ).json()
        assert product_filtered["total"] == 2

        sentiment_filtered = client.get(
            f"/api/graphs/{graph_id}/records",
            params={"sentiment": "positive"},
        ).json()
        assert sentiment_filtered["records"][0]["recordId"] == "r3"

        december_filtered = client.get(
            f"/api/graphs/{graph_id}/records",
            params={"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
        ).json()
        assert december_filtered["total"] == 1

        by_internal_id = client.get(
            f"/api/graphs/{graph_id}/records/{first_page['records'][0]['id']}"
        ).json()
        assert by_internal_id["recordId"] == first_page["records"][0]["recordId"]


def test_synthetic_5k_upload_upsert_scoped_views_and_delete_cascade(tmp_path: Path) -> None:
    records = generate_records(5000, 42)
    with _client(tmp_path) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]

        first_upload = _upload_batches(client, graph_id, records)
        assert first_upload == {"created": 5000, "updated": 0, "rejected": 0}
        graph_detail = client.get(f"/api/graphs/{graph_id}").json()
        assert graph_detail["recordCount"] == 5000
        all_records_view = next(
            view for view in graph_detail["views"] if view["name"] == "all_records"
        )
        assert all_records_view["recordCount"] == 5000

        second_upload = _upload_batches(client, graph_id, records)
        assert second_upload == {"created": 0, "updated": 5000, "rejected": 0}
        assert client.get(f"/api/graphs/{graph_id}/records").json()["total"] == 5000

        source_type = "social_comment"
        source_count = _count(records, sourceType=source_type)
        source_view = _create_view(
            client,
            graph_id,
            "social",
            {"sourceTypes": [source_type]},
        )
        assert source_view["recordCount"] == source_count

        december_count = _count(records, timestampPrefix="2025-12")
        december_view = _create_view(
            client,
            graph_id,
            "december",
            {"timeRange": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"}},
        )
        assert december_view["recordCount"] == december_count

        topic_id = "december_energy_crash_spike"
        topic_count = _count(records, groundTruthTopicId=topic_id)
        topic_view = _create_view(
            client,
            graph_id,
            "topic",
            {"metadataEquals": {"groundTruthTopicId": topic_id}},
        )
        assert topic_view["recordCount"] == topic_count

        combined_count = _count(
            records,
            sourceType=source_type,
            timestampPrefix="2025-12",
            groundTruthTopicId=topic_id,
        )
        combined_view = _create_view(
            client,
            graph_id,
            "combined",
            {
                "sourceTypes": [source_type],
                "timeRange": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
                "metadataEquals": {"groundTruthTopicId": topic_id},
            },
        )
        assert combined_view["recordCount"] == combined_count
        assert combined_view["defaultEmbeddingRunId"] is None
        assert combined_view["defaultClusterRunId"] is None
        assert combined_view["defaultLayoutRunId"] is None
        assert combined_view["defaultLabelRunId"] is None
        assert combined_view["defaultTrendRunId"] is None

        view_list = client.get(f"/api/graphs/{graph_id}/views").json()
        assert {view["name"] for view in view_list["views"]} >= {
            "all_records",
            "social",
            "december",
            "topic",
            "combined",
        }
        view_detail = client.get(f"/api/graphs/{graph_id}/views/{combined_view['id']}").json()
        assert view_detail["recordCount"] == combined_count

        records_in_view = client.get(
            f"/api/graphs/{graph_id}/views/{combined_view['id']}/records",
            params={"limit": 1000},
        ).json()
        assert records_in_view["total"] == combined_count
        assert len(records_in_view["records"]) == min(combined_count, 1000)

        duplicate = client.post(
            f"/api/graphs/{graph_id}/views",
            json={"name": "combined", "scope": {}},
        )
        assert duplicate.status_code == 409

        unknown_scope = client.post(
            f"/api/graphs/{graph_id}/views",
            json={"name": "bad-scope", "scope": {"unknown": True}},
        )
        assert unknown_scope.status_code == 422

        delete = client.delete(f"/api/graphs/{graph_id}")
        assert delete.status_code == 204
        with connect(client.app.state.settings.db_path) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM records WHERE graph_id = ?",
                    (graph_id,),
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM views WHERE graph_id = ?",
                    (graph_id,),
                ).fetchone()[0]
                == 0
            )


def _create_view(
    client: TestClient,
    graph_id: str,
    name: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(f"/api/graphs/{graph_id}/views", json={"name": name, "scope": scope})
    assert response.status_code == 201, response.text
    return response.json()
