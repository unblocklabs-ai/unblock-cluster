from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from datagraph.main import create_app
from tests.helpers import test_settings


@pytest.mark.slow
def test_head_artifact_headers_empty_body_and_read_only(built_graph) -> None:
    client = built_graph.client
    graph_id = built_graph.graph_id
    view_id = built_graph.view_id
    data_dir = client.app.state.settings.data_dir

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

    with TestClient(
        create_app(
            test_settings(
                data_dir,
                read_only=True,
            )
        )
    ) as client:
        read_only_head = client.head(path)
        assert read_only_head.status_code == 200, read_only_head.text
        assert read_only_head.content == b""
        assert read_only_head.headers["etag"] == get_response.headers["etag"]


def test_head_health_uses_get_route_headers_without_body(tmp_path: Path) -> None:
    with TestClient(create_app(test_settings(tmp_path / "data"))) as client:
        get_response = client.get("/api/health")
        head_response = client.head("/api/health")

    assert get_response.status_code == 200
    assert head_response.status_code == 200
    assert head_response.content == b""
    assert head_response.headers["content-type"] == get_response.headers["content-type"]
