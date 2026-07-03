#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
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


class StructuredTopicProvider(MockEmbeddingProvider):
    def __init__(self, text_to_topic: dict[str, str], *, dimensions: int = 32) -> None:
        super().__init__(dimensions=dimensions)
        self.text_to_topic = text_to_topic
        self.centroids = {
            topic: self._centroid(topic) for topic in sorted(set(text_to_topic.values()))
        }

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed_text(text) for text in texts]

    def embed_text(self, text: str) -> np.ndarray:
        topic = self.text_to_topic[text]
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
        noise = rng.normal(0, 0.01, self.dimensions).astype(np.float32)
        return normalize_l2(self.centroids[topic] + noise)

    def _centroid(self, topic: str) -> np.ndarray:
        seed = int.from_bytes(topic.encode("utf-8")[:8].ljust(8, b"0"), "big")
        rng = np.random.default_rng(seed)
        return normalize_l2(rng.normal(0, 1, self.dimensions).astype(np.float32))


class ScriptedLabelProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def label_cluster(self, prompt: str, representatives: list[str]) -> LabelResult:
        self.calls += 1
        text = " ".join(representatives).lower()
        if "energy" in text or "crash" in text:
            label = "December energy crash"
            tags = ["energy", "december"]
        elif "creatine" in text:
            label = "Creatine launch questions"
            tags = ["creatine", "launch"]
        elif "packaging" in text or "pouch" in text:
            label = "Packaging seal complaints"
            tags = ["packaging"]
        else:
            label = f"Customer topic {self.calls}"
            tags = ["synthetic"]
        await asyncio.sleep(0)
        return LabelResult(
            label=label,
            summary=f"Scripted offline label for {label.lower()}.",
            key_signals=tags,
            tags=tags,
            coherent=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a complete offline Data Graph demo.")
    parser.add_argument("--data-dir", type=Path, default=Path("output/demo-data"))
    parser.add_argument("--size", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.data_dir.exists():
        shutil.rmtree(args.data_dir)
    args.data_dir.mkdir(parents=True, exist_ok=True)

    records = generate_records(args.size, args.seed)
    text_config = {
        "textFields": ["title", "customerText", "product", "tags"],
        "maxInputTokens": 8000,
    }
    text_to_topic = {
        render_embedding_text(record, text_config).text: record["metadata"]["groundTruthTopicId"]
        for record in records
    }
    embedding_provider = StructuredTopicProvider(text_to_topic)
    label_provider = ScriptedLabelProvider()
    settings = Settings(data_dir=args.data_dir, port=args.port)
    with TestClient(
        create_app(
            settings,
            embedding_provider_factory=lambda _config: embedding_provider,
            label_provider_factory=lambda _config: label_provider,
        )
    ) as client:
        graph_id, view_id = _create_graph(client)
        _post_records(client, graph_id, records)
        embed_id = client.post(f"/api/graphs/{graph_id}/embeddings", json={}).json()["id"]
        _run(client, graph_id, embed_id)
        _run(
            client,
            graph_id,
            client.post(f"/api/graphs/{graph_id}/views/{view_id}/cluster", json={}).json()["id"],
            timeout=180,
        )
        _run(
            client,
            graph_id,
            client.post(f"/api/graphs/{graph_id}/views/{view_id}/label", json={}).json()["id"],
        )
        _run(
            client,
            graph_id,
            client.post(
                f"/api/graphs/{graph_id}/views/{view_id}/trends",
                json={
                    "time": {"bucket": "week"},
                    "window": {
                        "start": "2025-12-01T00:00:00Z",
                        "end": "2025-12-31T23:59:59Z",
                    },
                },
            ).json()["id"],
        )
        _run(
            client,
            graph_id,
            client.post(f"/api/graphs/{graph_id}/views/{view_id}/layout", json={}).json()["id"],
            timeout=240,
        )
        artifact = client.get(f"/api/graphs/{graph_id}/views/{view_id}/artifact").json()

    viz_url = f"http://127.0.0.1:{args.port}/?graphId={graph_id}&viewId={view_id}"
    print(
        json.dumps(
            {
                "dataDir": str(args.data_dir),
                "graphId": graph_id,
                "viewId": view_id,
                "vizUrl": viz_url,
                "artifactUrl": f"http://127.0.0.1:{args.port}/api/graphs/{graph_id}/views/{view_id}/artifact",
                "topics": len(artifact["topics"]),
                "records": len(artifact["data"]),
                "serveCommand": (
                    f"DATAGRAPH_DATA_DIR={args.data_dir} "
                    ".venv/bin/python -m datagraph.main"
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _create_graph(client: TestClient) -> tuple[str, str]:
    response = client.post(
        "/api/graphs",
        json={
            "name": "Offline Demo",
            "config": {
                "embedding": {
                    "provider": "mock",
                    "model": "structured-demo",
                    "dimensions": 32,
                    "textFields": ["title", "customerText", "product", "tags"],
                },
                "cluster": {
                    "space": {"method": "none"},
                    "hdbscan": {"minClusterSize": 10, "minSamples": 3},
                    "seed": 42,
                },
            },
        },
    )
    response.raise_for_status()
    graph = response.json()
    view_id = next(view["id"] for view in graph["views"] if view["name"] == "all_records")
    return graph["id"], view_id


def _post_records(client: TestClient, graph_id: str, records: list[dict[str, Any]]) -> None:
    for start in range(0, len(records), 1000):
        response = client.post(
            f"/api/graphs/{graph_id}/records",
            json={"records": records[start : start + 1000]},
        )
        response.raise_for_status()


def _run(
    client: TestClient,
    graph_id: str,
    run_id: str,
    *,
    timeout: float = 120,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/graphs/{graph_id}/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
        if run["status"] == "succeeded":
            return run
        if run["status"] in {"failed", "cancelled"}:
            raise RuntimeError(f"run {run_id} ended {run['status']}: {run}")
        time.sleep(0.05)
    raise TimeoutError(f"run {run_id} did not finish")


if __name__ == "__main__":
    raise SystemExit(main())
