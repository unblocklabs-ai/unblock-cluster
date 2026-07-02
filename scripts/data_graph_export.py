#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path):
    if not path or not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_json(url, token):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise SystemExit(f"GET {url} failed: {error.code} {detail}") from error


def main():
    parser = argparse.ArgumentParser(description="Export Data Graph config, latest artifact, or bundle JSON.")
    parser.add_argument("graph_id")
    parser.add_argument("--base-url", default=os.environ.get("DATA_GRAPH_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--token", default=None)
    parser.add_argument("--kind", choices=["config", "artifact", "bundle"], default="bundle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    load_env(args.env)
    token = args.token or os.environ.get("DATA_GRAPH_API_TOKEN")
    if not token:
        raise SystemExit("Provide --token or set DATA_GRAPH_API_TOKEN in .env.")

    base_url = args.base_url.rstrip("/")
    graph = get_json(f"{base_url}/api/data-graph/{args.graph_id}", token)
    artifact = None
    if args.kind in {"artifact", "bundle"}:
        artifact = get_json(f"{base_url}/api/data-graph/{args.graph_id}/artifact/latest", token)

    if args.kind == "config":
        payload = graph["config"]
    elif args.kind == "artifact":
        payload = artifact
    else:
        payload = {"graph": graph, "artifact": artifact}

    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
