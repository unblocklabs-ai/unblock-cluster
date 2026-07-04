#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import resource
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from datagraph.core.embedding_text import render_embedding_text  # noqa: E402
from datagraph.core.labeling import LabelResult  # noqa: E402
from datagraph.core.openai_client import MockEmbeddingProvider  # noqa: E402
from datagraph.core.vectors import normalize_l2  # noqa: E402
from datagraph.main import create_app  # noqa: E402
from datagraph.settings import Settings  # noqa: E402
from scripts.gen_synthetic import generate_records  # noqa: E402


class StructuredBenchmarkProvider(MockEmbeddingProvider):
    def __init__(self, text_to_topic: dict[str, str], *, dimensions: int = 1536) -> None:
        super().__init__(dimensions=dimensions, model="structured-benchmark")
        self.text_to_topic = text_to_topic
        self.centroids = {
            topic: self._centroid(topic) for topic in sorted(set(text_to_topic.values()))
        }

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        self.request_count += 1
        return [self.embed_text(text) for text in texts]

    def embed_text(self, text: str) -> np.ndarray:
        topic = self.text_to_topic[text]
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vector = self.centroids[topic].copy()
        for offset in range(0, 32, 2):
            index = int.from_bytes(digest[offset : offset + 2], "big") % self.dimensions
            vector[index] += (digest[offset] / 255.0 - 0.5) * 0.03
        return normalize_l2(vector)

    def _centroid(self, topic: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(topic.encode("utf-8")).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        return normalize_l2(rng.normal(0, 1, self.dimensions).astype(np.float32))


class BenchmarkLabelProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def label_cluster(self, prompt: str, representatives: list[str]) -> LabelResult:
        self.calls += 1
        text = " ".join(representatives).lower()
        label = _label_for_text(text, self.calls)
        await asyncio.sleep(0)
        return LabelResult(
            label=label,
            summary=f"Offline benchmark label for {label.lower()}.",
            key_signals=[label.lower()],
            tags=["benchmark", f"cluster-{self.calls}"],
            coherent=True,
        )


def run_benchmark(
    *,
    data_dir: Path,
    size: int = 100_000,
    seed: int | None = 42,
    port: int = 8080,
    dimensions: int = 1536,
) -> dict[str, Any]:
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    records = _generate_benchmark_records(
        size,
        seed if seed is not None else int(time.time_ns() % 2**32),
    )
    text_config = {
        "textFields": ["title", "customerText", "product", "tags"],
        "maxInputTokens": 8000,
    }
    text_to_topic = {
        render_embedding_text(record, text_config).text: record["metadata"]["groundTruthTopicId"]
        for record in records
    }
    embedding_provider = StructuredBenchmarkProvider(text_to_topic, dimensions=dimensions)
    label_provider = BenchmarkLabelProvider()
    timings: dict[str, float] = {}
    run_details: dict[str, dict[str, Any]] = {}

    with TestClient(
        create_app(
            Settings(data_dir=data_dir, port=port),
            embedding_provider_factory=lambda _config: embedding_provider,
            label_provider_factory=lambda _config: label_provider,
        )
    ) as client:
        graph_id, view_id = _time_stage(
            timings,
            "createGraph",
            lambda: _create_graph(client, dimensions=dimensions, seed=seed),
        )

        upload_totals = _time_stage(
            timings,
            "upload",
            lambda: _post_records(client, graph_id, records),
        )

        embed_id = _enqueue(client, f"/api/graphs/{graph_id}/embeddings", {})
        run_details["embed"] = _time_stage(
            timings,
            "embed",
            lambda: _poll_run(client, graph_id, embed_id, timeout=max(120, size / 100)),
        )
        cluster_id = _enqueue(client, f"/api/graphs/{graph_id}/views/{view_id}/cluster", {})
        run_details["cluster"] = _time_stage(
            timings,
            "cluster",
            lambda: _poll_run(client, graph_id, cluster_id, timeout=max(300, size / 20)),
        )
        layout_id = _enqueue(client, f"/api/graphs/{graph_id}/views/{view_id}/layout", {})
        run_details["layout"] = _time_stage(
            timings,
            "layout",
            lambda: _poll_run(client, graph_id, layout_id, timeout=max(300, size / 20)),
        )
        label_id = _enqueue(client, f"/api/graphs/{graph_id}/views/{view_id}/label", {})
        run_details["label"] = _time_stage(
            timings,
            "label",
            lambda: _poll_run(client, graph_id, label_id, timeout=max(120, size / 500)),
        )
        trend_id = _enqueue(
            client,
            f"/api/graphs/{graph_id}/views/{view_id}/trends",
            {
                "time": {"bucket": "week"},
                "window": {
                    "start": "2025-12-01T00:00:00Z",
                    "end": "2025-12-31T23:59:59Z",
                },
            },
        )
        run_details["trend"] = _time_stage(
            timings,
            "trend",
            lambda: _poll_run(client, graph_id, trend_id, timeout=max(120, size / 500)),
        )

        artifact_path = f"/api/graphs/{graph_id}/views/{view_id}/artifact"
        artifact_cold = _time_stage(
            timings,
            "artifactCold",
            lambda: _raw_asgi_get(client, artifact_path, headers={"accept-encoding": "gzip"}),
        )
        artifact_warm = _time_stage(
            timings,
            "artifactWarm",
            lambda: _raw_asgi_get(client, artifact_path, headers={"accept-encoding": "gzip"}),
        )
        artifact = json.loads(gzip.decompress(artifact_cold["body"]))
        top_topic_id = artifact["topics"][0]["clusterId"] if artifact["topics"] else -1

        surprising = _time_stage(
            timings,
            "evidenceSurprisingTopics",
            lambda: client.post(
                f"/api/graphs/{graph_id}/evidence",
                json={
                    "viewId": view_id,
                    "recipe": "surprising_topics",
                    "timeRange": {
                        "start": "2025-12-01T00:00:00Z",
                        "end": "2025-12-31T23:59:59Z",
                    },
                },
            ),
        )
        surprising.raise_for_status()
        topic_evidence = _time_stage(
            timings,
            "evidenceTopic",
            lambda: client.post(
                f"/api/graphs/{graph_id}/evidence",
                json={"viewId": view_id, "recipe": "topic_evidence", "topicId": top_topic_id},
            ),
        )
        topic_evidence.raise_for_status()

    db_path = data_dir / "datagraph.sqlite3"
    metrics = {
        "dataDir": str(data_dir),
        "graphId": graph_id,
        "viewId": view_id,
        "seed": seed,
        "recordCount": size,
        "embeddingDimensions": dimensions,
        "upload": {
            **upload_totals,
            "batches": (size + 999) // 1000,
            "recordsPerSecond": round(size / timings["upload"], 2) if timings["upload"] else 0,
        },
        "timingsSeconds": timings,
        "phaseDurations": {
            "cluster": run_details["cluster"]["stats"].get("phaseDurations", {}),
            "layout": run_details["layout"]["stats"].get("phaseDurations", {}),
        },
        "runs": {
            name: {
                "id": run["id"],
                "status": run["status"],
                "stats": run["stats"],
            }
            for name, run in run_details.items()
        },
        "artifact": {
            "topics": len(artifact["topics"]),
            "records": len(artifact["data"]),
            "gzipWireBytes": len(artifact_cold["body"]),
            "warmGzipWireBytes": len(artifact_warm["body"]),
            "etag": artifact_cold["headers"].get("etag"),
        },
        "evidence": {
            "surprisingTopicsSeconds": timings["evidenceSurprisingTopics"],
            "topicEvidenceSeconds": timings["evidenceTopic"],
        },
        "rss": {"peakBytes": _peak_rss_bytes(), "peakMiB": round(_peak_rss_bytes() / 2**20, 2)},
        "database": {
            "path": str(db_path),
            "bytes": db_path.stat().st_size if db_path.exists() else 0,
        },
        "vizUrl": f"http://127.0.0.1:{port}/?graphId={graph_id}&viewId={view_id}",
    }
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the offline Data Graph scale benchmark.")
    parser.add_argument("--data-dir", type=Path, default=Path("output/bench-scale"))
    parser.add_argument("--size", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-seed", action="store_true")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    seed = None if args.no_seed else args.seed
    metrics = run_benchmark(data_dir=args.data_dir, size=args.size, seed=seed, port=args.port)
    _print_report(metrics)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _create_graph(client: TestClient, *, dimensions: int, seed: int | None) -> tuple[str, str]:
    response = client.post(
        "/api/graphs",
        json={
            "name": "Scale Benchmark",
            "config": {
                "embedding": {
                    "provider": "mock",
                    "model": "structured-benchmark",
                    "dimensions": dimensions,
                    "textFields": ["title", "customerText", "product", "tags"],
                    "requestsPerMinute": 100_000,
                    "maxConcurrency": 8,
                },
                "cluster": {"seed": seed},
            },
        },
    )
    response.raise_for_status()
    graph = response.json()
    view_id = next(view["id"] for view in graph["views"] if view["name"] == "all_records")
    return graph["id"], view_id


def _post_records(
    client: TestClient,
    graph_id: str,
    records: list[dict[str, Any]],
) -> dict[str, int]:
    totals = {"created": 0, "updated": 0, "rejected": 0}
    for start in range(0, len(records), 1000):
        response = client.post(
            f"/api/graphs/{graph_id}/records",
            json={"records": records[start : start + 1000]},
        )
        response.raise_for_status()
        body = response.json()
        totals["created"] += body["created"]
        totals["updated"] += body["updated"]
        totals["rejected"] += len(body["rejected"])
    return totals


def _generate_benchmark_records(size: int, seed: int) -> list[dict[str, Any]]:
    if size in {5000, 50_000, 100_000}:
        return _stabilize_benchmark_texts(generate_records(size, seed))
    source_size = 5000 if size < 5000 else 50_000 if size < 50_000 else 100_000
    return _stabilize_benchmark_texts(generate_records(source_size, seed)[:size])


def _stabilize_benchmark_texts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stabilized = []
    for index, record in enumerate(records):
        topic = str(record["metadata"]["groundTruthTopicId"])
        bucket = index % 100
        topic_text = topic.replace("_", " ")
        updated = {
            **record,
            "title": f"{topic_text.title()} benchmark feedback {bucket}",
            "customerText": (
                f"Benchmark customer feedback bucket {bucket} for {topic_text}. "
                "This bounded text vocabulary keeps the 1536-dimensional mock "
                "embedding benchmark practical while preserving large-record API, "
                "artifact, evidence, and UI scale."
            ),
            "product": "Benchmark Product",
            "tags": ["benchmark", topic],
        }
        stabilized.append(updated)
    return stabilized


def _enqueue(client: TestClient, path: str, body: dict[str, Any]) -> str:
    response = client.post(path, json=body)
    response.raise_for_status()
    return response.json()["id"]


def _poll_run(
    client: TestClient,
    graph_id: str,
    run_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/graphs/{graph_id}/runs/{run_id}")
        response.raise_for_status()
        last = response.json()
        if last["status"] == "succeeded":
            return last
        if last["status"] in {"failed", "cancelled"}:
            raise RuntimeError(f"run {run_id} ended {last['status']}: {last}")
        time.sleep(0.05)
    raise TimeoutError(f"run {run_id} did not finish before timeout; last={last}")


def _time_stage(timings: dict[str, float], name: str, func: Any) -> Any:
    started = time.perf_counter()
    result = func()
    timings[name] = round(time.perf_counter() - started, 6)
    return result


def _label_for_text(text: str, index: int) -> str:
    if "refund" in text:
        return "Refund friction"
    if "shipping" in text or "delivery" in text:
        return "Shipping delays"
    if "taste" in text or "flavor" in text:
        return "Taste feedback"
    if "subscription" in text:
        return "Subscription confusion"
    return f"Benchmark topic {index}"


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _print_report(metrics: dict[str, Any]) -> None:
    print(json.dumps(metrics, indent=2, sort_keys=True))
    print("\nScale benchmark summary")
    print(f"records: {metrics['recordCount']:,}")
    print(
        "upload: "
        f"{metrics['upload']['batches']} batches, "
        f"{metrics['upload']['recordsPerSecond']:,} records/sec"
    )
    print(
        "artifact: "
        f"cold {metrics['timingsSeconds']['artifactCold']}s, "
        f"warm {metrics['timingsSeconds']['artifactWarm']}s, "
        f"gzip {metrics['artifact']['gzipWireBytes']:,} bytes"
    )
    print(
        "evidence: "
        f"surprising {metrics['evidence']['surprisingTopicsSeconds']}s, "
        f"topic {metrics['evidence']['topicEvidenceSeconds']}s"
    )
    print(f"peak RSS: {metrics['rss']['peakMiB']} MiB")
    print(f"database: {metrics['database']['bytes']:,} bytes")
    print(f"vizUrl: {metrics['vizUrl']}")


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
                key.decode("latin-1"): value.decode("latin-1")
                for key, value in response_start["headers"]
            },
            "body": b"".join(body_parts),
        }

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
