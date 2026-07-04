from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from datagraph.main import create_app
from datagraph.settings import Settings
from scripts.gen_synthetic import generate_records
from tests.test_phase4 import ScriptedLabelProvider
from tests.test_phase7 import (
    _all_records_view_id,
    _create_graph,
    _enqueue_cluster,
    _phase7_client,
    _poll_run,
    _post_records,
)


def test_head_artifact_headers_empty_body_and_read_only(tmp_path: Path) -> None:
    records = generate_records(5000, 42)[:160]
    data_dir = tmp_path / "data"
    with _phase7_client(tmp_path, records, ScriptedLabelProvider()) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)
        embed_run_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        assert _poll_run(client, graph_id, embed_run_id, timeout=60)["status"] == "succeeded"
        cluster_run_id = _enqueue_cluster(client, graph_id, view_id)
        assert _poll_run(client, graph_id, cluster_run_id, timeout=120)["status"] == "succeeded"
        layout_run_id = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/layout",
            json={},
        ).json()["id"]
        assert _poll_run(client, graph_id, layout_run_id, timeout=120)["status"] == "succeeded"

        path = f"/api/graphs/{graph_id}/views/{view_id}/artifact"
        get_response = client.get(path)
        assert get_response.status_code == 200, get_response.text

        head_response = client.head(path)
        assert head_response.status_code == 200, head_response.text
        assert head_response.content == b""
        assert head_response.headers["etag"] == get_response.headers["etag"]
        assert head_response.headers["cache-control"] == "no-cache"
        assert head_response.headers["content-type"] == get_response.headers["content-type"]

        not_modified = client.head(path, headers={"If-None-Match": get_response.headers["etag"]})
        assert not_modified.status_code == 304
        assert not_modified.content == b""
        assert not_modified.headers["etag"] == get_response.headers["etag"]
        assert not_modified.headers["cache-control"] == "no-cache"

    with TestClient(create_app(Settings(data_dir=data_dir, port=0, read_only=True))) as client:
        read_only_head = client.head(path)
        assert read_only_head.status_code == 200, read_only_head.text
        assert read_only_head.content == b""
        assert read_only_head.headers["etag"] == get_response.headers["etag"]


def test_head_health_uses_get_route_headers_without_body(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path / "data", port=0))) as client:
        get_response = client.get("/api/health")
        head_response = client.head("/api/health")

    assert get_response.status_code == 200
    assert head_response.status_code == 200
    assert head_response.content == b""
    assert head_response.headers["content-type"] == get_response.headers["content-type"]
