from __future__ import annotations

import asyncio
import gzip
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from datagraph.core.ids import new_id
from datagraph.db import connect
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


def test_artifact_gzip_etag_cache_float_precision_and_records_slimming(
    tmp_path: Path,
) -> None:
    records = generate_records(5000, 42)
    with _phase7_client(tmp_path, records, ScriptedLabelProvider()) as client:
        graph = _create_graph(client)
        graph_id = graph["id"]
        view_id = _all_records_view_id(graph)
        _post_records(client, graph_id, records)

        list_response = client.get(f"/api/graphs/{graph_id}/records?limit=1")
        assert list_response.status_code == 200, list_response.text
        listed = list_response.json()["records"][0]
        assert "normalized" not in listed
        included_response = client.get(
            f"/api/graphs/{graph_id}/records?limit=1&include=normalized"
        )
        assert "normalized" in included_response.json()["records"][0]
        single_response = client.get(f"/api/graphs/{graph_id}/records/{listed['id']}")
        assert single_response.status_code == 200, single_response.text
        assert "normalized" in single_response.json()

        view_list_response = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/records?limit=1"
        )
        assert "normalized" not in view_list_response.json()["records"][0]
        view_include_response = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/records?limit=1&include=normalized"
        )
        assert "normalized" in view_include_response.json()["records"][0]

        embed_run_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        assert _poll_run(client, graph_id, embed_run_id, timeout=60)["status"] == "succeeded"
        cluster_run_id = _enqueue_cluster(client, graph_id, view_id)
        assert _poll_run(client, graph_id, cluster_run_id, timeout=180)["status"] == "succeeded"
        label_run_id = client.post(f"/api/graphs/{graph_id}/views/{view_id}/label", json={}).json()[
            "id"
        ]
        assert _poll_run(client, graph_id, label_run_id, timeout=180)["status"] == "succeeded"
        trend_run_id = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/trends",
            json={
                "time": {"bucket": "week"},
                "window": {
                    "start": "2025-12-01T00:00:00Z",
                    "end": "2025-12-31T23:59:59Z",
                },
            },
        ).json()["id"]
        assert _poll_run(client, graph_id, trend_run_id, timeout=60)["status"] == "succeeded"
        layout_run_id = client.post(
            f"/api/graphs/{graph_id}/views/{view_id}/layout",
            json={},
        ).json()["id"]
        assert _poll_run(client, graph_id, layout_run_id, timeout=240)["status"] == "succeeded"

        path = f"/api/graphs/{graph_id}/views/{view_id}/artifact"
        first = _raw_asgi_get(client, path, headers={"accept-encoding": "gzip"})
        assert first["status"] == 200
        assert first["headers"]["content-encoding"] == "gzip"
        assert int(first["headers"]["content-length"]) <= 550_000
        assert len(first["body"]) <= 550_000
        assert first["headers"]["cache-control"] == "no-cache"
        etag = first["headers"]["etag"]
        artifact = json.loads(gzip.decompress(first["body"]))
        assert artifact["runRefs"] == {
            "embeddingRunId": embed_run_id,
            "clusterRunId": cluster_run_id,
            "layoutRunId": layout_run_id,
            "labelRunId": label_run_id,
            "trendRunId": trend_run_id,
        }
        _assert_artifact_float_precision(artifact)
        assert client.app.state.artifact_compositions == 1

        not_modified = _raw_asgi_get(
            client,
            path,
            headers={"accept-encoding": "gzip", "if-none-match": etag},
        )
        assert not_modified["status"] == 304
        assert not_modified["body"] == b""
        assert client.app.state.artifact_compositions == 1

        cold_repeat = _raw_asgi_get(client, path, headers={"accept-encoding": "gzip"})
        assert cold_repeat["status"] == 200
        assert client.app.state.artifact_compositions == 1
        assert gzip.decompress(cold_repeat["body"]) == gzip.decompress(first["body"])

        replacement_cluster_id = artifact["topics"][0]["clusterId"]
        _insert_non_default_label(
            client,
            graph_id=graph_id,
            view_id=view_id,
            cluster_run_id=cluster_run_id,
            cluster_id=replacement_cluster_id,
        )
        relabeled = _raw_asgi_get(
            client,
            path,
            headers={"accept-encoding": "gzip", "if-none-match": etag},
        )
        assert relabeled["status"] == 200
        assert relabeled["headers"]["etag"] != etag
        relabeled_artifact = json.loads(gzip.decompress(relabeled["body"]))
        relabeled_topic = next(
            topic
            for topic in relabeled_artifact["topics"]
            if topic["clusterId"] == replacement_cluster_id
        )
        assert relabeled_topic["label"] == "Phase 10 non-default relabel"
        assert client.app.state.artifact_compositions == 2


