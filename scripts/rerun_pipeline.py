#!/usr/bin/env python3
from __future__ import annotations

# ruff: noqa: E402
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, TextIO
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from datagraph.main import create_app
from datagraph.settings import Settings, load_settings

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class ApiClient:
    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def get(self, path: str) -> dict[str, Any]:
        raise NotImplementedError


class HttpApiClient(ApiClient):
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path, None)

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - local/operator URL.
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise RuntimeError(f"{method} {path} failed with {exc.code}: {detail}") from exc


class InProcessApiClient(ApiClient):
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    def get(self, path: str) -> dict[str, Any]:
        response = self.client.get(path)
        response.raise_for_status()
        return response.json()


def run_rerun_pipeline(
    api: ApiClient,
    *,
    graph_id: str,
    view_id: str,
    summarize: bool = False,
    representation: str = "raw",
    overrides: dict[str, Any] | None = None,
    base_viz_url: str = "http://127.0.0.1:8080",
    poll_interval: float = 0.25,
    timeout: float = 600,
    output: TextIO = sys.stdout,
) -> dict[str, Any]:
    overrides = overrides or {}
    rows: list[dict[str, Any]] = []

    if summarize:
        run = _start_and_wait(
            api,
            graph_id,
            "summarize",
            f"/api/graphs/{graph_id}/summarize",
            {"summarization": overrides.get("summarization", {})},
            poll_interval=poll_interval,
            timeout=timeout,
        )
        rows.append(run)

    embed_body = {"representation": representation, **overrides.get("embedding", {})}
    embed_run = _start_and_wait(
        api,
        graph_id,
        "embed",
        f"/api/graphs/{graph_id}/embeddings",
        embed_body,
        poll_interval=poll_interval,
        timeout=timeout,
    )
    rows.append(embed_run)

    cluster_run = _start_and_wait(
        api,
        graph_id,
        "cluster",
        f"/api/graphs/{graph_id}/views/{view_id}/cluster",
        {
            "embeddingRunId": embed_run["id"],
            "cluster": overrides.get("cluster", {}),
            "setDefault": True,
        },
        poll_interval=poll_interval,
        timeout=timeout,
    )
    rows.append(cluster_run)

    layout_run = _start_and_wait(
        api,
        graph_id,
        "layout",
        f"/api/graphs/{graph_id}/views/{view_id}/layout",
        {
            "embeddingRunId": embed_run["id"],
            "layout": overrides.get("layout", {}),
            "setDefault": True,
        },
        poll_interval=poll_interval,
        timeout=timeout,
    )
    rows.append(layout_run)

    label_run = _start_and_wait(
        api,
        graph_id,
        "label",
        f"/api/graphs/{graph_id}/views/{view_id}/label",
        {
            "clusterRunId": cluster_run["id"],
            "labeling": overrides.get("labeling", {}),
            "setDefault": True,
        },
        poll_interval=poll_interval,
        timeout=timeout,
    )
    rows.append(label_run)

    trend_body: dict[str, Any] = {"clusterRunId": cluster_run["id"], "setDefault": True}
    if "time" in overrides:
        trend_body["time"] = overrides["time"]
    if "window" in overrides:
        trend_body["window"] = overrides["window"]
    trend_run = _start_and_wait(
        api,
        graph_id,
        "trend",
        f"/api/graphs/{graph_id}/views/{view_id}/trends",
        trend_body,
        poll_interval=poll_interval,
        timeout=timeout,
    )
    rows.append(trend_run)

    viz_url = f"{base_viz_url.rstrip('/')}/?graphId={graph_id}&viewId={view_id}"
    _print_table(rows, viz_url, output)
    return {
        "graphId": graph_id,
        "viewId": view_id,
        "vizUrl": viz_url,
        "runs": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Rerun the Data Graph pipeline through the API.")
    parser.add_argument("--graph", required=True, help="Graph id to rerun.")
    parser.add_argument("--view", required=True, help="View id to rerun.")
    parser.add_argument("--base-url", help="Existing server URL. Omit to use an in-process app.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--representation", choices=["raw", "summary"], default="raw")
    parser.add_argument(
        "--config-json",
        default="{}",
        help="Override JSON object or path. Supports summarization, embedding, cluster, layout, "
        "labeling, time, and window keys.",
    )
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    args = parser.parse_args()

    overrides = _load_json_arg(args.config_json)
    base_viz_url = args.base_url or f"http://127.0.0.1:{args.port}"
    if args.base_url:
        result = run_rerun_pipeline(
            HttpApiClient(args.base_url),
            graph_id=args.graph,
            view_id=args.view,
            summarize=args.summarize,
            representation=args.representation,
            overrides=overrides,
            base_viz_url=base_viz_url,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
    else:
        env_settings = load_settings()
        settings = Settings(
            data_dir=args.data_dir,
            port=args.port,
            openai_api_key=env_settings.openai_api_key,
            read_only=env_settings.read_only,
            dist_dir=env_settings.dist_dir,
        )
        with TestClient(create_app(settings)) as client:
            result = run_rerun_pipeline(
                InProcessApiClient(client),
                graph_id=args.graph,
                view_id=args.view,
                summarize=args.summarize,
                representation=args.representation,
                overrides=overrides,
                base_viz_url=base_viz_url,
                poll_interval=args.poll_interval,
                timeout=args.timeout,
            )
    return 0 if all(row["status"] == "succeeded" for row in result["runs"]) else 1


def _start_and_wait(
    api: ApiClient,
    graph_id: str,
    stage: str,
    path: str,
    body: dict[str, Any],
    *,
    poll_interval: float,
    timeout: float,
) -> dict[str, Any]:
    created = api.post(path, body)
    run_id = created["id"]
    run = _poll_run(api, graph_id, run_id, poll_interval=poll_interval, timeout=timeout)
    return {"stage": stage, **run}


def _poll_run(
    api: ApiClient,
    graph_id: str,
    run_id: str,
    *,
    poll_interval: float,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = api.get(f"/api/graphs/{graph_id}/runs/{run_id}")
        if last["status"] in TERMINAL_STATUSES:
            if last["status"] != "succeeded":
                raise RuntimeError(f"run {run_id} ended {last['status']}: {last}")
            return last
        time.sleep(poll_interval)
    raise TimeoutError(f"run {run_id} did not finish; last={last}")


def _print_table(rows: list[dict[str, Any]], viz_url: str, output: TextIO) -> None:
    print(
        "stage      runId                             status     "
        "stats                         tokenUsage",
        file=output,
    )
    for row in rows:
        stats = row.get("stats", {})
        token_usage = stats.get("tokenUsage") or row.get("tokenUsage") or {}
        print(
            f"{row['stage']:<10} {row['id']:<33} {row['status']:<10} "
            f"{_stats_summary(stats):<29} {_token_summary(token_usage)}",
            file=output,
        )
    print(f"vizUrl: {viz_url}", file=output)


def _stats_summary(stats: dict[str, Any]) -> str:
    keys = [
        "records",
        "embedded",
        "clusters",
        "labeled",
        "failed",
        "points",
        "buckets",
        "durationSeconds",
    ]
    parts = [f"{key}={stats[key]}" for key in keys if key in stats]
    return ", ".join(parts) if parts else "-"


def _token_summary(token_usage: dict[str, Any]) -> str:
    if not token_usage:
        return "-"
    return (
        f"prompt={token_usage.get('promptTokens', 0)}, "
        f"completion={token_usage.get('completionTokens', 0)}, "
        f"total={token_usage.get('totalTokens', 0)}"
    )


def _load_json_arg(value: str) -> dict[str, Any]:
    if value.lstrip().startswith("{"):
        parsed = json.loads(value)
    else:
        parsed = json.loads(Path(value).read_text())
    if not isinstance(parsed, dict):
        raise ValueError("--config-json must be a JSON object")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