def test_static_cache_control_headers(tmp_path: Path) -> None:
    dist = Path(__file__).resolve().parents[1] / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>Phase 10</title>", encoding="utf-8")
    (assets / "phase10-cache-test.js").write_text("console.log('phase10');", encoding="utf-8")

    with TestClient(create_app(Settings(data_dir=tmp_path / "data", port=0))) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert index.headers["cache-control"] == "no-cache"
        asset = client.get("/assets/phase10-cache-test.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


def _assert_artifact_float_precision(artifact: dict[str, Any]) -> None:
    for topic in artifact["topics"]:
        _assert_max_4_decimals(topic["meanProbability"])
        if topic["trend"] is not None:
            _assert_max_4_decimals(topic["trend"]["spikeScore"])
    for row in artifact["data"]:
        _assert_max_4_decimals(row["x"])
        _assert_max_4_decimals(row["y"])
        _assert_max_4_decimals(row["clusterProbability"])
        _assert_max_4_decimals(row["outlierScore"])


def _assert_max_4_decimals(value: float) -> None:
    exponent = Decimal(str(value)).as_tuple().exponent
    assert abs(min(exponent, 0)) <= 4, value


def _insert_non_default_label(
    client: TestClient,
    *,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
    cluster_id: int,
) -> None:
    label_run_id = new_id("run")
    created_at = "2099-01-01T00:00:00Z"
    with connect(client.app.state.settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (
              id, graph_id, view_id, type, status, params_json, progress_json,
              error_text, input_refs_json, stats_json, created_at, started_at, completed_at
            )
            VALUES (?, ?, ?, 'label', 'succeeded', ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                label_run_id,
                graph_id,
                view_id,
                json.dumps({"label": {"setDefault": False}}, sort_keys=True),
                json.dumps({"state": "complete"}, sort_keys=True),
                json.dumps({"clusterRunId": cluster_run_id}, sort_keys=True),
                json.dumps({"topics": 1}, sort_keys=True),
                created_at,
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO cluster_labels (
              id, label_run_id, cluster_run_id, cluster_id, model,
              prompt_hash, top_k, label, summary, key_signals_json,
              tags_json, coherent, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("lbl"),
                label_run_id,
                cluster_run_id,
                cluster_id,
                "phase10-regression",
                "phase10-regression",
                12,
                "Phase 10 non-default relabel",
                "Inserted after the artifact was cached.",
                json.dumps(["phase10"], sort_keys=True),
                json.dumps(["phase10"], sort_keys=True),
                1,
                created_at,
            ),
        )
        conn.commit()


def _raw_asgi_get(
    client: TestClient,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        response_start: dict[str, Any] = {}
        body_parts: list[bytes] = []
        request_headers = [(b"host", b"testserver")]
        for key, value in (headers or {}).items():
            request_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": request_headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                response_start.update(message)
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))

        await client.app(scope, receive, send)
        return {
            "status": response_start["status"],
            "headers": {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in response_start["headers"]
            },
            "body": b"".join(body_parts),
        }

    return asyncio.run(run())
